"""HEALTHZ, DEEP_PROBE and TLS_EXPIRY (design 4.1, 4.2, 4.6). All traffic goes through net.fetch."""
from __future__ import annotations

import json
import time
from typing import Callable
from urllib.parse import urlsplit

from .. import net, secrets
from ..config import App, Credentials
from .base import CheckResult

RETRY_DELAY_S = 3.0
REDIRECTS = (301, 302, 303, 307, 308)
VERSION_FIELDS = ("version", "build", "builtAt", "commit", "commitFull")
AUTH_REJECTED_TEXT = ("service token refused: either the app's auth broke (jobs 2026-09-20 pattern) "
                      "or the token expired or was removed from the policy; check the token first")

Fetch = Callable[..., net.Response]


def is_access_redirect(resp: net.Response) -> bool:
    if resp.status not in REDIRECTS:
        return False
    loc = resp.headers.get("location", "")
    try:
        host = (urlsplit(loc).hostname or "").lower()
    except ValueError:
        return False
    return host == "cloudflareaccess.com" or host.endswith(".cloudflareaccess.com")


def _request(url: str, headers: dict, allow_private: bool, timeout_s: float, fetch: Fetch,
             sleep: Callable[[float], None], allow_loopback: bool = False) -> net.Response | net.FetchError:
    """One request, plus one retry after 3 s on connect error, timeout or 5xx."""
    last: net.Response | net.FetchError | None = None
    for attempt in range(2):
        try:
            extra = {"allow_loopback": True} if allow_loopback else {}
            last = fetch(url, headers=headers, allow_private=allow_private, timeout_s=timeout_s, **extra)
            if last.status < 500:
                return last
        except net.FetchError as e:
            last = e
            if e.kind not in ("CONNECT", "TIMEOUT", "PROTOCOL", "DNS"):
                return e
        if attempt == 0:
            sleep(RETRY_DELAY_S)
    assert last is not None
    return last


def _fetch_error_result(e: net.FetchError) -> CheckResult:
    """Map a transport failure. data["net_error"] marks failures of the network path itself (used to
    tell 'monitor offline' from 'app down' when every app fails at once)."""
    nd = {"net_error": e.kind}
    if e.kind == "BLOCKED":
        return CheckResult("unknown", "BLOCKED_TARGET", f"refused by the SSRF guard: {e.message}")
    if e.kind == "BAD_URL":
        return CheckResult("unknown", "BAD_URL", e.message)
    if e.kind == "TIMEOUT":
        return CheckResult("crit", "TIMEOUT", e.message, nd)
    if e.kind == "TLS":
        return CheckResult("crit", "TLS_ERROR", e.message)
    if e.kind == "TOO_LARGE":
        return CheckResult("crit", "BAD_BODY", e.message)
    return CheckResult("crit", "DOWN", e.message, nd)


def _same_type_equal(a, b) -> bool:
    return type(a) is type(b) and a == b


def healthz(app: App, fetch: Fetch = net.fetch, sleep: Callable[[float], None] = time.sleep) -> CheckResult:
    h = app.health
    if h is None:
        return CheckResult("unknown", "NOT_CONFIGURED", "no health block")
    r = _request(h.url, {"Accept": "application/json"}, h.allow_private, h.timeout_s, fetch, sleep, h.allow_loopback)
    if isinstance(r, net.FetchError):
        return _fetch_error_result(r)
    if is_access_redirect(r):
        return CheckResult("unknown", "ACCESS_REDIRECT",
                           "the URL is gated by Cloudflare Access and no service token was sent; "
                           "bypass this path in Access or add a version block with auth: access",
                           {"http": r.status})
    if r.status >= 500:
        return CheckResult("crit", "DOWN", f"HTTP {r.status} in {r.elapsed_ms}ms", {"http": r.status})
    if r.status != h.expect_status:
        return CheckResult("crit", "BAD_STATUS", f"HTTP {r.status}, expected {h.expect_status}", {"http": r.status})
    if h.expect_json:
        try:
            doc = json.loads(r.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return CheckResult("crit", "BAD_BODY", "body is not JSON but expect_json is set", {"http": r.status})
        if not isinstance(doc, dict):
            return CheckResult("crit", "BAD_BODY", "JSON body is not an object", {"http": r.status})
        wrong = [k for k, v in h.expect_json.items() if k not in doc or not _same_type_equal(doc[k], v)]
        if wrong:
            return CheckResult("crit", "JSON_MISMATCH", "unexpected value for key(s): " + ", ".join(sorted(map(str, wrong))),
                               {"http": r.status})
    return CheckResult("ok", "OK", f"HTTP {r.status} in {r.elapsed_ms}ms", {"http": r.status, "ms": r.elapsed_ms})


def _access_headers(app: App, creds: Credentials, get_secret: Callable[[str], str | None]):
    """(headers, missing_env_names). Headers only for https URLs of apps with auth: access."""
    v = app.version
    if v is None or v.auth != "access" or not v.url.startswith("https://"):
        return {}, []
    names = [creds.access_client_id_env, creds.access_client_secret_env]
    vals = [get_secret(n) if n else None for n in names]
    missing = [n or "(unconfigured)" for n, val in zip(names, vals) if not val]
    if missing:
        return {}, missing
    return {"CF-Access-Client-Id": vals[0], "CF-Access-Client-Secret": vals[1]}, []


def deep_probe(app: App, creds: Credentials, fetch: Fetch = net.fetch,
               sleep: Callable[[float], None] = time.sleep,
               get_secret: Callable[[str], str | None] = secrets.get) -> CheckResult:
    v = app.version
    if v is None:
        return CheckResult("unknown", "NOT_CONFIGURED", "no version block")
    headers, missing = _access_headers(app, creds, get_secret)
    if missing:
        return CheckResult("unknown", "NO_CREDENTIALS", "env var not set: " + ", ".join(missing))
    headers = {"Accept": "application/json", **headers}
    r = _request(v.url, headers, v.allow_private, v.timeout_s, fetch, sleep, v.allow_loopback)
    if isinstance(r, net.FetchError):
        return _fetch_error_result(r)
    if r.status in (401, 403) or is_access_redirect(r):
        return CheckResult("crit", "AUTH_REJECTED", AUTH_REJECTED_TEXT, {"http": r.status})
    if r.status >= 500:
        return CheckResult("crit", "DOWN", f"HTTP {r.status} from the version URL", {"http": r.status})
    if r.status != 200:
        return CheckResult("crit", "BAD_STATUS", f"HTTP {r.status}, expected 200", {"http": r.status})
    try:
        doc = json.loads(r.body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return CheckResult("crit", "BAD_BODY", "version URL did not return JSON", {"http": 200})
    if not isinstance(doc, dict):
        return CheckResult("crit", "BAD_BODY", "version JSON is not an object", {"http": 200})
    data: dict = {"http": 200, "ms": r.elapsed_ms}
    for k in VERSION_FIELDS:
        val = doc.get(k)
        if isinstance(val, str):
            data[k] = val[:80]
        elif val is None and k in doc:
            data[k] = None
    return CheckResult("ok", "OK", f"HTTP 200 in {r.elapsed_ms}ms", data)


def tls_expiry(app: App, now: float | None = None, cert_fn: Callable[..., tuple] = net.peer_cert) -> CheckResult:
    now = time.time() if now is None else now
    host_port = app.tls.host
    allow_private = bool(app.health and app.health.allow_private)
    if host_port:
        url = f"https://{host_port}/"
    elif app.health and app.health.url.startswith("https://"):
        url = app.health.url
    else:
        return CheckResult("unknown", "NOT_CONFIGURED", "no https URL or tls.host to check")
    try:
        lo = bool(app.health and app.health.allow_loopback)
        ip, not_after = cert_fn(url, allow_private=allow_private, **({"allow_loopback": True} if lo else {}))
    except net.FetchError as e:
        if e.kind == "BLOCKED":
            return CheckResult("unknown", "BLOCKED_TARGET", f"refused by the SSRF guard: {e.message}")
        if e.kind == "TIMEOUT":
            return CheckResult("crit", "TIMEOUT", e.message)
        return CheckResult("crit", "TLS_ERROR", e.message)
    if not_after is None:
        return CheckResult("unknown", "NO_CERT_DATE", "handshake succeeded but no certificate expiry was readable")
    days = (not_after - now) / 86400
    data = {"days": round(days, 1), "not_after": int(not_after)}
    if days < app.tls.crit_days:
        return CheckResult("crit", "EXPIRES_SOON", f"certificate expires in {days:.1f} days", data)
    if days < app.tls.warn_days:
        return CheckResult("warn", "EXPIRES_SOON", f"certificate expires in {days:.1f} days", data)
    return CheckResult("ok", "OK", f"certificate valid for {days:.0f} more days", data)

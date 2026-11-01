import json
import socket
import time
from functools import partial

import pytest

from fleet_truth import config, net, secrets
from fleet_truth.checks import http as H
from fleet_truth.checks.base import CheckResult
from tests.fakes import FakeServer, client_ctx, drip_body, make_cert

LO = net.TestHooks(allow_loopback=True)
CREDS = config.Credentials(access_client_id_env="FLEET_CF_ID", access_client_secret_env="FLEET_CF_SECRET")
SECRET = "SEKRIT-cf-secret-value"


def mkapp(health=None, version=None, tls=None, creds=True):
    d = {"apps": [{"name": "a"}]}
    if health:
        d["apps"][0]["health"] = health
    if version:
        d["apps"][0]["version"] = version
    if tls:
        d["apps"][0]["tls"] = tls
    if creds:
        d["credentials"] = {"access_client_id_env": "FLEET_CF_ID", "access_client_secret_env": "FLEET_CF_SECRET"}
    return config.parse(d).apps[0]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    secrets.register("FLEET_CF_ID", "FLEET_CF_SECRET")
    monkeypatch.setenv("FLEET_CF_ID", "id-SEKRIT-1234")
    monkeypatch.setenv("FLEET_CF_SECRET", SECRET)


def fetcher(resolver=None, hooks=LO):
    return partial(net.fetch, resolver=resolver, _hooks=hooks)


def json_server(doc, status=200):
    return FakeServer(lambda r: (status, {"Content-Type": "application/json"}, json.dumps(doc).encode()))


def sleeper():
    calls = []
    return calls, calls.append


# ------------------------------------------------------------------ HEALTHZ
@pytest.mark.loopback
def test_healthz_ok_and_expect_json():
    srv = json_server({"ok": True})
    try:
        app = mkapp(health={"url": srv.url("/healthz"), "allow_private": True, "expect_json": {"ok": True}})
        r = H.healthz(app, fetcher())
        assert (r.status, r.verdict) == ("ok", "OK")
        bad = mkapp(health={"url": srv.url("/healthz"), "allow_private": True, "expect_json": {"ok": 1}})
        assert H.healthz(bad, fetcher()).verdict == "JSON_MISMATCH"  # True != 1: no loose equality
        miss = mkapp(health={"url": srv.url("/healthz"), "allow_private": True, "expect_json": {"nope": True}})
        assert H.healthz(miss, fetcher()).status == "crit"
    finally:
        srv.close()


@pytest.mark.loopback
def test_healthz_bad_status_and_5xx():
    srv = FakeServer(lambda r: (404, {}, b"no"))
    srv2 = FakeServer(lambda r: (503, {}, b"down"))
    try:
        calls, sl = sleeper()
        assert H.healthz(mkapp(health={"url": srv.url(), "allow_private": True}), fetcher(), sl).verdict == "BAD_STATUS"
        assert calls == []
        r = H.healthz(mkapp(health={"url": srv2.url(), "allow_private": True}), fetcher(), sl)
        assert (r.status, r.verdict) == ("crit", "DOWN")
        assert calls == [3.0] and len(srv2.requests) == 2  # exactly one retry after 3 s
    finally:
        srv.close(); srv2.close()


@pytest.mark.loopback
def test_retry_recovers_on_second_attempt_and_connect_error_retries_once():
    state = {"n": 0}

    def h(r):
        state["n"] += 1
        return (503, {}, b"") if state["n"] == 1 else (200, {}, b"{}")

    srv = FakeServer(h)
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    try:
        calls, sl = sleeper()
        assert H.healthz(mkapp(health={"url": srv.url(), "allow_private": True}), fetcher(), sl).status == "ok"
        assert calls == [3.0]
        calls2, sl2 = sleeper()
        r = H.healthz(mkapp(health={"url": f"http://127.0.0.1:{port}/", "allow_private": True}), fetcher(), sl2)
        assert (r.status, r.verdict) == ("crit", "DOWN") and calls2 == [3.0]
    finally:
        srv.close()


@pytest.mark.loopback
def test_access_redirect_is_unknown_for_healthz_and_auth_rejected_for_deep_probe_and_hop_not_requested():
    seen = []

    def resolver(host, port, type=0):
        seen.append(host)
        if host != "app.test":
            raise OSError("must not be looked up")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    srv = FakeServer(lambda r: (302, {"Location": "https://x.cloudflareaccess.com/cdn-cgi/access/login/app.test?kid=1"}, b""))
    try:
        base = f"http://app.test:{srv.port}"
        app = mkapp(health={"url": base + "/healthz", "allow_private": True},
                    version={"url": base + "/api/version", "allow_private": True})
        r = H.healthz(app, fetcher(resolver))
        assert (r.status, r.verdict) == ("unknown", "ACCESS_REDIRECT")
        r = H.deep_probe(app, CREDS, fetcher(resolver))
        assert (r.status, r.verdict) == ("crit", "AUTH_REJECTED")
        assert "token" in r.detail and "auth broke" in r.detail
        assert set(seen) == {"app.test"}, "the Access hop was never resolved or requested"
        assert len(srv.requests) == 2
    finally:
        srv.close()


@pytest.mark.loopback
def test_non_access_redirect_is_not_access_redirect():
    srv = FakeServer(lambda r: (302, {"Location": "https://evil.example.com/cloudflareaccess.com"}, b""))
    try:
        r = H.healthz(mkapp(health={"url": srv.url(), "allow_private": True}), fetcher())
        assert r.verdict == "BAD_STATUS"
    finally:
        srv.close()


@pytest.mark.loopback
def test_healthz_timeout_verdict():
    srv = FakeServer(lambda r: drip_body())
    try:
        t0 = time.monotonic()
        r = H.healthz(mkapp(health={"url": srv.url(), "allow_private": True, "timeout_s": 1}), fetcher(), lambda s: None)
        assert (r.status, r.verdict) == ("crit", "TIMEOUT") and time.monotonic() - t0 < 6
    finally:
        srv.close()


def test_healthz_blocked_target_is_unknown_not_crit():
    app = mkapp(health={"url": "https://169.254.169.254/healthz"})
    r = H.healthz(app, fetcher(hooks=None))
    assert (r.status, r.verdict) == ("unknown", "BLOCKED_TARGET")


# ------------------------------------------------------------------ DEEP_PROBE
@pytest.mark.loopback
def test_deep_probe_ok_keeps_only_version_fields():
    srv = json_server({"version": "1.2.3", "build": "abcd1234", "builtAt": "2026-11-01T10:00:00Z", "commit": "abc1234",
                       "extra": "x" * 5000, "token": "should-not-be-kept"})
    try:
        app = mkapp(version={"url": srv.url("/api/version"), "allow_private": True})
        r = H.deep_probe(app, CREDS, fetcher())
        assert (r.status, r.verdict) == ("ok", "OK")
        assert r.data["commit"] == "abc1234" and r.data["version"] == "1.2.3"
        assert "extra" not in r.data and "token" not in r.data
        assert len(json.dumps(r.data)) < 2048
    finally:
        srv.close()


@pytest.mark.loopback
@pytest.mark.parametrize("status,body,verdict", [
    (200, b"<html>login</html>", "BAD_BODY"), (200, b"[1,2]", "BAD_BODY"), (200, b"x" * 70000, "BAD_BODY"),
    (401, b"", "AUTH_REJECTED"), (403, b"forbidden", "AUTH_REJECTED"), (503, b"", "DOWN"), (404, b"", "BAD_STATUS"),
    (204, b"", "BAD_STATUS"),
])
def test_deep_probe_verdicts(status, body, verdict):
    srv = FakeServer(lambda r: (status, {}, body))
    try:
        app = mkapp(version={"url": srv.url(), "allow_private": True})
        r = H.deep_probe(app, CREDS, fetcher(), lambda s: None)
        assert (r.status, r.verdict) == ("crit", verdict)
    finally:
        srv.close()


@pytest.mark.loopback
def test_deep_probe_on_http_url_needs_no_credentials(monkeypatch):
    monkeypatch.delenv("FLEET_CF_SECRET")
    srv = json_server({})
    try:
        app = mkapp(version={"url": srv.url(), "allow_private": True, "auth": "access"})
        # http URL: the header would not be sent anyway, so no credential is required
        assert H.deep_probe(app, CREDS, fetcher()).status == "ok"
    finally:
        srv.close()


@pytest.mark.loopback
def test_access_headers_only_on_https_access_apps(tmp_path):
    cert = make_cert(tmp_path)
    tls = FakeServer(lambda r: (200, {}, b'{"version":"1"}'), tls=cert)
    plain = json_server({"version": "1"})
    resolver = lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]  # noqa: E731
    try:
        hooks = net.TestHooks(True, client_ctx(cert[0]))
        https_access = mkapp(version={"url": f"https://fleet.test:{tls.port}/api/version", "auth": "access"})
        assert H.deep_probe(https_access, CREDS, fetcher(resolver, hooks)).status == "ok"
        h = tls.requests[-1].headers
        assert h["cf-access-client-id"] == "id-SEKRIT-1234" and h["cf-access-client-secret"] == SECRET

        https_plain = mkapp(version={"url": f"https://fleet.test:{tls.port}/api/version"})
        H.deep_probe(https_plain, CREDS, fetcher(resolver, hooks))
        assert "cf-access" not in tls.requests[-1].raw.lower()

        http_access = mkapp(version={"url": plain.url("/api/version"), "allow_private": True, "auth": "access"})
        H.deep_probe(http_access, CREDS, fetcher())
        assert "cf-access" not in plain.requests[-1].raw.lower()
        H.healthz(mkapp(health={"url": plain.url(), "allow_private": True}), fetcher())
        assert "cf-access" not in plain.requests[-1].raw.lower()
    finally:
        tls.close(); plain.close()


def test_missing_access_env_names_reported_not_values(monkeypatch):
    monkeypatch.delenv("FLEET_CF_SECRET")
    app = mkapp(version={"url": "https://a.example.com/api/version", "auth": "access"})
    called = []
    r = H.deep_probe(app, CREDS, lambda *a, **k: called.append(1))
    assert (r.status, r.verdict) == ("unknown", "NO_CREDENTIALS")
    assert "FLEET_CF_SECRET" in r.detail and "FLEET_CF_ID" not in r.detail and not called


@pytest.mark.loopback
def test_detail_and_data_never_contain_header_values():
    """A hostile server echoes the secret headers back in every shape; none may reach the result."""
    def echo(r):
        leak = (r.headers.get("cf-access-client-secret", "") + r.headers.get("cf-access-client-id", "")).encode()
        return (200, {"X-Echo": leak.decode()}, json.dumps({"version": leak.decode(), "commit": "abc1234"}).encode())

    cases = [FakeServer(echo), FakeServer(lambda r: (503, {}, SECRET.encode())),
             FakeServer(lambda r: (403, {"X": SECRET}, SECRET.encode())), FakeServer(lambda r: (200, {}, SECRET.encode()))]
    try:
        for srv in cases:
            tlsless = mkapp(version={"url": srv.url(), "allow_private": True, "auth": "access"})
            # exercise the https path with headers via a custom fetch that always adds them
            def fetch(url, headers=None, **kw):
                return net.fetch(url, headers={**(headers or {}), "CF-Access-Client-Secret": SECRET,
                                               "CF-Access-Client-Id": "id-SEKRIT-1234"}, _hooks=LO, **kw)
            r = H.deep_probe(tlsless, CREDS, fetch, lambda s: None)
            blob = repr(r)
            assert SECRET not in blob and "id-SEKRIT-1234" not in blob
    finally:
        for s in cases:
            s.close()


def test_checkresult_scrubs_registered_secrets():
    r = CheckResult("crit", "X", f"boom {SECRET} end", {"k": f"v {SECRET}", "n": [SECRET]})
    assert SECRET not in repr(r) and "***" in r.detail


# ------------------------------------------------------------------ TLS_EXPIRY
@pytest.mark.loopback
@pytest.mark.parametrize("days,status", [(40, "ok"), (10, "warn"), (3, "crit")])
def test_tls_expiry_thresholds(tmp_path, days, status):
    cert = make_cert(tmp_path, days=days)
    srv = FakeServer(lambda r: (200, {}, b"{}"), tls=cert)
    try:
        resolver = lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]  # noqa: E731
        app = mkapp(health={"url": f"https://fleet.test:{srv.port}/healthz"})
        fn = partial(net.peer_cert, resolver=resolver, _hooks=net.TestHooks(True, client_ctx(cert[0])))
        r = H.tls_expiry(app, cert_fn=fn)
        assert r.status == status, r
        assert abs(r.data["days"] - days) < 1.5
    finally:
        srv.close()


@pytest.mark.loopback
def test_tls_expiry_verification_failure_is_crit(tmp_path):
    cert = make_cert(tmp_path)
    srv = FakeServer(lambda r: (200, {}, b"{}"), tls=cert)
    try:
        resolver = lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]  # noqa: E731
        app = mkapp(health={"url": f"https://fleet.test:{srv.port}/healthz"})
        r = H.tls_expiry(app, cert_fn=partial(net.peer_cert, resolver=resolver, _hooks=LO))
        assert (r.status, r.verdict) == ("crit", "TLS_ERROR")
        blocked = H.tls_expiry(mkapp(health={"url": "https://169.254.169.254/"}), cert_fn=partial(net.peer_cert))
        assert (blocked.status, blocked.verdict) == ("unknown", "BLOCKED_TARGET")
    finally:
        srv.close()

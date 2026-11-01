"""Alert channels and the dead-man ping (design 6.5, 10).

Senders sit behind one small interface (`Sender.send(AlertMessage) -> SendResult`). Telegram, ntfy
and webhook go through the pinned fetcher (public hosts only: allow_private is always False);
email goes through a pinned SMTP connection with STARTTLS. Credentials come from env vars named
in fleet.yaml and appear only where the protocol needs them (Telegram: the URL path sent to
api.telegram.org). Message text and stored errors are scrubbed and never include tokens.
"""
from __future__ import annotations

import json
import logging
import re
import smtplib
import socket
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Protocol
from urllib.parse import quote

from . import net, secrets
from .config import AlertCfg, Config
from .state import AlertDecision
from .store import Store

log = logging.getLogger("fleet_truth.alerts")
MAX_TEXT = 500
LEVEL_RANK = {"info": 0, "warn": 1, "crit": 2}
TELEGRAM_API = "https://api.telegram.org"
_TOKENISH = re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}|https?://[^/\s@]+@")
Fetch = Callable[..., net.Response]


@dataclass(frozen=True)
class AlertMessage:
    kind: str       # alert | reminder | recovered | blind | test
    app: str
    check: str
    status: str
    level: str
    verdict: str
    detail: str
    since: int
    consecutive: int
    text: str
    page_url: str


@dataclass(frozen=True)
class SendResult:
    ok: bool
    error: str = ""


class Sender(Protocol):
    name: str
    min_status: str

    def send(self, msg: AlertMessage) -> SendResult: ...


# ----------------------------------------------------------------------------- message text
def clean(text: str) -> str:
    """Scrub env secret values and token-shaped strings."""
    return _TOKENISH.sub("***", secrets.scrub(text))


def page_url(cfg: Config) -> str:
    host = "127.0.0.1" if cfg.web.bind in ("0.0.0.0", "::") else cfg.web.bind
    return f"http://{host}:{cfg.web.port}/"


def format_message(d: AlertDecision, url: str) -> AlertMessage:
    hhmm = time.strftime("%H:%M", time.localtime(d.since))
    n = f"{d.consecutive} check{'s' if d.consecutive != 1 else ''}"
    if d.kind == "recovered":
        head = f"[OK] {d.app} {d.check}: recovered"
        body = f"{d.verdict}"
    elif d.kind == "reset":
        head = "[RESET] Fleet Truth state was reset"
        body = d.detail
    elif d.kind == "blind":
        head = f"[BLIND] {d.app} {d.check}: monitor cannot judge this (not an app outage)"
        body = d.detail
    elif d.kind == "reminder":
        head = f"[{d.level.upper()}] still failing: {d.app} {d.check}"
        body = d.detail
    else:
        head = f"[{d.level.upper()}] {d.app} {d.check}"
        body = d.detail
    tail = f" (since {hhmm}, {n})\n{url}"
    room = MAX_TEXT - len(head) - len(tail) - 2
    body = clean(body)
    if len(body) > room:
        body = body[: max(room - 1, 0)] + "…"
    text = clean(f"{head}: {body}{tail}" if body else f"{head}{tail}")
    return AlertMessage(d.kind, d.app, d.check, d.status, d.level, d.verdict, clean(d.detail)[:300], d.since,
                        d.consecutive, text[:MAX_TEXT], url)


def test_message(url: str, version: str) -> AlertMessage:
    text = clean(f"[TEST] Fleet Truth {version} test alert: if you can read this, this channel works.\n{url}")
    return AlertMessage("test", "_test", "TEST", "ok", "crit", "TEST", "test alert", int(time.time()), 1, text, url)


# ----------------------------------------------------------------------------- senders
def _fail(e: Exception | str) -> SendResult:
    if isinstance(e, net.FetchError):
        return SendResult(False, clean(f"{e.kind}: {e.message}")[:200])
    return SendResult(False, clean(str(e) if isinstance(e, str) else type(e).__name__)[:200])


def _missing(*pairs) -> str | None:
    names = [n or "(unconfigured)" for n, v in pairs if not v]
    return ("env var not set: " + ", ".join(names)) if names else None


class _Base:
    def __init__(self, cfg: AlertCfg, name: str, fetch: Fetch, get_secret: Callable[[str], str | None]):
        self.cfg, self.name, self.min_status = cfg, name, cfg.min_status
        self.fetch, self.get = fetch, get_secret

    def _post(self, url: str, headers: dict, body: bytes, method: str = "POST") -> net.Response:
        return self.fetch(url, headers=headers, allow_private=False, method=method, body=body, timeout_s=10.0)


class TelegramSender(_Base):
    def send(self, msg: AlertMessage) -> SendResult:
        tok, chat = (self.get(self.cfg.bot_token_env) if self.cfg.bot_token_env else None,
                     self.get(self.cfg.chat_id_env) if self.cfg.chat_id_env else None)
        miss = _missing((self.cfg.bot_token_env, tok), (self.cfg.chat_id_env, chat))
        if miss:
            return SendResult(False, miss)
        try:
            r = self._post(f"{TELEGRAM_API}/bot{tok}/sendMessage", {"Content-Type": "application/json"},
                           json.dumps({"chat_id": chat, "text": msg.text, "disable_web_page_preview": True}).encode())
        except net.FetchError as e:
            return _fail(e)
        except Exception as e:  # noqa: BLE001
            return _fail(e)
        if r.status != 200:
            return SendResult(False, f"HTTP {r.status} from Telegram")
        try:
            ok = bool(json.loads(r.body.decode("utf-8")).get("ok"))
        except (ValueError, UnicodeDecodeError, AttributeError):
            ok = False
        return SendResult(ok, "" if ok else "Telegram answered ok=false")


class NtfySender(_Base):
    def send(self, msg: AlertMessage) -> SendResult:
        topic = self.get(self.cfg.topic_env) if self.cfg.topic_env else None
        token = self.get(self.cfg.token_env) if self.cfg.token_env else None
        miss = _missing((self.cfg.topic_env, topic))
        if self.cfg.token_env and not token:
            miss = (miss + "; " if miss else "env var not set: ") + self.cfg.token_env
        if miss:
            return SendResult(False, miss)
        headers = {"Title": "Fleet Truth", "Priority": "5" if msg.level == "crit" and msg.kind != "recovered" else "3",
                   "Content-Type": "text/plain; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            r = self._post(f"{(self.cfg.url or 'https://ntfy.sh').rstrip('/')}/{quote(topic, safe='')}", headers,
                           msg.text.encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return _fail(e)
        return SendResult(r.status == 200, "" if r.status == 200 else f"HTTP {r.status} from ntfy")


class WebhookSender(_Base):
    def send(self, msg: AlertMessage) -> SendResult:
        url = self.get(self.cfg.url_env) if self.cfg.url_env else None
        miss = _missing((self.cfg.url_env, url))
        if miss:
            return SendResult(False, miss)
        url = url.rstrip("/")
        try:
            if self.cfg.mode == "healthchecks":
                if msg.kind == "recovered" or msg.kind == "test":
                    target = url
                elif msg.level == "crit" and msg.kind in ("alert", "reminder"):
                    target = url + "/fail"
                else:
                    return SendResult(True, "")  # no mapping for warn/blind in healthchecks mode (see wiki)
                r = self._post(target, {"Content-Type": "text/plain; charset=utf-8"}, msg.text.encode("utf-8"))
            else:
                body = {"app": msg.app, "check": msg.check, "status": msg.status, "verdict": msg.verdict,
                        "detail": msg.detail, "since": msg.since, "url": msg.page_url, "kind": msg.kind}
                r = self._post(url, {"Content-Type": "application/json"}, json.dumps(body).encode())
        except Exception as e:  # noqa: BLE001
            return _fail(e)
        ok = 200 <= r.status < 300
        return SendResult(ok, "" if ok else f"HTTP {r.status} from webhook")


SMTP_TIMEOUT_S = 10.0


class _PinnedSMTP(smtplib.SMTP):
    """SMTP that connects to an already-validated IP while TLS still verifies the hostname."""

    def __init__(self, ips: list[str], timeout: float = SMTP_TIMEOUT_S):
        self._ips = ips
        super().__init__(timeout=timeout)  # explicit timeout: smtplib's default sentinel breaks settimeout()

    def connect(self, host="localhost", port=0, source_address=None):
        self._host = host  # starttls() verifies the certificate against this name
        return super().connect(host, port, source_address)

    def _get_socket(self, host, port, timeout):
        last = None
        for ip in self._ips:
            s = socket.socket(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(timeout)
            try:
                s.connect((ip, port))
                return s
            except OSError as e:
                s.close()
                last = e
        raise last or OSError("no address")


def _default_smtp_factory(host: str, port: int, resolver=None, hooks=None,
                          timeout: float = SMTP_TIMEOUT_S) -> smtplib.SMTP:
    import ipaddress
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    t = net.Target("smtp", host, port, "/", literal)
    ips = net.validate_addresses(t, False, resolver, hooks)
    smtp = _PinnedSMTP(ips, timeout)
    try:
        code, _msg = smtp.connect(host, port)
        if code != 220:  # smtplib.connect returns the greeting instead of raising on a refusal
            raise smtplib.SMTPConnectError(code, "bad greeting")
    except Exception:
        smtp.close()
        raise
    return smtp


def _smtp_failure(e: Exception) -> str:
    """Precise but secret-free: exception class plus the SMTP reply code when there is one."""
    if isinstance(e, net.FetchError):
        return f"{e.kind}: {e.message}"
    code = getattr(e, "smtp_code", None)
    return f"{type(e).__name__}" + (f" (SMTP {code})" if code else "")


class EmailSender:
    def __init__(self, cfg: AlertCfg, name: str, get_secret: Callable[[str], str | None],
                 smtp_factory: Callable[[str, int], smtplib.SMTP] = _default_smtp_factory,
                 ssl_context: Callable[[], ssl.SSLContext] = ssl.create_default_context):
        self.cfg, self.name, self.min_status = cfg, name, cfg.min_status
        self.get, self.smtp_factory, self.ctx = get_secret, smtp_factory, ssl_context

    def send(self, msg: AlertMessage) -> SendResult:
        user = self.get(self.cfg.username_env) if self.cfg.username_env else None
        pw = self.get(self.cfg.password_env) if self.cfg.password_env else None
        miss = None
        if self.cfg.username_env or self.cfg.password_env:
            miss = _missing((self.cfg.username_env, user), (self.cfg.password_env, pw))
        if miss:
            return SendResult(False, miss)
        m = EmailMessage()
        m["Subject"] = clean(msg.text.splitlines()[0])[:150]
        m["From"], m["To"] = self.cfg.from_addr, self.cfg.to_addr
        m.set_content(msg.text)
        try:
            smtp = self.smtp_factory(self.cfg.smtp_host, self.cfg.smtp_port)
            try:
                smtp.ehlo()
                smtp.starttls(context=self.ctx())  # no login or message before TLS is up
                smtp.ehlo()
                if user and pw:
                    smtp.login(user, pw)
                smtp.send_message(m)
            finally:
                try:
                    smtp.quit()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            return _fail(_smtp_failure(e))
        return SendResult(True)


def build_senders(cfg: Config, fetch: Fetch = net.fetch, get_secret: Callable[[str], str | None] = secrets.get,
                  smtp_factory: Callable | None = None) -> list:
    out: list = []
    counts: dict[str, int] = {}
    total = {t: sum(1 for a in cfg.alerts if a.type == t) for t in {a.type for a in cfg.alerts}}
    for a in cfg.alerts:
        counts[a.type] = counts.get(a.type, 0) + 1
        name = a.type if total[a.type] == 1 else f"{a.type}#{counts[a.type]}"
        if a.type == "telegram":
            out.append(TelegramSender(a, name, fetch, get_secret))
        elif a.type == "ntfy":
            out.append(NtfySender(a, name, fetch, get_secret))
        elif a.type == "webhook":
            out.append(WebhookSender(a, name, fetch, get_secret))
        elif a.type == "email":
            kw = {"smtp_factory": smtp_factory} if smtp_factory else {}
            out.append(EmailSender(a, name, get_secret, **kw))
    return out


# ----------------------------------------------------------------------------- dispatch
@dataclass(frozen=True)
class Delivery:
    decision: AlertDecision
    eligible: int
    delivered: int
    configured: int = 0

    @property
    def undelivered(self) -> bool:
        """No channel accepted it. A severity filtered out on purpose (warn on a crit-only channel) is not a
        failure; having no channel at all, or a monitor-blind alert nobody can receive, is."""
        if self.delivered > 0:
            return False
        return self.eligible > 0 or self.configured == 0 or self.decision.kind == "blind"


class Dispatcher:
    def __init__(self, senders: list, store: Store | None, url: str, now_fn: Callable[[], float] = time.time):
        self.senders, self.store, self.url, self.now_fn = senders, store, url, now_fn

    def _send(self, s, msg: AlertMessage) -> SendResult:
        try:
            res = s.send(msg)
        except Exception as e:  # noqa: BLE001 - a broken sender must not stop the others
            res = SendResult(False, type(e).__name__)
        err = clean(res.error)
        if not res.ok:
            log.warning("alert channel %s failed: %s", s.name, err)
        if self.store is not None:
            self.store.record_alert(msg.app, msg.check, msg.status, s.name, self.now_fn(), res.ok, err or None)
        return SendResult(res.ok, err)

    def dispatch(self, decisions: list[AlertDecision]) -> list[Delivery]:
        out = []
        for d in decisions:
            msg = format_message(d, self.url)
            eligible = [s for s in self.senders if LEVEL_RANK[d.level] >= LEVEL_RANK[s.min_status]]
            ok = sum(1 for s in eligible if self._send(s, msg).ok)
            out.append(Delivery(d, len(eligible), ok, len(self.senders)))
        return out

    def test_all(self, version: str) -> dict[str, SendResult]:
        msg = test_message(self.url, version)
        return {s.name: self._send(s, msg) for s in self.senders}  # ignores min_status: tests every channel

    def channels_healthy(self) -> bool:
        """True when at least one channel exists whose latest send did not fail (no send yet counts as healthy)."""
        if not self.senders or self.store is None:
            return bool(self.senders)
        last = self.store.last_alert_ok_by_channel()
        return any(last.get(s.name, True) for s in self.senders)


# ----------------------------------------------------------------------------- dead-man
@dataclass(frozen=True)
class DeadManResult:
    action: str   # pinged | fail_pinged | ping_error | withheld | skipped
    ok: bool = True
    error: str = ""


def dead_man_ping(cfg: Config, *, cycle_ok: bool, error_class: str | None, channels_healthy: bool,
                  fetch: Fetch = net.fetch, get_secret: Callable[[str], str | None] = secrets.get,
                  store: Store | None = None) -> DeadManResult:
    """Ping on a completed cycle with a healthy channel; `/fail` on a cycle error; otherwise stay silent
    so healthchecks.io raises the alarm."""
    name = cfg.dead_man.url_env
    url = get_secret(name) if name else None
    if not url:
        return DeadManResult("skipped", True, "dead_man url env var not set" if name else "no dead_man configured")
    url = url.rstrip("/")
    if cycle_ok and not channels_healthy:
        return DeadManResult("withheld", False, "every alert channel is failing: ping withheld on purpose")
    target = url if cycle_ok else url + "/fail"
    body = b"" if cycle_ok else (error_class or "cycle error").encode()[:100]
    try:
        r = fetch(target, headers={}, allow_private=False, method="POST" if body else "GET", body=body or None, timeout_s=10.0)
    except Exception as e:  # noqa: BLE001
        return DeadManResult("ping_error", False, _fail(e).error)
    ok = 200 <= r.status < 300
    if not ok:
        return DeadManResult("ping_error", False, f"HTTP {r.status}")
    if cycle_ok and store is not None:
        store.meta_set("last_dead_man_ok", str(int(time.time())))
    return DeadManResult("pinged" if cycle_ok else "fail_pinged", True)

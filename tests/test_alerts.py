import functools
import json
import logging
import socket
import time

import pytest

from fleet_truth import alerts, cli, config, cycle, net, runtime, secrets
from fleet_truth.checks.base import CheckResult
from fleet_truth.state import AlertDecision
from fleet_truth.store import Store
from tests.fakes import FakeServer, client_ctx, make_cert

TOKEN = "123456789:SEKRIT-telegram-token-abcdefghijk"
LO = net.TestHooks(allow_loopback=True)


class Stub:
    """Stub transport: records every request, returns/raises as scripted."""

    def __init__(self, status=200, body=b'{"ok": true}', raises=None):
        self.calls, self.status, self.body, self.raises = [], status, body, raises

    def __call__(self, url, headers=None, allow_private=False, method="GET", body=None, timeout_s=10.0, **kw):
        self.calls.append({"url": url, "headers": dict(headers or {}), "allow_private": allow_private,
                           "method": method, "body": body})
        if self.raises:
            raise self.raises
        return net.Response(self.status, {}, self.body, "1.2.3.4", None, 1)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    for k, v in {"TG_TOKEN": TOKEN, "TG_CHAT": "-100777", "NTFY_TOPIC": "sekrit-topic-xyz", "NTFY_TOK": "tk_SEKRIT_ntfy",
                 "HOOK_URL": "https://hc.example.com/ping/SEKRIT-uuid", "DM_URL": "https://hc.example.com/ping/SEKRIT-dm",
                 "SMTP_USER": "mailer", "SMTP_PW": "SEKRIT-smtp-pw"}.items():
        monkeypatch.setenv(k, v)


def cfg_with(*alert_cfgs, dead_man=True, tmp=None):
    d = {"apps": [{"name": "a", "health": {"url": "https://a.example.com/healthz"}}], "alerts": list(alert_cfgs)}
    if dead_man:
        d["dead_man"] = {"url_env": "DM_URL"}
    if tmp:
        d["data_dir"] = str(tmp)
    return config.parse(d)


TG = {"type": "telegram", "bot_token_env": "TG_TOKEN", "chat_id_env": "TG_CHAT", "min_status": "crit"}
NTFY = {"type": "ntfy", "topic_env": "NTFY_TOPIC", "token_env": "NTFY_TOK", "min_status": "warn"}


def decision(kind="alert", level="crit", app="jobs", check="DEEP_PROBE", detail="503 from /api/version", n=2, since=None):
    return AlertDecision(kind, app, check, level if kind != "recovered" else "ok", level, "DOWN", detail,
                         since or int(time.time()) - 600, n)


# ------------------------------------------------------------------ message text
def test_message_shape_and_length_cap():
    m = alerts.format_message(decision(), "http://127.0.0.1:8790/")
    assert m.text.startswith("[CRIT] jobs DEEP_PROBE: 503 from /api/version (since ")
    assert "2 checks" in m.text and m.text.endswith("http://127.0.0.1:8790/")
    long = alerts.format_message(decision(detail="x" * 3000), "http://127.0.0.1:8790/")
    assert len(long.text) <= 500 and long.text.endswith("http://127.0.0.1:8790/")
    rec = alerts.format_message(decision(kind="recovered", level="crit"), "u")
    assert rec.text.startswith("[OK] jobs DEEP_PROBE: recovered")
    blind = alerts.format_message(decision(kind="blind", level="warn", detail="check raised ValueError"), "u")
    assert blind.text.startswith("[BLIND]") and "not an app outage" in blind.text


def test_message_never_contains_tokens():
    secrets.register("TG_TOKEN")
    d = decision(detail=f"boom {TOKEN} and https://u:pw@host/x and bot123456:ABCDEFGHIJKLMNOPQRSTUVWX")
    m = alerts.format_message(d, "http://127.0.0.1:8790/")
    for needle in (TOKEN, "u:pw@", "ABCDEFGHIJKLMNOPQRSTUVWX"):
        assert needle not in m.text and needle not in m.detail


# ------------------------------------------------------------------ senders (stub transport)
def test_telegram_request_and_token_only_in_url_path():
    cfg = cfg_with(TG)
    stub = Stub()
    (s,) = alerts.build_senders(cfg, stub)
    res = s.send(alerts.format_message(decision(), "http://127.0.0.1:8790/"))
    assert res.ok
    c = stub.calls[0]
    assert c["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert c["method"] == "POST" and c["allow_private"] is False
    body = json.loads(c["body"])
    assert body["chat_id"] == "-100777" and body["text"].startswith("[CRIT] jobs DEEP_PROBE")
    assert TOKEN not in c["body"].decode() and TOKEN not in json.dumps(c["headers"])


def test_telegram_failures_never_leak_token_into_error_or_logs(caplog, tmp_path):
    cfg = cfg_with(TG, tmp=tmp_path)
    store = Store(tmp_path / "t.db")
    hostile = net.FetchError("CONNECT", f"cannot connect to api.telegram.org/bot{TOKEN}/sendMessage")
    for stub in (Stub(raises=hostile), Stub(status=401, body=b'{"ok": false}'), Stub(raises=RuntimeError(TOKEN)),
                 Stub(body=b"not json")):
        disp = alerts.Dispatcher(alerts.build_senders(cfg, stub), store, "u")
        with caplog.at_level(logging.DEBUG):
            out = disp.dispatch([decision()])
        assert out[0].undelivered
    rows = store.db.execute("SELECT error, ok FROM alerts").fetchall()
    assert len(rows) == 4 and all(r["ok"] == 0 and r["error"] for r in rows)
    blob = " ".join(r["error"] for r in rows) + caplog.text
    assert TOKEN not in blob and "SEKRIT-telegram" not in blob


def test_missing_env_names_are_reported_not_values(monkeypatch):
    monkeypatch.delenv("TG_CHAT")
    (s,) = alerts.build_senders(cfg_with(TG), Stub())
    r = s.send(alerts.format_message(decision(), "u"))
    assert not r.ok and "TG_CHAT" in r.error and "TG_TOKEN" not in r.error and TOKEN not in r.error


def test_ntfy_request():
    stub = Stub(body=b"{}")
    (s,) = alerts.build_senders(cfg_with(NTFY), stub)
    assert s.send(alerts.format_message(decision(), "u")).ok
    c = stub.calls[0]
    assert c["url"] == "https://ntfy.sh/sekrit-topic-xyz" and c["method"] == "POST"
    assert c["headers"]["Priority"] == "5" and c["headers"]["Authorization"] == "Bearer tk_SEKRIT_ntfy"
    assert c["body"].decode().startswith("[CRIT] jobs")
    s.send(alerts.format_message(decision(level="warn"), "u"))
    assert stub.calls[1]["headers"]["Priority"] == "3"


def test_ntfy_without_token_sends_no_auth_header():
    cfg = cfg_with({"type": "ntfy", "topic_env": "NTFY_TOPIC"})
    stub = Stub(body=b"{}")
    (s,) = alerts.build_senders(cfg, stub)
    s.send(alerts.format_message(decision(), "u"))
    assert "Authorization" not in stub.calls[0]["headers"]


def test_webhook_json_and_healthchecks_modes():
    cfg = cfg_with({"type": "webhook", "url_env": "HOOK_URL"})
    stub = Stub(body=b"OK")
    (s,) = alerts.build_senders(cfg, stub)
    assert s.send(alerts.format_message(decision(), "http://127.0.0.1:8790/")).ok
    body = json.loads(stub.calls[0]["body"])
    assert set(body) == {"app", "check", "status", "verdict", "detail", "since", "url", "kind"}
    assert stub.calls[0]["url"] == "https://hc.example.com/ping/SEKRIT-uuid"

    hc = cfg_with({"type": "webhook", "url_env": "HOOK_URL", "mode": "healthchecks"})
    stub = Stub(body=b"OK")
    (s,) = alerts.build_senders(hc, stub)
    s.send(alerts.format_message(decision(), "u"))
    s.send(alerts.format_message(decision(kind="recovered", level="crit"), "u"))
    s.send(alerts.format_message(decision(level="warn"), "u"))  # warn: informational /log, a real call
    assert [c["url"] for c in stub.calls] == ["https://hc.example.com/ping/SEKRIT-uuid/fail",
                                              "https://hc.example.com/ping/SEKRIT-uuid",
                                              "https://hc.example.com/ping/SEKRIT-uuid/log"]


class FakeSMTP:
    log = []

    def __init__(self, host, port):
        FakeSMTP.log = [("connect", host, port)]

    def ehlo(self):
        FakeSMTP.log.append(("ehlo",))

    def starttls(self, context=None):
        FakeSMTP.log.append(("starttls", context is not None))

    def login(self, u, p):
        FakeSMTP.log.append(("login", u, p))

    def send_message(self, m):
        FakeSMTP.log.append(("send", m["Subject"], m["To"], m.get_content()))

    def quit(self):
        FakeSMTP.log.append(("quit",))


def test_email_starttls_before_login_and_message_has_no_password():
    cfg = cfg_with({"type": "email", "smtp_host": "smtp.example.com", "from": "f@x.org", "to": "t@x.org",
                    "username_env": "SMTP_USER", "password_env": "SMTP_PW"})
    (s,) = alerts.build_senders(cfg, Stub(), smtp_factory=FakeSMTP)
    assert s.send(alerts.format_message(decision(), "u")).ok
    names = [e[0] for e in FakeSMTP.log]
    assert names == ["connect", "ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert FakeSMTP.log[0][1:] == ("smtp.example.com", 587) and FakeSMTP.log[2][1] is True
    send = FakeSMTP.log[5]
    assert send[1].startswith("[CRIT] jobs") and send[2] == "t@x.org" and "SEKRIT-smtp-pw" not in send[3]


def test_email_failure_reports_class_only():
    class Boom(FakeSMTP):
        def login(self, u, p):
            raise smtplib_err(f"auth failed for {p}")

    import smtplib
    smtplib_err = smtplib.SMTPAuthenticationError.__mro__[0] and (lambda m: smtplib.SMTPException(m))
    cfg = cfg_with({"type": "email", "smtp_host": "smtp.example.com", "from": "f@x.org", "to": "t@x.org",
                    "username_env": "SMTP_USER", "password_env": "SMTP_PW"})
    secrets.register("SMTP_PW")
    (s,) = alerts.build_senders(cfg, Stub(), smtp_factory=Boom)
    r = s.send(alerts.format_message(decision(), "u"))
    assert not r.ok and "SEKRIT-smtp-pw" not in r.error


# ------------------------------------------------------------------ filters, records, retry
def test_min_status_filter_and_recording(tmp_path):
    cfg = cfg_with(TG, NTFY, tmp=tmp_path)
    stub = Stub()
    store = Store(tmp_path / "t.db")
    disp = alerts.Dispatcher(alerts.build_senders(cfg, stub), store, "u")
    out = disp.dispatch([decision(level="warn"), decision(level="crit")])
    assert [(d.eligible, d.delivered) for d in out] == [(1, 1), (2, 2)]
    assert [c["url"].split("/")[2] for c in stub.calls] == ["ntfy.sh", "api.telegram.org", "ntfy.sh"]
    assert store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 3
    assert all(c["allow_private"] is False for c in stub.calls)


def test_recovery_goes_to_channels_that_heard_the_alert():
    cfg = cfg_with(TG, NTFY)
    stub = Stub()
    disp = alerts.Dispatcher(alerts.build_senders(cfg, stub), None, "u")
    out = disp.dispatch([decision(kind="recovered", level="warn")])  # was only a warn: telegram (crit) skipped
    assert (out[0].eligible, out[0].delivered) == (1, 1)


def test_one_broken_sender_does_not_stop_the_others():
    cfg = cfg_with(TG, NTFY)

    def fetch(url, **kw):
        if "telegram" in url:
            raise RuntimeError("boom")
        return net.Response(200, {}, b"{}", "1.1.1.1", None, 1)

    disp = alerts.Dispatcher(alerts.build_senders(cfg, fetch), None, "u")
    out = disp.dispatch([decision()])
    assert (out[0].eligible, out[0].delivered) == (2, 1) and not out[0].undelivered


def test_undelivered_alert_is_retried_next_cycle(tmp_path):
    cfg = cfg_with(NTFY, dead_man=False, tmp=tmp_path)
    cfg = config.parse({**{"apps": [{"name": "a", "health": {"url": "https://a.example.com/healthz"}}],
                           "alerts": [NTFY], "data_dir": str(tmp_path), "confirm": 1}})

    class R:
        def __init__(self, *a, **k): pass
        def run_app(self, app, now):
            yield "HEALTHZ", CheckResult("crit", "DOWN", "x")

    bad, good = Stub(status=500), Stub(body=b"{}")
    o1 = runtime.run_once(cfg, runner_factory=R, now=1000, fetch=bad)
    assert [d.undelivered for d in o1.deliveries] == [True]
    assert o1.store.get_state("a", "HEALTHZ").alerted_status is None  # rolled back
    o1.store.close()
    o2 = runtime.run_once(cfg, runner_factory=R, now=1300, fetch=good)
    assert [d.kind for d in o2.deliveries and [x.decision for x in o2.deliveries]] == ["alert"]
    assert len(good.calls) == 1
    o3 = runtime.run_once(cfg, runner_factory=R, now=1600, fetch=good)
    assert o3.deliveries == [] and len(good.calls) == 1
    o3.store.close()


# ------------------------------------------------------------------ dead-man
class R_crit:
    def __init__(self, *a, **k): pass
    def run_app(self, app, now):
        yield "HEALTHZ", CheckResult("crit", "DOWN", "x")


class R_ok(R_crit):
    def run_app(self, app, now):
        yield "HEALTHZ", CheckResult("ok", "OK")


def dm_cfg(tmp_path, *chan):
    return config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/healthz"}}],
                         "alerts": list(chan), "dead_man": {"url_env": "DM_URL"}, "data_dir": str(tmp_path), "confirm": 1})


def test_dead_man_pings_on_completed_cycle_with_healthy_channel(tmp_path):
    cfg = dm_cfg(tmp_path, NTFY)
    stub = Stub(body=b"{}")
    o = runtime.run_once(cfg, runner_factory=R_ok, now=1000, fetch=stub)
    assert o.dead_man.action == "pinged"
    assert [c["url"] for c in stub.calls] == ["https://hc.example.com/ping/SEKRIT-dm"]
    assert stub.calls[0]["allow_private"] is False and o.store.meta_get("last_dead_man_ok")
    o.store.close()


def test_all_channels_failing_withholds_the_dead_man_ping(tmp_path):
    cfg = dm_cfg(tmp_path, NTFY, TG)

    def fetch(url, **kw):
        if "hc.example.com" in url and not url.endswith("/fail"):
            raise AssertionError("the OK ping must not be sent")
        return net.Response(500 if "hc.example.com" not in url else 200, {}, b"", "1.1.1.1", None, 1)

    o = runtime.run_once(cfg, runner_factory=R_crit, now=1000, fetch=fetch)
    assert o.dead_man.action == "fail_pinged"  # nobody heard the alert: /fail, never the OK ping
    assert all(d.undelivered for d in o.deliveries)
    o.store.close()


def test_one_healthy_channel_is_enough_for_the_ping(tmp_path):
    cfg = dm_cfg(tmp_path, NTFY, TG)
    seen = []

    def fetch(url, **kw):
        seen.append(url)
        return net.Response(500 if "telegram" in url else 200, {}, b"{}", "1.1.1.1", None, 1)

    o = runtime.run_once(cfg, runner_factory=R_crit, now=1000, fetch=fetch)
    assert o.dead_man.action == "pinged" and seen[-1].endswith("SEKRIT-dm")
    o.store.close()


def test_no_channels_configured_withholds_ping(tmp_path):
    cfg = dm_cfg(tmp_path)
    stub = Stub()
    o = runtime.run_once(cfg, runner_factory=R_ok, now=1000, fetch=stub)
    assert o.dead_man.action == "withheld" and stub.calls == []
    o.store.close()


def test_cycle_error_pings_fail_endpoint_and_reraises(tmp_path, monkeypatch):
    cfg = dm_cfg(tmp_path, NTFY)
    stub = Stub()

    def boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(cycle, "run_cycle", boom)
    with pytest.raises(RuntimeError):
        runtime.run_once(cfg, runner_factory=R_ok, now=1000, fetch=stub)
    assert [c["url"] for c in stub.calls] == ["https://hc.example.com/ping/SEKRIT-dm/fail"]
    assert b"RuntimeError" in stub.calls[0]["body"] and b"exploded" not in stub.calls[0]["body"]


def test_no_dead_man_configured_or_env_unset_is_skipped(tmp_path, monkeypatch):
    monkeypatch.delenv("DM_URL")
    cfg = dm_cfg(tmp_path, NTFY)
    o = runtime.run_once(cfg, runner_factory=R_ok, now=1000, fetch=Stub(body=b"{}"))
    assert o.dead_man.action == "skipped" and TOKEN not in o.dead_man.error
    o.store.close()


# ------------------------------------------------------------------ test-alert subcommand
def test_test_alert_sends_one_test_message_to_every_channel(tmp_path, capsys, monkeypatch):
    stub = Stub()
    real = runtime.test_alerts
    monkeypatch.setattr(runtime, "test_alerts", lambda cfg, **kw: real(cfg, fetch=stub, **kw))
    f = tmp_path / "fleet.yaml"
    f.write_text("""apps: [{name: a, health: {url: 'https://a.example.com/healthz'}}]
alerts:
  - {type: telegram, bot_token_env: TG_TOKEN, chat_id_env: TG_CHAT, min_status: crit}
  - {type: ntfy, topic_env: NTFY_TOPIC, min_status: crit}
""")
    assert cli.main(["test-alert", "-c", str(f)]) == 0
    out = capsys.readouterr().out
    assert "telegram: ok" in out and "ntfy: ok" in out
    assert len(stub.calls) == 2
    assert json.loads(stub.calls[0]["body"])["text"].startswith("[TEST]")
    assert stub.calls[1]["body"].decode().startswith("[TEST]")
    assert TOKEN not in out


def test_test_alert_reports_failed_channel_and_exit_1(tmp_path, capsys, monkeypatch):
    stub = Stub(status=500)
    real = runtime.test_alerts
    monkeypatch.setattr(runtime, "test_alerts", lambda cfg, **kw: real(cfg, fetch=stub, **kw))
    monkeypatch.delenv("NTFY_TOPIC")
    f = tmp_path / "fleet.yaml"
    f.write_text("""apps: [{name: a, health: {url: 'https://a.example.com/healthz'}}]
alerts:
  - {type: telegram, bot_token_env: TG_TOKEN, chat_id_env: TG_CHAT}
  - {type: ntfy, topic_env: NTFY_TOPIC}
""")
    assert cli.main(["test-alert", "-c", str(f)]) == 1
    out = capsys.readouterr().out
    assert "telegram: failed" in out and "ntfy: failed (env var not set: NTFY_TOPIC)" in out
    assert TOKEN not in out


def test_test_alert_without_channels(tmp_path, capsys):
    f = tmp_path / "fleet.yaml"
    f.write_text("apps: [{name: a, health: {url: 'https://a.example.com/healthz'}}]\n")
    assert cli.main(["test-alert", "-c", str(f)]) == 1


# ------------------------------------------------------------------ through the real pinned fetcher
def test_default_transport_refuses_loopback_targets():
    cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}}],
                        "alerts": [{"type": "webhook", "url_env": "HOOK_URL"}]})
    import os
    os.environ["HOOK_URL"] = "https://127.0.0.1/hook"
    (s,) = alerts.build_senders(cfg)  # default net.fetch
    r = s.send(alerts.format_message(decision(), "u"))
    assert not r.ok and "BLOCKED" in r.error


@pytest.mark.loopback
def test_webhook_and_dead_man_through_pinned_tls_fetch(tmp_path, monkeypatch):
    cert = make_cert(tmp_path)
    srv = FakeServer(lambda r: (200, {}, b"OK"), tls=cert)
    resolver = lambda host, port, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]  # noqa: E731
    fetch = functools.partial(net.fetch, resolver=resolver, _hooks=net.TestHooks(True, client_ctx(cert[0])))
    monkeypatch.setenv("HOOK_URL", f"https://fleet.test:{srv.port}/hook/SEKRIT")
    monkeypatch.setenv("DM_URL", f"https://fleet.test:{srv.port}/ping/SEKRIT-dm")
    try:
        cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}}],
                            "alerts": [{"type": "webhook", "url_env": "HOOK_URL"}], "dead_man": {"url_env": "DM_URL"}})
        (s,) = alerts.build_senders(cfg, fetch)
        assert s.send(alerts.format_message(decision(), "u")).ok
        assert json.loads(srv.requests[0].body)["app"] == "jobs" and srv.requests[0].path == "/hook/SEKRIT"
        dm = alerts.dead_man_ping(cfg, cycle_ok=True, error_class=None, channels_healthy=True, fetch=fetch)
        assert dm.action == "pinged" and srv.requests[-1].path == "/ping/SEKRIT-dm" and srv.requests[-1].method == "GET"
        dm = alerts.dead_man_ping(cfg, cycle_ok=False, error_class="ValueError", channels_healthy=False, fetch=fetch)
        assert dm.action == "fail_pinged" and srv.requests[-1].path == "/ping/SEKRIT-dm/fail"
    finally:
        srv.close()


@pytest.mark.loopback
def test_alert_redirect_is_not_followed(tmp_path, monkeypatch):
    hop = FakeServer(lambda r: (200, {}, b"hop"))
    srv = FakeServer(lambda r: (302, {"Location": hop.url("/x")}, b""))
    monkeypatch.setenv("HOOK_URL", srv.url("/hook"))
    fetch = functools.partial(net.fetch, _hooks=LO)
    try:
        # plain http to loopback needs allow_private, which senders never pass: refused outright
        cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}}],
                            "alerts": [{"type": "webhook", "url_env": "HOOK_URL"}]})
        (s,) = alerts.build_senders(cfg, fetch)
        assert not s.send(alerts.format_message(decision(), "u")).ok
        assert not srv.requests and not hop.requests
    finally:
        srv.close(); hop.close()


def test_test_alert_result_restores_channel_health_for_the_dead_man(tmp_path, monkeypatch):
    cfg = dm_cfg(tmp_path, NTFY)
    o = runtime.run_once(cfg, runner_factory=R_crit, now=1000, fetch=Stub(status=500))
    assert o.deliveries and o.deliveries[0].undelivered
    o.store.close()
    o = runtime.run_once(cfg, runner_factory=R_ok, now=1500, fetch=Stub(status=500))
    assert o.dead_man.action == "withheld"  # clean cycle, but the only channel's last send failed
    o.store.close()
    ok = Stub(body=b"{}")
    res = runtime.test_alerts(cfg, fetch=ok)
    assert res["ntfy"].ok
    o2 = runtime.run_once(cfg, runner_factory=R_ok, now=2000, fetch=ok)
    assert o2.dead_man.action == "pinged"
    o2.store.close()


# ------------------------------------------------------------------ REAL code paths against loopback servers
from functools import partial  # noqa: E402

from tests.smtpstub import SMTPStub  # noqa: E402


def smtp_sender(port, cert, *, creds=True, timeout=3.0, host="mail.test"):
    d = {"type": "email", "smtp_host": host, "smtp_port": port, "from": "f@x.org", "to": "t@x.org"}
    if creds:
        d.update(username_env="SMTP_USER", password_env="SMTP_PW")
    cfg = cfg_with(d)
    resolver = lambda h, p, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))]  # noqa: E731
    factory = partial(alerts._default_smtp_factory, resolver=resolver, hooks=LO, timeout=timeout)
    ctx = lambda: client_ctx(cert[0])  # noqa: E731
    (s,) = [alerts.EmailSender(cfg.alerts[0], "email", secrets.get, smtp_factory=factory, ssl_context=ctx)]
    return s


@pytest.mark.loopback
def test_real_email_path_succeeds_with_starttls_then_login_then_message(tmp_path):
    cert = make_cert(tmp_path, "mail.test")
    stub = SMTPStub("ok", cert)
    try:
        r = smtp_sender(stub.port, cert).send(alerts.format_message(decision(), "http://127.0.0.1:8790/"))
        assert r.ok, r
        assert stub.auth and stub.auth[0][-1] == b"SEKRIT-smtp-pw"
        assert stub.messages and stub.messages[0][0] == "tls", "message must only travel after STARTTLS"
        assert "[CRIT] jobs DEEP_PROBE" in stub.messages[0][1]
        order = [e[1] for e in stub.events]
        assert order.index("STARTTLS") < order.index("AUTH") < order.index("DATA")
    finally:
        stub.close()


@pytest.mark.loopback
def test_real_email_path_failure_modes_are_precise_and_secret_free(tmp_path):
    cert = make_cert(tmp_path, "mail.test")
    msg = alerts.format_message(decision(), "u")
    secrets.register("SMTP_PW")
    # server without STARTTLS: refuse to send in clear
    stub = SMTPStub("no_starttls", cert)
    try:
        r = smtp_sender(stub.port, cert).send(msg)
        assert not r.ok and "SMTPNotSupportedError" in r.error
        assert not stub.messages and not stub.auth, "nothing may be sent or authenticated in clear"
    finally:
        stub.close()
    # refused connection
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    r = smtp_sender(port, cert).send(msg)
    assert not r.ok and "ConnectionRefusedError" in r.error
    # bad greeting
    stub = SMTPStub("bad_greeting", cert)
    try:
        r = smtp_sender(stub.port, cert).send(msg)
        assert not r.ok and "SMTPConnectError" in r.error and "554" in r.error
    finally:
        stub.close()
    # slow / silent server
    stub = SMTPStub("silent", cert)
    try:
        t0 = time.monotonic()
        r = smtp_sender(stub.port, cert, timeout=0.8).send(msg)
        assert not r.ok and time.monotonic() - t0 < 4
        assert "Timeout" in r.error or "timed out" in r.error or "SMTPServerDisconnected" in r.error
    finally:
        stub.close()
    # credentials rejected, DATA rejected
    for mode, name in (("auth_reject", "SMTPAuthenticationError"), ("data_reject", "SMTPDataError")):
        stub = SMTPStub(mode, cert)
        try:
            r = smtp_sender(stub.port, cert).send(msg)
            assert not r.ok and name in r.error and "SEKRIT-smtp-pw" not in r.error
        finally:
            stub.close()
    # untrusted certificate: STARTTLS handshake fails, nothing sent
    other = make_cert(tmp_path / "o", "mail.test")
    stub = SMTPStub("ok", cert)
    try:
        r = smtp_sender(stub.port, other).send(msg)
        assert not r.ok and "SSL" in r.error and not stub.messages
    finally:
        stub.close()


@pytest.mark.loopback
def test_real_email_factory_refuses_unvalidated_targets():
    with pytest.raises(net.FetchError) as e:
        alerts._default_smtp_factory("127.0.0.1", 25)  # no test hook: loopback is blocked
    assert e.value.kind == "BLOCKED"
    with pytest.raises(net.FetchError):
        alerts._default_smtp_factory("169.254.169.254", 25)


def https_fixture(tmp_path, host, handler):
    cert = make_cert(tmp_path, host)
    srv = FakeServer(handler, tls=cert)
    resolver = lambda h, p, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))]  # noqa: E731
    fetch = functools.partial(net.fetch, resolver=resolver, _hooks=net.TestHooks(True, client_ctx(cert[0])))
    return srv, fetch


@pytest.mark.loopback
def test_real_telegram_path(tmp_path, monkeypatch):
    srv, fetch = https_fixture(tmp_path, "api.telegram.org", lambda r: (200, {}, b'{"ok": true}'))
    try:
        monkeypatch.setattr(alerts, "TELEGRAM_API", f"https://api.telegram.org:{srv.port}")
        (s,) = alerts.build_senders(cfg_with(TG), fetch)
        assert s.send(alerts.format_message(decision(), "u")).ok
        req = srv.requests[0]
        assert req.path == f"/bot{TOKEN}/sendMessage" and req.method == "POST"
        assert json.loads(req.body)["chat_id"] == "-100777" and TOKEN not in req.body.decode()
        assert req.headers["host"].startswith("api.telegram.org")
        srv.requests.clear()
    finally:
        srv.close()
    srv, fetch = https_fixture(tmp_path / "b", "api.telegram.org", lambda r: (401, {}, b'{"ok": false}'))
    try:
        monkeypatch.setattr(alerts, "TELEGRAM_API", f"https://api.telegram.org:{srv.port}")
        (s,) = alerts.build_senders(cfg_with(TG), fetch)
        r = s.send(alerts.format_message(decision(), "u"))
        assert not r.ok and r.error == "HTTP 401 from Telegram" and TOKEN not in r.error
    finally:
        srv.close()


@pytest.mark.loopback
def test_real_ntfy_path(tmp_path):
    srv, fetch = https_fixture(tmp_path, "ntfy.test", lambda r: (200, {}, b"{}"))
    try:
        cfg = cfg_with({"type": "ntfy", "url": f"https://ntfy.test:{srv.port}", "topic_env": "NTFY_TOPIC", "token_env": "NTFY_TOK"})
        (s,) = alerts.build_senders(cfg, fetch)
        assert s.send(alerts.format_message(decision(), "u")).ok
        req = srv.requests[0]
        assert req.path == "/sekrit-topic-xyz" and req.headers["authorization"] == "Bearer tk_SEKRIT_ntfy"
        assert req.headers["priority"] == "5" and req.body.decode().startswith("[CRIT]")
    finally:
        srv.close()
    srv, fetch = https_fixture(tmp_path / "b", "ntfy.test", lambda r: (403, {}, b"no"))
    try:
        cfg = cfg_with({"type": "ntfy", "url": f"https://ntfy.test:{srv.port}", "topic_env": "NTFY_TOPIC"})
        (s,) = alerts.build_senders(cfg, fetch)
        r = s.send(alerts.format_message(decision(), "u"))
        assert not r.ok and r.error == "HTTP 403 from ntfy"
    finally:
        srv.close()


@pytest.mark.loopback
def test_real_webhook_json_and_healthchecks_paths(tmp_path, monkeypatch):
    srv, fetch = https_fixture(tmp_path, "hook.test", lambda r: (200, {}, b"OK"))
    try:
        monkeypatch.setenv("HOOK_URL", f"https://hook.test:{srv.port}/ping/u-1")
        (s,) = alerts.build_senders(cfg_with({"type": "webhook", "url_env": "HOOK_URL"}), fetch)
        assert s.send(alerts.format_message(decision(), "u")).ok
        assert json.loads(srv.requests[0].body)["check"] == "DEEP_PROBE" and srv.requests[0].path == "/ping/u-1"
        (h,) = alerts.build_senders(cfg_with({"type": "webhook", "url_env": "HOOK_URL", "mode": "healthchecks"}), fetch)
        assert h.send(alerts.format_message(decision(), "u")).ok
        assert srv.requests[-1].path == "/ping/u-1/fail"
        assert h.send(alerts.format_message(decision(kind="recovered"), "u")).ok
        assert srv.requests[-1].path == "/ping/u-1"
    finally:
        srv.close()
    srv, fetch = https_fixture(tmp_path / "b", "hook.test", lambda r: (500, {}, b"x"))
    try:
        monkeypatch.setenv("HOOK_URL", f"https://hook.test:{srv.port}/p")
        (s,) = alerts.build_senders(cfg_with({"type": "webhook", "url_env": "HOOK_URL"}), fetch)
        assert s.send(alerts.format_message(decision(), "u")).error == "HTTP 500 from webhook"
    finally:
        srv.close()


# ------------------------------------------------------------------ review F5: blind alerts must reach somebody
class R_unknown:
    def __init__(self, *a, **k): pass
    def run_app(self, app, now):
        yield "HEALTHZ", CheckResult("unknown", "BLOCKED_TARGET", "x")


def test_blind_alert_is_crit_eligible_by_default_and_reaches_a_crit_only_channel(tmp_path):
    cfg = dm_cfg(tmp_path, TG)      # crit-only channel
    stub = Stub()
    o1 = runtime.run_once(cfg, runner_factory=R_unknown, now=1000, fetch=stub)
    o1.store.close()
    o2 = runtime.run_once(cfg, runner_factory=R_unknown, now=1000 + 31 * 60, fetch=stub)
    assert [d.decision.kind for d in o2.deliveries] == ["blind"] and o2.deliveries[0].decision.level == "crit"
    assert not o2.deliveries[0].undelivered and any("telegram" in c["url"] for c in stub.calls)
    assert o2.dead_man.action == "pinged"
    o2.store.close()


def test_blind_alert_nobody_can_receive_withholds_ok_ping_and_sends_fail(tmp_path):
    cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/healthz"}}], "alerts": [TG],
                        "dead_man": {"url_env": "DM_URL"}, "data_dir": str(tmp_path), "blind_level": "warn"})
    stub = Stub()
    runtime.run_once(cfg, runner_factory=R_unknown, now=1000, fetch=stub).store.close()
    o = runtime.run_once(cfg, runner_factory=R_unknown, now=1000 + 31 * 60, fetch=stub)
    assert o.deliveries[0].undelivered and o.dead_man.action == "fail_pinged"
    assert stub.calls[-1]["url"].endswith("/fail") and not [c for c in stub.calls if "telegram" in c["url"]]
    o.store.close()


def test_decision_with_no_channels_configured_is_undelivered_and_fails_the_ping(tmp_path):
    cfg = dm_cfg(tmp_path)
    stub = Stub()
    o = runtime.run_once(cfg, runner_factory=R_crit, now=1000, fetch=stub)
    assert o.deliveries[0].undelivered and o.dead_man.action == "fail_pinged"
    o.store.close()


def test_severity_filtered_warn_on_crit_only_channel_is_not_a_failure(tmp_path):
    cfg = dm_cfg(tmp_path, TG)

    class R_warn(R_crit):
        def run_app(self, app, now):
            yield "HEALTHZ", CheckResult("warn", "W", "x")

    stub = Stub()
    o = runtime.run_once(cfg, runner_factory=R_warn, now=1000, fetch=stub)
    assert o.deliveries and not o.deliveries[0].undelivered and o.dead_man.action == "pinged"
    o.store.close()


def test_blind_level_config():
    base = {"apps": [{"name": "a"}]}
    assert config.parse(base).blind_level == "crit"
    assert config.parse({**base, "blind_level": "warn"}).blind_level == "warn"
    with pytest.raises(config.ConfigError):
        config.parse({**base, "blind_level": "info"})


def test_each_scrub_layer_works_alone(tmp_path):
    secrets.register("TG_TOKEN")
    # layer 1: _fail() scrubs what a sender reports
    assert TOKEN not in alerts._fail(net.FetchError("CONNECT", f"x {TOKEN}")).error
    assert TOKEN not in alerts._fail(f"y {TOKEN}").error

    # layer 2: the Dispatcher scrubs before logging or storing, even if a sender forgot to
    class Careless:
        name, min_status = "careless", "info"

        def send(self, msg):
            return alerts.SendResult(False, f"raw error with {TOKEN}")

    store = Store(tmp_path / "t.db")
    out = alerts.Dispatcher([Careless()], store, "u")._send(Careless(), alerts.format_message(decision(), "u"))
    assert TOKEN not in out.error
    assert TOKEN not in store.db.execute("SELECT error FROM alerts").fetchone()[0]


# ------------------------------------------------------------------ review 2 R1 end to end
def test_crit_unknown_alternation_for_24h_reaches_a_crit_only_channel(tmp_path):
    cfg = dm_cfg(tmp_path, TG)
    seq = [CheckResult("crit", "TIMEOUT", "x"), CheckResult("unknown", "TIMEOUT_CYCLE", "y")]
    n = {"i": 0}

    class R:
        def __init__(self, *a, **k): pass
        def run_app(self, app, now):
            n["i"] += 1
            yield "HEALTHZ", seq[n["i"] % 2]

    stub = Stub()
    delivered = []
    for i in range(288):
        o = runtime.run_once(cfg, runner_factory=R, now=1_000_000 + i * 300, fetch=stub)
        delivered += [d for d in o.deliveries if d.delivered]
        o.store.close()
    tg = [c for c in stub.calls if "telegram" in c["url"]]
    assert len(tg) >= 4 and json.loads(tg[0]["body"])["text"].startswith("[CRIT] a HEALTHZ")


# ------------------------------------------------------------------ review 2 R2: healthchecks mode never no-ops
@pytest.mark.loopback
def test_healthchecks_mode_every_kind_is_a_real_http_call_and_counts_only_on_2xx(tmp_path, monkeypatch):
    srv, fetch = https_fixture(tmp_path, "hc.test", lambda r: (200, {}, b"OK"))
    try:
        monkeypatch.setenv("HOOK_URL", f"https://hc.test:{srv.port}/ping/base")
        cfg = cfg_with({"type": "webhook", "url_env": "HOOK_URL", "mode": "healthchecks", "min_status": "warn"})
        disp = alerts.Dispatcher(alerts.build_senders(cfg, fetch), None, "u")
        cases = [("alert", "crit"), ("reminder", "crit"), ("blind", "crit"), ("flapping", "crit"), ("reset", "crit"),
                 ("alert", "warn"), ("blind", "warn"), ("flapping", "warn")]
        decs = [decision(kind=k, level=lv) for k, lv in cases]
        out = disp.dispatch(decs)
        assert [o.delivered for o in out] == [1] * 8 and not any(o.undelivered for o in out)
        assert [r.path for r in srv.requests] == ["/ping/base/fail"] * 5 + ["/ping/base/log"] * 3
        assert all(r.method == "POST" and r.body.startswith(b"[") for r in srv.requests)
    finally:
        srv.close()
    # non-2xx is a failure, never a delivery
    srv, fetch = https_fixture(tmp_path / "b", "hc.test", lambda r: (500, {}, b"no"))
    try:
        monkeypatch.setenv("HOOK_URL", f"https://hc.test:{srv.port}/ping/base")
        disp = alerts.Dispatcher(alerts.build_senders(cfg, fetch), None, "u")
        out = disp.dispatch([decision(kind="blind", level="crit")])
        assert out[0].delivered == 0 and out[0].undelivered
    finally:
        srv.close()


@pytest.mark.loopback
def test_one_app_recovering_does_not_ping_success_on_the_shared_url_while_another_is_down(tmp_path, monkeypatch):
    srv, fetch = https_fixture(tmp_path, "hc.test", lambda r: (200, {}, b"OK"))
    try:
        monkeypatch.setenv("HOOK_URL", f"https://hc.test:{srv.port}/ping/base")
        cfg = cfg_with({"type": "webhook", "url_env": "HOOK_URL", "mode": "healthchecks", "min_status": "warn"})
        disp = alerts.Dispatcher(alerts.build_senders(cfg, fetch), None, "u")
        rec = decision(kind="recovered", level="crit", app="a")
        out = disp.dispatch([rec], cycle_clear=False, clear_apps={"a"})
        assert srv.requests == [] and out[0].delivered == 0 and out[0].skipped == 1
        assert not out[0].undelivered, "a deliberate withhold is neither a delivery nor a failure"
        disp.dispatch([rec], cycle_clear=True, clear_apps={"a", "b"})
        assert [r.path for r in srv.requests] == ["/ping/base"]
    finally:
        srv.close()


@pytest.mark.loopback
def test_per_app_healthchecks_urls(tmp_path, monkeypatch):
    srv, fetch = https_fixture(tmp_path, "hc.test", lambda r: (200, {}, b"OK"))
    try:
        monkeypatch.setenv("HOOK_URL", f"https://hc.test:{srv.port}/ping/base")
        monkeypatch.setenv("HC_JOBS", f"https://hc.test:{srv.port}/ping/jobs-uuid")
        cfg = cfg_with({"type": "webhook", "url_env": "HOOK_URL", "mode": "healthchecks", "min_status": "warn",
                        "app_url_envs": {"jobs": "HC_JOBS"}})
        disp = alerts.Dispatcher(alerts.build_senders(cfg, fetch), None, "u")
        disp.dispatch([decision(app="jobs"), decision(app="other"), decision(kind="recovered", level="crit", app="jobs")],
                      cycle_clear=False, clear_apps={"jobs"})
        assert [r.path for r in srv.requests] == ["/ping/jobs-uuid/fail", "/ping/base/fail", "/ping/jobs-uuid"]
    finally:
        srv.close()
    base = {"apps": [{"name": "a"}]}
    for bad in ({"jobs": "lowercase"}, {"Bad Name": "HC"}, "x"):
        with pytest.raises(config.ConfigError):
            config.parse({**base, "alerts": [{"type": "webhook", "url_env": "H", "app_url_envs": bad}]})
    monkeypatch.delenv("HC_JOBS")
    r = alerts.build_senders(cfg, Stub())[0].send(alerts.format_message(decision(app="jobs"), "u"))
    assert not r.ok and "HC_JOBS" in r.error


@pytest.mark.loopback
def test_runtime_computes_clear_flags_from_the_whole_cycle(tmp_path, monkeypatch):
    srv, fetch = https_fixture(tmp_path, "hc.test", lambda r: (200, {}, b"OK"))
    try:
        monkeypatch.setenv("HOOK_URL", f"https://hc.test:{srv.port}/ping/base")
        cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}},
                                     {"name": "b", "health": {"url": "https://b.example.com/"}}],
                            "alerts": [{"type": "webhook", "url_env": "HOOK_URL", "mode": "healthchecks"}],
                            "data_dir": str(tmp_path / "d"), "confirm": 1})
        phase = {"a": "crit", "b": "crit"}

        class R:
            def __init__(self, *x, **k): pass
            def run_app(self, app, now):
                yield "HEALTHZ", CheckResult(phase[app.name], "V", "d")

        runtime.run_once(cfg, runner_factory=R, now=1000, fetch=fetch, senders=None).store.close()
        phase["a"] = "ok"
        srv.requests.clear()
        for t in (1300, 1600):   # a recovers (two ok cycles); b is still crit
            runtime.run_once(cfg, runner_factory=R, now=t, fetch=fetch).store.close()
        assert "/ping/base" not in [r.path for r in srv.requests], "b is still crit: the shared URL must stay red"
        phase["b"] = "ok"
        for t in (1900, 2200):   # b needs two ok cycles too
            runtime.run_once(cfg, runner_factory=R, now=t, fetch=fetch).store.close()
        assert [r.path for r in srv.requests if r.path == "/ping/base"] == ["/ping/base"]
    finally:
        srv.close()


# ------------------------------------------------------------------ review 2 R3: suppression is capped and self-correcting
class R_fleet_down:
    ref = False

    def __init__(self, *a, **k): pass
    def reference_ok(self, now):
        return type(self).ref
    def run_app(self, app, now):
        yield "HEALTHZ", CheckResult("crit", "DOWN", "cannot connect", {"net_error": "CONNECT"})


def off_cfg(tmp_path, **kw):
    d = {"apps": [{"name": f"a{i}", "health": {"url": f"https://a{i}.example.com/healthz"}} for i in range(3)],
         "alerts": [NTFY], "data_dir": str(tmp_path), "confirm": 2, "reference_urls": ["https://reference.example.org/"]}
    d.update(kw)
    return config.parse(d)


def test_reference_down_for_a_day_stops_suppressing_after_the_monitor_alert_is_delivered(tmp_path):
    cfg = off_cfg(tmp_path)
    R_fleet_down.ref = False
    stub = Stub(body=b"{}")
    per_app, mon = [], []
    for i in range(288):
        o = runtime.run_once(cfg, runner_factory=R_fleet_down, now=1_000_000 + i * 300, fetch=stub)
        for d in o.deliveries:
            (mon if d.decision.app == "_monitor" else per_app).append((i, d.decision.app, d.decision.kind, d.delivered))
        o.store.close()
    assert mon[0][0] == 1 and mon[0][3] == 1, "the monitor-offline alert is sent on the first confirmed cycle"
    assert sorted({a for _, a, _, _ in per_app if _ == _}) == ["a0", "a1", "a2"], "per-app alerts must come out"
    assert min(i for i, *_ in per_app) == 2, "suppressed for exactly the cycle(s) before delivery was proven"
    st = runtime.Store(runtime.db_path(cfg)).get_state("_monitor", "NETWORK")
    assert st.verdict == "FLEET_OUTAGE" and st.status == "crit"


def test_undelivered_monitor_offline_alert_cannot_suppress_forever(tmp_path):
    cfg = off_cfg(tmp_path, offline_suppress_cycles=2)
    R_fleet_down.ref = False
    dead = Stub(status=500)
    seen = []
    for i in range(6):
        o = runtime.run_once(cfg, runner_factory=R_fleet_down, now=2_000_000 + i * 300, fetch=dead)
        seen.append(sorted({d.decision.app for d in o.deliveries}))
        o.store.close()
    assert seen[1] == ["_monitor"] and "a0" not in sum(seen[:2], [])   # capped at 2 cycles ...
    assert any("a0" in s for s in seen[2:]), "... then per-app alerts flow even though nothing was delivered"


def test_offline_cap_restarts_after_the_network_comes_back(tmp_path):
    cfg = off_cfg(tmp_path)
    stub = Stub(body=b"{}")
    for i, (ref, ok) in enumerate([(False, False)] * 3 + [(True, True)] * 2 + [(False, False)] * 2):
        R_fleet_down.ref = ref
        cls = R_ok if ok else R_fleet_down
        o = runtime.run_once(cfg, runner_factory=cls, now=3_000_000 + i * 300, fetch=stub)
        streak = o.store.meta_get("offline_streak")
        o.store.close()
    assert streak == "2"          # fresh cap in the second episode


def test_reference_urls_must_be_independent_of_the_fleet():
    base = {"apps": [{"name": "a", "health": {"url": "https://jobs.dftm.cloud/healthz"},
                      "version": {"url": "https://api.dftm.cloud/v"}}]}
    ok = config.parse({**base, "reference_urls": ["https://example.org/"]})
    assert ok.reference_urls
    for bad in ("https://jobs.dftm.cloud/", "https://status.dftm.cloud/", "https://dftm.cloud/", "https://API.DFTM.CLOUD./x"):
        with pytest.raises(config.ConfigError) as e:
            config.parse({**base, "reference_urls": [bad]})
        assert "independent of the fleet" in e.value.errors[0], bad
    with pytest.raises(config.ConfigError):
        config.parse({"apps": [{"name": "a", "tls": {"host": "x.dftm.cloud:8443"}}], "reference_urls": ["https://y.dftm.cloud/"]})
    assert config.parse({**base, "offline_suppress_cycles": 0}).offline_suppress_cycles == 0
    with pytest.raises(config.ConfigError):
        config.parse({**base, "offline_suppress_cycles": -1})


# ------------------------------------------------------------------ review 2 R5: undelivered reminders and flapping alerts are retried
def test_undelivered_reminder_is_retried_next_cycle_not_6h_later(tmp_path):
    cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/healthz"}}], "alerts": [NTFY],
                        "data_dir": str(tmp_path), "confirm": 1})
    good, bad = Stub(body=b"{}"), Stub(status=500)
    t0 = 1_000_000
    runtime.run_once(cfg, runner_factory=R_crit, now=t0, fetch=good).store.close()              # alert (delivered)
    o = runtime.run_once(cfg, runner_factory=R_crit, now=t0 + 6 * 3600, fetch=bad)             # reminder, channel down
    assert [d.decision.kind for d in o.deliveries] == ["reminder"] and o.deliveries[0].undelivered
    o.store.close()
    o = runtime.run_once(cfg, runner_factory=R_crit, now=t0 + 6 * 3600 + 300, fetch=good)      # retried right away
    assert [d.decision.kind for d in o.deliveries] == ["reminder"] and o.deliveries[0].delivered
    o.store.close()
    o = runtime.run_once(cfg, runner_factory=R_crit, now=t0 + 6 * 3600 + 600, fetch=good)      # and not again
    assert o.deliveries == []
    o.store.close()


def test_undelivered_flapping_alert_is_retried_next_cycle_not_6h_later(tmp_path):
    cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/healthz"}}], "alerts": [NTFY],
                        "data_dir": str(tmp_path), "confirm": 2, "flap": {"flips": 3, "cycles": 12, "remind_hours": 6}})
    n = {"i": 0}

    class R:
        def __init__(self, *a, **k): pass
        def run_app(self, app, now):
            n["i"] += 1
            yield "HEALTHZ", CheckResult("crit", "DOWN") if n["i"] % 2 else CheckResult("ok", "OK")

    bad, good = Stub(status=500), Stub(body=b"{}")
    got = []
    for i in range(12):
        o = runtime.run_once(cfg, runner_factory=R, now=1_000_000 + i * 300, fetch=bad if i < 4 else good)
        got += [(i, d.decision.kind, d.delivered) for d in o.deliveries if d.decision.kind == "flapping"]
        o.store.close()
    failed = [g for g in got if not g[2]]
    delivered = [g for g in got if g[2]]
    assert failed and delivered and delivered[0][0] == failed[-1][0] + 1, got
    assert len(delivered) == 1


# ------------------------------------------------------------------ review 2 R9: alert sending has a time budget
@pytest.mark.loopback
def test_slow_smtp_cannot_push_a_run_past_its_alert_budget(tmp_path):
    cert = make_cert(tmp_path, "mail.test")
    stub = SMTPStub("slowdrip", cert)
    try:
        cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}},
                                     {"name": "b", "health": {"url": "https://b.example.com/"}}],
                            "alerts": [{"type": "email", "smtp_host": "mail.test", "smtp_port": stub.port,
                                        "from": "f@x.org", "to": "t@x.org"}],
                            "data_dir": str(tmp_path / "d"), "confirm": 1, "alert_budget_seconds": 1})
        sender = smtp_sender(stub.port, cert, creds=False, timeout=30.0)   # per-operation timeout far above the budget
        t0 = time.monotonic()
        o = runtime.run_once(cfg, runner_factory=R_crit, now=1000, fetch=Stub(), senders=[sender])
        took = time.monotonic() - t0
        assert took < 4.0, took
        assert len(o.deliveries) == 2 and all(d.undelivered for d in o.deliveries)
        rows = [r["error"] for r in o.store.db.execute("SELECT error FROM alerts ORDER BY id")]
        assert "exceeded the alert time budget" in rows[0] and "budget exhausted" in rows[1]
        for app in ("a", "b"):   # both rolled back: retried next cycle
            assert o.store.get_state(app, "HEALTHZ").alerted_status is None
        o.store.close()
    finally:
        stub.close()


def test_budget_config_and_dispatcher_skips_remaining_after_exhaustion():
    base = {"apps": [{"name": "a"}]}
    assert config.parse(base).alert_budget_seconds == 60.0
    assert config.parse({**base, "alert_budget_seconds": 5}).alert_budget_seconds == 5.0
    with pytest.raises(config.ConfigError):
        config.parse({**base, "alert_budget_seconds": 0})

    class Slow:
        name, min_status = "slow", "info"

        def send(self, msg):
            time.sleep(0.6)
            return alerts.SendResult(True)

    disp = alerts.Dispatcher([Slow()], None, "u", budget_s=1.0)
    t0 = time.monotonic()
    out = disp.dispatch([decision(), decision(), decision()])
    assert time.monotonic() - t0 < 2.0
    assert [o.delivered for o in out] == [1, 0, 0]


def test_a_skipped_result_is_never_a_delivery_even_if_a_sender_marks_it_ok():
    class Sloppy:
        name, min_status = "sloppy", "info"

        def send(self, msg):
            return alerts.SendResult(True, "", skipped=True)

    out = alerts.Dispatcher([Sloppy()], None, "u").dispatch([decision(kind="blind", level="crit")])
    assert out[0].delivered == 0 and out[0].skipped == 1


def test_delivery_of_the_monitor_offline_alert_alone_ends_suppression(tmp_path):
    cfg = off_cfg(tmp_path, offline_suppress_cycles=50)     # the cap cannot be what ends it
    R_fleet_down.ref = False
    stub = Stub(body=b"{}")
    first_app_alert = None
    for i in range(6):
        o = runtime.run_once(cfg, runner_factory=R_fleet_down, now=1_000_000 + i * 300, fetch=stub)
        if first_app_alert is None and any(d.decision.app != "_monitor" for d in o.deliveries):
            first_app_alert = i
        o.store.close()
    assert first_app_alert == 2
    dead = Stub(status=500)
    cfg2 = off_cfg(tmp_path / "x", offline_suppress_cycles=50)
    for i in range(6):
        o = runtime.run_once(cfg2, runner_factory=R_fleet_down, now=1_000_000 + i * 300, fetch=dead)
        assert all(d.decision.app == "_monitor" for d in o.deliveries), "undelivered monitor alert: suppression continues up to the cap"
        o.store.close()

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
    s.send(alerts.format_message(decision(level="warn"), "u"))  # no mapping: nothing sent
    assert [c["url"] for c in stub.calls] == ["https://hc.example.com/ping/SEKRIT-uuid/fail",
                                              "https://hc.example.com/ping/SEKRIT-uuid"]


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
        if "hc.example.com" in url:
            raise AssertionError("dead-man must not be pinged")
        return net.Response(500, {}, b"", "1.1.1.1", None, 1)

    o = runtime.run_once(cfg, runner_factory=R_crit, now=1000, fetch=fetch)
    assert o.dead_man.action == "withheld"
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

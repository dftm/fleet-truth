"""Review 3 F4 (per-channel budgets, no blackout, no duplicate storm) and F1 (skipped is not a failure)."""
import json
import threading
import time

import pytest

from fleet_truth import alerts, config, runtime
from fleet_truth.checks.base import CheckResult
from fleet_truth.state import AlertDecision
from fleet_truth.store import Store
from tests.test_alerts import (NTFY, R_ok, TG, Stub, decision, dm_cfg, env, https_fixture, make_cert,  # noqa: F401
                               smtp_sender, SMTPStub)


class Chan:
    """A scripted channel: records every send; `delay` seconds per send, optional hang until released."""

    def __init__(self, name, min_status="info", delay=0.0, hang=None, ok=True):
        self.name, self.min_status, self.delay, self.hang, self.ok = name, min_status, delay, hang, ok
        self.sent, self.lock = [], threading.Lock()

    def send(self, msg):
        with self.lock:
            self.sent.append(msg.text)
        if self.hang is not None:
            self.hang.wait(30)
        elif self.delay:
            time.sleep(self.delay)
        return alerts.SendResult(self.ok)


def disp(tmp_path, chans, **kw):
    store = Store(tmp_path / "d.db")
    kw.setdefault("channel_budget_s", 0.5)
    kw.setdefault("budget_s", 5.0)
    return alerts.Dispatcher(chans, store, "u", **kw), store


def test_a_slow_channel_never_blocks_a_healthy_one_over_four_cycles(tmp_path):
    """The reviewer's probe: slow sender 2.5 s, budget 2 s, fast channel. Before: 0 deliveries, 4 duplicates."""
    slow, fast = Chan("slow", delay=2.5), Chan("fast")
    d, store = disp(tmp_path, [slow, fast], channel_budget_s=0.4)
    t0 = time.monotonic()
    outs = [d.dispatch([decision(app=f"a{i}")]) for i in range(4)]
    assert time.monotonic() - t0 < 3.0
    assert [o[0].delivered for o in outs] == [1, 1, 1, 1], "the healthy channel delivers every alert"
    assert not any(o[0].undelivered for o in outs)
    assert len(fast.sent) == 4
    assert len(slow.sent) == 1, "the hung channel is NOT retried every cycle (backoff)"
    assert outs[0][0].unknown == 1 and [o[0].backoff for o in outs] == [0, 1, 1, 1]
    rows = store.db.execute("SELECT channel, ok FROM alerts ORDER BY id").fetchall()
    assert [(r["channel"], r["ok"]) for r in rows].count(("slow", 0)) == 1


def test_a_channel_that_always_hangs_backs_off_3_then_6_then_12_cycles(tmp_path):
    gate = threading.Event()
    hung, fast = Chan("hung", hang=gate), Chan("fast")
    d, _ = disp(tmp_path, [hung, fast], channel_budget_s=0.1)
    attempts = []
    try:
        for cycle in range(1, 31):
            d.dispatch([decision()])
            if len(hung.sent) > len(attempts):
                attempts.append(cycle)
    finally:
        gate.set()
    assert attempts == [1, 5, 12, 25], attempts       # skips 3, then 6, then 12 (cap), then 12 again
    assert len(fast.sent) == 30


def test_only_channel_hanging_leaves_the_alert_undelivered_without_resend_storm(tmp_path):
    gate = threading.Event()
    hung = Chan("hung", hang=gate)
    d, _ = disp(tmp_path, [hung], channel_budget_s=0.1, budget_s=0.1)
    try:
        outs = [d.dispatch([decision()])[0] for _ in range(6)]
    finally:
        gate.set()
    assert all(o.undelivered for o in outs) and len(hung.sent) == 2    # cycle 1, and 5 after three skipped cycles
    assert [o.backoff for o in outs] == [0, 1, 1, 1, 0, 1]


def test_late_results_are_discarded_and_never_applied(tmp_path):
    class Late:
        name, min_status = "late", "info"
        wrote = None

        saved = None

        def bind(self, state):
            if state is not None:
                self.saved = state         # keep the proxy the way a slow worker would

        def send(self, msg):
            time.sleep(0.4)
            if self.saved:
                self.saved.set("hc:late:base", "down")    # state write after the dispatch closed
            return alerts.SendResult(True)

    late = Late()
    d, store = disp(tmp_path, [late], channel_budget_s=0.1, budget_s=0.1)
    out = d.dispatch([decision()])
    time.sleep(0.7)
    assert out[0].delivered == 0 and out[0].unknown == 1
    assert store.meta_get("hc:late:base") is None
    rows = store.db.execute("SELECT ok, error FROM alerts").fetchall()
    assert len(rows) == 1 and rows[0]["ok"] == 0, "the late success was not recorded"


def test_a_real_delivery_clears_the_backoff(tmp_path):
    chan = Chan("c", delay=0.3)
    d, store = disp(tmp_path, [chan], channel_budget_s=0.1, budget_s=0.1)
    d.dispatch([decision()])
    assert json.loads(store.meta_get("bo:c")) == {"level": 3, "skips": 3}
    chan.delay = 0.0
    for _ in range(3):
        assert d.dispatch([decision()])[0].backoff == 1
    out = d.dispatch([decision()])[0]
    assert out.delivered == 1
    assert json.loads(store.meta_get("bo:c")) == {"level": 0, "skips": 0}


def test_budget_config_keys():
    base = {"apps": [{"name": "a"}]}
    c = config.parse(base)
    assert (c.channel_budget_seconds, c.channel_backoff_cycles, c.alert_budget_seconds) == (20.0, 3, 60.0)
    c = config.parse({**base, "channel_budget_seconds": 5, "channel_backoff_cycles": 2})
    assert (c.channel_budget_seconds, c.channel_backoff_cycles) == (5.0, 2)
    for bad in ({"channel_budget_seconds": 0}, {"channel_backoff_cycles": 0}):
        with pytest.raises(config.ConfigError):
            config.parse({**base, **bad})


@pytest.mark.loopback
def test_slow_drip_smtp_next_to_a_healthy_channel_end_to_end(tmp_path):
    cert = make_cert(tmp_path, "mail.test")
    stub = SMTPStub("slowdrip", cert)
    try:
        cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}}], "alerts": [NTFY],
                            "data_dir": str(tmp_path / "d"), "confirm": 1, "channel_budget_seconds": 1,
                            "dead_man": {"url_env": "DM_URL"}})
        email = smtp_sender(stub.port, cert, creds=False, timeout=30.0)
        fast = alerts.build_senders(cfg, Stub(body=b"{}"))[0]
        calls = Stub(body=b"{}")
        senders = [email, alerts.build_senders(cfg, calls)[0]]

        class R:
            def __init__(self, *a, **k): pass
            def run_app(self, app, now):
                yield "HEALTHZ", CheckResult("crit", "DOWN", "x")

        t0 = time.monotonic()
        o = runtime.run_once(cfg, runner_factory=R, now=1000, fetch=calls, senders=senders)
        assert time.monotonic() - t0 < 4.0
        assert o.deliveries[0].delivered == 1 and o.deliveries[0].unknown == 1 and not o.deliveries[0].undelivered
        assert o.dead_man.action == "pinged", "one channel delivered, so the dead-man stays green"
        o.store.close()
    finally:
        stub.close()


# ------------------------------------------------------------------ F1: skipped is not a failure
def phase_runner(phase):
    class R:
        def __init__(self, *a, **k): pass

        def run_app(self, app, now):
            yield "HEALTHZ", CheckResult(phase[app.name], "V", "d")
    return R


def hc_cfg(tmp_path, port):
    return config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}},
                                  {"name": "b", "health": {"url": "https://b.example.com/"}}],
                         "alerts": [{"type": "webhook", "url_env": "HOOK_URL", "mode": "healthchecks"}],
                         "dead_man": {"url_env": "DM_URL"}, "data_dir": str(tmp_path / "d"), "confirm": 1})


@pytest.mark.loopback
def test_withheld_success_ping_is_not_a_failed_send_and_base_goes_up_on_the_first_clean_cycle(tmp_path, monkeypatch):
    """The reviewer's probe: A crit, A recovers while B blips unknown, then everything clean."""
    srv, fetch = https_fixture(tmp_path, "hc.test", lambda r: (200, {}, b"OK"))
    try:
        monkeypatch.setenv("HOOK_URL", f"https://hc.test:{srv.port}/ping/base")
        monkeypatch.setenv("DM_URL", f"https://hc.test:{srv.port}/ping/dm")
        cfg = hc_cfg(tmp_path, srv.port)
        phase = {"a": "crit", "b": "ok"}
        R = phase_runner(phase)
        t = 1_000_000
        seq = [({"a": "crit", "b": "ok"}), ({"a": "ok", "b": "ok"}), ({"a": "ok", "b": "unknown"}), ({"a": "ok", "b": "unknown"}),
               ({"a": "ok", "b": "ok"}), ({"a": "ok", "b": "ok"}), ({"a": "ok", "b": "ok"})]
        pings, dm_actions = [], []
        for i, ph in enumerate(seq):
            phase.update(ph)
            srv.requests.clear()
            o = runtime.run_once(cfg, runner_factory=R, now=t + i * 300, fetch=fetch)
            pings.append([r.path for r in srv.requests if r.path.startswith("/ping/base")])
            dm_actions.append(o.dead_man.action)
            assert o.store.last_alert_ok_by_channel() in ({}, {"webhook": True}), "a withheld ping never looks like a failure"
            o.store.close()
        assert pings[0] == ["/ping/base/fail"]
        assert "/ping/base" not in sum(pings[1:4], []), "b blipped unknown until cycle 3"
        assert "/ping/base" in sum(pings[4:], []), pings                    # the first fully clean cycle brings it UP
        assert sum(pings, []).count("/ping/base") == 1
        assert "withheld" not in dm_actions, dm_actions                        # the dead-man never cried 'dead channel'
    finally:
        srv.close()


@pytest.mark.loopback
def test_an_earlier_unrecovered_crit_keeps_the_shared_check_red(tmp_path, monkeypatch):
    """Surviving mutant: bad_apps must include checks whose alert is still in force although this cycle's result is ok."""
    srv, fetch = https_fixture(tmp_path, "hc.test", lambda r: (200, {}, b"OK"))
    try:
        monkeypatch.setenv("HOOK_URL", f"https://hc.test:{srv.port}/ping/base")
        monkeypatch.setenv("DM_URL", f"https://hc.test:{srv.port}/ping/dm")
        cfg = hc_cfg(tmp_path, srv.port)
        phase = {"a": "crit", "b": "crit"}
        R = phase_runner(phase)
        steps = [({"a": "crit", "b": "crit"}), ({"a": "crit", "b": "ok"}), ({"a": "ok", "b": "ok"}), ({"a": "ok", "b": "ok"})]
        seen = []
        for i, ph in enumerate(steps):
            phase.update(ph)
            srv.requests.clear()
            o = runtime.run_once(cfg, runner_factory=R, now=1000 + i * 300, fetch=fetch)
            seen.append([r.path for r in srv.requests if r.path.startswith("/ping/base")])
            o.store.close()
        # cycle 3: b recovers while a's result is ok but its crit alert is still in force (one ok cycle so far):
        # a's alert alone must keep the shared check red
        assert seen[2] == [], seen
        assert seen[3] == ["/ping/base"], seen        # a recovers: first fully clean cycle, exactly one UP ping
    finally:
        srv.close()


def test_skipped_results_are_not_recorded_and_do_not_affect_channel_health(tmp_path):
    class Skipper:
        name, min_status = "skipper", "info"

        def send(self, msg):
            return alerts.SendResult(False, "withheld", skipped=True)

    d, store = disp(tmp_path, [Skipper()])
    out = d.dispatch([decision(kind="recovered", level="crit")])
    assert out[0].skipped == 1 and out[0].delivered == 0 and not out[0].undelivered
    assert store.db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0
    assert d.channels_healthy() is True


# ------------------------------------------------------------------ review 4 G7
def test_a_slow_but_successful_send_is_a_delivery_not_an_unknown_outcome(tmp_path):
    """The only channel needs 0.3 s, its own budget is 0.1 s, the overall cap 2 s: the late success counts."""
    slow = Chan("slow", delay=0.3)
    d, store = disp(tmp_path, [slow], channel_budget_s=0.1, budget_s=2.0)
    outs = [d.dispatch([decision(app=f"a{i}")])[0] for i in range(4)]
    assert [o.delivered for o in outs] == [1, 1, 1, 1] and not any(o.undelivered for o in outs)
    assert len(slow.sent) == 4, "one send per alert: no duplicates, no backoff"
    assert store.meta_get("bo:slow") is None and d.channels_healthy()


def test_the_overall_cap_still_bounds_a_channel_that_never_answers(tmp_path):
    gate = threading.Event()
    hung = Chan("hung", hang=gate)
    d, _ = disp(tmp_path, [hung], channel_budget_s=0.1, budget_s=0.4)
    try:
        t0 = time.monotonic()
        out = d.dispatch([decision()])[0]
        assert time.monotonic() - t0 < 1.5 and out.unknown == 1 and out.undelivered
    finally:
        gate.set()


def test_a_slow_reconcile_does_not_put_a_delivering_channel_into_backoff(tmp_path):
    class SlowReconcile(Chan):
        def reconcile(self, cycle_clear, clear_apps):
            time.sleep(0.5)

    chan = SlowReconcile("hc")
    d, store = disp(tmp_path, [chan], channel_budget_s=0.15, budget_s=0.15)
    out = d.dispatch([decision()])[0]
    assert out.delivered == 1 and out.unknown == 0
    assert store.meta_get("bo:hc") is None
    assert d.dispatch([decision()])[0].backoff == 0


@pytest.mark.parametrize("exc", [SystemExit(3), KeyboardInterrupt(), GeneratorExit(), RuntimeError("x")])
def test_a_sender_raising_a_base_exception_is_a_failed_send_not_a_lost_one(tmp_path, exc):
    class Raiser:
        name, min_status = "raiser", "info"

        def send(self, msg):
            raise exc

    d, store = disp(tmp_path, [Raiser()])
    out = d.dispatch([decision()])[0]
    assert out.delivered == 0 and out.undelivered
    rows = store.db.execute("SELECT ok, error FROM alerts").fetchall()
    assert [(r["ok"], r["error"]) for r in rows] == [(0, type(exc).__name__)]
    d._deadline = time.monotonic() + 5
    assert d.test_all("v")["raiser"].error == type(exc).__name__

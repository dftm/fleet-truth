"""Fourth review: hidden blindness behind flap suppression, false messages, config bounds."""
import math
import random

import pytest

from fleet_truth import alerts, config, cycle, flap
from fleet_truth.checks.base import CheckResult
from fleet_truth.store import Store
from tests.test_alerts import env, https_fixture  # noqa: F401
from tests.test_cycle import FakeRunner, mkcfg
from tests.test_flapping import CRIT, OK, UNK, WARN, drive, pattern_runner
from tests.test_oracle import Chan

NET = CheckResult("crit", "DOWN", "cannot connect", {"net_error": "CONNECT"})


def run_with_channels(cfg, plan, chans, check="HEALTHZ", store=None, n=None):
    store = store or Store(":memory:")
    rn = FakeRunner(lambda app, now: iter([(check, plan[state["i"]])]))
    state = {"i": 0}
    disp = alerts.Dispatcher(chans, store, "u")
    decisions = []
    for i in range(n or len(plan)):
        state["i"] = i
        for c in chans:
            c.i = i
        cr = cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5)
        for d in disp.dispatch(list(cr.decisions)):
            if d.undelivered:
                cycle.rollback_alert_state(store, d.decision)
            decisions.append((i, d))
    return store, decisions


# ------------------------------------------------------------------ G2: blind is never swallowed by flap suppression
def test_unknown_ok_flapping_then_sustained_unknown_reaches_a_crit_only_channel():
    """Probe p2: `ouououou` then unknown for 25 h. Before: zero messages, dead-man OK throughout."""
    plan = [OK, UNK] * 4 + [UNK] * 300
    cfg = mkcfg(access=False, n=1)
    tg = Chan("tg", "crit", lambda i: False)
    _store, decs = run_with_channels(cfg, plan, [tg])
    heads = [(i, t.split(":")[0]) for i, t in tg.got]
    assert any(h.startswith("[BLIND]") for _, h in heads), heads
    first = [i for i, h in heads if h.startswith("[BLIND]")][0]
    assert first <= 8 + 8, first                       # within ~40 minutes of the unknown run starting
    ctl = Chan("c", "crit", lambda i: False)
    run_with_channels(mkcfg(access=False, n=1), [UNK] * 20, [ctl])
    assert any(t.startswith("[BLIND]") for _, t in ctl.got)       # the control already worked


def test_a_dropped_alert_is_not_remembered_as_sent(tmp_path):
    store = Store(tmp_path / "x.db")
    cfg = mkcfg(access=False, n=1)
    seq = [CRIT, OK] * 3 + [OK] * 3 + [CRIT, CRIT, CRIT]       # flapping on; a NEW confirmed episode while it still is
    rn = pattern_runner(seq)
    sent = []
    for i in range(len(seq)):
        cr = cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5)
        sent += [(i, d.kind) for d in cr.decisions]
    st = store.get_state("app0", "HEALTHZ")
    assert flap._load(store, "app0", "HEALTHZ")["flapping"]
    assert st.unhealthy_cycles >= 3 and st.worst_status == "crit"
    assert st.alerted_status is None, "dropped by flap suppression, so it must not look sent"
    assert [k for i, k in sent if i >= 9] == []


def test_sustained_hand_back_uses_the_worst_level_seen():
    seq = [CRIT, OK] * 4 + [WARN] * 30
    out, _ = drive_tmp(seq)
    sus = [d for _, d in out if d.kind == "sustained"]
    assert sus and sus[0].level == "crit", [(d.kind, d.level) for _, d in out]    # the dips were crit; the run is warn


def drive_tmp(seq):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        return drive(Path(d), seq, len(seq), name="x")


# ------------------------------------------------------------------ G3a: [STABLE] is "up", never /fail
@pytest.mark.loopback
def test_stable_pings_healthchecks_up_never_fail_and_ntfy_priority_is_low(tmp_path, monkeypatch):
    from tests.test_alerts import cfg_with, decision
    srv, fetch = https_fixture(tmp_path, "hc.test", lambda r: (200, {}, b"OK"))
    try:
        monkeypatch.setenv("HOOK_URL", f"https://hc.test:{srv.port}/ping/base")
        cfg = cfg_with({"type": "webhook", "url_env": "HOOK_URL", "mode": "healthchecks", "min_status": "warn"})
        disp = alerts.Dispatcher(alerts.build_senders(cfg, fetch), None, "u")
        out = disp.dispatch([decision(kind="stable", level="crit")])[0]
        assert out.delivered == 1 and [r.path for r in srv.requests] == ["/ping/base"]
        srv.requests.clear()
        out = disp.dispatch([decision(kind="stable", level="crit")], cycle_clear=False)[0]
        assert srv.requests == [] and out.skipped == 1 and not out.undelivered     # something else is still down
        disp.dispatch([decision(kind="flapping", level="crit")])
        assert [r.path for r in srv.requests] == ["/ping/base/fail"]
    finally:
        srv.close()
    stub = []

    def fetch2(url, **kw):
        stub.append(kw["headers"])
        return alerts.net.Response(200, {}, b"{}", "1.1.1.1", None, 1)

    ntfy = cfg_with({"type": "ntfy", "topic_env": "NTFY_TOPIC", "min_status": "warn"})
    s = alerts.build_senders(ntfy, fetch2)[0]
    s.send(alerts.format_message(decision(kind="stable", level="crit"), "u"))
    s.send(alerts.format_message(decision(kind="alert", level="crit"), "u"))
    assert [h["Priority"] for h in stub] == ["3", "5"]


# ------------------------------------------------------------------ G3b/c: intermittent only for what it is
def count_kinds(out):
    return collections_counter([d.kind for _, d in out])


def collections_counter(xs):
    import collections
    return collections.Counter(xs)


def test_no_intermittent_stream_after_a_resolved_flap(tmp_path):
    seq = [CRIT, OK] * 4 + [OK] * 14 * 12              # a short flap, then ~14 h of green
    out, _ = drive(tmp_path, seq, len(seq), name="after")
    kinds = [d.kind for _, d in out]
    assert "flapping" in kinds and "stable" in kinds
    assert "intermittent" not in kinds, kinds        # the flap episode is not a series of unconfirmed blips


def test_no_intermittent_during_or_for_a_confirmed_outage(tmp_path):
    seq = ([CRIT] * 30 + [OK] * 3) * 3
    out, _ = drive(tmp_path, seq, len(seq), name="outage")
    kinds = [d.kind for _, d in out]
    assert kinds.count("alert") >= 3 and "intermittent" not in kinds
    # three alerted outages inside the window did not become "too short to confirm"


def test_isolated_blips_at_one_percent_for_a_week_cost_at_most_one_message_a_day(tmp_path):
    rng = random.Random(7)
    n = 7 * 288
    seq = [CRIT if rng.random() < 0.01 else OK for _ in range(n)]
    out, _ = drive(tmp_path, seq, n, name="noisy")
    inter = [(i, d) for i, d in out if d.kind == "intermittent"]
    assert len(inter) <= 7, len(inter)
    assert all(b[0] - a[0] >= 288 for a, b in zip(inter, inter[1:])), "at most one per 24 h"
    for i, _ in inter:                                 # and only because the threshold was truly reached
        starts = sum(1 for j in range(max(0, i - 288), i + 1) if seq[j] is CRIT and (j == 0 or seq[j - 1] is not CRIT))
        assert starts >= 3, (i, starts)


def test_a_repeat_needs_a_new_failure(tmp_path):
    seq = ([CRIT] + [OK] * 8) * 3 + [OK] * 800          # three blips, then silence for a long time
    out, _ = drive(tmp_path, seq, len(seq), name="rep")
    assert [d.kind for _, d in out].count("intermittent") == 1


# ------------------------------------------------------------------ G4: correlation-suppressed cycles are not observed
def test_monitor_offline_phases_leave_no_flap_or_intermittent_state(tmp_path):
    """Probe p3: an intermittent monitor-offline phase used to declare the apps flapping silently, so a real
    outage later was first reported 50 minutes late."""
    store = Store(tmp_path / "p3.db")
    cfg = mkcfg(access=False, n=2, offline_suppress_cycles=1000, reference_urls=["https://ref.example.org/"])
    state = {"i": 0, "mode": "off"}

    class R(FakeRunner):
        def __init__(self):
            super().__init__(None)

        def reference_ok(self, now):
            return False if state["mode"] == "off" else True

        def run_app(self, app, now):
            if state["mode"] == "off":
                yield "HEALTHZ", NET if state["i"] % 2 == 0 else OK
            elif state["mode"] == "real" and app.name == "app0":
                yield "HEALTHZ", CRIT
            else:
                yield "HEALTHZ", OK

    rn = R()
    decs = []
    for i in range(40):                                  # alternating "monitor cannot reach anything" / "fine"
        state["i"] = i
        cr = cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5)
        decs += [(i, d.app, d.kind) for d in cr.decisions]
    fl = flap._load(store, "app0", "HEALTHZ")
    # suppressed cycles are skipped; the ok cycles in between are observed but have no observed neighbour to flip against
    assert not fl["flapping"] and fl["flips"] == [] and fl["fails"] == []
    assert not [x for x in decs if x[1] in ("app0", "app1") and x[2] in ("flapping", "stable", "intermittent", "sustained")]
    state["mode"] = "real"
    first = None
    for i in range(40, 46):
        state["i"] = i
        cr = cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5)
        if first is None and any(d.app == "app0" and d.kind == "alert" for d in cr.decisions):
            first = i
    assert first == 41, first                            # confirm 2: the second failing cycle, not 50 minutes later


# ------------------------------------------------------------------ G5: honest numbers, exact undo
def test_flapping_reminder_only_while_actually_flapping_even_with_a_grown_quiet_period(tmp_path):
    """Probe p_8x: after the quiet period grew to 8x, the 6 h reminder fired after 6 h of green: 0 state changes."""
    store = Store(tmp_path / "e.db")
    args = dict(flips=4, cycles=12, remind_hours=6, clear_cycles=12, window_cycles=48, min_failures=99)
    prev, t, msgs = "ok", 1000, []

    def run(seq):
        nonlocal prev, t
        for st in seq:
            o = flap.step(store, "a", "c", prev, st, t, 300, **args)
            if o.decision:
                msgs.append((t, o.decision))
            prev, t = st, t + 300

    for _ in range(4):                     # flap, stable, flap again, ... until the quiet period has grown
        run(["crit", "ok"] * 3)
        run(["ok"] * 100)
    run(["crit", "ok"] * 3)
    need = flap._load(store, "a", "c")["clear_need"]
    assert need == 12 * 8
    n0 = len(msgs)
    run(["ok"] * 90)                       # 7.5 h of green, still inside the 96-cycle quiet period
    assert len(msgs) == n0, [m for m in msgs[n0:]]
    assert not any("(0 state changes" in d[2] for _, d in msgs)


def test_a_lost_stable_is_restored_exactly_and_never_reads_zero_state_changes(tmp_path):
    cfg = mkcfg(access=False, n=1, flap={"flips": 4, "cycles": 12, "remind_hours": 6, "clear_cycles": 5})
    store = Store(tmp_path / "u.db")
    seq = [CRIT, OK] * 4 + [OK] * 12
    rn = pattern_runner(seq)
    texts, lost = [], {"n": 0}
    for i in range(len(seq)):
        cr = cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5)
        for d in cr.decisions:
            texts.append((i, d.kind, d.detail))
            if d.kind == "stable" and lost["n"] == 0:      # the first [STABLE] reaches nobody
                lost["n"] = 1
                cycle.rollback_alert_state(store, d)
    assert [k for _, k, _ in texts].count("stable") == 2 and lost["n"] == 1
    assert not any("0 state changes" in t for _, _, t in texts)
    assert not any(k == "flapping" and i > 12 for i, k, _ in texts), texts


def test_flap_numbers_come_from_the_config():
    cfg = mkcfg(access=False, n=1, flap={"flips": 3, "cycles": 6, "remind_hours": 1, "clear_cycles": 2})
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "c.db")
        rn = pattern_runner([CRIT, OK] * 3)
        out = []
        for i in range(6):
            out += cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5).decisions
    fl = [d for d in out if d.kind == "flapping"]
    assert fl and "in the last 6 cycles" in fl[0].detail and "2 cycles" in fl[0].detail


# ------------------------------------------------------------------ G6: config bounds
@pytest.mark.parametrize("key,val", [
    ("alert_budget_seconds", float("inf")), ("alert_budget_seconds", float("nan")), ("alert_budget_seconds", 0),
    ("alert_budget_seconds", 99999), ("channel_budget_seconds", float("nan")), ("channel_budget_seconds", -1),
    ("channel_budget_seconds", 1e9), ("channel_backoff_cycles", 0), ("channel_backoff_cycles", 10 ** 6),
    ("recover_after", 0), ("recover_after", 21), ("recover_after", 10 ** 9), ("reopen_cooldown_cycles", -1),
    ("reopen_cooldown_cycles", 101), ("offline_suppress_cycles", -1), ("offline_suppress_cycles", 10 ** 6),
    ("flap", {"remind_hours": float("nan")}), ("flap", {"remind_hours": 1e12}), ("flap", {"remind_hours": 0}),
    ("flap", {"flips": 20, "cycles": 12}), ("flap", {"flips": 1}), ("flap", {"cycles": 10 ** 6}),
    ("flap", {"clear_cycles": 0}), ("flap", {"clear_cycles": float("inf")}),
    ("intermittent", {"window_cycles": 1}), ("intermittent", {"window_cycles": 10 ** 7}),
    ("intermittent", {"min_failures": 1}), ("intermittent", {"min_failures": 10 ** 6}),
    ("intermittent", {"remind_hours": float("nan")}), ("intermittent", {"remind_hours": 0.1}),
])
def test_nonsensical_numbers_are_clean_config_errors(key, val):
    with pytest.raises(config.ConfigError) as e:      # a ConfigError, never a traceback from deep inside
        config.parse({"apps": [{"name": "a"}], key: val})
    assert e.value.errors and all(isinstance(x, str) for x in e.value.errors)


def test_infinity_from_yaml_text_is_a_clean_error_and_sane_values_pass():
    with pytest.raises(config.ConfigError) as e:
        config.parse_text("apps: [{name: a}]\nalert_budget_seconds: .inf\n")
    assert "finite" in e.value.errors[0] or "between" in e.value.errors[0]
    with pytest.raises(config.ConfigError):
        config.parse_text("apps: [{name: a}]\nflap: {remind_hours: .nan}\n")
    c = config.parse({"apps": [{"name": "a"}], "recover_after": 20, "reopen_cooldown_cycles": 100,
                      "flap": {"flips": 12, "cycles": 12, "remind_hours": 720}, "intermittent": {"remind_hours": 1}})
    assert c.recover_after == 20 and c.flap.flips == 12 and c.intermittent.remind_hours == 1

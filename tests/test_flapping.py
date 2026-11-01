"""Review 3 F2/F3: alert storms and hidden intermittent failures."""
import pytest

from fleet_truth import alerts, config, cycle, flap
from fleet_truth.checks.base import CheckResult
from fleet_truth.store import Store
from tests.test_cycle import FakeRunner, mkcfg

OK = CheckResult("ok", "OK")
CRIT = CheckResult("crit", "DOWN", "x")
UNK = CheckResult("unknown", "TIMEOUT_CYCLE", "y")
WARN = CheckResult("warn", "W", "w")


def pattern_runner(pattern):
    n = {"i": 0}

    def fn(app, now):
        r = pattern[n["i"] % len(pattern)]
        n["i"] += 1
        yield "HEALTHZ", r
    return FakeRunner(fn)


def drive(tmp_path, pattern, cycles, interval=300, name="d", **cfgkw):
    store = Store(tmp_path / f"{name}.db")
    cfg = mkcfg(access=False, n=1, interval_seconds=interval, **cfgkw)
    rn = pattern_runner(pattern)
    out = []
    for i in range(cycles):
        cr = cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * interval, deadline_s=5)
        out += [(i, d) for d in cr.decisions]
    return out, store


@pytest.mark.parametrize("pat", [[CRIT, CRIT, OK], [CRIT, UNK, OK], [CRIT, WARN, OK], [CRIT, CRIT, OK, OK],
                                 [CRIT, OK, CRIT, OK, OK], [WARN, WARN, OK]])
def test_short_episodes_do_not_storm_for_24h(tmp_path, pat):
    out, _ = drive(tmp_path, pat, 288)
    kinds = [d.kind for _, d in out]
    assert len(out) <= 12, (len(out), kinds)                 # reviewer saw 192 messages for the first three
    assert kinds.count("alert") <= 2 and kinds.count("recovered") <= 2
    assert "flapping" in kinds or kinds.count("alert") == 1


def test_flapping_replaces_individual_pairs_and_a_stable_notice_ends_it(tmp_path):
    store = Store(tmp_path / "s.db")
    cfg = mkcfg(access=False, n=1)
    seq = [CRIT, CRIT, OK] * 6 + [OK] * 14 + [CRIT, CRIT, OK, OK, OK]
    rn = pattern_runner(seq)
    got = []
    for i in range(len(seq)):
        for d in cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5).decisions:
            got.append((i, d.kind, d.level))
    kinds = [k for _, k, _ in got]
    assert kinds.count("flapping") >= 1 and kinds.count("stable") == 1
    stable_at = [i for i, k, _ in got if k == "stable"][0]
    flapping_at = [i for i, k, _ in got if k == "flapping"][0]
    between = [k for i, k, _ in got if flapping_at < i < stable_at]
    assert set(between) <= {"flapping"}, between               # no CRIT/OK pairs while flapping
    assert all(lv == "crit" for _, k, lv in got if k in ("flapping", "stable"))
    after = [k for i, k, _ in got if i > stable_at]
    assert "alert" in after, "normal alerting resumes after [STABLE]"


def test_stable_needs_clear_cycles_ok_in_a_row(tmp_path):
    store = Store(tmp_path / "s.db")
    cfg = mkcfg(access=False, n=1, flap={"flips": 4, "cycles": 12, "remind_hours": 6, "clear_cycles": 5})
    seq = [CRIT, OK] * 4 + [OK] * 3 + [CRIT] + [OK] * 5
    rn = pattern_runner(seq)
    got = []
    for i in range(len(seq)):
        got += [(i, d.kind) for d in cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5).decisions]
    stable = [i for i, k in got if k == "stable"]
    assert len(stable) == 1 and stable[0] == len(seq) - 1      # reset by the crit, then 5 ok in a row


def feed(store, seq, t0=2000, **kw):
    prev, outs = "ok", []
    args = dict(flips=4, cycles=12, remind_hours=6, clear_cycles=3, window_cycles=48, min_failures=3)
    args.update(kw)
    for i, st in enumerate(seq):
        outs.append(flap.step(store, "a", "c", prev, st, t0 + i * 300, 300, **args))
        prev = st
    return outs


def test_undelivered_flapping_and_stable_are_produced_again(tmp_path):
    store = Store(tmp_path / "s.db")
    outs = feed(store, ["crit", "ok", "crit", "ok", "crit"])
    assert any(o.decision and o.decision[0] == "flapping" for o in outs)
    flap.undo(store, "a", "c", "flapping", 3)
    again = feed(store, ["crit"], t0=2000 + 5 * 300, remind_hours=0.001)
    assert again[0].decision and again[0].decision[0] == "flapping"
    # stable: three ok cycles end it; if that notice was lost the next ok cycle produces it again
    outs = feed(store, ["ok", "ok", "ok"], t0=2000 + 6 * 300)
    assert outs[-1].decision[0] == "stable" and not flap._load(store, "a", "c")["flapping"]
    flap.undo(store, "a", "c", "stable", 3)
    assert flap._load(store, "a", "c")["flapping"]
    redo = feed(store, ["ok"], t0=2000 + 9 * 300)
    assert redo[0].decision and redo[0].decision[0] == "stable"


# ------------------------------------------------------------------ F3: the 1-in-7 failure that never confirms
@pytest.mark.parametrize("interval", [300, 21600])
def test_one_failure_in_seven_is_reported_as_intermittent(tmp_path, interval):
    out, _ = drive(tmp_path, [CRIT] + [OK] * 6, 7 * 12, interval=interval, name=f"i{interval}")
    inter = [(i, d) for i, d in out if d.kind == "intermittent"]
    assert inter and inter[0][1].level == "crit"
    assert inter[0][0] == 14                      # the third separate failure (cycle 0, 7, 14)
    assert "3 separate failures" in inter[0][1].detail
    assert not [d for _, d in out if d.kind in ("alert", "recovered")]       # never confirmed, so no CRIT/OK pairs
    per_day = {}
    for i, d in out:
        per_day[i * interval // 86400] = per_day.get(i * interval // 86400, 0) + 1
    assert max(per_day.values()) <= 1 + 24 // 6, per_day                    # the 24 h bound per check
    gaps = [b[0] - a[0] for a, b in zip(inter, inter[1:])]
    assert all(g * interval >= 6 * 3600 for g in gaps), gaps               # reminders only at the normal interval


def test_single_blips_do_not_alert_and_a_clean_window_clears_it(tmp_path):
    out, _ = drive(tmp_path, [CRIT] + [OK] * 47, 48 * 2, name="b")
    assert out == []                                           # 1 failure in 48 cycles
    out, store = drive(tmp_path, [WARN] + [OK] * 20, 62, name="w")  # 3 warn blips within 24 h
    assert [d.kind for _, d in out] == ["intermittent"] and out[0][1].level == "warn"
    # a failure only EPISODE counts once, however long
    out, _ = drive(tmp_path, [CRIT] * 40 + [OK] * 8, 48, name="long")
    assert "intermittent" not in [d.kind for _, d in out]
    # clean for the whole window: the alert state clears and a later single blip stays quiet
    cfg = mkcfg(access=False, n=1)
    rn = pattern_runner([OK])
    for i in range(300):
        cycle.run_cycle(cfg, store, rn, now=1_000_000 + 90 * 300 + i * 300, deadline_s=5)
    assert flap._load(store, "app0", "HEALTHZ")["int_alert"] == 0


def test_intermittent_config_and_message_heads():
    base = {"apps": [{"name": "a"}]}
    c = config.parse({**base, "intermittent": {"window_cycles": 10, "min_failures": 2}, "recover_after": 3,
                      "reopen_cooldown_cycles": 2, "flap": {"clear_cycles": 4}})
    assert (c.intermittent.window_cycles, c.intermittent.min_failures, c.recover_after, c.reopen_cooldown_cycles,
            c.flap.clear_cycles) == (10, 2, 3, 2, 4)
    d = config.parse(base)
    assert (d.flap.flips, d.flap.cycles, d.recover_after, d.intermittent.min_failures) == (4, 12, 2, 3)
    for bad in ({"intermittent": {"min_failures": 1}}, {"recover_after": 0}, {"flap": {"clear_cycles": 0}}):
        with pytest.raises(config.ConfigError):
            config.parse({**base, **bad})
    from fleet_truth.state import AlertDecision
    mk = lambda kind, lv: alerts.format_message(AlertDecision(kind, "a", "c", "warn", lv, "V", "d", 1000, 2), "u").text  # noqa: E731
    assert mk("flapping", "crit").startswith("[CRIT FLAPPING] a c")
    assert mk("flapping", "warn").startswith("[FLAPPING] a c")
    assert mk("stable", "crit").startswith("[STABLE] a c")
    assert mk("intermittent", "crit").startswith("[INTERMITTENT] a c (crit)")

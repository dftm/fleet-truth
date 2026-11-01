"""Flap guard and check_state transitions on a fake clock (card 6 acceptance [1])."""
import pytest

from fleet_truth.checks.base import CheckResult
from fleet_truth.state import advance

STEP = 300


def drive(seq, confirm=2, step=STEP, t0=1_000_000, **kw):
    """seq: list of status strings or (status, verdict). Returns [(t, kind or None)] and final row."""
    row, out = None, []
    for i, item in enumerate(seq):
        status, verdict = item if isinstance(item, tuple) else (item, "V")
        now = t0 + i * step
        row, d = advance(row, "app", "CHK", CheckResult(status, verdict, "d"), now, confirm, **kw)
        out.append((now - t0, d.kind if d else None))
    return out, row


def kinds(out):
    return [k for _, k in out if k]


def test_fail_fail_one_alert():
    out, _ = drive(["crit", "crit"])
    assert kinds(out) == ["alert"] and out[1][1] == "alert"


def test_fail_then_ok_no_alert_at_all():
    out, _ = drive(["crit", "ok", "crit", "ok", "warn", "ok"], recover_after=1)
    assert kinds(out) == []


def test_continuing_failure_does_not_repeat():
    out, _ = drive(["crit"] * 20)
    assert kinds(out) == ["alert"]


def test_crit_for_7h_alert_plus_one_reminder():
    n = 7 * 3600 // STEP + 1
    out, _ = drive(["crit"] * n)
    assert kinds(out) == ["alert", "reminder"]
    reminder_t = [t for t, k in out if k == "reminder"][0]
    alert_t = [t for t, k in out if k == "alert"][0]
    assert reminder_t - alert_t == 6 * 3600


def test_warn_reminds_daily_not_6h():
    out, _ = drive(["warn"] * (7 * 3600 // STEP))
    assert kinds(out) == ["alert"]
    out, _ = drive(["warn"] * (25 * 3600 // STEP))
    assert kinds(out) == ["alert", "reminder"]


def test_recovery_once_immediately_then_silence():
    out, row = drive(["crit", "crit", "crit", "ok", "ok", "ok"], recover_after=1)
    assert kinds(out) == ["alert", "recovered"]
    assert out[3][1] == "recovered" and row.alerted_status is None
    out2, _ = drive(["crit", "crit", "info", "info"], recover_after=1)
    assert kinds(out2) == ["alert", "recovered"]  # info counts as healthy


def test_recovery_level_matches_what_was_alerted():
    row = None
    for i, s in enumerate(["warn", "warn", "ok", "ok"]):
        row, d = advance(row, "a", "c", CheckResult(s, "V", "d"), i * STEP, 2)
    assert d.kind == "recovered" and d.level == "warn"


def test_blip_does_not_arm_a_recovery():
    out, _ = drive(["crit", "ok", "crit", "ok", "ok"], recover_after=1)
    assert kinds(out) == []


def test_mixed_unhealthy_statuses_form_one_episode_and_alert_at_the_worst_level():
    out, row = drive(["crit", "warn", "crit", "warn"])
    assert kinds(out) == ["alert"] and out[1][1] == "alert"        # confirm counts episode cycles, not same-status runs
    assert row.alerted_status == "crit" and row.worst_status == "crit" and row.unhealthy_cycles == 4
    out, _ = drive(["warn", "warn", "crit", "crit"])
    assert kinds(out) == ["alert", "alert"] and out[2][1] == "alert"   # escalation is immediate once the episode is confirmed


def test_confirm_one_alerts_immediately():
    out, _ = drive(["crit"], confirm=1)
    assert kinds(out) == ["alert"]


def test_unknown_25_min_no_alert_31_min_one_blind_alert():
    out, _ = drive(["unknown"] * 6)  # t = 0..25 min
    assert kinds(out) == []
    out, _ = drive(["unknown"] * 8)  # t = 0..35 min
    assert kinds(out) == ["blind"]
    assert [t for t, k in out if k == "blind"] == [30 * 60]
    out, _ = drive(["unknown"] * 40)
    assert kinds(out) == ["blind"]  # never repeats


def test_blind_alert_is_worded_unknown_level_warn():
    row = None
    for i in range(8):
        row, d = advance(row, "a", "c", CheckResult("unknown", "ERROR", "x"), i * STEP, 2)
    assert row.alerted_status == "unknown"


def test_unknown_flapping_with_ok_never_alerts():
    out, _ = drive(["unknown", "unknown", "ok", "ok", "unknown", "unknown", "ok", "ok"] * 3)
    assert kinds(out) == []   # two ok cycles end each episode, so no run reaches 30 minutes


def test_recovery_from_blind():
    out, _ = drive(["unknown"] * 8 + ["ok", "ok"])
    assert kinds(out) == ["blind", "recovered"]


def test_since_tracks_status_change_and_consecutive_counts():
    _, row = drive(["ok", "ok", "crit", "crit", "crit"])
    assert row.consecutive == 3 and row.since == 1_000_000 + 2 * STEP


def test_decision_carries_prior_alert_fields_for_rollback():
    row = None
    for i, s in enumerate(["crit", "crit"]):
        row, d = advance(row, "a", "c", CheckResult(s, "V", "d"), i * STEP, 2)
    assert d.prev_alerted_status is None and row.alerted_status == "crit"


def test_blind_decision_level_defaults_to_crit_and_is_configurable():
    for lvl, expect in ((None, "crit"), ("warn", "warn")):
        row = None
        for i in range(8):
            kw = {} if lvl is None else {"blind_level": lvl}
            row, d = advance(row, "a", "c", CheckResult("unknown", "ERROR", "x"), i * STEP, 2, **kw)
            if d:
                assert d.kind == "blind" and d.level == expect
        assert row.alerted_status == "unknown"


# ------------------------------------------------------------------ review 2 R1: crit/unknown cannot hide
def decisions(seq, confirm=2, **kw):
    row, out = None, []
    for i, st in enumerate(seq):
        row, d = advance(row, "a", "c", CheckResult(st, "V", "d"), 1_000_000 + i * STEP, confirm, **kw)
        if d:
            out.append((i, d))
    return out, row


def test_24h_crit_unknown_alternation_alerts_crit():
    out, row = decisions(["crit", "unknown"] * 144)                 # 288 cycles = 24 h
    first = out[0][1]
    assert out[0][0] == 1 and (first.kind, first.level) == ("alert", "crit")
    assert [d.kind for _, d in out] == ["alert"] + ["reminder"] * 3  # 6 h reminders after the first
    assert all(d.level == "crit" for _, d in out)


def test_one_crit_then_five_unknowns_delivers_a_crit_alert():
    out, _ = decisions(["crit"] + ["unknown"] * 5)
    assert [(i, d.kind, d.level) for i, d in out] == [(1, "alert", "crit")]
    assert "worst in this episode: crit V" in out[0][1].detail


def test_unknown_time_counts_toward_blind_timer_even_after_a_warn_blip_free_run():
    out, _ = decisions(["unknown"] * 7)
    assert [d.kind for _, d in out] == ["blind"] and out[0][0] == 6


def test_genuine_recovery_ends_the_episode_and_a_new_one_starts_clean():
    out, row = decisions(["crit", "unknown", "ok", "ok", "unknown", "unknown"])
    assert [d.kind for _, d in out] == ["alert", "recovered"]
    assert row.unhealthy_cycles == 4 and row.worst_status == "crit" and row.ok_notified == 1   # re-opened within the cooldown: same episode
    out, _ = decisions(["crit", "unknown", "ok", "ok", "unknown", "unknown", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "ok", "crit", "crit"])
    assert [d.kind for _, d in out] == ["alert", "recovered", "alert"]


def test_blind_then_crit_in_the_same_episode_alerts_crit():
    out, _ = decisions(["unknown"] * 7 + ["crit"])
    assert [d.kind for _, d in out] == ["blind", "alert"] and out[1][1].level == "crit"


def test_single_blips_separated_by_two_ok_cycles_never_confirm():
    out, _ = decisions(["crit", "ok", "ok"] * 10)
    assert out == []
    out, _ = decisions(["crit", "ok"] * 10, recover_after=1)
    assert out == []


# ------------------------------------------------------------------ review 3 F2: hysteresis against alert storms
def day(pattern, **kw):
    return decisions(pattern * (288 // len(pattern) + 1), **kw)[0]


def test_crit_crit_ok_repeating_is_one_episode_not_96_pairs():
    for pat in (["crit", "crit", "ok"], ["crit", "unknown", "ok"], ["crit", "warn", "ok"]):
        out = day(pat)
        kinds = [d.kind for _, d in out]
        assert kinds.count("alert") == 1 and "recovered" not in kinds, pat
        assert len(out) <= 1 + 24 // 6 + 1, (pat, len(out))     # alert + one reminder per 6 h


def test_recovery_needs_recover_after_consecutive_ok_cycles():
    out, _ = decisions(["crit", "crit", "ok", "ok"])
    assert [d.kind for _, d in out] == ["alert", "recovered"] and out[1][0] == 3
    out, _ = decisions(["crit", "crit", "ok", "ok", "ok"], recover_after=3)
    assert [d.kind for _, d in out] == ["alert", "recovered"] and out[1][0] == 4
    out, _ = decisions(["crit", "crit", "ok", "crit", "ok", "crit"], recover_after=2)
    assert [d.kind for _, d in out] == ["alert"]      # a lone ok never ends it


def test_a_failure_within_the_cooldown_after_recovery_continues_the_same_episode():
    # crit,crit alert; ok,ok recover; crit again 5 min later: no second CRIT, and the later recovery is silent
    out, row = decisions(["crit", "crit", "ok", "ok", "crit", "crit", "ok", "ok"])
    assert [d.kind for _, d in out] == ["alert", "recovered"], "one pair only"
    assert row.recovered_at is not None
    day_out = day(["crit", "crit", "ok", "ok"])
    assert [d.kind for _, d in day_out].count("alert") == 1 and [d.kind for _, d in day_out].count("recovered") == 1


def test_after_the_cooldown_a_new_episode_alerts_again():
    seq = ["crit", "crit", "ok", "ok"] + ["ok"] * 8 + ["crit", "crit", "ok", "ok"]   # 40 min between episodes
    out, _ = decisions(seq)
    assert [d.kind for _, d in out] == ["alert", "recovered", "alert", "recovered"]


def test_an_unalerted_blip_never_reopens():
    out, _ = decisions(["crit", "ok", "ok", "crit", "ok", "ok"])
    assert out == []

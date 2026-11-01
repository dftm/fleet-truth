"""Flap guard and check_state transitions on a fake clock (card 6 acceptance [1])."""
import pytest

from fleet_truth.checks.base import CheckResult
from fleet_truth.state import advance

STEP = 300


def drive(seq, confirm=2, step=STEP, t0=1_000_000):
    """seq: list of status strings or (status, verdict). Returns [(t, kind or None)] and final row."""
    row, out = None, []
    for i, item in enumerate(seq):
        status, verdict = item if isinstance(item, tuple) else (item, "V")
        now = t0 + i * step
        row, d = advance(row, "app", "CHK", CheckResult(status, verdict, "d"), now, confirm)
        out.append((now - t0, d.kind if d else None))
    return out, row


def kinds(out):
    return [k for _, k in out if k]


def test_fail_fail_one_alert():
    out, _ = drive(["crit", "crit"])
    assert kinds(out) == ["alert"] and out[1][1] == "alert"


def test_fail_then_ok_no_alert_at_all():
    out, _ = drive(["crit", "ok", "crit", "ok", "warn", "ok"])
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
    out, row = drive(["crit", "crit", "crit", "ok", "ok", "ok"])
    assert kinds(out) == ["alert", "recovered"]
    assert out[3][1] == "recovered" and row.alerted_status is None
    out2, _ = drive(["crit", "crit", "info", "info"])
    assert kinds(out2) == ["alert", "recovered"]  # info counts as healthy


def test_recovery_level_matches_what_was_alerted():
    row = None
    for i, s in enumerate(["warn", "warn", "ok"]):
        row, d = advance(row, "a", "c", CheckResult(s, "V", "d"), i * STEP, 2)
    assert d.kind == "recovered" and d.level == "warn"


def test_blip_does_not_arm_a_recovery():
    out, _ = drive(["crit", "ok", "crit", "ok", "ok"])
    assert kinds(out) == []


def test_status_change_resets_the_confirm_counter():
    out, _ = drive(["crit", "warn", "crit", "warn"])
    assert kinds(out) == []
    out, _ = drive(["warn", "warn", "crit", "crit"])
    assert kinds(out) == ["alert", "alert"]  # escalation alerts again once confirmed


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
    out, _ = drive(["unknown", "unknown", "ok", "unknown", "unknown", "ok"] * 3)
    assert kinds(out) == []


def test_recovery_from_blind():
    out, _ = drive(["unknown"] * 8 + ["ok"])
    assert kinds(out) == ["blind", "recovered"]


def test_since_tracks_status_change_and_consecutive_counts():
    _, row = drive(["ok", "ok", "crit", "crit", "crit"])
    assert row.consecutive == 3 and row.since == 1_000_000 + 2 * STEP


def test_decision_carries_prior_alert_fields_for_rollback():
    row = None
    for i, s in enumerate(["crit", "crit"]):
        row, d = advance(row, "a", "c", CheckResult(s, "V", "d"), i * STEP, 2)
    assert d.prev_alerted_status is None and row.alerted_status == "crit"

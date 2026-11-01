import sqlite3

import pytest

from fleet_truth.checks.base import CheckResult
from fleet_truth.store import MIGRATIONS, StateRow, Store, StoreError


def test_schema_wal_and_user_version(tmp_path):
    s = Store(tmp_path / "x.db")
    assert s.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    tables = {r[0] for r in s.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"runs", "results", "check_state", "alerts", "meta"} <= tables
    s.close()
    Store(tmp_path / "x.db").close()  # reopening is idempotent


def test_newer_schema_refused_and_corrupt_file_reports_rebuildable(tmp_path):
    p = tmp_path / "n.db"
    c = sqlite3.connect(p)
    c.execute(f"PRAGMA user_version={len(MIGRATIONS) + 5}")
    c.close()
    with pytest.raises(StoreError):
        Store(p)
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"this is not a database" * 100)
    with pytest.raises(StoreError) as e:
        Store(bad)
    assert "delete" in str(e.value)


def test_results_state_alerts_meta_roundtrip_and_caps(tmp_path):
    s = Store(tmp_path / "x.db")
    rid = s.start_run(100)
    s.add_result(rid, "a", "HEALTHZ", CheckResult("ok", "OK", "fine", {"x": 1}), 100)
    s.add_result(rid, "a", "DRIFT", CheckResult("warn", "BEHIND", "d", {"big": "x" * 5000}), 100)
    rows = {r["check_name"]: r for r in s.results_for_run(rid)}
    assert rows["HEALTHZ"]["data_json"] == '{"x": 1}'
    assert rows["DRIFT"]["data_json"] == '{"truncated": true}'
    s.put_state(StateRow("a", "HEALTHZ", "ok", "OK", 100, 1))
    assert s.get_state("a", "HEALTHZ").consecutive == 1 and s.get_state("a", "nope") is None
    s.record_alert("a", "c", "crit", "telegram", 100, False, "boom")
    s.record_alert("a", "c", "crit", "telegram", 200, True)
    s.record_alert("a", "c", "crit", "ntfy", 200, False, "x")
    assert s.last_alert_ok_by_channel() == {"telegram": True, "ntfy": False}
    s.meta_set("k", "v")
    assert s.meta_get("k") == "v" and s.meta_get("zz") is None


def test_retention_pruning(tmp_path):
    s = Store(tmp_path / "x.db")
    day = 86400
    now = 400 * day
    old, mid, new = s.start_run(now - 100 * day), s.start_run(now - 40 * day), s.start_run(now - day)
    for rid, t in ((old, now - 100 * day), (mid, now - 40 * day), (new, now - day)):
        s.add_result(rid, "a", "H", CheckResult("ok", "OK"), t)
    s.record_alert("a", "H", "ok", "tg", now - 366 * day, True)
    s.record_alert("a", "H", "ok", "tg", now - 10 * day, True)
    s.prune(now)
    assert [r["run_id"] for r in s.db.execute("SELECT run_id FROM results")] == [new]
    assert {r["id"] for r in s.db.execute("SELECT id FROM runs")} == {mid, new}  # runs kept 90 days
    assert [r["sent_at"] for r in s.db.execute("SELECT sent_at FROM alerts")] == [now - 10 * day]  # alerts 365 days


# ------------------------------------------------------------------ review 3 F5: upgrading keeps open episodes open
def old_db(tmp_path):
    """A database as the previous fix pass wrote it: schema 2, episode columns present but NULL for old rows."""
    import sqlite3
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(MIGRATIONS[0] + MIGRATIONS[1])
    c.execute("PRAGMA user_version=2")
    c.execute("INSERT INTO check_state(app,check_name,status,verdict,since,consecutive,alerted_status,last_alert_at)"
              " VALUES ('a','HEALTHZ','crit','DOWN',1000000,5,'crit',1000300)")
    c.execute("INSERT INTO check_state(app,check_name,status,verdict,since,consecutive,alerted_status,last_alert_at)"
              " VALUES ('a','OTHER','ok','OK',1000000,9,NULL,NULL)")
    c.commit()
    c.close()
    return p


def test_upgrade_reconstructs_the_open_episode(tmp_path):
    s = Store(old_db(tmp_path))
    st = s.get_state("a", "HEALTHZ")
    assert (st.unhealthy_since, st.unhealthy_cycles, st.worst_status, st.worst_verdict) == (1000000, 5, "crit", "DOWN")
    assert st.alerted_status == "crit" and st.last_alert_at == 1000300
    ok = s.get_state("a", "OTHER")
    assert ok.unhealthy_since is None and ok.worst_status is None
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


@pytest.mark.parametrize("then", ["warn", "unknown", "crit"])
def test_no_new_alert_after_upgrading_an_alerted_crit_episode(tmp_path, then):
    from fleet_truth import config, cycle
    from tests.test_cycle import FakeRunner
    s = Store(old_db(tmp_path))
    cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}}]})
    rn = FakeRunner(lambda app, now: iter([("HEALTHZ", CheckResult(then, "V", "d"))]))
    got = []
    for i in range(8):   # ~40 minutes: long enough for the blind timer too
        got += cycle.run_cycle(cfg, s, rn, now=1_000_900 + i * 300, deadline_s=5).decisions
    assert [d.kind for d in got if d.check == "HEALTHZ"] == [], got

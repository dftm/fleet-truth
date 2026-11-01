import fcntl
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

from fleet_truth import config
from fleet_truth.checks import files as F

LIB = Path("/home/dftms/worktrees/events-app/wt-67-backup/deploy/backup/backup_lib.py")
NOW = 1_790_000_000.0


def bcfg(tmp_path, **kw):
    return config.BackupCfg(str(tmp_path / "state" / "status.json"), "events-app", kw.get("max_age_hours", 26.0),
                            kw.get("required", False))


def write_status(tmp_path, **doc):
    p = tmp_path / "state"
    p.mkdir(exist_ok=True)
    (p / "status.json").write_text(json.dumps(doc))


def snapshot_dir(d: Path):
    return sorted((str(p.relative_to(d)), p.stat().st_mtime_ns) for p in d.rglob("*"))


CASES = {
    "never": None,
    "wrong_app": dict(app="other", result="ok", last_ok_epoch=NOW - 3600),
    "failed": dict(app="events-app", result="failed", stage="dump", timestamp="t", last_ok="x", last_ok_epoch=NOW - 3600),
    "stale": dict(app="events-app", result="ok", last_ok_epoch=NOW - 30 * 3600),
    "ok": dict(app="events-app", result="ok", last_ok_epoch=NOW - 3600, last_ok="2026-11-01T00:00:00"),
    "ok_edge": dict(app="events-app", result="ok", last_ok_epoch=NOW - 25.9 * 3600),
    "no_epoch": dict(app="events-app", result="ok"),
}
EXPECT = {"never": ("warn", "NEVER_RAN"), "wrong_app": ("crit", "WRONG_APP"), "failed": ("crit", "FAILED"),
          "stale": ("crit", "STALE"), "ok": ("ok", "OK"), "ok_edge": ("ok", "OK"), "no_epoch": ("crit", "STALE")}


@pytest.mark.parametrize("name", list(CASES))
def test_backup_age_verdicts(tmp_path, name):
    if CASES[name] is not None:
        write_status(tmp_path, **CASES[name])
    r = F.backup_age(bcfg(tmp_path), NOW)
    assert (r.status, r.verdict) == EXPECT[name]


def test_never_ran_is_crit_when_required(tmp_path):
    assert F.backup_age(bcfg(tmp_path, required=True), NOW).status == "crit"


def test_unreadable_status_file_is_crit(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "status.json").write_text("{not json")
    assert F.backup_age(bcfg(tmp_path), NOW).verdict == "UNREADABLE"


def _load_lib():
    if not LIB.exists():
        pytest.skip(f"backup_lib not present at {LIB}")
    sys.dont_write_bytecode = True  # never write .pyc into the other worktree
    spec = importlib.util.spec_from_file_location("backup_lib_ref", LIB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", list(CASES))
def test_same_ok_not_ok_as_backup_lib_check_status(tmp_path, name):
    lib = _load_lib()
    now = time.time()
    doc = CASES[name]
    if doc is not None:
        doc = {k: (v - NOW + now if k == "last_ok_epoch" else v) for k, v in doc.items()}
        write_status(tmp_path, **doc)
    state = tmp_path / "state"
    cfg = {"APP_NAME": "events-app", "STATE_DIR": str(state)}
    lib_ok, _ = lib._check_status(cfg, 26.0, "status.json")
    mine = F.backup_age(bcfg(tmp_path), now)
    assert (mine.status in ("ok", "info")) == lib_ok, (name, mine, lib_ok)


def test_held_lock_yields_running_and_nothing_is_created(tmp_path):
    write_status(tmp_path, **CASES["failed"])
    state = tmp_path / "state"
    (state / "lock").write_text("")
    before = snapshot_dir(state)
    fd = os.open(state / "lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        r = F.backup_age(bcfg(tmp_path), NOW)
        assert (r.status, r.verdict) == ("info", "RUNNING")
        # existing status already ok: not masked
        write_status(tmp_path, **CASES["ok"])
        assert F.backup_age(bcfg(tmp_path), NOW).verdict == "OK"
        # wrong app is never masked by a running backup
        write_status(tmp_path, **CASES["wrong_app"])
        assert F.backup_age(bcfg(tmp_path), NOW).verdict == "WRONG_APP"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    write_status(tmp_path, **CASES["failed"])
    assert F.backup_age(bcfg(tmp_path), NOW).verdict == "FAILED"  # lock released: judged normally
    assert sorted(p.name for p in state.iterdir()) == ["lock", "status.json"]


def test_check_never_creates_lock_or_any_file(tmp_path):
    for name in ("never", "failed", "ok"):
        if CASES[name] is not None:
            write_status(tmp_path, **CASES[name])
        F.backup_age(bcfg(tmp_path), NOW)
    state = tmp_path / "state"
    assert not (state / "lock").exists()
    assert sorted(p.name for p in state.iterdir()) == ["status.json"]
    # state dir absent altogether: still nothing created
    empty = config.BackupCfg(str(tmp_path / "nowhere" / "status.json"), "x", 26.0, False)
    F.backup_age(empty, NOW)
    assert not (tmp_path / "nowhere").exists()


# ------------------------------------------------------------------ HEARTBEAT
def hb(tmp_path, doc, field="daemon.last_beat", max_min=10.0, raw=None):
    p = tmp_path / "hb.json"
    p.write_text(raw if raw is not None else json.dumps(doc))
    return config.HeartbeatCfg(str(p), field, max_min)


def test_heartbeat_epoch_and_iso(tmp_path):
    assert F.heartbeat(hb(tmp_path, {"daemon": {"last_beat": NOW - 120}}), NOW).status == "ok"
    iso = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(NOW - 120))
    assert F.heartbeat(hb(tmp_path, {"daemon": {"last_beat": iso}}), NOW).status == "ok"
    z = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 120))
    assert F.heartbeat(hb(tmp_path, {"daemon": {"last_beat": z}}), NOW).status == "ok"
    plus2 = time.strftime("%Y-%m-%dT%H:%M:%S+02:00", time.gmtime(NOW - 120 + 7200))
    assert F.heartbeat(hb(tmp_path, {"daemon": {"last_beat": plus2}}), NOW).status == "ok"  # offset honoured
    r = F.heartbeat(hb(tmp_path, {"daemon": {"last_beat": NOW - 3600}}), NOW)
    assert (r.status, r.verdict) == ("crit", "STALE")


@pytest.mark.parametrize("doc,field", [
    ({}, "a"), ({"a": "garbage"}, "a"), ({"a": None}, "a"), ({"a": True}, "a"), ({"a": "2026-11-01"}, "a"),
    ({"a": "2026-11-01T10:00:00"}, "a"), ({"a": 1.7e12}, "a"), ({"a": [1]}, "a"), ({"a": {"b": 1}}, "a.b.c"),
    ({"a": NOW + 7200}, "a"),
])
def test_heartbeat_unknown_cases(tmp_path, doc, field):
    r = F.heartbeat(hb(tmp_path, doc, field), NOW)
    assert r.status == "unknown", r


def test_heartbeat_missing_or_broken_file(tmp_path):
    assert F.heartbeat(config.HeartbeatCfg(str(tmp_path / "no.json"), "a", 10), NOW).verdict == "MISSING"
    assert F.heartbeat(hb(tmp_path, None, "a", raw="{oops"), NOW).verdict == "UNPARSEABLE"


# ------------------------------------------------------------------ STALE_BUILD
def test_stale_build_date_only_is_unknown_never_alert():
    r = F.stale_build("2026-09-01", 45, NOW)
    assert (r.status, r.verdict) == ("unknown", "BUILD_TIME_NOT_SET")
    assert F.stale_build(None, 45, NOW).status == "unknown"
    assert F.stale_build("2026-11-01T10:00:00", 45, NOW).status == "unknown"  # no offset: not guessed


def test_stale_build_age_rules():
    old = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 60 * 86400))
    new = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(NOW - 5 * 86400))
    assert (F.stale_build(old, 45, NOW).status, F.stale_build(old, 45, NOW).verdict) == ("info", "OLD_BUILD")
    assert F.stale_build(new, 45, NOW).status == "ok"
    for s in (old, new, "2026-01-01", None, "junk"):
        assert F.stale_build(s, 45, NOW).status in ("ok", "info", "unknown")  # never warn/crit

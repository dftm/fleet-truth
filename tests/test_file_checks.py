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


# ------------------------------------------------------------------ bounded RUNNING guard (review F2)
def held(state):
    fd = os.open(state / "lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


def release(fd):
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


class Meta(dict):
    def get_(self, k):
        return dict.get(self, k)

    def set_(self, k, v):
        self[k] = v


def test_hung_backup_cannot_hide_a_30_day_stale_required_backup(tmp_path):
    """The reproduced scenario: lock held forever, status 30 days stale, required: true."""
    write_status(tmp_path, **{**CASES["stale"], "last_ok_epoch": NOW - 30 * 86400})
    cfg = bcfg(tmp_path, required=True)
    fd = held(tmp_path / "state")
    m = Meta()
    try:
        r0 = F.backup_age(cfg, NOW, interval_s=300, meta_get=m.get_, meta_set=m.set_)
        assert (r0.status, r0.verdict) == ("info", "RUNNING")           # first sighting: masked
        r1 = F.backup_age(cfg, NOW + 5 * 3600, interval_s=300, meta_get=m.get_, meta_set=m.set_)
        assert r1.verdict == "RUNNING"                                   # inside the 6 h default
        r2 = F.backup_age(cfg, NOW + 7 * 3600, interval_s=300, meta_get=m.get_, meta_set=m.set_)
        assert (r2.status, r2.verdict) == ("crit", "LOCK_STUCK")
        assert "backup lock held for 7.0 hours" in r2.detail and "STALE" in r2.detail
    finally:
        release(fd)
    # lock released: normal judgement, and the held-since marker is cleared
    r3 = F.backup_age(cfg, NOW + 8 * 3600, interval_s=300, meta_get=m.get_, meta_set=m.set_)
    assert r3.verdict == "STALE"
    fd = held(tmp_path / "state")
    try:
        assert F.backup_age(cfg, NOW + 9 * 3600, interval_s=300, meta_get=m.get_, meta_set=m.set_).verdict == "RUNNING"
    finally:
        release(fd)


def test_running_grace_defaults_and_config(tmp_path):
    assert F.running_grace_hours(bcfg(tmp_path), 300) == 6.0
    assert F.running_grace_hours(bcfg(tmp_path), 4 * 3600) == 8.0          # 2 x interval wins when larger
    custom = config.BackupCfg("x", "a", 26.0, False, 1.5)
    assert F.running_grace_hours(custom, 300) == 1.5
    d = {"apps": [{"name": "a", "backup": {"status_file": "/s/status.json", "app_name": "a", "running_grace": 3}}]}
    assert config.parse(d).apps[0].backup.running_grace_hours == 3.0


def test_grace_without_store_falls_back_to_lock_mtime(tmp_path):
    write_status(tmp_path, **CASES["stale"])
    state = tmp_path / "state"
    fd = held(state)
    try:
        old = NOW - 10 * 3600
        os.utime(state / "lock", (old, old))
        assert F.backup_age(bcfg(tmp_path), NOW).verdict == "LOCK_STUCK"
    finally:
        release(fd)


def test_fresh_ok_status_with_a_lock_held_beyond_the_grace_is_lock_stuck(tmp_path):
    """Review 2 R6: the window starts when the lock is first SEEN held, whatever the status says."""
    write_status(tmp_path, **CASES["ok"])
    fd = held(tmp_path / "state")
    m = Meta()
    try:
        r = F.backup_age(bcfg(tmp_path), NOW, meta_get=m.get_, meta_set=m.set_)
        assert r.verdict == "OK" and m  # recorded the first sighting
        assert F.backup_age(bcfg(tmp_path), NOW + 5 * 3600, meta_get=m.get_, meta_set=m.set_).verdict == "OK"
        r = F.backup_age(bcfg(tmp_path), NOW + 7 * 3600, meta_get=m.get_, meta_set=m.set_)
        assert (r.status, r.verdict) == ("crit", "LOCK_STUCK") and "7.0 hours" in r.detail and r.data["would_be"] == "OK"
    finally:
        release(fd)
    assert F.backup_age(bcfg(tmp_path), NOW + 8 * 3600, meta_get=m.get_, meta_set=m.set_).verdict == "OK"   # released: cleared


def test_wrong_app_and_unreadable_are_not_turned_into_lock_stuck(tmp_path):
    write_status(tmp_path, **CASES["wrong_app"])
    fd = held(tmp_path / "state")
    m = Meta()
    try:
        F.backup_age(bcfg(tmp_path), NOW, meta_get=m.get_, meta_set=m.set_)
        assert F.backup_age(bcfg(tmp_path), NOW + 20 * 3600, meta_get=m.get_, meta_set=m.set_).verdict == "WRONG_APP"
    finally:
        release(fd)


# ------------------------------------------------------------------ review 2 R7: the probe never takes the lock
def proc_locks_line(lockfile, kind="FLOCK", mode="WRITE"):
    st = os.stat(lockfile)
    return f"1: {kind}  ADVISORY  {mode} 4242 {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n"


def test_lock_detection_reads_proc_locks_and_matches_device_and_inode(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "lock").write_text("")
    other = tmp_path / "other"
    other.write_text("")
    f = tmp_path / "locks"
    f.write_text("1: POSIX  ADVISORY  READ 1 00:00:1 0 EOF\n" + proc_locks_line(other))
    assert F.lock_is_held(state, None, str(f)) is False                       # a lock on a different inode
    f.write_text("garbage line\n1: FLOCK ADVISORY WRITE x notaninode 0 EOF\n" + proc_locks_line(state / "lock", "OFDLCK"))
    assert F.lock_is_held(state, None, str(f)) is True
    st = os.stat(state / "lock")
    f.write_text(f"1: FLOCK  ADVISORY  WRITE 9 {(os.major(st.st_dev) + 1) % 256:02x}:{os.minor(st.st_dev):02x}:{st.st_ino} 0 EOF\n")
    assert F.lock_is_held(state, None, str(f)) is False                       # right inode, wrong device


def test_lock_detection_fallback_without_proc_locks(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "lock").write_text("")
    assert F.lock_is_held(state, {"running": True}, str(tmp_path / "nope")) is True
    assert F.lock_is_held(state, {"running": False}, str(tmp_path / "nope")) is False
    assert F.lock_is_held(state, None, str(tmp_path / "nope")) is False
    assert F.lock_is_held(tmp_path / "absent", {"running": True}, str(tmp_path / "nope")) is False


def test_real_flock_held_by_another_process_is_seen_and_never_contended(tmp_path, monkeypatch):
    import subprocess
    import sys
    state = tmp_path / "state"
    state.mkdir()
    (state / "lock").write_text("")
    if not os.path.exists("/proc/locks"):
        pytest.skip("no /proc/locks")
    code = ("import fcntl,os,sys,time\n"
            "fd=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\n"
            "print('held',flush=True); time.sleep(30)\n")
    p = subprocess.Popen([sys.executable, "-c", code, str(state / "lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        assert F.lock_is_held(state) is True
    finally:
        p.kill()
        p.wait()
    assert F.lock_is_held(state) is False
    # the probe itself never calls flock/lockf at all
    def boom(*a, **k):
        raise AssertionError("the monitor must never take the backup's lock")
    monkeypatch.setattr(F.fcntl, "flock", boom, raising=False)
    monkeypatch.setattr(F.fcntl, "lockf", boom, raising=False)
    write_status(tmp_path, **CASES["ok"])
    assert F.backup_age(bcfg(tmp_path), NOW).verdict == "OK"


# ------------------------------------------------------------------ implausible status timestamps (review F3)
@pytest.mark.parametrize("raw,reason", [
    ("NaN", "not finite"), ("Infinity", "not finite"), ("-Infinity", "not finite"),
    (str(int((NOW + 400 * 86400) * 1000)), "milliseconds"), (str(int(NOW) * 1000), "milliseconds"),
    (str(int(NOW + 2 * 86400)), "in the future"), ("0", "not positive"), ("-5", "not positive"),
    ('"1790000000"', "not a number"), ("true", "not a number"), ("null", "not a number"), ("[1]", "not a number"),
])
def test_backup_age_rejects_impossible_timestamps(tmp_path, raw, reason):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "status.json").write_text('{"app": "events-app", "result": "ok", "last_ok_epoch": %s}' % raw)
    r = F.backup_age(bcfg(tmp_path), NOW)
    assert (r.status, r.verdict) == ("crit", "UNREADABLE") and reason in r.detail


def test_small_clock_skew_is_tolerated_and_missing_epoch_stays_stale(tmp_path):
    write_status(tmp_path, app="events-app", result="ok", last_ok_epoch=NOW + 60)
    assert F.backup_age(bcfg(tmp_path), NOW).verdict == "OK"
    write_status(tmp_path, app="events-app", result="ok")
    assert F.backup_age(bcfg(tmp_path), NOW).verdict == "STALE"


def test_unreadable_is_not_masked_by_a_running_lock(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "status.json").write_text('{"app": "events-app", "result": "ok", "last_ok_epoch": NaN}')
    fd = held(tmp_path / "state")
    try:
        assert F.backup_age(bcfg(tmp_path), NOW).verdict == "UNREADABLE"
    finally:
        release(fd)

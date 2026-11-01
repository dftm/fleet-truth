"""Review F6/F7: corrupt or wiped DB, run lock."""
import fcntl
import os

import pytest

from fleet_truth import cli, config, runtime
from fleet_truth.checks.base import CheckResult
from tests.test_alerts import NTFY, R_crit, R_ok, Stub, dm_cfg  # noqa: F401  (fixtures: env autouse is imported with them)
from tests.test_alerts import env  # noqa: F401


def test_corrupt_db_is_moved_aside_not_deleted_and_fail_ping_and_reset_alert_go_out(tmp_path):
    cfg = dm_cfg(tmp_path, NTFY)
    db = runtime.db_path(cfg)
    runtime.run_once(cfg, runner_factory=R_ok, now=1000, fetch=Stub(body=b"{}")).store.close()
    junk = b"this is definitely not sqlite" * 50
    db.write_bytes(junk)
    stub = Stub(body=b"{}")
    o = runtime.run_once(cfg, runner_factory=R_ok, now=2000, fetch=stub)
    moved = [p for p in tmp_path.iterdir() if ".corrupt-" in p.name]
    assert len(moved) == 1 and moved[0].read_bytes() == junk, "the bad file is kept, byte for byte"
    urls = [c["url"] for c in stub.calls]
    assert urls[0].endswith("/ping/SEKRIT-dm/fail") and b"StateReset" in stub.calls[0]["body"]
    assert any("ntfy.sh" in u for u in urls)
    body = [c for c in stub.calls if "ntfy.sh" in c["url"]][0]["body"].decode()
    assert body.startswith("[RESET] Fleet Truth state was reset") and ".corrupt-" in body
    assert o.store.meta_get("state_reset_at")
    o.store.close()
    # next run is normal: no reset alert
    stub2 = Stub(body=b"{}")
    runtime.run_once(cfg, runner_factory=R_ok, now=3000, fetch=stub2).store.close()
    assert not [c for c in stub2.calls if "ntfy.sh" in c["url"]]


def test_unopenable_data_dir_pings_fail_and_raises(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}}],
                        "dead_man": {"url_env": "DM_URL"}, "data_dir": str(blocker / "sub")})
    stub = Stub()
    with pytest.raises(Exception):
        runtime.run_once(cfg, runner_factory=R_ok, fetch=stub)
    assert [c["url"] for c in stub.calls] == ["https://hc.example.com/ping/SEKRIT-dm/fail"]


def test_wiped_db_forces_a_conservative_channel_retest(tmp_path):
    cfg = dm_cfg(tmp_path, NTFY)
    first = Stub(body=b"{}")
    o = runtime.run_once(cfg, runner_factory=R_ok, now=1000, fetch=first)
    assert not [c for c in first.calls if "ntfy.sh" in c["url"]], "a first-ever run is not a reset"
    o.store.close()
    db = runtime.db_path(cfg)
    for suffix in ("", "-wal", "-shm"):
        p = db.with_name(db.name + suffix)
        if p.exists():
            p.unlink()
    dead = Stub(status=500)
    o2 = runtime.run_once(cfg, runner_factory=R_ok, now=2000, fetch=dead)
    sent = [c for c in dead.calls if "ntfy.sh" in c["url"]]
    assert sent and sent[0]["body"].decode().startswith("[RESET]")
    # the channel failed its re-test: channel health is NOT assumed good after the wipe
    assert o2.store.last_alert_ok_by_channel() == {"ntfy": False}
    assert o2.dead_man.action != "pinged"
    o2.store.close()
    good = Stub(body=b"{}")
    o3 = runtime.run_once(cfg, runner_factory=R_ok, now=3000, fetch=good)
    assert o3.dead_man.action == "withheld"  # still unhealthy until a send succeeds
    o3.store.close()


def test_second_overlapping_run_exits_5_without_alerts(tmp_path, capsys, monkeypatch):
    f = tmp_path / "fleet.yaml"
    f.write_text(f"data_dir: {tmp_path / 'd'}\napps: [{{name: a, health: {{url: 'https://a.example.com/'}}}}]\n")
    (tmp_path / "d").mkdir()
    fd = os.open(tmp_path / "d" / "run.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    called = []
    monkeypatch.setattr(runtime, "Runner", lambda *a, **k: called.append(1))
    try:
        assert cli.main(["once", "-c", str(f)]) == 5
        assert "run lock" in capsys.readouterr().err and not called
        with pytest.raises(runtime.RunLocked):
            runtime.run_once(config.load(f), runner_factory=R_ok, fetch=Stub())
    finally:
        os.close(fd)
    cfg = config.load(f)
    runtime.run_once(cfg, runner_factory=R_ok, now=1, senders=[]).store.close()   # lock released: runs normally


def test_lock_is_released_after_a_failing_run(tmp_path, monkeypatch):
    from fleet_truth import cycle
    cfg = dm_cfg(tmp_path, NTFY)
    monkeypatch.setattr(cycle, "run_cycle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        runtime.run_once(cfg, runner_factory=R_ok, fetch=Stub())
    monkeypatch.undo()
    runtime.run_once(cfg, runner_factory=R_ok, now=5, fetch=Stub(body=b"{}")).store.close()


# ------------------------------------------------------------------ review 2 R4: a broken lock is not a busy lock
def test_enolck_is_an_internal_error_with_fail_ping_and_exit_4(tmp_path, monkeypatch, capsys):
    import errno
    import fcntl as fc
    cfg = dm_cfg(tmp_path, NTFY)
    real = fc.flock

    def broken(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(runtime.fcntl, "flock", broken)
    stub = Stub()
    with pytest.raises(runtime.LockError) as e:
        runtime.run_once(cfg, runner_factory=R_ok, fetch=stub)
    assert "ENOLCK" in str(e.value) and "/mnt/c" in str(e.value)
    assert [c["url"] for c in stub.calls] == ["https://hc.example.com/ping/SEKRIT-dm/fail"]
    f = tmp_path / "fleet.yaml"
    f.write_text(f"data_dir: {tmp_path / 'd2'}\napps: [{{name: a, health: {{url: 'https://a.example.com/'}}}}]\n")
    assert cli.main(["once", "-c", str(f)]) == 4
    assert "ENOLCK" in capsys.readouterr().err
    for code in (errno.EACCES, errno.EINVAL, errno.EIO):
        monkeypatch.setattr(runtime.fcntl, "flock", lambda fd, op, c=code: (_ for _ in ()).throw(OSError(c, "x")))
        with pytest.raises(runtime.LockError):
            runtime.run_once(cfg, runner_factory=R_ok, fetch=Stub())
    monkeypatch.setattr(runtime.fcntl, "flock", real)


def test_real_contention_is_still_exit_5(tmp_path, capsys):
    f = tmp_path / "fleet.yaml"
    f.write_text(f"data_dir: {tmp_path / 'd'}\napps: [{{name: a, health: {{url: 'https://a.example.com/'}}}}]\n")
    (tmp_path / "d").mkdir()
    fd = os.open(tmp_path / "d" / "run.lock", os.O_CREAT | os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        assert cli.main(["once", "-c", str(f)]) == 5
    finally:
        os.close(fd)


def test_check_config_warns_about_mnt_data_dir(tmp_path, capsys):
    f = tmp_path / "fleet.yaml"
    f.write_text("data_dir: /mnt/c/fleet\napps: [{name: a, health: {url: 'https://a.example.com/'}}]\n")
    assert cli.main(["check-config", str(f)]) == 0
    assert "warning: data_dir is under /mnt/" in capsys.readouterr().out


# ------------------------------------------------------------------ review 2 R6: the marker survives a wiped data dir
def test_wiping_the_whole_data_dir_is_detected(tmp_path):
    import shutil
    data = tmp_path / "data" / "ft"
    cfg = config.parse({"apps": [{"name": "a", "health": {"url": "https://a.example.com/"}}], "alerts": [NTFY],
                        "data_dir": str(data), "dead_man": {"url_env": "DM_URL"}})
    first = Stub(body=b"{}")
    runtime.run_once(cfg, runner_factory=R_ok, now=1000, fetch=first).store.close()
    marker = runtime.marker_path(cfg)
    assert marker.exists() and data not in marker.parents, "the marker must live outside the data dir"
    shutil.rmtree(data)
    stub = Stub(body=b"{}")
    o = runtime.run_once(cfg, runner_factory=R_ok, now=2000, fetch=stub)
    sent = [c["body"].decode() for c in stub.calls if "ntfy.sh" in c["url"]]
    assert sent and sent[0].startswith("[RESET]") and "missing" in sent[0]
    o.store.close()


def test_marker_goes_next_to_the_config_file(tmp_path, monkeypatch):
    conf = tmp_path / "etc" / "fleet.yaml"
    conf.parent.mkdir()
    conf.write_text(f"data_dir: {tmp_path / 'd'}\napps: [{{name: a, health: {{url: 'https://a.example.com/'}}}}]\n")
    cfg = config.load(conf)
    assert runtime.marker_path(cfg, conf).parent == conf.parent
    monkeypatch.setattr(runtime, "Runner", R_ok)
    assert cli.main(["once", "-c", str(conf)]) == 0
    assert list(conf.parent.glob(".fleet-truth-*.initialized"))


# ------------------------------------------------------------------ review 2 R8: rollback is not corruption
def test_database_from_a_newer_build_is_refused_not_renamed(tmp_path, capsys):
    import sqlite3
    from fleet_truth.store import MIGRATIONS
    cfg = dm_cfg(tmp_path, NTFY)
    db = runtime.db_path(cfg)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE keepme(x)")
    c.execute(f"PRAGMA user_version={len(MIGRATIONS) + 3}")
    c.commit()
    c.close()
    before = db.read_bytes()
    for _ in range(3):
        stub = Stub()
        with pytest.raises(runtime.SchemaTooNew):
            runtime.run_once(cfg, runner_factory=R_ok, fetch=stub)
        assert [c["url"] for c in stub.calls] == ["https://hc.example.com/ping/SEKRIT-dm/fail"]
    assert db.exists() and db.read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if "corrupt" in p.name]
    f = tmp_path / "fleet.yaml"
    f.write_text(f"data_dir: {tmp_path}\napps: [{{name: a, health: {{url: 'https://a.example.com/'}}}}]\n")
    assert cli.main(["once", "-c", str(f)]) == 4
    assert "newer than this build" in capsys.readouterr().err


def test_corrupt_copies_are_capped_and_the_first_is_never_deleted(tmp_path):
    cfg = dm_cfg(tmp_path, NTFY)
    db = runtime.db_path(cfg)
    for i in range(7):
        db.write_bytes(f"corrupt generation {i}".encode() * 20)
        runtime.run_once(cfg, runner_factory=R_ok, now=1000 + i, fetch=Stub(body=b"{}")).store.close()
    copies = sorted(p for p in tmp_path.iterdir() if ".corrupt-" in p.name and not p.name.endswith(("-wal", "-shm")))
    assert len(copies) == runtime.MAX_CORRUPT_COPIES == 3
    contents = [p.read_bytes() for p in copies]
    assert contents[0].startswith(b"corrupt generation 0"), "the first corrupt copy is kept for good"
    assert contents[-1].startswith(b"corrupt generation 6") and contents[-2].startswith(b"corrupt generation 5")

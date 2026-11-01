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

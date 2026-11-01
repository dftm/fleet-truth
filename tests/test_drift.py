"""Spike tests (spike/test_check_drift.py) ported to the mirror, plus mirror-specific cases."""
import os
import time
from pathlib import Path

import pytest

from fleet_truth import gitmirror as gm
from fleet_truth.checks import drift as D
from tests.gitfix import ENV, T0, Repos, sh


@pytest.fixture
def R(tmp_path):
    r = Repos(tmp_path)
    r.c1 = r.commit("1.0.0", T0 - 72 * 3600)
    r.c2 = r.commit("1.0.1", T0 - 48 * 3600)
    r.push()
    res = r.refresh()
    assert res.origin_ok and res.local_ok, res
    return r


def verdict(R, live, now=None, grace=24.0):
    return D.compute_drift(R.mirror, live, "main", "VERSION", grace, now if now is not None else time.time(),
                           gm.last_fetch_ok(R.mirror))


def test_in_sync_full_and_short_sha(R):
    assert verdict(R, {"commit": R.c2})["verdict"] == "IN_SYNC"
    assert verdict(R, {"commit": R.c2[:7]})["verdict"] == "IN_SYNC"
    assert verdict(R, {"commit": R.c2[:7].upper()})["verdict"] == "IN_SYNC"


def test_behind_within_and_after_grace(R):
    r = verdict(R, {"commit": R.c1}, now=T0 - 47 * 3600)
    assert (r["verdict"], r["severity"], r["behind"]) == ("PENDING_DEPLOY", "info", 1)
    r = verdict(R, {"commit": R.c1}, now=T0)
    assert (r["verdict"], r["severity"]) == ("BEHIND", "warn")


def test_live_runs_unpushed_commit_is_crit(R):
    """The events-app incident: a live feature built from commits only on the laptop."""
    c3 = R.commit("1.1.0", T0 - 3600)
    assert R.refresh().local_ok  # the commit enters the mirror through refs/local
    r = verdict(R, {"commit": c3[:7]}, now=T0)
    assert (r["verdict"], r["severity"], r["not_on_origin"]) == ("LIVE_NOT_ON_ORIGIN", "crit", 1)
    assert r["unpushed"]["count"] == 1
    assert r["unpushed"]["oldest_h"] == pytest.approx(1.0)


def test_unpushed_commit_not_yet_in_mirror_is_live_unknown(R):
    c3 = R.commit("1.1.0", T0 - 3600)
    assert verdict(R, {"commit": c3[:7]}, now=T0)["verdict"] == "LIVE_UNKNOWN"  # no refresh since the commit


def test_unknown_commit_fresh_fetch_is_crit_stale_is_warn(R):
    ghost = "deadbee"
    r = verdict(R, {"commit": ghost})
    assert (r["verdict"], r["severity"]) == ("LIVE_UNKNOWN", "crit")
    f = R.mirror / gm.LAST_FETCH_FILE
    f.write_text(f"{time.time() - 7 * 3600:.0f}\n")
    assert verdict(R, {"commit": ghost})["severity"] == "warn"
    f.unlink()
    assert verdict(R, {"commit": ghost})["severity"] == "warn"


def test_no_commit_falls_back_to_version(R):
    assert verdict(R, {"commit": None, "version": "1.0.1"})["verdict"] == "NO_COMMIT_VERSION_MATCH"
    assert verdict(R, {"version": "1.0.0"})["verdict"] == "NO_COMMIT_VERSION_DIFF"
    assert verdict(R, {})["verdict"] == "NO_COMMIT"


def test_hostile_commit_values_never_reach_git_as_options(R):
    for bad in ["--output=/tmp/x", "HEAD", "main", "abc", "g" * 40, 123, ["a"], "a" * 41,
                "origin/main", "refs/remotes/origin/main", "refs/local/main", "refs/local/main~0", "origin/main^{commit}",
                "-1", "--all", " --exec=x"]:
        r = verdict(R, {"commit": bad})
        assert r["verdict"] in ("LIVE_UNKNOWN", "NO_COMMIT", "NO_COMMIT_VERSION_DIFF"), bad
        assert r["verdict"] != "IN_SYNC", bad


def test_missing_origin_ref_is_unknown_check_result(tmp_path):
    r = Repos(tmp_path)
    r.commit("1", T0)
    assert D.drift_check(r.mirror, {"commit": "abc1234"}, "main", "VERSION", 24).verdict == "NO_MIRROR"
    r.push(); r.refresh()
    r2 = D.drift_check(r.mirror, {"commit": "abc1234"}, "main", "VERSION", 24)
    assert r2.verdict == "LIVE_UNKNOWN" and r2.status == "crit"


def test_drift_check_maps_to_check_result(R):
    r = D.drift_check(R.mirror, {"commit": R.c2}, "main", "VERSION", 24)
    assert (r.status, r.verdict) == ("ok", "IN_SYNC")
    assert r.data["origin"] == R.c2 and "unpushed" not in r.data


# ------------------------------------------------------------------ mirror refresh
def test_refresh_against_file_origin_and_rate_limit(tmp_path):
    r = Repos(tmp_path)
    c = r.commit("1", T0)
    r.push()
    gm.ARGV_LOG = []
    try:
        t = 1_000_000.0
        res = r.refresh(now=t, force=False)
        assert res.origin_ok and res.local_ok and not res.skipped
        assert gm.last_fetch_ok(r.mirror) == t
        assert gm.resolve(r.mirror, "refs/remotes/origin/main") == c
        n = len(gm.ARGV_LOG)
        again = r.refresh(now=t + 14 * 60, force=False)
        assert again.skipped and len(gm.ARGV_LOG) > n - 1 and not any("fetch" in a for a in gm.ARGV_LOG[n:])
        due = r.refresh(now=t + 16 * 60, force=False)
        assert not due.skipped and gm.last_fetch_ok(r.mirror) == t + 16 * 60
    finally:
        gm.ARGV_LOG = None


def test_unpushed_commit_only_in_working_clone_detected_through_refs_local(R):
    c3 = R.commit("1.1.0", T0 - 7200, msg="wip: feature")
    R.refresh()
    assert gm.resolve(R.mirror, "refs/local/main") == c3
    assert gm.resolve(R.mirror, "refs/remotes/origin/main") == R.c2
    u = D.unpushed_check(R.mirror, R.repo, "main", 24, 24, now=T0)
    assert (u.status, u.verdict) == ("info", "UNPUSHED_COMMITS")
    assert u.data["count"] == 1 and u.data["subjects"] == ["wip: feature"]


def test_failed_origin_fetch_keeps_last_ok_and_records_error(tmp_path):
    r = Repos(tmp_path)
    r.commit("1", T0); r.push()
    r.refresh(now=1000.0)
    bad = gm.MirrorSpec(r.spec.name, r.spec.mirror_dir, "main", "https://127.0.0.1:1/x/y.git", r.repo)
    res = gm.refresh(bad, 5000.0, 15, force=True)
    assert res.origin_ok is False and res.local_ok is True and res.error
    assert gm.last_fetch_ok(r.mirror) == 1000.0


def test_local_branch_missing_drops_stale_local_ref(tmp_path):
    r = Repos(tmp_path)
    r.commit("1", T0); r.push(); r.refresh()
    assert gm.resolve(r.mirror, "refs/local/main")
    sh(r.repo, "checkout", "-q", "-b", "other")
    sh(r.repo, "branch", "-q", "-D", "main")
    res = r.refresh()
    assert res.local_ok is False
    assert gm.resolve(r.mirror, "refs/local/main") is None
    assert D.unpushed_check(r.mirror, r.repo, "main", 24, 24).verdict == "NO_LOCAL_BRANCH"


# ------------------------------------------------------------------ UNPUSHED
def test_unpushed_ok_age_escalation_and_subject_limits(tmp_path):
    r = Repos(tmp_path)
    r.commit("0", T0 - 100 * 3600); r.push(); r.refresh()
    assert D.unpushed_check(r.mirror, r.repo, "main", 24, 24, now=T0).status == "ok"
    for i in range(12):
        r.commit(f"v{i}", T0 - 30 * 3600 + i, msg=("x" * 200) if i == 0 else f"commit {i}\x1b[31mred")
    r.refresh()
    u = D.unpushed_check(r.mirror, r.repo, "main", 24, 24, now=T0)
    assert (u.status, u.verdict) == ("warn", "UNPUSHED_COMMITS") and u.data["count"] == 12
    assert len(u.data["subjects"]) == 10 and all(len(s) <= 120 for s in u.data["subjects"])
    assert all("\x1b" not in s for s in u.data["subjects"])
    assert D.unpushed_check(r.mirror, r.repo, "main", 48, 24, now=T0).status == "info"


def test_dirty_tracked_files_info_only_after_dirty_hours(tmp_path):
    r = Repos(tmp_path)
    r.commit("0", T0 - 100 * 3600); r.push(); r.refresh()
    (r.repo / "VERSION").write_text("dirty\n")
    (r.repo / "untracked.txt").write_text("x")
    fresh = D.unpushed_check(r.mirror, r.repo, "main", 24, 24, now=time.time())
    assert fresh.status == "ok"
    old = time.time() - 30 * 3600
    os.utime(r.repo / "VERSION", (old, old))
    u = D.unpushed_check(r.mirror, r.repo, "main", 24, 24, now=time.time())
    assert (u.status, u.verdict) == ("info", "DIRTY_FILES") and u.data["dirty_files"] == 1


def test_mirror_only_app_skips_unpushed(R):
    assert D.unpushed_check(R.mirror, None, "main", 24, 24).verdict == "NOT_CONFIGURED"


# ------------------------------------------------------------------ safety: working clone untouched
def snapshot(gitdir: Path) -> dict:
    out = {}
    for p in sorted(gitdir.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[p.relative_to(gitdir).as_posix()] = (st.st_size, st.st_mtime_ns, p.read_bytes() if st.st_size < 65536 else b"")
    return out


def test_working_clone_dot_git_unchanged_after_full_run(R):
    (R.repo / "VERSION").write_text("dirty\n")  # forces `status` to look at the index
    R.commit("1.1.0", T0 - 3600)
    (R.repo / "VERSION").write_text("dirty again\n")
    refs_before = sh(R.repo, "for-each-ref")
    before = snapshot(R.repo / ".git")
    for _ in range(2):
        R.refresh()
        D.unpushed_check(R.mirror, R.repo, "main", 24, 24)
        D.drift_check(R.mirror, {"commit": R.c2}, "main", "VERSION", 24)
        gm.dirty_files(R.repo)
    after = snapshot(R.repo / ".git")
    assert set(after) == set(before), set(after) ^ set(before)
    assert after == before
    assert sh(R.repo, "for-each-ref") == refs_before
    assert not (R.repo / ".git" / "index.lock").exists()


def test_hooks_and_fsmonitor_in_working_clone_never_execute(R):
    R.commit("1.1.0", T0 - 3600)  # the test's own git calls run first, before the trap is armed
    marker = R.root / "PWNED"
    script = R.root / "evil.sh"
    script.write_text(f"#!/bin/sh\necho ran >> {marker}\nexit 0\n")
    script.chmod(0o755)
    hooks = R.repo / ".git" / "hooks"
    for name in ("post-checkout", "post-merge", "reference-transaction", "pre-auto-gc", "fsmonitor-watchman",
                 "post-update", "pre-push", "post-commit", "update", "pre-receive", "post-receive"):
        (hooks / name).write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
        (hooks / name).chmod(0o755)
    for key in ("core.fsmonitor", "uploadpack.packObjectsHook", "core.sshCommand", "diff.external"):
        sh(R.repo, "config", key, str(script))
    sh(R.repo, "config", "core.hooksPath", str(hooks))
    (R.repo / "VERSION").write_text("dirty\n")  # a tracked change, so status has work to do
    R.refresh()
    D.unpushed_check(R.mirror, R.repo, "main", 24, 24)
    gm.dirty_files(R.repo)
    assert not marker.exists(), marker.read_text() if marker.exists() else ""
    # and the mirror itself is configured to run nothing
    assert sh(R.mirror, "config", "core.hooksPath") == os.devnull
    assert sh(R.mirror, "config", "core.fsmonitor") == "false"


# ------------------------------------------------------------------ safety: token handling
def test_token_never_in_mirror_config_argv_logs_or_errors(tmp_path, caplog, monkeypatch):
    from fleet_truth import secrets
    token = "ghp_SEKRITtoken1234567890"
    monkeypatch.setenv("FLEET_GITHUB_TOKEN", token)
    secrets.register("FLEET_GITHUB_TOKEN")
    r = Repos(tmp_path)
    r.commit("1", T0)
    ask = gm.write_askpass(r.data)
    bad = gm.MirrorSpec("app", r.mirror, "main", "https://127.0.0.1:1/dftm/x.git", r.repo)
    gm.ARGV_LOG = []
    try:
        import logging
        with caplog.at_level(logging.DEBUG):
            res = gm.refresh(bad, 1.0, 15, token=token, askpass=ask, force=True)
        assert res.origin_ok is False and res.error
        flat = " ".join(" ".join(a) for a in gm.ARGV_LOG)
        assert token not in flat
        assert token not in res.error and token not in caplog.text
        assert token not in (r.mirror / "config").read_text()
        assert not any(token in p.read_text(errors="ignore") for p in r.mirror.rglob("*") if p.is_file())
        assert token not in ask.read_text()
    finally:
        gm.ARGV_LOG = None
    # the helper hands git the token from the environment only
    import subprocess
    env = {"PATH": os.environ["PATH"], gm.ASKPASS_TOKEN_VAR: token}
    pw = subprocess.run([str(ask), "Password for 'https://x@github.com': "], env=env, capture_output=True, text=True)
    user = subprocess.run([str(ask), "Username for 'https://github.com': "], env=env, capture_output=True, text=True)
    assert pw.stdout.strip() == token and user.stdout.strip() == "x-access-token"
    assert oct(ask.stat().st_mode & 0o777) == "0o700"


def test_origin_url_with_credentials_refused(tmp_path):
    r = Repos(tmp_path)
    spec = gm.MirrorSpec("app", r.mirror, "main", "https://user:pw@github.com/a/b.git", r.repo)
    res = gm.refresh(spec, 1.0, 15, force=True)
    assert res.origin_ok is None and "credentials" in res.error


def test_error_text_hides_url_userinfo():
    assert "pw" not in gm._safe_err("fatal: unable to access 'https://user:pw@github.com/x/': 403", ["fetch"])


def test_protocols_are_limited_in_every_fetch(R):
    gm.ARGV_LOG = []
    try:
        R.refresh()
        fetches = [a for a in gm.ARGV_LOG if "fetch" in a]
        assert len(fetches) == 2
        for a in fetches:
            joined = " ".join(a)
            for needle in ("protocol.allow=never", "protocol.https.allow=always", "core.hooksPath=/dev/null",
                           "core.fsmonitor=false", "credential.helper="):
                assert needle in joined
    finally:
        gm.ARGV_LOG = None

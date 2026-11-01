"""Tests for the drift spike. Stdlib unittest; builds throwaway repos under a temp dir.

Run: python3 -m unittest discover -s spike -v   (from the repo root)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import check_drift as cd  # noqa: E402

T0 = 1_790_000_000  # fixed "now"-ish epoch for deterministic ages
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout.strip()


class Fleet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        self.repo = root / "clone"
        sh(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        sh(root, "clone", "-q", str(self.origin), str(self.repo))
        sh(self.repo, "checkout", "-q", "-b", "main")
        self.c1 = self.commit("1.0.0", T0 - 72 * 3600)
        self.c2 = self.commit("1.0.1", T0 - 48 * 3600)
        sh(self.repo, "push", "-q", "origin", "main")
        sh(self.repo, "fetch", "-q")  # creates FETCH_HEAD; mtime is "now" in real time

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, version, epoch):
        (self.repo / "VERSION").write_text(version + "\n")
        sh(self.repo, "add", "VERSION")
        env = {**ENV, "GIT_AUTHOR_DATE": f"@{epoch} +0000", "GIT_COMMITTER_DATE": f"@{epoch} +0000"}
        subprocess.run(["git", "commit", "-q", "-m", version], cwd=self.repo, env=env, check=True)
        return sh(self.repo, "rev-parse", "HEAD")

    def verdict(self, live, now=None, grace=24.0):
        return cd.drift(self.repo, live, "main", "VERSION", grace, now if now is not None else time.time())

    def test_in_sync_full_and_short_sha(self):
        self.assertEqual(self.verdict({"commit": self.c2})["verdict"], "IN_SYNC")
        self.assertEqual(self.verdict({"commit": self.c2[:7]})["verdict"], "IN_SYNC")
        self.assertEqual(self.verdict({"commit": self.c2[:7].upper()})["verdict"], "IN_SYNC")

    def test_behind_within_and_after_grace(self):
        r = self.verdict({"commit": self.c1}, now=T0 - 47 * 3600)  # undeployed commit is 1h old
        self.assertEqual((r["verdict"], r["severity"], r["behind"]), ("PENDING_DEPLOY", "info", 1))
        r = self.verdict({"commit": self.c1}, now=T0)  # 48h old
        self.assertEqual((r["verdict"], r["severity"]), ("BEHIND", "warn"))

    def test_live_runs_unpushed_commit_is_crit(self):
        """The events-app incident: a live feature built from commits only on the laptop."""
        c3 = self.commit("1.1.0", T0 - 3600)
        r = self.verdict({"commit": c3[:7]}, now=T0)
        self.assertEqual((r["verdict"], r["severity"], r["not_on_origin"]), ("LIVE_NOT_ON_ORIGIN", "crit", 1))
        self.assertEqual(r["unpushed"]["count"], 1)
        self.assertAlmostEqual(r["unpushed"]["oldest_h"], 1.0)

    def test_unknown_commit_fresh_fetch_is_crit_stale_is_warn(self):
        ghost = "deadbee"
        r = self.verdict({"commit": ghost})
        self.assertEqual((r["verdict"], r["severity"]), ("LIVE_UNKNOWN", "crit"))
        fh = Path(sh(self.repo, "rev-parse", "--absolute-git-dir")) / "FETCH_HEAD"
        old = time.time() - 7 * 3600
        os.utime(fh, (old, old))
        self.assertEqual(self.verdict({"commit": ghost})["severity"], "warn")
        fh.unlink()
        self.assertEqual(self.verdict({"commit": ghost})["severity"], "warn")

    def test_no_commit_falls_back_to_version(self):
        self.assertEqual(self.verdict({"commit": None, "version": "1.0.1"})["verdict"], "NO_COMMIT_VERSION_MATCH")
        self.assertEqual(self.verdict({"version": "1.0.0"})["verdict"], "NO_COMMIT_VERSION_DIFF")
        self.assertEqual(self.verdict({})["verdict"], "NO_COMMIT")

    def test_hostile_commit_values_never_reach_git_as_options(self):
        for bad in ["--output=/tmp/x", "HEAD", "main", "abc", "g" * 40, 123, ["a"], "a" * 41]:
            r = self.verdict({"commit": bad})
            self.assertIn(r["verdict"], ("LIVE_UNKNOWN", "NO_COMMIT", "NO_COMMIT_VERSION_DIFF"), bad)
            self.assertNotEqual(r["verdict"], "IN_SYNC", bad)

    def test_cli_exit_codes(self):
        f = Path(self.tmp.name) / "live.json"
        f.write_text(json.dumps({"commit": self.c2[:7], "version": "1.0.1"}))
        self.assertEqual(cd.main(["--repo", str(self.repo), "--live-json", str(f)]), 0)
        f.write_text(json.dumps({"commit": "deadbee"}))
        self.assertEqual(cd.main(["--repo", str(self.repo), "--live-json", str(f)]), 2)
        self.assertEqual(cd.main(["--repo", str(self.repo), "--live-json", str(f), "--branch", "nope"]), 3)


if __name__ == "__main__":
    unittest.main()

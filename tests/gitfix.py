"""Throwaway git repos for tests: a bare origin, a working clone, and Fleet Truth's mirror."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from fleet_truth import gitmirror as gm

T0 = 1_790_000_000
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def sh(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, env=env or ENV, check=True, capture_output=True, text=True).stdout.strip()


class Repos:
    def __init__(self, root: Path, branch="main"):
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.branch = branch
        self.origin = root / "origin.git"
        self.repo = root / "clone"
        sh(root, "init", "-q", "--bare", "-b", branch, str(self.origin))
        sh(root, "clone", "-q", str(self.origin), str(self.repo))
        sh(self.repo, "checkout", "-q", "-b", branch)
        self.data = root / "data"
        self.spec = gm.MirrorSpec("app", self.data / "mirrors" / "app.git", branch,
                                  origin_url=f"file://{self.origin}", clone_path=self.repo)
        self.mirror = self.spec.mirror_dir

    def commit(self, version, epoch, msg=None, path="VERSION"):
        (self.repo / path).write_text(version + "\n")
        sh(self.repo, "add", path)
        env = {**ENV, "GIT_AUTHOR_DATE": f"@{epoch} +0000", "GIT_COMMITTER_DATE": f"@{epoch} +0000"}
        subprocess.run(["git", "commit", "-q", "-m", msg or version], cwd=self.repo, env=env, check=True)
        return sh(self.repo, "rev-parse", "HEAD")

    def push(self):
        sh(self.repo, "push", "-q", "origin", self.branch)

    def refresh(self, now=None, force=True, token=None, askpass=None, minutes=15):
        import time
        return gm.refresh(self.spec, time.time() if now is None else now, minutes, token, askpass, force)

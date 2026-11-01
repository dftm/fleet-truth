#!/usr/bin/env python3
"""check_drift.py -- spike for Fleet Truth's riskiest assumption.

ASSUMPTION UNDER TEST: given what a live app reports about itself (its /api/version JSON)
and a local clone of its repo, we can compute an honest drift verdict -- including when the
app reports only a 7-char short SHA, or no commit at all (true today for every app in the
fleet, because no image workflow passes GIT_SHA yet).

Stdlib only. Never fetches, never writes to the repo. The live JSON is UNTRUSTED: the
commit value reaches git only after a strict hex regex, so it can never become an option.

USAGE
    check_drift.py --repo PATH --live-json FILE [--branch main] [--version-file VERSION]
                   [--grace-hours 24] [--now EPOCH]
Prints one JSON object; exit 0 ok/info, 1 warn, 2 crit, 3 usage/repo error.

VERDICTS (drift)
    IN_SYNC              live commit == origin/<branch>                       ok
    PENDING_DEPLOY       live is an ancestor of origin, oldest undeployed
                         commit younger than grace                             info
    BEHIND               same, but older than grace                            warn
    LIVE_NOT_ON_ORIGIN   live commit exists locally but is not reachable
                         from origin/<branch> (deployed unpushed work)         crit
    LIVE_UNKNOWN         live commit not in the clone at all                   warn if the clone's
                                                                               fetch is stale, else crit
    NO_COMMIT_VERSION_MATCH   no commit reported; VERSION at origin == live   warn (weak evidence)
    NO_COMMIT_VERSION_DIFF    no commit reported; VERSION differs              warn
    NO_COMMIT                 no commit and no version to compare              warn
VERDICTS (local)
    unpushed: count of commits on local <branch> not on origin/<branch>, age of the oldest.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HEX = re.compile(r"^[0-9a-f]{7,40}$")
SEVERITY_EXIT = {"ok": 0, "info": 0, "warn": 1, "crit": 2}
FETCH_STALE_HOURS = 6.0


class RepoError(Exception):
    pass


def git(repo: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
    if check and r.returncode != 0:
        raise RepoError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip() if r.returncode == 0 else ""


def resolve(repo: Path, rev: str) -> str | None:
    """Full SHA for rev, or None if unknown or ambiguous. rev must already be validated."""
    out = git(repo, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False)
    return out or None


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b], capture_output=True)
    return r.returncode == 0


def commits_between(repo: Path, base: str, tip: str) -> list[tuple[str, int]]:
    """(sha, committer epoch) for commits in tip not in base, oldest first."""
    out = git(repo, "log", "--reverse", "--format=%H %ct", f"{base}..{tip}", check=False)
    return [(s, int(t)) for s, t in (l.split() for l in out.splitlines() if l)]


def fetch_age_hours(repo: Path, now: float) -> float | None:
    gitdir = Path(git(repo, "rev-parse", "--absolute-git-dir"))
    fh = gitdir / "FETCH_HEAD"
    return (now - fh.stat().st_mtime) / 3600 if fh.exists() else None


def normalize_commit(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    c = raw.strip().lower()
    return c if HEX.match(c) else None


def drift(repo: Path, live: dict, branch: str = "main", version_file: str | None = "VERSION",
          grace_hours: float = 24.0, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    origin_ref = f"origin/{branch}"
    origin = resolve(repo, origin_ref)
    if not origin:
        raise RepoError(f"{origin_ref} not found in {repo}")
    res: dict = {"origin": origin, "live_raw": live.get("commit"), "branch": branch}
    fa = fetch_age_hours(repo, now)
    res["fetch_age_h"] = None if fa is None else round(fa, 1)

    commit = normalize_commit(live.get("commit"))
    if live.get("commit") not in (None, "") and commit is None:
        res.update(verdict="LIVE_UNKNOWN", severity="crit",
                   detail="live commit field is not a 7-40 char hex SHA; refusing to interpret it")
    elif commit is None:
        res.update(_no_commit(repo, live, origin_ref, version_file))
    else:
        full = resolve(repo, commit)
        res["live"] = full
        if full is None:
            stale = fa is None or fa > FETCH_STALE_HOURS
            res.update(verdict="LIVE_UNKNOWN", severity="warn" if stale else "crit",
                       detail=(f"live commit {commit} is not in the local clone (unknown or ambiguous prefix); "
                               + ("clone fetch is stale or never ran -- fetch first" if stale
                                  else "clone was fetched recently, so this build came from code git has never seen")))
        elif full == origin:
            res.update(verdict="IN_SYNC", severity="ok", detail="live == origin")
        elif is_ancestor(repo, full, origin):
            pending = commits_between(repo, full, origin)
            oldest_h = (now - pending[0][1]) / 3600 if pending else 0.0
            late = oldest_h > grace_hours
            res.update(verdict="BEHIND" if late else "PENDING_DEPLOY", severity="warn" if late else "info",
                       behind=len(pending), oldest_undeployed_h=round(oldest_h, 1),
                       detail=f"live is {len(pending)} commit(s) behind origin; oldest undeployed {oldest_h:.1f}h")
        else:
            extra = commits_between(repo, origin, full)
            res.update(verdict="LIVE_NOT_ON_ORIGIN", severity="crit", not_on_origin=len(extra),
                       detail=f"live runs {len(extra)} commit(s) that origin/{branch} does not have: deployed unpushed work")
    res["unpushed"] = _unpushed(repo, branch, origin_ref, now)
    return res


def _no_commit(repo: Path, live: dict, origin_ref: str, version_file: str | None) -> dict:
    lv = live.get("version")
    if not version_file or not isinstance(lv, str) or not lv.strip():
        return {"verdict": "NO_COMMIT", "severity": "warn",
                "detail": "app reports no commit and no version; pass GIT_SHA at image build"}
    ov = git(repo, "show", f"{origin_ref}:{version_file}", check=False).splitlines()
    ov = ov[0].strip() if ov else ""
    if ov and ov == lv.strip():
        return {"verdict": "NO_COMMIT_VERSION_MATCH", "severity": "warn", "origin_version": ov,
                "detail": f"no commit reported; VERSION {ov} matches origin (weak: a bump is not every commit)"}
    return {"verdict": "NO_COMMIT_VERSION_DIFF", "severity": "warn", "origin_version": ov or None,
            "detail": f"no commit reported; live version {lv.strip()} vs origin {ov or '(no ' + version_file + ')'}"}


def _unpushed(repo: Path, branch: str, origin_ref: str, now: float) -> dict:
    if not resolve(repo, f"refs/heads/{branch}"):
        return {"count": None, "detail": f"no local branch {branch}"}
    ahead = commits_between(repo, origin_ref, f"refs/heads/{branch}")
    if not ahead:
        return {"count": 0}
    return {"count": len(ahead), "oldest_h": round((now - ahead[0][1]) / 3600, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--live-json", required=True, type=Path, help="fixture file with the /api/version body")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--version-file", default="VERSION")
    ap.add_argument("--grace-hours", type=float, default=24.0)
    ap.add_argument("--now", type=float, default=None)
    a = ap.parse_args(argv)
    try:
        live = json.loads(a.live_json.read_text())
        if not isinstance(live, dict):
            raise ValueError("live JSON is not an object")
        res = drift(a.repo, live, a.branch, a.version_file, a.grace_hours, a.now)
    except (RepoError, OSError, ValueError) as e:
        print(json.dumps({"verdict": "ERROR", "severity": "warn", "detail": str(e)}))
        return 3
    print(json.dumps(res, indent=1, sort_keys=True))
    return SEVERITY_EXIT[res["severity"]]


if __name__ == "__main__":
    sys.exit(main())

"""DRIFT and UNPUSHED (design 4.3, 4.4), computed inside the read-only mirror.

DRIFT is the spike's verdict table (spike/check_drift.py, kept unchanged) with `origin/<branch>`
replaced by refs/remotes/origin/<branch>, the local branch by refs/local/<branch>, and the clone's
FETCH_HEAD age by the mirror's `last_fetch_ok` epoch.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .. import gitmirror as gm
from .base import CheckResult

HEX = re.compile(r"^[0-9a-f]{7,40}$")
FETCH_STALE_HOURS = 6.0


class RepoError(Exception):
    pass


def origin_ref(branch: str) -> str:
    return f"refs/remotes/origin/{branch}"


def local_ref(branch: str) -> str:
    return f"refs/local/{branch}"


def normalize_commit(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    c = raw.strip().lower()
    return c if HEX.match(c) else None


def compute_drift(mirror: Path, live: dict, branch: str = "main", version_file: str | None = "VERSION",
                  grace_hours: float = 24.0, now: float | None = None, fetched_at: float | None = None) -> dict:
    """Same shape as the spike's drift(): verdict, severity, detail and verdict-specific fields."""
    now = time.time() if now is None else now
    o_ref = origin_ref(branch)
    origin = gm.resolve(mirror, o_ref)
    if not origin:
        raise RepoError(f"{o_ref} not found in the mirror")
    res: dict = {"origin": origin, "live_raw": live.get("commit"), "branch": branch}
    fa = None if fetched_at is None else (now - fetched_at) / 3600
    res["fetch_age_h"] = None if fa is None else round(fa, 1)

    commit = normalize_commit(live.get("commit"))
    if live.get("commit") not in (None, "") and commit is None:
        res.update(verdict="LIVE_UNKNOWN", severity="crit",
                   detail="live commit field is not a 7-40 char hex SHA; refusing to interpret it")
    elif commit is None:
        res.update(_no_commit(mirror, live, o_ref, version_file))
    else:
        full = gm.resolve(mirror, commit)
        res["live"] = full
        if full is None:
            stale = fa is None or fa > FETCH_STALE_HOURS
            res.update(verdict="LIVE_UNKNOWN", severity="warn" if stale else "crit",
                       detail=(f"live commit {commit} is not in the mirror (unknown or ambiguous prefix); "
                               + ("mirror fetch is stale or never ran -- fetch first" if stale
                                  else "mirror was fetched recently, so this build came from code git has never seen")))
        elif full == origin:
            res.update(verdict="IN_SYNC", severity="ok", detail="live == origin")
        elif gm.is_ancestor(mirror, full, origin):
            pending = gm.commits_between(mirror, full, origin)
            oldest_h = (now - pending[0][1]) / 3600 if pending else 0.0
            late = oldest_h > grace_hours
            res.update(verdict="BEHIND" if late else "PENDING_DEPLOY", severity="warn" if late else "info",
                       behind=len(pending), oldest_undeployed_h=round(oldest_h, 1),
                       detail=f"live is {len(pending)} commit(s) behind origin; oldest undeployed {oldest_h:.1f}h")
        else:
            extra = gm.commits_between(mirror, o_ref, full)
            res.update(verdict="LIVE_NOT_ON_ORIGIN", severity="crit", not_on_origin=len(extra),
                       detail=f"live runs {len(extra)} commit(s) that origin/{branch} does not have: deployed unpushed work")
    res["unpushed"] = _unpushed_summary(mirror, branch, o_ref, now)
    return res


def _no_commit(mirror: Path, live: dict, o_ref: str, version_file: str | None) -> dict:
    lv = live.get("version")
    if not version_file or not isinstance(lv, str) or not lv.strip():
        return {"verdict": "NO_COMMIT", "severity": "warn",
                "detail": "app reports no commit and no version; pass GIT_SHA at image build"}
    ov = gm.git_read(mirror, "cat-file", "-p", f"{o_ref}:{version_file}").splitlines()
    ov = ov[0].strip() if ov else ""
    if ov and ov == lv.strip():
        return {"verdict": "NO_COMMIT_VERSION_MATCH", "severity": "warn", "origin_version": ov,
                "detail": f"no commit reported; VERSION {ov} matches origin (weak: a bump is not every commit)"}
    return {"verdict": "NO_COMMIT_VERSION_DIFF", "severity": "warn", "origin_version": ov or None,
            "detail": f"no commit reported; live version {lv.strip()} vs origin {ov or '(no ' + version_file + ')'}"}


def _unpushed_summary(mirror: Path, branch: str, o_ref: str, now: float) -> dict:
    l_ref = local_ref(branch)
    if not gm.resolve(mirror, l_ref):
        return {"count": None, "detail": f"no local branch {branch}"}
    ahead = gm.commits_between(mirror, o_ref, l_ref)
    if not ahead:
        return {"count": 0}
    return {"count": len(ahead), "oldest_h": round((now - ahead[0][1]) / 3600, 1)}


def drift_check(mirror: Path, live: dict, branch: str, version_file: str | None, grace_hours: float,
                now: float | None = None) -> CheckResult:
    """CheckResult for the cycle. `live` is the DEEP_PROBE data (untrusted strings)."""
    now = time.time() if now is None else now
    if not (mirror / "HEAD").exists():
        return CheckResult("unknown", "NO_MIRROR", "no mirror yet (first fetch has not succeeded)")
    try:
        r = compute_drift(mirror, live, branch, version_file, grace_hours, now, gm.last_fetch_ok(mirror))
    except RepoError as e:
        return CheckResult("unknown", "NO_MIRROR", f"{e}: the first fetch has not succeeded")
    except gm.GitError as e:
        return CheckResult("unknown", "GIT_ERROR", str(e))
    data = {k: v for k, v in r.items() if k not in ("detail", "severity", "verdict", "unpushed", "live_raw")}
    data["live_raw"] = str(r.get("live_raw"))[:80] if r.get("live_raw") is not None else None
    return CheckResult(r["severity"], r["verdict"], r["detail"], data)


def unpushed_check(mirror: Path, clone: Path | None, branch: str, max_age_hours: float, dirty_hours: float,
                   now: float | None = None) -> CheckResult:
    now = time.time() if now is None else now
    if clone is None:
        return CheckResult("unknown", "NOT_CONFIGURED", "no local clone configured")
    if not (mirror / "HEAD").exists() or not gm.resolve(mirror, origin_ref(branch)):
        return CheckResult("unknown", "NO_MIRROR", f"mirror has no origin/{branch} yet")
    l_ref = local_ref(branch)
    if not gm.resolve(mirror, l_ref):
        return CheckResult("unknown", "NO_LOCAL_BRANCH",
                           f"working clone has no local branch '{branch}' (or the fetch from it failed)")
    try:
        ahead = gm.commits_between(mirror, origin_ref(branch), l_ref)
        subjects = gm.subjects_between(mirror, origin_ref(branch), l_ref) if ahead else []
        dirty = gm.dirty_files(clone)
    except gm.GitError as e:
        return CheckResult("unknown", "GIT_ERROR", str(e))
    data: dict = {"count": len(ahead), "subjects": subjects}
    status, verdict, detail = "ok", "OK", "nothing unpushed"
    if ahead:
        oldest_h = (now - ahead[0][1]) / 3600
        data["oldest_h"] = round(oldest_h, 1)
        status = "warn" if oldest_h > max_age_hours else "info"
        verdict = "UNPUSHED_COMMITS"
        detail = f"{len(ahead)} commit(s) exist only on this machine; oldest {oldest_h:.1f}h"
    if dirty:
        mt = 0.0
        for rel in dirty:
            try:
                mt = max(mt, os.stat(clone / rel).st_mtime)
            except OSError:
                pass
        age_h = (now - mt) / 3600 if mt else None
        data["dirty_files"] = len(dirty)
        if age_h is not None:
            data["dirty_age_h"] = round(age_h, 1)
        if age_h is not None and age_h > dirty_hours:
            if status == "ok":
                status, verdict = "info", "DIRTY_FILES"
                detail = f"{len(dirty)} tracked file(s) changed and uncommitted for {age_h:.1f}h"
            else:
                detail += f"; also {len(dirty)} uncommitted tracked file(s)"
    return CheckResult(status, verdict, detail, data)

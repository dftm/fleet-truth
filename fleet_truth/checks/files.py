"""BACKUP_AGE, HEARTBEAT, STALE_BUILD (design 4.5, 4.7, 4.8). Local files only; nothing is written."""
from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import time
from pathlib import Path

from ..config import BackupCfg, HeartbeatCfg
from .base import CheckResult

FUTURE_SLACK_S = 300
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ----------------------------------------------------------------------------- BACKUP_AGE
def _read_status(path: Path) -> dict | None:
    """Parsed status.json, {} when absent, None when present but unreadable."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def lock_is_held(state_dir: Path) -> bool:
    """True when another process holds the backup lock. Never creates the file: an absent lock is 'not held'."""
    p = state_dir / "lock"
    try:
        fd = os.open(p, os.O_RDONLY)
    except OSError:
        return False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def backup_age(cfg: BackupCfg, now: float | None = None) -> CheckResult:
    """Mirrors backup_lib._check_status verdict for verdict, then adds the running-backup guard."""
    now = time.time() if now is None else now
    path = Path(cfg.status_file)
    st = _read_status(path)
    not_ok_never = "crit" if cfg.required else "warn"
    if st is None:
        return CheckResult("crit", "UNREADABLE", "status file exists but cannot be read or parsed")
    verdict, status, detail, data = "OK", "ok", "", {}
    if not st:
        verdict, status, detail = "NEVER_RAN", not_ok_never, "no status file: backup has never run"
    elif st.get("app") != cfg.app_name:
        verdict, status, detail = "WRONG_APP", "crit", "status file belongs to a different app (shared state dir?)"
    elif st.get("result") != "ok":
        verdict, status = "FAILED", "crit"
        detail = (f"last run FAILED at stage {_s(st.get('stage'))} ({_s(st.get('timestamp'))}); "
                  f"last ok {_s(st.get('last_ok'))}")
        data = {"stage": _s(st.get("stage")), "timestamp": _s(st.get("timestamp")), "last_ok": _s(st.get("last_ok"))}
    else:
        last = st.get("last_ok_epoch")
        age_h = (now - last) / 3600 if isinstance(last, (int, float)) and not isinstance(last, bool) else float("inf")
        data = {"age_h": None if age_h == float("inf") else round(age_h, 1), "last_ok": _s(st.get("last_ok"))}
        if age_h > cfg.max_age_hours:
            verdict, status = "STALE", "crit"
            detail = (f"last success is {age_h:.1f}h old (limit {cfg.max_age_hours:g}h)" if age_h != float("inf")
                      else "status has no usable last_ok_epoch")
        else:
            detail = f"ok, last success {age_h:.1f}h ago"
    if verdict in ("NEVER_RAN", "FAILED", "STALE") and lock_is_held(path.parent):
        return CheckResult("info", "RUNNING", f"a backup run is in progress (would otherwise read: {verdict})", data)
    return CheckResult(status, verdict, detail, data)


def _s(v) -> str | None:
    return None if v is None else str(v)[:80]


# ----------------------------------------------------------------------------- HEARTBEAT
def _dig(doc, dotted: str):
    cur = doc
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            raise KeyError(part)
    return cur


def parse_timestamp(v) -> float:
    """Epoch seconds (number) or ISO-8601 with offset (string) -> epoch. Raises ValueError otherwise."""
    if isinstance(v, bool):
        raise ValueError("boolean")
    if isinstance(v, (int, float)):
        if not (0 < v < 1e11):
            raise ValueError("number is not epoch seconds")
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if DATE_ONLY.match(s):
            raise ValueError("date without time")
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            raise ValueError("no UTC offset")
        return dt.timestamp()
    raise ValueError("unsupported type")


def heartbeat(cfg: HeartbeatCfg, now: float | None = None) -> CheckResult:
    now = time.time() if now is None else now
    try:
        doc = json.loads(Path(cfg.file).read_text())
    except FileNotFoundError:
        return CheckResult("unknown", "MISSING", "heartbeat file does not exist")
    except (OSError, ValueError):
        return CheckResult("unknown", "UNPARSEABLE", "heartbeat file cannot be read as JSON")
    try:
        raw = _dig(doc, cfg.field)
    except KeyError:
        return CheckResult("unknown", "MISSING", f"field '{cfg.field}' not found in the heartbeat file")
    try:
        ts = parse_timestamp(raw)
    except ValueError as e:
        return CheckResult("unknown", "UNPARSEABLE", f"field '{cfg.field}' is not a usable timestamp ({e})")
    age_min = (now - ts) / 60
    if age_min < -FUTURE_SLACK_S / 60:
        return CheckResult("unknown", "FUTURE_TIMESTAMP", f"timestamp is {-age_min:.0f} min in the future (clock skew?)")
    data = {"age_min": round(age_min, 1)}
    if age_min > cfg.max_age_minutes:
        return CheckResult("crit", "STALE", f"last heartbeat {age_min:.0f} min ago (limit {cfg.max_age_minutes:g} min)", data)
    return CheckResult("ok", "OK", f"last heartbeat {max(age_min, 0):.0f} min ago", data)


# ----------------------------------------------------------------------------- STALE_BUILD
def stale_build(built_at, max_age_days: float = 45.0, now: float | None = None) -> CheckResult:
    now = time.time() if now is None else now
    if built_at is None or built_at == "":
        return CheckResult("unknown", "NO_BUILT_AT", "the version endpoint reports no builtAt")
    if not isinstance(built_at, str):
        return CheckResult("unknown", "NO_BUILT_AT", "builtAt is not a string")
    s = built_at.strip()
    if DATE_ONLY.match(s):
        return CheckResult("unknown", "BUILD_TIME_NOT_SET", "BUILD_TIME not set: builtAt is only a file-mtime date")
    try:
        ts = parse_timestamp(s)
    except ValueError:
        return CheckResult("unknown", "BUILD_TIME_NOT_SET", "builtAt is not a full ISO-8601 timestamp with offset")
    age_d = (now - ts) / 86400
    if age_d > max_age_days:
        return CheckResult("info", "OLD_BUILD", f"image built {age_d:.0f} days ago (limit {max_age_days:g}); base-image updates are aging",
                           {"age_days": round(age_d, 1)})
    return CheckResult("ok", "OK", f"image built {max(age_d, 0):.0f} days ago", {"age_days": round(age_d, 1)})

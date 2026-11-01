"""Single source of truth for the version (fleet_truth/VERSION, one line of semver).

info() -> {version, build, builtAt, commit}
  version  the VERSION file.
  build    8-hex fingerprint of VERSION plus every .py file in this package (names and bytes).
  builtAt  env BUILD_TIME when the image build set it, else the ISO date (UTC) of the newest
           package file mtime. APPROXIMATE: a COPY into an image resets mtimes.
  commit   env GIT_SHA (full value, not shortened), else None.
Reads nothing outside this package.
"""
from __future__ import annotations

import datetime
import hashlib
import os
import pathlib

HERE = pathlib.Path(__file__).parent
VERSION_FILE = HERE / "VERSION"
_SUFFIXES = {".py"}


def read_version(path: pathlib.Path | None = None) -> str:
    try:
        v = (path or VERSION_FILE).read_text().strip().splitlines()[0].strip()
        return v or "0.0.0"
    except (OSError, IndexError):
        return "0.0.0"


def _files(root: pathlib.Path | None = None):
    root = root or HERE
    for f in sorted(root.rglob("*")):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        if f.suffix in _SUFFIXES or f.name == "VERSION":
            yield f


def fingerprint(root: pathlib.Path | None = None) -> str:
    root = root or HERE
    h = hashlib.sha256()
    for f in _files(root):
        h.update(f.relative_to(root).as_posix().encode() + b"\0")
        try:
            h.update(f.read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:8]


def built_at(root: pathlib.Path | None = None) -> str | None:
    bt = (os.environ.get("BUILD_TIME") or "").strip()
    if bt:
        return bt
    newest = 0.0
    for f in _files(root):
        try:
            newest = max(newest, f.stat().st_mtime)
        except OSError:
            pass
    if not newest:
        return None
    return datetime.datetime.fromtimestamp(newest, datetime.timezone.utc).date().isoformat()


def commit() -> str | None:
    sha = (os.environ.get("GIT_SHA") or "").strip()
    return sha or None


BUILD = fingerprint()


def info() -> dict:
    return {"version": read_version(), "build": BUILD, "builtAt": built_at(), "commit": commit()}

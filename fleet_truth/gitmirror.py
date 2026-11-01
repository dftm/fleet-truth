"""Read-only git mirror per app (design 6.3).

`<data_dir>/mirrors/<app>.git` is a bare repo Fleet Truth owns. Each refresh (rate-limited by
git.fetch_minutes):
  [1]  fetch origin's branch into refs/remotes/origin/<branch> (GitHub over https; read-only token
       through a GIT_ASKPASS helper, never written to config or arguments);
  [2]  fetch the owner's working clone's branch into refs/local/<branch> (reads unpushed commits).
Ancestry questions are then answered inside the mirror. The working clone is only ever the SOURCE
of a fetch (upload-pack, read-only) or the subject of `status --no-optional-locks`; nothing is
written there, no lock is taken, no hook or fsmonitor program runs.

Every git invocation: argument list (no shell), isolated config (no global/system/credential
helper), hooks and fsmonitor disabled, terminal prompts off.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from . import secrets

HEX_REF = re.compile(r"^[0-9a-f]{7,40}$")
LAST_FETCH_FILE = "fleet_last_fetch_ok"
GIT_TIMEOUT_S = 120
ASKPASS_TOKEN_VAR = "FLEET_GIT_TOKEN"  # internal; only ever set in a git child's environment

ASKPASS_SCRIPT = f"""#!/bin/sh
# Prints credentials for git from the environment. Contains no secret itself.
case "$1" in
  Username*) printf '%s\\n' x-access-token ;;
  *) printf '%s\\n' "${ASKPASS_TOKEN_VAR}" ;;
esac
"""

HARDEN = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "credential.helper=",
          "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "core.askPass="]
PROTOCOLS = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.file.allow=always"]


class GitError(Exception):
    pass


@dataclass(frozen=True)
class MirrorSpec:
    name: str
    mirror_dir: Path
    branch: str
    origin_url: str | None = None      # https://github.com/<owner>/<repo>.git (file:// in tests)
    clone_path: Path | None = None     # the owner's working clone (read-only source)


@dataclass(frozen=True)
class RefreshResult:
    skipped: bool
    origin_ok: bool | None   # None when not attempted
    local_ok: bool | None
    error: str = ""


# argv of every git call, for tests asserting that no secret appears in process arguments
ARGV_LOG: list[list[str]] | None = None


def spec_for(app, data_dir: str | Path) -> MirrorSpec:
    root = Path(data_dir).expanduser() / "mirrors"
    return MirrorSpec(
        name=app.name, mirror_dir=root / f"{app.name}.git", branch=app.branch,
        origin_url=f"https://github.com/{app.github}.git" if app.github else None,
        clone_path=Path(app.repo) if app.repo else None)


def _env(token: str | None = None, askpass: Path | None = None, home: Path | None = None) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "LANG": "C",
        "HOME": str(home or "/nonexistent"),
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_LAZY_FETCH": "1",
    }
    if token and askpass:
        env["GIT_ASKPASS"] = str(askpass)
        env[ASKPASS_TOKEN_VAR] = token
    return env


def run_git(args: list[str], *, cwd: Path | None = None, env: dict | None = None, check: bool = True,
            timeout: float = GIT_TIMEOUT_S) -> str:
    argv = ["git", *args]
    if ARGV_LOG is not None:
        ARGV_LOG.append(list(argv))
    try:
        r = subprocess.run(argv, cwd=cwd, env=env if env is not None else _env(), capture_output=True,
                           text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0] if args else ''} timed out after {timeout:.0f}s") from None
    if r.returncode != 0:
        if check:
            raise GitError(_safe_err(r.stderr, args))
        return ""
    return r.stdout


def _safe_err(stderr: str, args: list[str]) -> str:
    msg = secrets.scrub(stderr.strip().splitlines()[-1] if stderr.strip() else "no error output")
    msg = re.sub(r"(https?://)[^/@\s]+@", r"\1***@", msg)  # never echo URL userinfo
    verb = next((a for a in args if a in ("fetch", "status", "init", "rev-parse", "log", "show", "cat-file",
                                           "merge-base", "update-ref", "config")), "git")
    return f"git {verb} failed: {msg[:300]}"


def write_askpass(data_dir: str | Path) -> Path:
    """Create (or refresh) the tiny GIT_ASKPASS helper inside the data dir. Holds no secret."""
    d = Path(data_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    p = d / "git-askpass.sh"
    if not p.exists() or p.read_text() != ASKPASS_SCRIPT:
        tmp = d / "git-askpass.sh.tmp"
        tmp.write_text(ASKPASS_SCRIPT)
        os.chmod(tmp, 0o700)
        os.replace(tmp, p)
    return p


def ensure_mirror(spec: MirrorSpec) -> Path:
    m = spec.mirror_dir
    env = _env(home=m.parent)
    if not (m / "HEAD").exists():
        m.parent.mkdir(parents=True, exist_ok=True)
        run_git(["init", "--bare", "-q", str(m)], env=env)
    # idempotent hardening on every call: hooks off, fsmonitor off, origin url without credentials
    run_git(["--git-dir", str(m), "config", "core.hooksPath", os.devnull], env=env)
    run_git(["--git-dir", str(m), "config", "core.fsmonitor", "false"], env=env)
    if spec.origin_url:
        if "@" in spec.origin_url.split("://", 1)[-1].split("/", 1)[0]:
            raise GitError("origin URL must not carry credentials")
        run_git(["--git-dir", str(m), "config", "remote.origin.url", spec.origin_url], env=env)
    return m


def last_fetch_ok(mirror: Path) -> float | None:
    try:
        return float((mirror / LAST_FETCH_FILE).read_text().strip())
    except (OSError, ValueError):
        return None


def refresh(spec: MirrorSpec, now: float, fetch_minutes: float = 15, token: str | None = None,
            askpass: Path | None = None, force: bool = False) -> RefreshResult:
    """Bring the mirror up to date unless fetched within fetch_minutes. Never raises."""
    try:
        m = ensure_mirror(spec)
    except (GitError, OSError) as e:
        return RefreshResult(False, None, None, secrets.scrub(str(e))[:300])
    last = last_fetch_ok(m)
    if not force and last is not None and now - last < fetch_minutes * 60:
        return RefreshResult(True, None, None)
    env = _env(token, askpass, home=m.parent)
    errors: list[str] = []
    origin_ok: bool | None = None
    local_ok: bool | None = None
    b = spec.branch
    if spec.origin_url:
        try:
            run_git([*HARDEN, *PROTOCOLS, "--git-dir", str(m), "fetch", "--prune", "--no-tags",
                     "--no-recurse-submodules", "origin", f"+refs/heads/{b}:refs/remotes/origin/{b}"], env=env)
            origin_ok = True
            tmp = m / (LAST_FETCH_FILE + ".tmp")
            tmp.write_text(f"{now:.0f}\n")
            os.replace(tmp, m / LAST_FETCH_FILE)
        except GitError as e:
            origin_ok = False
            errors.append(str(e))
    if spec.clone_path:
        try:
            run_git([*HARDEN, *PROTOCOLS, "--git-dir", str(m), "fetch", "--no-tags", "--no-recurse-submodules",
                     "--", str(spec.clone_path), f"+refs/heads/{b}:refs/local/{b}"], env=env)
            local_ok = True
        except GitError as e:
            local_ok = False
            errors.append(str(e))
            # a ref from an earlier run no longer reflects the clone: remove it rather than trust it
            run_git(["--git-dir", str(m), "update-ref", "-d", f"refs/local/{b}"], env=env, check=False)
    return RefreshResult(False, origin_ok, local_ok, "; ".join(errors)[:600])


# ----------------------------------------------------------------------------- read helpers
def git_read(mirror: Path, *args: str, check: bool = False) -> str:
    return run_git(["--git-dir", str(mirror), *args], env=_env(home=mirror.parent), check=check).strip()


def resolve(mirror: Path, rev: str) -> str | None:
    """Full SHA for rev, or None if unknown or ambiguous. rev must already be validated."""
    out = git_read(mirror, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    return out or None


def is_ancestor(mirror: Path, a: str, b: str) -> bool:
    r = subprocess.run(["git", "--git-dir", str(mirror), "merge-base", "--is-ancestor", a, b],
                       env=_env(home=mirror.parent), capture_output=True, stdin=subprocess.DEVNULL)
    return r.returncode == 0


def commits_between(mirror: Path, base: str, tip: str) -> list[tuple[str, int]]:
    """(sha, committer epoch) for commits in tip not in base, oldest first."""
    out = git_read(mirror, "log", "--reverse", "--format=%H %ct", f"{base}..{tip}")
    return [(s, int(t)) for s, t in (ln.split() for ln in out.splitlines() if ln)]


def subjects_between(mirror: Path, base: str, tip: str, limit: int = 10, width: int = 120) -> list[str]:
    out = git_read(mirror, "log", f"--max-count={limit}", "--format=%s", f"{base}..{tip}")
    clean = []
    for ln in out.splitlines():
        s = "".join(ch for ch in ln if ch.isprintable())[:width]
        clean.append(secrets.scrub(s))
    return clean


def dirty_files(clone: Path) -> list[str]:
    """Tracked files with uncommitted changes, via a read-only status (no index lock, no hooks)."""
    out = run_git([*HARDEN, "-C", str(clone), "--no-optional-locks", "status", "--porcelain=v1", "-z", "-uno",
                   "--ignore-submodules=all"], env=_env(home=clone.parent), check=False)
    files: list[str] = []
    parts = out.split("\0")
    i = 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if len(e) < 4:
            continue
        xy, path = e[:2], e[3:]
        files.append(path)
        if "R" in xy or "C" in xy:  # rename/copy entries carry the original path as a second field
            i += 1
    return files

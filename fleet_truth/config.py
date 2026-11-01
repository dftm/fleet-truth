"""fleet.yaml loader and validation (design section 7).

Rules: unknown keys are errors with the key path; names unique and `^[a-z0-9][a-z0-9-]{0,40}$`;
URLs https:// (or http:// only with allow_private: true), no userinfo; every `*_env` value is an env
var NAME; a key that looks like a literal secret is an error. Secret VALUES are never read here:
see secrets.get(). Frozen dataclasses throughout. There is deliberately no key through which a
file can switch off the SSRF checks or allow loopback (that is a test-only parameter in net.py).
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from . import secrets

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
GITHUB_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,100}$")
# keys that look like they hold a literal secret
SECRET_KEY_RE = re.compile(r"(^|_)(token|secret|password|passwd|apikey|api_key|passphrase)(_|$)", re.I)
MIN_STATUSES = ("info", "warn", "crit")
ALERT_TYPES = ("telegram", "ntfy", "email", "webhook")


class ConfigError(Exception):
    """Invalid configuration. `errors` holds one line per problem, each starting with the key path."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# ----------------------------------------------------------------------------- dataclasses
@dataclass(frozen=True)
class Web:
    bind: str = "127.0.0.1"
    port: int = 8790


@dataclass(frozen=True)
class Credentials:
    github_token_env: str | None = None
    github_token_expires: datetime.date | None = None
    access_client_id_env: str | None = None
    access_client_secret_env: str | None = None
    access_token_expires: datetime.date | None = None


@dataclass(frozen=True)
class AlertCfg:
    type: str
    min_status: str = "warn"
    url: str | None = None            # ntfy server (public https)
    topic_env: str | None = None      # ntfy
    token_env: str | None = None      # ntfy optional access token
    bot_token_env: str | None = None  # telegram
    chat_id_env: str | None = None    # telegram
    url_env: str | None = None        # webhook target (may embed a secret)
    mode: str = "json"                # webhook: json | healthchecks
    smtp_host: str | None = None      # email
    smtp_port: int = 587
    username_env: str | None = None
    password_env: str | None = None
    from_addr: str | None = None
    to_addr: str | None = None

    @property
    def label(self) -> str:
        return self.type


@dataclass(frozen=True)
class DeadMan:
    url_env: str | None = None


@dataclass(frozen=True)
class GitCfg:
    fetch_minutes: int = 15


@dataclass(frozen=True)
class DriftCfg:
    grace_hours: float = 24.0


@dataclass(frozen=True)
class UnpushedCfg:
    max_age_hours: float = 24.0
    dirty_hours: float = 24.0


@dataclass(frozen=True)
class BackupCfg:
    status_file: str | None = None
    app_name: str | None = None
    max_age_hours: float = 26.0
    required: bool = False
    running_grace_hours: float | None = None  # how long a held lock may mask a bad status; None = max(2 x interval, 6 h)


@dataclass(frozen=True)
class TlsCfg:
    host: str | None = None
    warn_days: float = 21.0
    crit_days: float = 7.0
    every_hours: float = 6.0


@dataclass(frozen=True)
class HeartbeatCfg:
    file: str | None = None
    field: str | None = None
    max_age_minutes: float = 10.0


@dataclass(frozen=True)
class BuildCfg:
    max_age_days: float = 45.0


@dataclass(frozen=True)
class HealthCfg:
    url: str
    expect_status: int = 200
    expect_json: dict | None = None
    timeout_s: float = 10.0
    allow_private: bool = False


@dataclass(frozen=True)
class VersionCfg:
    url: str
    auth: str | None = None           # None | "access"
    timeout_s: float = 10.0
    allow_private: bool = False


@dataclass(frozen=True)
class App:
    name: str
    branch: str = "main"
    github: str | None = None
    repo: str | None = None           # absolute local clone path (~ expanded)
    version_file: str | None = None
    health: HealthCfg | None = None
    version: VersionCfg | None = None
    backup: BackupCfg | None = None
    heartbeat: HeartbeatCfg | None = None
    drift: DriftCfg = field(default_factory=DriftCfg)
    unpushed: UnpushedCfg = field(default_factory=UnpushedCfg)
    tls: TlsCfg = field(default_factory=TlsCfg)
    build: BuildCfg = field(default_factory=BuildCfg)


@dataclass(frozen=True)
class Config:
    interval_seconds: int = 300
    confirm: int = 2
    data_dir: str = "~/.local/share/fleet-truth"
    web: Web = field(default_factory=Web)
    git: GitCfg = field(default_factory=GitCfg)
    credentials: Credentials = field(default_factory=Credentials)
    alerts: tuple[AlertCfg, ...] = ()
    dead_man: DeadMan = field(default_factory=DeadMan)
    apps: tuple[App, ...] = ()

    def env_names(self) -> list[str]:
        """Every env var NAME this config refers to, in first-seen order."""
        out: list[str] = []

        def add(n):
            if n and n not in out:
                out.append(n)

        c = self.credentials
        for n in (c.github_token_env, c.access_client_id_env, c.access_client_secret_env):
            add(n)
        for a in self.alerts:
            for n in (a.topic_env, a.token_env, a.bot_token_env, a.chat_id_env, a.url_env,
                      a.username_env, a.password_env):
                add(n)
        add(self.dead_man.url_env)
        return out


# ----------------------------------------------------------------------------- parsing helpers
class _Ctx:
    def __init__(self) -> None:
        self.errors: list[str] = []

    def err(self, path: str, msg: str) -> None:
        self.errors.append(f"{path}: {msg}")


def _scan_literal_secrets(node: Any, path: str, ctx: _Ctx) -> None:
    """Flag keys that look like literal secrets anywhere in the file (before unknown-key checks)."""
    if isinstance(node, dict):
        for k, v in node.items():
            kp = f"{path}.{k}" if path else str(k)
            ks = str(k)
            if SECRET_KEY_RE.search(ks) and not ks.endswith("_env") and not ks.endswith("_expires"):
                ctx.err(kp, "looks like a literal secret; put it in an env var and reference it as "
                            f"'{ks}_env: NAME' (secrets never live in fleet.yaml)")
            if ks != "expect_json":  # arbitrary response keys to compare, not config
                _scan_literal_secrets(v, kp, ctx)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _scan_literal_secrets(v, f"{path}[{i}]", ctx)


def _obj(v: Any, path: str, ctx: _Ctx, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if v is None:
        v = {}
    if not isinstance(v, dict):
        ctx.err(path, "must be a mapping")
        return {}
    for k in v:
        if k not in allowed:
            ctx.err(f"{path}.{k}" if path else str(k), "unknown key")
    for k in required:
        if k not in v:
            ctx.err(f"{path}.{k}" if path else k, "required key is missing")
    return {k: x for k, x in v.items() if k in allowed}


def _num(d: dict, key: str, path: str, ctx: _Ctx, default: float, lo: float = 0.0, integer: bool = False):
    if key not in d:
        return default
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)):
        ctx.err(f"{path}.{key}", "must be an integer" if integer else "must be a number")
        return default
    if v < lo:
        ctx.err(f"{path}.{key}", f"must be >= {lo:g}")
        return default
    return v


def _bool(d: dict, key: str, path: str, ctx: _Ctx, default: bool = False) -> bool:
    if key not in d:
        return default
    if not isinstance(d[key], bool):
        ctx.err(f"{path}.{key}", "must be true or false")
        return default
    return d[key]


def _str(d: dict, key: str, path: str, ctx: _Ctx, default: str | None = None) -> str | None:
    if key not in d or d[key] is None:
        return default
    v = d[key]
    if not isinstance(v, str) or not v.strip():
        ctx.err(f"{path}.{key}", "must be a non-empty string")
        return default
    return v.strip()


def _env(d: dict, key: str, path: str, ctx: _Ctx) -> str | None:
    v = _str(d, key, path, ctx)
    if v is None:
        return None
    if not key.endswith("_env"):
        ctx.err(f"{path}.{key}", "internal: not an *_env key")
        return None
    if not secrets.ENV_NAME.match(v):
        ctx.err(f"{path}.{key}", "must be an environment variable NAME like FLEET_TOKEN "
                                 "(matching ^[A-Z][A-Z0-9_]*$), not a value")
        return None
    return v


def _date(d: dict, key: str, path: str, ctx: _Ctx) -> datetime.date | None:
    if key not in d or d[key] is None:
        return None
    v = d[key]
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    if isinstance(v, str):
        try:
            return datetime.date.fromisoformat(v)
        except ValueError:
            pass
    ctx.err(f"{path}.{key}", "must be a date like 2027-01-30")
    return None


def _url(v: Any, path: str, ctx: _Ctx, allow_private: bool, public_only: bool = False) -> str | None:
    if not isinstance(v, str) or not v.strip():
        ctx.err(path, "must be a URL string")
        return None
    u = v.strip()
    try:
        s = urlsplit(u)
        host = s.hostname
        s.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        ctx.err(path, "not a valid URL")
        return None
    if s.scheme not in ("http", "https"):
        ctx.err(path, "scheme must be https:// (or http:// with allow_private: true)")
        return None
    if s.scheme == "http" and not allow_private:
        ctx.err(path, "http:// is allowed only with allow_private: true (private/tailnet targets)")
        return None
    if s.username is not None or s.password is not None or "@" in s.netloc:
        ctx.err(path, "URL must not contain user:pass@ credentials")
        return None
    if not host:
        ctx.err(path, "URL has no host")
        return None
    return u


def _path(v: str) -> str:
    return str(Path(v).expanduser())


def _merge(base: dict | None, over: dict | None) -> dict:
    out = dict(base or {})
    out.update(over or {})
    return out


# ----------------------------------------------------------------------------- sections
def _alert(raw: Any, path: str, ctx: _Ctx) -> AlertCfg | None:
    if not isinstance(raw, dict):
        ctx.err(path, "must be a mapping")
        return None
    typ = raw.get("type")
    if typ not in ALERT_TYPES:
        ctx.err(f"{path}.type", f"must be one of {', '.join(ALERT_TYPES)}")
        return None
    per_type = {
        "telegram": {"bot_token_env", "chat_id_env"},
        "ntfy": {"url", "topic_env", "token_env"},
        "email": {"smtp_host", "smtp_port", "username_env", "password_env", "from", "to"},
        "webhook": {"url_env", "mode"},
    }
    d = _obj(raw, path, ctx, {"type", "min_status"} | per_type[typ])
    ms = _str(d, "min_status", path, ctx, "warn")
    if ms not in MIN_STATUSES:
        ctx.err(f"{path}.min_status", f"must be one of {', '.join(MIN_STATUSES)}")
        ms = "warn"
    kw: dict[str, Any] = {"type": typ, "min_status": ms}
    for k in per_type[typ]:
        if k.endswith("_env"):
            kw[k] = _env(d, k, path, ctx)
    if typ == "telegram":
        for k in ("bot_token_env", "chat_id_env"):
            if kw.get(k) is None and f"{path}.{k}" not in " ".join(ctx.errors):
                ctx.err(f"{path}.{k}", "required for telegram")
    elif typ == "ntfy":
        kw["url"] = _url(d.get("url", "https://ntfy.sh"), f"{path}.url", ctx, allow_private=False)
        if kw.get("topic_env") is None:
            ctx.err(f"{path}.topic_env", "required for ntfy")
    elif typ == "webhook":
        mode = _str(d, "mode", path, ctx, "json")
        if mode not in ("json", "healthchecks"):
            ctx.err(f"{path}.mode", "must be json or healthchecks")
            mode = "json"
        kw["mode"] = mode
        if kw.get("url_env") is None:
            ctx.err(f"{path}.url_env", "required for webhook (the URL may embed a secret, so it lives in an env var)")
    elif typ == "email":
        kw["smtp_host"] = _str(d, "smtp_host", path, ctx)
        kw["smtp_port"] = int(_num(d, "smtp_port", path, ctx, 587, 1, integer=True))
        kw["from_addr"] = _str(d, "from", path, ctx)
        kw["to_addr"] = _str(d, "to", path, ctx)
        for k in ("smtp_host", "from_addr", "to_addr"):
            if kw.get(k) is None:
                ctx.err(f"{path}.{'from' if k == 'from_addr' else 'to' if k == 'to_addr' else k}", "required for email")
    return AlertCfg(**kw)


def _app(raw: Any, path: str, defaults: dict, ctx: _Ctx) -> App | None:
    if not isinstance(raw, dict):
        ctx.err(path, "must be a mapping")
        return None
    allowed = {"name", "github", "branch", "repo", "version_file", "health", "version", "backup",
               "heartbeat", "drift", "unpushed", "tls", "build"}
    d = _obj(raw, path, ctx, allowed, {"name"})
    name = _str(d, "name", path, ctx)
    if name is not None and not NAME_RE.match(name):
        ctx.err(f"{path}.name", "must match ^[a-z0-9][a-z0-9-]{0,40}$")
    gh = _str(d, "github", path, ctx)
    if gh is not None and not GITHUB_RE.match(gh):
        ctx.err(f"{path}.github", "must look like owner/repo")
    branch = _str(d, "branch", path, ctx, "main")
    if branch is not None and (not BRANCH_RE.match(branch) or ".." in branch or branch.endswith(("/", ".lock"))):
        ctx.err(f"{path}.branch", "not a safe branch name")
        branch = "main"
    repo = _str(d, "repo", path, ctx)
    vfile = _str(d, "version_file", path, ctx)
    if vfile is not None and (vfile.startswith(("/", "-")) or ".." in vfile.split("/")):
        ctx.err(f"{path}.version_file", "must be a relative path inside the repo")
        vfile = None

    health = None
    if d.get("health") is not None:
        h = _obj(d["health"], f"{path}.health", ctx,
                 {"url", "expect_status", "expect_json", "timeout_s", "allow_private"}, {"url"})
        ap = _bool(h, "allow_private", f"{path}.health", ctx)
        url = _url(h.get("url"), f"{path}.health.url", ctx, ap) if "url" in h else None
        ej = h.get("expect_json")
        if ej is not None and not isinstance(ej, dict):
            ctx.err(f"{path}.health.expect_json", "must be a mapping of key -> value")
            ej = None
        if url:
            health = HealthCfg(url, int(_num(h, "expect_status", f"{path}.health", ctx, 200, 100, True)), ej,
                               float(_num(h, "timeout_s", f"{path}.health", ctx, 10.0, 1)), ap)
    version = None
    if d.get("version") is not None:
        v = _obj(d["version"], f"{path}.version", ctx, {"url", "auth", "timeout_s", "allow_private"}, {"url"})
        ap = _bool(v, "allow_private", f"{path}.version", ctx)
        url = _url(v.get("url"), f"{path}.version.url", ctx, ap) if "url" in v else None
        auth = v.get("auth")
        if auth not in (None, "access"):
            ctx.err(f"{path}.version.auth", "must be 'access' or omitted")
            auth = None
        if url:
            version = VersionCfg(url, auth, float(_num(v, "timeout_s", f"{path}.version", ctx, 10.0, 1)), ap)

    def section(key, cls, allowed_keys, required=frozenset()):
        if d.get(key) is None and not defaults.get(key):
            return None
        merged = _merge(defaults.get(key), d.get(key) if isinstance(d.get(key), dict) else None)
        if d.get(key) is not None and not isinstance(d.get(key), dict):
            ctx.err(f"{path}.{key}", "must be a mapping")
            return None
        return _obj(merged, f"{path}.{key}", ctx, allowed_keys, required)

    drift = section("drift", DriftCfg, {"grace_hours"}) or {}
    unp = section("unpushed", UnpushedCfg, {"max_age_hours", "dirty_hours"}) or {}
    tls = section("tls", TlsCfg, {"host", "warn_days", "crit_days", "every_hours"}) or {}
    build = section("build", BuildCfg, {"max_age_days"}) or {}
    backup_d = section("backup", BackupCfg, {"status_file", "app_name", "max_age_hours", "required", "running_grace"})
    hb_d = section("heartbeat", HeartbeatCfg, {"file", "field", "max_age_minutes"})

    backup = None
    if d.get("backup") is not None and backup_d is not None:
        bp = f"{path}.backup"
        sf = _str(backup_d, "status_file", bp, ctx)
        an = _str(backup_d, "app_name", bp, ctx)
        if sf is None:
            ctx.err(f"{bp}.status_file", "required key is missing")
        if an is None:
            ctx.err(f"{bp}.app_name", "required key is missing")
        if sf and an:
            backup = BackupCfg(_path(sf), an, float(_num(backup_d, "max_age_hours", bp, ctx, 26.0, 0.001)),
                               _bool(backup_d, "required", bp, ctx),
                               float(_num(backup_d, "running_grace", bp, ctx, 0, 0.001)) if "running_grace" in backup_d else None)
    heartbeat = None
    if d.get("heartbeat") is not None and hb_d is not None:
        hp = f"{path}.heartbeat"
        f = _str(hb_d, "file", hp, ctx)
        fld = _str(hb_d, "field", hp, ctx)
        for k, val in (("file", f), ("field", fld)):
            if val is None:
                ctx.err(f"{hp}.{k}", "required key is missing")
        if f and fld:
            heartbeat = HeartbeatCfg(_path(f), fld, float(_num(hb_d, "max_age_minutes", hp, ctx, 10.0, 0.001)))

    if name is None:
        return None
    return App(
        name=name, branch=branch or "main", github=gh, repo=_path(repo) if repo else None,
        version_file=vfile, health=health, version=version, backup=backup, heartbeat=heartbeat,
        drift=DriftCfg(float(_num(drift, "grace_hours", f"{path}.drift", ctx, 24.0, 0))),
        unpushed=UnpushedCfg(float(_num(unp, "max_age_hours", f"{path}.unpushed", ctx, 24.0, 0)),
                             float(_num(unp, "dirty_hours", f"{path}.unpushed", ctx, 24.0, 0))),
        tls=TlsCfg(_str(tls, "host", f"{path}.tls", ctx),
                   float(_num(tls, "warn_days", f"{path}.tls", ctx, 21.0, 0)),
                   float(_num(tls, "crit_days", f"{path}.tls", ctx, 7.0, 0)),
                   float(_num(tls, "every_hours", f"{path}.tls", ctx, 6.0, 0.001))),
        build=BuildCfg(float(_num(build, "max_age_days", f"{path}.build", ctx, 45.0, 0))),
    )


def parse(data: Any) -> Config:
    """Validate an already-loaded YAML document. Raises ConfigError listing every problem."""
    ctx = _Ctx()
    if not isinstance(data, dict):
        raise ConfigError(["<root>: the file must contain a mapping"])
    _scan_literal_secrets(data, "", ctx)
    top = _obj(data, "", ctx, {"interval_seconds", "confirm", "data_dir", "web", "git", "credentials", "alerts",
                               "dead_man", "defaults", "apps"})
    interval = int(_num(top, "interval_seconds", "interval_seconds", ctx, 300, 10, True)) if "interval_seconds" in top else 300
    confirm = int(_num(top, "confirm", "confirm", ctx, 2, 1, True)) if "confirm" in top else 2
    data_dir = _str(top, "data_dir", "data_dir", ctx, "~/.local/share/fleet-truth") or "~/.local/share/fleet-truth"

    w = _obj(top.get("web"), "web", ctx, {"bind", "port"})
    web = Web(_str(w, "bind", "web", ctx, "127.0.0.1") or "127.0.0.1", int(_num(w, "port", "web", ctx, 8790, 1, True)))
    g = _obj(top.get("git"), "git", ctx, {"fetch_minutes"})
    git = GitCfg(int(_num(g, "fetch_minutes", "git", ctx, 15, 1, True)))

    c = _obj(top.get("credentials"), "credentials", ctx,
             {"github_token_env", "github_token_expires", "access_client_id_env", "access_client_secret_env",
              "access_token_expires"})
    creds = Credentials(
        _env(c, "github_token_env", "credentials", ctx), _date(c, "github_token_expires", "credentials", ctx),
        _env(c, "access_client_id_env", "credentials", ctx), _env(c, "access_client_secret_env", "credentials", ctx),
        _date(c, "access_token_expires", "credentials", ctx))

    alerts: list[AlertCfg] = []
    raw_alerts = top.get("alerts")
    if raw_alerts is not None and not isinstance(raw_alerts, list):
        ctx.err("alerts", "must be a list")
    for i, ra in enumerate(raw_alerts if isinstance(raw_alerts, list) else []):
        a = _alert(ra, f"alerts[{i}]", ctx)
        if a:
            alerts.append(a)

    dm = _obj(top.get("dead_man"), "dead_man", ctx, {"url_env"})
    dead_man = DeadMan(_env(dm, "url_env", "dead_man", ctx))

    defaults = _obj(top.get("defaults"), "defaults", ctx, {"drift", "unpushed", "backup", "tls", "build", "heartbeat"})

    apps: list[App] = []
    seen: set[str] = set()
    raw_apps = top.get("apps")
    if raw_apps is None:
        ctx.err("apps", "required key is missing (at least one app)")
    elif not isinstance(raw_apps, list) or not raw_apps:
        ctx.err("apps", "must be a non-empty list")
    else:
        for i, ra in enumerate(raw_apps):
            a = _app(ra, f"apps[{i}]", defaults, ctx)
            if a is None:
                continue
            if a.name in seen:
                ctx.err(f"apps[{i}].name", f"duplicate app name '{a.name}'")
            seen.add(a.name)
            apps.append(a)

    for a in apps:
        if a.version and a.version.auth == "access":
            if not (creds.access_client_id_env and creds.access_client_secret_env):
                ctx.err(f"apps[{a.name}].version.auth",
                        "auth: access needs credentials.access_client_id_env and access_client_secret_env")
                break

    if ctx.errors:
        raise ConfigError(ctx.errors)
    cfg = Config(interval, confirm, data_dir, web, git, creds, tuple(alerts), dead_man, tuple(apps))
    secrets.register(*cfg.env_names())
    return cfg


def load(path: str | Path) -> Config:
    p = Path(path).expanduser()
    try:
        text = p.read_text()
    except OSError as e:
        raise ConfigError([f"{p}: cannot read ({e.strerror or type(e).__name__})"]) from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" line {mark.line + 1}" if mark else ""
        raise ConfigError([f"{p}:{where} invalid YAML"]) from None
    return parse(data)


# ----------------------------------------------------------------------------- plan
def checks_for(app: App) -> list[str]:
    """Names of the checks that will run for this app (cycle order, design 6.4)."""
    out: list[str] = []
    if app.health:
        out.append("HEALTHZ")
    if app.version:
        out.append("DEEP_PROBE")
        if app.github or app.repo:
            out.append("DRIFT")
    if app.repo:
        out.append("UNPUSHED")
    if app.backup:
        out.append("BACKUP_AGE")
    if app.health and app.health.url.startswith("https://") or app.tls.host:
        out.append("TLS_EXPIRY")
    if app.heartbeat:
        out.append("HEARTBEAT")
    if app.version:
        out.append("STALE_BUILD")
    return out


def render_plan(cfg: Config) -> str:
    """Human-readable resolved plan. Env var NAMES only, never values."""
    lines = [f"{len(cfg.apps)} apps, interval {cfg.interval_seconds}s, confirm {cfg.confirm}"]
    for a in cfg.apps:
        lines.append(f"  {a.name}  branch={a.branch}  checks: {', '.join(checks_for(a)) or '(none)'}")
    missing = [n for n in cfg.env_names() if secrets.get(n) is None]
    if missing:
        lines.append("missing env vars (names only; dependent checks will report unknown): " + ", ".join(missing))
    else:
        lines.append("all referenced env vars are set")
    return "\n".join(lines)

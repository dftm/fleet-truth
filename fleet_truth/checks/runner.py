"""Per-app check sequence (design 6.4): mirror refresh -> HEALTHZ -> DEEP_PROBE -> DRIFT -> UNPUSHED ->
BACKUP_AGE -> TLS_EXPIRY (every `tls.every_hours`) -> HEARTBEAT -> STALE_BUILD.

A check never raises: an exception becomes `unknown` ERROR with the exception class only.
DRIFT and STALE_BUILD are `unknown` PROBE_FAILED when DEEP_PROBE did not succeed (no double alert).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable, Iterator

from .. import gitmirror as gm
from .. import net, secrets
from ..config import App, Config
from . import drift as drift_mod
from . import files as files_mod
from . import http as http_mod
from .base import CheckResult

log = logging.getLogger("fleet_truth.runner")


def _safe(name: str, fn: Callable[[], CheckResult]) -> CheckResult:
    try:
        return fn()
    except Exception as e:  # noqa: BLE001 - the contract is "never raises"
        log.warning("check %s raised %s", name, type(e).__name__)
        return CheckResult("unknown", "ERROR", f"check raised {type(e).__name__}")


class Runner:
    def __init__(self, cfg: Config, *, fetch: Callable = net.fetch, cert_fn: Callable = net.peer_cert,
                 sleep: Callable[[float], None] = time.sleep, get_secret: Callable[[str], str | None] = secrets.get,
                 meta_get: Callable[[str], str | None] | None = None, meta_set: Callable[[str, str], None] | None = None,
                 spec_factory: Callable = gm.spec_for):
        self.cfg = cfg
        self.fetch, self.cert_fn, self.sleep, self.get_secret = fetch, cert_fn, sleep, get_secret
        self.meta_get, self.meta_set = meta_get, meta_set
        self.spec_factory = spec_factory
        self.data_dir = Path(cfg.data_dir).expanduser()
        self.refresh_results: dict[str, gm.RefreshResult] = {}
        self._askpass: Path | None = None

    def _askpass_path(self) -> Path:
        if self._askpass is None:
            self._askpass = gm.write_askpass(self.data_dir)
        return self._askpass

    def _tls_due(self, app: App, now: float) -> bool:
        if self.meta_get is None:
            return True
        last = self.meta_get(f"tls_last:{app.name}")
        try:
            return last is None or now - float(last) >= app.tls.every_hours * 3600
        except ValueError:
            return True

    def reference_ok(self, now: float) -> bool | None:
        """True if any reference URL answers (any HTTP status below 500), False if none does, None if none configured."""
        if not self.cfg.reference_urls:
            return None
        for url in self.cfg.reference_urls:
            try:
                r = self.fetch(url, headers={}, allow_private=False, timeout_s=5.0)
                if r.status < 500:
                    return True
            except Exception:  # noqa: BLE001 - any failure means this reference did not answer
                continue
        return False

    def run_app(self, app: App, now: float) -> Iterator[tuple[str, CheckResult]]:
        cfg = self.cfg
        spec = None
        if app.github or app.repo:
            spec = self.spec_factory(app, self.data_dir)
            token = self.get_secret(cfg.credentials.github_token_env) if cfg.credentials.github_token_env else None
            try:
                res = gm.refresh(spec, now, cfg.git.fetch_minutes, token, self._askpass_path() if token else None)
            except Exception as e:  # noqa: BLE001
                res = gm.RefreshResult(False, False, False, f"refresh raised {type(e).__name__}")
            self.refresh_results[app.name] = res
        probe: CheckResult | None = None
        if app.health:
            yield "HEALTHZ", _safe("HEALTHZ", lambda: http_mod.healthz(app, self.fetch, self.sleep))
        if app.version:
            probe = _safe("DEEP_PROBE", lambda: http_mod.deep_probe(app, cfg.credentials, self.fetch, self.sleep, self.get_secret))
            yield "DEEP_PROBE", probe
            if app.github or app.repo:
                if probe.status != "ok":
                    yield "DRIFT", CheckResult("unknown", "PROBE_FAILED", "DEEP_PROBE did not succeed this cycle")
                else:
                    assert spec is not None
                    live = {k: probe.data.get(k) for k in ("commit", "version", "build", "builtAt")}
                    if probe.data.get("commitFull"):
                        live["commit"] = probe.data["commitFull"]  # prefer the full SHA when the app offers one
                    yield "DRIFT", _safe("DRIFT", lambda: drift_mod.drift_check(
                        spec.mirror_dir, live, app.branch, app.version_file, app.drift.grace_hours, now))
        if app.repo:
            assert spec is not None
            yield "UNPUSHED", _safe("UNPUSHED", lambda: drift_mod.unpushed_check(
                spec.mirror_dir, Path(app.repo), app.branch, app.unpushed.max_age_hours, app.unpushed.dirty_hours, now))
        if app.backup:
            yield "BACKUP_AGE", _safe("BACKUP_AGE", lambda: files_mod.backup_age(
                app.backup, now, interval_s=cfg.interval_seconds, meta_get=self.meta_get, meta_set=self.meta_set))
        if (app.health and app.health.url.startswith("https://") or app.tls.host) and self._tls_due(app, now):
            yield "TLS_EXPIRY", _safe("TLS_EXPIRY", lambda: http_mod.tls_expiry(app, now, self.cert_fn))
            if self.meta_set:
                self.meta_set(f"tls_last:{app.name}", f"{now:.0f}")
        if app.heartbeat:
            yield "HEARTBEAT", _safe("HEARTBEAT", lambda: files_mod.heartbeat(app.heartbeat, now))
        if app.version:
            if probe is not None and probe.status != "ok":
                yield "STALE_BUILD", CheckResult("unknown", "PROBE_FAILED", "DEEP_PROBE did not succeed this cycle")
            else:
                yield "STALE_BUILD", _safe("STALE_BUILD", lambda: files_mod.stale_build(
                    (probe.data.get("builtAt") if probe else None), app.build.max_age_days, now))

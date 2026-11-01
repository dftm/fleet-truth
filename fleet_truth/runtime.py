"""Glue for the CLI: load config, open the store, run one cycle."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import alerts, net, secrets, version
from . import config as cfgmod
from . import cycle as cyc
from .checks.runner import Runner
from .store import Store

log = logging.getLogger("fleet_truth")
DB_NAME = "fleet-truth.db"


def db_path(cfg: cfgmod.Config) -> Path:
    return Path(cfg.data_dir).expanduser() / DB_NAME


def open_store(cfg: cfgmod.Config) -> Store:
    d = Path(cfg.data_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    return Store(db_path(cfg))


def format_summary(cr: cyc.CycleResult) -> str:
    lines = []
    for (app, check), r in sorted(cr.results.items()):
        lines.append(f"{r.status.upper():8} {app:14} {check:12} {r.verdict}")
    return "\n".join(lines)


@dataclass
class Outcome:
    cr: cyc.CycleResult
    store: Store
    runner: object
    deliveries: list = field(default_factory=list)
    dead_man: alerts.DeadManResult | None = None


def run_once(cfg: cfgmod.Config, *, runner_factory: Callable | None = None, now: float | None = None,
             deadline_s: float | None = None, fetch: Callable | None = None, senders: list | None = None,
             get_secret: Callable[[str], str | None] = secrets.get) -> Outcome:
    """One cycle, then alerts, then the dead-man ping. A cycle error pings `/fail` and re-raises."""
    secrets.install_log_scrubber()
    store = open_store(cfg)
    fetch_fn = fetch or net.fetch
    kw = {"fetch": fetch} if fetch else {}
    runner = (runner_factory or Runner)(cfg, meta_get=store.meta_get, meta_set=store.meta_set, **kw)
    snd = alerts.build_senders(cfg, fetch_fn, get_secret) if senders is None else senders
    disp = alerts.Dispatcher(snd, store, alerts.page_url(cfg))
    try:
        cr = cyc.run_cycle(cfg, store, runner, now=now, deadline_s=deadline_s)
    except Exception as e:
        alerts.dead_man_ping(cfg, cycle_ok=False, error_class=type(e).__name__, channels_healthy=disp.channels_healthy(),
                             fetch=fetch_fn, get_secret=get_secret, store=store)
        raise
    deliveries = disp.dispatch(cr.decisions)
    for d in deliveries:
        if d.undelivered:  # nobody heard it: retry next cycle
            cyc.rollback_alert_state(store, d.decision)
    dm = alerts.dead_man_ping(cfg, cycle_ok=True, error_class=None, channels_healthy=disp.channels_healthy(),
                              fetch=fetch_fn, get_secret=get_secret, store=store)
    return Outcome(cr, store, runner, deliveries, dm)


def test_alerts(cfg: cfgmod.Config, *, fetch: Callable | None = None,
                get_secret: Callable[[str], str | None] = secrets.get, senders: list | None = None) -> dict:
    """Send one [TEST] message to every configured channel (ignores min_status)."""
    secrets.install_log_scrubber()
    snd = alerts.build_senders(cfg, fetch or net.fetch, get_secret) if senders is None else senders
    store = open_store(cfg)  # test sends are recorded, so a repaired channel counts as healthy again
    try:
        return alerts.Dispatcher(snd, store, alerts.page_url(cfg)).test_all(version.read_version())
    finally:
        store.close()

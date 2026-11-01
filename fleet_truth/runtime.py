"""Glue for the CLI: load config, open the store, run one cycle."""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Callable

from . import config as cfgmod
from . import cycle as cyc
from . import secrets
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


def run_once(cfg: cfgmod.Config, *, runner_factory: Callable | None = None, now: float | None = None,
             deadline_s: float | None = None) -> tuple[cyc.CycleResult, Store, Runner]:
    secrets.install_log_scrubber()
    store = open_store(cfg)
    runner = (runner_factory or Runner)(cfg, meta_get=store.meta_get, meta_set=store.meta_set)
    cr = cyc.run_cycle(cfg, store, runner, now=now, deadline_s=deadline_s)
    return cr, store, runner

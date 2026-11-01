"""Glue for the CLI: load config, open the store, run one cycle."""
from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import alerts, net, secrets, version
from . import config as cfgmod
from . import cycle as cyc
from .checks.runner import Runner
from .state import AlertDecision
from .store import SchemaTooNew, Store, StoreError

log = logging.getLogger("fleet_truth")
DB_NAME = "fleet-truth.db"


def db_path(cfg: cfgmod.Config) -> Path:
    return Path(cfg.data_dir).expanduser() / DB_NAME


class RunLocked(Exception):
    """Another run holds the data dir's run lock."""


class LockError(Exception):
    """The lock itself failed (not contention): e.g. ENOLCK on drvfs, 9p or NFS. An internal error, exit 4."""


@contextlib.contextmanager
def run_lock(cfg: cfgmod.Config):
    """Exclusive non-blocking flock on <data_dir>/run.lock: two overlapping runs would send duplicate alerts."""
    d = Path(cfg.data_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(d / "run.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:   # EWOULDBLOCK/EAGAIN: someone else holds it
            raise RunLocked("another fleet-truth run holds the lock") from None
        except OSError as e:      # anything else is a broken lock, not a busy one
            name = errno.errorcode.get(e.errno or 0, "errno " + str(e.errno))
            log.error("run lock failed on %s: %s", d / "run.lock", name)
            raise LockError(f"cannot take the run lock in {d} ({name}); keep data_dir on the Linux filesystem, "
                            "not on /mnt/c (drvfs/9p/NFS may not support flock)") from None
        try:  # who holds it and since when, for stale-run detection
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {int(time.time())}\n".encode())
        except OSError:
            pass
        yield
    finally:
        os.close(fd)  # closing releases the flock


def _report_stale_run(cfg: cfgmod.Config, fetch_fn, get_secret) -> None:
    """The lock is held by a run older than 2 intervals (hung or overrunning): tell the dead-man once per
    stuck run, so exit 5 forever is not silent."""
    d = Path(cfg.data_dir).expanduser()
    try:
        started = int((d / "run.lock").read_text().split()[1])
    except (OSError, ValueError, IndexError):
        return
    if time.time() - started <= 2 * cfg.interval_seconds:
        return
    reported = d / "run.lock.stale-reported"
    try:
        if reported.read_text().strip() == str(started):
            return
    except OSError:
        pass
    log.error("a previous run has held the run lock since %s (more than 2 intervals): it is hung or overrunning", started)
    alerts.dead_man_ping(cfg, cycle_ok=False, error_class="StaleRun", channels_healthy=False,
                         fetch=fetch_fn, get_secret=get_secret)
    with contextlib.suppress(OSError):
        reported.write_text(f"{started}\n")


def _move_aside(path: Path) -> Path:
    """Rename a bad database (and its WAL/SHM files) out of the way. Never deletes."""
    ns = time.time_ns()
    # sortable and monotonic even when copies were deleted in between (a reused counter would reorder them)
    dest = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S', time.localtime(ns // 10**9))}-{ns % 10**9:09d}")
    for suffix in ("", "-wal", "-shm"):
        src = path.with_name(path.name + suffix)
        if src.exists():
            os.rename(src, dest.with_name(dest.name + suffix))
    return dest


MAX_CORRUPT_COPIES = 3


def _cap_corrupt_copies(path: Path) -> None:
    """Keep the FIRST corrupt copy ever made plus the newest ones, at most MAX_CORRUPT_COPIES in total."""
    copies = sorted(p for p in path.parent.glob(path.name + ".corrupt-*") if not p.name.endswith(("-wal", "-shm")))
    if len(copies) <= MAX_CORRUPT_COPIES:
        return
    keep = {copies[0], *copies[-(MAX_CORRUPT_COPIES - 1):]}
    for c in copies:
        if c not in keep:
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(OSError):
                    os.unlink(c.with_name(c.name + suffix))


def marker_path(cfg: cfgmod.Config, config_path: str | Path | None = None) -> Path:
    """The 'this install has run before' marker lives OUTSIDE the data dir (next to fleet.yaml, or next to the
    data dir when no config path is known), so wiping the whole data dir is detected as a reset."""
    d = Path(cfg.data_dir).expanduser()
    base = Path(config_path).expanduser().resolve().parent if config_path else d.parent
    tag = hashlib.sha1(str(d.resolve()).encode()).hexdigest()[:8]
    return base / f".fleet-truth-{tag}.initialized"


def open_store_checked(cfg: cfgmod.Config, config_path: str | Path | None = None) -> tuple[Store, str | None]:
    """Open the DB; returns (store, reset_note). A corrupt file is moved aside; a DB that vanished after a
    previous run (marker present) is reported too. The note is None for a normal open."""
    d = Path(cfg.data_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass
    path = db_path(cfg)
    marker = marker_path(cfg, config_path)
    note = None
    existed = path.exists()
    try:
        store = Store(path)
    except SchemaTooNew:
        raise  # a rollback, not corruption: keep the file, say so, let the caller ping /fail
    except StoreError:
        moved = _move_aside(path)
        _cap_corrupt_copies(path)
        store = Store(path)
        note = f"database was unusable and was moved to {moved.name}; history, alert de-duplication and channel health are reset"
    else:
        if not existed and marker.exists():
            note = "database file was missing; history, alert de-duplication and channel health are reset"
    if note:
        store.meta_set("state_reset_at", str(int(time.time())))
    try:
        marker.touch()
    except OSError:
        log.warning("cannot write the reset marker %s; a wiped data dir will not be flagged", marker.name)
    return store, note


def open_store(cfg: cfgmod.Config) -> Store:
    return open_store_checked(cfg)[0]


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
             get_secret: Callable[[str], str | None] = secrets.get, config_path: str | Path | None = None) -> Outcome:
    """One cycle, then alerts, then the dead-man ping. A cycle error pings `/fail` and re-raises."""
    secrets.install_log_scrubber()
    fetch_fn = fetch or net.fetch
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(run_lock(cfg))
    except RunLocked:
        _report_stale_run(cfg, fetch_fn, get_secret)
        raise
    except Exception as e:  # cannot even create the lock/data dir
        alerts.dead_man_ping(cfg, cycle_ok=False, error_class=type(e).__name__, channels_healthy=False,
                             fetch=fetch_fn, get_secret=get_secret)
        raise
    with stack:
        try:
            store, reset_note = open_store_checked(cfg, config_path)
        except Exception as e:
            alerts.dead_man_ping(cfg, cycle_ok=False, error_class=type(e).__name__, channels_healthy=False,
                                 fetch=fetch_fn, get_secret=get_secret)
            raise
        return _run_locked(cfg, store, reset_note, runner_factory, now, deadline_s, fetch, fetch_fn, senders, get_secret)


def _run_locked(cfg, store, reset_note, runner_factory, now, deadline_s, fetch, fetch_fn, senders, get_secret) -> Outcome:
    kw = {"fetch": fetch} if fetch else {}
    runner = (runner_factory or Runner)(cfg, meta_get=store.meta_get, meta_set=store.meta_set, **kw)
    snd = alerts.build_senders(cfg, fetch_fn, get_secret) if senders is None else senders
    disp = alerts.Dispatcher(snd, store, alerts.page_url(cfg), budget_s=cfg.alert_budget_seconds,
                             channel_budget_s=cfg.channel_budget_seconds, backoff_cycles=cfg.channel_backoff_cycles)
    if reset_note:
        # the corrupt/missing DB is itself a failure: tell the dead-man now, and below tell the humans
        alerts.dead_man_ping(cfg, cycle_ok=False, error_class="StateReset", channels_healthy=False,
                             fetch=fetch_fn, get_secret=get_secret)
    try:
        cr = cyc.run_cycle(cfg, store, runner, now=now, deadline_s=deadline_s)
    except Exception as e:
        alerts.dead_man_ping(cfg, cycle_ok=False, error_class=type(e).__name__, channels_healthy=disp.channels_healthy(),
                             fetch=fetch_fn, get_secret=get_secret, store=store)
        raise
    decisions = list(cr.decisions)
    if reset_note:  # also the conservative channel re-test: this send is recorded, so a dead channel shows as failing
        decisions.append(AlertDecision("reset", "_monitor", "STATE", "warn", "crit", "STATE_RESET", reset_note,
                                       int(cr.started_at), 1))
    bad = {"crit", "unknown"}
    bad_apps = {a for (a, _c), r in cr.results.items() if r.status in bad}
    for st in store.all_states():  # an un-recovered crit/blind alert from an earlier cycle also keeps things not-clear
        if st.alerted_status in ("crit", "unknown"):
            bad_apps.add(st.app)
    cycle_clear = not bad_apps
    clear_apps = {a.name for a in cfg.apps} - bad_apps
    deliveries = disp.dispatch(decisions, cycle_clear, clear_apps)
    for d in deliveries:
        if (d.delivered and d.decision.app == cyc.MONITOR_APP and d.decision.check == cyc.NETWORK_CHECK
                and d.decision.verdict == "MONITOR_OFFLINE"):
            store.meta_set("offline_alert_delivered", "1")  # it got out, so the monitor is online: stop suppressing
    for d in deliveries:
        if d.undelivered and d.decision.kind != "reset":  # nobody heard it: retry next cycle
            cyc.rollback_alert_state(store, d.decision)
    lost = any(d.undelivered for d in deliveries)
    # an alert nobody received means the monitor cannot do its job: say so through the dead-man, never "all good"
    dm = alerts.dead_man_ping(cfg, cycle_ok=not lost, error_class="AlertUndelivered" if lost else None,
                              channels_healthy=disp.channels_healthy(), fetch=fetch_fn, get_secret=get_secret, store=store)
    return Outcome(cr, store, runner, deliveries, dm)


def test_alerts(cfg: cfgmod.Config, *, fetch: Callable | None = None,
                get_secret: Callable[[str], str | None] = secrets.get, senders: list | None = None) -> dict:
    """Send one [TEST] message to every configured channel (ignores min_status).

    Takes the run lock (RunLocked when a run is in progress) and only ever OPENS the database: an unusable file
    is left exactly where it is (no store: the sends are then not recorded), never moved aside or replaced."""
    secrets.install_log_scrubber()
    snd = alerts.build_senders(cfg, fetch or net.fetch, get_secret) if senders is None else senders
    with run_lock(cfg):
        store = None
        try:
            store = Store(db_path(cfg))   # test sends are recorded, so a repaired channel counts as healthy again
        except StoreError as e:
            log.warning("test-alert: database not opened, sends will not be recorded: %s", type(e).__name__)
        try:
            return alerts.Dispatcher(snd, store, alerts.page_url(cfg), budget_s=cfg.alert_budget_seconds,
                                     channel_budget_s=cfg.channel_budget_seconds).test_all(version.read_version())
        finally:
            if store is not None:
                store.close()

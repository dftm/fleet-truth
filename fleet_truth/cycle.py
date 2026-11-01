"""The cycle: run every app's checks (thread pool of 4, per-app order, hard deadline), write results,
advance check_state, apply correlation, return alert decisions (design 6.4, 12).

Sending is alerts.py. A check that outlives the deadline is recorded `unknown` TIMEOUT_CYCLE and the
cycle returns on time; the stuck worker thread is a daemon and its late result is discarded.
"""
from __future__ import annotations

import dataclasses
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import config as cfgmod
from .checks.base import RANK, CheckResult, worst
from .config import App, Config
from . import flap
from .state import AlertDecision, advance
from .store import Store

MONITOR_APP = "_monitor"
ACCESS_CHECK = "ACCESS_TOKEN"
NETWORK_CHECK = "NETWORK"
NETWORK_CHECKS = ("HEALTHZ", "DEEP_PROBE", "TLS_EXPIRY")
NET_ERRORS = ("CONNECT", "DNS", "TIMEOUT", "PROTOCOL")


@dataclass
class CycleResult:
    run_id: int
    started_at: float
    results: dict = field(default_factory=dict)       # (app, check) -> CheckResult
    decisions: list = field(default_factory=list)     # AlertDecision, correlated and de-duplicated
    timed_out_apps: list = field(default_factory=list)
    worst: str = "ok"

    @property
    def exit_code(self) -> int:
        return {"ok": 0, "info": 0, "unknown": 1, "warn": 1, "crit": 2}[self.worst]


def _run_apps(cfg: Config, runner, now: float, deadline_s: float, workers: int
              ) -> tuple[dict, list[str]]:
    """-> ({app: [(check, result)]}, timed-out app names)."""
    q: queue.Queue = queue.Queue()
    for a in cfg.apps:
        q.put(a)
    lock = threading.Lock()
    partial: dict[str, list] = {a.name: [] for a in cfg.apps}
    done: set[str] = set()
    cancelled = threading.Event()
    all_done = threading.Event()

    def worker() -> None:
        while not cancelled.is_set():
            try:
                app = q.get_nowait()
            except queue.Empty:
                return
            try:
                for name, res in runner.run_app(app, now):
                    with lock:
                        if cancelled.is_set():
                            return
                        partial[app.name].append((name, res))
            except Exception as e:  # noqa: BLE001 - runner contract says no raise; belt and braces
                with lock:
                    if not cancelled.is_set():
                        partial[app.name].append(("RUNNER", CheckResult("unknown", "ERROR", f"runner raised {type(e).__name__}")))
            with lock:
                done.add(app.name)
                if len(done) == len(partial):
                    all_done.set()

    threads = [threading.Thread(target=worker, daemon=True, name=f"ft-worker-{i}") for i in range(min(workers, max(1, len(cfg.apps))))]
    for t in threads:
        t.start()
    all_done.wait(timeout=deadline_s)
    with lock:
        cancelled.set()
        snapshot = {k: list(v) for k, v in partial.items()}
        timed_out = [a.name for a in cfg.apps if a.name not in done]
    return snapshot, timed_out


def correlate(cfg: Config, results: dict, reference: Callable[[], bool | None] | None = None,
              meta_get: Callable[[str], str | None] | None = None,
              meta_set: Callable[[str, str], None] | None = None) -> tuple[dict, set]:
    """Add monitor-level pseudo results and return (extra results, suppressed (app, check) keys).

    When every app's HEALTHZ failed at the network layer, the cause is ambiguous: this machine's network or
    the whole fleet (one VPS or tunnel). An independent reference URL decides, and only a failing reference
    suppresses per-app alerts:
      reference answers  -> FLEET_OUTAGE (crit), per-app alerts still go out;
      reference fails    -> MONITOR_OFFLINE (crit), per-app network alerts suppressed this cycle;
      no reference set   -> ALL_APPS_FAILING (crit), says it cannot tell, suppresses nothing.
    Suppression is capped: at most `offline_suppress_cycles` cycles, and it ends as soon as a MONITOR_OFFLINE
    alert was actually delivered (that proves the monitor is online, so the outage is the fleet's): from then on
    the result is FLEET_OUTAGE and per-app alerts flow.
    """
    extra: dict = {}
    suppressed: set = set()
    hz = {a.name: results.get((a.name, "HEALTHZ")) for a in cfg.apps if a.health}
    hz = {k: v for k, v in hz.items() if v is not None}
    all_fail = len(hz) >= 2 and all(v.status == "crit" and v.data.get("net_error") in NET_ERRORS for v in hz.values())
    offline = False
    ref_down = False
    if len(hz) >= 2:
        if not all_fail:
            extra[(MONITOR_APP, NETWORK_CHECK)] = CheckResult("ok", "OK", "network reachable")
        else:
            n = len(hz)
            try:
                ref = reference() if reference else None
            except Exception:  # noqa: BLE001
                ref = None
            if ref is True:
                extra[(MONITOR_APP, NETWORK_CHECK)] = CheckResult(
                    "crit", "FLEET_OUTAGE", f"fleet-wide outage (VPS or tunnel): all {n} apps fail from here while the "
                    "reference URL answers. Per-app alerts are not suppressed")
            elif ref is False:
                ref_down = True
                streak = 1
                delivered = False
                if meta_get:
                    try:
                        streak = int(meta_get("offline_streak") or 0) + 1
                    except ValueError:
                        streak = 1
                    delivered = meta_get("offline_alert_delivered") == "1"
                if meta_set:
                    meta_set("offline_streak", str(streak))
                if delivered or streak > cfg.offline_suppress_cycles:
                    why = ("a monitor-offline alert was delivered, so the monitor is online" if delivered
                           else f"suppression is capped at {cfg.offline_suppress_cycles} cycles")
                    extra[(MONITOR_APP, NETWORK_CHECK)] = CheckResult(
                        "crit", "FLEET_OUTAGE", f"fleet-wide outage (VPS or tunnel): all {n} apps fail and the "
                        f"reference URL fails too, but {why}. Per-app alerts are not suppressed")
                else:
                    offline = True
                    extra[(MONITOR_APP, NETWORK_CHECK)] = CheckResult(
                        "crit", "MONITOR_OFFLINE", f"monitor network offline: all {n} apps and the reference URL fail "
                        "from here. App network alerts are suppressed this cycle (capped)")
            else:
                extra[(MONITOR_APP, NETWORK_CHECK)] = CheckResult(
                    "crit", "ALL_APPS_FAILING", f"all {n} apps failing simultaneously: VPS/tunnel outage OR monitor "
                    "offline (no reference_urls configured to tell which). Per-app alerts are not suppressed")
    if meta_set and meta_get and not ref_down and (meta_get("offline_streak") or meta_get("offline_alert_delivered")):
        meta_set("offline_streak", "0")        # the episode is over: the next one starts a fresh cap
        meta_set("offline_alert_delivered", "0")
    if offline:
        for a in cfg.apps:
            for c in NETWORK_CHECKS:
                suppressed.add((a.name, c))
    # every auth: access app's DEEP_PROBE says AUTH_REJECTED -> one shared cause, one alert
    acc = [a for a in cfg.apps if a.version and a.version.auth == "access"]
    probes = {a.name: results.get((a.name, "DEEP_PROBE")) for a in acc}
    probes = {k: v for k, v in probes.items() if v is not None}
    if len(acc) >= 2 and len(probes) == len(acc) and not offline:
        if all(v.verdict == "AUTH_REJECTED" for v in probes.values()):
            extra[(MONITOR_APP, ACCESS_CHECK)] = CheckResult(
                "crit", "TOKEN_REFUSED_EVERYWHERE",
                f"the Access service token was refused by all {len(acc)} apps in one cycle: check the token's "
                "expiry and its Service Auth policies first")
            for a in acc:
                suppressed.add((a.name, "DEEP_PROBE"))
        else:
            extra[(MONITOR_APP, ACCESS_CHECK)] = CheckResult("ok", "OK", "service token accepted by at least one app")
    return extra, suppressed


def run_cycle(cfg: Config, store: Store, runner, *, now: float | None = None,
              now_fn: Callable[[], float] = time.time, deadline_s: float | None = None, workers: int = 4) -> CycleResult:
    now = now_fn() if now is None else now
    deadline = cfg.check_deadline_s if deadline_s is None else deadline_s
    run_id = store.start_run(now)
    cr = CycleResult(run_id, now)
    try:
        per_app, timed_out = _run_apps(cfg, runner, now, deadline, workers)
        cr.timed_out_apps = timed_out
        for app in cfg.apps:
            got = dict(per_app.get(app.name, []))
            for name, res in per_app.get(app.name, []):
                cr.results[(app.name, name)] = res
            if app.name in timed_out:
                for name in cfgmod.checks_for(app):
                    if name not in got:
                        cr.results[(app.name, name)] = CheckResult(
                            "unknown", "TIMEOUT_CYCLE", f"check did not finish within the {deadline:.0f}s cycle deadline")
        ref_fn = (lambda: runner.reference_ok(now)) if hasattr(runner, "reference_ok") else None
        extra, suppressed = correlate(cfg, cr.results, ref_fn, store.meta_get, store.meta_set)
        for key, res in extra.items():
            cr.results[key] = res
        for (app, check), res in cr.results.items():
            store.add_result(run_id, app, check, res, now)
            prev = store.get_state(app, check)
            reopen_s = max(1800.0, cfg.reopen_cooldown_cycles * cfg.interval_seconds)
            row, decision = advance(prev, app, check, res, now, cfg.confirm, cfg.blind_level, cfg.recover_after, reopen_s)
            if (app, check) in suppressed:
                # a correlated cause is reported once for all apps: nothing is "alerted" for this one, its
                # confirmation restarts from zero, and the cycle is NOT observed by the flap / intermittent windows
                # (their messages would be dropped while their state kept growing)
                row = dataclasses.replace(row, alerted_status=prev.alerted_status if prev else None,
                                          last_alert_at=prev.last_alert_at if prev else None,
                                          unhealthy_cycles=0 if row.unhealthy_since is not None else row.unhealthy_cycles)
                store.put_state(row)
                continue
            fo = flap.step(store, app, check, prev.status if prev else None, res.status, now, cfg.interval_seconds,
                           cfg.flap.flips, cfg.flap.cycles, cfg.flap.remind_hours, cfg.flap.clear_cycles,
                           cfg.intermittent.window_cycles, cfg.intermittent.min_failures,
                           alerted=row.alerted_status in ("warn", "crit"), int_remind_hours=cfg.intermittent.remind_hours)
            if fo.suppress and decision is not None and decision.kind in ("alert", "reminder", "recovered"):
                # a flapping check gets the one [FLAPPING] notice, not alert/OK pairs. [BLIND] is never swallowed. The
                # alert must not be remembered as sent when it is dropped here, or it could never be sent later.
                if decision.kind in ("alert", "reminder"):
                    row = dataclasses.replace(row, alerted_status=prev.alerted_status if prev else None,
                                              last_alert_at=prev.last_alert_at if prev else None)
                decision = None
            if fo.decision and fo.decision[0] == "sustained" and decision is not None and decision.kind in ("alert", "reminder"):
                decision = None   # the sustained-failure message is this cycle's alert; never send both
            if decision is not None:
                cr.decisions.append(decision)
            if fo.decision:
                kind, lvl, detail = fo.decision
                cr.decisions.append(AlertDecision(
                    kind, app, check, "crit" if lvl == "crit" else "warn", lvl, kind.upper(), detail, row.since,
                    row.consecutive, prev.alerted_status if prev else None, prev.last_alert_at if prev else None,
                    fo.undo))
                if kind == "sustained":   # the real alert now going out puts the check back under normal alerting
                    in_force = row.worst_status if row.worst_status in ("warn", "crit") else lvl
                    row = dataclasses.replace(row, alerted_status=in_force, last_alert_at=int(now))
            store.put_state(row)
        cr.worst = worst(r.status for r in cr.results.values())
        store.prune(now)
        store.finish_run(run_id, time.time(), True)
    except Exception as e:
        store.finish_run(run_id, time.time(), False, type(e).__name__)
        raise
    return cr


def rollback_alert_state(store: Store, d: AlertDecision) -> None:
    """No channel accepted the message: restore the pre-decision alert fields so the next cycle retries."""
    if d.kind in ("flapping", "stable", "intermittent", "sustained"):
        # the flap module stamped its own state: restore the exact previous values so the next cycle repeats it
        flap.undo(store, d.app, d.check, d.kind, d.flap_undo)
    row = store.get_state(d.app, d.check)
    if row is not None:
        extra = {}
        if d.kind == "recovered":   # the [OK] reached nobody: the episode is still open until it does
            extra = dict(recovered_at=None, ep_alerted=None, ep_last_alert=None, ok_notified=0)
        store.put_state(dataclasses.replace(row, alerted_status=d.prev_alerted_status,
                                            last_alert_at=d.prev_last_alert_at, **extra))

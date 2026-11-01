"""Flap and intermittent-failure detection, per check (reviews F8, F2, F3).

State lives in the `meta` table (one JSON blob per check), so it needs no schema of its own.

FLAPPING. Every cycle's raw status is observed (cycles that also made an alert decision included). A flip is a
healthy <-> unhealthy change. `flips` flips within `cycles` cycles declare the check flapping: ONE `flapping`
decision goes out at the worst level seen (crit if any dip was crit), the check's individual alert, reminder,
recovery and blind decisions are suppressed, and `flapping` repeats at `remind_hours`. When the check has been
healthy for `clear_cycles` cycles in a row one `stable` notice ends it.

INTERMITTENT. A check that fails once in a while but never long enough to confirm is counted by failure
EPISODES (a long outage is one) in a rolling window of max(24 h, window_cycles x interval); `min_failures` of them
raise ONE `intermittent` decision (crit if any failure was crit), repeating at the normal reminder interval while
the rate stays above the threshold and cleared when the window is clean. It stays quiet while the check is
flapping (that notice already covers it).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .store import Store

GOOD = ("ok", "info")
REMIND_S = {"crit": 6 * 3600, "warn": 24 * 3600}


@dataclass(frozen=True)
class Outcome:
    suppress: bool                       # drop this check's alert/reminder/recovered/blind decisions this cycle
    decision: tuple | None = None        # (kind, level, detail)


def _key(app: str, check: str) -> str:
    return f"fl:{app}:{check}"


def _load(store: Store, app: str, check: str) -> dict:
    try:
        st = json.loads(store.meta_get(_key(app, check)) or "{}")
    except ValueError:
        st = {}
    if not isinstance(st, dict):
        st = {}
    st.setdefault("flips", [])
    st.setdefault("fails", [])
    st.setdefault("flapping", False)
    st.setdefault("worst", "warn")
    st.setdefault("clear", 0)
    st.setdefault("last_alert", 0)
    st.setdefault("last_msg", 0)      # when a flapping-class message (flapping / stable) last went out
    st.setdefault("notified", False)  # a [FLAPPING] notice is outstanding, so a [STABLE] is owed
    st.setdefault("int_alert", 0)
    return st


def _save(store: Store, app: str, check: str, st: dict) -> None:
    store.meta_set(_key(app, check), json.dumps(st))


def step(store: Store, app: str, check: str, prev_status: str | None, status: str, now: float, interval_s: float,
         flips: int, cycles: int, remind_hours: float, clear_cycles: int,
         window_cycles: int, min_failures: int) -> Outcome:
    st = _load(store, app, check)
    window = cycles * interval_s
    hist = [e for e in st["flips"] if now - e[0] <= window]
    if prev_status is not None and (prev_status in GOOD) != (status in GOOD):
        hist.append([int(now), prev_status if status in GOOD else status])
    st["flips"] = hist
    decision = None
    remind_s = remind_hours * 3600

    # failure episodes for the intermittent rule (recorded always, evaluated only when not flapping)
    iwindow = max(86400.0, window_cycles * interval_s)
    fails = [e for e in st["fails"] if now - e[0] <= iwindow]
    if status in GOOD:
        st["run_counted"] = False
    elif status in ("warn", "crit") and not st.get("run_counted"):
        fails.append([int(now), status])      # one entry per run of bad cycles, however its statuses mix
        st["run_counted"] = True
    st["fails"] = fails

    if not st["flapping"]:
        if len(hist) >= flips:
            lvl = "crit" if any(e[1] == "crit" for e in hist) else "warn"
            # flapping again soon after a [STABLE] means "stable" was premature: require a longer quiet period
            # next time (doubling, at most 8x), so flap -> stable -> flap cannot ping-pong every hour
            recent = st["last_msg"] and now - st["last_msg"] < 86400
            need = min(st.get("clear_need", clear_cycles) * 2, clear_cycles * 8) if recent else clear_cycles
            st.update(flapping=True, worst=lvl, clear=0, last_alert=int(now), bad_run=0, clear_need=need,
                      notified=True, last_msg=int(now))
            decision = ("flapping", lvl, f"changed state {len(hist)} times in the last {cycles} cycles; individual "
                                         f"alerts for this check are paused until it stays ok for {need} cycles")
    else:
        escalated = status == "crit" and st["worst"] != "crit"
        if status == "crit":
            st["worst"] = "crit"
        st["clear"] = st["clear"] + 1 if status in GOOD else 0
        st["bad_run"] = 0 if status in GOOD else st.get("bad_run", 0) + 1
        if st["bad_run"] >= cycles and st["worst"] in ("warn", "crit"):
            # a whole flap window without a single flip: this is a sustained failure, not flapping. Hand the check
            # back to normal alerting, and say so with a real alert (the pair alerts were suppressed meanwhile).
            lvl = st["worst"] if status in ("warn", "crit") else "crit" if st["worst"] == "crit" else "warn"
            st.update(flapping=False, flips=[], clear=0, last_alert=0, bad_run=0)
            hist = []
            decision = ("sustained", lvl, f"failing for {cycles} cycles in a row after flapping: a sustained failure "
                                       f"(latest: {status})")
        elif st["clear"] >= st.get("clear_need", clear_cycles):
            if st["notified"]:
                decision = ("stable", st["worst"], f"stable again: ok for {st.get('clear_need', clear_cycles)} cycles in a row; "
                                                   "normal alerting resumed")
                st.update(notified=False, last_msg=int(now))
            st.update(flapping=False, flips=[], clear=0, last_alert=0, bad_run=0)
            hist = []
        elif escalated:   # the [FLAPPING] notice went out at warn; a crit dip must reach crit-only channels too
            st.update(last_alert=int(now), notified=True, last_msg=int(now))
            decision = ("flapping", "crit", "still flapping, and the dips now include a crit failure")
        elif now - st["last_alert"] >= remind_s:
            st["last_alert"] = int(now)
            st.update(notified=True, last_msg=int(now))
            decision = ("flapping", st["worst"], f"still flapping ({len(hist)} state changes in the last {cycles} cycles)")

    if decision is None and not st["flapping"]:
        n = len(fails)
        if n == 0:
            st["int_alert"] = 0
        elif n >= min_failures:
            lvl = "crit" if any(e[1] == "crit" for e in fails) else "warn"
            if not st["int_alert"] or now - st["int_alert"] >= REMIND_S[lvl]:
                st["int_alert"] = int(now)
                hours = iwindow / 3600
                decision = ("intermittent", lvl, f"{n} separate failures in the last {hours:.0f} h "
                                                 f"(last at {int(fails[-1][0])}); each was too short to confirm")
    _save(store, app, check, st)
    return Outcome(st["flapping"], decision)


def undo(store: Store, app: str, check: str, kind: str, clear_cycles: int) -> None:
    """No channel heard the decision: make the next cycle produce it again."""
    st = _load(store, app, check)
    if kind == "flapping":
        st.update(last_alert=0, last_msg=0, notified=False)
    elif kind == "intermittent":
        st["int_alert"] = 0
    elif kind == "sustained":   # the sustained-failure hand-over: stay in flapping until the alert gets through
        st.update(flapping=True, bad_run=10 ** 6)
    elif kind == "stable":
        st.update(flapping=True, clear=max(clear_cycles - 1, 0), notified=True)
    _save(store, app, check, st)

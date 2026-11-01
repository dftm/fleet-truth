"""Flap detection (review F8): a check that alternates healthy/unhealthy every cycle never reaches `confirm`
consecutive failures, so the normal flap guard stays silent forever. Count status flips per check in a
sliding window and raise one rate-limited `flapping` warn."""
from __future__ import annotations

import json

from .store import Store

GOOD = ("ok", "info")


def _key(app: str, check: str) -> str:
    return f"flips:{app}:{check}"


def observe(store: Store, app: str, check: str, prev_status: str | None, status: str, now: float,
            window_s: float, flips: int, remind_s: float) -> int | None:
    """Record a possible flip; return the flip count when a flapping alert is due, else None."""
    if prev_status is None:
        return None
    key = _key(app, check)
    try:
        hist = [t for t in json.loads(store.meta_get(key) or "[]") if now - t <= window_s]
    except ValueError:
        hist = []
    if prev_status != status and not (prev_status in GOOD and status in GOOD):  # ok<->info is not a flip
        hist.append(int(now))
    store.meta_set(key, json.dumps(hist))
    if len(hist) < flips:
        return None
    try:
        last = float(store.meta_get("flap_alerted:" + key) or 0)
    except ValueError:
        last = 0.0
    if last and now - last < remind_s:
        return None
    store.meta_set("flap_alerted:" + key, f"{now:.0f}")
    return len(hist)

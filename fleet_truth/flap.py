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
            window_s: float, flips: int, remind_s: float) -> tuple[int, str] | None:
    """Record a possible healthy<->unhealthy flip; return (flip count, level) when a flapping alert is due.
    The level is crit if any unhealthy side seen in the window was crit (never downgrade a crit)."""
    if prev_status is None:
        return None
    key = _key(app, check)
    try:
        hist = [e for e in json.loads(store.meta_get(key) or "[]") if isinstance(e, list) and now - e[0] <= window_s]
    except (ValueError, TypeError, IndexError):
        hist = []
    if (prev_status in GOOD) != (status in GOOD):
        hist.append([int(now), prev_status if status in GOOD else status])
    store.meta_set(key, json.dumps(hist))
    if len(hist) < flips:
        return None
    try:
        last = float(store.meta_get("flap_alerted:" + key) or 0)
    except ValueError:
        last = 0.0
    if last and now - last < remind_s:
        return None
    level = "crit" if any(e[1] == "crit" for e in hist) else "warn"
    store.meta_set("flap_alerted:" + key, f"{now:.0f}")
    return len(hist), level

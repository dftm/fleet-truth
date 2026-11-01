"""check_state transitions and the flap guard (design 4 "False-alarm policy").

advance() is pure: (previous row, new result, now, confirm) -> (new row, decision or None).

  -  warn/crit alerts only after `confirm` consecutive cycles with the SAME status;
  -  ok/info after an alert sends one recovery immediately; a blip that never alerted sends nothing;
  -  unknown alerts only after 30 minutes, worded as "monitor is blind", never as "app is down";
  -  a still-failing crit reminds every 6 h, warn every 24 h.
"""
from __future__ import annotations

from dataclasses import dataclass

from .checks.base import CheckResult
from .store import StateRow

BLIND_AFTER_S = 30 * 60
REMINDER_S = {"crit": 6 * 3600, "warn": 24 * 3600}
HEALTHY = ("ok", "info")


@dataclass(frozen=True)
class AlertDecision:
    kind: str                  # alert | reminder | recovered | blind
    app: str
    check: str
    status: str                # status now
    level: str                 # warn | crit: what a channel's min_status is compared against
    verdict: str
    detail: str
    since: int
    consecutive: int
    prev_alerted_status: str | None = None   # to roll back if no channel accepted the message
    prev_last_alert_at: int | None = None


def advance(prev: StateRow | None, app: str, check: str, r: CheckResult, now: float, confirm: int
            ) -> tuple[StateRow, AlertDecision | None]:
    now_i = int(now)
    same = prev is not None and prev.status == r.status
    since = prev.since if same else now_i
    consecutive = prev.consecutive + 1 if same else 1
    alerted = prev.alerted_status if prev else None
    last_at = prev.last_alert_at if prev else None
    silenced = prev.silenced_until if prev else None
    decision = None

    def mk(kind: str, level: str) -> AlertDecision:
        return AlertDecision(kind, app, check, r.status, level, r.verdict, r.detail, since, consecutive,
                             prev.alerted_status if prev else None, prev.last_alert_at if prev else None)

    if r.status in ("warn", "crit"):
        if consecutive >= confirm:
            if alerted != r.status:
                decision = mk("alert", r.status)
                alerted, last_at = r.status, now_i
            elif last_at is not None and now_i - last_at >= REMINDER_S[r.status]:
                decision = mk("reminder", r.status)
                last_at = now_i
    elif r.status == "unknown":
        if now_i - since >= BLIND_AFTER_S and alerted != "unknown":
            decision = mk("blind", "warn")
            alerted, last_at = "unknown", now_i
    else:  # healthy
        if alerted is not None:
            level = alerted if alerted in ("warn", "crit") else "warn"
            decision = mk("recovered", level)
            alerted, last_at = None, None
    row = StateRow(app, check, r.status, r.verdict, since, consecutive, alerted, last_at, silenced)
    return row, decision

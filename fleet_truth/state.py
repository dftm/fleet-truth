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


BAD_RANK = {"unknown": 1, "warn": 2, "crit": 3}


def advance(prev: StateRow | None, app: str, check: str, r: CheckResult, now: float, confirm: int,
            blind_level: str = "crit") -> tuple[StateRow, AlertDecision | None]:
    """An *episode* is a run of non-healthy results in any mix of crit, warn and unknown; only ok/info ends it.
    `confirm` counts episode cycles, the alert level is the WORST status of a failure kind seen in the episode
    (crit > warn), and unknown time counts toward the blind timer, so a check alternating crit and unknown
    cannot hide. A healthy result ends the episode at once (recovery alert if one was sent)."""
    now_i = int(now)
    same = prev is not None and prev.status == r.status
    since = prev.since if same else now_i
    consecutive = prev.consecutive + 1 if same else 1
    alerted = prev.alerted_status if prev else None
    last_at = prev.last_alert_at if prev else None
    silenced = prev.silenced_until if prev else None
    decision = None
    bad = r.status not in HEALTHY
    in_episode = bad and prev is not None and prev.status not in HEALTHY and prev.unhealthy_since is not None
    if bad:
        ep_since = prev.unhealthy_since if in_episode else now_i
        ep_cycles = (prev.unhealthy_cycles if in_episode else 0) + 1
        worst, worst_v = r.status, r.verdict
        if in_episode and prev.worst_status and BAD_RANK[prev.worst_status] >= BAD_RANK[r.status]:
            worst, worst_v = prev.worst_status, prev.worst_verdict
    else:
        ep_since, ep_cycles, worst, worst_v = None, 0, None, None

    def mk(kind: str, level: str, detail: str | None = None) -> AlertDecision:
        return AlertDecision(kind, app, check, r.status, level, worst_v if bad and worst != "unknown" else r.verdict,
                             detail if detail is not None else r.detail, ep_since if bad else since, ep_cycles if bad else consecutive,
                             prev.alerted_status if prev else None, prev.last_alert_at if prev else None)

    if bad:
        detail = r.detail
        if worst in ("warn", "crit") and r.status != worst:
            detail = f"{r.detail} (worst in this episode: {worst} {worst_v})"
        if worst in ("warn", "crit"):
            if ep_cycles >= confirm:
                if alerted != worst:
                    decision = mk("alert", worst, detail)
                    alerted, last_at = worst, now_i
                elif last_at is not None and now_i - last_at >= REMINDER_S[worst]:
                    decision = mk("reminder", worst, detail)
                    last_at = now_i
        elif now_i - ep_since >= BLIND_AFTER_S and alerted != "unknown":
            decision = mk("blind", blind_level)
            alerted, last_at = "unknown", now_i
    elif alerted is not None:
        level = alerted if alerted in ("warn", "crit") else "warn"
        decision = mk("recovered", level)
        alerted, last_at = None, None
    row = StateRow(app, check, r.status, r.verdict, since, consecutive, alerted, last_at, silenced,
                   ep_since, ep_cycles, worst, worst_v)
    return row, decision

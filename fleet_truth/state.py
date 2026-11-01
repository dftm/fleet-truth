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
            blind_level: str = "crit", recover_after: int = 2, reopen_s: float = 1800.0
            ) -> tuple[StateRow, AlertDecision | None]:
    """An *episode* is a run of non-healthy results in any mix of crit, warn and unknown. `confirm` counts
    episode cycles, the alert level is the worst failure kind seen (crit > warn), unknown time counts toward
    the blind timer.

    Hysteresis against alert storms: an episode ends (and a recovery is sent) only after `recover_after`
    consecutive healthy cycles; a failure that returns within `reopen_s` of a recovery continues the SAME
    episode (no new alert, no second [OK]), so crit,crit,ok repeating is one episode, not 96 pairs a day."""
    now_i = int(now)
    same = prev is not None and prev.status == r.status
    since = prev.since if same else now_i
    consecutive = prev.consecutive + 1 if same else 1
    alerted = prev.alerted_status if prev else None
    last_at = prev.last_alert_at if prev else None
    silenced = prev.silenced_until if prev else None
    ok_notified = prev.ok_notified if prev else 0
    decision = None
    bad = r.status not in HEALTHY
    has_ep = prev is not None and prev.unhealthy_since is not None
    p_open = has_ep and prev.recovered_at is None
    p_reopen = (has_ep and prev.recovered_at is not None and prev.ep_alerted is not None
                and now_i - prev.recovered_at <= reopen_s)   # only an episode that was alerted re-opens
    ep_since, ep_cycles = (prev.unhealthy_since, prev.unhealthy_cycles) if has_ep else (None, 0)
    worst, worst_v = (prev.worst_status, prev.worst_verdict) if has_ep else (None, None)
    ok_streak = 0
    recovered_at, ep_alerted, ep_last = (prev.recovered_at, prev.ep_alerted, prev.ep_last_alert) if prev else (None, None, None)

    def mk(kind: str, level: str, detail: str | None = None) -> AlertDecision:
        in_ep = bad or kind in ("alert", "reminder", "blind")
        return AlertDecision(kind, app, check, r.status if bad or kind == "recovered" else (worst or r.status), level,
                             worst_v if in_ep and worst != "unknown" else r.verdict,
                             detail if detail is not None else r.detail, ep_since if in_ep else since,
                             ep_cycles if in_ep else consecutive,
                             prev.alerted_status if prev else None, prev.last_alert_at if prev else None)

    pending = (not bad) and p_open and ((prev.ok_streak + 1) if prev.status in HEALTHY else 1) < recover_after
    if bad:
        if p_reopen:  # same episode continuing: restore what was in force, and remember the [OK] already went out
            alerted, last_at = prev.ep_alerted, prev.ep_last_alert
            ok_notified = 1 if prev.ep_alerted else 0
        elif not p_open:  # a genuinely new episode
            ep_since, ep_cycles, worst, worst_v, ok_notified = now_i, 0, None, None, 0
        recovered_at, ep_alerted, ep_last = None, None, None
        ep_cycles += 1
        if worst is None or BAD_RANK[r.status] > BAD_RANK[worst]:
            worst, worst_v = r.status, r.verdict
    if bad or pending:
        # (a pending cycle is an ok cycle inside an open episode: the alert logic still runs, so an alert whose
        # delivery failed is retried instead of waiting for the next failure)
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
    if not bad:
        ok_streak = (prev.ok_streak + 1) if prev is not None and prev.status in HEALTHY else 1
        if p_open and ok_streak >= recover_after:
            if alerted is not None and not ok_notified:
                level = alerted if alerted in ("warn", "crit") else "warn"
                decision = mk("recovered", level)
            recovered_at, ep_alerted, ep_last = now_i, alerted, last_at
            alerted, last_at, ok_notified = None, None, 0
        elif has_ep and prev.recovered_at is not None and now_i - prev.recovered_at > reopen_s:
            ep_since, ep_cycles, worst, worst_v = None, 0, None, None   # the re-open window passed: forget the episode
            recovered_at, ep_alerted, ep_last = None, None, None
    row = StateRow(app, check, r.status, r.verdict, since, consecutive, alerted, last_at, silenced,
                   ep_since, ep_cycles, worst, worst_v, ok_streak, recovered_at, ep_alerted, ep_last, ok_notified)
    return row, decision

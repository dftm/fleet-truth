"""A seeded random cycle simulator: 40 seeds x 300 cycles, 3 checks, a crit-only and a warn channel that fail 30-50% of
the time and sometimes hang. After every cycle it asserts the invariants a reviewer used by hand:

  [I1]  the dead-man is never told "all good" while a decision is undelivered or a confirmed crit episode has no
        alert in force (flapping excepted: the [FLAPPING] notice stands in for it);
  [I2]  no two delivered alerts of the same level within one episode;
  [I3]  reminders are never closer than their interval (unless the previous attempt was lost);
  [I4]  at most MAX_PER_DAY delivered messages per check in any 24 h;
  [I5]  every persistent crit stretch (>= 20 cycles) and every recurrent crit (3 starts in 48 cycles) eventually gets
        a delivered crit-level message.
"""
import random
import threading
import time

import pytest

from fleet_truth import alerts, config, cycle, flap, runtime
from fleet_truth.checks.base import CheckResult
from fleet_truth.state import REMINDER_S
from fleet_truth.store import Store
from tests.test_cycle import FakeRunner

CHECKS = ("HEALTHZ", "BACKUP_AGE", "HEARTBEAT")
OK, CRIT, UNK, WARN = (CheckResult("ok", "OK"), CheckResult("crit", "DOWN", "x"), CheckResult("unknown", "U", "u"),
                       CheckResult("warn", "W", "w"))
INTERVAL = 300
CYCLES = 300
SEEDS = range(40)
# Per check and 24 h. The storm patterns (192 messages a day before the fix) are bounded separately and tightly in
# test_flapping.py (<= 12); this random mix changes shape every ~20 cycles, so up to a dozen distinct episodes a day,
# each legitimately worth one or two messages (alert/recovery, flapping, intermittent, sustained).
MAX_PER_DAY = 20


def make_plan(rng: random.Random, n: int) -> list:
    """A status per cycle for one check, built from random segments of different failure shapes."""
    plan = []
    while len(plan) < n:
        kind = rng.choice(["ok", "ok", "blip", "persist", "persist_long", "alt_crit_ok", "crit_crit_ok", "crit_unk_ok",
                           "crit_warn_ok", "one_in_n", "unknown", "warn_run"])
        ln = rng.randint(8, 50)
        if kind == "ok":
            seg = [OK] * ln
        elif kind == "blip":
            seg = [OK if rng.random() > 0.08 else rng.choice([CRIT, WARN, UNK]) for _ in range(ln)]
        elif kind == "persist":
            seg = [CRIT] * rng.randint(3, 14)
        elif kind == "persist_long":
            seg = [CRIT] * rng.randint(20, 60)
        elif kind == "alt_crit_ok":
            seg = [CRIT, OK] * (ln // 2)
        elif kind == "crit_crit_ok":
            seg = [CRIT, CRIT, OK] * (ln // 3)
        elif kind == "crit_unk_ok":
            seg = [CRIT, UNK, OK] * (ln // 3)
        elif kind == "crit_warn_ok":
            seg = [CRIT, WARN, OK] * (ln // 3)
        elif kind == "one_in_n":
            k = rng.randint(5, 9)
            seg = ([CRIT] + [OK] * (k - 1)) * (ln // k + 1)
        elif kind == "unknown":
            seg = [UNK] * rng.randint(3, 14)
        else:
            seg = [WARN] * rng.randint(3, 30)
        plan += seg
    return plan[:n]


class Chan:
    """Fails with probability `p`; with probability `hang_p` takes longer than its budget."""

    def __init__(self, name, min_status, p, rng, hang_p=0.004):
        self.name, self.min_status, self.p, self.rng, self.hang_p = name, min_status, p, rng, hang_p
        self.log = []

    def send(self, msg):
        r = self.rng.random()
        if r < self.hang_p:
            time.sleep(0.15)
            return alerts.SendResult(True)
        return alerts.SendResult(r >= self.p + self.hang_p)


def simulate(seed: int):
    rng = random.Random(seed)
    plans = {c: make_plan(rng, CYCLES) for c in CHECKS}
    cfg = config.parse({"apps": [{"name": "app0", "health": {"url": "https://a.example.com/"}}], "confirm": 2,
                        "interval_seconds": INTERVAL})
    store = Store(":memory:")
    chans = [Chan("tg", "crit", rng.uniform(0.3, 0.5), random.Random(seed * 7 + 1)),
             Chan("ntfy", "warn", rng.uniform(0.3, 0.5), random.Random(seed * 7 + 2))]
    disp = alerts.Dispatcher(chans, store, "u", channel_budget_s=0.05, budget_s=1.0)
    step = {"i": 0}

    def fn(app, now):
        for c in CHECKS:
            yield c, plans[c][step["i"]]

    rn = FakeRunner(fn)
    delivered = {c: [] for c in CHECKS}       # (cycle, decision)
    attempts = {c: [] for c in CHECKS}        # (cycle, decision, delivered?)
    seen_levels = {}
    problems = []
    for i in range(CYCLES):
        step["i"] = i
        now = 1_000_000 + i * INTERVAL
        cr = cycle.run_cycle(cfg, store, rn, now=now, deadline_s=5)
        dels = disp.dispatch(cr.decisions)
        for d in dels:
            if d.undelivered:
                cycle.rollback_alert_state(store, d.decision)
            attempts[d.decision.check].append((i, d.decision, d.delivered > 0))
            if d.delivered > 0:
                delivered[d.decision.check].append((i, d.decision))
        lost = runtime.cycle_lost(dels)
        # [I1] un-alerted confirmed crit while telling the dead-man "all good"
        for c in CHECKS:
            st = store.get_state("app0", c)
            if st is None or st.unhealthy_since is None or st.recovered_at is not None:
                continue
            if st.worst_status == "crit" and st.unhealthy_cycles >= cfg.confirm and st.alerted_status != "crit":
                flapping = flap._load(store, "app0", c)["flapping"]
                if not lost and not flapping:
                    problems.append(f"I1 seed={seed} cycle={i} {c}: confirmed crit with no alert in force, dead-man OK")
        # [I2] same-level duplicates within one episode
        for d in dels:
            if d.delivered > 0 and d.decision.kind == "alert":
                st = store.get_state("app0", d.decision.check)
                key = (d.decision.check, st.unhealthy_since if st else None, d.decision.level)
                if key in seen_levels:
                    problems.append(f"I2 seed={seed} cycle={i} duplicate {key}")
                seen_levels[key] = i
    # [I3] reminders
    for c in CHECKS:
        last = None
        for i, d, ok in attempts[c]:
            if d.kind in ("alert", "reminder"):
                if d.kind == "reminder" and last is not None and last[1] and (i - last[0]) * INTERVAL < REMINDER_S[d.level]:
                    problems.append(f"I3 seed={seed} {c}: reminder after {(i - last[0]) * INTERVAL}s")
                last = (i, ok)
        # [I4] per-24h bound
        times = [i for i, _ in delivered[c]]
        for a in range(len(times)):
            n = sum(1 for t in times[a:] if (t - times[a]) * INTERVAL < 86400)
            if n > MAX_PER_DAY:
                problems.append(f"I4 seed={seed} {c}: {n} messages within 24 h")
                break
        # [I5] persistent and recurrent crit
        plan = plans[c]
        j = 0
        starts = []
        while j < CYCLES:
            if plan[j] is CRIT:
                k = j
                while k < CYCLES and plan[k] is CRIT:
                    k += 1
                starts.append(j)
                if k - j >= 20 and k + 12 < CYCLES:
                    if not any(j - 72 <= t <= k + 12 and d.level == "crit" for t, d in delivered[c]):
                        problems.append(f"I5 seed={seed} {c}: persistent crit {j}-{k} never delivered")
                j = k
            else:
                j += 1
        for a in range(len(starts) - 2):
            if starts[a + 2] - starts[a] <= 48 and starts[a + 2] + 30 < CYCLES:
                # somebody was told about crit within the last 6 h or soon after (reminders keep it fresh)
                if not any(starts[a + 2] - 72 <= t <= starts[a + 2] + 30 and d.level == "crit" for t, d in delivered[c]):
                    problems.append(f"I5 seed={seed} {c}: recurrent crit at {starts[a:a + 3]} never delivered")
                    break
    stats = {"messages": sum(len(v) for v in delivered.values())}
    return problems, stats


@pytest.mark.parametrize("block", range(4))
def test_random_cycles_hold_the_invariants(block):
    problems, total = [], 0
    for seed in list(SEEDS)[block * 10:(block + 1) * 10]:
        p, stats = simulate(seed)
        problems += p
        total += stats["messages"]
    assert not problems, "\n".join(problems[:15])
    assert total > 0


def test_a_slow_but_successful_channel_costs_no_duplicates_and_never_fails_the_dead_man():
    """Review 4 G7: a channel slower than its own budget but faster than the overall cap used to be re-sent every backoff
    window and the dead-man failed forever. Now a late success is a delivery: exactly one send per decision."""
    rng = random.Random(99)
    plans = {c: make_plan(rng, 120) for c in CHECKS}

    class Slow:
        name, min_status = "slow", "info"

        def __init__(self):
            self.sent = []

        def send(self, msg):
            time.sleep(0.06)
            self.sent.append(msg.text.splitlines()[0])
            return alerts.SendResult(True)

    cfg = config.parse({"apps": [{"name": "app0", "health": {"url": "https://a.example.com/"}}], "confirm": 2})
    store = Store(":memory:")
    slow = Slow()
    disp = alerts.Dispatcher([slow], store, "u", channel_budget_s=0.03, budget_s=1.0)
    step = {"i": 0}
    rn = FakeRunner(lambda app, now: ((c, plans[c][step["i"]]) for c in CHECKS))
    decisions = lost = 0
    for i in range(120):
        step["i"] = i
        cr = cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5)
        dels = disp.dispatch(cr.decisions)
        decisions += len(cr.decisions)
        lost += runtime.cycle_lost(dels)
        for d in dels:
            if d.undelivered:
                cycle.rollback_alert_state(store, d.decision)
    assert decisions > 5 and len(slow.sent) == decisions and lost == 0
    assert store.meta_get("bo:slow") is None

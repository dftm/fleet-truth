"""An independent HIDDEN-OUTAGE ORACLE (from the fourth review), 60 seeds x 600 cycles.

Whatever the history of a check, if it has been failing long enough that somebody should know, the owner's last
word on a crit-capable channel must not be "all good" while the dead-man says OK. Concretely, per check and cycle:

  [O1]  a run of >= HIDDEN_AFTER consecutive non-healthy cycles containing a crit, with the owner's belief "up"
        (last classified message was [OK]/[STABLE], or none) and a dead-man OK that cycle, is a hidden outage;
  [O2]  a 96-cycle window with >= 30% crit must have had a crit-level message in it or in the 6 h before;
  [O3]  nobody gets more than 30 messages per check per 24 h.
"""
import collections
import random
import time

import pytest

from fleet_truth import alerts, config, cycle, runtime
from fleet_truth.checks.base import CheckResult
from fleet_truth.store import Store

OK, CRIT, UNK, WARN = (CheckResult("ok", "OK"), CheckResult("crit", "DOWN", "x"), CheckResult("unknown", "U", "u"),
                       CheckResult("warn", "W", "w"))
SYM = {"o": OK, "c": CRIT, "u": UNK, "w": WARN}
HIDDEN_AFTER = 16
# A delivered [OK] is followed by a fresh [CRIT] when the failure returns (review 4: silence there hid an outage for
# 6 h), so chaotic histories with many short episodes cost a few more messages than before; the damping is the
# flap detector, and the tight storm bounds live in test_flapping.py.
MAX_PER_DAY = 30
CHECKS = ("HEALTHZ", "BACKUP_AGE")


def classify(text: str):
    head = text.split(":")[0]
    if head.startswith("[OK]") or head.startswith("[STABLE]"):
        return "up"
    if head.startswith("[CRIT") or head.startswith("[BLIND]") or ("[INTERMITTENT]" in head and "(crit)" in head):
        return "down"
    return None


def plan(rng: random.Random, n: int) -> list:
    out = []
    while len(out) < n:
        k = rng.choice(["ok", "ok", "persist", "long", "alt", "cco", "ccoo", "oneN", "unk", "warn", "mostly", "uoflap",
                        "reopen"])
        ln = rng.randint(8, 60)
        if k == "ok":
            s = "o" * ln
        elif k == "persist":
            s = "c" * rng.randint(3, 14)
        elif k == "long":
            s = "c" * rng.randint(20, 120)
        elif k == "alt":
            s = "co" * (ln // 2)
        elif k == "cco":
            s = "cco" * (ln // 3)
        elif k == "ccoo":
            s = "ccoo" * (ln // 4)
        elif k == "oneN":
            s = ("c" + "o" * rng.randint(4, 9)) * 4
        elif k == "reopen":     # the reviewer's pattern: alert, recovery, and the failure comes straight back
            s = "cc" + "oo" + "c" * rng.randint(10, 80)
        elif k == "unk":
            s = "u" * rng.randint(3, 40)
        elif k == "warn":
            s = "w" * rng.randint(3, 30)
        elif k == "mostly":
            s = ("c" * rng.randint(5, 11) + "o") * 4
        else:
            s = "uo" * 4 + "u" * rng.randint(10, 40)
        out += list(s)
    return out[:n]


class Chan:
    def __init__(self, name, min_status, fail):
        self.name, self.min_status, self.fail, self.i, self.got = name, min_status, fail, 0, []

    def send(self, msg):
        ok = not self.fail(self.i)
        if ok:
            self.got.append((self.i, msg.text.splitlines()[0]))
        return alerts.SendResult(ok, "" if ok else "boom")


class Runner:
    def __init__(self, plans):
        self.plans, self.i = plans, 0

    def run_app(self, app, now):
        for c, p in self.plans.items():
            yield c, SYM[p[self.i]]

    def reference_ok(self, now):
        return True


def simulate(seed, n=600):
    rng = random.Random(seed)
    plans = {c: plan(rng, n) for c in CHECKS}
    p = rng.uniform(0, 0.4)
    down_from, down_len = rng.randint(0, n), rng.choice([0, 0, 50, 300])
    tg = Chan("tg", "crit", lambda i: (down_from <= i < down_from + down_len) or rng.random() < p)
    nt = Chan("nt", "warn", lambda i: rng.random() < p)
    cfg = config.parse({"apps": [{"name": "app0", "health": {"url": "https://app0.example.com/"}}], "confirm": 2,
                        "interval_seconds": 300, "reference_urls": ["https://ref.example.org/"]})
    store = Store(":memory:")
    disp = alerts.Dispatcher([tg, nt], store, "u", channel_budget_s=cfg.channel_budget_seconds,
                             budget_s=cfg.alert_budget_seconds)
    rn = Runner(plans)
    deadman = []
    for i in range(n):
        rn.i = tg.i = nt.i = i
        cr = cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5)
        bad_apps = {a for (a, _c), r in cr.results.items() if r.status in ("crit", "unknown")}
        for st in store.all_states():
            if st.alerted_status in ("crit", "unknown"):
                bad_apps.add(st.app)
        dels = disp.dispatch(list(cr.decisions), not bad_apps, {"app0"} - bad_apps)
        for d in dels:
            if d.undelivered and d.decision.kind != "reset":
                cycle.rollback_alert_state(store, d.decision)
        deadman.append("fail" if runtime.cycle_lost(dels) else "ok" if disp.channels_healthy() else "withheld")
    problems = []
    for c in CHECKS:
        msgs = sorted(set((i, t) for i, t in tg.got + nt.got if f" app0 {c}" in t))
        by = collections.defaultdict(list)
        for i, t in msgs:
            by[i].append(t)
        belief, run_ = "up", 0
        for i in range(n):
            for t in by.get(i, []):
                cl = classify(t)
                if cl:
                    belief = cl
            s = plans[c][i]
            run_ = run_ + 1 if s != "o" else 0
            window_has_crit = "c" in plans[c][max(0, i - run_ + 1):i + 1]
            if run_ >= HIDDEN_AFTER and window_has_crit and belief == "up" and deadman[i] == "ok":
                problems.append(f"O1 seed={seed} {c} cycle={i}: failing for {run_} cycles, owner last told UP, dead-man OK")
                break
        crit_msgs = [i for i, t in msgs if classify(t) == "down"]
        for i in range(0, n - 96, 8):
            if plans[c][i:i + 96].count("c") >= 29 and not any(i - 72 <= j < i + 96 for j in crit_msgs):
                problems.append(f"O2 seed={seed} {c} window {i}: >=30% crit and no crit-level message")
                break
        ii = [i for i, _ in msgs]
        worst = max((sum(1 for j in ii if i <= j < i + 288) for i in range(n)), default=0)
        if worst > MAX_PER_DAY:
            problems.append(f"O3 seed={seed} {c}: {worst} messages within 24 h")
    return problems


@pytest.mark.parametrize("block", range(6))
def test_no_hidden_outage_in_random_histories(block):
    problems = []
    for seed in range(block * 10, block * 10 + 10):
        problems += simulate(seed)
    assert not problems, "\n".join(problems[:12])


def test_the_reviewers_probe_reopen_after_a_delivered_ok_is_a_new_alert():
    """oocc oo c...: [CRIT] at +15 min, [OK] at +25 min, down again from +30 min. Before the fix the next message came
    at +375 min and the dead-man said OK meanwhile."""
    cfg = config.parse({"apps": [{"name": "app0", "health": {"url": "https://app0.example.com/"}}], "confirm": 2,
                        "interval_seconds": 300})
    plan_ = list("oocc" + "oo") + ["c"] * 40
    tg = Chan("tg", "crit", lambda i: False)
    store = Store(":memory:")
    disp = alerts.Dispatcher([tg], store, "u")
    rn = Runner({"HEALTHZ": plan_})
    seen = []
    for i in range(len(plan_)):
        rn.i = tg.i = i
        cr = cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5)
        disp.dispatch(list(cr.decisions))
    heads = [(i, t.split(":")[0]) for i, t in tg.got]
    assert heads[:3] == [(3, "[CRIT] app0 HEALTHZ"), (5, "[OK] app0 HEALTHZ"), (7, "[CRIT] app0 HEALTHZ")], heads
    assert [h for _, h in heads].count("[CRIT] app0 HEALTHZ") >= 2


def test_an_undelivered_ok_keeps_the_episode_open_and_is_retried():
    cfg = config.parse({"apps": [{"name": "app0", "health": {"url": "https://app0.example.com/"}}], "confirm": 2,
                        "interval_seconds": 300})
    plan_ = list("cc" + "oo" + "ooo" + "c" * 6)
    down = {"v": False}
    tg = Chan("tg", "crit", lambda i: down["v"])
    store = Store(":memory:")
    disp = alerts.Dispatcher([tg], store, "u")
    rn = Runner({"HEALTHZ": plan_})
    for i in range(len(plan_)):
        rn.i = tg.i = i
        down["v"] = i in (3, 4)             # the first [OK] attempts fail
        cr = cycle.run_cycle(cfg, store, rn, now=1_000_000 + i * 300, deadline_s=5)
        for d in disp.dispatch(list(cr.decisions)):
            if d.undelivered:
                cycle.rollback_alert_state(store, d.decision)
    heads = [(i, t.split(":")[0]) for i, t in tg.got]
    assert heads[0] == (1, "[CRIT] app0 HEALTHZ") and heads[1] == (5, "[OK] app0 HEALTHZ"), heads   # retried until it got through
    assert heads[2][1] == "[CRIT] app0 HEALTHZ"

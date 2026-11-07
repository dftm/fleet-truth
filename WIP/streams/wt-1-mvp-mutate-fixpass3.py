import os, shutil, subprocess, sys, time, json
SRC = "/home/USER/worktrees/fleet-truth/wt-1-mvp"
DST = os.path.join(os.environ["TMPDIR"], "mut", "repo")
if os.path.exists(DST):
    shutil.rmtree(DST)
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))

ST=["tests/test_state.py"]; FL=["tests/test_flapping.py"]; DP=["tests/test_dispatch.py"]; SIM=["tests/test_simulator.py"]
CY=["tests/test_cycle.py"]; RS=["tests/test_resilience.py"]; STO=["tests/test_store.py"]; HC=["tests/test_http_checks.py"]
M = [
 ("recover_after ignored", "fleet_truth/state.py", "if p_open and ok_streak >= recover_after:", "if p_open:", ST),
 ("reopen never happens", "fleet_truth/state.py", "p_reopen = (has_ep and prev.recovered_at is not None and prev.ep_alerted is not None", "p_reopen = (False and prev.recovered_at is not None and prev.ep_alerted is not None", ST),
 ("second OK sent after reopen", "fleet_truth/state.py", "if alerted is not None and not ok_notified:", "if alerted is not None:", ST),
 ("unalerted blips reopen", "fleet_truth/state.py", "and prev.ep_alerted is not None\n", "\n", ST),
 ("no pending-cycle alert retry", "fleet_truth/state.py", "    if bad or pending:", "    if bad:", SIM),
 ("cooldown forgotten at once", "fleet_truth/state.py", "now_i - prev.recovered_at <= reopen_s)   # only", "now_i - prev.recovered_at <= -1)   # only", ST),
 ("flap not suppressing pairs", "fleet_truth/cycle.py", "            if fo.suppress:\n                decision = None", "            if False:\n                decision = None", FL),
 ("flap flips not counted every cycle", "fleet_truth/flap.py", "if prev_status is not None and (prev_status in GOOD) != (status in GOOD):", "if False:", FL),
 ("flapping level always warn", "fleet_truth/flap.py", 'lvl = "crit" if any(e[1] == "crit" for e in hist) else "warn"\n            # flapping again', 'lvl = "warn"\n            # flapping again', FL),
 ("stable never sent", "fleet_truth/flap.py", "            if st[\"notified\"]:", "            if False:", FL),
 ("sustained hand-over missing", "fleet_truth/flap.py", 'if st["bad_run"] >= cycles and st["worst"] in ("warn", "crit"):', "if False:", FL+SIM),
 ("crit escalation of flapping missing", "fleet_truth/flap.py", "        elif escalated:", "        elif False:", SIM),
 ("quiet period never grows", "fleet_truth/flap.py", "need = min(st.get(\"clear_need\", clear_cycles) * 2, clear_cycles * 8) if recent else clear_cycles", "need = clear_cycles", FL+SIM),
 ("intermittent counts cycles not runs", "fleet_truth/flap.py", 'elif status in ("warn", "crit") and not st.get("run_counted"):', 'elif status in ("warn", "crit"):', FL+CY),
 ("intermittent threshold ignored", "fleet_truth/flap.py", "elif n >= min_failures:", "elif n >= 1:", FL),
 ("intermittent level always warn", "fleet_truth/flap.py", 'lvl = "crit" if any(e[1] == "crit" for e in fails) else "warn"', 'lvl = "warn"', FL),
 ("intermittent window too short", "fleet_truth/flap.py", "iwindow = max(86400.0, window_cycles * interval_s)", "iwindow = window_cycles * interval_s / 10", FL),
 ("crit flapping head missing", "fleet_truth/alerts.py", "head = f\"[{'CRIT ' if d.level == 'crit' else ''}FLAPPING] {d.app} {d.check}\"", "head = f\"[FLAPPING] {d.app} {d.check}\"", FL),
 ("channels sent one after another", "fleet_truth/alerts.py", "            t.start()\n        for name, t in threads.items():", "            t.start(); t.join()\n        for name, t in threads.items():", DP),
 ("late results applied", "fleet_truth/alerts.py", "                    if ctx.closed:\n                        return          # late: discarded", "                    pass", DP),
 ("late state writes applied", "fleet_truth/alerts.py", "            if self.ctx.closed or self.store is None:", "            if self.store is None:", DP),
 ("no backoff after a hang", "fleet_truth/alerts.py", "        for name in bad_unknown | (hung & set(threads)):\n            self._bo_trip(name)", "        pass", DP),
 ("backoff never doubles", "fleet_truth/alerts.py", "level = self.backoff_cycles if not prev else min(prev * 2, self.backoff_max)", "level = self.backoff_cycles", DP),
 ("backoff never cleared", "fleet_truth/alerts.py", "                self._bo_set(name, 0, 0)       # a real delivery clears the backoff", "                pass", DP),
 ("skipped is recorded as a failure", "fleet_truth/alerts.py", "        if res.skipped:\n            return SendResult(res.ok, err, True)   # a deliberate", "        if False:\n            return SendResult(res.ok, err, True)   # a deliberate", DP),
 ("healthchecks reconcile missing", "fleet_truth/alerts.py", "            if hasattr(s, \"reconcile\"):", "            if False:", DP),
 ("alerted states ignored for clear flag", "fleet_truth/runtime.py", '        if st.alerted_status in ("crit", "unknown"):\n            bad_apps.add(st.app)', "        pass", DP),
 ("upgrade does not rebuild episodes", "fleet_truth/store.py", "WHERE unhealthy_since IS NULL AND status NOT IN ('ok','info');", "WHERE 0;", STO),
 ("tls.host covered by allow_loopback", "fleet_truth/checks/http.py", "app.health.allow_loopback and not host_port)", "app.health.allow_loopback)", HC),
 ("deadline unvalidated", "fleet_truth/config.py", "if room < MIN_CHECK_DEADLINE_S and not ctx.errors:", "if False:", RS),
 ("effective deadline ignores the alert budget", "fleet_truth/config.py", "return min(0.8 * self.interval_seconds, 0.9 * self.interval_seconds - self.alert_budget_seconds - DEAD_MAN_PING_S)", "return 0.8 * self.interval_seconds", RS),
 ("stale run never reported", "fleet_truth/runtime.py", "    if time.time() - started <= 2 * cfg.interval_seconds:\n        return", "    return", RS),
 ("test-alert ignores the run lock", "fleet_truth/runtime.py", "    with run_lock(cfg):\n        store = None", "    if True:\n        store = None", RS),
 ("test-alert moves a bad DB aside", "fleet_truth/runtime.py", "            store = Store(db_path(cfg))   # test sends", "            store = open_store(cfg)   # test sends", RS),
]
M = [m if len(m)==5 else (m[0],m[1],m[2],None,m[3]) for m in M]
only = sys.argv[1:]
res = []
for i, (name, f, old, new, tests) in enumerate(M):
    if only and str(i) not in only:
        continue
    p = os.path.join(DST, f)
    orig = open(p).read()
    pairs = old if isinstance(old, list) else [(old, new)]
    mutated = orig
    bad = False
    for o, n in pairs:
        if o not in mutated:
            bad = True; break
        mutated = mutated.replace(o, n, 1)
    if bad:
        res.append((i, name, "PATTERN-NOT-FOUND")); print(i, name, "PATTERN-NOT-FOUND", flush=True); continue
    open(p, "w").write(mutated)
    t0 = time.time()
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *tests], cwd=DST,
                           capture_output=True, text=True, timeout=45)
        out = "KILLED" if r.returncode != 0 else "SURVIVED"
        if r.returncode not in (0, 1):
            out = f"ERROR rc={r.returncode}"
    except subprocess.TimeoutExpired:
        out = "KILLED (hang/timeout)"
    open(p, "w").write(orig)
    res.append((i, name, out)); print(i, name, out, f"{time.time()-t0:.1f}s", flush=True)
json.dump(res, open(os.path.join(os.environ["TMPDIR"], "mut_results.json"), "w"))

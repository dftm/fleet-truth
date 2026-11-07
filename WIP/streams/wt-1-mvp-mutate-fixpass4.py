import os, shutil, subprocess, sys, time, json
SRC = "/home/USER/worktrees/fleet-truth/wt-1-mvp"
DST = os.path.join(os.environ["TMPDIR"], "mut", "repo")
if os.path.exists(DST):
    shutil.rmtree(DST)
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))

R4=["tests/test_review4.py"]; ORC=["tests/test_oracle.py","-k","reviewers or undelivered_ok"]; ORCALL=["tests/test_oracle.py"]
DP=["tests/test_dispatch.py"]; ST=["tests/test_state.py"]; CF=["tests/test_review4.py","-k","config or infinity"]
M = [
 ("undelivered [OK] not re-opened by rollback", "fleet_truth/cycle.py", "extra = dict(recovered_at=None, ep_alerted=None, ep_last_alert=None, ok_notified=0)", "extra = {}", ORC),
 ("delivered [OK] continues the old episode", "fleet_truth/state.py", "        if not p_open:  # a genuinely new episode", "        if not (p_open or (has_ep and prev.recovered_at is not None and now_i - prev.recovered_at <= reopen_s)):  # a genuinely new episode", ST),
 ("dropped alert remembered as sent", "fleet_truth/cycle.py", "                if decision.kind in (\"alert\", \"reminder\"):\n                    row = dataclasses.replace(row, alerted_status=prev.alerted_status if prev else None,\n                                              last_alert_at=prev.last_alert_at if prev else None)", "                if False:\n                    pass", R4),
 ("blind swallowed by flap suppression", "fleet_truth/cycle.py", 'decision.kind in ("alert", "reminder", "recovered"):\n                # a flapping', 'decision.kind in ("alert", "reminder", "recovered", "blind"):\n                # a flapping', R4),
 ("sustained hand-back at warn", "fleet_truth/flap.py", "            lvl = st[\"worst\"]\n            st.update(flapping=False, flips=[], clear=0, last_alert=0, bad_run=0)", "            lvl = \"warn\"\n            st.update(flapping=False, flips=[], clear=0, last_alert=0, bad_run=0)", R4),
 ("stable pings /fail by level", "fleet_truth/alerts.py", 'if msg.kind in ("recovered", "test", "stable"):', 'if msg.kind in ("recovered", "test"):', R4),
 ("stable withheld check removed", "fleet_truth/alerts.py", 'if msg.kind in ("recovered", "stable") and not clear:', 'if msg.kind == "recovered" and not clear:', R4),
 ("ntfy stable at priority 5", "fleet_truth/alerts.py", 'msg.kind not in ("recovered", "stable") else "3"', 'msg.kind != "recovered" else "3"', R4),
 ("flap episode stays in intermittent window", "fleet_truth/flap.py", [("notified=True, last_msg=int(now), fails=[], run_entry=None)", "notified=True, last_msg=int(now))"), ("st.update(flapping=False, flips=[], clear=0, last_alert=0, bad_run=0, fails=[], run_entry=None)", "st.update(flapping=False, flips=[], clear=0, last_alert=0, bad_run=0)")], R4),
 ("alerted outage counted as intermittent", "fleet_truth/flap.py", "    elif alerted:\n        if st.get(\"run_entry\") is not None:", "    elif False:\n        if st.get(\"run_entry\") is not None:", R4),
 ("intermittent not rate limited", "fleet_truth/flap.py", "now - st[\"int_alert\"] >= int_remind_hours * 3600 and newest", "newest", R4),
 ("intermittent repeats without a new failure", "fleet_truth/flap.py", " and newest > st.get(\"int_seen\", 0))", ")", R4),
 ("intermittent counts alerted outages", "fleet_truth/flap.py", [("    elif alerted:\n        if st.get(\"run_entry\") is not None:", "    elif False:\n        if st.get(\"run_entry\") is not None:"), ("if decision is None and not st[\"flapping\"] and not alerted:", "if decision is None and not st[\"flapping\"]:")], R4),
 ("flapping reminder when no state changes", "fleet_truth/flap.py", "        elif hist and now - st[\"last_alert\"] >= remind_s:", "        elif now - st[\"last_alert\"] >= remind_s:", R4),
 ("undo restores nothing", "fleet_truth/flap.py", "    if before:\n        st.update(before)", "    if False:\n        st.update(before)", R4+["tests/test_flapping.py"]),
 ("suppressed cycles are observed", "fleet_truth/cycle.py", "                flap.mark_unobserved(store, app, check)\n                continue", "                pass", R4),
 ("unobserved neighbour counts as a flip", "fleet_truth/flap.py", "    if st.pop(\"unobserved\", False):", "    if st.pop(\"unobserved\", False) and False:", R4),
 ("config accepts NaN/inf", "fleet_truth/config.py", "    if isinstance(v, float) and not math.isfinite(v):", "    if False:", CF),
 ("flips may exceed cycles", "fleet_truth/config.py", "    if flap.flips > flap.cycles:", "    if False:", CF),
 ("recover_after unbounded", "fleet_truth/config.py", '"recover_after", ctx, 2, 1, True, 20)', '"recover_after", ctx, 2, 1, True)', CF),
 ("late success not awaited", "fleet_truth/alerts.py", "            if unheard:\n                grace_end", "            if False:\n                grace_end", DP),
 ("slow reconcile trips backoff", "fleet_truth/alerts.py", "        for name in bad_unknown:      # only", "        for name in bad_unknown | (hung & set(threads)):      # only", DP),
 ("SystemExit in a sender is lost", "fleet_truth/alerts.py", "                except BaseException as e:  # noqa: BLE001 - a broken sender (even SystemExit)", "                except Exception as e:  # noqa: BLE001 - a broken sender (even SystemExit)", DP),
 ("sustained does not replace the alert", "fleet_truth/cycle.py", "                decision = None   # the sustained-failure message is this cycle's alert; never send both", "                pass", ["tests/test_simulator.py"]),
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

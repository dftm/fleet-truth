import os, shutil, subprocess, sys, time, json
SRC = "/home/dftms/worktrees/fleet-truth/wt-1-mvp"
DST = os.path.join(os.environ["TMPDIR"], "mut", "repo")
if os.path.exists(DST):
    shutil.rmtree(DST)
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))

ST=["tests/test_state.py"]; CY=["tests/test_cycle.py"]; AL=["tests/test_alerts.py"]; RS=["tests/test_resilience.py"]; FC=["tests/test_file_checks.py"]
NET = ["tests/test_net.py", "-k", "not default_timeout"]
M = [
 ("R1 episode never forms", "fleet_truth/state.py", "in_episode = bad and prev is not None and prev.status not in HEALTHY and prev.unhealthy_since is not None", "in_episode = False", ST+AL),
 ("R1 confirm counts same-status run", "fleet_truth/state.py", "if ep_cycles >= confirm:", "if consecutive >= confirm:", ST),
 ("R1 blind timer restarts on status change", "fleet_truth/state.py", "now_i - ep_since >= BLIND_AFTER_S", "now_i - since >= BLIND_AFTER_S", ST),
 ("R1 worst not tracked", "fleet_truth/state.py", "if in_episode and prev.worst_status and BAD_RANK[prev.worst_status] >= BAD_RANK[r.status]:", "if False:", ST),
 ("R1 flapping level hard-coded warn", "fleet_truth/flap.py", 'level = "crit" if any(e[1] == "crit" for e in hist) else "warn"', 'level = "warn"', CY),
 ("R2 warn mapped to /fail", "fleet_truth/alerts.py", 'target = url + "/log"', 'target = url + "/fail"', AL),
 ("R2 crit not mapped to /fail", "fleet_truth/alerts.py", 'target = url + "/fail"       # crit alert', 'target = url + "/log"       # crit alert', AL),
 ("R2 recovered pings while others down", "fleet_truth/alerts.py", 'if msg.kind == "recovered" and not clear:', "if False:", AL),
 ("R2 skipped counted as delivered", "fleet_truth/alerts.py", "if r.ok and not r.skipped)", "if r.ok)", AL),
 ("R2 skip treated as failure", "fleet_truth/alerts.py", "if self.skipped and self.skipped == self.eligible:", "if False:", AL),
 ("R2 per-app url ignored", "fleet_truth/alerts.py", "per_app = dict(self.cfg.app_url_envs).get(msg.app)", "per_app = None", AL),
 ("R3 suppression uncapped and ignores delivery", "fleet_truth/cycle.py", "if delivered or streak > cfg.offline_suppress_cycles:", "if False:", AL),
 ("R3 delivery not recorded", "fleet_truth/runtime.py", 'store.meta_set("offline_alert_delivered", "1")', "pass", AL),
 ("R3 reference may share the fleet domain", "fleet_truth/config.py", "if any(_site(rh) == _site(h) for h in app_hosts):", "if False:", AL),
 ("R3 cap never restarts", "fleet_truth/cycle.py", 'meta_set("offline_streak", "0")        # the episode', 'pass        # the episode', AL),
 ("R4 any OSError is 'run in progress'", "fleet_truth/runtime.py", "except BlockingIOError:", "except OSError:", RS),
 ("R5 flapping rate limit not undone", "fleet_truth/cycle.py", 'store.meta_set(f"flap_alerted:flips:{d.app}:{d.check}", "0")', "pass", AL),
 ("R5 rollback keeps last_alert_at", "fleet_truth/cycle.py", "last_alert_at=d.prev_last_alert_at))", "last_alert_at=row.last_alert_at))", AL),
 ("R6 fresh ok status exempt from LOCK_STUCK", "fleet_truth/checks/files.py", 'if verdict not in ("OK", "NEVER_RAN", "FAILED", "STALE"):', 'if verdict not in ("NEVER_RAN", "FAILED", "STALE"):', FC),
 ("R6 marker inside the data dir", "fleet_truth/runtime.py", "base = Path(config_path).expanduser().resolve().parent if config_path else d.parent", "base = d", RS),
 ("R7 inode not matched", "fleet_truth/checks/files.py", "if int(ino) == want_ino and", "if True and", FC),
 ("R7 device not matched", "fleet_truth/checks/files.py", "(int(maj, 16), int(mnr, 16)) == want_dev", "True", FC),
 ("R7 no status fallback", "fleet_truth/checks/files.py", 'return bool(status and status.get("running") is True)', "return False", FC),
 ("R7 probe takes the lock again", "fleet_truth/checks/files.py", "    want_ino = st.st_ino\n", "    want_ino = st.st_ino\n    import fcntl as _f\n    _fd = os.open(p, os.O_RDONLY); _f.flock(_fd, _f.LOCK_SH | _f.LOCK_NB); _f.flock(_fd, _f.LOCK_UN); os.close(_fd)\n", FC),
 ("R8 newer schema treated as corruption", "fleet_truth/runtime.py", "    except SchemaTooNew:\n        raise  # a rollback", "    except ZeroDivisionError:\n        raise  # a rollback", RS),
 ("R8 WAL pragma before the version check", "fleet_truth/store.py", "self._check_not_newer()   # before", "pass   # before", RS),
 ("R8 corrupt copies uncapped", "fleet_truth/runtime.py", "    if len(copies) <= MAX_CORRUPT_COPIES:\n        return", "    return", RS),
 ("R8 first corrupt copy not kept", "fleet_truth/runtime.py", "keep = {copies[0], *copies[-(MAX_CORRUPT_COPIES - 1):]}", "keep = {*copies[-MAX_CORRUPT_COPIES:]}", RS),
 ("R9 send join unbounded", "fleet_truth/alerts.py", "t.join(left)", "t.join()", AL),
 ("R9 budget exhaustion ignored", "fleet_truth/alerts.py", "        if left <= 0:\n            return SendResult(False, \"alert time budget exhausted", "        if False:\n            return SendResult(False, \"alert time budget exhausted", AL),
 ("R10 flag admits any host", "fleet_truth/net.py", "if allow_loopback and is_loopback_literal(target):", "if allow_loopback:", NET),
 ("R10 names count as loopback literals", "fleet_truth/net.py", "    if ip is None:\n        return False\n    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped", "    if ip is None:\n        return True\n    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped", NET),
 ("R10 plain http not allowed with the flag", "fleet_truth/net.py", "not (allow_private or allow_loopback):\n        raise FetchError(\"BAD_URL\", \"plain http", "not allow_private:\n        raise FetchError(\"BAD_URL\", \"plain http", NET),
 ("R10 config accepts any host with the flag", "fleet_truth/config.py", 'if allow_loopback and (host or "").lower() != "localhost":', "if False:", NET),
 ("R10 mapped loopback accepted as literal", "fleet_truth/net.py", "if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:\n        return False  # a mapped", "if False:\n        return False  # a mapped", NET),
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

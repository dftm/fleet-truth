import os, shutil, subprocess, sys, time, json
SRC = "/home/USER/worktrees/fleet-truth/wt-1-mvp"
DST = os.path.join(os.environ["TMPDIR"], "mut", "repo")
if os.path.exists(DST):
    shutil.rmtree(DST)
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))

NET = ["tests/test_net.py", "-k", "not default_timeout"]
AL = ["tests/test_alerts.py"]
FC = ["tests/test_file_checks.py"]
CY = ["tests/test_cycle.py"]
M = [
 ("smtp: no explicit timeout", "fleet_truth/alerts.py", "super().__init__(timeout=timeout)", "super().__init__()", AL),
 ("smtp: TLS hostname not set", "fleet_truth/alerts.py", "self._host = host  # starttls", "pass  # starttls", AL),
 ("smtp: greeting not checked", "fleet_truth/alerts.py", "if code != 220:", "if False:", AL),
 ("smtp: STARTTLS skipped", "fleet_truth/alerts.py", "smtp.starttls(context=self.ctx())  # no login or message before TLS is up", "pass", AL),
 ("telegram: wrong API path", "fleet_truth/alerts.py", 'f"{TELEGRAM_API}/bot{tok}/sendMessage"', 'f"{TELEGRAM_API}/{tok}/sendMessage"', AL),
 ("ntfy: auth header dropped", "fleet_truth/alerts.py", 'headers["Authorization"] = f"Bearer {token}"', "pass", AL),
 ("webhook healthchecks: /fail dropped", "fleet_truth/alerts.py", 'target = url + "/fail"', "target = url", AL),
 ("files: RUNNING masks forever", "fleet_truth/checks/files.py", "if held_h > grace:", "if False:", FC),
 ("files: lock-held-since never recorded", "fleet_truth/checks/files.py", '                meta_set(key, f"{now:.0f}")\n        held_h', "                pass\n        held_h", FC),
 ("files: NaN/inf accepted", "fleet_truth/checks/files.py", 'if v != v or v in (float("inf"), float("-inf")):', "if False:", FC),
 ("files: future epoch accepted", "fleet_truth/checks/files.py", "if v > now + FUTURE_SLACK_S:", "if False:", FC),
 ("files: millisecond epoch accepted", "fleet_truth/checks/files.py", "if v >= 1e11:", "if False:", FC),
 ("cycle: reference answer ignored", "fleet_truth/cycle.py", "if ref is True:", "if False:", CY),
 ("cycle: failing reference does not suppress", "fleet_truth/cycle.py", "                offline = True\n                extra[(MONITOR_APP, NETWORK_CHECK)] = CheckResult(\n                    \"crit\", \"MONITOR_OFFLINE\"", "                extra[(MONITOR_APP, NETWORK_CHECK)] = CheckResult(\n                    \"crit\", \"MONITOR_OFFLINE\"", CY),
 ("cycle: no-reference case suppresses", "fleet_truth/cycle.py", "            else:\n                extra[(MONITOR_APP, NETWORK_CHECK)] = CheckResult(\n                    \"crit\", \"ALL_APPS_FAILING\"", "            else:\n                offline = True\n                extra[(MONITOR_APP, NETWORK_CHECK)] = CheckResult(\n                    \"crit\", \"ALL_APPS_FAILING\"", CY),
 ("state: blind level hard-coded warn", "fleet_truth/state.py", 'decision = mk("blind", blind_level)', 'decision = mk("blind", "warn")', AL + ["tests/test_state.py"]),
 ("alerts: unreachable blind not undelivered", "fleet_truth/alerts.py", ' or self.decision.kind == "blind"', "", AL),
 ("alerts: no-channel decision not undelivered", "fleet_truth/alerts.py", "self.eligible > 0 or self.configured == 0", "self.eligible > 0", AL),
 ("runtime: undelivered still pings OK", "fleet_truth/runtime.py", "cycle_ok=not lost,", "cycle_ok=True,", AL),
 ("runtime: corrupt DB not moved aside", "fleet_truth/runtime.py", "moved = _move_aside(path)", "moved = path", ["tests/test_resilience.py"]),
 ("runtime: reset alert not sent", "fleet_truth/runtime.py", "if reset_note:  # also the conservative", "if False:  # also the conservative", ["tests/test_resilience.py"]),
 ("runtime: no /fail ping on reset", "fleet_truth/runtime.py", 'error_class="StateReset"', 'error_class="StateReset" if False else None', ["tests/test_resilience.py"]),
 ("runtime: run lock disabled", "fleet_truth/runtime.py", "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)", "pass", ["tests/test_resilience.py"]),
 ("runtime: no /fail when data dir unusable", "fleet_truth/runtime.py", "    except Exception as e:  # cannot even create the lock/data dir\n        alerts.dead_man_ping(", "    except Exception as e:  # cannot even create the lock/data dir\n        (lambda *a, **k: None)(", ["tests/test_resilience.py"]),
 ("runtime: wiped DB not detected", "fleet_truth/runtime.py", "if not existed and marker.exists():", "if False:", ["tests/test_resilience.py"]),
 ("flap: threshold ignored", "fleet_truth/flap.py", "if len(hist) < flips:", "if False:", CY),
 ("flap: no rate limit", "fleet_truth/flap.py", "if last and now - last < remind_s:", "if False:", CY),
 ("flap: ok<->info counted", "fleet_truth/flap.py", "and not (prev_status in GOOD and status in GOOD)", "", CY),
 ("yaml: anchors allowed", "fleet_truth/config.py", "if isinstance(tok, (yaml.AnchorToken, yaml.AliasToken)):", "if False:", ["tests/test_config.py"]),
 ("yaml: duplicate keys allowed", "fleet_truth/config.py", "if isinstance(key, (str, int, float, bool)) and key in seen:", "if False:", ["tests/test_config.py"]),
 ("yaml: size cap off", "fleet_truth/config.py", "if len(text.encode(\"utf-8\", \"replace\")) > MAX_FILE_BYTES:", "if False:", ["tests/test_config.py"]),
 ("yaml: depth cap off", "fleet_truth/config.py", "if depth > MAX_DEPTH:", "if False:", ["tests/test_config.py"]),
 ("config: port 0 accepted", "fleet_truth/config.py", "if port is not None and not 1 <= port <= 65535:", "if False:", ["tests/test_net.py", "-k", "config_load"]),
 ("net: metadata hostnames allowed", "fleet_truth/net.py", 'if any(bare == m or bare.endswith("." + m) for m in _METADATA_HOSTS):', "if False:", NET),
 ("net: port 0 means 443", "fleet_truth/net.py", "    if port is not None and not 1 <= port <= 65535:\n        raise FetchError(\"BAD_URL\", \"port out of range\")", "", NET),
 ("net: DNS outside the time limit", "fleet_truth/net.py", "t.join(max(timeout_s, 0.05))\n    if t.is_alive():", "t.join()\n    if False:", NET),
 ("net: metadata IPs allowed under allow_private", "fleet_truth/net.py", "if ip in _METADATA_IPS:", "if False:", NET),
 ("net: special ranges allowed", "fleet_truth/net.py", "if any(ip.version == n.version and ip in n for n in _ALWAYS_BLOCKED):", "if False:", NET),
 ("net: ULA not admitted", "fleet_truth/net.py", "if allow_private and isinstance(ip, ipaddress.IPv6Address) and ip in _ULA:", "if False:", NET),
 ("net: embedded IPv4 unwrap removed (stdlib neutralised)", "fleet_truth/net.py", "for v4 in _embedded_v4(ip):", "for v4 in []:", NET),
 ("net: explicit link-local nets removed (stdlib neutralised)", "fleet_truth/net.py", "if ip.is_link_local or _in(ip, _LINK_LOCAL_NETS):", "if ip.is_link_local:", NET),
 ("net: explicit loopback nets removed", "fleet_truth/net.py", "if ip.is_loopback or _in(ip, _LOOPBACK_NETS):", "if ip.is_loopback:", NET),
 ("net: link-local branch removed entirely", "fleet_truth/net.py", 'if ip.is_link_local or _in(ip, _LINK_LOCAL_NETS):\n        return "link-local address (includes cloud metadata)"', "if False:\n        return None", NET),
 ("net: declared-size check removed (layer alone)", "fleet_truth/net.py", 'if cl.isdigit() and int(cl) > max_bytes and method != "HEAD":', "if False:", NET),
 ("git: --no-optional-locks flag removed", "fleet_truth/gitmirror.py", '"-C", str(clone), "--no-optional-locks", "status"', '"-C", str(clone), "status"', ["tests/test_drift.py", "-k", "optional_lock"]),
 ("git: GIT_OPTIONAL_LOCKS env removed", "fleet_truth/gitmirror.py", '"GIT_OPTIONAL_LOCKS": "0", ', "", ["tests/test_drift.py", "-k", "optional_lock"]),
 ("runtime: cycle error does not ping /fail", "fleet_truth/runtime.py", "        alerts.dead_man_ping(cfg, cycle_ok=False, error_class=type(e).__name__, channels_healthy=disp.channels_healthy(),", "        (lambda *a, **k: None)(cfg, cycle_ok=False, error_class=type(e).__name__, channels_healthy=disp.channels_healthy(),", AL),
 ("runtime: undelivered alert not rolled back (no retry)", "fleet_truth/runtime.py", 'if d.undelivered and d.decision.kind != "reset":', "if False:", AL),
 ("alerts: _fail layer unscrubbed", "fleet_truth/alerts.py", 'return SendResult(False, clean(f"{e.kind}: {e.message}")[:200])', 'return SendResult(False, f"{e.kind}: {e.message}")', AL),
 ("alerts: dispatcher layer unscrubbed", "fleet_truth/alerts.py", "err = clean(res.error)", "err = res.error", AL),
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

import os, shutil, subprocess, sys, time, json
SRC = "/home/dftms/worktrees/fleet-truth/wt-1-mvp"
DST = os.path.join(os.environ["TMPDIR"], "mut", "repo")
if os.path.exists(DST):
    shutil.rmtree(DST)
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache"))

NET = ["tests/test_net.py", "-k", "not default_timeout"]
HTTP = ["tests/test_http_checks.py"]
M = [
 ("ssrf: loopback allowed always", "fleet_truth/net.py", 'return None if allow_loopback else "loopback address"', "return None", NET),
 ("ssrf: link-local allowed", "fleet_truth/net.py", 'if ip.is_link_local:\n        return "link-local address (includes cloud metadata)"', 'if False:\n        return "x"', NET),
 ("ssrf: legacy numeric hosts accepted", "fleet_truth/net.py", "def _legacy_numeric(host: str) -> bool:\n", "def _legacy_numeric(host: str) -> bool:\n    return False\n", NET),
 ("ssrf: only first resolved IP validated", "fleet_truth/net.py", "    for ip in ips:\n        reason = blocked_reason", "    for ip in ips[:1]:\n        reason = blocked_reason", NET),
 ("ssrf: private allowed without allow_private", "fleet_truth/net.py", "if allow_private and isinstance(ip, ipaddress.IPv4Address) and any(", "if isinstance(ip, ipaddress.IPv4Address) and any(", NET),
 ("ssrf: IPv4-mapped not unwrapped", "fleet_truth/net.py", "for v4 in _embedded_v4(ip):", "for v4 in []:", NET),
 ("pin: connect by hostname not validated IP", "fleet_truth/net.py", "sock.connect((ip, target.port))", "sock.connect((target.host, target.port))", NET),
 ("pin: SNI is the IP not the hostname", "fleet_truth/net.py", "server_hostname=target.host)", "server_hostname=ip)", NET),
 ("pin: userinfo allowed", "fleet_truth/net.py", 'if "@" in s.netloc or s.username is not None or s.password is not None:', "if False:", NET),
 ("size cap removed (streamed)", "fleet_truth/net.py", "                    if total > max_bytes:", "                    if False:", NET),
 ("size cap removed (declared)", "fleet_truth/net.py", 'if cl.isdigit() and int(cl) > max_bytes and method != "HEAD":', "if False:", NET),
 ("time cap: watchdog 100x longer", "fleet_truth/net.py", "threading.Timer(timeout_s, self._fire)", "threading.Timer(timeout_s * 100, self._fire)", NET),
 ("error message includes URL path", "fleet_truth/net.py", 'f"response from {target.host} exceeds {max_bytes} bytes")\n                    chunks.append', 'f"response from {target.host}{target.path} exceeds {max_bytes} bytes")\n                    chunks.append', NET),
 ("access headers sent on http", "fleet_truth/checks/http.py", 'or not v.url.startswith("https://"):', "or False:", HTTP),
 ("access headers sent to non-access apps", "fleet_truth/checks/http.py", 'if v is None or v.auth != "access"', "if v is None", HTTP),
 ("access redirect not detected", "fleet_truth/checks/http.py", "    if resp.status not in REDIRECTS:\n        return False", "    return False", HTTP),
 ("result detail not scrubbed", "fleet_truth/checks/base.py", 'object.__setattr__(self, "detail", secrets.scrub(self.detail)[:MAX_DETAIL])', 'object.__setattr__(self, "detail", self.detail[:MAX_DETAIL])', HTTP),
 ("result data not scrubbed", "fleet_truth/checks/base.py", 'object.__setattr__(self, "data", _clean(self.data))', 'object.__setattr__(self, "data", self.data)', HTTP),
 ("retry removed", "fleet_truth/checks/http.py", "for attempt in range(2):", "for attempt in range(1):", HTTP),
 ("config: literal secret keys allowed", "fleet_truth/config.py", '_scan_literal_secrets(data, "", ctx)\n    top', 'top', ["tests/test_config.py"]),
 ("config: userinfo URL allowed", "fleet_truth/config.py", 'if s.username is not None or s.password is not None or "@" in s.netloc:', "if False:", ["tests/test_config.py"]),
 ("config: http without allow_private", "fleet_truth/config.py", 'if s.scheme == "http" and not allow_private:', "if False:", ["tests/test_config.py"]),
 ("config: env names unchecked", "fleet_truth/config.py", "if not secrets.ENV_NAME.match(v):", "if False:", ["tests/test_config.py"]),
 ("config: env value printed in plan", "fleet_truth/config.py", '"missing env vars (names only; dependent checks will report unknown): " + ", ".join(missing)', '"missing env vars: " + ", ".join(f"{m}={__import__(\'os\').environ.get(\'FLEET_GITHUB_TOKEN\')}" for m in missing)', ["tests/test_config.py", "-k", "env_values or missing_env"]),
 ("git: fsmonitor not disabled", "fleet_truth/gitmirror.py", '"-c", "core.fsmonitor=false", "-c", "credential.helper="', '"-c", "credential.helper="', ["tests/test_drift.py", "-k", "hooks or protocols"]),
 ("git: status takes optional locks", "fleet_truth/gitmirror.py", '"-C", str(clone), "--no-optional-locks", "status"', '"-C", str(clone), "status"', ["tests/test_drift.py", "-k", "unchanged"]),
 ("git: token placed in argv", "fleet_truth/gitmirror.py", 'PROTOCOLS, "--git-dir", str(m), "fetch", "--prune"', 'PROTOCOLS, "-c", f"http.extraHeader=X-T: {token}", "--git-dir", str(m), "fetch", "--prune"', ["tests/test_drift.py", "-k", "token"]),
 ("git: askpass script embeds token", "fleet_truth/gitmirror.py", 'p.read_text() != ASKPASS_SCRIPT', 'True', ["tests/test_drift.py", "-k", "token"]),
 ("git: protocol allow-list dropped", "fleet_truth/gitmirror.py", '"-c", "protocol.allow=never", ', '', ["tests/test_drift.py", "-k", "protocols"]),
 ("drift: IN_SYNC never reported", "fleet_truth/checks/drift.py", "elif full == origin:", "elif False:", ["tests/test_drift.py"]),
 ("drift: unpushed reads origin not refs/local", "fleet_truth/checks/drift.py", 'return f"refs/local/{branch}"', 'return f"refs/remotes/origin/{branch}"', ["tests/test_drift.py"]),
 ("drift: hostile commit not regex-checked", "fleet_truth/checks/drift.py", "return c if HEX.match(c) else None", "return c", ["tests/test_drift.py"]),
 ("flap: confirm ignored", "fleet_truth/state.py", "if consecutive >= confirm:", "if True:", ["tests/test_state.py"]),
 ("flap: recovery without prior alert", "fleet_truth/state.py", "        if alerted is not None:\n            level", "        if True:\n            level", ["tests/test_state.py"]),
 ("flap: reminder every hour", "fleet_truth/state.py", '"crit": 6 * 3600', '"crit": 3600', ["tests/test_state.py"]),
 ("flap: blind after 1 min", "fleet_truth/state.py", "BLIND_AFTER_S = 30 * 60", "BLIND_AFTER_S = 60", ["tests/test_state.py"]),
 ("correlation: access rule off", "fleet_truth/cycle.py", "if len(acc) >= 2 and", "if len(acc) >= 99 and", ["tests/test_cycle.py"]),
 ("correlation: offline rule off", "fleet_truth/cycle.py", "offline = len(hz) >= 2 and", "offline = len(hz) >= 99 and", ["tests/test_cycle.py"]),
 ("cycle: no deadline", "fleet_truth/cycle.py", "all_done.wait(timeout=deadline_s)", "all_done.wait()", ["tests/test_cycle.py", "-k", "deadline or mid_app"]),
 ("runner: DRIFT runs after failed probe", "fleet_truth/checks/runner.py", "                if probe.status != \"ok\":\n                    yield \"DRIFT\"", "                if False:\n                    yield \"DRIFT\"", ["tests/test_cycle.py"]),
 ("alerts: dead-man not withheld", "fleet_truth/alerts.py", "if cycle_ok and not channels_healthy:", "if False:", ["tests/test_alerts.py"]),
 ("alerts: telegram token in body", "fleet_truth/alerts.py", '"text": msg.text, "disable_web_page_preview": True}', '"text": msg.text + tok, "disable_web_page_preview": True}', ["tests/test_alerts.py"]),
 ("alerts: min_status ignored", "fleet_truth/alerts.py", "if LEVEL_RANK[d.level] >= LEVEL_RANK[s.min_status]]", "if True]", ["tests/test_alerts.py"]),
 ("alerts: text not scrubbed", "fleet_truth/alerts.py", 'return _TOKENISH.sub("***", secrets.scrub(text))', "return text", ["tests/test_alerts.py"]),
 ("alerts: stored error not scrubbed", "fleet_truth/alerts.py", "    return SendResult(False, clean(str(e) if isinstance(e, str) else type(e).__name__)[:200])", "    return SendResult(False, str(e) if isinstance(e, str) else str(e))", ["tests/test_alerts.py"]),
 ("alerts: private targets allowed", "fleet_truth/alerts.py", "allow_private=False, method=method, body=body, timeout_s=10.0)", "allow_private=True, method=method, body=body, timeout_s=10.0)", ["tests/test_alerts.py"]),
 ("alerts: undelivered not retried", "fleet_truth/runtime.py", "        if d.undelivered:  # nobody heard it: retry next cycle", "        if False:", ["tests/test_alerts.py"]),
 ("alerts: cycle error does not ping /fail", "fleet_truth/runtime.py", "alerts.dead_man_ping(cfg, cycle_ok=False,", "(lambda *a, **k: None)(cfg, cycle_ok=False,", ["tests/test_alerts.py"]),
 ("files: lock file created", "fleet_truth/checks/files.py", "fd = os.open(p, os.O_RDONLY)", "fd = os.open(p, os.O_RDONLY | os.O_CREAT, 0o600)", ["tests/test_file_checks.py"]),
 ("files: date-only builtAt alerts", "fleet_truth/checks/files.py", 'return CheckResult("unknown", "BUILD_TIME_NOT_SET", "BUILD_TIME not set: builtAt is only a file-mtime date")', 'return CheckResult("warn", "BUILD_TIME_NOT_SET", "x")', ["tests/test_file_checks.py"]),
 ("files: RUNNING guard removed", "fleet_truth/checks/files.py", 'if verdict in ("NEVER_RAN", "FAILED", "STALE") and lock_is_held(path.parent):', "if False:", ["tests/test_file_checks.py"]),
 ("store: WAL off", "fleet_truth/store.py", 'self.db.execute("PRAGMA journal_mode=WAL")', "pass", ["tests/test_store.py"]),
 ("conftest: connect not blocked", "tests/conftest.py", 'monkeypatch.setattr(socket.socket, "connect", connect)', "pass", ["tests/test_scaffold.py"]),
 ("conftest: getaddrinfo not blocked", "tests/conftest.py", 'monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)', "pass", ["tests/test_scaffold.py"]),
]
M += [
 ("ssrf: link-local AND non-global fallback removed (metadata reachable)", "fleet_truth/net.py", [('if ip.is_link_local:\n        return "link-local address (includes cloud metadata)"', 'if False:\n        return "x"'), ('if ip.is_global and not (isinstance(ip, ipaddress.IPv4Address) and ip in _CGNAT):\n        return None\n    return', 'return None\n    return')], NET),
 ("error message includes URL path (declared-size branch)", "fleet_truth/net.py", 'raise FetchError("TOO_LARGE", f"response from {target.host} exceeds {max_bytes} bytes")\n            chunks', 'raise FetchError("TOO_LARGE", f"response from {target.host}{target.path} exceeds {max_bytes} bytes")\n            chunks', NET),
 ("git: status without --no-optional-locks AND GIT_OPTIONAL_LOCKS unset", "fleet_truth/gitmirror.py", [('"-C", str(clone), "--no-optional-locks", "status"', '"-C", str(clone), "status"'), ('"GIT_OPTIONAL_LOCKS": "0", ', '')], ["tests/test_drift.py", "-k", "unchanged"]),
 ("git: askpass hands the token out for the Username prompt", "fleet_truth/gitmirror.py", "x-access-token ;;", "$FLEET_GIT_TOKEN ;;", ["tests/test_drift.py", "-k", "token"]),
 ("alerts: stored error not scrubbed (both layers)", "fleet_truth/alerts.py", [("    return SendResult(False, clean(str(e) if isinstance(e, str) else type(e).__name__)[:200])", "    return SendResult(False, str(e) if isinstance(e, str) else str(e))"), ("        err = clean(res.error)", "        err = res.error"), ("    return SendResult(False, clean(f\"{e.kind}: {e.message}\")[:200])", "    return SendResult(False, f\"{e.kind}: {e.message}\")")], ["tests/test_alerts.py"]),
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

# Implementation notes (cards [1]-[7], branch wt/1-mvp)

Code is authoritative; [design.md](design.md) is historical now. This page records where the build chose, deviated or added something the design left open. Review these first.

## Layout

  -  `fleet_truth/`: `config.py` (loader), `secrets.py` (env access and scrubbing), `net.py` (pinned fetcher, the only HTTP client), `gitmirror.py`, `checks/{base,http,drift,files,runner}.py`, `store.py`, `state.py` (flap guard), `cycle.py`, `alerts.py`, `runtime.py`, `cli.py`, `version.py`, `VERSION`.  
  -  `tests/`: one file per module plus `fakes.py` (fake servers on 127.0.0.x, openssl-made certs) and `gitfix.py` (temp origin, clone, mirror). `spike/` is untouched and runs in the same pytest session.  
  -  Run: `python3 -m pytest -q` (pyproject sets `pythonpath`, so no install is needed for tests).  

## Decisions the cards did not make (please confirm or overrule)

  [1]  **Config keys for channels the design only names:** `email: {smtp_host, smtp_port (587), username_env, password_env, from, to}`, `webhook: {url_env, mode: json|healthchecks}` (the URL may embed a secret, so it is an env var), `ntfy: {url, topic_env, token_env}`. Top-level `git: {fetch_minutes: 15}` added.  
  [2]  **Exit code 4** for an internal cycle failure (the card defines 0 to 3). `unknown` maps to exit 1.  
  [3]  **`unknown` ranks between `info` and `warn`** when computing the worst status, so it is never green.  
  [4]  **Correlation thresholds:** the Access rule needs at least 2 `auth: access` apps all `AUTH_REJECTED`; the offline rule needs at least 2 apps whose HEALTHZ failed at the network layer (`data.net_error` set by the fetch-error mapper). HTTP 503 everywhere is not "monitor offline". Both collapse to a pseudo check on app `_monitor` that runs through the normal flap guard (so it also recovers once).  
  [5]  **Undelivered alerts retry:** if a decision reached no eligible channel, `alerted_status` and `last_alert_at` are rolled back so the next cycle decides again. No payload table is needed.  
  [6]  **Dead-man "healthy channel":** a channel whose most recent `alerts` row is ok, or that has no row yet. No channels configured means the ping is withheld. `test-alert` records its sends, so repairing a channel and running it restores the ping. A cycle error always pings `/fail`.  
  [7]  **healthchecks mode:** only crit alert/reminder maps to `/fail` and recovery/test to the base URL. warn and blind send nothing in that mode (open: see Deferred).  
  [8]  **BACKUP_AGE** adds verdict `UNREADABLE` (crit) for a status file that exists but is not valid JSON; the library reports that as "never ran". The RUNNING guard also covers `NEVER_RAN`; `WRONG_APP` is never masked.  
  [9]  **TLS_EXPIRY** runs when `tls.every_hours` (6) has passed since the last run (stored in `meta`), not every cycle; with `confirm: 2` a warn therefore needs two runs (6 to 12 h) to alert. Acceptable for 21-day warnings; revisit if not.  
  [10]  **DRIFT input:** uses `commitFull` from the version body when present, else `commit`.  
  [11]  **HEARTBEAT** timestamps: epoch seconds (not milliseconds) or ISO-8601 with a UTC offset; naive ISO, date-only, future (more than 5 min) and non-numbers are `unknown`. Same offset rule for `STALE_BUILD`.  
  [12]  **allow_private** permits RFC 1918, 100.64.0.0/10 and IPv6 ULA (`fc00::/7`, Tailscale's `fd7a:115c:a1e0::/48` among it); never loopback, link-local, site-local (`fec0::/10`), special-purpose ranges or cloud metadata (169.254.169.254, 169.254.170.2, 100.100.100.200, 168.63.129.16, fd00:ec2::254 and the metadata hostnames), also not through IPv6-embedded forms. These rules are explicit networks next to the stdlib `is_*` properties, so the verdict does not depend on the Python version (tests neutralise the stdlib and still pass).  

## Review fix pass (REVIEW-wt-1-mvp.md)

  [1]  **Email** really sends now: the pinned SMTP class passed no timeout (TypeError on every send) and never set the TLS server name (STARTTLS would have failed); both fixed, greeting code checked, failures reported as exception class plus SMTP code. Tested against a loopback SMTP stub (`tests/smtpstub.py`: success with STARTTLS before AUTH before DATA, no STARTTLS offered, refused, bad greeting, silent server, bad credentials, rejected DATA, untrusted cert). Telegram, ntfy and webhook each have a test through their real sender and `net.fetch` against a loopback TLS server (`TELEGRAM_API` is a module constant the test overrides).  
  [2]  **BACKUP_AGE RUNNING is bounded:** `backup.running_grace` (hours; default `max(2 x interval, 6 h)`) counted from the first cycle that saw the lock held (kept in `meta`, falls back to the lock file's mtime without a store). After it: crit `LOCK_STUCK`, "backup lock held for N hours". An ok status and `WRONG_APP`/`UNREADABLE` are never masked.  
  [3]  **BACKUP_AGE rejects impossible `last_ok_epoch`** (NaN, Infinity, non-positive, non-number, >= 1e11 i.e. milliseconds, more than 5 min in the future): crit `UNREADABLE` with the reason. A missing epoch stays `STALE` as in the library. **Follow-up:** the backup library's `_check_status` (events-app `wt-67-backup/deploy/backup/backup_lib.py`) has the same gap (NaN/Infinity/future/millisecond epochs read ok); not edited here.  
  [4]  **Outage correlation** needs an independent signal: optional top-level `reference_urls` (at most 3, public https). All apps failing at the network layer plus: reference answers -> crit `FLEET_OUTAGE` ("fleet-wide outage (VPS or tunnel)"), nothing suppressed; reference fails -> crit `MONITOR_OFFLINE` ("monitor network offline"), app network alerts suppressed; no reference configured -> crit `ALL_APPS_FAILING` ("VPS/tunnel outage OR monitor offline"), nothing suppressed. The reference is only fetched when every app fails.  
  [5]  **Blind alerts** are `blind_level: crit` by default (config may lower to warn). A decision is *undelivered* when no channel accepted it and either some channel was eligible, no channel is configured, or it is a blind alert. Any undelivered decision withholds the OK dead-man ping and sends `/fail` (class `AlertUndelivered`), and rolls the alert state back for a retry. **Deliberate deviation:** a warn-level alert that a crit-only channel filters out by `min_status` is NOT undelivered (otherwise a deliberate `min_status: crit` would retry and `/fail` every cycle forever). Please confirm or overrule.  
  [6]  **Resilience:** (a) a corrupt DB is renamed to `fleet-truth.db.corrupt-<stamp>` (plus -wal/-shm; never deleted), a `/fail` ping goes out, the run continues on a fresh DB and a `[RESET]` alert goes to every channel; any failure before the cycle (data dir, lock, DB) also pings `/fail`. (b) a `.initialized` marker in the data dir detects a vanished DB; the reset alert is the forced channel re-test and it is recorded, so a dead channel is unhealthy after a wipe instead of assumed good. (c) `flock` on `<data_dir>/run.lock`: an overlapping `once` exits 5 and does nothing. (d) `flap: {flips: 6, cycles: 12, remind_hours: 6}`: any status change except ok<->info counts; N within the window gives one `flapping` warn per check per `remind_hours`, only while no failure alert is active. (e) `fleet.yaml` refuses YAML anchors/aliases, duplicate keys, files over 64 KB, nesting over 12 levels, over 5000 values and over 5000 flow brackets. (f) see [12] above; ports must be 1-65535 in URLs, `tls.host` and `smtp_port`; DNS lookup runs in a thread bounded by the probe's time limit. (g) layer tests: stdlib `ipaddress` classification neutralised by monkeypatch; own rules still refuse every special range, mapped, NAT64, 6to4 and compat form.  
  -  Exit codes of `once`: 0 ok/info, 1 warn/unknown, 2 crit, 3 config error, 4 internal error, **5 another run holds the lock**.  

## Security behaviours and where they are proven

  -  SSRF table, pinning, no redirect, size and time caps, proxy env ignored, error text carries host only: `tests/test_net.py`.  
  -  Access headers only on https `auth: access` URLs; detail and data never carry header values: `tests/test_http_checks.py`.  
  -  Config rejects literal secrets, userinfo, http without allow_private, bad env names; env values never printed or logged; config has no key that reaches the test-only loopback hook: `tests/test_config.py`, `tests/test_net.py::test_config_cannot_reach_the_loopback_hook`.  
  -  Working clone `.git` byte-identical after a full run; hooks, fsmonitor, packObjectsHook in the clone never execute; token absent from mirror config, argv, logs, errors: `tests/test_drift.py`.  
  -  Flap guard, recovery, reminders, blind rule, correlation, deadline: `tests/test_state.py`, `tests/test_cycle.py`.  
  -  Alert text and stored errors scrubbed; Telegram token only in the URL path; dead-man withheld when all channels fail; `/fail` on cycle error: `tests/test_alerts.py`.  
  -  Mutation proofs (scratch copy; first pass 59 mutants, 52 killed, 7 explained; fix pass 50 more, all killed): [../WIP/streams/wt-1-mvp.md](../WIP/streams/wt-1-mvp.md).  
  -  Test-suite guards: sockets blocked unless `@pytest.mark.loopback` (127.0.0.1 and ::1 only); git refuses any remote except local paths, `file://` and `127.0.0.1`; `HOME` is a temp dir per test.  

## Not built (outside cards [1]-[7])

  -  `MONITOR_SELF` (mirror fetch errors are kept on `Runner.refresh_results` for it), clock-skew warning, `heartbeat.url`, `probe_via_proxy`, `silenced_until` use (card [12]), web page and `serve` (card [8]).  

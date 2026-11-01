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
  [12]  **allow_private** permits RFC 1918 and 100.64.0.0/10 only, as designed. IPv6 ULA (Tailscale's `fd7a:115c:a1e0::/48`) is not permitted.  

## Security behaviours and where they are proven

  -  SSRF table, pinning, no redirect, size and time caps, proxy env ignored, error text carries host only: `tests/test_net.py`.  
  -  Access headers only on https `auth: access` URLs; detail and data never carry header values: `tests/test_http_checks.py`.  
  -  Config rejects literal secrets, userinfo, http without allow_private, bad env names; env values never printed or logged; config has no key that reaches the test-only loopback hook: `tests/test_config.py`, `tests/test_net.py::test_config_cannot_reach_the_loopback_hook`.  
  -  Working clone `.git` byte-identical after a full run; hooks, fsmonitor, packObjectsHook in the clone never execute; token absent from mirror config, argv, logs, errors: `tests/test_drift.py`.  
  -  Flap guard, recovery, reminders, blind rule, correlation, deadline: `tests/test_state.py`, `tests/test_cycle.py`.  
  -  Alert text and stored errors scrubbed; Telegram token only in the URL path; dead-man withheld when all channels fail; `/fail` on cycle error: `tests/test_alerts.py`.  
  -  Mutation proofs (scratch copy, 59 mutants, 52 killed, 7 explained): [../WIP/streams/wt-1-mvp.md](../WIP/streams/wt-1-mvp.md).  
  -  Test-suite guards: sockets blocked unless `@pytest.mark.loopback` (127.0.0.1 and ::1 only); git refuses any remote except local paths, `file://` and `127.0.0.1`; `HOME` is a temp dir per test.  

## Not built (outside cards [1]-[7])

  -  `MONITOR_SELF` (mirror fetch errors are kept on `Runner.refresh_results` for it), clock-skew warning, `heartbeat.url`, `probe_via_proxy`, `silenced_until` use (card [12]), web page and `serve` (card [8]).  

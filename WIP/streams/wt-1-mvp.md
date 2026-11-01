# Stream report: wt/1-mvp (cards [1]-[7])

Builder: one agent. Branch `wt/1-mvp`, worktree `/home/dftms/worktrees/fleet-truth/wt-1-mvp`, off `main` at ca46b59. Nothing pushed, no board edited, no `gh`, no ssh, no docker. All tests use fake servers on 127.0.0.x, temp git repos and temp status files; no real repo, URL or token was touched; no real Telegram, healthchecks or email message was sent.

## Totals

  -  **236 tests pass** (7 spike + 229 new) in about 20 s: `python3 -m pytest -q`. Also green from a fresh `git clone` of the branch (run from a scratch directory).  
  -  By file: test_net 57, test_file_checks 32, test_alerts 27, test_http_checks 25, test_drift 22, test_config 22, test_cycle 18, test_state 16, spike 7, test_scaffold 6, test_store 4.  
  -  Mutation check, scratch copy outside the repo ([wt-1-mvp-mutate.py](wt-1-mvp-mutate.py)): 59 mutants of security behaviours, **52 killed**, 7 survivors explained below.  
  -  Python 3.14.4 here; code targets 3.12+. PyYAML 6.0.3 and pytest 9.1.1 were already installed; fastapi, uvicorn, jinja2, httpx are not used by cards [1]-[7] (FastAPI is card [8]).  

## Commits

  [1]  `d230763` card 1, `dcd8c4d` card 2, `3175a8b` card 3, `1c30411` card 4, `462400a` card 5, `948fdcc` card 6, `edd97ec` card 7, `4ed91af` fix-up (test-alert recording, HOME isolation, wider hostile-commit cases), then a docs commit (notes, KANBAN status lines, log, session state, this report).

## Per card, per acceptance criterion

### Card [1] Scaffold

  [1]  fresh-clone pytest: PASS for `python3 -m pytest -q` (pyproject `pythonpath`). `python3 -m venv .venv && pip install -e .[dev]`: **not run** (see Unverifiable).  
  [2]  `--help` lists `once`, `serve`, `check-config` (plus `test-alert` from card [7]); `--version` prints `0.1.0`: `test_cli_version_and_help`, `test_console_module_runs`.  
  [3]  un-opted test cannot open a socket: `test_unopted_test_cannot_open_a_socket` (create_connection, getaddrinfo, connect, connect_ex); `test_loopback_marker_allows_only_loopback`.  
  -  Also: `version.py` (`info()` returns version, build, builtAt, commit; commit is the full `GIT_SHA`), `fleet_truth/VERSION`, `.gitignore` (kept the existing `*.pyc` line).  

### Card [2] Config

  [1]  `fleet.example.yaml` (copied verbatim from design 7) loads; `check-config` prints 5 apps, exit 0: `test_example_loads_with_five_apps`.  
  [2]  unknown key, duplicate name, http without allow_private, `user:pass@`, literal `token:` (six key spellings, nested), bad env name, missing env var (exit 0, name only): `test_unknown_key_reports_path`, `test_duplicate_and_bad_names`, `test_http_requires_allow_private`, `test_url_with_userinfo_rejected_and_not_echoed`, `test_literal_secret_keys_rejected`, `test_bad_env_names`, `test_missing_env_var_reported_not_printed`.  
  [3]  `FLEET_GITHUB_TOKEN=sekrit-123` and four more env values never appear in stdout, stderr, log records, `repr(config)` or error text: `test_env_values_never_printed_or_logged`, `test_log_scrub_filter`.  

### Card [3] Pinned probe

  [1]  refused: `127.0.0.1`, `::1`, `169.254.169.254`, `0.0.0.0`, `::ffff:127.0.0.1`, `2130706433`, `0x7f000001`, `127.1`, plus octal, `::ffff:7f00:1`, `::127.0.0.1`, 6to4, NAT64, IPv6 metadata, `user@` smuggling, 0.0.0.0/8, broadcast; a name with one public and one private answer; each with allow_private false and true; the refusal happens before any socket (a connect attempt would raise the conftest guard, not FetchError). With allow_private `10.x`, `192.168.x`, `172.16.x`, `100.100.40.10` accepted, loopback and link-local still refused: `test_literal_hosts_refused_before_any_socket` (42 cases), `test_allow_private_accepts_rfc1918_and_tailnet_but_never_loopback_or_linklocal`, `test_mixed_public_and_private_answers_refused`, `test_name_resolving_to_loopback_refused_even_when_allow_private`, `test_url_hygiene`.  
  [2]  local TLS server on 127.0.0.2 with a self-signed cert and test-only trust store: peer IP is the validated IP, SNI and Host are the hostname, one lookup only (a rebinding resolver would send a second lookup to 127.0.0.1): `test_tls_peer_is_validated_ip_and_sni_host_are_the_hostname`. Config cannot reach the loopback hook (no config key, name absent from config.py): `test_config_cannot_reach_the_loopback_hook`. Hostname mismatch and untrusted cert fail: `test_tls_hostname_mismatch_and_untrusted_cert_fail`.  
  [3]  302 to `x.cloudflareaccess.com` gives `ACCESS_REDIRECT` (HEALTHZ, unknown) / `AUTH_REJECTED` (DEEP_PROBE, crit); the next hop is never resolved or requested: `test_access_redirect_is_unknown_for_healthz_and_auth_rejected_for_deep_probe_and_hop_not_requested`, `test_redirect_is_a_result_and_never_followed`.  
  [4]  >64 KB (declared and chunked) gives `BAD_BODY`; slow drip (body and headers) gives `TIMEOUT` within the deadline; default timeout run finishes under 11 s: `test_body_over_cap_is_refused_declared_and_undeclared`, `test_slow_drip_body_and_headers_time_out_within_deadline`, `test_default_timeout_is_under_eleven_seconds`, `test_deep_probe_verdicts`.  
  [5]  Access headers absent on http URLs and on apps without `auth: access`, present on https access apps (raw request inspected): `test_access_headers_only_on_https_access_apps`.  
  [6]  `detail` and `data` never contain header values, even when a hostile server echoes them in body and headers: `test_detail_and_data_never_contain_header_values`, `test_checkresult_scrubs_registered_secrets`, `test_error_messages_carry_host_only`.  
  -  Also: proxy env ignored (`test_proxy_environment_is_ignored`), retry once after 3 s on connect error, timeout, 5xx (injected sleep), TLS_EXPIRY thresholds ok/warn/crit and verification failure.  
  -  The "reviewer attempts to bypass the IP check" part of Done-when is for the separate reviewer.  

### Card [4] Mirror, DRIFT, UNPUSHED

  [1]  all 7 spike tests ported to the mirror with the same fixtures (plus hostile `origin/main`-style values): `tests/test_drift.py`; mirror refresh against a `file://` bare origin and the rate limit: `test_refresh_against_file_origin_and_rate_limit`; unpushed commit only in the working clone detected via `refs/local`: `test_unpushed_commit_only_in_working_clone_detected_through_refs_local`; `LIVE_NOT_ON_ORIGIN` when live is that commit: `test_live_runs_unpushed_commit_is_crit`.  
  [2]  working clone `.git` file list, sizes, mtimes, bytes and refs identical before and after two full runs (with a dirty tracked file), no `index.lock`: `test_working_clone_dot_git_unchanged_after_full_run`.  
  [3]  clone configured with `core.fsmonitor`, `core.hooksPath`, `uploadpack.packObjectsHook`, `core.sshCommand`, `diff.external` and 11 executable hooks: nothing executes: `test_hooks_and_fsmonitor_in_working_clone_never_execute`. Every fetch carries the protocol allow-list and hardening flags: `test_protocols_are_limited_in_every_fetch`.  
  [4]  fake token with a failing https remote: absent from the mirror `config` and every file in the mirror, from all git argv (recorded), logs and the error text; the askpass helper prints the token only from the child's environment and holds none itself: `test_token_never_in_mirror_config_argv_logs_or_errors`, `test_origin_url_with_credentials_refused`, `test_error_text_hides_url_userinfo`.  
  -  Also: UNPUSHED age escalation, subject limits (10 x 120, control characters stripped), dirty tracked files info only, branch missing gives unknown and drops the stale `refs/local`.  

### Card [5] File checks

  [1]  fixtures for `NEVER_RAN`, `WRONG_APP`, `FAILED`, `STALE`, ok (plus edge and missing epoch) agree with the library's `_check_status` on ok/not-ok; the library was imported from `/home/dftms/worktrees/events-app/wt-67-backup/deploy/backup/backup_lib.py` with bytecode writing off (it ran, it was not skipped): `test_same_ok_not_ok_as_backup_lib_check_status`, `test_backup_age_verdicts`.  
  [2]  held `flock(LOCK_EX)` on `lock` gives `RUNNING` / info; ok status is not masked; `WRONG_APP` is not masked; nothing is created in the state dir (not even `lock`, not even the dir): `test_held_lock_yields_running_and_nothing_is_created`, `test_check_never_creates_lock_or_any_file`.  
  [3]  date-only `builtAt` gives `unknown`; STALE_BUILD never exceeds `info`: `test_stale_build_date_only_is_unknown_never_alert`, `test_stale_build_age_rules`.  
  -  HEARTBEAT: epoch, ISO with offset and `Z`, stale crit, ten unknown cases.  

### Card [6] Store, cycle, flap guard

  [1]  fake-clock table: fail,fail one alert; fail,ok none; 7 h crit gives alert plus exactly one reminder (6 h apart); warn reminds at 24 h not 6 h; recovery once; unknown 25 min none, 30 to 35 min one blind alert, never repeated: `tests/test_state.py` (16 tests).  
  [2]  five apps `AUTH_REJECTED` together give exactly 1 decision (app `_monitor`), per-app alerts held back, one recovery when fixed; four of five rejected alert per app; one access app alerts normally; all HEALTHZ network-failed gives 1 "monitor offline"; 503s everywhere are per-app: `test_five_apps_auth_rejected_together_give_exactly_one_decision` and the five tests after it.  
  [3]  a check stuck past the deadline: `TIMEOUT_CYCLE`, cycle returns in under 2 s with a 0.4 s deadline, late result discarded; partial results of a half-finished app kept: `test_check_past_deadline_is_timeout_cycle_and_cycle_returns_on_time`, `test_partial_results_kept_when_deadline_hits_mid_app`.  
  [4]  DB (and WAL files) deleted, next `once` recreates it: `test_deleting_the_db_recreates_it`. Store tests: schema, WAL, `user_version`, newer-schema refusal, corrupt-file message, 2 KB cap, retention (results 30 d, runs 90 d, alerts 365 d).  
  -  Also: exceptions become `unknown` ERROR (class name only); DRIFT and STALE_BUILD `unknown` PROBE_FAILED when DEEP_PROBE failed; TLS runs every 6 h; one full real-Runner cycle against a fake HTTP server, a file:// origin and a temp backup file; `once` exit codes.  

### Card [7] Alerts and dead-man

  [1]  stub transport: Telegram request is `POST https://api.telegram.org/bot<token>/sendMessage` with the token only in the URL path (not body, not headers); ntfy, webhook (json and healthchecks), email (STARTTLS before login) build the documented requests; every transport call has `allow_private=False`; failures from four hostile shapes (including an exception whose message contains the token) leave the token out of `alerts.error`, the returned error and the log: `test_telegram_request_and_token_only_in_url_path`, `test_telegram_failures_never_leak_token_into_error_or_logs`, `test_ntfy_request`, `test_webhook_json_and_healthchecks_modes`, `test_email_starttls_before_login_and_message_has_no_password`. Through the real pinned fetcher over a local TLS server: webhook and dead-man `/` and `/fail`.  
  [2]  all channels failing: ping withheld; one healthy channel: ping sent; no channels: withheld; cycle error: `/fail` with the exception class only: `test_all_channels_failing_withholds_the_dead_man_ping`, `test_one_healthy_channel_is_enough_for_the_ping`, `test_no_channels_configured_withholds_ping`, `test_cycle_error_pings_fail_endpoint_and_reraises`.  
  [3]  `fleet-truth test-alert [-c PATH]` sends one `[TEST]` per channel (ignores `min_status`), prints `name: ok` or `name: failed (reason)`, exit 0 only if all ok: `test_test_alert_sends_one_test_message_to_every_channel`, `test_test_alert_reports_failed_channel_and_exit_1`.  
  -  Also: `min_status` filter, recovery routed by the level that was alerted, one broken sender does not stop the others, undelivered alerts are retried next cycle, every send recorded in `alerts`, 500-character cap, token-shaped strings scrubbed from text, default transport refuses loopback targets, redirects not followed.  
  -  **Done-when not met by design:** a real `[TEST]` on the owner's channel needs Q3 and a real bot.  

## Mutation evidence (scratch copy, [wt-1-mvp-mutate.py](wt-1-mvp-mutate.py))

Killed (52): loopback allowed; private allowed without allow_private; only first resolved IP validated; legacy numeric hosts accepted; link-local plus non-global fallback removed (metadata reachable); connect by hostname instead of validated IP; SNI set to the IP; userinfo allowed; streamed size cap removed; watchdog 100x longer (hang caught by timeout); URL path in error message; Access headers on http; Access headers to non-access apps; Access redirect undetected; result detail and data unscrubbed; retry removed; config accepts literal secret keys, userinfo URLs, http without allow_private, unchecked env names, prints an env value; fsmonitor not disabled; status without optional-locks protection; token placed in git argv; askpass hands the token to the Username prompt; protocol allow-list dropped; IN_SYNC never reported; UNPUSHED reading origin instead of `refs/local`; commit regex removed (needed the extra `origin/main`-style cases added during the run); confirm ignored; recovery without alert; reminder 1 h; blind 1 min; Access and offline correlation off; no cycle deadline (hang caught); DRIFT run after failed probe; dead-man not withheld; Telegram token in body; `min_status` ignored; alert text and stored errors unscrubbed; private alert targets allowed; undelivered not retried; `/fail` not pinged; lock file created; date-only builtAt alerting; RUNNING guard removed; WAL off; conftest connect and getaddrinfo guards removed.  

Survivors (7), all explained:

  -  **link-local branch removed alone** (1), **IPv4-mapped unwrap removed alone** (5), **declared-size check removed alone** (10), **`--no-optional-locks` removed alone** (25), **single scrub layer removed** (44): redundant layers (stdlib `is_global`, the streamed cap, `GIT_OPTIONAL_LOCKS=0`, the Dispatcher's second scrub). Each was re-run with its backup layer also removed and was killed (mutants 54, 56, 58). Layers 5 and 10 are equivalent on Python 3.14 (stdlib already classes those forms as non-global; the streamed cap catches the declared case).  
  -  **URL path in the streamed-size message** (12): mutated the branch the test does not reach; the declared branch variant (55) was killed.  
  -  **askpass embeds token** (27): invalid mutant (the script is a constant); replaced by mutant 57, killed.  

## Deferred (judgment calls the cards do not resolve; item stopped, proposal given)

  [1]  **`heartbeat.url`** (design 4.7 mentions a URL variant; card [5] specifies a file). Proposal: add after card [9] gives apps a probe path, reusing `net.fetch`.  
  [2]  **healthchecks-mode mapping for warn and blind** (design maps only crit and recovery). Currently nothing is sent for them in that mode. Proposal: map warn and blind to `<ping-url>/log` (informational, does not flip the check).  
  [3]  **`MONITOR_SELF`** (design 4.10: mirror fetch errors, send errors, credential expiry dates, clock skew) is not in cards [1]-[7]; the data exists (`Runner.refresh_results`, `credentials.*_expires`, `alerts` table). Proposal: fold into card [8] or [10].  
  [4]  **TLS alert latency** with `confirm: 2` and a 6 h run interval (6 to 12 h). Proposal: leave; or run the check each cycle when the last result was not ok.  
  [5]  **IPv6 ULA tailnet addresses** are refused under allow_private (spec lists IPv4 ranges only). Proposal: add `fd7a:115c:a1e0::/48` only if a monitored target needs it.  
  [6]  **`probe_via_proxy`** (design 9.1 step 5, off by default) not built; proxies are always ignored.  
  [7]  `silenced_until` column exists; silence CLI and digest are card [12].  
  [8]  DNS resolution itself cannot be interrupted, so a hanging resolver can exceed `timeout_s`; the cycle deadline still bounds the cycle. Proposal: none needed unless it shows up live.  

Twelve smaller decisions (config keys for email/webhook/ntfy, exit code 4, correlation thresholds, dead-man channel-health rule, and so on) are listed in [../../wiki/implementation-notes.md](../../wiki/implementation-notes.md) for the owner or reviewer to confirm.

## Unverifiable here

  -  `python3 -m venv .venv && pip install -e .[dev]`: this environment has no `pip` and no network. `pyproject.toml` was not exercised by an install; tests run through `pythonpath`. The `[project.scripts]` console entry was only checked through `python3 -m fleet_truth.cli`.  
  -  Real GitHub authentication through the askpass helper (the helper's output and git's refusal of a dead https remote are tested, not a successful token fetch).  
  -  Real Telegram, ntfy, SMTP and healthchecks.io delivery, and the dead-man firing when the process stops (card [8] criterion 3).  
  -  TLS verification against the system trust store for real hosts (tests use a test-only trust store and prove that the default store rejects a self-signed cert).  
  -  Behaviour on Python 3.12 and 3.13 (only 3.14.4 available); the code avoids newer-only syntax.  
  -  WSL `systemd --user` and cron behaviour (card [8]).  

## Owner steps

  [1]  Review the branch: `git -C /home/dftms/fleet-truth log wt/1-mvp`; spawn the separate adversarial reviewer per card (the cards require it) with the card text plus the diff.  
  [2]  Answer Q1-Q6 ([open-questions.md](../../wiki/open-questions.md)); Q3 gates card [7]'s real `[TEST]`, Q4 gates the invoicing liveness URL.  
  [3]  Land `wt/1-mvp` on `main` yourself (the stream fence forbids the builder from doing it).  
  [4]  Before card [8]: create the healthchecks.io check (period 5 min, grace 15 min), the fine-grained GitHub token (Contents read-only), choose the alert channel, put the values in `~/.config/fleet-truth/env` (0600), write `fleet.yaml` from `fleet.example.yaml`, then run `fleet-truth check-config fleet.yaml` and `fleet-truth test-alert` yourself.  
  [5]  Install tooling for the venv step: `python3 -m venv .venv && .venv/bin/pip install -e .[dev]` on a machine with network, to confirm the acceptance line this environment could not.  


# Fix pass (after REVIEW-wt-1-mvp.md, verdict "fix then ship")

Totals: **416 tests pass** (baseline 236, +180) in about 23 s, also from a fresh clone. Mutation check for the new code and tests: [wt-1-mvp-mutate-fixpass.py](wt-1-mvp-mutate-fixpass.py), 50 mutants, **50 killed** (first run left one survivor, a depth-cap test that raised for the wrong reason; fixed and re-killed). Re-run of the first-pass script: the same documented survivors; patterns that moved are covered by the new script.

## Per item

  [1]  **Email/senders (HIGH).** Root causes: no timeout (TypeError in `settimeout`) and no TLS server name (the second bug, found by the new test). Also added: greeting-code check, precise failure text (class plus SMTP code). `tests/smtpstub.py` is a real SMTP server on 127.0.0.1; `test_real_email_path_*` cover success (message only after STARTTLS, AUTH after it) and seven failure modes; `test_real_email_factory_refuses_unvalidated_targets`. Real-path tests for the other senders: `test_real_telegram_path`, `test_real_ntfy_path`, `test_real_webhook_json_and_healthchecks_paths` (through `net.fetch` and a loopback TLS server, success and HTTP-error cases).  
  [2]  **RUNNING bound (HIGH).** `test_hung_backup_cannot_hide_a_30_day_stale_required_backup` is the reproduced scenario; plus `test_running_grace_defaults_and_config`, `test_grace_without_store_falls_back_to_lock_mtime`, `test_ok_status_is_never_replaced_by_lock_stuck`.  
  [3]  **Timestamps.** `test_backup_age_rejects_impossible_timestamps` (12 table rows), `test_small_clock_skew_is_tolerated_and_missing_epoch_stays_stale`, `test_unreadable_is_not_masked_by_a_running_lock`. Library follow-up recorded in implementation-notes (not edited).  
  [4]  **Correlation.** Three-case tests, reference only consulted when needed, real `Runner.reference_ok`, config validation (`tests/test_cycle.py`).  
  [5]  **Blind/undelivered.** `test_blind_alert_is_crit_eligible_by_default_and_reaches_a_crit_only_channel`, `test_blind_alert_nobody_can_receive_withholds_ok_ping_and_sends_fail`, `test_decision_with_no_channels_configured_is_undelivered_and_fails_the_ping`, `test_severity_filtered_warn_on_crit_only_channel_is_not_a_failure`, state and config tests.  
  [6]  **Smaller.** (a)(b)(c) `tests/test_resilience.py` (5 tests); (d) four flap tests in `tests/test_cycle.py`; (e) four YAML tests in `tests/test_config.py` (alias bomb rejected in under 1 s); (f) metadata addresses and hostnames, site-local and special ranges, ports (URL, config, tls.host, smtp_port), DNS inside the time limit in `tests/test_net.py`; (g) `stdlib_says_everything_is_fine` fixture tests, the declared-size layer alone, both git no-lock guards, both scrub layers.  

## Judgment calls to confirm

  -  Filtered-by-`min_status` warns are not "undelivered" (see implementation-notes [5]).  
  -  `allow_private` now admits IPv6 ULA (you wrote "loopback/RFC1918/CGNAT/ULA"); loopback stays refused (card [3] acceptance and the existing tests require that). Per-host explicitness is the existing per-URL `allow_private` flag.  
  -  The `/fail` ping on a state reset is followed, in the same run, by the normal OK ping if the cycle completes and a channel heard the reset alert.  
  -  Flap counts every status change except ok<->info (so crit<->unknown and crit<->warn flap too).  

## Deferred, updated

  -  Item [5] of the first Deferred list (IPv6 ULA) is resolved. Remaining: `heartbeat.url`, healthchecks warn/blind mapping, `MONITOR_SELF`, TLS alert latency, `probe_via_proxy`, silences (card [12]).  
  -  New: `backup_lib._check_status` has the timestamp gap from [3] (library not edited).  

## Unverifiable here (unchanged, plus)

  -  Real SMTP providers (STARTTLS negotiation, AUTH mechanisms beyond what smtplib picks) were exercised only against the loopback stub.  
  -  Python 3.12 is still not available to run; the version-dependent layers are now covered by stdlib-neutralising tests instead.  

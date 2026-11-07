# Fleet Truth -- Kanban

Card format: `### [N] title`, then Severity / Priority / Est / Why / Action / Acceptance / Rollback / Done when. Cards [1]-[8] are the weekend MVP, in build order; each is one builder agent, under a day. Cards [9]-[14] are the one-month version. Design: [wiki/design.md](wiki/design.md). Plan and test strategy: [wiki/mvp-plan.md](wiki/mvp-plan.md).

Every card is reviewed by a separate adversarial agent that sees only the card and the diff; it is not done until findings are fixed or answered on the card.

## In Progress

  -  Cards [1]-[7] are built on branch `wt/1-mvp` (worktree `/home/dftms/worktrees/fleet-truth/wt-1-mvp`), one commit per card, 236 tests green. Awaiting the separate adversarial reviewer and the owner steps listed in [WIP/streams/wt-1-mvp.md](WIP/streams/wt-1-mvp.md). Not merged to `main`.  

## Needs review

### [1] Scaffold: package, CLI, version, tests

  -  **Status (2026-11-01):** built, tests green, awaiting reviewer. Commit on `wt/1-mvp`; evidence in [WIP/streams/wt-1-mvp.md](WIP/streams/wt-1-mvp.md).  
  -  **Severity:** medium  
  -  **Priority:** high (blocks everything)  
  -  **Est:** 2 h  
  -  **Why:** Every later card needs one layout, one test command and one CLI entry point.  
  -  **Action:** Create `pyproject.toml` (Python >= 3.12; deps `fastapi`, `uvicorn`, `jinja2`, `pyyaml`; dev dep `pytest`, `httpx` for the FastAPI test client only), package `fleet_truth/` with `__init__.py`, `cli.py` (`argparse` subcommands `once`, `serve`, `check-config`, each a stub that exits 0 printing "not implemented"), `VERSION` (`0.1.0`) and `version.py` copied in spirit from `/home/dftms/health-web/healthapp/version.py` (`info()` -> `{version, build, builtAt, commit}`, commit = full `GIT_SHA` if set). `tests/conftest.py` that blocks real network (monkeypatch `socket.getaddrinfo` and `socket.create_connection` to raise unless a test opts in), like events-app's proxy-hermetic conftest. Move `spike/` tests into the `pytest` run (keep the spike files unchanged). `.gitignore`: `fleet.yaml`, `*.env`, `.env*`, `data/`, `__pycache__/`, `.venv/`.  
  -  **Acceptance:**  
       [1]  `python3 -m venv .venv && .venv/bin/pip install -e .[dev] && .venv/bin/pytest -q` passes from a fresh `git clone` of the repo.  
       [2]  `fleet-truth --help` lists the three subcommands; `fleet-truth --version` prints `VERSION`.  
       [3]  A test proves an un-opted test cannot open a socket.  
  -  **Rollback:** `git revert` the card's commit(s); nothing outside the repo is touched.  
  -  **Done when:** acceptance passes and the reviewer's findings are closed.  

### [2] fleet.yaml loader and validation

  -  **Status (2026-11-01):** built, tests green, awaiting reviewer. Commit on `wt/1-mvp`; evidence in [WIP/streams/wt-1-mvp.md](WIP/streams/wt-1-mvp.md).  
  -  **Severity:** high (wrong config = silent blind spots)  
  -  **Priority:** high  
  -  **Est:** 3 h  
  -  **Why:** One file describes the fleet; it must reject mistakes loudly and never carry secrets.  
  -  **Action:** `fleet_truth/config.py`: load YAML with `yaml.safe_load`, build frozen dataclasses for the schema in [wiki/design.md](wiki/design.md) section 7, apply `defaults` per app, expand `~`. Rules: unknown keys are errors with the key path; app names `^[a-z0-9][a-z0-9-]{0,40}$` and unique; URLs `https://` or (`http://` only with `allow_private: true`); no userinfo in URLs; every `*_env` key holds an env var NAME matching `^[A-Z][A-Z0-9_]*$`; a key that looks like a literal secret (`token:`, `secret:`, `password:` without `_env`) is an error saying "put it in an env var". Resolve env values lazily through `secrets.get(name)` that returns `None` when unset. Commit `fleet.example.yaml` = the example in design section 7. `fleet-truth check-config PATH` prints the resolved apps and which checks will run for each, and which env vars are missing (names only).  
  -  **Acceptance:**  
       [1]  `fleet.example.yaml` loads; `check-config` prints 5 apps and exits 0.  
       [2]  Tests cover: unknown key, duplicate name, `http` without `allow_private`, URL with `user:pass@`, literal `token:` key, bad env name, missing env var (exit 0, reported as missing, not printed as a value).  
       [3]  No code path prints or logs an env var's value (test sets `FLEET_GITHUB_TOKEN=sekrit-123` and greps captured output and logs).  
  -  **Rollback:** revert commit(s).  
  -  **Done when:** acceptance passes; reviewer closed.  

### [3] Pinned HTTP probe: HEALTHZ, DEEP_PROBE, TLS_EXPIRY

  -  **Status (2026-11-01):** built, tests green, awaiting reviewer (including a bypass attempt on the IP check). Commit on `wt/1-mvp`; evidence in [WIP/streams/wt-1-mvp.md](WIP/streams/wt-1-mvp.md).  
  -  **Severity:** high (SSRF and secret-leak surface)  
  -  **Priority:** high  
  -  **Est:** 6 h  
  -  **Why:** All network checks share one fetcher; it must connect only to validated IPs and never leak the Access token.  
  -  **Action:** `fleet_truth/net.py`: `fetch(url, headers, allow_private, method="GET", body=None, timeout_s=10, max_bytes=65536, resolver=socket.getaddrinfo) -> Response(status, headers, body, peer_ip, cert_not_after, elapsed_ms)` implementing design section 9.1 steps [1]-[5] with stdlib `socket` + `ssl` + `http.client` (subclass `HTTPSConnection`/`HTTPConnection`, override `connect()` to use the pre-validated IP). Never follows redirects. Ignores proxy env. `fleet_truth/checks/http.py`: `HEALTHZ`, `DEEP_PROBE` (adds `CF-Access-Client-Id`/`CF-Access-Client-Secret` only when `auth: access` and scheme is https), `TLS_EXPIRY`, each per design 4.1, 4.2, 4.6, returning `CheckResult(status, verdict, detail, data)`; one retry after 3 s on connect error/timeout/5xx. Port `ip_is_blocked` logic from `/home/dftms/events-app/src/events_app/http.py` (read it; do not import it).  
  -  **Acceptance:**  
       [1]  Unit tests with an injected resolver: refuses `127.0.0.1`, `::1`, `169.254.169.254`, `0.0.0.0`, `::ffff:127.0.0.1`, `2130706433`, `0x7f000001`, `127.1`, a name resolving to one public and one private IP; with `allow_private` accepts `10.x`, `192.168.x`, `100.64.0.10` but still refuses loopback and link-local (and never admits cloud metadata). Loopback is reachable only through the separate per-URL `allow_loopback: true`, which admits just the literal host `localhost` or a loopback IP literal, never a DNS name that resolves to loopback and never any other address class (fix pass 2, review item R10).  
       [2]  A test with a local TLS server (self-signed cert, test-only trust store, loopback allowed via a test-only parameter that config cannot set) proves the TCP peer is the validated IP and SNI/Host are the hostname.  
       [3]  A 302 to `https://x.cloudflareaccess.com/...` yields `ACCESS_REDIRECT` (HEALTHZ) / `AUTH_REJECTED` (DEEP_PROBE); the next hop is never requested.  
       [4]  Body over 64 KB -> `BAD_BODY`; slow-drip server -> `TIMEOUT` within 11 s.  
       [5]  Access headers are absent on `http://` URLs and on apps without `auth: access` (test inspects the raw request).  
       [6]  `detail` never contains header values (test with a known secret).  
  -  **Rollback:** revert commit(s).  
  -  **Done when:** acceptance passes; reviewer closed, including a reviewer attempt to bypass the IP check.  

### [4] Git mirror, DRIFT and UNPUSHED (port the spike)

  -  **Status (2026-11-01):** built, tests green, awaiting reviewer. Commit on `wt/1-mvp`; evidence in [WIP/streams/wt-1-mvp.md](WIP/streams/wt-1-mvp.md).  
  -  **Severity:** high (core value)  
  -  **Priority:** high  
  -  **Est:** 5 h  
  -  **Why:** The drift verdict is the product. The spike proved the logic on a normal clone; the app needs it on a mirror that never touches the owner's working clone.  
  -  **Action:** `fleet_truth/gitmirror.py` per design 6.3: `ensure_mirror(app)` (`git init --bare`, set `core.hooksPath=/dev/null`, `core.fsmonitor=false`), `refresh(app)` (fetch origin with `GIT_ASKPASS` helper + `-c credential.helper=` + `GIT_TERMINAL_PROMPT=0` + `-c protocol.allow=never -c protocol.https.allow=always -c protocol.file.allow=always`; then fetch the working clone's branch into `refs/local/<branch>`), rate-limited by `git.fetch_minutes`, recording `last_fetch_ok` epoch in a file in the mirror dir. `fleet_truth/checks/drift.py`: move `spike/check_drift.py` logic in, parameterised by ref names (`refs/remotes/origin/<b>`, `refs/local/<b>`) and fetch age from `last_fetch_ok`; keep the spike file as-is. `UNPUSHED` per design 4.4 including dirty files via `git -C <clone> --no-optional-locks status --porcelain`. Show subjects of unpushed commits in `data` (max 10, 120 chars each) for the page, not stored beyond the run's result row.  
  -  **Acceptance:**  
       [1]  All spike tests pass against the new module (ported, same fixtures) plus: mirror refresh against a `file://` bare origin; unpushed commit present only in the working clone is detected through `refs/local`; `LIVE_NOT_ON_ORIGIN` when the live commit is that unpushed commit.  
       [2]  After a full test run the working clone's `.git` has no new files, no changed refs and no `index.lock` (test snapshots `.git` file list and ref values before/after).  
       [3]  A test with a repo that has a `post-checkout`/`fsmonitor` hook configured proves nothing executes.  
       [4]  The token never appears in the mirror's `config`, in `ps`-visible args, or in logs (test with a fake token and a fake https remote that fails).  
  -  **Rollback:** revert commit(s); delete `data/mirrors/` (it is a cache).  
  -  **Done when:** acceptance passes; reviewer closed.  

### [5] File checks: BACKUP_AGE, HEARTBEAT, STALE_BUILD

  -  **Status (2026-11-01):** built, tests green, awaiting reviewer. Commit on `wt/1-mvp`; evidence in [WIP/streams/wt-1-mvp.md](WIP/streams/wt-1-mvp.md).  
  -  **Severity:** medium  
  -  **Priority:** high  
  -  **Est:** 3 h  
  -  **Why:** Backups exist in four apps and nothing watches their age; investo-bot already writes a heartbeat file.  
  -  **Action:** `fleet_truth/checks/files.py` per design 4.5, 4.7, 4.8. `BACKUP_AGE` reproduces `_check_status` from `/home/dftms/worktrees/events-app/wt-67-backup/deploy/backup/backup_lib.py` (read lines 489-560) without importing it; running-backup guard opens `<dir>/lock` only if it exists, `O_RDONLY`, `flock(LOCK_SH|LOCK_NB)`. `HEARTBEAT` reads a JSON file, dotted `field`, accepts epoch seconds or ISO-8601 with offset. `STALE_BUILD` reads `builtAt` from the DEEP_PROBE result.  
  -  **Acceptance:**  
       [1]  Fixtures for each `BACKUP_AGE` verdict (`NEVER_RAN`, `WRONG_APP`, `FAILED`, `STALE`, ok) produce the same ok/not-ok as the library's `_check_status` on the same files (a test imports the library from the worktree path if present, else skips with a reason).  
       [2]  A held flock on `lock` (taken by the test) yields `RUNNING` / info; the check never creates `lock` or any other file in the state dir.  
       [3]  `STALE_BUILD` returns `unknown` (no alert) for date-only `builtAt`.  
  -  **Rollback:** revert commit(s).  
  -  **Done when:** acceptance passes; reviewer closed.  

### [6] SQLite store, cycle runner, flap guard

  -  **Status (2026-11-01):** built, tests green, awaiting reviewer. Commit on `wt/1-mvp`; evidence in [WIP/streams/wt-1-mvp.md](WIP/streams/wt-1-mvp.md).  
  -  **Severity:** high (bad state logic = alert spam or silence)  
  -  **Priority:** high  
  -  **Est:** 5 h  
  -  **Why:** Checks are only useful with history, confirmation and a cycle that always finishes.  
  -  **Action:** `fleet_truth/store.py` with the schema in design section 5 (`PRAGMA journal_mode=WAL`, `user_version` migrations). `fleet_truth/cycle.py`: per design 6.4 -- thread pool of 4, per-app order, hard deadline 80% of interval, exceptions -> `unknown`, `DRIFT` = `unknown` when `DEEP_PROBE` failed. `check_state` transitions: consecutive counter, `confirm`, `unknown` 30-min rule, recovery, reminder times (crit 6 h, warn daily). Correlation rules from design section 12 (all `auth: access` apps `AUTH_REJECTED` -> one monitor alert; all network checks failing -> one "monitor offline"). Output: a list of `AlertDecision`s; sending is card [7]. `fleet-truth once` runs one cycle and exits with 0 ok/info, 1 warn, 2 crit, 3 config error. Retention pruning.  
  -  **Acceptance:**  
       [1]  Table-driven tests over a fake clock: fail, fail -> one alert; fail, ok -> no alert; crit for 7 h -> alert + 1 reminder; recovery -> one recovery alert; `unknown` 25 min -> no alert, 31 min -> one "blind" alert.  
       [2]  Correlation tests: 5 apps `AUTH_REJECTED` together -> exactly 1 decision.  
       [3]  A check that sleeps past the deadline is recorded `TIMEOUT_CYCLE` and the cycle returns on time.  
       [4]  Deleting the DB file and re-running recreates it (rebuildable).  
  -  **Rollback:** revert commit(s); delete the DB file.  
  -  **Done when:** acceptance passes; reviewer closed.  

### [7] Alerts and dead-man ping

  -  **Status (2026-11-01):** built with stub-transport tests; real `[TEST]` on the owner's channel (Q3) still open; awaiting reviewer. Commit on `wt/1-mvp`; evidence in [WIP/streams/wt-1-mvp.md](WIP/streams/wt-1-mvp.md).  
  -  **Severity:** high  
  -  **Priority:** high  
  -  **Est:** 4 h  
  -  **Why:** A monitor nobody hears is a log file; a monitor that dies quietly is worse than none.  
  -  **Action:** `fleet_truth/alerts.py`: senders `telegram`, `ntfy`, `email` (SMTP STARTTLS), `webhook` (JSON and `healthchecks` mode) per design 6.5, each through `net.fetch`-style pinned connections where they are HTTP (Telegram/ntfy/webhook are public hosts; `allow_private` false). `min_status` filter per channel. Record every send in `alerts`. Dead-man per design section 10: ping on a completed cycle with at least one channel healthy; `/fail` on a cycle error. Message text per design 6.5, max 500 chars, no secrets.  
  -  **Acceptance:**  
       [1]  Tests with a stub transport: each sender builds the documented request; Telegram token appears only in the URL path sent to `api.telegram.org`, never in `alerts.error` or logs.  
       [2]  All channels failing -> dead-man ping withheld (test).  
       [3]  `fleet-truth test-alert` subcommand sends one `[TEST]` message to every configured channel and prints per-channel ok/failed.  
  -  **Rollback:** revert commit(s); remove the alert env vars to silence it.  
  -  **Done when:** acceptance passes; owner received a real `[TEST]` on his chosen channel (open question Q3); reviewer closed.  


## Backlog Index

  -  [1] Scaffold: package, CLI, version, tests -- 2 h  
  -  [2] fleet.yaml loader and validation -- 3 h  
  -  [3] Pinned HTTP probe: HEALTHZ, DEEP_PROBE, TLS_EXPIRY -- 6 h  
  -  [4] Git mirror, DRIFT and UNPUSHED (port the spike) -- 5 h  
  -  [5] File checks: BACKUP_AGE, HEARTBEAT, STALE_BUILD -- 3 h  
  -  [6] SQLite store, cycle runner, flap guard -- 5 h  
  -  [7] Alerts and dead-man ping -- 4 h  
  -  [8] Status page, WSL runbook, first live run -- 5 h  
  -  [9] Apps report a commit to the fleet token (cross-repo) -- 1 d over 5 repos  
  -  [10] Access service token: setup, correlation, expiry -- 3 h  
  -  [11] Docker image and Access-gated exposure -- 6 h  
  -  [12] SPLIT_BRAIN, silences, daily digest -- 5 h  
  -  [13] Adversarial security review of the whole app -- 4 h  
  -  [14] Contracts for other users: version snippet and status-file spec -- 4 h  

## Backlog

### [8] Status page, WSL runbook, first live run

  -  **Severity:** medium  
  -  **Priority:** high (this is the moment it becomes useful)  
  -  **Est:** 5 h  
  -  **Why:** The red/green page and a live run on the real fleet are the weekend's definition of done.  
  -  **Action:** `fleet_truth/web.py` FastAPI: `GET /` (Jinja, one table: rows = apps, columns = checks, cell = status colour + verdict + "since"; header shows last cycle age and turns red past 2 intervals; unpushed subjects listed under the app), `GET /api/status` (JSON of latest `check_state`), `GET /healthz` (`{"ok": true}`), `GET /api/version`. Headers: CSP `default-src 'none'; style-src 'self'; img-src 'self'`, `X-Frame-Options: DENY`, `Referrer-Policy: same-origin`, `Cache-Control: no-store`. No forms, no POST routes. `serve` runs uvicorn on `web.bind:web.port` (default `127.0.0.1:8790`, refusing `0.0.0.0` unless `--i-know-this-is-public`) and the cycle loop in a thread. `wiki/runbook.md`: venv install, env file at `~/.config/fleet-truth/env` (0600), `systemd --user` unit with `Restart=on-failure`, cron fallback, how to stop it. Then the owner-run first live cycle against `fleet.yaml` built from design section 7.  
  -  **Acceptance:**  
       [1]  TestClient tests: page renders with fixtures for every status; a `detail` containing `<script>` is escaped; headers present; any POST returns 405.  
       [2]  Live run on his fleet (owner present): `fleet-truth once` completes in under 60 s, the page shows every app, and at least `UNPUSHED` for invoicing reports the real ahead count (2 on 2026-11-01, or whatever `git rev-list --count origin/main..main` says that day).  
       [3]  Stopping the process for longer than the healthchecks.io grace produces the dead-man alert on the owner's phone.  
  -  **Rollback:** `systemctl --user disable --now fleet-truth` (or remove the cron lines); delete `~/.local/share/fleet-truth`. Nothing on the VPS or GitHub changes.  
  -  **Done when:** acceptance passes, [wiki/session-state.md](wiki/session-state.md) updated, reviewer closed.  

### [9] Apps report a commit to the fleet token (cross-repo)

  -  **Severity:** high (without it DRIFT stays at NO_COMMIT for every app)  
  -  **Priority:** high (first month card)  
  -  **Est:** 1 d across 5 repos  
  -  **Why:** No app reports a live commit today: image workflows never pass `GIT_SHA` (audit HW-21; `skills-n-bills/wiki/dashboard-app.md`; `invoicing/wiki/ops-deploy.md`), and `/api/version` is closed to service tokens (health-web `SERVICE_PATHS`) or does not exist (invoicing, events-app).  
  -  **Action:** One card per app on that app's own board, linking here; executed in that repo under its own rules, not from this repo. Per app: [1] image workflow passes `--build-arg GIT_SHA=${{ github.sha }} --build-arg BUILD_TIME=$(date -u +%FT%TZ)` (workflow-file pushes go through the owner's terminal); [2] origin access middleware allows the fleet token's client id to `GET /api/version` only; [3] events-app: add `GET /api/version` behind the gate (reuse the `version.py` pattern); invoicing: serve `version.info()` from authgate or a tiny route Caddy forwards; [4] `version.py` may keep the 7-char `commit` (Fleet Truth resolves prefixes) but should add `commitFull` when `GIT_SHA` is 40 hex.  
  -  **Acceptance:** for each deployed app, `DEEP_PROBE` ok and `DRIFT` not `NO_COMMIT*` on the page; a test in each app proves a service token other than the fleet id is still refused on `/api/version` and the fleet id is refused on every other path.  
  -  **Rollback:** per app, revert its commit and redeploy the previous image tag (each app's `:prev`/`:rollback` convention).  
  -  **Done when:** every deployed app shows a real DRIFT verdict.  

### [10] Access service token: setup, correlation, expiry

  -  **Severity:** medium  
  -  **Priority:** medium  
  -  **Est:** 3 h  
  -  **Why:** The token is the single key to every deep probe; its expiry or removal must produce one clear alert, not a wall of red.  
  -  **Action:** `wiki/access-setup.md`: owner steps to create `fleet-truth-probe` (Zero Trust > Access > Service credentials), add a Service Auth policy per app, set duration, store id/secret in bws and the env file. Verify the correlation rule from card [6] end to end with a deliberately wrong secret. `MONITOR_SELF` expiry warnings from `credentials.*_expires`.  
  -  **Acceptance:** with a wrong secret, exactly one "service token refused everywhere" alert; expiry 20 days out -> warn on the page.  
  -  **Rollback:** delete the policy entries and the token in Cloudflare (owner step); remove env vars.  
  -  **Done when:** owner confirms the token works on all `auth: access` apps.  

### [11] Docker image and Access-gated exposure

  -  **Severity:** medium  
  -  **Priority:** medium  
  -  **Est:** 6 h  
  -  **Why:** "A single docker image" for other users and for a phone-reachable page.  
  -  **Action:** Multi-stage `Dockerfile` (python slim, non-root uid, read-only root fs, `HEALTHCHECK` on `/healthz`, `GIT_SHA`/`BUILD_TIME` args), compose example mounting repos and backup state read-only and `data/` read-write. Origin JWT verification copied from `/home/dftms/health-web/healthapp/access.py` (fail closed; `/healthz` the only exemption). Exposure via his cloudflared+caddy pattern is an owner step with its own undo.  
  -  **Acceptance:** container passes its own checks; without Access config every route except `/healthz` returns 503; mounts are `:ro` (test: write attempt fails).  
  -  **Rollback:** stop the container; remove the tunnel route (owner).  
  -  **Done when:** page reachable through Access from the phone; reviewer closed.  

### [12] SPLIT_BRAIN, silences, daily digest

  -  **Severity:** medium  
  -  **Priority:** medium  
  -  **Est:** 5 h  
  -  **Why:** The events-app `app-access` incident (two containers, two images) and deploy-window noise.  
  -  **Action:** `SPLIT_BRAIN` per design 4.9; `fleet-truth silence <app>[/<check>] --for 2h` and `unsilence` (CLI only, writes `check_state.silenced_until`); daily 08:00 digest of all `warn` to channels with `min_status: warn`.  
  -  **Acceptance:** fixture with two routes reporting different `build` -> crit; silenced check runs, shows greyed, sends nothing; digest groups warnings into one message.  
  -  **Rollback:** revert commit(s).  
  -  **Done when:** acceptance passes; reviewer closed.  

### [13] Adversarial security review of the whole app

  -  **Severity:** high  
  -  **Priority:** medium (before any exposure or sharing)  
  -  **Est:** 4 h  
  -  **Why:** The app holds a GitHub token and an Access token and makes outbound requests to configured URLs.  
  -  **Action:** Separate reviewer agent, given only [wiki/mvp-plan.md](wiki/mvp-plan.md) "Security checklist", [wiki/design.md](wiki/design.md) section 9 and the code. Findings become cards.  
  -  **Acceptance:** every checklist line has evidence (test name or file:line) or a card.  
  -  **Rollback:** n/a (read-only review).  
  -  **Done when:** no open high findings.  

### [14] Contracts for other users: version snippet and status-file spec

  -  **Severity:** low  
  -  **Priority:** low (only if the owner chooses to share it, open question Q6)  
  -  **Est:** 4 h  
  -  **Why:** Other solo devs do not have his `version.py` or backup library; the product only works for them with a copy-paste contract.  
  -  **Action:** `docs/contract-version.md` (JSON shape `{version, commit, commitFull, build, builtAt}`, FastAPI/Express/Next snippets, Dockerfile build args, GitHub Actions line) and `docs/contract-status-file.md` (the backup library's `status.json` fields as a spec, plus a 10-line shell writer for restic/borg cron jobs).  
  -  **Acceptance:** a fresh agent with only these docs adds the endpoint to a toy app and Fleet Truth reports `IN_SYNC` for it.  
  -  **Rollback:** revert commit(s).  
  -  **Done when:** acceptance passes.  

## Done

  -  **2026-11-01** Planning pack and drift spike ([spike/check_drift.py](spike/check_drift.py), 7 tests).  

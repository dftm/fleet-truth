# Fleet Truth -- design

**Status:** planning pack, 2026-11-01. Nothing built beyond [../spike/check_drift.py](../spike/check_drift.py). Code will be authoritative once it exists; this page is then historical.

## 1. Problem

A solo developer runs 5 to 20 small apps on one or two VPSes, deployed by hand or by a thin CI step. Uptime monitors answer "does the URL return 200?". The failures that actually hurt this fleet were all invisible to that question:

  -  **jobs.dftm.cloud, 2026-09-20.** A commit from another machine (`cca46b3`, fail-closed Access guard) rode along in a local build. Every path returned 503 for minutes; `/healthz` answered 200 throughout because that commit exempts it on purpose. Source: [feedback_check_git_log_before_deploying.md](/home/dftms/.claude/projects/-home-dftms/memory/feedback_check_git_log_before_deploying.md).  
  -  **events-app, 2026-10-22.** The live Sources/Leads feature ran from 4 commits that existed only on the laptop (`git log origin/main..HEAD`: 5636828, fb79b3d, 71ec2c0, bce4129). A disk loss would have lost the code of a running feature. Source: [audit/events-app.md](/home/dftms/worktrees/maintenance-run-20261031/audit/events-app.md) EV header.  
  -  **events-app public 404.** The tailnet container was recreated, the Access-facing `app-access` container was not, so two containers served two different images ([audit EV-01](/home/dftms/worktrees/maintenance-run-20261031/audit/events-app.md)).  
  -  **investo-bot** sat with 34 unpushed commits ([repo-sync-check.py](/home/dftms/claude-self/scripts/repo-sync-check.py) docstring).  
  -  **invoicing, today (2026-11-01):** local `main` is 2 commits ahead of `origin/main` (measured with the spike, read-only).  
  -  Backups exist as code in four apps but none has run live yet; nothing alerts on their age ([HANDOVER-20261101.md](/home/dftms/worktrees/HANDOVER-20261101.md) "Backups" item [9]). Every audit lists "no monitoring beyond `/healthz`" (JB-03, EV-06, SC-09, HW-23).  

Fleet Truth treats **"what runs is not what git says"** as an outage, next to the usual liveness, backup, certificate and heartbeat checks, on one page with one alert stream.

## 2. Users

  [1]  **Demetri (first and design user).** 6 deployed or deployable apps on one Hostinger VPS behind Cloudflare Tunnel + Access, repos private on GitHub, clones on WSL, backups pulled to WSL by the shared backup library. Wants a red/green page and a phone alert, never a dashboard to babysit.  
  [2]  **Later: other solo devs / tiny teams** with a handful of Docker apps on a VPS, GitHub repos, possibly Cloudflare in front. They will not have his backup library; they need a documented status-file contract and a version-endpoint snippet to copy.  

Non-users: Kubernetes shops (Argo CD already does drift there), teams wanting log search or metrics.

## 3. The real fleet today (input for the weekend MVP)

Measured from the repos on 2026-11-01. "commit today" = what `/api/version` would report in production now.

| App | Local clone | Branch | Public host | Version endpoint | commit today | Backup status file |
|---|---|---|---|---|---|---|
| jobs dashboard | `~/skills-n-bills` | `master` | jobs.dftm.cloud | `GET /api/version` behind origin Access guard (`app/jobs/main.py`) | null (workflow passes no `GIT_SHA`) | `~/.local/state/backup-pull/<APP_NAME>/status.json` once `wt/111-backup` lands |
| health-web | `~/health-web` | `main` | health.dftm.cloud | `GET /api/version` behind guard; service tokens limited to `SERVICE_PATHS`, which exclude it (`healthapp/access.py`) | null (audit HW-21) | same pattern, `wt/10006-backup` |
| investo-bot | `~/investo-bot` | `master` | investobot.dftm.cloud | `GET /api/version` behind guard (`web/app.py`) | null | n/a; daemon writes a heartbeat `status.json` and can push `HEARTBEAT_PUSH_URL` (`runtime/daemon.py`) |
| invoicing | `~/invoicing` | `main` | invoicing.dftm.cloud | none: Streamlit, version only in the sidebar (`wiki/ops-deploy.md`) | n/a | same pattern, `wt/63-backup` |
| events-app | `~/events-app` | `main` | events.dftm.cloud | none; `/healthz` on the Access listener is loopback-only (403 from outside, `web.py` `_access_gate`) | n/a | same pattern, `wt/67-backup` |
| spam-calls | `~/spam-calls` | `main` | unverified (not found in docs) | `GET /api/version`, applicant-auth class | null | unverified |
| week-leader (decree) | `~/week-leader` | `main` | not deployed (audit WL-02) | public `GET /api/version` | n/a | none |

**Consequence:** no app reports a live commit today. The weekend MVP must be useful without one (unpushed, backup, liveness, deep probe, version-file comparison), and card [9] in [../KANBAN.md](../KANBAN.md) makes the apps report one.

## 4. Checks

Statuses: `ok`, `info`, `warn`, `crit`, `unknown` (the monitor could not judge: never shown green). Every check returns `(status, verdict, detail, data)`. A check never raises; an exception becomes `unknown` with the exception class in `detail`.

**False-alarm policy, shared by all checks:**

  -  A `warn`/`crit` result alerts only after `confirm` consecutive cycles (default 2; at 5 min that is 5-10 min). `ok` after a failure alerts once ("recovered") immediately.  
  -  `unknown` alerts only if it lasts 30 min, as "monitor is blind for app X", never as "app X is down".  
  -  Network checks retry once inside the cycle after 3 s before recording a failure.  
  -  Silences: `fleet-truth silence <app>[/<check>] --for 2h` (deploy windows). Silenced checks still run and show on the page, greyed.  
  -  Reminders: a still-failing `crit` re-alerts every 6 h, `warn` once a day in a digest.  

### 4.1 `HEALTHZ` -- liveness

  -  **Input:** `health.url`, optional `health.expect_status` (default 200), optional `health.expect_json` (dict of key -> value, all must match).  
  -  **Rule:** pass = expected status within `timeout_s` (default 10) and JSON matches. Redirects are never followed. A 302/303 to `*.cloudflareaccess.com` = verdict `ACCESS_REDIRECT` (the URL is gated and no service token was sent): `unknown`, detail tells the owner to add `auth: access` or bypass the path.  
  -  **Severity:** `crit`.  
  -  **Known limit, printed on the page:** a constant `{"ok": true}` proves only that the process answers. That is why `DEEP_PROBE` exists.  

### 4.2 `DEEP_PROBE` -- a real authenticated path through the full stack

  -  **Input:** `version.url` (normally `/api/version`), sent with the Access service token when `auth: access`.  
  -  **Rule:** pass = 200 with a JSON object body. 401/403 from the origin or a redirect to Access login = `AUTH_REJECTED`; 5xx = `DOWN`; timeout = `TIMEOUT`; non-JSON = `BAD_BODY`.  
  -  **Severity:** `DOWN`, `TIMEOUT`, `BAD_BODY` -> `crit`. `AUTH_REJECTED` -> `crit` with the text "service token refused: either the app's auth broke (jobs 2026-09-20 pattern) or the token expired/was removed from the policy". Both are worth a page; the detail names both causes so the owner checks the token first.  
  -  This is the check that would have caught the jobs 503: the guard refused every non-`/healthz` path.  

### 4.3 `DRIFT` -- live commit vs origin

  -  **Input:** the `DEEP_PROBE` body (`commit`, `version`, `build`, `builtAt`), the app's mirror (section 6.3), `branch`, `version_file`, `drift.grace_hours` (default 24).  
  -  **Rule:** exactly the spike's verdict table ([../spike/check_drift.py](../spike/check_drift.py)):  
| Verdict | Meaning | Status |
|---|---|---|
| `IN_SYNC` | live commit == origin head | ok |
| `PENDING_DEPLOY` | live is an ancestor of origin, oldest undeployed commit younger than grace | info |
| `BEHIND` | same, older than grace | warn |
| `LIVE_NOT_ON_ORIGIN` | live commit is known locally but not reachable from origin: deployed unpushed work | crit |
| `LIVE_UNKNOWN` | live commit not in the mirror at all, or not a 7-40 hex string | crit if the mirror fetched within 6 h, else warn |
| `NO_COMMIT_VERSION_MATCH` | no commit; `version` equals `VERSION` at origin | warn (weak evidence) |
| `NO_COMMIT_VERSION_DIFF` | no commit; versions differ | warn |
| `NO_COMMIT` | no commit, no version | warn |

  -  **False alarms avoided:** short SHAs (the apps report 7 chars) resolve by prefix; an ambiguous prefix is `LIVE_UNKNOWN` not `IN_SYNC`; a stale mirror downgrades `LIVE_UNKNOWN` to warn; a fresh push is `PENDING_DEPLOY` for `grace_hours` instead of `BEHIND`.  
  -  The `commit` value is untrusted input from the network: it reaches git only after the regex `^[0-9a-f]{7,40}$` (tested in the spike against `--output=...`, `HEAD`, branch names).  
  -  If `DEEP_PROBE` failed this cycle, `DRIFT` is `unknown` (no double alert).  

### 4.4 `UNPUSHED` -- local work that exists only on one disk

  -  **Input:** `repo` (local working clone path), `branch`; computed in the mirror after copying the working clone's branch ref in (section 6.3).  
  -  **Rule:** `count` = commits on local `branch` not on origin `branch`. Pass = 0. Also tracked-file changes whose newest mtime is older than `unpushed.dirty_hours` (default 24), the rule of `repo-sync-check.py`.  
  -  **Severity:** `info` while the oldest unpushed commit is younger than `unpushed.max_age_hours` (default 24), then `warn`. Escalates to `crit` only through `DRIFT=LIVE_NOT_ON_ORIGIN` (live code depends on it). Dirty tracked files: `info` only.  
  -  **False alarms avoided:** a branch with no local copy is `unknown`, not 0; detached HEAD and other branches are ignored in MVP.  

### 4.5 `BACKUP_AGE`

  -  **Input:** `backup.status_file` (local path, the backup library's `status.json`), `backup.app_name`, `backup.max_age_hours` (default 26, the library's default).  
  -  **Rule (mirrors `backup_lib._check_status` exactly):** missing file = `NEVER_RAN`; `app` field != `app_name` = `WRONG_APP`; `result != "ok"` = `FAILED` (detail: stage, timestamp, last_ok); `now - last_ok_epoch > max_age_hours` = `STALE`; else ok.  
  -  **Running-backup guard:** before judging, open `<state dir>/lock` read-only if it exists and try `flock(LOCK_SH | LOCK_NB)`; if it is held, the verdict is `RUNNING` (`info`) unless the existing status is already ok. Never create files in the state dir.  
  -  **Severity:** `FAILED`, `STALE`, `WRONG_APP` -> `crit`; `NEVER_RAN` -> `warn` (backups are not live yet; flip to crit with `backup.required: true`).  
  -  **Later (month):** `backup.status_url` for setups where the status file lives on another host, served read-only by the app.  

### 4.6 `TLS_EXPIRY`

  -  **Input:** host and port of `health.url` (or `tls.host`), connect via the validated IP (section 9.1).  
  -  **Rule:** full verification with the system trust store and hostname check; pass = days to `notAfter` > `tls.warn_days` (21).  
  -  **Severity:** < 21 days `warn`, < 7 days `crit`, verification failure `crit`.  
  -  **Honest note:** Cloudflare renews edge certificates for `*.dftm.cloud` itself, so on his fleet this mainly catches edge misconfiguration. It earns its place for other users and for non-Cloudflare hosts.  

### 4.7 `HEARTBEAT` -- jobs that should have run

  -  **Input:** `heartbeat.file` (local JSON) with `heartbeat.field` (epoch or ISO-8601 key, dotted path) or `heartbeat.url` (JSON over the same probe path), `heartbeat.max_age_minutes`.  
  -  **Rule:** pass = timestamp present and younger than max age. Unparseable or missing = `unknown`.  
  -  **Severity:** `crit`.  
  -  Covers investo-bot's daemon `status.json` and `~/.claude/state/jobs.json` from [job-heartbeat.py](/home/dftms/claude-self/scripts/job-heartbeat.py). It does not replace healthchecks.io for cron jobs; it reads files that already exist.  

### 4.8 `STALE_BUILD`

  -  **Input:** `builtAt` from the version body; `build.max_age_days` (default 45).  
  -  **Rule:** pass = full ISO timestamp younger than the limit. A date-only `builtAt` (the mtime fallback in every `version.py`) = `unknown` with detail "BUILD_TIME not set", never an alert.  
  -  **Severity:** `info` (base-image security updates are aging). Never pages.  

### 4.9 `SPLIT_BRAIN` (month)

  -  **Input:** `version.routes`: two or more URLs that reach different containers of the same app (for example the Access-facing gate and the tailnet listener).  
  -  **Rule:** all routes must report the same `build` (and `commit` when present).  
  -  **Severity:** `crit`. Catches the events-app `app-access` not-recreated case (EV-01).  

### 4.10 `MONITOR_SELF`

  -  Last cycle finished, cycle duration < 80% of the interval, mirror fetch errors per repo, alert-channel send errors, and credential expiry dates from config (`credentials.github_token_expires`, `credentials.access_token_expires`): warn at 30 days, crit at 7.  
  -  The dead-man ping (section 10) is the part that works when the monitor is dead.  

## 5. Data model (SQLite, WAL)

```sql
CREATE TABLE runs (
  id INTEGER PRIMARY KEY, started_at INTEGER NOT NULL, finished_at INTEGER,
  ok INTEGER, error TEXT);
CREATE TABLE results (
  run_id INTEGER NOT NULL REFERENCES runs(id), app TEXT NOT NULL, check_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('ok','info','warn','crit','unknown')),
  verdict TEXT NOT NULL, detail TEXT NOT NULL, data_json TEXT NOT NULL DEFAULT '{}',
  observed_at INTEGER NOT NULL, PRIMARY KEY (run_id, app, check_name));
CREATE TABLE check_state (
  app TEXT NOT NULL, check_name TEXT NOT NULL, status TEXT NOT NULL, verdict TEXT NOT NULL,
  since INTEGER NOT NULL, consecutive INTEGER NOT NULL, alerted_status TEXT,
  last_alert_at INTEGER, silenced_until INTEGER, PRIMARY KEY (app, check_name));
CREATE TABLE alerts (
  id INTEGER PRIMARY KEY, app TEXT, check_name TEXT, status TEXT, channel TEXT NOT NULL,
  sent_at INTEGER NOT NULL, ok INTEGER NOT NULL, error TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- schema_version, last_dead_man_ok
```

  -  `fleet.yaml` is the source of truth for apps; there is no `apps` table. Removing an app from the file stops its checks; its history stays until retention deletes it.  
  -  Retention: `results` 30 days, `runs` 90 days, `alerts` 365 days; pruned at the end of a cycle.  
  -  `detail` and `data_json` never hold headers, tokens or response bodies beyond the parsed version fields (max 2 KB each).  
  -  The DB is rebuildable: losing it loses history and alert de-duplication only. It is not backed up in MVP.  

## 6. Architecture

### 6.1 Stack

  -  Python 3.12+, FastAPI + Jinja2 for one server-rendered page and one JSON endpoint, SQLite (stdlib `sqlite3`), PyYAML. Probes use stdlib `http.client`/`ssl` over a pinned socket (section 9.1), so no HTTP client library is needed. Git through the `git` binary via `subprocess` with argument lists.  
  -  Same habits as his other apps: `version.py` + `VERSION` + `GET /api/version` + `/healthz` on Fleet Truth itself (it should pass its own checks), `pytest`, wiki in repo.  
  -  One process, two entry points: `fleet-truth once` (one cycle, exit code = worst status, for cron/systemd timers) and `fleet-truth serve` (web UI + the loop in a background thread, interval from config, default 300 s).  
  -  Packaging: a `pyproject.toml` console script for the weekend; a single Docker image in the month version.  

### 6.2 Where it runs

  -  **MVP: on WSL, next to the clones and the backup state.** `UNPUSHED` and `BACKUP_AGE` need the laptop's disk; the backup library is a pull model, so its `status.json` lives on WSL, not the VPS. A monitor on the VPS could not see either.  
  -  The cost: when the laptop sleeps, nothing is checked. The dead-man ping turns that into an explicit "monitor silent" alert from healthchecks.io after its grace period, not a false sense of green.  
  -  **Later:** split into a collector (laptop: unpushed, backup files) that pushes signed results to a server (VPS or hosted), with remote probes run server-side. Not in the month plan.  

### 6.3 Git mirror per app

Fleet Truth never fetches into, writes to, or takes locks in the owner's working clone.

  -  `data/mirrors/<app>.git` is a bare repo Fleet Truth owns. Each cycle (at most every `git.fetch_minutes`, default 15):  
       [1]  `git fetch --prune origin +refs/heads/<branch>:refs/remotes/origin/<branch>` with the read-only GitHub token supplied through `GIT_ASKPASS` (section 8.2), never written into the mirror's config.  
       [2]  `git fetch --no-tags <working clone path> +refs/heads/<branch>:refs/local/<branch>`: reads the laptop's commits into the mirror, including unpushed ones, without touching the working clone.  
  -  All ancestry questions (`DRIFT`, `UNPUSHED`) are then answered inside the mirror, which has every object. The spike runs the same logic against a normal clone; the builder swaps `origin/<branch>` for `refs/remotes/origin/<branch>` and the local branch for `refs/local/<branch>`.  
  -  Dirty-file detection runs `git -C <working clone> --no-optional-locks status --porcelain` (read-only, takes no index lock).  
  -  A repo with no local clone (`repo:` omitted) is mirrored from GitHub only; `UNPUSHED` is then skipped.  

**Probing local apps (added with fix pass 2):** Fleet Truth's own `/healthz` and any app on the same host live on `127.0.0.1`. `allow_private` still refuses loopback (section 9.1); a URL may set the separate, explicit `allow_loopback: true`, valid only when its host is the literal `localhost` or a loopback IP literal. `localhost` is not resolved through DNS, a name that resolves to loopback stays refused, and the flag lifts nothing else (no private ranges, no metadata, no link-local).  

### 6.4 Cycle

```
load fleet.yaml (validate) -> for each app, in a thread pool of 4:
   mirror refresh (if due) -> HEALTHZ -> DEEP_PROBE -> DRIFT (uses probe body) -> UNPUSHED
   -> BACKUP_AGE -> TLS_EXPIRY (every 6 h) -> HEARTBEAT -> STALE_BUILD
-> write results -> update check_state -> decide alerts -> send -> prune -> dead-man ping (success only)
```
A cycle has a hard deadline of 80% of the interval; apps still running are recorded `unknown` with `TIMEOUT_CYCLE`.

### 6.5 Alert channels

All optional; any number. One message format: `[CRIT] jobs DEEP_PROBE: 503 from /api/version (since 10:05, 2 checks)` plus the page URL.

  -  `ntfy`: POST to `https://ntfy.sh/<topic>` or a self-hosted server; priority 5 for crit, 3 for warn. Optional access token from env.  
  -  `telegram`: Bot API `sendMessage`; bot token and chat id from env. He already uses Telegram for `job-heartbeat.py`.  
  -  `email`: SMTP with STARTTLS; password from env.  
  -  `webhook`: generic JSON POST (`{app, check, status, verdict, detail, since, url}`), plus a `healthchecks` mode that maps crit to `<ping-url>/fail` and recovery to `<ping-url>`, so a healthchecks.io or Uptime Kuma push monitor can relay.  
  -  A failed send is logged in `alerts` and retried next cycle; three failed sends in a row on every channel raise `MONITOR_SELF` crit, which the dead-man covers (the ping is skipped while all channels fail).  

## 7. Configuration: `fleet.yaml`

Only non-secret data. Secrets are referenced by environment variable name and read at runtime.

```yaml
# fleet.yaml -- the whole fleet. Secrets are NEVER in this file: *_env names an environment variable.
interval_seconds: 300
confirm: 2                      # consecutive failures before alerting
data_dir: ~/.local/share/fleet-truth
web: {bind: 127.0.0.1, port: 8790}

credentials:
  github_token_env: FLEET_GITHUB_TOKEN          # fine-grained, Contents: read-only, selected repos
  github_token_expires: 2027-01-30              # entered by hand; MONITOR_SELF warns 30 d before
  access_client_id_env: FLEET_CF_ACCESS_CLIENT_ID
  access_client_secret_env: FLEET_CF_ACCESS_CLIENT_SECRET
  access_token_expires: 2027-11-01

alerts:
  - {type: ntfy, url: https://ntfy.sh, topic_env: FLEET_NTFY_TOPIC, min_status: warn}
  - {type: telegram, bot_token_env: FLEET_TG_TOKEN, chat_id_env: FLEET_TG_CHAT, min_status: crit}
dead_man: {url_env: FLEET_DEADMAN_URL}            # healthchecks.io check, period 5 min, grace 15 min

defaults:
  drift: {grace_hours: 24}
  unpushed: {max_age_hours: 24, dirty_hours: 24}
  backup: {max_age_hours: 26, required: false}
  tls: {warn_days: 21, crit_days: 7, every_hours: 6}

apps:
  - name: jobs
    github: dftm/skills-n-bills
    branch: master
    repo: ~/skills-n-bills
    version_file: app/jobs/VERSION
    health: {url: https://jobs.dftm.cloud/healthz, expect_json: {ok: true}}
    version: {url: https://jobs.dftm.cloud/api/version, auth: access}
    backup: {status_file: ~/.local/state/backup-pull/jobs/status.json, app_name: jobs}

  - name: health-web
    github: dftm/health-web
    branch: main
    repo: ~/health-web
    version_file: healthapp/VERSION
    health: {url: https://health.dftm.cloud/healthz}
    version: {url: https://health.dftm.cloud/api/version, auth: access}
    backup: {status_file: ~/.local/state/backup-pull/health-web/status.json, app_name: health-web}

  - name: investo-bot
    github: dftm/investo-bot
    branch: master
    repo: ~/investo-bot
    version_file: web/VERSION
    health: {url: https://investobot.dftm.cloud/healthz}
    version: {url: https://investobot.dftm.cloud/api/version, auth: access}

  - name: invoicing                      # no version route: liveness, unpushed and backup only
    github: dftm/invoicing
    branch: main
    repo: ~/invoicing
    health: {url: https://invoicing.dftm.cloud/healthz}     # path unverified: see open-questions Q4
    backup: {status_file: ~/.local/state/backup-pull/invoicing/status.json, app_name: invoicing}

  - name: events-app                     # no version route; /healthz is loopback-only on the Access listener
    github: dftm/events-app
    branch: main
    repo: ~/events-app
    health: {url: http://100.100.40.10:8780/healthz, allow_private: true}   # tailnet break-glass listener
    backup: {status_file: ~/.local/state/backup-pull/events-app/status.json, app_name: events-app}
```

  -  The `APP_NAME` values in the backup paths are unverified until the backup branches land; the builder copies them from each app's backup env file.  
  -  Validation at load (pydantic or hand-rolled): unknown keys are errors; URLs must be `http`/`https`; `http` is allowed only with `allow_private: true`; names match `^[a-z0-9][a-z0-9-]{0,40}$`; each `*_env` must be set when its feature is used, else the app's affected checks are `unknown` with "env FLEET_... not set" (never a crash).  
  -  `fleet-truth check-config fleet.yaml` validates and prints the resolved plan without probing.  

## 8. Auth model

### 8.1 Cloudflare Access (outbound, to his apps)

  -  One Access **service token**, `fleet-truth-probe`, created by the owner in the Zero Trust dashboard. Its client id and secret live only in the env file Fleet Truth reads (mode 0600, outside the repo) or in bws, injected at start.  
  -  Each app's Access application gets a policy with action **Service Auth** including that token. Requests carry `CF-Access-Client-Id` and `CF-Access-Client-Secret` headers ([Cloudflare service tokens](https://developers.cloudflare.com/cloudflare-one/identity/service-tokens/)).  
  -  **Origin side, per app (outside this repo):** his apps verify the Access JWT at the origin and treat service tokens as `common_name = <client id>` with a path allowlist (`health-web` `SERVICE_PATHS`). Each app must allow that one client id to `GET /api/version` and nothing else. That is a small change per repo, listed as card [9]. Leak impact of the token: version metadata of each app, nothing more.  
  -  The token is sent only to hosts listed under an app with `auth: access`, only over HTTPS, and never on a redirect (redirects are not followed).  

### 8.2 GitHub (outbound, read-only)

  -  A **fine-grained personal access token**, resource owner `dftm`, only the selected repos in `fleet.yaml`, repository permission **Contents: Read-only** (Metadata read-only is implied), expiry 90 days or more ([GitHub fine-grained PATs](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)). No write scope, no account scopes.  
  -  Passed to git through a tiny `GIT_ASKPASS` helper that prints the env var; `git -c credential.helper=` disables any stored helper so the owner's own GitHub credential is never used or cached by Fleet Truth. Public repos need no token.  
  -  Fleet Truth never stores app credentials: no app passwords, no admin tokens, no feed tokens, no SSH keys. It does not ssh to the VPS and does not talk to Docker.  

### 8.3 Inbound (who may see the page)

  -  MVP binds `127.0.0.1` only; the page and `/api/status` are read-only GETs. Nothing to authenticate.  
  -  When exposed (month), it goes behind Cloudflare Access like his other apps, with origin JWT verification that **fails closed** (copy `health-web/healthapp/access.py`, including its "missing config refuses every request" rule) and `/healthz` as the only exempt path.  

## 9. Threat model

| Asset | Threat | Mitigation |
|---|---|---|
| GitHub token, Access service token, alert tokens | Leak through logs, DB, page, error messages, git config | Env only; redaction filter on every log line and `detail` (replace any env secret value with `***`); never in `fleet.yaml`, the DB or the mirror config; tests assert absence |
| The monitor's network position (laptop on tailnet, VPS later) | SSRF: a URL in config (or, hosted, a customer URL) aimed at `169.254.169.254`, `localhost`, the tailnet or the LAN | Connect-by-validated-IP (9.1) |
| Version endpoint response | Malicious or broken body: huge, slow, non-JSON, hostile `commit` | 64 KB cap, 10 s total deadline, JSON object required, hex regex before git, values HTML-escaped by Jinja autoescape |
| Status page | XSS from `detail` text; clickjacking | Autoescape on, strict CSP (`default-src 'none'; style-src 'self'`), no inline script, `X-Frame-Options: DENY` |
| Future write endpoints (silence, ack) | CSRF | Month version only: POST + `SameSite=Strict` session cookie behind Access + `Origin` header must equal the page origin + per-form token; MVP has no write endpoints at all |
| Mirrors | A repo's hooks or config executing code | Bare mirrors, `core.hooksPath=/dev/null`, `protocol.allow=never` except `https` and `file`, `-c core.fsmonitor=false`; never check out a work tree |
| Working clones | Corruption or lock contention with the owner's git use | Read-only access only: `fetch` from the clone path into the mirror, `--no-optional-locks` status |
| Alert channel | Spoofed or flooded alerts | Outbound only; flap guard and reminder caps |

### 9.1 Connect-by-validated-IP

The idea his apps already use (`events_app/http.py` `ip_is_blocked` + `check_request`, `skills-n-bills/app/jobs/sources/fetch.py` `_check_address_safety`) taken one step further, because a check-then-connect gap allows DNS rebinding (`fetch.py` line 27 names that gap).

  [1]  Parse the URL; scheme must be `https`, or `http` with `allow_private: true`. Reject userinfo, and numeric legacy host spellings (`2130706433`, `0x7f000001`, `127.1`) unless written as dotted quads.  
  [2]  Resolve with `getaddrinfo`; refuse if **any** answer is blocked: not `is_global`, or loopback, link-local, multicast, reserved, unspecified, IPv4-mapped/6to4 forms of those. `allow_private: true` (per app, owner-set) permits only RFC 1918 and `100.64.0.0/10` (tailnet), never loopback, link-local or metadata.  
  [3]  Connect a socket to that exact IP; wrap TLS with `server_hostname=<host>` and full verification; send `Host: <host>`. The resolved IP is the one used: no second lookup.  
  [4]  No redirects followed, ever; a 3xx is a result.  
  [5]  Ignore `HTTP(S)_PROXY` env for probes (a proxy would re-resolve and defeat pinning; the handover flagged this for events-app). If the host requires an egress proxy, the owner sets `probe_via_proxy: true` and loses private-target protection; documented, off by default.  

## 10. Who watches the watcher

  -  **Dead-man ping:** after every completed cycle with at least one working alert channel, GET the healthchecks.io ping URL (env `FLEET_DEADMAN_URL`). healthchecks.io alerts the owner (email/Telegram configured there) when pings stop: laptop asleep, process crashed, cycle hung. Free tier covers it ([pricing](https://healthchecks.io/pricing/)). Any healthchecks-compatible URL works (self-hosted Healthchecks, Uptime Kuma push).  
  -  A failing cycle pings `<url>/fail` with the error class.  
  -  Process supervision: `systemd --user` unit with `Restart=on-failure` in MVP (systemd user units on this WSL are untested per the handover, so the runbook also gives a cron `@reboot` + `once` every 5 min fallback).  
  -  The page shows "last cycle N s ago" in the header and turns the whole header red when it is older than 2 intervals.  

## 11. Privacy

  -  Data collected: HTTP status codes, latency, version JSON fields, commit SHAs and counts, commit timestamps, backup status fields, cert dates. No commit messages, file contents, author emails, response bodies or request logs are stored.  
  -  Commit subjects of unpushed commits are shown on the page (fetched live from the mirror, not stored) because "which work is at risk" is the point; hosted tier would make this opt-in.  
  -  Nothing leaves the machine except probes to the owner's own apps, git fetches from GitHub, alerts to configured channels and the dead-man ping (which carries no data).  

## 12. Failure modes of the monitor itself

| Failure | Effect | Detection |
|---|---|---|
| Laptop asleep / WSL stopped | No checks | Dead-man alert after grace |
| GitHub unreachable or token expired | Mirror stale, `DRIFT` may become `LIVE_UNKNOWN` | Fetch error in `MONITOR_SELF`; stale-mirror rule downgrades to warn; expiry warning from config date |
| Access token expired or removed from policy | Every `DEEP_PROBE` `AUTH_REJECTED` at once | Correlation rule: if all `auth: access` apps fail with `AUTH_REJECTED` in one cycle, send ONE alert "service token refused everywhere" instead of N |
| Network down at the laptop | All network checks fail at once | Same correlation: all network checks failing together = one `MONITOR_SELF` "monitor offline" alert, app alerts suppressed for that cycle |
| SQLite locked or corrupt | Cycle error | Cycle fails -> dead-man `/fail`; DB is rebuildable (delete and restart) |
| All alert channels failing | Silent alerts | Dead-man ping withheld, so healthchecks.io alerts |
| Clock skew | Wrong ages | Ages use the local clock consistently; `MONITOR_SELF` warns if the `Date` header of probed apps differs from local time by > 5 min |

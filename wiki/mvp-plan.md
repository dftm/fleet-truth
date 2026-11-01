# MVP plan

Builds on [design.md](design.md). Cards with full acceptance criteria and rollback lines: [../KANBAN.md](../KANBAN.md). This page says what each stage must achieve, how it is tested and what security bar it must clear.

## Weekend MVP

**Definition of useful:** one `fleet-truth once` on WSL, every 5 minutes, that tells Demetri on his phone within 10 minutes when any of these is true on his real fleet, and shows all of it on one local page:

  -  an app's `/healthz` stops answering (jobs, health-web, investo-bot, invoicing; events-app through the tailnet listener);  
  -  an authenticated path fails although `/healthz` is green (the jobs 2026-09-20 incident) -- for the apps whose `/api/version` the fleet token can reach; until card [9] lands that may be none, and the page says so;  
  -  a local branch holds commits not on GitHub for more than 24 h (invoicing is 2 ahead today);  
  -  a backup's `status.json` says failed or is older than 26 h (once the backup branches land; `NEVER_RAN` warns until then);  
  -  the monitor itself stops (healthchecks.io dead-man).  

`DRIFT` ships in the weekend and runs, but on today's fleet it reports `NO_COMMIT_VERSION_MATCH/DIFF` or nothing, because no app reports a commit yet (design section 3). That is expected, shown honestly as `warn`, and fixed by card [9].

### Build order and acceptance (summary; full text on the cards)

| Task | Card | Est | Accepted when |
|---|---|---|---|
| Scaffold | [1] | 2 h | `pip install -e .[dev] && pytest` green from a fresh clone; sockets blocked in tests |
| Config | [2] | 3 h | `check-config fleet.example.yaml` lists 5 apps; 7 invalid-config cases rejected; no env value ever printed |
| Probe + HTTP checks | [3] | 6 h | SSRF refusal table passes; peer IP == validated IP; redirects never followed; Access headers only on https `auth: access` |
| Mirror + DRIFT + UNPUSHED | [4] | 5 h | spike tests pass on the mirror; working clone byte-for-byte unchanged after tests; hooks never run; token absent from config/args/logs |
| File checks | [5] | 3 h | verdicts agree with `backup_lib._check_status` on fixtures; held lock -> `RUNNING`; no files created in state dir |
| Store + cycle | [6] | 5 h | flap/recovery/reminder/blind table passes on a fake clock; correlation collapses N to 1; deadline respected |
| Alerts + dead-man | [7] | 4 h | stub-transport tests; `test-alert` reaches the owner's phone; dead-man withheld when all channels fail |
| Page + runbook + live run | [8] | 5 h | escaping/headers/405 tests; live cycle < 60 s on his fleet; dead-man fires when stopped |

Total about 33 h of builder time: a long weekend for an agent swarm (one builder per card, cards [3]-[5] can run in parallel after [2]), plus about 1 h of owner time for card [8] live steps and the healthchecks.io/Telegram setup.

### Owner steps the builders cannot do

  [1]  Create a healthchecks.io check (period 5 min, grace 15 min) and put its ping URL in the env file.  
  [2]  Create the fine-grained GitHub token (Contents: read-only, the 7 repos) and put it in the env file.  
  [3]  Choose the alert channel and put its token/topic in the env file.  
  [4]  Write `fleet.yaml` from [../fleet.example.yaml](../fleet.example.yaml) once card [2] ships it (or accept the builder's draft from design section 7).  

## One-month version

Cards [9]-[14], in this order: [9] apps report a commit (the step that turns `DRIFT` on), [10] Access token runbook and correlation, [12] split-brain + silences + digest, [11] Docker image and Access-gated page reachable from the phone, [13] full adversarial security review before exposure, [14] contracts for other users only if the owner wants to share it (open question Q6).

At the end of the month: every deployed app shows a real `DRIFT` verdict, the page is reachable on the phone behind Access, the events-app two-container case is detected, and deploy windows can be silenced.

Not in the month: a GitHub App, multi-user accounts, a hosted service, a collector/server split, metrics graphs.

## Test strategy

  -  **No network in tests.** `tests/conftest.py` blocks `getaddrinfo` and `create_connection`; tests that need a server opt in to loopback through a test-only parameter that the config loader cannot set (a test proves `fleet.yaml` cannot reach it).  
  -  **Git:** real `git` against throwaway repos in `tmp_path`: a bare "origin", a working clone, the mirror; commit times fixed through `GIT_AUTHOR_DATE`/`GIT_COMMITTER_DATE` (pattern in [../spike/test_check_drift.py](../spike/test_check_drift.py)); `GIT_CONFIG_GLOBAL=/dev/null` so the developer's git config cannot leak in.  
  -  **Time:** every check and the cycle take `now` as a parameter; no test sleeps longer than the 3 s retry, and that one uses a monkeypatched sleep.  
  -  **HTTP:** a local `ThreadingHTTPServer` (plain and TLS with a generated self-signed cert) with handlers for 200, 302-to-Access, 503, huge body, slow drip, non-JSON.  
  -  **Secrets:** a fixture sets every `*_env` var to a recognisable value (`SEKRIT-<name>`); an autouse check fails the test if that value appears in captured stdout/stderr, log records, the SQLite file or the mirror's config.  
  -  **Contract tests against his apps' code (read-only):** `BACKUP_AGE` compared with `backup_lib._check_status` on the same fixtures when the worktree exists; skipped with a reason otherwise.  
  -  **Live verification (owner present, card [8]):** one real cycle, a deliberate stop to see the dead-man, `fleet-truth test-alert`.  
  -  **Clean-clone rule** from events-app: before declaring a card done, run the suite from a fresh `git clone` of this repo (a `.gitignore` rule once hid fixtures there).  

## Security checklist

Each line needs evidence (test name or file:line) at card [13] review.

  -  [ ]  No secret in `fleet.yaml`, the DB, the mirror config, logs, page, `/api/status`, alert text or process arguments.  
  -  [ ]  Env file mode 0600, outside the repo; `.gitignore` covers `fleet.yaml`, `*.env`, `.env*`, `data/`.  
  -  [ ]  GitHub token is fine-grained, Contents read-only, selected repos only; git never uses the owner's credential helper.  
  -  [ ]  Access service token sent only to `auth: access` apps, only over HTTPS, never across a redirect.  
  -  [ ]  Every outbound connection goes to a validated IP (section 9.1 of the design); loopback, link-local and metadata refused even with `allow_private`.  
  -  [ ]  No redirect followed anywhere (probes, alerts, dead-man).  
  -  [ ]  Response size and time capped; live `commit` regex-checked before reaching git; all git calls use argument lists.  
  -  [ ]  Mirrors: no hooks, no fsmonitor, only https and file protocols, never a work tree.  
  -  [ ]  Working clones: read-only access proved by a before/after `.git` snapshot test.  
  -  [ ]  Page: autoescape, CSP without inline script, `X-Frame-Options: DENY`, no state-changing routes in MVP; bind 127.0.0.1 by default.  
  -  [ ]  When exposed: origin Access JWT verification fails closed; `/healthz` the only exempt path.  
  -  [ ]  Fleet Truth does not ssh, does not talk to Docker, does not store any app's own credentials.  
  -  [ ]  Dependencies pinned with hashes (`pip-compile --generate-hashes`) before the Docker image ships.  

## Plan B (if the owner does not want an app)

About 2 hours: run `spike/check_drift.py` per app from cron with a fixture fetched by `curl` with the Access headers, plus `repo-sync-check.py` and `backup_pull.py --check-status`, piping non-zero exits to the existing `job-heartbeat.py` Telegram path. No page, no flap guard, no correlation; three alert streams. It covers the incidents but will be noisy after every push.

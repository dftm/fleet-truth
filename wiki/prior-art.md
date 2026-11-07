# Prior art and competitors

Scan done 2026-11-01. "Checked" means a source was read today; "unverified" means from general knowledge or a third-party page, to be confirmed before it is quoted anywhere public.

## Summary

Nothing found that compares a running app's commit with its git remote **and** with the developer's unpushed local work, outside Kubernetes. Uptime tools can assert a static value in a JSON body; GitOps tools do drift for clusters; repo-hygiene scripts look at disks, not at production. Fleet Truth's edge is joining those three, plus backup age, in one verdict per app. The edge is narrow and easy to copy (see the end of this page).

## Tools

| Tool | What it does | Overlap with Fleet Truth | Gap | Source |
|---|---|---|---|---|
| Uptime Kuma | Self-hosted uptime monitor: HTTP(s), keyword, JSON Query, TCP, DNS, Docker container and push monitors; cert-expiry warnings on HTTP(s); many notification channels; status pages | `HEALTHZ`, `TLS_EXPIRY`, `HEARTBEAT` (push), custom headers allow the Access service token | JSON Query compares against a **static** expected value; no notion of origin head, ancestry, unpushed commits or backup status files | [repo](https://github.com/louislam/uptime-kuma), [Internal API wiki](https://github.com/louislam/uptime-kuma/wiki/Internal-API) (push `/api/push/<token>`), [ansible module docs](https://github.com/lucasheld/ansible-uptime-kuma/wiki/monitor); monitor count varies by source (23 vs 30+): unverified |
| Healthchecks.io | Dead-man's-switch for cron jobs: alerts when pings stop; BSD-3 open source and self-hostable | Fleet Truth uses it as its own dead-man; `BACKUP_AGE`/`HEARTBEAT` overlap only if every job pings | Passive only: never probes, never reads git | [pricing](https://healthchecks.io/pricing/) (checked: Hobbyist $0 / 20 jobs, Supporter $5 / 20, Business $20 / 100, Business Plus $80 / 1000), [repo + license](https://github.com/healthchecks/healthchecks) (checked: BSD 3-clause, Telegram and email integrations; ntfy not named in the README) |
| Gatus | Config-file status page and alerting: conditions on `[STATUS]`, `[BODY]` with JSONPath, `[CERTIFICATE_EXPIRATION]`, `[DOMAIN_EXPIRATION]`; ntfy/Telegram/email/custom alerts; SQLite or Postgres storage; "external endpoints" accept pushed results | Closest in shape (YAML fleet, developer-oriented). Suites (alpha) can carry a value from one response into a later condition via `[CONTEXT]` | Cannot ask git anything; a suite could compare two URLs' bodies, but origin head is not a URL it can reach without a GitHub API call and auth, and unpushed work is on a disk. A Fleet Truth-style script could push verdicts into Gatus external endpoints | [README](https://raw.githubusercontent.com/TwiN/gatus/master/README.md) (checked) |
| Beszel | Lightweight host and container metrics (CPU, memory, disk, network, S.M.A.R.T.) with alerts | None directly; complements | No HTTP version checks, git or backup verification | [repo](https://github.com/henrygd/beszel) (checked) |
| Dozzle | Live web viewer for Docker container logs | None | Does not store logs or alert on deploy state | [repo](https://github.com/amir20/dozzle) (checked) |
| Watchtower | Auto-updates running containers when a new image tag appears | Inverse problem: it makes prod follow the registry | Archived 2025-12-17 (unmaintained); community fork `nickfedor/watchtower` reported active (unverified). Registry tag != git commit | [repo](https://github.com/containrrr/watchtower), [linuxiac report](https://linuxiac.com/docker-update-tool-watchtower-reaches-end-of-maintenance/) |
| Renovate Dependency Dashboard | A GitHub issue listing pending dependency updates per repo | "Things not yet shipped" view, but for dependencies | Says nothing about what is deployed | [docs](https://docs.renovatebot.com/key-concepts/dashboard/) (unverified today) |
| Argo CD / Flux | GitOps controllers: report `OutOfSync` when cluster state differs from a git revision, optionally self-heal | The exact idea of drift-as-a-signal | Kubernetes only; his fleet is Docker compose on one VPS | [Argo CD diffing](https://argo-cd.readthedocs.io/en/stable/user-guide/diffing/) (unverified today) |
| helmforge | Small project with a `drift` command comparing desired commit to a marker file on the host | Same idea for hosts | Obscure; needs its own deploy tooling to write the marker | [repo](https://github.com/7irelo/helmforge) (search result only, unverified) |
| Retool Deployment Dashboard | Shows the commit deployed from a source-control main branch | Same idea | Retool apps only | [docs](https://docs.retool.com/docs/manage-source-control-deployments) (search result) |
| Better Stack, Cronitor, Cronping, UptimeRobot | Hosted uptime + heartbeat monitoring | `HEALTHZ`, `TLS_EXPIRY`, heartbeats | No git drift (unverified for each; none advertises it) | [Better Stack vs Healthchecks.io](https://betterstack.com/community/comparisons/better-stack-vs-healthchecks-io/), [cronping comparison](https://cronping.com/blog/cronping-vs-healthchecks-vs-cronitor) (competitor-published) |
| DIY pattern | "Put the SHA in the health endpoint, cron a compare against origin/main" | Exactly the core of `DRIFT` | Everyone re-writes it; nobody handles short SHAs, missing commits, grace windows, unpushed work and stale fetches | [Helpthread issue #176](https://github.com/Helpthread/helpthread/issues/176) (search result) |
| His own scripts | [repo-sync-check.py](/home/USER/claude-self/scripts/repo-sync-check.py) (unpushed/dirty repos), [job-heartbeat.py](/home/USER/claude-self/scripts/job-heartbeat.py) (stale jobs, Telegram), `backup_pull.py --check-status` | `UNPUSHED`, `HEARTBEAT`, `BACKUP_AGE` | Three outputs, no production view, no single page | local |

## Why not just Uptime Kuma plus a script?

An honest answer: for **liveness, certs and pushes, Uptime Kuma is better** and Fleet Truth should not compete there. Kuma plus `repo-sync-check.py` plus healthchecks.io pings from each backup job covers maybe 70% of the incidents above. What that combination misses:

  [1]  **Moving target.** Kuma's JSON Query asserts `commit == "abc1234"`. The right value changes on every push. Keeping it right means a script that rewrites Kuma monitors through its unofficial Socket.IO API after each push -- which is Fleet Truth's `DRIFT` check with a worse storage layer.  
  [2]  **Ancestry, not equality.** "Live differs from origin" is normal for an hour after a push and an outage when live contains commits origin lacks. Equality checks either alarm on every push or are ignored. The verdict needs git (`PENDING_DEPLOY` vs `BEHIND` vs `LIVE_NOT_ON_ORIGIN`).  
  [3]  **The laptop is part of production state.** The events-app incident was "live code exists only on one disk". That needs the working clone and the live SHA in the same computation. Kuma runs on a server; `repo-sync-check.py` never looks at production.  
  [4]  **One verdict per app.** Three tools give three alert streams with three flap policies. The page that answers "is everything I own deployed, pushed and backed up?" does not exist.  

If the owner decides the remaining 30% is not worth an app, the fallback is in [mvp-plan.md](mvp-plan.md) "Plan B": the spike plus a cron line and a Telegram send, about 2 hours.

## How easy is the edge to copy?

Easy. Gatus could add a git placeholder; Kuma could add a "compare to GitHub branch head" monitor type. The defensible parts are small: the verdict table with its false-alarm rules, the unpushed-work join, and a documented status-file contract for backups and heartbeats. Treat this as a tool first; see [business.md](business.md).

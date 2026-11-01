# Fleet Truth -- project schema

This repo is self-contained: open it and you have the context. Three layers (LLM-wiki pattern):

  -  **Raw sources** -- the code, tests, [spike/](spike/), and the read-only evidence it was designed from (his other repos, the 2026-10-31 audits under `/home/dftms/worktrees/maintenance-run-20261031/audit/`). Never edited from here.  
  -  **Wiki** -- [wiki/index.md](wiki/index.md) is the entry point; one page per concept; [wiki/log.md](wiki/log.md) is append-only history. The agent maintains it.  
  -  **Schema** -- this file: how to work here.  

## Session start
  [1]  Read [wiki/index.md](wiki/index.md), then [wiki/session-state.md](wiki/session-state.md), then [KANBAN.md](KANBAN.md) (In Progress first), then [wiki/open-questions.md](wiki/open-questions.md) for anything still unanswered.  
  [2]  `git log --oneline -10` and read any commit you did not write before building on it.  

## Session continuity
  -  [wiki/session-state.md](wiki/session-state.md) is a snapshot, overwritten whenever in-flight state changes; [wiki/log.md](wiki/log.md) is append-only. Resume cold from session-state's "Next action".  

## Rules
  -  One kanban: [KANBAN.md](KANBAN.md). New idea or bug becomes a card there, not a chat note. One card = one builder; a separate adversarial reviewer sees only the card and the diff.  
  -  Anything durable learned or decided goes into a wiki page in the same commit as the change, plus a line in [wiki/log.md](wiki/log.md). Design docs are historical once code exists; code is authoritative.  
  -  **Read-only toward everything it monitors.** Fleet Truth never writes to a working clone (no fetch into it, no locks: `--no-optional-locks`), never ssh-es, never talks to Docker, never sends a mutating request to a monitored app. Its only writes are its own data dir, alerts and the dead-man ping.  
  -  **Secrets:** only two read-only credentials (fine-grained GitHub token with Contents read-only; one Cloudflare Access service token) plus alert-channel tokens, all from environment variables named in `fleet.yaml`. Never in `fleet.yaml`, the DB, git config, logs, the page or alert text. No app credentials are ever stored. This is a monitor, not a secrets product (the owner works at Bitwarden).  
  -  **Outbound requests** go only through the pinned fetcher (connect-by-validated-IP, no redirects, size and time caps): [wiki/design.md](wiki/design.md) section 9.1. No other HTTP client in the code.  
  -  Values from the network (version JSON) are untrusted: regex-validate before passing to git; argument lists only, never `shell=True`.  
  -  No push, no `gh`, no external writes without the owner's explicit go-ahead. Cross-repo work (card [9]) happens in those repos under their own rules.  
  -  Data-loss safety: every state-changing step names its backup, rollback and stop-line. The SQLite DB and `data/mirrors/` are rebuildable caches.  

## Run and test
  -  Spike (stdlib only, today): `python3 -m unittest discover -s spike -v`; CLI: `python3 spike/check_drift.py --repo PATH --live-json FILE [--branch main] [--version-file VERSION]`.  
  -  After card [1]: `python3 -m venv .venv && .venv/bin/pip install -e .[dev] && .venv/bin/pytest -q`. Tests never touch the network (conftest blocks sockets) and must pass from a fresh `git clone`.  
  -  Run (after card [8]): `fleet-truth check-config fleet.yaml`, `fleet-truth once`, `fleet-truth serve`; runbook in `wiki/runbook.md` (card [8]).  

# Fleet Truth

A monitor for 5 to 20 small self-hosted apps that treats **git drift as an outage**. For each app it asks: is it up, does a real authenticated path work, is the running commit the one on `origin/main`, is there work that exists only on the laptop, did the last backup succeed recently, are certificates and heartbeats fresh -- and shows the answer on one red/green page with one alert stream.

Status (2026-11-01): **planning pack only.** Nothing is built except a stdlib spike of the drift verdict.

  -  Why and how: [wiki/design.md](wiki/design.md)  
  -  What exists already: [wiki/prior-art.md](wiki/prior-art.md)  
  -  What to build first: [wiki/mvp-plan.md](wiki/mvp-plan.md) and [KANBAN.md](KANBAN.md)  
  -  Decisions needed: [wiki/open-questions.md](wiki/open-questions.md)  
  -  Working rules: [CLAUDE.md](CLAUDE.md)  

## Spike

```bash
python3 -m unittest discover -s spike -v
python3 spike/check_drift.py --repo ~/invoicing --live-json live.json   # live.json = an /api/version body
```

Exit code: 0 ok/info, 1 warn, 2 crit, 3 error. Read-only: it never fetches or writes to the repo.

# Pricing and packaging (honest version)

Premises: Demetri works at Bitwarden, so nothing here may become a secrets-management or password product, and the employment contract question from [NEW-APP-IDEAS-20261101.md](/home/USER/worktrees/NEW-APP-IDEAS-20261101.md) (`20261101-193850-Q2`) is open: nothing ships publicly until it is answered. Fleet Truth holds two read-only tokens to do its job; it does not store, share or manage anyone's secrets, and its docs should say so plainly.

## What it is worth

  -  **To him:** high. It watches the exact failures that cost him outages and near-losses this autumn, for about 33 builder hours and no running cost beyond the healthchecks.io free tier.  
  -  **As portfolio material:** good. It is small, security-conscious (SSRF pinning, read-only tokens, fail-closed Access), well tested and explains a real incident. Fits Customer Engineer / solutions roles in the job hunt.  
  -  **As a product:** weak to modest. The buyer (solo dev, 5-20 apps) already pays $0-5 a month for uptime and often nothing at all. The git-drift edge is easy to copy ([prior-art.md](prior-art.md)).  

## Packaging options

| Option | What | Price | Honest assessment |
|---|---|---|---|
| A. Open source, self-hosted | The Docker image + docs + contracts (card [14]); MIT or AGPL-3.0 | free | Cheapest path to users and portfolio value; no revenue. Recommended first. |
| B. Sponsor / "supporter" tier | Same code; GitHub Sponsors or a one-off licence for a "pro" build (digest, multi-fleet) | EUR 3-5/month or EUR 39 one-off | Healthchecks.io charges $5 for its Supporter tier with the same limits as free ([pricing](https://healthchecks.io/pricing/)), which shows people pay small amounts out of goodwill. Expect tens of supporters at best. |
| C. Hosted | Fleet Truth runs the probes and GitHub drift for you | EUR 5/month for 10 apps, EUR 12 for 30 | The hosted side cannot see the laptop's unpushed commits or local backup files without a collector agent, so the hosted tier is the weaker product. It would also hold customers' Access service tokens and GitHub tokens: a security and compliance burden too big for a side project (and close to his employer's domain). Not recommended before 100 self-hosted users. |

## If money is the goal

  -  Price anchor: EUR 5/month for 10 apps (ideas doc). At 2% conversion of free users, EUR 100/month needs 1,000 active self-hosted installs. That is unlikely for a niche tool without marketing.  
  -  Invoicing would go through DFTM TEK (ideas doc Q3); Greek myDATA e-invoicing applies to B2B sales. Not worth setting up before there is demand.  
  -  Better lever: publish the incident write-up ("/healthz said 200 while every page returned 503") with the tool as the answer. That builds reputation for the job hunt even if nobody pays.  

## Recommendation

Build it as his own tool (weekend + month). Decide on open-sourcing after it has run on his fleet for 4 weeks and the contract question is answered (open question Q6). Do not plan for hosted.

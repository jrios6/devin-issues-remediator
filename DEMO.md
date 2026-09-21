# Demo script — 5-minute Loom

Audience: VP of Engineering + senior ICs evaluating Devin.

## 1. What (60s)
- "Engineering teams drown in small, well-understood maintenance debt — lint debt,
  legacy typing, deprecated patterns. Cheap to fix individually, never prioritized."
- "I built an event-driven remediation pipeline on the Devin API: label a GitHub
  issue `devin-fix`, and an autonomous session fixes it and opens a PR."
- Show: 4 open issues on jrios6/superset — real maintenance issues I identified
  in the Apache Superset codebase (legacy `Dict`/`Optional` typing, `any` types,
  non-idiomatic `len()` checks).

## 2. How (2m)
- Live: label an issue `devin-fix` → within seconds the dashboard shows it
  detected, a Devin session spins up, the issue gets a comment with the session
  link and moves to `devin-in-progress`.
- Architecture walkthrough (code):
  - `app/main.py` — webhook endpoint (HMAC-verified) + poller fallback, so the
    same trigger works behind a firewall.
  - `app/dispatcher.py` — the prompt compiler: issue title/body/acceptance
    criteria become a scoped Devin session pinned to the repo with a structured
    output contract.
  - `app/devin_client.py` — 3 calls: create session, poll status, message.
  - `app/db.py` — SQLite for dedupe + durable state.
- Open one Devin session in the UI — show it running `pre-commit`, editing files.

## 3. Observability (60s)
- Dashboard `/`: stat cards up top — issues, sessions dispatched, PRs opened,
  merged, failed, merge rate, avg time-to-remediate — "if I were an
  engineering leader, this is my throughput + success rate."
- Then the remediation table: per-issue state, session + PR links; and the
  event log beneath (detected → dispatched → pr_opened).

## 4. Why Devin (30s)
- A linter or dependabot can *detect* these issues; it can't *resolve* them.
  Each fix needs repo context, judgment, and test runs — that's an agent.
- Devin sessions are a *primitive*: one API call turns a GitHub event into a PR.
- Failure isolation: each fix is an isolated session + PR — a human reviews
  diffs, not agent internals.

## 5. When / next steps (30s)
- Point it at real queues: Dependabot alerts, Sentry regressions, SAST findings.
- Batch scheduling: nightly scan → auto-file issues → auto-remediate.
- Merge-safety: require CI green + reviewer approval; route failures to a Slack
  triage channel.
- Cost control already built in: per-session ACU cap, dedupe, label gating.

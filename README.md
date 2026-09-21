# devin-issue-remediator

Event-driven GitHub-issue remediation powered by the Devin API.

When an issue in the target repo (`jrios6/superset`) is labeled **`devin-fix`**, this
service dispatches a Devin session to implement the fix, tracks the session to
completion, and reports back on the issue — labeling it and commenting with the
resulting pull request. **`devin-done` means merged**: issues are labeled
`devin-pr-opened` while the PR awaits human review and only flip to `devin-done`
when the PR actually merges (a closed-unmerged PR → `devin-failed`).

## Architecture

```
GitHub issue labeled "devin-fix"
        │
        ├──► POST /webhooks/github   (real-time, HMAC-verified)
        └──► background poller       (POLL_INTERVAL_SECONDS — works with no public URL)
                        │
                        ▼
              POST /v3/organizations/{org}/sessions      (Devin API)
              prompt = issue title + body + acceptance criteria
              repos = ["jrios6/superset"], structured_output_schema enforced
                        │
                        ▼
              background tracker polls GET /v3/.../sessions/{id}
                        │
            ┌───────────┴────────────┐
            ▼                        ▼
      PR opened                  failed / suspended
      label → devin-pr-opened    label → devin-failed
      comment with PR link       comment with session link
            │
            ▼ (PR watcher polls GitHub for merge state)
      merged → devin-done     closed unmerged → devin-failed
```

State lives in SQLite (`/data/remediator.db`), so restarts are safe and
issues are deduplicated — each issue is dispatched at most once.

## Observability

| Endpoint | What it answers |
|---|---|
| `GET /` | Live HTML dashboard: every remediation, its session, its PR, and a recent event log |
| `GET /api/tasks` · `/api/events` | Same data as JSON |
| `GET /metrics` | Prometheus counters: detected / dispatched / PRs / failures / in-flight / latency |
| `GET /report` | Markdown rollup a leader can read: success rate, throughput, links |
| issue comments | Per-issue narrative: session dispatched → PR opened |

## Run it

```bash
cp .env.example .env   # fill in DEVIN_API_KEY, DEVIN_ORG_ID, GITHUB_TOKEN
docker compose up --build
```

Or locally:

```bash
pip install -r requirements.txt
set -a; source .env; set +a
DB_PATH=./remediator.db uvicorn app.main:app --port 8000
```

## Trigger it

- **Real webhook**: point a GitHub webhook (`issues` event) at
  `https://<host>/webhooks/github`, set `GITHUB_WEBHOOK_SECRET`.
- **Local/demo**: leave `POLL_INTERVAL_SECONDS=60` — labeling an issue `devin-fix`
  is picked up within a minute. Or force it:
  ```bash
  curl -X POST localhost:8000/scan
  ./scripts/simulate_webhook.sh 3        # simulate the webhook payload directly
  curl -X POST localhost:8000/issues/3/dispatch
  ```

### Writing good issues

The service remediates whatever the issue says — quality in, quality out. Each
`devin-fix` issue should state the problem, a narrow scope ("only these
files"), acceptance criteria, and a **rule source**: a link to the repo doc or
lint rule being enforced (e.g. `AGENTS.md`, `.cursor/rules/*.mdc`, an external
standard like PEP 8) so reviewers can verify the claim instead of trusting it.

## Tests

```bash
pip install pytest && pytest tests/
```

## Safety rails

- `DEVIN_MAX_ACU_LIMIT` caps spend per session.
- Only issues carrying `TRIGGER_LABEL` are dispatched; dedupe is enforced in the DB.
- Webhook payloads are HMAC-verified when `GITHUB_WEBHOOK_SECRET` is set.

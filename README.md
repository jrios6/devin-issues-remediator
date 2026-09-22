# devin-issue-remediator

Event-driven GitHub-issue remediation powered by the Devin API.

When an issue in the target repo (`jrios6/superset`) is labeled **`devin-fix`**, this
service dispatches a Devin session to implement the fix, tracks the session to
completion, and reports back on the issue — labeling it and commenting with the
resulting pull request.

## Workflow labels

| Label | Meaning |
|---|---|
| `devin-fix` | Trigger: queue this issue for autonomous remediation. |
| `devin-in-progress` | A Devin session has been created and is working on the issue. |
| `devin-pr-opened` | Devin opened a remediation PR that is awaiting review or merge. |
| `devin-done` | Terminal success: the remediation PR was merged. |
| `devin-failed` | Terminal failure: the session ended without a PR, or its PR closed without merging. |

The normal transition is `devin-fix` → `devin-in-progress` →
`devin-pr-opened` → `devin-done`. A failed session or closed-unmerged PR
transitions to `devin-failed` instead.

Suspended sessions remain tracked and keep their `devin-in-progress` label.
The dashboard's **Paused or waiting** filter includes suspension, user-input,
and approval waits; expand a suspended row for its reason. Resume the session
in Devin after resolving the cause. The next tracking cycle picks up resumption
or any PR already opened; suspension alone does not count as failure.

## Architecture

```text
                         GitHub issue
                      label: devin-fix
                              |
                 +------------+------------+
                 |                         |
                 v                         v
          GitHub webhook          Periodic label poller
                 |                         |
                 +------------+------------+
                              |
                              v
                    Validate and refetch
                              |
                              v
                  SQLite dedupe + event log --------> Dashboard
                              |
                              v
                    Build scoped prompt
                    Create Devin session
                              |
                              v
                    Devin implements
                    tests, and opens PR
                              |
                              v
                    Track session and PR
                              |
                  +-----------+-----------+
                  |                       |
               PR opened                No PR
                  |                       |
                  v                       v
       label: devin-pr-opened    label: devin-failed
                  |
                  v
              PR outcome
                  |
          +-------+-------+
          |               |
       Merged           Closed
          |               |
          v               v
 label: devin-done  label: devin-failed
```

State lives in SQLite (`/data/remediator.db`), so restarts are safe and
issues are deduplicated — each issue is dispatched at most once.

## Observability

| Endpoint | What it answers |
|---|---|
| `GET /` | Live HTML dashboard: every remediation, its session, its PR, and a recent event log |
| `GET /api/tasks` · `/api/events` | Same data as JSON, including per-PR CI status, size (+/−), ACUs consumed + Devin mode, and aggregate totals / time-to-PR |
| `GET /api/integrations` | GitHub and Devin read-sync health, successful sync timestamps, and per-resource failures |
| issue comments | Per-issue narrative: session dispatched → PR opened |

The dashboard leads with what needs action: open PRs awaiting review or merge,
merged/failed counts, and average dispatch-to-first-PR time. Attention buttons
filter the table; each row expands for PR size, ACUs, mode, and failure detail.
Recent events sit in a side rail (50 shown, **show more** reveals older ones).

CI badges only report what the backend has verified recently:
`GET /api/integrations` tracks the last successful upstream read per resource,
and a badge shows **Unverified** until its PR and check reads are both fresh —
so a dead token can't masquerade as a green board.

## Credentials and initial setup

The service needs two credentials and, when using the webhook trigger, one
shared secret:

| Variable | Required | Purpose |
|---|---|---|
| `DEVIN_API_KEY` | Yes | `cog_...` service-user key used to create and inspect Devin sessions. |
| `DEVIN_ORG_ID` | Yes | Organization identifier used in the Devin v3 API URL; this is not a secret. |
| `GITHUB_TOKEN` | Yes | Fine-grained GitHub PAT used to read issues and PRs and to update issue labels/comments. |
| `GITHUB_WEBHOOK_SECRET` | Webhook only | Shared random value used to verify GitHub webhook signatures. It can be omitted when using only the poller locally. |

### 1. Connect the repository to Devin

Ensure the Devin GitHub integration has access to `jrios6/superset`. Devin must
be able to clone that repository, push branches, and open pull requests before
sessions created by this service can remediate its issues.

### 2. Create the Devin service-user key

1. In Devin, open **Settings → Devin API → Service users**.
2. Select **Provision service user**, give it a descriptive name, and assign the
   **Member** role. Member access is sufficient to create and manage sessions.
3. Copy the API key immediately; it starts with `cog_` and is shown only once.
4. Copy the organization ID shown at the top of the same **Devin API** page.

See Devin's official [authentication guide](https://docs.devin.ai/api-reference/authentication)
and [Teams quick start](https://docs.devin.ai/api-reference/getting-started/teams-quickstart).

### 3. Create the GitHub token

Create a [fine-grained personal access token](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
restricted to `jrios6/superset` with these repository permissions:

- **Issues: Read and write** — fetch issues, add/remove labels, and post comments.
- **Pull requests: Read-only** — track whether a remediation PR is open, merged,
  or closed.
- **Metadata: Read-only** — granted automatically by GitHub.

The Devin GitHub integration, not this token, supplies the code access Devin
uses to create branches and pull requests.

### 4. Configure the environment

Copy the template, then replace every placeholder in `.env`:

```bash
cp .env.example .env
```

```dotenv
DEVIN_API_KEY=cog_replace_me
DEVIN_ORG_ID=replace_me
GITHUB_TOKEN=github_pat_replace_me
GITHUB_REPO=jrios6/superset
```

The `.env` file is ignored by Git. Never commit or paste real credentials into
issues, logs, or screenshots.

### 5. Create the workflow labels

These labels already exist in `jrios6/superset`. When configuring a different
target repository, create them once before starting the service:

```bash
gh label create devin-fix --repo jrios6/superset --color 0E8A16 --description "Queued for autonomous remediation" --force
gh label create devin-in-progress --repo jrios6/superset --color FBCA04 --description "Devin session in progress" --force
gh label create devin-pr-opened --repo jrios6/superset --color 6F42C1 --description "Remediation PR awaiting review" --force
gh label create devin-done --repo jrios6/superset --color 1D76DB --description "Remediation PR merged" --force
gh label create devin-failed --repo jrios6/superset --color D73A4A --description "Remediation failed or PR closed unmerged" --force
```

### 6. Optional: configure the webhook

Generate a secret:

```bash
openssl rand -hex 32
```

Copy the result into `GITHUB_WEBHOOK_SECRET` in `.env`. In the target GitHub
repository, open **Settings → Webhooks → Add webhook** and configure:

- Payload URL: `https://<your-host>/webhooks/github`
- Content type: `application/json`
- Secret: the same generated value
- Events: **Issues**

For a local demo without a public URL, leave `GITHUB_WEBHOOK_SECRET` blank and
keep `POLL_INTERVAL_SECONDS` greater than zero.

## Run it

```bash
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

With Node.js on `PATH`, the same command also runs JavaScript regression checks
for attention filters, cached CI, remediation pagination, event expansion,
sorting, escaping, and refresh failure/recovery. Without Node.js, that test is skipped.

## Safety rails

- `DEVIN_MAX_ACU_LIMIT` caps spend per session.
- `DEVIN_MODE` selects the agent mode (normal/fast/lite/ultra/fusion; empty = org default). `devin_mode`, ACU cap and poll interval are also editable live via the dashboard's `config` panel (`GET/POST /api/config`; runtime-only, resets on restart).
- Only issues carrying `TRIGGER_LABEL` are dispatched; dedupe is enforced in the DB.
- `POST /webhooks/github` supports HMAC-SHA256 verification when
  `GITHUB_WEBHOOK_SECRET` is configured; with it unset the endpoint is
  unauthenticated (a startup warning is logged). For a public deployment,
  require the secret and protect or disable the unauthenticated `/scan` and
  `/issues/{number}/dispatch` endpoints.

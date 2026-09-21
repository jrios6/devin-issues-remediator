#!/usr/bin/env bash
# Simulate a GitHub "issues.labeled" webhook against a running service.
# Fetches the real issue payload via gh when available, else posts a stub
# (the service refetches canonical issue data from the API either way).
# Usage: ./scripts/simulate_webhook.sh [issue_number] [base_url]
set -euo pipefail
ISSUE="${1:-1}"
BASE="${2:-http://localhost:8000}"
SECRET="${GITHUB_WEBHOOK_SECRET:-}"
REPO="${GITHUB_REPO:-jrios6/superset}"

if command -v gh >/dev/null 2>&1; then
  ISSUE_JSON=$(gh api "repos/${REPO}/issues/${ISSUE}")
else
  ISSUE_JSON="{\"number\":${ISSUE},\"title\":\"issue ${ISSUE}\",
    \"html_url\":\"https://github.com/${REPO}/issues/${ISSUE}\",
    \"labels\":[{\"name\":\"devin-fix\"}]}"
fi

BODY="{\"action\":\"labeled\",\"issue\":${ISSUE_JSON}}"

HEADERS=(-H "Content-Type: application/json" -H "X-GitHub-Event: issues")
if [ -n "$SECRET" ]; then
  SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
  HEADERS+=(-H "X-Hub-Signature-256: $SIG")
fi

curl -sf "${HEADERS[@]}" -d "$BODY" "$BASE/webhooks/github"
echo

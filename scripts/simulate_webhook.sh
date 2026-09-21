#!/usr/bin/env bash
# Simulate a GitHub "issues.labeled" webhook against a running service.
# Usage: ./scripts/simulate_webhook.sh [issue_number] [base_url]
set -euo pipefail
ISSUE="${1:-1}"
BASE="${2:-http://localhost:8000}"
SECRET="${GITHUB_WEBHOOK_SECRET:-}"

BODY=$(cat <<EOF
{"action":"labeled","issue":{"number":${ISSUE},
 "title":"Simulated issue ${ISSUE}","html_url":"https://github.com/jrios6/superset/issues/${ISSUE}",
 "body":"Simulated","labels":[{"name":"devin-fix"}]}}
EOF
)

HEADERS=(-H "Content-Type: application/json" -H "X-GitHub-Event: issues")
if [ -n "$SECRET" ]; then
  SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
  HEADERS+=(-H "X-Hub-Signature-256: $SIG")
fi

curl -sf "${HEADERS[@]}" -d "$BODY" "$BASE/webhooks/github"
echo

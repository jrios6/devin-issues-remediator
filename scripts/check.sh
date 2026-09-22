#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
command -v node >/dev/null || {
  echo "Node.js is required; refusing to skip dashboard regression tests." >&2
  exit 1
}
python -m pytest tests/ -q
ruff check app tests
pyright app tests
git diff --check

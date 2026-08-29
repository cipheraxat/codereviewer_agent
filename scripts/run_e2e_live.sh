#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

unset ATLASSIAN_EMAIL ATLASSIAN_API_TOKEN ATLASSIAN_DOMAIN

if [[ -z "${GITHUB_TOKEN:-}" ]] && command -v gh >/dev/null 2>&1; then
  GITHUB_TOKEN="$(gh auth token)"
  export GITHUB_TOKEN
fi

PYTHON="python3"
if [[ -x "$ROOT/.venv311/bin/python" ]]; then
  PYTHON="$ROOT/.venv311/bin/python"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PYTHON="$ROOT/.venv/bin/python"
fi

exec "$PYTHON" scripts/e2e_live_flow.py "$@"

#!/usr/bin/env bash
# Mirror GitHub Actions CI job locally
set -euo pipefail
cd "$(dirname "$0")/.."

export DATABASE_URL="${DATABASE_URL:-sqlite:///:memory:}"
export USE_ADK="${USE_ADK:-true}"
export GEMINI_API_KEY="${GEMINI_API_KEY:-}"

echo "==> pytest"
python -m pytest -q

echo "==> docker build"
docker build -t fraud-platform-api:test .

echo "CI checks passed locally."

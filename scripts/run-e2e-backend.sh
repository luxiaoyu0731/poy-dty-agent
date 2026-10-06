#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd -P)"
host="${1:-127.0.0.1}"
port="${2:-18000}"
owned_root=0

if [[ -z "${DG01_TEST_DB_ROOT:-}" ]]; then
  system_tmp="${TMPDIR:-/tmp}"
  DG01_TEST_DB_ROOT="$(mktemp -d "${system_tmp%/}/poy-dty-e2e.XXXXXX")"
  owned_root=1
fi
chmod 700 "$DG01_TEST_DB_ROOT"
mkdir -p "$DG01_TEST_DB_ROOT/tmp"
chmod 700 "$DG01_TEST_DB_ROOT/tmp"

export DG01_TEST_DB_ROOT
export TMPDIR="$DG01_TEST_DB_ROOT/tmp"
export SQLITE_PATH="${SQLITE_PATH:-$DG01_TEST_DB_ROOT/e2e.db}"
export PYTHONPATH="$repo_root/server:$repo_root${PYTHONPATH:+:$PYTHONPATH}"
export EMBEDDING_PROVIDER=e2e_lexical
export EMBEDDING_FALLBACK_POLICY=lexical_only
export INTRADAY_PRICE_SCHEDULER_ENABLED=0
export INDUSTRIAL_INTELLIGENCE_ENABLED=1

cleanup() {
  if [[ "$owned_root" == 1 && "$DG01_TEST_DB_ROOT" == */poy-dty-e2e.* ]]; then
    rm -rf -- "$DG01_TEST_DB_ROOT"
  fi
}
trap cleanup EXIT

cd "$repo_root/server"
uv run python scripts/seed_e2e_database.py
uv run uvicorn app.main:app --host "$host" --port "$port"

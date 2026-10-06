#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
review_tmp_base=$(CDPATH= cd -- "${TMPDIR:-/tmp}" && pwd -P)
review_root=$(mktemp -d "$review_tmp_base/poy-dty-backend-review.XXXXXX")
chmod 700 "$review_root"
mkdir -m 700 "$review_root/tmp"

cleanup() {
  case "$review_root" in
    "$review_tmp_base"/poy-dty-backend-review.*) rm -rf -- "$review_root" ;;
  esac
}
trap cleanup EXIT HUP INT TERM

cd "$repo_root/server"
PYTHONPATH="$repo_root/server:$repo_root" \
DG01_TEST_DB_ROOT="$review_root" \
TMPDIR="$review_root/tmp" \
SQLITE_PATH="$review_root/agent.db" \
COVERAGE_FILE="$review_root/.coverage" \
uv run pytest tests --cov=app --cov-report=term-missing --cov-fail-under=70

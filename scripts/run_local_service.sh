#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${LOCAL_SERVICE_ENV_FILE:-$ROOT_DIR/.env.local-production}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export NO_PROXY="127.0.0.1,localhost${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
export SQLITE_PATH="${SQLITE_PATH:-$ROOT_DIR/server/data/agent.db}"
NODE_BIN="${NODE_BIN:-/opt/homebrew/bin/node}"
NPM_CLI="${NPM_CLI:-/opt/homebrew/lib/node_modules/npm/bin/npm-cli.js}"
export PATH="$(dirname "$NODE_BIN"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

case "${1:-}" in
  backend)
    cd "$ROOT_DIR/server"
    exec "$ROOT_DIR/server/.venv/bin/uvicorn" app.main:app --host 127.0.0.1 --port 8000
    ;;
  frontend)
    cd "$ROOT_DIR"
    exec "$NODE_BIN" "$NPM_CLI" run preview -- --host 127.0.0.1 --port 5173 --strictPort
    ;;
  *)
    echo "usage: $0 {backend|frontend}" >&2
    exit 64
    ;;
esac

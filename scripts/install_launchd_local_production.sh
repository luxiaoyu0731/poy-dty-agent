#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${1:-}" == "--services-only" ]]; then
  exec "$ROOT_DIR/scripts/manage_local_services.sh" install
fi
PYTHON_BIN="${PYTHON_BIN:-server/.venv/bin/python}"
OUTPUT_DIR="${LOCAL_PRODUCTION_OUTPUT_DIR:-$ROOT_DIR/.codex-run/local-production}"
LAUNCHD_SRC="$OUTPUT_DIR/launchd"
LAUNCHD_DST="$HOME/Library/LaunchAgents"
LOAD=0

if [[ "${1:-}" == "--load" ]]; then
  LOAD=1
fi

if [[ ! -x "$ROOT_DIR/$PYTHON_BIN" ]]; then
  PYTHON_BIN="python3"
fi

if [[ ! -f "$ROOT_DIR/.env.local-production" ]]; then
  echo "Missing $ROOT_DIR/.env.local-production. Copy deploy/examples/.env.local-production.example and fill secrets first." >&2
  exit 2
fi

cd "$ROOT_DIR"
"$PYTHON_BIN" server/scripts/run_local_daily.py --dry-run --skip-health --output-dir "$OUTPUT_DIR"
mkdir -p "$LAUNCHD_DST" "$OUTPUT_DIR/logs"

for plist in "$LAUNCHD_SRC"/com.poydty.agent.*.plist; do
  plutil -lint "$plist" >/dev/null
  install -m 0644 "$plist" "$LAUNCHD_DST/$(basename "$plist")"
  echo "installed $LAUNCHD_DST/$(basename "$plist")"
  if [[ "$LOAD" == "1" ]]; then
    launchctl bootout "gui/$UID" "$LAUNCHD_DST/$(basename "$plist")" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/$UID" "$LAUNCHD_DST/$(basename "$plist")"
  fi
done

if [[ "$LOAD" == "1" ]]; then
  launchctl print "gui/$UID" | grep -E 'com\.poydty\.agent\.' || true
else
  echo "Plists installed but not loaded. Re-run with --load after reviewing .env.local-production."
fi

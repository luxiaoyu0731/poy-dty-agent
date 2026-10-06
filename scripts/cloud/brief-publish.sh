#!/bin/bash
# The Python runner records a calendar skip without opening the database.
set -uo pipefail
ROOT=${AGENT_ROOT:-/opt/agent}
cd "$ROOT" || exit 1
set -a
source "$ROOT/.env" || exit 1
set +a
export TZ=Asia/Shanghai
LOG="$ROOT/state/logs/brief-$(date +%F).log"
{
  echo "=== $(date -Is) brief publish pass ==="
  sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend \
    python server/scripts/run_industrial_intelligence_daily.py --apply \
    --db /data/agent.db --output-dir /data/industrial-intelligence
  BRIEF_RC=$?
  echo "=== $(date -Is) brief rc=$BRIEF_RC ==="
} >> "$LOG" 2>&1
exit "$BRIEF_RC"

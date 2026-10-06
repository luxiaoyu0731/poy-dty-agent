#!/bin/bash
# Installed as /opt/agent/bin/daily.sh. The cloud is the only active writer.
set -uo pipefail
ROOT=${AGENT_ROOT:-/opt/agent}
cd "$ROOT" || exit 1
set -a
source "$ROOT/.env" || exit 1
set +a
export TZ=Asia/Shanghai
LOG="$ROOT/state/logs/daily-$(date +%F).log"
{
  echo "=== $(date -Is) daily chain ==="
  # N0 (multi-agent plan §6.4, ADR-8): the event pipeline runs BEFORE issuance so
  # the agent chain and the 08:00 forecast see same-day intelligence, not T-1.
  sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm backend \
    python server/scripts/run_industrial_intelligence_daily.py --apply \
    --db /data/agent.db --output-dir /data/industrial-intelligence
  INTELLIGENCE_RC=$?
  echo "=== $(date -Is) intelligence rc=$INTELLIGENCE_RC ==="
  sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm scheduler \
    python server/scripts/run_production_scheduler.py --once
  DAILY_RC=$?
  echo "=== $(date -Is) daily rc=$DAILY_RC ==="
  SNAPSHOT_RC=0
  if [ "$DAILY_RC" -eq 0 ]; then
    curl -fsS -o /dev/null --max-time 180 -X POST \
      -H "x-internal-token: ${INTERNAL_API_TOKEN:?missing internal token}" \
      -H "Content-Type: application/json" -d '{}' \
      http://127.0.0.1:8000/api/v1/workbench/snapshot/materialize
    SNAPSHOT_RC=$?
  else
    echo 'snapshot materialization skipped: daily chain did not succeed'
  fi
  echo "=== $(date -Is) snapshot rc=$SNAPSHOT_RC ==="
  curl -fsS -o /dev/null --max-time 120 \
    -H "x-internal-token: ${INTERNAL_API_TOKEN:?missing internal token}" \
    http://127.0.0.1:8000/api/v1/workbench/snapshot
  CACHE_RC=$?
  "$ROOT/bin/retention.sh"
  RETENTION_RC=$?
  echo "=== $(date -Is) cache rc=$CACHE_RC retention rc=$RETENTION_RC ==="
} >> "$LOG" 2>&1
# Capture statuses immediately: a final echo must never turn failure into green.
for rc in "$DAILY_RC" "$INTELLIGENCE_RC" "$SNAPSHOT_RC" "$CACHE_RC" "$RETENTION_RC"; do
  [ "$rc" -eq 0 ] || exit "$rc"
done

#!/bin/bash
# 每 10 分钟：输出新鲜度检查 + 公网健康探测 + 事件链节点告警（cloud-daily-operations.md §5）。
set -uo pipefail
cd /opt/agent || exit 1
set -a; source .env; set +a
export PUBLIC_HEALTH_MIN_HEALTHY_RATIO="${PUBLIC_HEALTH_MIN_HEALTHY_RATIO:-0.7}"
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm -T --no-deps backend \
  python server/scripts/check_output_freshness.py \
  --db /data/agent.db \
  --status-in /data/local-production/output-freshness/latest.json \
  --output /data/local-production/output-freshness/latest.json >/dev/null 2>&1 || true
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm -T --no-deps backend \
  python server/scripts/probe_public_health.py \
  --base-url "${PUBLIC_CANONICAL_ORIGIN:-https://app.kaipingrc.com}" \
  --output /data/public-health/latest.json \
  --timeout 5 --attempts 6 --retry-delay 5
# 2026-10-02 (audit P2): chain-node alerting — degraded node A/B steps must not
# pass silently again (this morning's node-A crash was found by hand).
sudo docker run --rm -v agent_agent-data:/data alpine sh -c '
  apk add -q jq >/dev/null 2>&1
  jq -r "[.event_signal.status // \"ok\", .agent_chain.status // \"ok\"]" /data/local-production/latest-status.json 2>/dev/null
' | while read -r sig chain; do
  [ "${sig}" = "degraded" ] || [ "${chain}" = "degraded" ] && {
    printf '{"chain_alert":"%s","event_signal":"%s","agent_chain":"%s","at":"%s"}\n' \
      "chain_step_degraded" "${sig}" "${chain}" "$(date -u +%FT%TZ)" \
      | sudo docker run --rm -i -v agent_agent-data:/data alpine sh -c 'cat > /data/public-health/chain-alert.json'
    echo "CHAIN ALERT: event_signal=${sig} agent_chain=${chain}" >&2
  }
done
# 2026-10-02 (operator): evidence content starvation — the dossier pages must
# never sit empty without an alarm again. Starving = zero mechanism-admitted
# claims (support+counter) across all seven targets in the current view for
# two consecutive probes (20 minutes), so a single cold cache cannot false-fire.
STARVED_NOW=1
for target in crude naphtha px pta meg poy dty; do
  admitted=$(curl -fsS --max-time 60 -H "x-internal-token: ${INTERNAL_API_TOKEN:?missing internal token}" \
    "http://127.0.0.1:8000/api/v1/forecasts/seven-product/evidence?target=${target}&horizon_days=7&view=current" 2>/dev/null \
    | jq -r '([( .current_support // [] ) | length, ( .current_counter // [] ) | length] | add) // 0' 2>/dev/null)
  if [ "${admitted:-0}" -gt 0 ] 2>/dev/null; then
    STARVED_NOW=0
    break
  fi
done
STARVED_STREAK=0
if [ "$STARVED_NOW" = "1" ]; then
  STARVED_STREAK=1
  PREV=$(sudo docker run --rm -v agent_agent-data:/data alpine sh -c 'cat /data/public-health/evidence-alert.json 2>/dev/null' | jq -r '.streak // 0' 2>/dev/null)
  case "${PREV}" in (*[!0-9]*|"") PREV=0;; esac
  STARVED_STREAK=$((PREV + 1))
  SINCE=$(sudo docker run --rm -v agent_agent-data:/data alpine sh -c 'cat /data/public-health/evidence-alert.json 2>/dev/null' | jq -r '.starved_since // empty' 2>/dev/null)
  SINCE="${SINCE:-$(date -u +%FT%TZ)}"
  printf '{"alert":"evidence_starved","streak":%s,"starved_since":"%s","at":"%s"}\n' \
    "$STARVED_STREAK" "$SINCE" "$(date -u +%FT%TZ)" \
    | sudo docker run --rm -i -v agent_agent-data:/data alpine sh -c 'cat > /data/public-health/evidence-alert.json'
  echo "EVIDENCE ALERT: zero admitted claims across targets, streak=${STARVED_STREAK}" >&2
else
  sudo docker run --rm -v agent_agent-data:/data alpine sh -c 'rm -f /data/public-health/evidence-alert.json' 2>/dev/null || true
fi
exit 0

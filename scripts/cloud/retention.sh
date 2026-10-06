#!/bin/bash
# 磁盘保留策略（每日链末尾由 daily.sh 调用；亦可手动执行）。
# 70G 盘 + 28G data 卷：GB 级产物各留最新 1 份，一次性迁移备份的
# 大 sqlite 超期只留清单，docker 只保留在用 + 少量近期回滚标签。
set -uo pipefail
LOG_TAG="retention-$(date +%FT%H:%M:%S)"
log() { echo "[$LOG_TAG] $*"; }

# ---- 1) 卷内 GB 级锚点：每类保留最新 1 份（原有策略） ----
sudo docker run --rm -v agent_agent-data:/data alpine sh -c "
ls -t /data/backups/daily-anchor/*.sqlite 2>/dev/null | tail -n +2 | xargs -r rm -f
rm -f /data/backups/daily-anchor/*.sqlite-shm /data/backups/daily-anchor/*.sqlite-wal 2>/dev/null
ls -t /data/backups/index-prune-anchors/*.sqlite 2>/dev/null | tail -n +2 | xargs -r rm -f
rm -f /data/backups/index-prune-anchors/*.sqlite-shm /data/backups/index-prune-anchors/*.sqlite-wal 2>/dev/null
ls -t /data/local-production/seven-product-evaluation/evaluation-input/*.sqlite 2>/dev/null | tail -n +2 | xargs -r rm -f
" || log "anchor retention failed"

# ---- 2) 一次性迁移/恢复目录（/data/*-recovery-20*、/data/*-integration-20*）----
# 超过 14 天后只删除目录内的大 sqlite 备份；清单/报告/哈希记录全部保留，
# 保证溯源能力。不再需要人工记得回来删 3GB 级的临时备份。
sudo docker run --rm -v agent_agent-data:/data alpine sh -c '
find /data -maxdepth 1 -type d \( -name "*-recovery-20*" -o -name "*-integration-20*" \) -mtime +14 2>/dev/null | while read -r dir; do
  # busybox find：-size 用字节字面量（+10M 后缀在部分 busybox 上不匹配）
  find "$dir" -maxdepth 2 -name "*.sqlite" -size +10485760c | while read -r f; do
    rm -f "$f" && echo "$f"
  done
done' | while read -r removed; do log "expired migration sqlite removed: $removed"; done

# ---- 3) content-addressed 报告封顶（防慢增长无界） ----
sudo docker run --rm -v agent_agent-data:/data alpine sh -c "
ls -t /data/industrial-intelligence/industrial-intelligence-daily-*.json 2>/dev/null | tail -n +91 | xargs -r rm -f
ls -t /data/local-production/seven-product-evaluation/seven-product-evaluation-*.json 2>/dev/null | tail -n +91 | xargs -r rm -f
" || log "report bounding failed"

# ---- 4) docker 清理（宿主侧） ----
# 4a) 悬空镜像与已停止容器
sudo docker image prune -f >/dev/null 2>&1 || log "dangling image prune failed"
sudo docker container prune -f >/dev/null 2>&1 || log "stopped container prune failed"
# 4b) 构建缓存只留 7 天内（近期层缓存保住重建速度）
sudo docker builder prune -f --filter until=168h >/dev/null 2>&1 || log "builder prune failed"
# 4c) 服务镜像标签：未被运行容器使用、非回滚标签的，每仓库只留最新 4 个
#     （回滚标签 *-rollback 由发布工件保留期保护；:latest 始终被运行容器引用）
for repo in agent-backend agent-news-scheduler agent-event-summary-worker agent-daily-candidate; do
  sudo docker images --format '{{.Repository}}\t{{.Tag}}\t{{.CreatedAt}}' \
    | sort -t"$(printf '\t')" -k3,3 -r \
    | awk -F'\t' -v repo="$repo" '$1==repo && $2!="<none>" && $2 !~ /rollback/' \
    | cut -f1,2 | tr '\t' ':' \
    | tail -n +5 \
    | while read -r image; do
        if sudo docker ps --format '{{.Image}}' | grep -qx "$image"; then continue; fi
        sudo docker rmi "$image" >/dev/null 2>&1 && log "docker image removed: $image"
      done
done

# ---- 5) 宿主发布目录：只留最新 8 个 release 工件目录 ----
ls -dt /opt/agent/state/releases/*/ 2>/dev/null | tail -n +9 | while read -r dir; do
  sudo rm -rf "$dir" && log "release artifacts removed: $dir"
done

# ---- 6) 状态输出 ----
log "host disk: $(df -h / | tail -1 | awk '{print $3"/"$2" used, "$4" free ("$5")"}')"
log "data volume: $(sudo docker run --rm -v agent_agent-data:/data alpine sh -c 'du -sh /data 2>/dev/null' | cut -f1)"
log "docker: $(sudo docker system df --format '{{.Type}} {{.Size}} (reclaimable {{.Reclaimable}})' | tr '\n' '; ')"

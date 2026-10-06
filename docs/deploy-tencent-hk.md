# 腾讯云香港部署手册（2026-09-22 实施版）

目标：把 Mac 本机生产（launchd 多服务 + cloudflared）整体迁到腾讯云香港轻量
2C4G/70G，Mac 保留为即时回滚。全程零入站端口（Cloudflare Tunnel 出站连接）。

## 0. 实例与前提（已实施）

- 腾讯云轻量（Lighthouse）香港三区 · 2C4G · 70GB SSD · 2TB 流量 · ¥90/月
- 实例 `YOUR_INSTANCE_ID`，公网 IP `YOUR_SERVER_HOST`，Ubuntu 26.04 LTS
- SSH 密钥：`~/.ssh/your-project-key.pem`（RSA 2048），用户 `ubuntu`（免密 sudo）
- 防火墙保持默认（仅 SSH 22；web 端口全关，cloudflared 仅出站）
- 4GB 内存补 4G swap（镜像自带 2G，合计 6G），`vm.swappiness=10`

## 1. 服务器初始化（已实施）

```bash
apt update && apt -y install docker.io docker-compose-v2 rsync curl jq
systemctl enable --now docker
fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
mkdir -p /opt/agent
```

## 2. 源可达性探测（2026-09-22 实测结论）

`scripts/probe_source_reachability.sh` 上机执行。**结果：门槛通过**。

- ✅ 日度决策链全通：郑商所/texnet/TNC/生意社/100ppi 200 & <3s；
  新浪日度 K 线主机 `stock2.finance.sina.com.cn`（akshare 路径）200；
  EIA/OFAC/oilprice/TradingEconomics/USGS 200
- ❌ 已知降级——盘中实时期货：`hq.sinajs.cn` 对香港机房 403（地域封锁）；
  Yahoo chart API 对腾讯 IP 段边缘 429（连 Cookie 引导域也被拒）；东财境外
  302 到 `push2delay` 且期货 secid 返回 `data:null`。现货品种盘中价不受影响
  （texnet/TNC 公开页路径），日度数据不受影响。
  后续修复选项：控制台免费换一次公网 IP 试 Yahoo；或继续攻东财参数。
- 假阳性说明：海关 english.customs、中石化 listco 裸探测 000（Mac 基线同样
  000），非机房风险；新浪 hq 与 stock2 是不同主机，前者封后者通。

## 3. 代码与数据迁移（已实施）

```bash
# 前端构建后整树同步（排除开发工件），服务器侧删除本地专用目录后 ~145M
rsync -a --exclude node_modules --exclude server/.venv --exclude .git \
  --exclude .visual-regression --exclude .codex-run --exclude agent-context \
  ~/Desktop/agent/ ubuntu@<IP>:/opt/agent/

# 数据库：先在线一致性备份再传（不要直接拷活动库）
python3 server/scripts/manage_public_production.py backup-db \
  --source ".../shared/data/agent.db" --destination ".../agent.server-migration.sqlite"
rsync -a agent.server-migration.sqlite ubuntu@<IP>:/opt/agent/data-agent.db

# 卷灌库（compose 项目名 agent → 卷名 agent_agent-data）
docker volume create agent_agent-data
docker run --rm -v agent_agent-data:/data -v /opt/agent:/src alpine \
  sh -c 'cp /src/data-agent.db /data/agent.db && mkdir -p /data/cache && chown -R 10001:10001 /data'
```

- 增量再同步技巧：服务器上先 `cp data-agent.db data-agent-final.db`，
  rsync 对同名文件走 delta（10GB 只传差异页）。
- 辅助目录一并入卷：`data/event-overviews`（26M，workbench/检索在用）、
  `information-reports`、`intelligence-runs`。
- 服务器 `.env` 由 Mac `shared/.env.production` 拷贝：
  `SQLITE_PATH=/data/agent.db` + 追加 `HOME=/data`、`XDG_CACHE_HOME=/data/cache`
  （fastembed 模型缓存必须落在卷内；容器用户 home 不可写）、
  `INTELLIGENCE_RUN_DIR=/data/intelligence-runs`、
  `PIPELINE_GRAPH_STATE_ROOT=/data`（工作流图读 worker 心跳/调度状态的根；
  不设则容器内解析为 `/`，事件摘要节点会误报"心跳缺失"）、
  `LOCAL_DAILY_STEP_TIMEOUT_SECONDS=1800`、
  `PRODUCTION_SCHEDULER_DAILY_TIMEOUT_SECONDS=5400`、
  `LIVE_INTELLIGENCE_PROJECTION_DEADLINE_SECONDS=90`（30 分钟雷达投影排空预算）。

## 4. 拓扑：host 网络（关键修正）

`scripts/local-public-server.mjs` 强制后端源为 loopback（SSRF 防护，不可绕）。
因此 frontend 与 cloudflared 都用 `network_mode: host`（见 docker-compose.prod.yml）：

- frontend 绑 `127.0.0.1:4173`，代理 `http://127.0.0.1:8000`（backend 的端口发布）
- cloudflared 容器挂 `./cloudflared/{config.yml,<tunnel-id>.json}`，
  ingress `service: http://127.0.0.1:4173`；凭据 json 需 `chmod 644`（容器用户读取）
- 与 Mac launchd 拓扑等价，无对外监听端口

## 5. 服务映射（Mac launchd → 服务器）

| Mac launchd | 服务器等价物 |
| --- | --- |
| public-backend / public-frontend / cloudflare-tunnel | compose backend / frontend / cloudflared |
| news-scheduler / event-summary-worker | compose 同名容器 |
| local-daily（09:30）| systemd `poydty-daily.timer`（**08:00**，2026-09-23 起；链跑 60-90 分钟，日报典型 09:15 前后发布，与日报设计截止 08:20/盘前口径对齐）→ `docker compose run --rm scheduler python server/scripts/run_production_scheduler.py --once` |
| intelligence-daily（09:30）| 同上 timer 内第二步：`run_industrial_intelligence_daily.py --apply` |
| body-acquisition（每小时，Mac 走 127.0.0.1:7890 代理）| systemd `poydty-body-acquisition.timer`（香港直连国际源，无需代理） |
| event-overviews（--watch）| systemd `poydty-event-overviews.service`（docker run 常驻，Restart=always；2026-09-24 起 `--no-healthcheck --network host -e EVENT_OVERVIEWS_EVENT_LIBRARY_ORIGIN=http://127.0.0.1:8000`：镜像无 curl 改用 urllib，且绕开公网密码门直连 backend） |
| morning-brief（curl 缓存）| 不迁移：日度链内生成，前端直读 `/api/v1/morning-brief` |
| public-health-probe（Mac）| systemd `poydty-health-probe.timer`（每 10 分钟，`/opt/agent/bin/health-probe.sh`；同时跑 check_output_freshness，产物供告警检查读取；`PUBLIC_HEALTH_MIN_HEALTHY_RATIO=0.7` 适配香港出口封锁现状） |
| keep-awake | 不迁移 |

- 单元文件在 `/etc/systemd/system/poydty-*`，编排脚本在 `/opt/agent/bin/`，
  日志在 `/opt/agent/state/logs/`。
- 注意：`docker compose run <svc> --flag` 会整体替换 list 形式的 command，
  必须写全 `python server/scripts/run_production_scheduler.py --once`。
- ~~worker 容器 docker healthcheck 恒 unhealthy 误报~~ 2026-09-24 已修：compose 中
  scheduler/news-scheduler/event-summary-worker 显式 `healthcheck: disable: true`
  （镜像级 HEALTHCHECK 只适用于跑 API 的 backend）。

## 6. 切换顺序（已实施）

1. 停 Mac 写入侧（news-scheduler/event-summary-worker/event-overviews/
   intelligence-daily/local-daily/morning-brief）→ 最终在线备份 → 增量同步 →
   卷内换库 → backend 重启验证
2. 服务器起 systemd 单元（body-acquisition/event-overviews）
3. 服务器起 cloudflared（双连接器并存）→ 公网匿名验证：
   `/` 200、`/api/v1/health/live` 返回服务器 release 标识、未授权 API 404、
   `/login`→`/`→200（SPA 客户端门禁）
4. 停 Mac 服务面（public-backend/public-frontend/cloudflare-tunnel/
   public-health-probe）
5. 服务器起 news-scheduler + event-summary-worker（先停 Mac 后起，避免双写）
6. 手动触发一次 `poydty-daily.service` 全链验证 + `source_fetch_audit` 落库确认

## 7. 回滚（一条路径，数据不动）

```bash
# 服务器下线
ssh ubuntu@YOUR_SERVER_HOST 'cd /opt/agent && sudo docker compose -f docker-compose.yml \
  -f docker-compose.prod.yml down && sudo systemctl stop poydty-*.timer poydty-event-overviews.service'
# Mac 重新上线（launchctl bootout 过的可直接 load）
for s in public-backend public-frontend cloudflare-tunnel public-health-probe \
         news-scheduler event-summary-worker event-overviews intelligence-daily \
         local-daily morning-brief; do
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.poydty.agent.$s.plist
done
```

Mac 数据库从未停写换库（仅暂停调度），秒级恢复；切换后 Mac 侧新增数据为空，
服务器侧数据独立演进——回滚会放弃服务器期间增量，属已知取舍。

## 8. 已知限制与观察项（2026-09-23 更新）

- 盘中实时期货降级（见第 2 节）；观察 `price_intraday` 各 provider 状态
- **已修复的连带阻断**：日度链质量门禁要求 8 品种盘中价全覆盖，原油
  （Brent/WTI）从香港无期货源曾导致 `public_benchmark_refresh` critical 失败
  → 全链 blocked。修复：`PUBLIC_SPOT_PAGES` 增加 Brent/WTI 的
  TradingEconomics 现货页（与 NAPHTHA 同主机同采集器同 B 级口径，
  `_crude_public_quote` 日期句绑定解析，测试
  `server/tests/test_crude_public_spot_pages.py`）。修复后 8/8 品种采集齐、
  门禁不放宽。国内期货代理价（PTA/MEG/PX 的 sina/eastmoney 通道）仍降级，
  由生意社现货页覆盖同名品种，不影响门禁。
- **push2his 机会性回退**：`_collect_eastmoney_hist_last_bar`（东财历史K线主机
  1 分钟线）作为期货瀑布末级回退；该主机对数据中心 IP 有 WAF 指纹/频控
  （探测期整 IP 被封持续 5 分钟以上），在被封时优雅报错降级，在家庭网络
  （如 Mac 回滚）下可用。测试 `server/tests/test_eastmoney_hist_kline.py`。
  注意 `.env` 的 `OUTBOUND_FETCH_HOSTS` 覆盖代码默认值，新主机需同步进 env。
- **Google News 发现文章的正文抓取范围**（2026-09-24 扩充）：
  `DISCOVERY_PUBLISHER_HOSTS` = finance.yahoo.com / www.cnbc.com / oilprice.com /
  **www.visualcapitalist.com / www.eastdaley.com**（后两个从香港实测可抓无墙）。
  ogj.com 有 Cloudflare 挑战、Reuters 401、WSJ 付费墙——刻意排除，不绕过。
  白名单外出版站的文章按设计保持 title_only（"原站未在自动采集范围内"），
  显示层会展示英文原题而非门禁文案。
- **2C 性能实测**：日度链全跑约 60-70 分钟；OOS 评估对 10GB 快照做前后
  双全文件哈希（各约 5 分钟 I/O）+ 评估本体，合计约 20 分钟。
  `PRODUCTION_SCHEDULER_DAILY_TIMEOUT_SECONDS=5400`、
  `LOCAL_DAILY_STEP_TIMEOUT_SECONDS=1800`（均 env 注入，勿低于此值）。
- **评估守卫竞态**：短时间多次手动重跑链会让 OOS 评估的
  `evaluation_input_database_changed_during_run` 守卫误触发（上轮未结束时
  下轮已开）。正常每日单跑不复现；手动重跑前确认
  `systemctl is-active poydty-daily.service` 为 inactive。
- **磁盘保留策略**（70G 盘关键）：每类 10GB 级产物只留最新 1 份——
  `daily-anchor`、`index-prune-anchors`、`evaluation-input`，由
  `/opt/agent/bin/retention.sh` 在每轮日度链末尾执行。不清理则每天 +20GB。
  稳态占用约 43G/69G。
- **行为一致性结论（2026-09-23 01:11 干净全链跑）**：benchmark completed
  零缺口、OOS 评估正常发布且证据有效；评分 0/21 与 Mac 同日结果完全一致
  （预报质量域既有状态，非迁移回归）。
- worker 的 docker unhealthy 是镜像健康检查误报（探 8000 端口），以
  `source_fetch_audit` 落库为准；并发链跑时偶发 SQLite 锁竞争会令
  event-summary-worker 一次性退出并由 restart 策略自愈。
- 每月 ~¥90；流量 2TB 足够。Mac 上 `127.0.0.1:7890` 代理解释了 Mac 能通
  Yahoo 而香港不能（Yahoo 封腾讯 IP 段）。
- 可选优化：腾讯控制台免费更换一次公网 IP，若 Yahoo 解封则原油恢复分钟级
  期货口径（现走 TE 现货页日更口径）。
- 注意 `docker compose up -d`（不带服务名）会启动循环式 `scheduler` 容器，
  与 systemd 09:30 定时器双跑——日常只 `up -d backend frontend
  news-scheduler event-summary-worker cloudflared`，scheduler 保持 stopped。
  重建镜像必须四服务一起：`docker compose build backend scheduler
  news-scheduler event-summary-worker`（compose 每服务独立镜像标签）。

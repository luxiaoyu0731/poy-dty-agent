# 长期维护方案（2026-09-24 制定）

适用：腾讯云香港 2C4G/70G 单机部署（ubuntu@YOUR_SERVER_HOST，密钥 ~/.ssh/your-project-key.pem）。
定位：单人运营的公网研判系统，目标是**每周 ≤2 小时维护量、故障自愈、风险有预案**。

---

## 0. 系统节奏（全自动，勿手动干扰）

| 时刻 | 事件 | 说明 |
|---|---|---|
| 08:00 | 日度链（poydty-daily.timer） | 采集→评估→OOS→锚备份，约 58-76 分钟 |
| 09:31 | brief 发布（poydty-brief.timer） | 设计发布时刻 09:30 后的幂等补跑；**日报 9:31 可读** |
| 每 30 分钟 | 新闻周期 + 雷达投影追平 | 游标应恒等于 news max rowid |
| 每小时 | 正文回补（poydty-body-acquisition） | |
| 每 10 分钟 | 健康探针（poydty-health-probe.timer） | 产出新鲜度 + 公网探针，供每日告警检查读取 |
| 周日 05:00 | 库压实（poydty-vacuum.timer） | VACUUM 后回收文件空间 |
| 常驻 | event-overviews / 六个 Docker 容器 | overviews 容器 `--network host` 直连 backend:8000 |

**红线**：手动重跑日度链前先 `systemctl is-active poydty-daily.service` 确认 inactive——
重叠运行会触发 OOS 评估的 `evaluation_input_database_changed_during_run` 守卫竞态。
不要 `docker compose up -d`（不带服务名）——会拉起循环式 scheduler 容器与 systemd 双跑。

## 1. 日常巡检（每天 5 分钟，早上读报时顺带）

```bash
ssh -i ~/.ssh/your-project-key.pem ubuntu@YOUR_SERVER_HOST '
systemctl is-active poydty-daily.service poydty-brief.service   # 应均 inactive（已跑完）
grep "===" /opt/agent/state/logs/daily-$(date +%F).log | tail -3 # 完成时刻
df -h / | tail -1                                               # 磁盘
'
curl -s https://app.kaipingrc.com/healthz                        # 公网探活（应 200）
```

浏览器读今日日报（09:31 后）；雷达最新事件时间应与当前小时同量级。

**异常判定**：
- brief 缺今日行 → `systemctl start poydty-brief.service` 补跑（幂等）
- 链 blocked 但 blockers 只有 OOS/quality → 已知质量域，非故障（见 §6）
- worker 容器 unhealthy → 误报（镜像健康检查探 8000 端口）；偶发 SQLite 锁退出会自愈，
  仅当最新事件观测停滞 >1 小时才需要人工介入（看 `docker logs`）

## 2. 磁盘治理（2026-09-24 完成重构手术，此后按设计自维持）

**已完成的手术**：152 个语义索引代际（10.8G 死块）修剪至 4 代 + VACUUM 压实，
库 15.5G → **1.7G**，磁盘 57G/69G → **29G/69G（余 37G）**。
根因复盘：周锚节流（7 天内不修剪）× 每 30 分钟一代 × 单次修剪上限 10 代 × 从不
VACUUM——四因素叠加使修剪速率永远追不上生成速率。

**防复发设计（已生效）**：
- `.env`：`SEMANTIC_INDEX_RETENTION_ANCHOR_MIN_AGE_DAYS=1`（修剪从每周 → 每日）
  + `SEMANTIC_INDEX_RETENTION_MAX_PRUNE=500`（单次上限足以清当日全部代际）
- `poydty-vacuum.timer`：**每周日 05:00** 自动停写入容器 → VACUUM → 重启
  （日志 `/opt/agent/state/logs/vacuum.log`）
- `/opt/agent/bin/retention.sh`：每日链末尾清三类锚（各留最新 1 份）+ wal checkpoint

**稳态预算**：库基线 ~1.7G，周内缓涨至 ~8G（当日代际的块行）后周日压实归位；
日锚 1 份（随库大小）；合计磁盘峰值 ~30G / 69G，长期安全。

**巡检口径**：`semantic_chunks > 5G` 或磁盘 >60% 即异常，查
`/data/backups/index-prune-anchors/prune-log.jsonl` 最新一条的 status
（`pruned` 正常；`skipped:weekly_anchor_active` 连续多天出现 = 策略失效告警）。

## 3. 数据与源健康

### 3.1 新鲜度（每天巡检项）
```sql
-- 投影是否追平（两数应相等）
SELECT cursor_after_json FROM intelligence_runs WHERE run_type='projection'
  ORDER BY created_at DESC LIMIT 1;          -- news_rowid
SELECT MAX(rowid) FROM news_articles;
```
- 落后 >2 小时：查 `intelligence_runs` 最近 projection 的 degraded_reasons
  （`projection_deadline_exceeded_resumable` 会自动续跑；持续落后才调
  `LIVE_INTELLIGENCE_PROJECTION_DEADLINE_SECONDS`，当前 90s）

### 3.2 已知源降级清单（误报，不要修）
| 源 | 状态 | 原因 |
|---|---|---|
| sina hq（盘中行情） | 403 | 香港机房地域封锁；日度 K 线主机 stock2 正常 |
| yahoo_finance_proxy | 429 | 腾讯 IP 段封锁；原油已由 TE 现货页覆盖 |
| eastmoney push2/实时 | 302/空 | 境外重定向延迟集群；push2his 分钟线为机会性回退（WAF 间歇） |

### 3.3 源结构变更（真故障）的发现与修复
- 征兆：某源连续 2 天 error 且 `preview` 显示验证页/改版 HTML
- 修复流程：本地加解析测试（`server/tests/`）→ 修 `server/app/` 对应解析器 →
  测试门禁跑过 → rsync → **四镜像全量重建**（backend/scheduler/news-scheduler/
  event-summary-worker 各有独立镜像标签，只建一个是本会话踩过的坑）→ 验证审计转 ok
- 新增数据源必须同步三处：source_registry.json、OUTBOUND_FETCH_HOSTS（**注意 .env
  里的同名变量会覆盖代码默认值，两处都要加**）、EXPECTED_SOURCE_IDS/OFFICIAL_HOSTS

## 4. 变更与升级流程

1. 本地改码 → `npm run check` + `npm run build`（前端）；后端测试：
   `DG01_TEST_DB_ROOT=/private/tmp/dg01-test-db TMPDIR=…/tmp SQLITE_PATH=…/agent.db
   server/.venv/bin/python -m pytest server/tests/...`（测试库用后即删，避免残留导致
   幂等断言失败）
2. 部署：rsync 代码（--exclude node_modules/.venv/.git 等）→ 四镜像 `docker compose build`
   → `up -d backend news-scheduler event-summary-worker`（**不要带 scheduler 服务名**；
   scheduler 常驻容器必须保持 stopped，否则会与 systemd 08:00 定时器双跑日度链）
   → 前端 rsync dist 后**必须**执行 `/opt/agent/bin/regen-release-json.sh <git_sha>`
   （`--delete` 会删掉服务器端 release.json，公网 /release.json 与健康探针依赖它）
3. 验证：`/api/v1/health/ready` 全绿 + 触发一次 `collect_intraday_prices` + 读当日审计
4. 回滚：git 工作树未提交时直接 revert 改动重新部署；运行态回滚靠 Mac 快照（见 §7）

**依赖更新**（每季度）：Docker 基础镜像（python:3.11-slim / node:22-alpine /
cloudflared）、Python 锁（uv sync）、npm。更新后全量跑一次后端测试再上线。

## 5. 监控与告警（2026-09-24 起链内闭环）

1. **链内探针（已上线）**：`poydty-health-probe.timer` 每 10 分钟跑产出新鲜度
   （check_output_freshness）+ 公网健康探针（probe_public_health），产物写入
   `/data/local-production/{output-freshness,public-health}/latest.json`，供每日告警检查读取。
   Mac 路径的 UNKNOWN 告警已消除。公网探针的"来源连续性"阈值已按香港出口现状调为
   `PUBLIC_HEALTH_MIN_HEALTHY_RATIO=0.7`（默认 0.8；已知 ~10 个源被永久封锁会拉低占比）
2. **告警通知（已降噪）**：`.env` 已设 `ALERT_PUSH_PROVIDER=log`（写入服务日志）。
   要推手机时改成 `serverchan`/`pushplus` + 对应 SENDKEY/TOKEN 即可，无需改码
3. **免费外部拨测**：UptimeRobot/Better Stack 盯 `https://app.kaipingrc.com/healthz`
   （匿名 200），宕机/隧道断线 5 分钟内手机可知——**零成本，优先做**
4. 服务器层：腾讯云控制台开免费告警（磁盘 >80%、CPU 持续 >90%、公网出流量异常）

## 6. 质量域 backlog（不阻塞运行，择期专项）

- **OOS 0/21 结构拆解（2026-09-24 查明）**：
  1. *样本成熟期*（19/21 格）：非原油品种的正式预测历史 09-18 才开始，
     有效样本 0-13 个（门槛 20）。h=1 约 10 月下旬达标、h=7 约 11 月中、
     h=30 约 12 月。工作台七品种节点已区分"样本积累中"与"方向/误差未达标"。
  2. *原油标签序列污染*（crude h7/h30 方向准确率 0.21/0.12 的主因）：
     标签用 `eia_petroleum_api` 布伦特现货（114-128 美元，且整周历史在
     09-23 20:25 一次性回填），而 Yahoo 期货/新闻口径是 99-102 美元——
     两参照系差 15-25 美元且走势脱节。**待决策**：是否把原油 OOS 标签
     换成市场一致序列（yahoo BZ=F 日收盘）或等待 EIA 修正。
  3. 链上 blocked → 反证扫描/日报解读跳过：属设计内 fail-closed，
     随前两项解决自然恢复。
- 日报 A/B 事实覆盖缺口（polyester_supply / logistics_geopolitics 域）
- EIA 130.8 宽基差：与上面原油标签问题同源，周三 EIA 更新后核对
- 摘要文案冗余（"0.89% 百分比"类）：摘要后处理清理

## 7. 备份与灾难恢复（当前缺口：服务器是单点）

- **现状**：卷内每日锚（自动）+ Mac 上 09-22 终态库（10.2G，已过期）
- **建议**（按性价比排序）：
  1. 每周把卷内最新 daily-anchor 拉回 Mac/NAS：
     `rsync server:/var/lib/docker/volumes/agent_agent-data/_data/backups/daily-anchor/最新 .`
     （~14G/次，家用带宽可接受；磁盘紧张可两周一次）
  2. 重要不可再生数据其实只有 agent.db——源可重抓、代码在 git
- **整机重建**：按 `docs/deploy-tencent-hk.md` 从零到公网约 3 小时（迁移实测口径）；
  数据从最近锚恢复
- **续费**：实例 **2026-10-22 到期（月付 ¥90）**——控制台开自动续费或设日历提醒，
  到期释放是唯一会丢一切的日程性风险

## 8. 成本预算

| 项 | 现状 | 上限/动作 |
|---|---|---|
| 服务器 | ¥90/月，流量 ~1% | 到期 10-22 续费 |
| 事件总览 LLM | $4.89 / $10 预算 | 触顶自动停（已内建） |
| DeepSeek 日预算 | 日额度+单价上限（.env） | 超限降级不中断 |
| 域名+Cloudflare | 免费档隧道 | 月流量 <2TB 免费安全 |

## 9. 周检/月检清单

**每周（10 分钟）**：磁盘与语义块体量 → 投影追平抽查 → 三个 timer 的 NEXT 正确 →
LLM 预算余量 → 拉回每周锚（§7）

**每月（30 分钟）**：依赖季度项顺延检查 → 全量后端测试本地跑一遍 → 换月后核对
流量包用量 → 检查证书/隧道连接器日志有无异常重连 → 复盘本月 blocked 天数与缺口分布（§6）

---

*本文档与 `docs/deploy-tencent-hk.md`（部署/回滚）、`docs/runbook.md` 配套使用。*
*沉淀自 2026-09-22~24 迁移与三日运营实测。*

# 多 Agent 预测系统全面缺陷与未完成工程审计（2026-10-02）

性质：只读审计报告。本报告是本次审计会话唯一写入的文件；所有修复由主会话执行。
方法：代码走读（生产者/消费者矩阵、参数对账、幂等重入）＋ 生产只读探针（backend 容器 uid 10001，仅 SELECT/ls）＋ 本地一次性探针脚本（/private/tmp/audit-tmp/probe_budget_crash.py）＋ 后端测试全量跑（3040 passed，235.5s）。
注意：审计期间生产 backend 镜像于 10-02 00:58 被主会话重建（含链模块），但 **agent-scheduler 镜像未重建**——见 P0-1。

---

## 一、分级发现清单

### P0（预测正确性 / 系统性失效）

#### P0-1 整条事件链（节点 A/B/融合/影子结算）在生产从未运行——scheduler 镜像陈旧
- **证据链**（全部生产实测）：
  - `agent-scheduler:latest` 构建于 **2026-09-28 02:35**（链代码 10-01 才进仓库）；容器内 `import app.event_signal` → `ModuleNotFoundError`。backend 新镜像（10-02 00:58）则含链模块。
  - `scripts/cloud/daily.sh:20-21` 经 `docker compose run --rm scheduler` 跑日度链 → 用的是陈旧镜像。
  - 生产 `/data/local-production/latest-status.json`（10-01 08:16 北京时间完成）：`event_signal: null`、`agent_chain: null`、lifecycle 无 `event_fusion`/`event_factor_settlement` 字段。
  - `/data/local-production/` 仅有 latest-status.json，**无** `event-signal-latest.json`、**无** `event-agent-chain-latest.json`。
  - `forecast_event_factors` **0 行**；`agent_lessons` **0 行**。
- **后果**：治理文档 §12 步骤 8 的"影子上线 ≥2 周"从未开始计时；所有"生产 08:00 链"描述只对仓库成立。基线 `direction` 不受影响（21 格合同未动），失效的是整条新链。
- **主会话验证入口**：`ssh -i ~/.ssh/your-project-key.pem ubuntu@YOUR_SERVER_HOST "sudo docker run --rm --entrypoint python agent-scheduler:latest -c 'import sys; sys.path.insert(0,\"/app/server\"); import app.event_signal; print(\"ok\")'"`（当前报 ModuleNotFoundError 即复现）。
- **建议修复**：`docker compose build scheduler && docker compose run --rm scheduler python server/scripts/run_production_scheduler.py --once --apply` 后核验 latest-status.json 出现 event_signal/agent_chain 键、DB 出现 forecast_event_factors 行。部署清单应把"五个 compose 服务镜像全部重建"写成硬步骤（docs/cloud-daily-operations.md 补登）。

#### P0-2 R5 裁决分支绕过全部融合约束（置信度阈值、先验支持、O1 期限门控、质疑保守修正）
- **证据**：`server/app/event_fusion.py:224-225`：
  ```python
  if product in adjudicated_factors:
      result = FusionResult("R5", factor_direction, "冲突裁决Agent裁决后重跑融合规则")
  ```
  裁决品种直接取裁决因子方向为 `event_adjusted_direction`——不检查 `confidence≥0.6`、不检查先验支持 ≥2、**不检查 horizon_days==1 的 O1 门控**（同文件 :75-78 明确"D1 cells always keep the baseline"）。裁决因子由 `run_event_adjudication_async`（:332-372）直接产出，从未过 `_conservative_revision` 或质疑 Agent；`skeptic_verdict:"裁决"` 只是标签。
- **矛盾点**：本文件 docstring（:18-19）与治理 §3.3 均称"裁决后**重跑融合规则**"；实现是直通。10-01 audit 文档称"O1 已部署生产"，但 R5 路径不受 O1 约束——裁决一旦成功，D1 格可被任意置信度的裁决因子切换。
- **当前可达性**：被 P0-1 压制（链没跑）；P0-1 修复后立即暴露。触发条件=跨品种矛盾＋裁决 LLM 调用成功。
- **测试现状**：`server/tests/test_event_fusion.py:159-181` 把"R5 无条件切换"固化为断言（测的是现状而非设计）；`test_fuse_cell_rules_r1_through_r4` 所有调用都不传 `horizon_days` → **O1 门控零测试覆盖**。
- **主会话验证入口**：`server/app/event_fusion.py:224`；复现：构造 adjudicated_factors 含 d1 方向因子，断言现实现返回 R5+切换（与 fuse_cell 自身 O1 矛盾）。
- **建议修复**：R5 分支改为 `fuse_cell(baseline, adjudicated_factor, …, horizon_days=cell.horizon_days)`，裁决因子先过 `_conservative_revision`（以合成+质疑后的因子为原值）；补 O1×R5 交叉测试。

### P1（功能失效 / 方法论失真）

#### P1-1 存活事件 >8 → `BudgetExhausted` 崩整链（应按 §3.4 模板回退）
- **证据**：`server/app/agent_chain.py:353` `self.budget.reserve(stage)` 抛 `BudgetExhausted`（RuntimeError 子类、**非** ModelPortError）；`run_historical_analog`（:550）与 `_synthesize_product_once`（:632）、`_skeptic_product_once`（:700）都只 catch `ModelPortError` → 异常穿透到 `run_local_daily.py:576` 的兜底 → node B "degraded"、当日报告不写、已消耗的 LLM 调用（political ≤16 + analog 8）全部作废。
- **生产触发面**：16 候选淘汰规则只删"exec<0.3 且 media_report"（:481-491），≥9 个存活即崩。**探针实证**：9 存活事件 → `CHAIN RAISED: BudgetExhausted: stage_budget_exhausted:historical_analog`（/private/tmp/audit-tmp/probe_budget_crash.py）。
- **回放为何没发现**：`REPLAY_BUDGETS` political=5/analog=5 且默认 `--events-per-day 5`，恒不触发；生产预算形态（16/8）从未被回放覆盖。
- **次生风险**：与 P1-2 叠加 → 崩溃日之后前一日链报告被当日 lifecycle 消费。
- **主会话验证入口**：跑上述探针；修复后同探针应产出 status=ok/degraded 报告（超出预算事件走 no_prior 模板）。
- **建议修复**：`_ask` 捕获 `BudgetExhausted` 转为 ModelPortError 语义（或调用点逐事件 try/except → 该事件回退模板、链继续）；补">8 存活事件"回归测试。

#### P1-2 lifecycle 消费陈旧链报告无 business_date 校验（跨日影子污染）
- **证据**：`server/scripts/run_local_daily.py:843-845` 只查 `is_file()`；`server/scripts/run_seven_product_forecast_lifecycle.py:222-228` 载入后不校验 `business_date`；`:147` 融合行的 business_date 直接用链报告值。输出目录 `/data/local-production`（`run_production_scheduler.py:15`，滚动共享，非按天分目录）。展示端 `prediction_event_factors.py:71-73` 倒是做了日期一致性防护——**防护不对称**。
- **场景**：D1 节点 B 成功；D2 节点 B 崩溃/跳过（P1-1、DeepSeek 故障、`AGENT_CHAIN_ENABLED` 关）→ D2 lifecycle 读到 D1 的 `event-agent-chain-latest.json` → D1 因子融进 D2 批次，fusion 行 business_date=D1、batch_id=D2。
- **主会话验证入口**：`run_seven_product_forecast_lifecycle.py:222`；复现：造一份 business_date=昨日的链报告 JSON，`--event-chain-report` 指向它，apply 后查 forecast_event_factors.business_date 与批次日期不一致。
- **建议修复**：lifecycle 载入处校验 `chain.business_date == 发牌 business_date`（或 as_of 同日），不匹配记 warning 跳过融合；节点 B 失败日主动删除/改名旧报告更稳。

#### P1-3 事件 taxonomy 与案例 taxonomy 双轨错位 → 类型化检索与条件先验全部失效
- **证据（生产实测）**：
  - 事件层 category：`energy(5931) / geopolitics_sanctions(5084) / shipping_ports(2925) / weather_disaster(2608) / plant_supply(2364) / other(1536) / macro_policy(431)`（`industrial_intelligence/identity.py:70-78` 的 CATEGORIES）。
  - 案例层 event_type：`macro_finance(T2 1396+legacy54+T1 5) / sanctions_geopolitics(66+14) / oil_policy(29+23) / shipping_security(11+1) / company_capacity(6+2)`。
  - 两集合**交集为空**。`retrieve_case_cards`（agent_chain.py:944）`same_type` 恒空 → 恒退全库（靠 tier/重叠度排序兜底）；`compute_empirical_prior(event_type=…)`（:846-848）恒空 → 恒退**全局**分布——prompt 里注入的"同类历史事件条件分布"从来不是条件于类型的。
- **影响**：治理 §5.3"结构化过滤（event_type…SQL）"落空；O2"先验库升级为统计源"的统计源在生产形态下只剩全局基率。25y 回放同病（`build_replay_25y.py:42-48` 的 CATEGORY_MAP 是第三套映射，把案例类型映射成事件层类别，但案例行本身的 event_type 未动）。
- **主会话验证入口**：生产容器内 `SELECT DISTINCT event_type FROM political_case_memory` vs `SELECT DISTINCT category FROM intelligence_event_revisions`；或本地跑 `compute_empirical_prior(as_of_time=…, event_type='macro_policy')` 断言返回 {}。
- **建议修复**：建立唯一映射表（案例 event_type ↔ 事件 category），`retrieve_case_cards`/`compute_empirical_prior` 入口做映射；或把两处统一改用事件层 8 类 taxonomy 重标案例库。

#### P1-4 prompt 基线与生产发牌基线三处参数失真
- **证据**（`agent_chain.py:878-915` `compute_baseline_for_prompts` vs `seven_product_forecast.py:679-689` `_cell`）：
  | 维度 | 生产 `_cell` | prompt 基线 |
  | --- | --- | --- |
  | 中性带底 | `NEUTRAL_FLOOR_PCT[target]`（crude/poy/dty 0.006，其余 0.008） | 硬编码 `0.005` |
  | 序列窗口 | 全序列（loader 最多 5000 点） | `values[-120:]` |
  | 期限 | `projection_days = horizon + 距最近观测天数`（issue-calendar.v1） | 纯 `1/7/30` |
- **后果**：合成/质疑 Agent 论证的基线方向与实际发牌 `direction` 不一致——带宽窄 17%-60% 使 prompt 基线在临界区间更常判 up/down（例：预测涨幅 +0.55% 时 naphtha prompt 基线说 up、生产说 neutral），`disagreement_with_baseline` 与 R1 确认语义错位。另：节点 B 直读实时库（`load_current_label_series`），发牌用 `capture_main_inputs` 冻结快照，存在小时级时点差。
- **主会话验证入口**：`agent_chain.py:904`（`neutral_floor_pct=0.005`、`values[-120:]`）；对比 `seven_product_forecast.py:683-689`。
- **建议修复**：prompt 基线直接复用发牌同款投影调用（传 `NEUTRAL_FLOOR_PCT[target]`、全序列、issue-calendar 语义）——最干净是让 lifecycle 先算好 21 格 direction 再喂节点 B，或 `compute_baseline_for_prompts` 内部改调 `build_seven_product_forecast(forecast_contract="issue-calendar.v1")` 只读结果。

#### P1-5 回放结算带宽与生产结算语义不一致（§8.4 未满足）
- **证据**：回放 `replay_event_chain.py:53` `SMOKE_NEUTRAL_BAND=0.005` 扁平用于 `actual_direction`（:370）；生产结算 `seven_product_forecast_ledger.py:618` `actual_direction = _direction(actual_change, cell.neutral_band_pct)` 用**逐格 robust 带**（`max(floor, 0.5σ√h)`，D30 常达数个百分点）。
- **影响**：所有回放实验的方向命中绝对值与生产结算不可比。25y 文档自认"0.5% 窄结算带→绝对值天然低"，同带 A/B 增量的辩护对相对增益成立；但 audit-and-o1-o2 用"总体 26.1%<55%"做的淘汰判定，其绝对线口径与生产 OOS（robust 宽带）不是同一把尺。
- **主会话验证入口**：`replay_event_chain.py:53,370` vs `seven_product_forecast_ledger.py:615-618`。
- **建议修复**：回放结算改调生产 `_direction(change, robust_band)`（带宽由同款公式从历史序列算出），重跑 25y/E1 聚合对比两口径差值。

#### P1-6 E1 模式对照组是硬编码恒 neutral 假基线
- **证据**：`replay_event_chain.py:371-375` 非 25y 分支 `baseline_direction=("neutral", {})`；同函数 :325-327 给链的 `baseline_context` 却是 `compute_baseline_for_prompts` 真方向——链与真基线辩论、融合对假 neutral 判定；`:403` `baseline_hit = "neutral"==actual` 不是生产 direction 命中。25y 模式已修（`_baseline_direction_robust`），E1 未修。
- **主会话验证入口**：`replay_event_chain.py:374`；E1 输出 JSON 里 `baseline_direction` 恒 "neutral" 即复现。
- **建议修复**：E1 与 25y 统一用 robust 基线函数；`baseline_hit` 用同一基线方向对 actual 判定。

### P2（质量 / 设计承诺落空）

#### P2-1 ADR-3"质疑→合成 1 轮返工"是生产形态下的死代码
- **证据**：`agent_chain.py:705` 返工条件 `used[SYNTHESIS] < caps[SYNTHESIS]`；FORMAL_PRODUCTS 恒 7 产品 × 每产品 1 次预留 = 恒 7/7 → False。**探针实证**：skeptic 全部输出 rework_request，synthesis artifacts 恰 7、无返工。即使放开预算，返工复审 `_ask(SKEPTIC)`（:709-714）不检查 skeptic 预算，7+1=8>7 会以 P1-1 机制崩链。
- **主会话验证入口**：探针 P2 段（/private/tmp/audit-tmp/probe_budget_crash.py）。
- **建议修复**：synthesis caps 提到 14（或返工预留独立额度）＋复审前检查 skeptic 预算；补返工路径测试。

#### P2-2 "40/日硬顶由 deepseek_client 基础设施强制"（§11.2）未实现
- **证据**：`set_http_attempt_budget` 全仓只有 `event_overview_store.py:301` 使用（limit=1）；`DeepSeekJsonPort` 的 client `http_attempt_limit=None`（deepseek_client.py:222-223 默认）。现行约束只是链内 BudgetManager 算术（16+8+7+7=38）＋`MAX_ADJUDICATIONS=2`；且 `_ask` 的 schema 修复重试不重复预留（1 预留最多 2 次 HTTP），传输层重试（`DEEPSEEK_MAX_RETRIES`）不计入任何预算。
- **主会话验证入口**：`grep -rn set_http_attempt_budget server/ --include="*.py" | grep -v tests`。
- **建议修复**：`DeepSeekJsonPort.__init__` 里 `client.set_http_attempt_budget(当日余量)`（从 llm_calls 台账算当日已用），使 40/日成为真硬顶。

#### P2-3 `horizon_impact` 与 `strength` 装配后无消费者
- **证据**：`event_signal.py:75` 装配 horizon_impact 进冻结候选集，但 `_political_user_prompt`（agent_chain.py:404-420）注入 facts/inferences/counterevidence/supply_chain_paths/direction_by_product——**不含 horizon_impact**；融合层也不用。事件库现成的 D1/D7/D30 结构化方向判断被冻结后从未被读。`factor_by_horizon.strength` 同理：融合只用 direction+confidence（event_fusion.py:215-216），展示层也不读。
- **主会话验证入口**：`grep -rn horizon_impact server/app/agent_chain.py`（无匹配）。
- **建议修复**：政治 prompt 注入 horizon_impact（给 LLM 事件库自己的期限判断做参照）或停止装配省 schema；strength 要么进融合（如 R4 弱因子细分）要么从合同里降级为记录字段。

#### P2-4 测试缺口：O1 门控、R5 约束、预算崩溃、陈旧报告、依据卡 e2e、新节点抽屉
- O1：`test_event_fusion.py` 无任何 `horizon_days=1` 用例。
- R5：现测试固化绕过行为（见 P0-2）。
- 预算崩溃：无">8 存活事件"用例。
- 陈旧报告：无 lifecycle×旧 business_date 用例。
- e2e：`/api/v1/prediction/event-factors` 无用例（tests/ 全目录 grep 无匹配）；18 节点图抽屉 e2e（tests/app.spec.ts:638-667）只点 counter_scan，新预测节点（event_signal 等 7 个）详情块无渲染用例（后端 builder 已在 pipeline_graph.py:957-1108，前端渲染未验证）。
- taxonomy：无"生产 category → 案例检索"映射用例。

#### P2-5 25y/E1 实验口径杂项
- e1 as_of=`08:00 UTC`（=北京 16:00），比生产 08:00 北京发牌多 8 小时知识（replay_event_chain.py:285）。
- `_event_prediction_days`（:144-195）按锚日 |return| 降序选日：极端日选择偏差（文档已承认"极端波动日锚定"）。
- 注入事件 `relevance/severity` 用**事件日实际涨跌**计算（build_replay_25y.py:201-202）——预测晨（事件+1 08:00Z）确实可知事件日收盘，无泄漏，但结果幅度渗入热度排名（叠加极端日选择，放大"大事件日"权重）。
- `anchor_day = day`（预测晨）与注释"prices settle from the event day close"（replay_event_chain.py:298）不符——结算起点是预测晨价，注释误导。
- `build_replay_25y.py:52` 硬编码占位 FRED_KEY 且注释"replaced from env below"无对应代码（该 key 仅入 source_url 元数据，未实际请求）。

### P3（卫生 / 语义粗放）

#### P3-1 事件 status 不过滤：注入 "resolved" 与生产 open/monitoring 混流
- `list_latest_events` 仅显式传 status 才过滤（industrial_intelligence/storage.py:937-939）；`event_signal.py:132-139` 不传。当前无下游读 status → 无错误行为；但"resolved 事件的方向判断进事前预测"的语义值得一次明确决策（至少对注入库如此——build_replay_25y.py:161 全部 status="resolved"）。生产事件库 status 混合是常态。

#### P3-2 T2 案例 1396 条全部 `affected_products=["crude"]`（apply_t2_cases.py:71）
- 非 crude 品种检索重叠度恒 0 → 分层退化为 tier+日期；`tier_rank`（T1/legacy 优先）缓冲了淹没，检索仍精选在前。真实损失=crude 之外先例池薄（T1 仅贸易战等个别事件带 px/pta）。

#### P3-3 legacy 166 条 `visible_at=2025-12-31T23:59:59Z` 一刀切（生产实测；backfill_legacy_posteriors 明示保留）
- 对 2026 生产无害（全可见）；对 2025 年内日期回放是保守延迟（事件结局 event+30 即可知，却拖到年末）——方向安全（无泄漏），信号损失小。

#### P3-4 节点 A 窗口/池语义
- 窗口按 `last_seen_at` 过滤（storage.py:943-948）：旧事件被重爬会重新进入 7 天窗（与 event_signal docstring "seen inside" 自洽，与"近 7 天事件"直觉不同）。
- 池先按 relevance 排序截断 64（limit×4）再热度排序（event_signal.py:131-152）：高 severity/urgency 低 relevance 事件可能在池截断时丢失（16 选中前已被 relevance 切掉）。

#### P3-5 测试会话尾部 "DG01 database safety gate failed: controlled test database environment changed during the session"
- 3040 passed 之后打印（本次实测）——某测试会话内修改了受控环境，安全门 teardown 报警未失败。值得定位是哪个测试改了 SQLITE_PATH（replay 测试 `object.__setattr__` 嫌疑最大）。

### 证伪项（怀疑清单核对为不成立）

| 怀疑 | 结论 | 证据 |
| --- | --- | --- |
| forecast_event_factors upsert ON CONFLICT 重置 outcome_* 清结算 | **证伪** | storage.py:6731-6741 `DO UPDATE SET` 不含 outcome_baseline/outcome_adjusted → 冲突时保留结算结果；仅新插入行写 NULL。重跑融合不清结算。 |
| 前端依据卡端点目录与生产写出目录不一致 | **证伪** | backend 容器实测 `PIPELINE_GRAPH_STATE_ROOT=/data`（来自 .env）→ `_local_production_dir()=/data/local-production`，与 scheduler 写出目录一致。依据卡为空的根因是 P0-1。 |
| compute_empirical_prior 生产链未调用 | **证伪（接线在）** | 生产 runner（run_local_daily.py:519-567）不传 cases_by_event → run_historical_analog 走真实路径并调用（agent_chain.py:525-528,515-519）。**但** P1-3 使其退化为全局分布——接线在、语义失效。 |
| run_event_fusion_step 裁决端口生产不可达 | **代码层证伪** | lifecycle apply＋链报告存在时实例化 `DeepSeekJsonPort()`（run_seven_product_forecast_lifecycle.py:141-151），路径同目录同名。真实不可达原因= P0-1（链报告从未产出）。 |
| 教训注入接线缺失 | **证伪** | run_local_daily.py:553 注入 `list_active_agent_lessons`；agent_lessons 表空是因为蒸馏 job 未写（未完成工程），不是接线问题。 |

---

## 二、参数对账表（生产 vs prompt 基线 vs 回放 vs 治理文档）

| 概念 | 生产发牌 | compute_baseline_for_prompts | 回放（e1/25y） | 治理文档 | 判定 |
| --- | --- | --- | --- | --- | --- |
| 中性带底 | NEUTRAL_FLOOR_PCT 0.006/0.008 | **0.005 硬编码** | 0.005（基线带与结算带同值） | §3.3 未给值 | ✗ 失真（P1-4/P1-5） |
| 投影窗口 | 全序列（≤5000 点） | **[-120:]** | [-120:] | — | ✗ 失真 |
| 期限语义 | horizon+日历偏移（issue-calendar.v1） | **纯 1/7/30** | 纯 1/7/30 | — | ✗ 失真 |
| 结算带宽 | 逐格 robust 带 max(floor,0.5σ√h) | — | **0.005 扁平** | §8.4"与生产一致" | ✗ 违诺（P1-5） |
| E1 对照基线 | —（本身即基线） | 真方向（喂链） | **恒 neutral（判融合）** | §7.1"对照组=现行纯价格" | ✗ 违诺（P1-6） |
| 融合置信度阈值 | 0.6（event_fusion.py:39） | — | 0.6（fuse_cell 默认） | §3.3 0.6 | ✓；O3 校准未做 |
| 先验支持数 | ≥2（:40） | — | ≥2 | §3.3 ≥2 | ✓；O3 未做 |
| O1 D1 恒基线 | fuse_cell :75-78 | — | fuse_cell 同款 | 实验文档 | ✓ R1-R4 路径；**R5 例外**（P0-2） |
| 阶段预算 | 16/8/7/7/2 | — | **5/5/7/7/2**＋events_per_day=5 | §3.1 16/8/7/7/2；§7.1 允许回放降配 | 回放合法但掩盖 P1-1 崩溃形态 |
| 事件窗口 | 7 天（last_seen_at） | 同 | 同 | §3.2 近 7 天 | ✓（P3-4 语义注记） |
| 热度权重 | 0.5/0.25/0.25 | — | 同 | 背景描述一致 | ✓ |
| 淘汰规则 | exec<0.3 且 media_report | — | 同 | §3.2 阶段1 | ✓；exec_prob 缺失时不淘汰（宽容） |
| 案例 tier 分层 | T1/legacy=0，T2=1 | — | 同 | §9.2b | ✓（依赖 metadata.tier） |
| 案例→事件类型匹配 | — | — | — | §5.3 结构化过滤 | **✗ 双 taxonomy 永不相交**（P1-3） |

---

## 三、消费者矩阵附录（产出字段 → 实际消费者）

| 产出 | 生产者 | 声称/期望消费者 | 实际消费者 | 判定 |
| --- | --- | --- | --- | --- |
| interest_map / power_structure / transmission_path / speech_act | 政治 Agent | 合成 prompt、依据卡 | 合成 prompt（整体 JSON 注入，agent_chain.py:589-601）；依据卡只取 speech_act.label+execution_probability+reasoning | 部分：interest_map/transmission_path 仅作 prompt 噪声，无结构化消费 |
| horizon_impact | 事件库→节点 A 冻结集 | 期限化方向判断 | **无**（政治 prompt 不含、融合不读） | ✗ P2-3 |
| direction_by_product（政治 Agent） | 政治 Agent | 合成/淘汰 | 合成 prompt；`_min_direction_confidence` 入信封 | ✓ |
| execution_probability＋speech_act | 政治 Agent | 淘汰规则 | run_political_analysis:481-491 | ✓ |
| analog_top3 / prior_by_horizon / prior | 类比 Agent | 合成 prompt、R2 先验门 | 合成 prompt；`prior_by_event_from_chain_report`→`_prior_for_product`（R2/R3） | ✓（依赖合成回显 supporting_event_ids） |
| median_magnitude_pct | 类比 Agent | 幅度判断 | 仅 prompt 展示，融合/结算不用幅度 | 记录性 |
| factor_by_horizon.strength | 合成/裁决 Agent | §3.2 因子三元组 | **无**（融合只用 direction+confidence） | ✗ P2-3 |
| factor_by_horizon.direction/confidence | 合成（经质疑修正） | 融合 R1-R4 | fuse_cell | ✓（R5 直通例外 P0-2） |
| skeptic verdict/revised factors | 质疑 Agent | product_factors | product_factors:741-764 | ✓；rework_request 死代码（P2-1） |
| counter_evidence / cross_product_consistency | 质疑 Agent | 依据卡/审计 | 仅信封落库 | 记录性 |
| 裁决 factor | 裁决 Agent | "重跑融合规则" | fuse_batch R5 分支直通 | ✗ P0-2 |
| event_adjusted_direction + 双轨结算 | 融合层 | 影子对比/依据卡 | forecast_event_factors + settle_event_factor_outcomes + _fusion_rows | ✓（生产 0 行，P0-1） |
| agent_lessons | 蒸馏 job（缺） | 全链 prompt | run_local_daily 注入（表恒空） | 接线✓ 内容✗ |
| covered_products / input_sha256 | 节点 A | 节点卡片/审计 | pipeline_graph 卡片 | ✓ |
| empirical prior（类型条件） | compute_empirical_prior | 类比 prompt 统计源 | 注入✓ 但恒全局（P1-3） | ✗ 语义失效 |

---

## 四、未完成工程清单（对齐治理文档逐项）

| 项 | 治理出处 | 现状证据 |
| --- | --- | --- |
| Mem0 校准记忆 | ADR-5/§4.2 | mem0ai 零 import（仅 agent_lessons.mem0_id 列）；ADR 偏差已知未补记 |
| 周度蒸馏 job | §4.2/§4.4 | 无脚本；生产 agent_lessons=0 |
| O3 阈值校准 | 10-01 audit 文档 | 0.6/≥2/O1 全硬编码 |
| 正式 E3（训练截止分段） | §7.3 | 25y 文档明示"待确认截止日后重跑" |
| 全日子回放（生产宽带口径） | 10-01 audit | 未做（现有=极端日锚定样本） |
| 幅度/误差指标 | §7.1 | 回放仅方向命中，无幅度误差 |
| MEG 负增益排查 | 10-01 audit 待办 | 25y 只有 crude，MEG 未回放 |
| 2001-2014 GDELT v1 | §9.2b | 自认未做 |
| 依据卡 e2e / 新节点抽屉 e2e | §12 步6 | tests/ 无匹配（P2-4） |
| §12 验收表收录 25y/O1/O2 | §12 | 表仍止于"影子上线 ≥2 周" |
| ADR-6 修订正式入第 14 章 | §14 | §14 仍写"影子优先晋级；回放仅淘汰门禁"；操作者修订（晋级证据=历史回放）只散见 build_replay_25y docstring 与实验文档 |
| T1 全窗回放增益量化 | §9.1b 待办 | 未做 |

---

## 五、按优先级的修复建议（方案＋验证命令，不含补丁）

1. **[P0-1] 重建并部署 scheduler 镜像**：`docker compose build scheduler`（或全服务 `docker compose build`）→ `docker compose run --rm scheduler python server/scripts/run_production_scheduler.py --once --apply` → 验证：latest-status.json 含非空 event_signal/agent_chain；`SELECT COUNT(*) FROM forecast_event_factors` >0。同时把"五服务镜像齐重建"写入 docs/cloud-daily-operations.md 部署清单。
2. **[P0-2] R5 回归融合规则**：`fuse_batch` 裁决分支改调 `fuse_cell`（含 horizon_days）＋裁决因子过 `_conservative_revision`；测试补 `fuse_cell(horizon_days=1)` O1 用例与 R5×O1 用例。验证：`python3 -m pytest server/tests/test_event_fusion.py -q`。
3. **[P1-1] 预算耗尽转模板回退**：`_ask` 或三个 run_* 调用点把 `BudgetExhausted` 降级为该事件/品种 fallback；回归测试=探针场景（9 存活事件 → status ok、analog 超限事件 no_prior）。
4. **[P1-2] lifecycle 校验链报告日期**：`chain.business_date == ledger_batch.business_date` 不等则跳过融合并告警；测试造旧日期报告断言跳过。
5. **[P1-3] taxonomy 统一**：建 `CASE_TYPE_TO_EVENT_CATEGORY` 映射（5 类→8 类）并在 `retrieve_case_cards`/`compute_empirical_prior` 入口应用；测试断言 `compute_empirical_prior(event_type="macro_policy")` 非空。
6. **[P1-4] prompt 基线对齐生产**：`compute_baseline_for_prompts` 改用 `NEUTRAL_FLOOR_PCT[target]`＋全序列＋issue-calendar 偏移（或直接复用 `build_seven_product_forecast` 只读输出）；测试对比 prompt 基线 direction 与同 cutoff 发牌 direction 逐品种一致。
7. **[P1-5/P1-6] 回放结算与对照基线对齐**：结算改用生产 robust 带；E1 基线改 `_baseline_direction_robust`；重跑 `25y` 与 `e3` 聚合并在实验文档双口径并列。
8. **[P2 批次]**：返工环预算修复（synthesis caps≥14＋复审预算检查）；`DeepSeekJsonPort` 接 `set_http_attempt_budget`（40/日真硬顶）；horizon_impact 注入政治 prompt 或停装配；补 O1/R5/崩溃/陈旧报告/依据卡 e2e/新节点抽屉 e2e。
9. **[P3 批次]**：status 过滤决策（至少注入库标 open/monitoring）；FRED_KEY 占位清理；anchor_day 注释修正；定位 teardown 安全门报警测试。
10. **[文档]**：§12 验收表补 25y/O1/O2 行；§14 补记 ADR-6 修订与 ADR-5 偏差；docs/cloud-daily-operations.md 补部署清单项。

---

## 六、验证与证据附录

- 后端测试：`DG01_TEST_DB_ROOT=/private/tmp/audit-tmp TMPDIR=/private/tmp/audit-tmp SQLITE_PATH=/private/tmp/audit-tmp/agent.db python3 -m pytest server/tests --ignore=server/tests/test_e2e_frontend.py --ignore=server/tests/test_readiness_http.py` → **3040 passed（235.53s）**；teardown 尾部见 P3-5 报警（不影响通过）。
- 探针脚本：`/private/tmp/audit-tmp/probe_budget_crash.py`（P1-1、P2-1 实证；脚本在 /tmp，不入库）。
- 生产只读探针（backend 容器 uid 10001，仅 SELECT/ls/stat）：
  - `political_case_memory`：1607=T2 1396＋legacy 166＋T1 45；tier×event_type 分布见 P1-3。
  - `intelligence_event_revisions` category 分布：energy 5931 / geopolitics_sanctions 5084 / shipping_ports 2925 / weather_disaster 2608 / plant_supply 2364 / other 1536 / macro_policy 431。
  - `agent_lessons`=0；`forecast_event_factors`=0。
  - `agent-scheduler:latest`（2026-09-28 构建）无 `app.event_signal`；`agent-backend:latest`（2026-10-02 00:58）有。
  - `/data/local-production/` 仅 latest-status.json（10-01 08:16 北京），链产物缺失；latest-status 无 event_signal/agent_chain 键。
  - 另见 `/data/agent.db.corrupt-20261001.gz`（10-01 库损坏事故残留，与既有运维记录一致）。
- 本地库（server/data/agent.db，旧 schema 副本）：legacy 166 条 visible_at 全为 2025-12-31T23:59:59Z、event_date 2025-06-16..2025-12-19（P3-3 佐证）。

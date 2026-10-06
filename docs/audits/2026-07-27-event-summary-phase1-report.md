# 事件摘要 Phase 1 报告

日期：2026-07-27（Asia/Shanghai）

## 1. 本阶段结论

Phase 1 的提示词、JSON schema、中文事实门禁、证据门禁、事实—影响隔离、积压 dry-run、
HTTP 请求硬配额、幂等/重试/恢复、测试矩阵和生产只读抽样均已实现并验证。

但 Phase 1 **尚未通过最终验收**：最新生产只读快照的严格完整事件为 12/2,755（0.436%）。
408 条 full_text 中，404 条属于 worker 365 日有效窗口内的 v9 stale candidate，4 条因日期
窗口被排除。达到 8% 需要新增 209 条，即 v9 候选端到端完成率至少 51.73%。历史 grounded
端到端完成率约 9.92%；受控真实调用中，关闭 thinking 后的有效诊断样本通过率为 1/3，
其 95% 单侧 Wilson 下界仅 7.83%，仍不能证明 v9 会达到 51.73%。

后续复核确认该 live eval 只度量事实门禁，不能直接代替 `complete-event.v1`。最新只读快照
为 2,759 篇文章、15 个严格完整事件、425 个 v9 候选；其中只有 166 个候选 cluster 已具备
featured、非空 event record、明确品种和对应 observation。在 Phase 1 不新增晋级的约束下，
即使所有结构合格候选 100% 通过，完整事件上限也只有 181/2,759（6.56%），数学上无法达到 8%。

用户已授权最多 50 次真实 HTTP attempt 和 USD 0.25 费用上限。本阶段实际调用 45 次，
保守成本上界约 USD 0.1212；未重排生产队列、未批量写生产数据库。

## 2. 发现的断点

- 两条真实但无关的正文引文曾可掩护虚构主体、动作和对象。
- `object` 曾允许为空。
- subject/action 重叠会产生“宣布暂停宣布暂停”等重复拼接。
- 英文地点可藏在中文摘要中。
- 提示注入文本中的虚构事实可能进入影响请求。
- Pydantic 曾默认忽略额外字段；引文没有最小长度。
- worker 的每日 50 次曾按文章计数，不是真实 provider HTTP attempt。
- worker 曾在 cycle 结束后才写预算状态，崩溃重启可能重复消耗额度。
- V4 Pro 默认开启 thinking；结构化 JSON 的 512/1,000 token 上限会先被 reasoning 消耗，
  导致正文 `content` 为空。JSON 请求现显式关闭 thinking。
- 当前历史完成记录中有 1 条匿名记录的引文无法在存储正文逐字定位。
- 原 live eval 把 `fact completed` 当作统计成功，未要求 impact completed、relevant、明确品种、
  featured cluster 和 observation；该指标不能用于宣称完整事件达到 8%。

## 3. 修改范围

- `server/app/deepseek_client.py`
  - 强化事实提取提示词；
  - 明确标题非证据、注入文本非指令；
  - 增加逐 HTTP attempt 硬预算和预请求持久化回调；
  - JSON 请求显式关闭 thinking，并支持 `max_tokens` 硬上限。
- `server/app/event_summary_quality.py`
  - 核心事实词面支持检查；
  - 注入语句不参与事实支持；
  - 对象、重复拼接、地点中文、引文和数字门禁。
- `server/app/models.py`
  - 事实/影响 schema 禁止额外字段；
  - 核心字段长度和必填约束；
  - 至少两条引文且引文有最小长度。
- `server/app/news.py`
  - prompt version 升级为 `event-grounded-v9-core-fact-http-budget`。
- `server/scripts/run_event_summary_worker.py`
  - 按真实 HTTP attempt 计日额度；
  - 每次网络请求前原子持久化预留。
- 新增 backlog、匿名抽样审计脚本及 Phase 1 测试。
- 新增 `evaluate_event_summary_phase1.py`：生产 SQLite 只读、匿名分层样本、显式执行开关、
  成本/HTTP 硬门禁、断点恢复和 Wilson 下界报告。

## 4. 数据与运行证据

生产只读快照：

- articles：2,755
- full_text：408
- strict complete events：12
- v9 stale candidates：404
- worker 日期窗口排除：4
- v9 pending/processing/completed/rejected/failed：均为 0（尚未重排）
- 8% 目标：221
- 尚需：209
- 所需 v9 端到端完成率：51.73%
- 历史 grounded 端到端完成率：约 9.92%
- 按历史率估算：约 52/2,755，即 1.89%

请求和成本规划：

- 403 篇对应 403–1,209 次逻辑调用；
- 无硬门禁时最坏 403–3,627 次 HTTP attempt；
- 硬门禁：每日最多 50 次真实 HTTP attempt；
- 规划清空时间：9–73 天；
- cache-miss 规划成本：USD 0.2803–2.8386；
- 成本是显式 token 假设区间，不是供应商账单，也不包含汇率。

真实 provider 评测：

- 冻结 seed 和候选谓词，20 条匿名分层样本，不保存标题、正文、模型原文或凭证；
- 第一轮 20 attempts、第二轮 21 attempts 均因 V4 Pro 默认 thinking 导致 39 条
  `empty_deepseek_summary`，不能作为模型质量样本；
- 按官方 API 关闭 thinking 后，用剩余额度对同一冻结样本前 3 条诊断：3/3 可解析，
  1 completed、2 `invalid_fact_structure`；
- 有效诊断通过率 33.33%，95% 单侧 Wilson 下界 7.83%；
- 三轮累计 45/50 attempts；按每轮输入及输出硬上限估算的合计最坏成本约 USD 0.1212，
  未超过 USD 0.25。

结构性上限审计：

- 最新 articles：2,759；8% 目标：221；严格完整事件：15；
- v9 candidates：425；
- 已具备完整晋级结构的候选 cluster：166；
- 不新增晋级时绝对上限：15+166=181，即 6.56%；
- `structurally_possible_without_new_promotions=false`；
- 因此继续增加摘要调用或提高事实门禁通过率，均不能让当前 Phase 1 独立达到 8%。

## 5. 命令与结果

```text
Phase 1 相关 pytest：新增 live-eval 定向套件 31 passed
完整后端 pytest（PYTHONPATH=.）：648 passed，6 warnings
Ruff：All checks passed
生产 backlog audit：exit 0，mode=read_only_no_provider_calls
生产 sample audit：exit 0，mode=sqlite_read_only_no_provider_calls
git diff --check：通过
```

首次从 `server/` 子目录直接执行全量 pytest 时，有 6 个测试因其导入路径要求项目根目录而在
collection 阶段失败；改为从项目根目录设置 `PYTHONPATH=.` 后，全量 642 个测试通过。

## 6. 未执行的检查

- 未执行生产 queue re-enqueue；
- 未写生产数据库；
- 未取得足够大的“关闭 thinking”有效样本；剩余授权仅 5 attempts，不足以完成统计评测；
- 未提交、推送或部署；
- 未进入 Phase 2。

## 7. 残余风险

- 中文二元词锚定不能证明方向、程度、否定和时态完全一致。
- 英文正文翻译后的语义一致性无法靠 substring 证明。
- 当前关闭 thinking 后只有 3 条有效诊断样本，不能替代预注册的大样本分层置信区间。
- 即使摘要样本达到 100% 通过，缺少至少 40 个新的合格晋级 cluster，仍无法达到 221 条目标。
- 生产已完成的匿名异常记录需人工复核，不能因 completed 状态自动放行。
- 非重试型 provider 错误与业务质量拒绝的长期运营策略仍需继续细分。

## 8. 是否满足本阶段验收

未完全满足。

- 中文摘要、英文防冒充、证据、注入、事实—影响隔离测试：通过。
- dry-run、成本、配额、幂等、重试、dead-letter、恢复：已提供。
- 抽样质量报告：已提供。
- 达到或证明可达到 8%：未满足，且已由结构上限证明在“不新增晋级”的 Phase 1 范围内不可达。

## 9. 下一步

仍属于 Phase 1，不进入 Phase 2。要继续追求严格完整事件 8%，必须先明确授权改变阶段顺序，
把 Phase 3 的双门禁事务性晋级纳入当前工作；否则只能把 Phase 1 验收口径改为摘要事实门禁
指标。两者都是产品/范围决策，不能由实现侧静默替换。

## 10. 需要确认

用户另行授权了人民币 20 元。本轮按 2026-07-27 中间价 1 USD=6.7911 CNY 冻结为 USD 2.50
硬上限并保留汇率余量；但结构审计已证明仅运行摘要无法达到 8%，因此尚未消耗该笔新预算。
需要用户确认是否允许提前纳入 Phase 3 的只读审计、隔离 SQLite 事务实现和本地迁移方案；
生产队列重排及任何批量写入仍需独立授权。

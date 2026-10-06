# 事件体系 Phase 0 基线报告

生成日期：2026-07-27（Asia/Shanghai）

## 结论

本报告建立三个严格分离的口径：

1. **生产数据库权威文章口径**：运行中服务实际使用的 SQLite 中一行
   `news_articles` 为一篇文章。
2. **生产公开事件口径**：`GET /api/v1/workbench/event-library` 返回的 canonical 事件集合。
3. **仓库本地数据库诊断口径**：`server/data/agent.db` 中一行 `news_articles` 为一篇文章。

仓库本地数据库不是生产快照。公开事件数不得与文章数、cluster 数或 observation 数直接比较。

## 完整事件唯一标准

唯一版本为 `complete-event.v1`，实现位于
`server/scripts/audit_event_pipeline_funnel.py::COMPLETE_EVENT_SQL`。

一个事件只有同时满足以下条件才完整：

- 摘要输入为 `full_text`；
- `summary_status`、`quality_status`、`fact_summary_status` 均为 `completed`；
- `impact_analysis_status=completed` 且影响被判定为相关；
- 正式事实摘要非空并通过中文比例门禁；
- cluster 为 `featured`，具有非空 `event_record_id` 和结构化明确影响品种；
- 存在同一 `event_record_id` 的 observation，且 observation 也具有结构化明确影响品种。

来源 tier 不参与完整事件判定。

## 生产数据库权威基线

诊断脚本以 SQLite `mode=ro` 和 `PRAGMA query_only=ON` 连接运行中服务实际打开的数据库，
未读取环境变量、密钥或 Cookie，未执行写语句。

文章及正文：

| 指标 | 数量 | 占生产文章 |
| --- | ---: | ---: |
| news_articles | 2,754 | 100.00% |
| full_text | 379 | 13.76% |
| partial_text | 2,203 | 79.99% |
| title_only | 95 | 3.45% |
| metadata missing | 77 | 2.80% |

摘要队列：

| 状态 | 数量 | 占生产文章 |
| --- | ---: | ---: |
| not_queued | 82 | 2.98% |
| pending | 273 | 9.91% |
| processing | 0 | 0.00% |
| completed | 55 | 2.00% |
| rejected | 2,344 | 85.11% |
| failed | 0 | 0.00% |

事实与影响状态：

| 状态组 | 状态 | 数量 |
| --- | --- | ---: |
| quality | completed | 55 |
| quality | rejected | 2,344 |
| quality | pending | 273 |
| fact | completed | 55 |
| fact | rejected | 66 |
| fact | pending | 2,551 |
| impact | completed | 22 |
| impact | irrelevant | 22 |
| impact | rejected | 11 |
| impact | not_requested | 2,617 |

关键阶段转化：

- full_text / article：379/2,754 = 13.76%
- fact completed / full_text：55/379 = 14.51%
- impact completed / fact completed：22/55 = 40.00%
- complete event / impact completed：12/22 = 54.55%
- complete event / article：12/2,754 = 0.436%

其他对象：

- clusters：1,952（candidate 1,735；featured 217）
- event observations：461
- linked observations：217
- standalone observations：244
- cluster + standalone observation（canonical alias 去重前）：2,196
- `complete-event.v1`：12/2,754，即 **0.436%**

生产摘要维度：

- prompt：`event-facts-v1=2,277`、`event-grounded-v8-durable-stage-status=394`、
  `event-grounded-v2=1`
- 模型：`deepseek-v4-pro=2,672`
- provider：`deepseek=2,672`
- 主要来源：`google_news_oil_rss=801`、
  `google_news_v2_oil_policy=475`、
  `google_news_v2_middle_east_shipping=440`、
  `google_news_v2_sanctions=267`
- 主要拒绝原因：`input_not_full_text=2,277`、
  `legacy_low_information_summary=1,856`、
  `unsupported_evidence_quote=52`、`incomplete_business_impact=11`、
  `missing_action=10`、`non_chinese_factual_summary=10`、`missing_subject=9`

生产重复项：

| 类型 | 重复组 | 超额记录 |
| --- | ---: | ---: |
| 原始 URL | 0 | 0 |
| canonical URL | 5 | 15 |
| 标准化标题 | 31 | 39 |
| content hash | 3 | 3 |

## 生产公开基线

查询时间：2026-07-27 02:44（Asia/Shanghai）。通过 28 个只读分页请求取回
2,757/2,757 条事件，无分页缺口。

| 指标 | 数量 | 占公开事件总数 |
| --- | ---: | ---: |
| 公开事件 | 2,757 | 100.00% |
| full_text | 373 | 13.53% |
| partial_text | 2,193 | 79.54% |
| title_only | 136 | 4.93% |
| metadata missing | 55 | 1.99% |
| 中文事实摘要 ready | 53 | 1.92% |
| 影响 completed / analysis available | 21 | 0.76% |
| 严格完整事件公开代理 | 21 | 0.762% |

公开摘要状态：

- queued：252
- awaiting_source：2,329
- grounding_review：65
- ready：53
- schema_review：3
- 状态缺失：55

公开事实状态：

- pending：2,507
- rejected：64
- completed：53
- 状态缺失：133

公开影响状态：

- not_requested：2,571
- completed：21
- irrelevant：21
- rejected：11
- 状态缺失：133

公开严格代理 21 条多于生产文章 SQL 的 12 条，因为公开集合还包含独立 observation 和
历史已晋级数据。客户正式完整事件的权威分子采用生产数据库 `complete-event.v1=12`，
公开代理只用于解释前端集合。

## 本地数据库基线

执行：

```bash
server/.venv/bin/python server/scripts/audit_event_pipeline_funnel.py \
  --db server/data/agent.db \
  --output /tmp/phase0-event-funnel.json
```

文章及正文：

| 指标 | 数量 | 占本地文章 |
| --- | ---: | ---: |
| news_articles | 2,394 | 100.00% |
| full_text | 10 | 0.42% |
| partial_text | 118 | 4.93% |
| title_only | 2,194 | 91.65% |
| metadata missing | 72 | 3.01% |

队列：

| 状态 | 数量 |
| --- | ---: |
| not_queued | 45 |
| pending | 10 |
| processing | 0 |
| completed | 2,292 |
| rejected | 47 |
| failed | 0 |

`completed=2,292` 主要是旧 prompt 记录，不等于事实门禁完成：旧记录仍可能是
`quality_status=pending`、`fact_summary_status=pending` 和 `impact_analysis_status=not_requested`。

其他对象：

- clusters：1,773（candidate 1,577；featured 196）
- event observations：440
- linked observations：196
- standalone observations：244
- cluster + standalone observation（canonical alias 去重前）：2,017
- `complete-event.v1`：0/2,394

本地摘要维度：

- prompt：`event-facts-v1=2,290`、`event-grounded-v2=43`、
  `event-grounded-v6-zh-summary=16`
- 模型：`deepseek-v4-pro=2,349`
- provider：`deepseek=2,349`
- 主要来源：`google_news_oil_rss=493`、
  `google_news_v2_oil_policy=475`、
  `google_news_v2_middle_east_shipping=440`、
  `google_news_v2_sanctions=267`
- 拒绝原因：`insufficient_source_text=43`、`project_irrelevant=4`、
  `unsupported_evidence_quote=3`、`missing_action=1`、`missing_subject=1`、
  `non_chinese_factual_summary=1`

重复项：

| 类型 | 重复组 | 超额记录 |
| --- | ---: | ---: |
| canonical URL | 2 | 3 |
| 标准化标题 | 21 | 26 |
| content hash | 2 | 2 |

## 公网数量与数据库数量不一致

`workbench/event-library` 的集合不是 `news_articles`、`news_event_clusters` 或
`event_observations` 任一表的直接计数。它：

1. 聚合文章/cluster 事件及未被 cluster 覆盖的独立 observations；
2. 排除已经由 `cluster.event_record_id` 表示的重复 observation；
3. 按共享 article、cluster、canonical URL 和同日标准化标题做 alias 去重；
4. 再应用搜索、分类、排序、分页和短期缓存。

生产与本地数据库的更新时间和部署路径也不同。因此公网 2,757、本地文章 2,394 和本地
cluster/standalone observation 2,017 均是合法但不同的口径。

## 数据流

```text
source registry
  → compliant fetch
  → raw/detail parsing and dedupe
  → full_text / partial_text / title_only classification
  → news_articles
  → news_event_clusters(candidate)
  → event_ai_summaries queue
  → grounded fact extraction
  → Chinese/evidence/number gate
  → independent business-impact gate
  → atomic cluster featured + event_observation promotion
  → /api/v1/workbench/event-library
  → frontend event list/detail
```

事实门禁失败时影响分析不得执行。来源等级只表达出处可信度，不决定方向、影响品种或晋级。

## 验证

- 漏斗指标测试：3 passed
- 既有事件安全门禁测试：66 passed
- Ruff：通过
- `git diff --check`：通过
- 本地审计连接：SQLite `mode=ro` 且 `PRAGMA query_only=ON`
- 英文事实摘要不能满足 `complete-event.v1`

## Phase 0 验收结论

- 总量口径唯一且可复现：通过。
- 完整事件标准可写成 SQL/代码谓词：通过。
- 漏斗数字具有生产只读查询证据：通过。
- 当前基线报告已固化：通过。

Phase 0 验收通过。按阶段约束，本报告完成后停止，不自动进入 Phase 1。

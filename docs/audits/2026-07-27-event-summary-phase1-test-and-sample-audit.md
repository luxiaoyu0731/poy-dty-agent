# Phase 1 事件摘要测试矩阵与抽样质量审计

日期：2026-07-27（Asia/Shanghai）

## 结论

本审计只覆盖 Phase 1 的测试充分性和生产只读抽样，不代表 Phase 1 整体验收。

- 生产 `completed/rejected` 共 2,399 条，其中 completed 55、rejected 2,344。
- 55 条 completed 的正式摘要均通过当前客户中文谓词，中文通过率为 100%。
- 55 条 completed 中 54 条未发现本审计定义的结构问题；1 条存在已落库引文无法在
  `raw_text` 中逐字定位的异常，匿名样本 ID 为 `4cfb66872565`。该记录同时标记
  `fact_summary_status=completed`、`impact_analysis_status=completed`，应在批处理前人工复核，
  不能因数据库状态为 completed 而忽略。
- 最终相关确定性测试共 98 个，全部通过；无真实 provider 请求。

## 测试矩阵

| 场景 | 可观察结果 | 测试证据 | 结果 |
| --- | --- | --- | --- |
| 中文正文/中文事实 | 正式摘要可用 | `test_phase1_language_and_grounding_matrix[natural_chinese]` | 通过 |
| 英文正文/中文提取 | 中文字段和逐字英文引文可共存 | `test_gate_allows_chinese_extraction_from_english_when_quotes_are_grounded` | 通过 |
| 英文摘录冒充中文 | 拒绝且不请求影响分析 | `english_excerpt`、`test_english_excerpt_with_chinese_prefix_is_not_a_customer_summary` | 通过 |
| 英中混合字段 | 长英文事实字段被拒绝；必要缩写允许 | `mixed_english_excerpt`、`supported_acronym` | 通过 |
| 不完整正文 | partial/title-only 不产生正式摘要 | `test_phase1_incomplete_text_never_becomes_formal_summary` | 通过 |
| 有依据数字 | 数字和单位可本地化，仍保留溯源 | `test_factual_summary_localizes_*` | 通过 |
| 无依据数字 | 拒绝且阻断影响分析 | `invented_number`、`test_gate_rejects_number_*` | 通过 |
| 无依据引文 | 拒绝 | `unsupported_evidence` | 通过 |
| 标题/正文冲突 | 标题明示为非证据；标题独有引文和正文不支持的核心事实均拒绝 | `test_title_only_quote_is_not_accepted_as_body_evidence` 及 prompt 契约测试 | 通过 |
| 提示注入 | prompt 将输入指令视为内容；无正文支撑的注入事实被拒绝 | `prompt_injection_as_fact` 及 prompt 契约测试 | 通过 |
| 免责声明/模型话术 | 拒绝 | `disclaimer_pollution` | 通过 |
| 重复拼接 | 相同渲染片段不重复 | `test_phase1_rendered_summary_does_not_repeat_identical_fact_fragments` | 通过（基础场景） |
| 媒体页面数字污染 | 视频时长、下载量不进入正式摘要 | `test_factual_summary_omits_media_asset_metrics` | 通过 |
| 事实失败后影响研判 | 第二次模型请求不得发生 | `test_deepseek_does_not_request_impact_when_fact_gate_rejects` | 通过 |

## 生产只读审计

执行工具：

```bash
server/.venv/bin/python server/scripts/audit_event_summary_phase1_samples.py \
  --db "<production-agent.db>" \
  --sample-per-stratum 3
```

脚本使用 SQLite URI `mode=ro` 并执行 `PRAGMA query_only=ON`。输出仅包含聚合指标、状态、
来源 ID、prompt version、语言和 SHA-256 截断匿名 ID；不输出标题、正文、正式摘要、
fact payload、provider 错误或凭证。

### 总体结果

| 指标 | 数量 |
| --- | ---: |
| completed | 55 |
| rejected | 2,344 |
| completed 中文谓词通过 | 55 |
| completed 无审计标志 | 54 |
| completed 有审计标志 | 1 |
| 分层匿名样本 | 101 |

抽样规则固定为：按 `summary_status + source_id` 分层，各层按匿名 ID 排序取前 3 条。
该规则可复现但不是随机统计推断；completed 总体 55 条则全部执行了机器质量检查。

主要拒绝原因：

| 原因 | 数量 |
| --- | ---: |
| input_not_full_text | 2,277 |
| legacy_low_information_summary | 1,856 |
| unsupported_evidence_quote | 52 |
| incomplete_business_impact | 11 |
| non_chinese_factual_summary | 10 |
| missing_action | 10 |
| missing_subject | 9 |

另有多种 `untraceable_number:*` 明细。它们保留具体数字有助于内部排错，但客户侧不得展示。

## 测试充分性缺口

### P1：核心字段的词法锚定不能证明语义一致

本阶段并发实现已增加 `unsupported_core_fact`，会用原文包含和中文二元词重合率筛掉明显无关的
主体、动作和对象。这是必要的词法门禁，但仍不能判断方向、程度、否定和时态是否与引文语义
一致，例如“大幅增加”和“小幅增加”可能共享足够多的二元词。需要增加受控事实对齐器，以及
“增/减、启/停、制裁/解除、已经/计划”等最小对立词和程度冲突测试。

### P1：跨语言标题冲突仍依赖逐字正文引文

系统提示明确标题只用于定位且冲突以正文为准；本地测试也证明只出现在标题、未出现在正文的
引文会被拒绝。中文正文还会对主体、动作和对象执行词面锚定。残余边界是英文正文翻译后的核心
字段无法用 substring 证明语义等价，仍需依赖逐字正文引文、固定对立语义用例和 provider eval。

### P1：生产 completed 中存在 1 条引文异常

匿名记录 `4cfb66872565` 的至少一条 evidence quote 无法在已存 `raw_text` 中逐字定位，
但状态仍为 fact/impact completed。应先人工复核是历史版本、清洗差异还是门禁回归；在确认前
不得把该异常静默算作抽样合格。

### P2：病句和术语替换仅有局部断言

现有测试覆盖 ISO 时间、数量级、英文数量限定词和媒体元数据，但没有系统的词法表测试，
也没有对主谓粘连、单位前空格、重复主体/动作、错误机构译名的自然度评分。建议建立固定
golden case，而不是依赖快照全文相等。

### P2：提示注入主要验证 prompt 和证据门禁组合

当前不调用真实 provider 的测试证明无依据注入内容无法通过，但不能证明不同模型在对抗文本下
稳定遵循系统提示。后续 eval 应使用受控 provider 预算并保持结果仍经过相同本地门禁；本轮未调用。

## 执行证据

```text
pytest（Phase 1 相关文件）：98 passed
ruff（新增脚本和测试）：All checks passed
git diff --check（本子任务文件）：通过
```

未执行：

- 未调用 DeepSeek 或其他 provider；
- 未写生产数据库；
- 未运行批量回填；
- 未进入 Phase 2；
- 未把跳过的 provider 对抗 eval 计为通过。

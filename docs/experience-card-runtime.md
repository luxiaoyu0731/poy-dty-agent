# Experience Card 纯计算运行契约

版本：`experience-card-runtime.v1`

本包实现 Phase A Experience Card 的确定性计算层，不连接数据库、不读取系统当前时间、不写文件、不调用 LLM。调用者必须显式提供预测批次、目标和基准价格路径、有效观测日及其日历 ID/版本、评估时点、上一 revision 及序列资格解析器。

## 身份和边界

稳定 `experience_card_id` 由以下字段共同确定：

- `prediction_batch_id`
- `node_id`
- `subtarget`
- `target_series_id`
- `identity_version`

POY 与 DTY 必须分别调用并生成不同 Card，禁止取均值。非末端节点不得携带 POY/DTY subtarget。

当前 Phase A Experience Card v1 尚未把 `prediction_batch_id`、`checkpoint_prediction_id`、`subtarget` 和 `target_series_id` 列为必需字段。纯计算结果将这些字段作为明确的运行时身份扩展输出；数据库或公共 API 接入前，必须通过后续 Schema 版本冻结其公开兼容语义。

## 渐进成熟和 revision

成熟期限使用调用方提供的有效观测日，不使用自然日差：

```text
首个有效观测日     → d1_preliminary
第 7 个有效观测日  → d7_intermediate
第 30 个有效观测日 → d30_mature
```

每次晋级产生新 revision，`previous_revision_id` 指向上一个成熟阶段。不得跳过阶段或倒退。相同阶段、相同计算输入返回既有 revision；相同阶段数据发生修订时生成新的不可变 revision。D+14 仅保留在旧系统的历史读取路径，不能创建新 Experience Card。

`horizons`、`prediction_ids_by_horizon` 和 `directions_by_horizon` 必须各自且彼此精确覆盖 `{1,7,30}`。期限 token 只接受非布尔整数 `1`、`7`、`30` 或规范字符串 `"1"`、`"7"`、`"30"`；浮点、布尔、空白、正号、前导零、小数形式、缺失、额外期限或 D+14 均失败闭合。

计算指纹绑定完整预测 bundle、预测 revision、数据快照、日历 ID/版本、重叠事件、完整有效观测日表，以及锚点和后验选中观测的 observation/revision/值/单位/报价/可见性身份。任一冻结输入变化都会产生新 revision，不能误返回 `unchanged`。

POY/DTY 的 target 是按冻结 `poy-dty-upstream-cost-pressure.v1` 公式从 PTA、MEG 共同有效观测日计算的成本压力指数；它不是 POY/DTY 成交或报价价格。Experience 的独立 benchmark 分别为 CCF 的 `POY 150D/48F` 和 `DTY 150D/48F低弹`日度评估路径，不能再使用同一 PTA/MEG 篮子作 benchmark。派生 target 的 revision 必须绑定滚动 20 个共同有效日内全部 40 条 PTA/MEG immutable capture revision，缺一条即不可评分。

## 价格路径和指标

锚点是预测冻结日及之前、在预测冻结时已经可见的最后一个合格 revision；预测后出现的同日更正不得覆盖锚点。后验路径则按 evaluation-as-of 解析当时已经可见的最新完整 revision 链。禁止插值、向前填充、代理补点或混用单位和报价口径。

对每个检查点：

```text
raw_change_abs = end_price - start_price
raw_change_pct = raw_change_abs / abs(start_price) × 100
relative_change = target_change_pct - benchmark_change_pct
```

上涨方向乘数为 `+1`，下跌方向为 `-1`：

```text
signed_return[t] = direction_multiplier × raw_return[t]
MFE = max(0, max(signed_return))
MAE = max(0, -min(signed_return))
```

`days_to_peak` 取第一次达到最大有利变化的有效观测日序号，从 1 开始。`first_reversal_at` 只有在路径先出现严格有利累计变化、随后回到或穿过零轴时才记录；从未有利不伪造反转。中性或不确定方向不定义方向型 MFE/MAE，且不可评分。

## strict-as-of 门禁

严格评分要求：

- 目标和基准序列均由显式注入的 resolver 在综合契约、授权和运行时状态后明确返回 `eligible`；`contractible` 本身不等于可评分，默认不得假定任何序列可用。
- 锚点在预测冻结时已经可见。
- 后验点在评估时点已经可见。
- 所有点质量为 `eligible`、visibility mode 明确为 `strict_as_of`、价格为正，且 revision 链唯一完整。
- 目标及基准各自的单位和报价口径在完整路径内一致；二者必须是独立经济路径，禁止把同一原料篮子同时充当 target 和 benchmark。
- 检查点所需有效观测日完整，无缺失或插值。
- 输入不是 `reconstructed`。
- 方向为可评分的 `up` 或 `down`。

任一门禁失败仍可输出诊断指标，但必须设置：

- `scoreability=unscorable`
- `diagnostic_only=true`
- 完整 `exclusion_reasons`
- `eligible_for_retrieval_at=null`
- `mechanism_support_status=inconclusive`

当前 Phase A 所有正式价格和正式目标序列均为 blocked。因此真实契约 resolver 必须令其不可评分；测试中的可评分路径只使用显式的测试专用 resolver，不代表生产能力。

## 持久化与 API 后续依赖

本包不复用 `prediction_ledger.review_status` 或 `historical_validation_assets` 保存 Card。持久化需要后续独立的不可变 revision 表；API 接入还需要模型、路由、OpenAPI 和兼容策略。上述工作不属于本纯计算包。

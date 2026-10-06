# Shadow 因子生命周期纯计算契约

版本：`shadow-factor-lifecycle-evaluation.v1`

本包实现 Shadow 因子的确定性生命周期评估，不连接数据库、不访问网络、不读系统时间，也不写文件。调用方必须显式提供因子身份、透明基线身份及透明性声明、当前状态、滚动样本外窗口、逐样本结果、带版本的阈值政策，以及是否请求退休。基线未声明为透明时阻断晋级。

## 冻结门禁

晋级同时要求：

- 至少 120 个 `scorable` 样本；
- 至少 3 个有可评分样本的滚动样本外窗口；
- 每个窗口相对透明基线的平均误差增益均达到政策阈值；
- 在因子和基线同时达到高置信阈值的 paired cohort 上，因子错误率相对基线的增量不超过政策阈值；
- paired cohort 样本量达到政策要求；
- 窗口与样本没有顺序错误或未来泄漏。

治理文档没有冻结“稳定增益”和“高置信错误”的数值。调用方因此必须传入以下 policy 字段：

```text
policy_version
minimum_window_gain
maximum_high_confidence_error_rate_delta
high_confidence_threshold
minimum_high_confidence_paired_samples
```

缺少 policy 或任一字段时，评估返回 `gate_status=blocked`，绝不采用隐含默认值。`minimum_window_gain` 必须严格大于 0；`maximum_high_confidence_error_rate_delta` 必须位于 `[-1, 0]`，因此政策不能允许错误率恶化。120 个样本和 3 个窗口是不可降低的治理下限，不由 policy 覆盖。

## 输入语义

窗口按调用顺序提供，每个窗口包含 `window_id`、`train_end`、`evaluation_start`、`evaluation_end`。时间必须带 UTC offset。训练结束必须早于评估开始；窗口必须严格递增、不重叠，后一个训练边界不得早于前一个评估结束。

每个样本包含 `sample_id`、`window_id` 和 `scoreability`。`blocked` 或 `unscorable` 样本必须提供非空 `exclusion_reasons`；它们不进入分母，并按“状态:原因”汇总报告。

`scorable` 样本还必须包含：

```text
prediction_at, outcome_visible_at
factor_error, baseline_error
factor_confidence, baseline_confidence
factor_incorrect, baseline_incorrect
```

误差必须为有限非负数，数值越低越好。均值使用稳定的有限数值聚合；聚合溢出或产生非有限值时，该窗口阻断且输出指标为 `null`，不得输出 `NaN` 或无穷值。窗口增益定义为：

```text
mean(baseline_error) - mean(factor_error)
```

高置信错误率只使用因子和基线置信度都达到 `high_confidence_threshold` 的样本交集。因子与基线严格使用同一个 `paired cohort` 和同一个分母；交集样本量达到 `minimum_high_confidence_paired_samples` 后才比较，避免不同样本构成造成偏差。

## 严格顺序和泄漏阻断

每个可评分样本必须满足：

```text
train_end < prediction_at
evaluation_start <= prediction_at <= evaluation_end
prediction_at < outcome_visible_at <= evaluation_end
```

时间泄漏不会作为普通不可评分样本静默排除，而会阻断整次晋级评估并报告样本 ID。未知窗口同样阻断。

## 生命周期状态机

唯一状态为 `shadow`、`promoted`、`degraded`、`retired`：

| 当前状态 | 门禁通过 | 门禁失败 | 显式退休请求 |
|---|---|---|---|
| `shadow` | `promoted` | `shadow` | `retired` |
| `promoted` | `promoted` | `degraded` | `retired` |
| `degraded` | `promoted` | `degraded` | `retired` |
| `retired` | `retired` | `retired` | `retired` |

`retired` 是吸收态。评估失败不会自动退休，退休只能由显式输入触发。

## 调用示例

```python
from app.shadow_factor_lifecycle import evaluate_shadow_factor_lifecycle

result = evaluate_shadow_factor_lifecycle(
    factor_id="factor-pta-spread",
    baseline_id="transparent-naive-direction-v1",
    baseline_is_transparent=True,
    current_status="shadow",
    windows=windows,
    samples=samples,
    policy={
        "policy_version": "shadow-promotion.v1",
        "minimum_window_gain": approved_gain,
        "maximum_high_confidence_error_rate_delta": approved_error_delta,
        "high_confidence_threshold": approved_confidence,
        "minimum_high_confidence_paired_samples": approved_minimum,
    },
)
```

返回值包含逐窗口指标、样本/窗口数量、排除原因、高置信错误率比较、完整门禁原因、当前与下一状态。所有输出只由参数决定。

## 回放 Shadow 样本安全装配

`server/app/shadow_sample_input_assembler.py`提供
`shadow-sample-input-assembly.v1`纯函数边界。它只接受形状符合
`plan_sequential_replay()`输出的caller-supplied `assemble_shadow_sample`动作、
显式窗口、治理身份以及可选的投影引用，并返回可直接作为
`evaluate_shadow_factor_lifecycle(**lifecycle_input)`参数的封闭输入。该边界不读
数据库、网络、文件、系统时间或调度状态，也不持久化结果。

当前已有按内容Hash读取已提交checkpoint动作的reader，但它只证明动作属于持久化
计划，不证明该计划的上游候选事实已经审计；纯装配器仍不读取该reader，因此每个样本
继续明确携带`replay_action_provenance_unverified`。同理，现有
outcome引用尚不能证明其target与预测对象完全绑定，携带
`outcome_binding_unverified`。完整样本身份绑定动作、完整window内容以及三类
完整projection内容的SHA-256，而不只绑定revision ID。

schema v33现已提供Shadow候选因子与透明基线的append-only revision及精确只读reader。
透明基线可在spec获批后按代码规则重算；因子输出在缺少受控执行trace或代码重算器时
仍固定阻断，不能因spec获批而自证输出。当前也没有获批的误差metric contract。纯装配
器尚未接入这些reader，投影引用因此只能声明为
`unaudited`或`unapproved`；调用方不能通过自报`audited`把它升级为可评分事实，
也不能在投影中夹带error、confidence或correctness数值。缺失、未审计、未批准、
非透明基线或未批准metric contract都会生成`blocked`样本。任何动作或投影为
`reconstructed`时，样本稳定生成`unscorable`并带`reconstructed_evidence`，二者
都不会进入晋级分母。

`server/app/shadow_metric_contracts.py`现提供内容寻址的候选合同注册表，但生产批准
trust root固定为空，调用方只能按digest选择已知候选，不能提交合同正文或自报批准。
首个候选使用`down/neutral/up`三分类方向的未缩放multiclass Brier sum；因子概率使用
和严格等于1的canonical decimal strings，透明基线由代码生成`1:1:1`等权有理数且
不读取数据。confidence为最大类别概率；方向错误定义为真实类别不在完整argmax集合，
并列时保留全部最大类别、不做任意tie-break，neutral是独立类别。该合同不包含高置信
阈值或晋级阈值；这些仍只能来自另行批准的lifecycle policy。均匀基线的最大概率固定
为`1/3`且完整argmax包含三类，因此只有`high_confidence_threshold <= 1/3`时才会进入
paired cohort。未来若批准该候选，必须把合同digest与满足这一条件的lifecycle policy
原子绑定；当前候选不得单独进入生产trust root。生产trust root获得独立
治理批准前，计算入口稳定返回`shadow_metric_contract_unapproved`且不产生生命周期指标。

装配器严格限制D1、D7、D30，并逐项校验产品、节点、期限、点时可见性、回放
幂等键和Shadow policy版本。它只传递调用方显式给出的数值policy，不提供默认
阈值。真正可评分的Shadow样本仍须后续增加独立受审计的factor/baseline投影
reader并冻结metric contract；本边界本身不能执行晋级、启动scheduler或声称
Shadow已投入生产。

在verified lifecycle-state与retirement reader落地前，装配器不接受调用方状态
或退休请求，输出固定为`current_status=shadow`、`retirement_requested=false`。
这避免调用方利用装配接口降级、恢复、退休或绕过正式生命周期状态所有权。

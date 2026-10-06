# 顺序历史回放纯计划契约

版本：`sequential-replay-plan.v1`

`server/app/sequential_replay.py`只负责生成确定性的历史回放动作和诊断。它不读取或写入数据库、文件、网络、系统时间或任务状态，也不会调用Experience、Shadow、预测服务或调度器。独立的`server/app/sequential_replay_checkpoint_store.py`只持久化该纯计划器的精确输出和运行事件；生产动作执行、调度及09:30实时任务避让仍属于后续适配层。

## 冻结范围和运行模式

历史边界固定为2025-01-01至2026-07-01，首尾均包含。所有checkpoint必须按`as_of`严格递增，同一日只允许一个checkpoint，且必须位于冻结范围内。

显式policy包含：

```text
policy_version
run_mode: dry_run | full
reconstructed_evidence_handling: exclude_unscorable
expected_checkpoint_dates: [YYYY-MM-DD, ...]
```

`expected_checkpoint_dates`是已经由上游治理过的交易/运行日期清单，必须严格递增、唯一并位于冻结范围内；实际checkpoint必须与它逐项相同。`dry_run`允许提供少量日期。`full`要求清单和实际输入同时包含2025-01-01与2026-07-01两个边界。回放器不会把所有自然日硬编码成交易日，也不会自行猜测周末、节假日、夜盘归属或补跑日。policy必须带非空版本；回放器没有隐含数值阈值，也不提供覆盖治理规则的`force`参数。

## 点时可见性

每个输入项必须显式给出：

```text
observed_at
available_at
evidence_times
evidence_status
```

时间必须是带明确已知offset的canonical RFC3339；表示未知本地偏移的`-00:00`被拒绝。任何`observed_at`、`available_at`、证据时间或结算`matured_at`晚于当前checkpoint的`as_of`，整次计划立即失败，不会被降级为普通排除。`available_at`也不能早于`observed_at`。`point_in_time`项至少需要一个`evidence_times`值，否则不能生成动作；`reconstructed`项可以没有证据时间，但仍必须转为`unscorable`。

无法证明点时可见的历史补录必须标记`evidence_status=reconstructed`。它会稳定转为`unscorable`并输出`reconstructed_evidence`原因，不产生正式预测、Experience结算或Shadow样本动作。

这个不可信证据判定优先于D14历史只读分类：即使输入种类是`historical_prediction`且期限为D14，只要证据是`reconstructed`，诊断也必须是`unscorable/reconstructed_evidence`，而不是`historical_read_only`，并且仍不产生动作。

## 输入隔离和稳定排除

每项都以`product`、`node_id`、`kind`和`item_id`形成全次计划身份。`product`只能是`poy`或`dty`，`node_id`必须来自Phase A冻结的15节点清单；同一item ID可在不同产品或节点中独立存在，不会共享状态或计数。相同隔离身份在同一或不同checkpoint重复时都fail closed，调用方必须为不同历史发生项提供不同item ID。

`scoreability`只能为`scorable`、`blocked`或`unscorable`。被排除项必须给出小写稳定reason code，原因会去重并稳定排序；它们不产生动作。可评分项不能携带排除原因。所有metric必须是有限的int或float，布尔值、NaN和无穷值均被拒绝；绝对值不得超过JSON安全整数上限`9007199254740991`，保证计划可由严格`json.dumps(..., allow_nan=False)`序列化。

为防止不受控适配器制造内存或输出放大，单次计划最多547个checkpoint（等于冻结范围的自然日总数），每个checkpoint最多512项，每项最多128个证据时间、64个metric、16个最终去重排除原因；`completed_horizons`最多3项。`reconstructed_evidence`等系统原因追加后也必须重新执行16项上限，已有同名原因会先去重，任何系统追加造成的第17个唯一原因都fail closed。`item_id`最多256字符；policy版本与Shadow policy版本最多128字符；reason code和metric key最多128字符。

这些局部上限不能相乘放大成无界整批，因此整次计划还同时冻结以下累计预算：最多2048项、8192个证据时间、4096个metric条目及4096个输出动作。累计预算跨全部checkpoint共享，不会在换日时重置。项数在进入逐项深度解析前累计；证据时间和metric在解析对应集合前累计；动作在汇入checkpoint输出前累计。任何累计值超过预算都会让整次计划fail closed，不返回部分结果。所有局部和累计上限在边界值可用，超过即拒绝；回放器不会静默截断。

输入边界使用plain dict与list/tuple的封闭字段集合，拒绝字符串伪装序列、未知字段、自定义mapping和歧义类型，防止适配器把未审计的时间或控制字段藏入payload。

## 正式期限和动作顺序

正式期限严格为D1、D7、D30。D14只能以`historical_prediction`读取并生成`d14_historical_read_only`诊断，不能生成新预测、结算或Shadow动作。其他期限一律拒绝。

可生成的动作只有：

```text
freeze_prediction
plan_experience_settlement
assemble_shadow_sample
```

每个产品和节点内部按上述顺序稳定排列。Experience迟到补跑只接受已完成阶段的合法前缀：空、D1、D1→D7或D1→D7→D30；再按D1→D7→D30补齐到显式`matured_horizon`，不能跳阶段或混入D14。已经到D30的项会生成一个显式`d30_recheck`动作，让后续已验收的settlement planner决定无变化或同阶段修订。Shadow动作必须引用显式`shadow_policy_version`，但本层不猜测或执行任何晋级阈值。

动作携带checkpoint、产品、节点、来源item、可见性与证据时间、有限metric、无歧义幂等键以及回放policy版本。它们只是后续适配器的输入计划，不代表已经写入预测账本、Experience revision或Shadow评估结果。

## 输出和后续接线边界

输出包含冻结历史范围、checkpoint级动作和诊断、总计以及按POY/DTY与节点隔离的计数。输入项顺序不影响输出；相同输入和policy始终得到相同结果。

## 持久候选只读加载器

`server/app/sequential_replay_candidate_loader.py`提供`sequential-replay-candidate-loader.v1`只读适配层。调用方必须显式提供canonical RFC3339 checkpoint `as_of`和每条selector；加载器不会选择revision、snapshot、产品、节点、期限、日历或replay policy。返回值中的`checkpoint`已经是`plan_sequential_replay(checkpoints=[...])`所需的精确形状，调用方不需要再翻译字段。

当前只支持两类持久记录：

- `formal_prediction_revision`：selector必须明确`revision_id`、`data_snapshot_id`、`product`、`node_id`和D1/D7/D30 `horizon`。加载器复用正式预测完整历史审计，按明确节点/期限取得唯一cell；终端成本压力节点还必须取得与产品一致的POY或DTY subtarget。非终端cell仍只映射到selector指定的单一产品，绝不复制成两个产品。
- `experience_revision`：除revision、snapshot、产品、节点和期限外，还必须明确其正式预测revision及`calendar_id/calendar_version`。加载器先审计checkpoint前可见的Experience不可变链，再重审其绑定的正式预测和snapshot。选中revision之前已经完成的期限作为`completed_horizons`，选中revision本身成为本次历史结算候选。

正式预测的`observed_at`、`available_at`、证据时间、scoreability和数值metric全部来自已审计batch/cell/subtarget字段；Experience对应字段只来自已审计card、持久时间和其绑定正式预测。所有时间必须不晚于checkpoint；跨offset按真实时间比较，不按字符串排序。无法证明严格历史可见性的Experience保持`reconstructed`并强制`unscorable`。缺失、篡改、未来可见、重复、歧义或selector不匹配会在返回任何checkpoint item前以稳定错误码fail closed，成功和失败路径都不写库。

其他持久种类只返回有界`unsupported_persisted_kind`诊断及selector指纹，不会伪造Shadow item。Shadow输入装配、D14历史标量适配、数值policy、生产调度和动作执行仍不在本加载器范围内。

## 检查点与暂停恢复

schema v32新增`sequential_replay_runs`与`sequential_replay_events`两张append-only表。初始化入口只接受原始checkpoints与显式policy，并在进程内调用纯计划器；调用方不能提交自签plan。run根绑定canonical plan Hash，事件逐项绑定checkpoint Hash、顺序、运行身份和时间。精确重复初始化及已提交checkpoint重试为零新增写入；不同plan、乱序、跳日、篡改、时间倒退或非法状态转换均以稳定错误码fail closed。最后一个checkpoint与`completed`事件在同一事务提交，失败整体回滚。

暂停、恢复与失败只追加事件，不更新或删除历史。每个run最多8192个事件，读取时以`LIMIT + 1`先执行有界门禁，再重算完整状态链。resume入口只使用`mode=ro/query_only`连接；缺库与旧schema都不会建库或迁移。该持久层不执行计划动作、不调用provider、不启动scheduler，也没有生产启用开关。

后续接线必须另行完成：

- 将`freeze_prediction`接入不可变预测revision；
- 将`plan_experience_settlement`逐项交给已验收的Experience settlement/storage适配层；
- 将`assemble_shadow_sample`装配为已验收Shadow生命周期输入，并提供正式版本化数值policy；
- 将已持久化的计划动作接入受控执行适配器，并以生产运行证明确认不会抢占08:20/09:30实时链路。

在这些适配与运行门禁完成前，本模块不能被描述为完整历史回放已经上线。

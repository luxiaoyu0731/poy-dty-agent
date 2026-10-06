# 正式序列 Eligibility 门禁

## 目的

`server/app/formal_series_eligibility.py` 是当前 Phase A v7 19 条正式证据序列的纯计算准入层。它不读取 Phase A JSON、数据库、文件、网络或系统时间；调用者必须显式提交待评估时点、正式期限、证据 bundle 和候选已批准证据 manifest。

“接口可连接”“数据库里已有行”“候选源有数值”都不是准入证据。只有固定的六项门禁全部通过，结果才是 `eligible`：

1. 来源已注册，授权和许可证有效；
2. 市场、具体品种/合约和报价口径已冻结；
3. 原始/标准单位、币种和确定性换算版本已冻结；
4. 新鲜度规则已冻结，且当前数据未超阈值；
5. 可见性规则已冻结，数据在评估时点前确实可见；
6. 交易日历 ID/版本和交易日归属有效。

## 冻结版本与范围

- policy：`formal-series-eligibility.v1`
- evidence bundle：`formal-series-evidence.v1`
- approved manifest：`formal-series-approved-evidence.v1`
- gate approval：`formal-series-gate-approval.v1`
- result schema：`formal-series-eligibility-result.v1`
- 正式期限：D1、D7、D30
- D14：仅历史读取，任何新增评估都返回 `legacy_horizon_read_only`
- 其他期限：`unsupported_horizon`
- 正式证据序列：严格等于 Phase A v7 的19条定义；CFETS USD/CNY 是第19条换算证据，必须通过完整六道门禁，但不生成独立汇率预测节点或预测单元；INE/SC仅保留在历史v1–v4合同
- 单批记录必须是内置 `list`，每条记录及全部嵌套证据只能使用内置 `dict`、`list` 和 JSON 标量；自定义 `Mapping`/`Sequence`、容器子类及其他可执行容器一律拒绝，且不会调用其 `len/items/keys/iter` 钩子
- 单批记录最多57条，即19条正式序列乘以D1/D7/D30；超限在记录规范化前以固定输入错误拒绝

## 证据结构

每条记录必须包含 `series_id`、`horizon_days` 和 `evidence_bundle`。bundle 必须精确包含版本、与记录相同的 `series_id` 和六个具名 gate。每个 gate 必须包含：

- `status`：仅 `passed/failed/conflict/unknown`
- 与 evaluator 相同的 `policy_version`
- 与记录及 bundle 完全相同的 `series_id`，防止授权或规格证据跨序列复用
- 非空且长度受限的不可变 `evidence_id`
- `valid_from`、`valid_through`：带时区 RFC3339 有效区间
- `applicable_horizons`：只能从 1、7、30 中无重复选择，并覆盖本次期限
- 该 gate 的完整固定布尔断言集合
- 非空 `numeric_checks`：授权、规格、换算和日历要求正整数证据数；新鲜度要求有限且满足 `0 <= age_seconds <= max_age_seconds`；可见性要求非负 `visible_age_seconds`

证据内容和 `evidence_id` 不进入输出。调用者不得把令牌、Cookie、账号或许可证正文放入输出层；即使恶意输入把凭据写入未知 series ID 或额外字段，结果也只返回固定占位和稳定 reason code。

## 已批准证据 Manifest

严格的 bundle 结构本身仍只是调用方声明，不能授权升级。每个 gate 还必须在 `formal-series-approved-evidence.v1` manifest 中有唯一 approval。approval 精确绑定：

- `series_id` 和 gate 名称；
- `evidence_id`；
- gate 完整事实的 canonical SHA-256；
- approval 版本、有效区间和适用期限。

evaluator 会对 manifest 做 canonical SHA-256，并且只接受命中模块/部署层冻结集合 `APPROVED_MANIFEST_DIGESTS` 的摘要。公开 evaluator 没有调用方可传的 trusted digest 参数，也没有“现场计算摘要并升级”的入口，所以同一业务请求不能给自己签发信任。新增受信摘要必须经过治理审批和代码/发布配置变更。测试可以 monkeypatch 该集合验证已审批路径可达，但这不改变生产信任根。

manifest 的 approval 总数最多114，即19条序列乘以6个gate。任何记录、manifest 或 gate 进入字段集合、键、值等业务遍历前，先以有界迭代检查确认它只含内置 JSON 类型，并满足最大深度 8、总节点 4,096、单字符串 512 字符、全部字符串合计 65,536 字符，以及任一集合最多 120 项。gate 的事实 Hash 使用相同资源边界。循环引用、自定义/非 JSON 容器、非 JSON 标量或任何超限输入都稳定 fail-closed，不进入业务遍历或递归 JSON encoder；encoder 后备捕获 `RecursionError`、`MemoryError` 等数据资源异常，但不吞掉 `KeyboardInterrupt` 或 `SystemExit`。记录内部的此类缺陷返回固定 `invalid_record_data`，manifest 内则返回固定 `approved_evidence_manifest_invalid`，均不拼接底层异常文本。

当前冻结集合为空，项目尚未批准任何真实 manifest，因此现实状态严格保持 `eligible=0/19`。缺少 manifest、摘要未获信、重复 approval、manifest/approval 字段或版本错误、事实 Hash 不一致、approval 过期及 gate/approval 期限集合不一致都会 blocked。

通过 manifest 校验时，结果顶层只记录安全的 `approved_manifest_digest` 作为审计身份；manifest 无效或缺失时该字段为 `null`。原始证据、证据 ID 和调用方提供的错误 digest 都不回显。

## Fail-closed 规则

缺失、额外字段、错误版本、未知/冲突状态、断言不完整、有效期不覆盖评估时点、期限不匹配和非有限/过大数值都会阻断。所有时间必须是带已知 UTC 偏移的 RFC3339；表示“本地偏移未知”的 `-00:00` 在 assessment、gate 和 approval 三处都拒绝。证据数最大为1,000,000；所有数值绝对值不得超过 `10^12`，这是解析稳定性边界，不是业务新鲜度阈值。相同 series/horizon 在同一批次重复出现时，两项都标记 `duplicate_series_horizon`，不按输入顺序择一。

外层容器或标量类型错误抛出 `FormalSeriesEligibilityInputError`，异常消息也是固定代码，不包含输入内容。除进程控制用的 `KeyboardInterrupt`/`SystemExit` 外，容器检查和数据遍历异常只能映射为上述固定输入错误或 blocked reason；未预期的遍历失败统一为 `input_container_traversal_failed`，不回显异常消息。极端超大期限在进入结果前以固定 `horizon_days_out_of_range` 拒绝，防止产生无法稳定 JSON 序列化的输出。语义不合格则返回 `blocked` 和固定 `blocked_reasons`。结果按冻结序列完整 tuple 顺序、期限和 reason 排序，不依赖调用者输入顺序。

## 非范围

本模块不负责获取证据、批准 manifest、验证外部网页、连接授权源、决定业务阈值、写数据库、升级 Phase A 静态 contract、发布 API 或调度任务。当前正式环境仍是 `eligible=0/19`；只有后续逐序列收集、独立验收，并通过受审代码/部署变更把 manifest 摘要加入冻结信任根后，单条结果才可能升级。

## 持久化基础（Schema 27）

`formal_eligibility_proofs.py` 只在当前运行版本的 `APPROVED_MANIFEST_DIGESTS` 授权且
57 项全部通过时，保存一份内容寻址 assessment。它在 `BEGIN IMMEDIATE` 内重读 live
snapshot，保存 canonical 57 输入、完整114项安全 approval projection、evaluator 结果、
三份19-series slice有效期以及只供历史审计的 captured trust root。typed child rows只是
canonical JSON 的受验证投影，不是第二份语义权威。

每次新 formal batch 还会再次使用当前模块 trust root 重演 evaluator；数据库里捕获的 root
不能授权新写。当前 production root 仍为空，因此 assessment 和 batch 均保持零正式写。
已经提交的历史记录可通过私有纯 replay core 使用 captured root 审计，但该入口不对普通
storage caller 或 HTTP 暴露。v4 历史 assessment 会按自身的20条序列、60项结果和120项
approval projection（含SC）重演；其历史 batch 保持45格审计语义，不能作为当前 v7 新写
入的资格证明或网格模板。

Schema 27 先把所有既有 scalar ledger 行（包括 D14）明确标记为
`legacy_scalar/legacy_unverified`，随后安装 INSERT blocker；迁移后任何 helper 或直接 SQL
新增 scalar prediction 都以 `formal_scalar_prediction_write_disabled` 零写拒绝。既有行仍可
读取、复盘和执行原有 review-status 操作，但不会进入 formal daily 或 Experience 消费链。

Snapshot中的每一条受治理事实都必须同时提供完整带偏移的事件时间与首次可见时间；缺失、
date-only、未知偏移、未来可见或时序反转一律稳定拒绝，不推断午夜。持久canonical列、审批、
结果和snapshot JSON在解析前先检查UTF-8字节上限；调用方records/manifest先通过bounded
plain-JSON evaluator检查，才允许canonical编码。

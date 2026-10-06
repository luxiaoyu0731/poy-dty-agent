# Phase A 详细数据契约

> 历史合同声明（2026-08-31）：本文件仅定义保留的 Phase A v1-v7 审计/回放语义。
> 当前产品合同已由 `docs/project-correction-100.md` 与 ADR-0001 改为七品种 × D1/D7/D30（21 格）；
> 下文“当前正式”均按其历史版本原义阅读，不具当前七品种正式资格。

版本：`phase-a.v7`（冻结 v1 基础契约、v2 CFETS 覆盖层、v3 中国期货连续规则、v4 公开个人复用/业务口径覆盖层；v5 将已停止的 INE/SC 线移出当前正式合同；v6 登记 CCTD 环渤海 5500K 日度候选；v7 登记 SunSirs 华东混二甲苯日度评估候选；两者仍保持证据门禁）

政策基线：2026-08-02 已接受的 Phase A 治理决定

机器可校验来源：`server/contracts/phase_a_contracts.v1.json`

## 1. 适用范围

本契约冻结 14 个当前正式治理节点及 D+1、D+7、D+30 三个正式写入期限。INE/SC 仅保留在 v1–v4 历史合同中，不属于当前资格矩阵或预测网格。正式输出只描述各节点及 POY/DTY 上游成本压力，不输出采购、报价、库存或交易执行指令。

D+14 仅允许读取既有历史记录。兼容响应必须标记 `legacy_horizon=true` 和 `write_allowed=false`；任何新写入均应失败。D+3、D+5、D+10、D+20 不属于正式预测面。

## 2. 序列与观测信封

每条序列定义必须声明唯一 `series_id`、节点、市场、规格、报价类型、角色、权威及备用来源、原始和标准单位/币种、确定性换算公式及版本、时区、日历、价格时间含义、新鲜度和资格状态。

每条实际观测必须另外保留原值和标准值、来源 URL、抓取/发布/首次可见时间、交易所交易日、原始内容哈希、HTTP/超时/重试结果、解析器版本、授权/许可证状态、换算证据以及不可变 revision 链。更正只能创建新 revision，不能覆盖历史观测。

价格口径必须隔离：

- 期货结算价与授权现货评估价使用不同 `series_id`。
- 备用来源必须有逐来源、可引用的同市场、同规格、同报价类型证据；现货评估不能作为期货结算价的备用。
- 公开评估价不能表述为真实成交价。
- `akshare_prototype` 和 `yahoo_futures_daily_proxy` 不得成为正式主源或备用源。
- 来源、许可证、单位、时间或新鲜度门禁失败时，观测不可评分，禁止模拟补齐。

候选来源只表示 registry 的产品范围可能匹配，不表示它已成为权威来源、备用来源、当前已授权或运行时可用。

## 3. 当前资格状态

当前没有任何正式价格或正式目标序列被声明为运行时 eligible。只有 CFETS USD/CNY 中间价辅助换算序列具备完整静态定义；运行时仍须逐观测通过授权、时间、新鲜度和质量门禁。

以下状态必须保持阻断，直至另行冻结详细定义：

| 节点 | 状态 | 原因 |
|---|---|---|
| Brent | `blocked_evidence_capture_pending` | 公开个人复用来源与前月连续规则已冻结；捕获、可见性和日历证据待完成 |
| WTI | `blocked_evidence_capture_pending` | 公开个人复用来源与前月连续规则已冻结；捕获、可见性和日历证据待完成 |
| 煤炭 | `blocked_evidence_capture_pending` | 已登记 CCTD 环渤海 5500K 日度参考候选；当前仍须完成来源、规格、可见性、日历等证据门禁才可作为正式日度价格。原 CCTD 周度来源继续仅作背景证据。 |
| 石脑油 | `blocked_evidence_capture_pending` | CCF 授权、人民币单位和日度评估规格已冻结；捕获、可见性、新鲜度和日历证据待完成 |
| MX | `blocked_evidence_capture_pending` | 华东混合二甲苯公开现货评估口径已冻结；捕获、可见性和日历证据待完成 |
| PX、甲醇、PTA、MEG 期货 | `blocked_evidence_capture_pending` | 官方日度源与主力连续规则已冻结；当前捕获、可见性和日历证据待完成 |
| PX、PTA、MEG 现货 | `blocked_evidence_capture_pending` | CCF 授权、人民币单位和日度评估规格已冻结；捕获、可见性、新鲜度和日历证据待完成 |
| 乙烯、EO | `blocked_evidence_capture_pending` | 华东聚合级乙烯、华东工业级 EO 公开现货评估口径已冻结；捕获、可见性和日历证据待完成 |
| 聚酯熔体 | `blocked_evidence_capture_pending` | 华东纤维级聚酯熔体公开评估口径已冻结；捕获、可见性和日历证据待完成 |
| 聚酯切片 | `blocked_evidence_capture_pending` | 华东纤维级 PET 切片公开评估口径已冻结；捕获、可见性和日历证据待完成 |
| POY/DTY 上游成本压力 | `blocked_evidence_capture_pending` | PTA 0.855、MEG 0.335 的 20 个共同交易日压力指数已实现；PTA/MEG 输入的授权、捕获、可见性和日历证据待完成 |

GACC 煤炭贸易流不是煤价；EIA 泛原油产品映射不是 ICE/CME 结算序列；ZCE `pet` 产品标记不足以证明其等同于治理中的聚酯切片。这些映射均不能解除阻断。

## 4. 版本化 Schema

### Event `phase-a.event.v1`

事件以稳定事件簇为单位，保存证据成员、首次可见时间、不可变 revision、生命周期、节点影响、生效/峰值/半衰期、各期限有效概率、确认/加强/结束/反转条件、人工复核门禁、完整度和可评分性。

### Fact Summary `phase-a.fact-summary.v1`

事实摘要绑定事件簇和输入证据快照。每条事实必须有独立证据 ID；冲突和未知项不能丢失。Schema 禁止方向、方向概率、走势、价格展望和建议字段。模型、Prompt、预算、超时、重试、降级及人工复核状态必须可审计。

### Prediction `phase-a.prediction.v1`

每个当前正式批次必须完整包含 `14 节点 × 3 期限 = 42` 个单元。数据不足的单元仍须存在并标记 `unscorable`，不得删除或模拟补齐。历史 v4 批次保持原有 45 格审计语义。每个单元至少保存方向、方向概率、幅度、置信度、仍有效概率、主要驱动事件、反向事件、完整度和缺失序列。

末端治理节点的每个期限单元必须在 `subtarget_results` 中恰好保留一条 POY 和一条 DTY 结果；其他节点的该字段必须为空。这不会把预测网格扩展成未经治理批准的新节点。

Schema 27 的内部存储入口把整份 canonical payload 作为唯一语义权威：一个 revision 必须在
单一 `BEGIN IMMEDIATE` 中写入 42 个 cell、6 个末端 subtarget 和 D1/D7/D30 三份 eligibility
proof。`revision_id/previous_revision_id` 是唯一 revision 链；首版 predecessor 为空，后续版
必须指向同一 batch 当前 head，禁止 fork、跳跃或跨 batch 引用。完全相同的 replay 返回首次
提交记录并保留 server-owned `persisted_at`。

同batch predecessor、batch assessment/snapshot、proof revision/assessment及subtarget
revision/parent均由复合UNIQUE/FK在数据库层交叉绑定，并由bounded post-read逐列复核。
三个slice Hash严格按冻结的`FORMAL_SERIES_IDS`顺序构造，不采用数据库字典序。返回对象和全部
可失败审计都在commit前完成；commit成功后不再SELECT、解析或Hash。

写入事务会重读 snapshot seal/as-of、assessment、当前 trust root 和三个 19-series slice。CFETS USD/CNY 是换算证据，不改变 14 节点 × 3 期限的 42 格预测网格。

### 中国期货主力连续规则 `china-futures-main-continuous.v1`

本规则定义当前 ZCE PX/MA/TA 与 DCE EG 四条期货结算序列的连续口径，不适用于 CCF 现货评估、CFETS 或预测目标。候选池仅包括交易所当天有效挂牌合约；按持仓量降序、成交量降序、到期日升序排序。候选合约连续两个交易日同时在持仓量和成交量上超过当前主力时，于下一交易日切换。当前主力进入最后交易月时，于下一交易日强制切换到排序最高且不在最后交易月的合约。只使用交易所官方日结算价；禁止回溯拼接、复权和模拟补值。该规则只补齐连续口径，不替代来源授权、可见性、交易日历或 manifest 审批。2026-08-06 起，INE/SC 按用户决定仅保留历史合同，不属于当前正式来源闭环。

### 公开个人复用与 POY/DTY 成本压力规则

项目允许第一方公开数据用于个人内部分析及不可逆的公网派生结论；必须保留来源归属、精确 URL、原始哈希、首次可见时间和不可变修订，且不得再分发原始数据。公开现货评估始终标记为评估，不得冒充成交价。POY 与 DTY 上游成本压力共用 `poy-dty-upstream-cost-pressure.v1`：当前物料篮子为 `0.855 × 内盘 PTA + 0.335 × 内盘 MEG`，以两项输入最近 20 个共同交易日的算术均值为基准，输出 `100 × 当前篮子 / 基准均值`。任一必需共同日缺失即不可评分；加工能耗、利润、库存、需求和成交价不得静默混入。
payload 的 `data_frozen_at` 必须等于 snapshot `created_at`，assessment/payload as-of 必须等于
snapshot metadata cutoff；server `persisted_at` 和派生 `authorization_at` 不能由调用方覆盖。
未来 publication、过期 slice、snapshot drift、缺 cell/subtarget 或任一 cross-binding 冲突都会
整批零写。当前没有 HTTP batch endpoint；本轮只提供内部基础。

### Experience Card `phase-a.experience-card.v1`

Experience Card 保存原始及相对价格变化、MFE、MAE、到峰值有效观测日数、首次反转、完整度、可评分性、重叠事件、机制支持情况及可复用经验。成熟度只能按 `d1_preliminary → d7_intermediate → d30_mature` 前进。

无法证明当时可见的历史数据必须标记 `reconstructed`，且不能计入严格回测；严格回测只接受 `strict_as_of`。

## 5. 兼容与变更规则

- `v1` 写入者只能写当前明确版本；未知 major 版本失败闭合。
- 旧版本仅能通过显式注册的只读适配器读取。
- v4 的20条序列/60项资格证明及45格 prediction revision（含SC）只能按其自身冻结的合同、captured trust root 和序列顺序复核；它们绝不扩展当前 v6 的19条资格矩阵、42格网格或新写入范围。
- 新增字段或语义变化不得静默改写已冻结记录。
- 正式节点、期限、来源层级、单位、日历或公式变化必须先形成新的用户决定，再发布新契约版本。
- 本包只读引用 `server/source_registry.json`，不改变任何来源授权或运行状态。

## 6. 验收边界

静态验收覆盖节点和期限精确集合、序列 ID 唯一性、候选/主源/备用来源的 registry 产品覆盖、代理禁用、blocked 不得携带主源或备用、备用来源同报价基础证据、报价口径隔离、单位政策、四类 Schema 必需字段及嵌套枚举、42 单元网格、末端 POY/DTY 子结果、D+14 只读策略、重建数据不可评分和 Experience Card 成熟度单向推进。

本契约已具备 Schema 27 内部数据库基础和严格 domain 写入入口。CFETS 与 CCTD 环渤海 5500K
候选已接入内部受控适配器；CCTD 仍不调度，且两者都不替代逐序列证据复核。生产 trust root
为空且真实 19-series evidence 尚未批准，因此现实状态仍为`eligible=0/19`，不能产生正式 batch。

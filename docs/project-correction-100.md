# POY/DTY 上游工业情报与原料预测 Agent：100 分修正总纲

版本：v1.28
生效日期：2026-08-31；证据更新至 2026-09-06；产品价值重排于 2026-09-05 生效
状态：双轨实施中——预测轨生产可用但终局未完成；工业情报轨已部署生产、等待首份自然调度日报与价值验证
历史审计基线：44.9/100；预测轨当前严格评分：79.8/100；预测终局硬门禁仍为 0/21；全项目判定 `INCOMPLETE`

## 0. 当前权威执行快照与 `/goal`（2026-09-06）

本节是当前执行入口；第 12 节按时间保留历史事实，若与本节当前态冲突，以本节和时间更晚的小节为准。

- 当前 `/goal`：依据本总纲沿两条互不代替的轨道持续完成项目。**工业情报轨**新增第八模块，把全球公开信息收集、事件聚类、证据链、产业传导、每日摘要、雷达、地图、来源运行和操作者反馈做成生产能力；**预测轨**保持七产品 × D1/D7/D30 合同不变，继续积累逐格真实 OOS 并按冻结门禁晋级。不得用情报上线提高预测分数，也不得用预测代码就绪代替情报价值证明。
- 当前完成度：预测轨严格 **79.8/100**，正式 OOS **0/21**；工业情报轨为 `Accepted Design / Production Deployed / First Scheduled Brief Pending / Value Validation Pending`。新增范围后不伪造一个加权“综合当前分”；全项目 100 分采用合取条件：预测轨自身达到 100 分，且情报轨完成生产技术交付并通过独立 20 个预注册业务日价值验证。
- 当前生产基线：提交 `aa253f99f1838cdbeaae350c3bdc0b835aab0e43` 已作为不可变 release `20260905T162117Z-e2f2f681480b032b` 部署，回滚目标为 `20260904T085912Z-ab3a9e3b4bd55b4a`；生产数据库为 schema v37、`integrity_check=ok`。DCE 与 CCF 保持软移除，SunSirs 是当前 MEG 标签，不再存在 DCE/EIA 凭据人工补齐目标（EIA 轮换要求已由操作者决定移除，见 12.37）。
- 当前分层状态：预测 release 与鉴权公网运行 **GO**，预测终局仍为 **NO-GO / INCOMPLETE**（79.8/100、正式 OOS 0/21）；工业情报中心已正式部署、功能开关已启用、本地 deep health 为 healthy、鉴权公网 **29/29** smoke 通过。按冻结合同，D7 仍需首份工作日 09:30 前释放的非 `blocked` 冻结日报后才能改称 `Production Implemented`；价值状态必须在随后独立验证。
- 当前已记录证据：12.39 的预测发布证据与 12.40 的工业情报 D1–D6 本地证据继续有效；12.41 记录 v37 备份/迁移/回滚演练、最终不可变 release、本地健康、五个情报 API、指标、公网 smoke 与调度恢复。两轨证据不得互相替代。
- 下一阶段：在 2026-09-07 首个工作日窗口核验以 08:20 为 cutoff、在 09:30 前释放的非 `blocked` 日报，然后启动独立 20 业务日价值实验。预测轨继续自然积累真实 OOS，并只在逐格证据通过时走治理晋级。
- 明确非目标：不实现“下一结果何时成熟、为何未结算、预计何时可评估”；不重开同一历史快照上的盲目调参；不恢复 CCF/DCE 当前采集；不降低预测门槛或把模拟结果计为 formal；不发送工业情报即时业务提醒；不让情报输出成为采购、销售、套保或交易执行指令。

工业情报轨的产品范围、实施合同和架构决策分别以 [`POY_DTY_上游原料智能Agent项目文档.md`](../POY_DTY_上游原料智能Agent项目文档.md)、[`docs/industrial-intelligence-center.md`](industrial-intelligence-center.md) 和 [ADR-0005](adr/0005-industrial-intelligence-center.md) 为准；本文件继续冻结预测轨评分与全项目完成关系。

## 1. 文档权威与修正原则

本文件是当前“完全完成”定义和实施验收的最高优先级项目修正文档。它把“完全完成”改为工业情报价值与预测科学两条独立轨道的合取条件，并取代其他文档中与下列结论冲突的当前态描述，但不删除历史事实：

- 单一“POY/DTY 上游成本压力”正式目标；
- 14 节点/42 格作为当前用户预测面；
- 19 条旧正式证据序列作为当前完成线；
- 公开数据仍需逐源 license/manifest 审批；
- CCF 可以继续捕获、导入、调度或参与当前正式资格；
- legacy scalar、旧批次元数据或启发式信号可以被展示成当前正式预测。
- 预测是产品唯一主价值，或工业情报上线可以自动提高预测评分。

本项目是单操作者、鉴权公网可用、服务于 POY/DTY 生产经营与采购研判的个人工作台；这种使用可能构成商业使用，不能因“个人工作台”而自动视为非商用。公开可见数据不再经过内部 license/manifest 审批仪式，但公开可见不等于可以任意复制、长期保存、再分发或绕过来源条款。系统必须遵守来源适用条款，不得绕过登录、付费墙、CAPTCHA、访问控制或明确技术封锁；权利未知时只保存元数据和证据引用。CCF 与 DCE 软移除：历史记录只读保留，不得恢复未来抓取、人工捕获、导入、调度、告警、情报 readiness 或当前正式预测资格。

“100 分”是一个由当前 E1/E2 证据证明的运行状态，不是文档声明。任何未验证项不得靠计划、代码存在或历史测试补分。

## 2. 产品完成线

全项目有两条同等真实、互不补分的完成轨道：工业情报轨证明“信息收集整合是否稳定且真正节省研判时间”，预测轨证明“数值结果是否在真实样本外优于朴素基线”。只有两条轨道各自达到本文件和对应规格的终局条件，才能宣称全项目 100 分。

预测轨的七个同等级正式目标为：原油、石脑油、PX、PTA、MEG、POY、DTY。每个目标每日产生 D1、D7、D30 三个单元，共 21 格。每格对用户展示：上涨/中性/下跌、置信度、数据状态、关键驱动和证据链；展开后展示连续点预测、价格区间、历史命中和误差。

系统不预测或优化用户真实库存、采购量、生产计划、采购价、销售价、加工利润和经营利润，也不输出买卖数量、采购、销售、套保或交易执行指令。

系统始终尽量提供参考结果，但必须严格区分：

1. `formal`：当前 21 格合同及该格所有质量/评估门禁通过；
2. `low_confidence`：模型可运行但置信度或稳定性不足；
3. `reference`：朴素基线或尚未晋级的候选估计；
4. `degraded`：部分特征、事件解释或来源失败；
5. `insufficient_data`：没有足够点时数据；
6. `model_unavailable`：数值模型不可用，禁止生成伪结果；
7. `historical_legacy_contract`：旧标量/旧 Phase A 记录，只作审计。

## 3. 不可妥协的预测合同

详细决策见 `docs/adr/0001-seven-product-formal-forecast-contract.md`。以下条件逐格适用：

- 固定、可追溯、版本化的真实标签；
- 逐行保存事件/观测时间、系统首次可见时间、来源、URL、原始证据或内容哈希、修订身份；
- 预测时只能读取 `visible_at <= as_of_time` 的版本；
- 品种级中性带按训练/验证期波动率冻结，不能看测试结果后调整；
- 模型内部保留点预测/变化率和区间，方向只是其派生显示；
- 模型、特征、标签、数据快照、评估配置、冠军/候选关系和回滚版本可追溯；
- LLM 只解释，不生成或替代数值预测。

## 4. 21 格硬门禁

每格必须在未参与训练和模型选择的时间样本外数据上同时满足：

1. expanding/rolling-origin 或冻结最终测试窗，无随机时间切分；
2. 至少比较价格不变和合理季节性两个基线；
3. 主要价格误差比最佳朴素基线改善至少 5%；
4. 三分类方向准确率至少 55%；
5. 报告有效且去重后的样本量；
6. 报告置信区间或等价不确定性；
7. 报告最差市场阶段、极端行情和结构变化表现；
8. point-in-time/修订/特征/阈值无可发现泄漏；
9. 可由模型、数据快照和评估配置哈希重跑复现。

任一格不通过，项目不能判定 `COMPLETE`。可以显示该格的参考结果，但正式状态必须降级。

## 5. 七项无终端用户闭环

用户必须只在界面中完成：

1. 查看七品种 D1/D7/D30 方向；
2. 查看置信度、关键驱动和数据状态；
3. 从每格追溯原始数据与事件证据；
4. 查看 freshness、缺口、降级和非正式原因；
5. 查看调度、备份、模型和系统健康；
6. 查看历史预测、实际结果、命中率和误差；
7. 导出与当前 API/界面一致的预测报告。

后端记录存在但 UI 不可完成，不算闭环。

## 6. 数据完成线

每个正式特征和标签至少有一个自动、可追溯、语义/单位明确、符合真实 cadence/freshness、失败隔离且幂等的来源。月度数据不得伪装成日度价格，代理变量不得冒充精确目标，C/D 级发现材料不得单独支撑高置信正式结论。

当前标签候选和限制以 ADR-0001 为准。标签注册表晋级前要完成：历史覆盖、重复/修订规则、时区与公布时点、主力换月、缺失值、代理标识和消融验证。历史 CCF 不能填补当前正式标签缺口。

## 7. 模型治理完成线

- 候选可训练，但不得自动替换冠军；
- 晋级依据是冻结的逐格评估，不是总体均分；
- 保存冠军、候选、父版本、训练窗、选择窗、最终测试窗和回滚目标；
- 预测记录绑定 model/data/features/label/evaluation/config；
- 监控输入漂移、标签失效、预测退化和最佳朴素基线反超；
- 触发后自动告警或将相关格降级；
- 不要求自动修复、自动训练后直接上线。

## 8. 生产与故障完成线

目标是单机生产、鉴权公网、日常零人工，异常时由单一操作者按 runbook 恢复；RPO ≤ 7 天，RTO ≤ 1 天。当前 release 必须有不可变身份和已验证回滚目标，具备备份完整性/恢复演练、磁盘预检、日志轮转、健康/调度/告警、重启自愈、并发锁、调度幂等、状态损坏保护、外部超时和限流处理。

发布前 15 项故障矩阵必须全部有本轮可重复 E2 证据：进程重启、机器/服务重启恢复、断网、HTTP 403、HTTP 429、超时、上游空数据、部分月份失败、状态损坏、重复调度、数据库备份、备份恢复、磁盘不足、LLM 不可用、数值模型不可用。通过后，当前 release 首次真实生产成功即可交付；不人为等待 30/90 天。

## 9. 预测轨从 44.9 到 100 的缺口恢复表

| 顺序 | 缺口 | 严重度 | 主计分域 | 可恢复分 | 完成证据 |
| ---: | --- | --- | --- | ---: | --- |
| 1 | legacy/旧批次可能被误标为当前正式 | P0 | C | 2.0 | API/UI 行为测试证明只显示历史身份，当前正式为空 |
| 2 | 七品种数值预测与21格当前合同缺失 | P1 | A | 9.0 | 每日 21 格结果、模型/标签/快照绑定及失败降级测试 |
| 3 | 21格逐格 OOS 评估和硬阈值缺失 | P1 | A | 18.0 | 可重跑 21 行矩阵，21/21 通过且哈希稳定 |
| 4 | 固定标签、中性带及 CCF 当前资格冲突 | P1 | B | 6.0 | 标签注册表、波动率阈值冻结、CCF 排除测试 |
| 5 | `visible_at`/revision/raw lineage 不统一 | P1 | B | 2.5 | 点时数据契约、迁移/隔离与泄漏反例测试 |
| 6 | 七品种 UI 闭环和导出缺失 | P1 | C | 10.0 | 浏览器/E2E 覆盖七项闭环与21格一致性 |
| 7 | 当前 release 日跑、故障、日志轮转未闭合 | P1 | D | 4.5 | 15/15 故障矩阵 + 当前 release 首次真实成功 |
| 8 | 新闻重定向未逐跳校验 allowlist | P1 | E | 1.0 | 每跳校验及跨域/循环/缺 Location 测试 |
| 9 | E2E 共享状态导致顺序失败 | P1 | F | 0.7 | 随机/重复顺序全绿，无共享业务状态依赖 |
| 10 | API/架构/状态文档仍声明旧合同 | P1 | F | 1.0 | 文档/OpenAPI/运行响应一致性测试 |
| 11 | FastAPI/TestClient 弃用与预测模块边界 | P2 | F | 0.4 | 零相关 warning、模块依赖检查和全量回归 |

总可恢复分 55.1。缺口关闭必须用当前 E1/E2 证据，不因“代码已写”自动回分。

### 9.1 已回分台账（v1.25 起强制维护）

历史轮次的逐轮回分未留痕，79.8 无法逐轮复算；自本版起新回分必须记入本表。当前精确剩余
20.2 分：18.0 分为缺口 3 的真实 OOS，约 2.2 分分散于缺口 2/4/5 的“各标签持续 point-in-time
历史与逐标签资格证据”，其缺口内拆分待真实证据形成时精确落账。

| 缺口 | 可恢复分 | 当前剩余 | 状态依据 |
| ---: | ---: | ---: | --- |
| 1 旧合同误标 | 2.0 | 0 | R0 闭合；§12 行 1 |
| 2 七品种 21 格 | 9.0 | 并入上述 2.2 余量 | 参考闭环已实现并发布（12.15/12.18） |
| 3 逐格 OOS | 18.0 | **18.0** | 真实 OOS 0/21（12.38 只读复核） |
| 4 标签/中性带/来源 | 6.0 | 并入上述 2.2 余量 | 冻结合同闭合；SunSirs 已晋级（12.18） |
| 5 点时与修订 | 2.5 | 并入上述 2.2 余量 | 合同与泄漏门禁闭合（12.16/12.31/12.32） |
| 6 UI 与导出 | 10.0 | 0 | 12.6 闭合 |
| 7 生产与故障 | 4.5 | 0 | 12.10/12.27 闭合 |
| 8 重定向安全 | 1.0 | 0 | 闭合 |
| 9 E2E 隔离 | 0.7 | 0 | 12.6 闭合 |
| 10 文档/API | 1.0 | 0 | 闭合 |
| 11 弃用与边界 | 0.4 | 0 | 12.13 回分（79.4→79.8） |

## 10. 预测轨实施顺序与停止条件

本节 R0–R6 只约束预测轨；工业情报轨按 `docs/industrial-intelligence-center.md` 的 D0–D8 独立实施。两者共享发布安全边界，但不共享完成分数。

### 阶段 R0：真实性边界

修正 legacy/formal 分类、旧快照文案、当前正式读模型和 CCF 当前资格。验收：所有旧记录都不能进入当前正式列表；无 21 格合格批次时 UI 明确显示“正式 0/21”。

### 阶段 R1：数据与标签

建立七标签注册表、点时观测适配器、统一 lineage、主力连续化和品种中性带。验收：标签合同测试、泄漏反例、断点/幂等和每日自动更新通过。

### 阶段 R2：数值预测与治理

先用现有 NumPy 实现可解释基线/候选和模型注册表，不新增依赖；只有在真实 OOS 证据表明必要且经用户确认后才加入 StatsForecast。验收：21 格每日输出、故障降级、冠军晋级/回滚和追溯通过。

### 阶段 R3：滚动样本外评估

固化 split、基线、误差、方向、置信区间、市场阶段和泄漏审计，生成内容寻址证据包。验收：21/21 分别过 5%/55% 门槛；未过时继续迭代数据/特征/模型，不降低阈值。

### 阶段 R4：产品闭环

实现七品种×三周期网格、证据抽屉、数据状态、系统健康、历史实际/误差和导出。验收：七项 E2E 全部通过，API/UI/导出同源。

### 阶段 R5：运行与安全加固

逐跳重定向校验、15 项故障矩阵、E2E 隔离、日志轮转/磁盘/告警和文档同步。验收：全量后端、前端、E2E、API、静态和供应链门禁全绿。

### 阶段 R6：发布与终审

先做只读发布就绪评审。只有用户明确授权后才 commit/push/部署或写生产迁移。发布后取得当前 release 首次真实日跑成功，再执行独立只读终审。预测轨终局标准：100.0/100、21/21、七项闭环 7/7、故障 15/15、P0/P1=0。全项目终局还必须满足工业情报轨 `Production Implemented + Value Validated`，不得把其中任一轨道的通过证据替代另一轨道。

## 11. 证据规则

- E1：本轮生产运行、数据库、API、公网或恢复结果；
- E2：本轮重跑测试、构建、静态检查或故障模拟；
- E3：当前代码、schema、配置；
- E4：文档、计划、旧报告。

预测性能只接受可重跑的当前 E2 产物；生产交付只接受 E1。大型证据只记录绝对路径、SHA-256、大小和摘要，不纳入 Git。每次实施都遵循“生成→评估→批评→修正”，失败即继续迭代或诚实降级。

## 12. 2026-09-01 当前实施快照

本节只记录本轮观察到的 E1/E2；不把“代码存在”换算成终局分数。当前终局仍为
`INCOMPLETE`，真实样本外门禁仍为 **0/21**，因此不得声明已达到 100 分。

| 缺口 | 当前状态 | 本轮证据 | 仍需完成 |
| --- | --- | --- | --- |
| 1 旧合同误标 | 代码门禁已闭合 | legacy scalar/Phase A 只能作为历史合同；API/UI 行为与全量后端回归通过 | 最终只读复核 |
| 2 七品种 21 格 | 参考闭环已实现 | DCE 软移除后的当前只读预览严格为 21 格：`formal=0`、`reference=15`、`unavailable=6`；其中 MEG 三格明确为来源缺口，缺数据不补造数值 | 21 格真实冠军与每日正式批次 |
| 3 逐格 OOS | 评估与自动证据链已实现，性能未通过 | 两基线、最终 50% 时间窗、区间、最差阶段、哈希均有；独立只读运行器已接入每日生产调度；真实结果 0/21 | 形成真实点时样本并迭代至 21/21，阈值不降低 |
| 4 标签/中性带/来源缺口 | 代码合同已闭合，标签资格未完成 | 七标签注册表与冻结中性带；CCF 与 DCE 被当前运行合同软移除；CZCE PTA/PX 主力标签和甲醇特征源已接入；MEG 三格保留并明确为来源缺口 | 为 MEG 选择并验收新的正式来源、石脑油等逐标签资格证据 |
| 5 点时与修订 | 部分闭合 | OOS gate v2 拒绝“同一时刻回填整段历史”冒充当时可见；EIA Brent、CZCE、DCE EG、石脑油、TNC POY/DTY 候选均按 append-only capture revision 合同实现；生产主库已迁移 v34，稳定自然键重复组为 0 | 七标签形成足够长的逐日 point-in-time 历史 |
| 6 UI 与导出 | E1/E2 闭合 | 活跃工作台展示七项闭环；完整 E2E 连续两轮 154/154 通过，视觉桌面/移动均 PASS；当前 release 鉴权公网 smoke 19/19 | 最终 21/21 后复核正式态展示 |
| 7 生产故障 | E2 15/15；部署 E1 完成但带警告 | 冻结故障矩阵 15/15；生产权限/v34 迁移完成；当前不可变 release 的冷启动与常态公网 smoke 均为 19/19，真实日跑 `ready_with_warnings` | 保留 MEG 显式来源缺口，关闭其余来源/效果警告并取得正式 GO |
| 8 重定向安全 | E1/E2 闭合 | 每跳 allowlist、循环、跨域和缺 Location 测试；安全扫描全绿；公网匿名/鉴权边界 smoke 通过 | 无当前代码缺口 |
| 9 E2E 顺序隔离 | E2 闭合 | 完整 E2E 重复两轮 154/154；事件搜索与行情刷新竞态已用响应/忙状态同步修正 | 发布后公网 E1 smoke |
| 10 文档/API | E2 闭合 | API、架构、状态、OpenAPI 双向一致性测试与全量回归通过 | 最终只读复核 |
| 11 弃用与边界 | 已闭合 | FastAPI 已迁移 lifespan；预测模块边界检查通过；应用、脚本和测试中的 SQLite 上下文已显式关闭，初始化异常路径 fail-closed；Starlette TestClient 已使用测试组中的 `httpx2`，不再走弃用的 `httpx` 回退；当前精确工作树全量回归 1762/1762 | 无当前代码缺口；生产运行时仍保留既有 `httpx`，未扩大运行时变更面 |

### 12.1 当前数据/性能阻断

- 原油有 1,351 个旧投影历史点（2021-03-23 至 2026-07-27），但只有 6 个唯一首次可见时刻，且现存批量导入记录不能证明每个历史点当时已可见；OOS v2 对此 fail-closed。EIA v2 只返回行的 `period/value`，没有行级发布时间；代码现以首次成功抓取作为保守 `published_at/visible_at`，只对 RBRTE Brent 行保存规范化行哈希与 append-only revision，预测不再把 legacy-only 投影视为标签匹配。日常来源自动化和鉴权 `/price-comparison/fetch` 两条 EIA 写入路径均已收口：投影与 revision 同事务提交，精确重放幂等，非法日期仍隔离，接口响应契约不变。2026-09-01 官方 API 受限验收取得最新 5 行/5 revisions，隔离库首次写入 5/5、精确重放 0 新增/5 unchanged，crude 从 revision 读取且来源匹配；未写生产。
- 石脑油公开估值页的前向自动路径已验证：2026-08-31 两次隔离采集均取得 2026-08-28 的 745.84 USD/mt，第一次写入 append-only capture revision，第二次精确重抓幂等为 0 个新 revision；投影与 revision 在同一事务写入。解析器优先使用带日期的“上涨/下跌至”摘要，避免滞后统计卡值抢占当日值；定向回归 31/31。该页历史下载/API 明确属于 premium，项目未访问；因此只能从现在起积累 point-in-time 历史，不能用页面宣称的 2005 年起历史补造 OOS。100ppi 公开归档在普通 HTTP 客户端下仍返回“网站防火墙/请求参数不合法”，不得绕过。
- PX/PTA 已接入 `czce_pta_px` 官方日线适配器，甲醇同时作为上游特征源接入；2026-08-31 官网文件只读实测为 37,607 bytes、SHA-256 `75cc18e546ea324d310c85e3bae2c3d962796dec3e0b0a0b8755fd420565b2f2`，解析 36 条有效合约，主力为 MA610、PX611 与 TA701。隔离 dry-run 的 10 日回看取得 213 条有效合约。该结果证明新数据链可用，不倒推历史可见性，也未写生产库。
- MEG 已实现 DCE 官方 EG 日线凭据适配器：只允许固定 `https://www.dce.com.cn`、禁止重定向，严格校验 EG 合约与价格/成交/持仓，按冻结规则选择主力，并原子保存全合约投影与每日主力 append-only revision；鉴权失败、短暂 API 错误和 token 刷新不会回显 secret/token。没有 `DCE_API_KEY` 与 `DCE_SECRET` 时 0ms 返回 `requires_api_key`，不发送 DCE 请求；隔离 scheduler dry-run 报告 SHA-256 `71d77a64e9df0d08ad4211665ab0364763a8156c654b5f8d510c70bce3e8f3be`，`writes_database=false`。GitHub 复用评估拒绝引入 Beta、单维护者且默认 HTTP 传凭据的 `dceapi` 依赖，改用现有 `httpx` 最小实现。当前本机没有 DCE 凭据，官方 HTTPS API 路径仍超时，故代码可调度不等于 live-ready；不得回退到明文 HTTP、WAF 绕过或非官方分钟代理冒充正式标签。生产库 DCE 正式行仍为 0。
- POY/DTY 的 TNC 公开历史已进入日常结构化抓取，并闭合为“投影 + append-only revision”原子写入；预测只认可 revision，只有旧投影时明确降级。2026-09-01 公网只读验收限制为每品种 2 页，取得 2026-08-03 至 2026-08-31 共 40 条观测和 40 个唯一 revision；隔离库首次写入 40/40，精确重放 0 新增/40 unchanged，POY/DTY 各可读取 20 个来源匹配点。它们可支撑当前参考预测，但同批首次捕获不能证明 8 月各历史日期当时已可见，OOS 仍必须 fail-closed。当前生产副本仍只有 POY 4 点、DTY 5 点的旧回退数据，隔离验收未写入生产。
- 当前最终只读、内容寻址 OOS/预测产物：`/private/tmp/seven-product-evaluation-v34.ZZ9Oba7/seven-product-evaluation-0806e9bab1313298.json`；latest 文件 SHA-256 `e4b6f184e457cbe767b045eb40c87930af08e5444879bd92962fedd93f629ac3`，111,908 bytes；证据正文 SHA-256 `0806e9bab1313298953770e754b53ccdda431204f4fb382fd9ce50e7d92cc455`，内部评估报告 SHA-256 `e1bb01200b5ed6b7b5fb2b048132f3026bf398e8e8ea164f5bdaa9d57bb243bc`。结果为 0/21、`formal=0`、`reference=18`、`unavailable=3`，运行器按设计返回退出码 2。输入为生产库的 schema-v34 迁移副本，SHA-256 `9752642ceb1f7922b43cad0d7933ec0cf2c12fe4e8d7c802cb668195b62f0176`、`integrity_check=ok`、运行中未改变；原生产库 SHA-256 仍为 `f10b35e7c22e5656ac73b6abda9ca37728096c1b30702598d9685fec7fc6a47e` 且 mtime 未变。当前生产副本没有 EIA/TNC/石脑油/DCE 的新正式 capture，因而未从隔离验收获得任何正式分数。
- 原油虽有 674/651/515 个 D1/D7/D30 滚动样本，但三个期限相对最佳朴素基线均未改善，方向准确率分别约 45.3%/52.8%/42.7%，并有 `non_monotonic_first_visibility`；修正后还明确报告 `frozen_label_source_mismatch`，因为生产副本尚无 EIA capture revision。其余六品种当前有效样本为 0。不能用原油样本量掩盖性能、来源身份和泄漏门禁失败。

### 12.2 官方期货日线与稳定投影身份

- 适配器只访问固定 HTTPS 官方文件路径，禁止重定向并执行出站域名白名单；最多回看 31 日，生产默认 10 日。
- 零成交且开高低收为零的非活跃合约会被跳过；任何有成交却缺价格、结算价异常或负成交/持仓会整源 fail-closed。
- 主力规则冻结为“收盘后持仓量最大，成交量次级、到期月最终打破平局”；每个 PTA/PX/甲醇交易日保存原始文件哈希、来源 URL、首次采集时间和不可变 revision。甲醇不是 MEG 标签，不得冒充 MEG 正式序列。
- 代码/持久化/幂等/预测读取定向回归通过；官网 10 日只读回看状态 `ok`。这些是 E2/只读网络证据，不是生产写入或足量 OOS 历史。
- DCE EG 适配器的缺凭据零网络、HTTPS-only、token 单次刷新、secret 不外泄、严格解析、原子投影/revision、精确重放与主力角色变化测试均通过；DCE 生产调度已标为 `important/daily`，但没有双凭据时必须降级，当前没有 live capture。
- 旧期货投影唯一键错误包含派生 `contract_role`，同一合约角色变化会生成重复投影。schema v34 改为稳定身份 `(source_id, trade_date, exchange, product, contract_code)`；升级前自动备份，只有行情数值和原始证据等价时才去重，非等价重复会回滚并失败关闭。
- 对生产库只读审计发现 66 组、132 行旧重复，全部来自 `akshare_prototype/MEG`，每组两行；原始载荷与全部行情数值一致，只有派生角色和来源发布时间不同。生产副本迁移演练将 schema 33→34、重复组 66→0、期货投影 19,054→18,988，主库和 v33 备份均 `integrity_check=ok`；原生产库哈希和 mtime 不变。生产实际迁移仍等待发布授权。

### 12.3 本轮故障矩阵证据

- 冻结合同：`server/contracts/seven_product_release_fault_matrix.v1.json`；15 个唯一场景。
- 运行器：`server/scripts/run_release_fault_matrix.py`；每次创建独立 DG01 根并输出 JUnit、manifest hash 与内容寻址报告。
- 本轮最终结果：`15 passed in 20.92s`，15/15；manifest SHA-256 `60cff647b18e5068f03915e0bc6a7c51a98e674b12053af2191ba46790ac87cb`。
- 报告：`/private/tmp/release-fault-matrix-v34.LlQ2OZz/fault-matrix-c6ff0ddba6993c13.json`，
  SHA-256 `f07655d362a8c871571a1d2e47ef184edd9f22fab6dca4e6e4cdca34801f1383`，2,617 bytes；
  报告正文 SHA-256 `c6ff0ddba6993c139d011e43d73577f894f52bf3145208606fa62f84bf719d37`。
- JUnit：`/private/tmp/release-fault-matrix-v34.LlQ2OZz/fault-matrix-junit.xml`，
  SHA-256 `b714af79f7f920efbc3a7128ae5390cca03b35a39b4d491b48693cc67c11dac2`，2,306 bytes。

该 15/15 仅关闭发布故障模拟的 E2 子门禁。尚未执行生产写入、发布、服务切换或首次真实日跑。

### 12.4 本机生产只读预检与 dry-run

- 预检：`/private/tmp/local-preflight.Aq4OPU/latest-preflight.json`，SHA-256 `9257f185c5ff41269027223ccbe877db967fd4c1eef51badd225fc121471cf50`，10,124 bytes；状态 `ready_with_warnings`，0 blocker，唯一 warning 为 5173 前端当时未运行。
- 每日链路使用生产库 SQLite 一致性临时快照执行 `dry_run`，未写生产库。联网结构化源 8/9 成功：EIA、FRED、CFETS、CFTC、CZCE、GACC、OFAC、TNC 成功，context 级 UN Comtrade 在整轮公开源预算耗尽后超时。
- dry-run 报告：`/private/tmp/local-daily-dry-run.WraBcg/output/latest-status.json`，SHA-256 `816153cb85d980c5d096fcd1f37ab254cbe4e527ee3d79eb5c6fb63a741bffa1`，44,855 bytes；状态 `blocked`。原因是 dry-run 不写入新采集值，而质量门读取快照中的旧 freshness、缺少当前回测摘要；这不是生产成功证据。
- UN 超时原来误报源级 `600s`，现已区分并报告真实整轮 `public_fetch_outer_deadline_exhausted_after_<budget>s`；回归 18/18 通过。

### 12.5 当前 E2 门禁摘要

- 后端全量：该阶段精确工作树在 DG01 隔离根和严格串行模式下 `1743/1743` 通过，735.56s、覆盖率 82.38%；新增 DCE EG、v34 稳定期货投影身份、等价去重、非等价失败关闭、EIA/TNC revisions、每日导入参数传递、幂等修订链、权限盘点并发失败关闭、隔离式 quick review 和 legacy 降级反例均包含在该轮全量中。迁移/基础设施定向集 124/124，DCE/官方期货定向集 57/57；所有直接 SQLite 上下文已改为 `closing(...)` 加事务上下文；`storage.connect()` 在 schema/迁移/PRAGMA 初始化失败时主动关闭。该阶段尚余的 TestClient/httpx2 外部弃用 warning 已在 12.13 闭合。
- 一次将普通全量与 2,683 文件的通用 Semgrep 重载扫描并行执行时，两个固定 10 秒预算用例分别在多进程迁移锁和本地栈健康失败路径超时，该次诚实记录为 1708/1710；停止重载扫描后两项连续两轮 4/4 通过，随后串行全量 1710/1710。发布门禁因此必须串行执行资源敏感测试，不得与重型全盘扫描并发。
- 备份权限定向回归：75/75 通过。未来的来源自动化备份、每日备份/恢复演练、迁移备份、公开生产复制/恢复和 RAG 重建备份统一把私有目录设为 0700，把数据库及 `-wal/-shm/-journal` 设为 0600，并在失败/保留期清理时连同伴随文件处理。
- 完整 E2E：连续两轮 154/154 通过；无共享业务状态顺序失败。
- AI：21/21 通过，provider mode 为 `offline_local_fallback`，外部模型调用 0；这是降级安全评测，不是在线模型质量证据。
- RAG：9/9 通过；`future_leakage_count=0`、`rejected_evidence_leakage_count=0`。
- 视觉：desktop 0.02% perceptual、mobile 0.25% perceptual，均 PASS。
- 安全/供应链：最终 `npm run review:security` 串行通过：项目自定义 ERROR 级 Semgrep 扫描 259 个 Git 跟踪目标，0 finding；Gitleaks 扫描 43 commits/约 8.85 MB，0 leak；Trivy 对 npm/uv 漏洞、Docker 误配和许可证均为 0。随后以同一自定义规则和 `--no-git-ignore` 覆盖 `server/app`、`server/scripts`、`server/tests`、`src`、`tests` 的当前未提交工作树，237 个适用目标、0 finding、0 error。通用 `--config auto --no-git-ignore` 的历史扫描噪声不能作为 0 finding 证据；本机 `.env` 被忽略且不得提交或复制到发布物。
- 静态：Ruff、TypeScript、资产检查、生产构建和 `git diff --check` 通过。
- 正式源运行时预检新增只读、零值泄露合同：不 source 或执行 env 内容，只接受当前用户所有、非 symlink、无 group/world 权限、UTF-8 且不超过 1 MiB 的文件；只报告 configured/missing 变量名。`.env.production.example`、`.env.local-production.example`、`.env.public-production.example` 已统一覆盖 EIA/FRED/UN/DCE。定向回归 70/70 通过。

以上通过项只关闭相应代码、测试和故障 E2 门禁。它们不能替代 21/21 真实 OOS、当前 release 首次生产成功和发布后公网 E1 smoke。

### 12.6 生产旧发布核验、权限迁移与 v34 发布准备

- 当前线上 `current` 指向发布 `20260831T072554Z-975bec7f8862d1df`，release hash 为
  `975bec7f8862d1df`；其 `dist/release.json` 记录 git SHA `9290a45ae8bd4b7f889f4011fc498b9af4dd085c`、`source_tree_dirty=true` 和明确 rollback target。当前仓库仍是同一 HEAD，但有 199 个状态项（154 个 tracked change、45 个 untracked entry），所以无法由 commit SHA 重建当前修正版。旧发布的 19 项鉴权公网 smoke、local live/ready/proxy 均通过，只能证明旧发布仍可用，不能作为当前修正版 E1。
- 停机前 7 个预期 launchd 任务已加载；Keychain 服务 `com.poydty.agent.public-login` 存在。为生产备份和迁移，后端、日跑、新闻调度和晨报已卸载并经进程/句柄复核；前端、隧道和健康探针保留。生产 runtime env 为私有普通文件，生产模式和单用户密码鉴权通过只读检查。新增正式源预检实测 EIA/FRED/UN 为 configured，DCE 双凭据均缺失；它不读取到报告或输出任何秘密值。
- 正式源预检报告：`/private/tmp/formal-source-preflight.yeUqv2T/latest-preflight.json`，SHA-256 `13ff20accca298402191509b192d7141496c6f70a378b164a38e125b1c091d81`，13,430 bytes；按设计退出码 1，唯一 blocker 为 `dce_meg` 缺少 `DCE_API_KEY,DCE_SECRET`，唯一 warning 为前端 shell 当时未运行。
- 2026-09-01 的一次手工 EIA 只读验收因测试路径重复而返回 404；未净化的本地命令 traceback 将查询字符串中的 EIA API key 显示在当前评审工具输出。生产 API 和调度路径已有 `safe_summary`/异常类型净化且相关回归通过。文档不记录旧值或新值。
- 最新短周期新闻/来源运行于 2026-08-31 16:26Z 至 16:30Z，状态 `degraded`；OFAC 正常 `unchanged`，公开结构化抓取无错误。隔离的新闻错误为 UKMTO 403、IEA 403/browser check、GDELT 超时、中石化连接错误、中石油超时。旧本地日跑还报告 5 个 manual/blocked、质量需人工复核和行动级效果门禁未过，不能冒充当前 21 格正式成功。
- 已按用户明确授权执行生产权限迁移。第一次在线 `--apply` 收紧 234 个文件和 36 个目录；随后旧日跑新建 3 个 0644 备份产物，证明必须停写入后复核。卸载后端、日跑、新闻调度和晨报写入任务后再次收紧，最终 325/325 个 SQLite 类产物为 0600、36/36 个相关目录为 0700、0 blocker。最终只读复核报告：`/private/tmp/sqlite-permission-finalcheck.tj1sSR/sqlite-permission-migration-88b9b25fe08d40ca.json`，正文 SHA-256 `88b9b25fe08d40ca6e6315c24c8a2b720629cb8f4d48053713211fd3eb28bcc4`。
- 权限工具现将盘点期间文件消失或内容变化报告为 blocker，不再抛出无结构的 `FileNotFoundError`；`--apply` 仍在 device/inode/owner 复核后才 chmod，失败尽力恢复 prior mode，绝不删除 sidecar。相关新增测试与完整回归通过。
- 停写入后已创建完整性校验的 v33 生产备份 `shared/data/pre-v34-backups/agent-v33-pre-v34-20260901T094000Z.db`，0600，467,623,936 bytes，SHA-256 `e54a956fef2590e501bdbad8dd11737e7b3a0996b1bd0f3b3ae10539b9bbd086`。备份含 20,945 条期货投影、504 个旧自然键重复组。
- 最新备份的独立副本已用当前代码重演 v33→v34：等价去重后 20,376 条，重复组 0，`integrity_check=ok`、外键违规 0；迁移副本 SHA-256 `c37cb9091c32bf7f333b39397ac6b80cdcf0a1f545b9a54ec6c0a00b0a5ef313`。在该准备时点生产主库仍为 v33；实际授权迁移结果见 12.7。

### 12.7 授权后的提交、生产迁移与部署结果

- 199 个审计内路径已提交为 `ca401ed4c80b2c57c51c1d32aee40338b6dcb42d`（`feat: deliver seven-product forecast readiness v34`），推送至 `origin/codex/project-correction-100-v34`；提交时工作树 clean。提交前精确工作树证据为：后端 1743/1743、覆盖率 82.38%，隔离 quick review、Ruff、TypeScript、资产、构建、Semgrep、Gitleaks、Trivy 全绿。
- 不可变 release `20260901T020226Z-d8b6bf1d6a05de07` 已原子切换为 `current`；release hash `d8b6bf1d6a05de07`，Git SHA 与上述提交一致，`source_tree_dirty=false`，rollback target 为 `20260831T072554Z-975bec7f8862d1df`。
- 生产主库已从 v33 迁移到 v34：20,945 条旧期货投影经等价去重为 20,376 条，稳定自然键重复组 0，`integrity_check=ok`、外键违规 0；迁移记录为 `stable_futures_projection_identity_v34`。主库迁移后 SHA-256 `292ec2762e38fe1248918f1b9e94235357b8b70168093dbd2aafd05b3aafb23b`，自动迁移备份和独立 v33 备份均为 0600。
- 新 release 的 local live/ready/deep 与公网单用户鉴权 smoke 19/19 通过；公网命中的 release id/hash 与部署目标一致，匿名 release 重定向、匿名 API 拒绝、鉴权 API、七模块页面和语义索引均正常。
- 新 release 首次真实 apply 日跑于 2026-09-01 02:09:29Z→02:16:39Z 完成，退出码 0、`ready_with_warnings`、0 blocker。备份/恢复演练、health/ready/deep/metrics、9/9 freshness、单位、正值和未来泄漏门禁通过；报告 SHA-256 `cf3a6c01bedd7fb37e352ed84aaf7fb2c263d1ef5e775ba4b35072c9c72a19dd`。
- 日跑暴露的生产 allowlist 漏项 `www.czce.com.cn`、`www.tnc.com.cn` 已补齐；定向真实重跑中 CZCE 官方日线存储 213 条、TNC POY/DTY 历史新增 252 条，0 error，报告 SHA-256 `c9a277fa3cf03a207871fa4f53b6d9bf339f22829643ab469305856d16dd0467`。模板同步修正，DCE 仍因双凭据缺失 0ms fail-closed。
- 发布后生产一致性副本评测仍为完整 21 格但 `formal=0`、`reference=18`、`unavailable=3`、OOS `0/21`，退出码 2；latest SHA-256 `9d8583ddcd580a590382a37b0577b90300d9118132780396cbf1d7cc8681d9c0`，证据正文 SHA-256 `861d8e682b18aec659400dc9af7bb2472556a95b7f894b6bb140a7e5aca9cf06`。部署、数据新增和首次日跑均未改变终局 `INCOMPLETE` 判定。
- 正式源生产预检在正确 4173 前端端口下仅余 `dce_meg` 缺 `DCE_API_KEY,DCE_SECRET`，0 warning；报告 SHA-256 `489a67e80432a33668e1d333c1c6542b53916d6abecf394557bb966e8a993622`。

### 12.8 来源就绪状态语义修正

- 首次生产日跑中的“5 个 manual/blocked”不是五个都需要人工。根因是状态汇总把全部未处于 `success/ready` 的任务（包括自动调度到期的 `due`）统称为 manual/blocked；同时 CZCE 的正式数据写入 `futures_daily_bars`，旧盘点却只读取 `market_observations`，UN Comtrade 的 historical-context 又错误按历史数据期而非最近成功可得性检查判 freshness。
- 状态合同现拆为 `not_ready` 与 `manual_or_blocked`：前者包含自动到期，后者只包含明确要求人类动作、manual automation level 或 `blocked`。CZCE/DCE 从官方期货日线表取最新交易日；UN historical-context 用最近成功可得性捕获时间作运行就绪证据，仍保持 `current_formal_eligible=false`。
- FRED 同时含日、周、月序列。旧 4 个自然日观测窗口会在周末后的首个工作日误报周五日频数据过期；运行就绪窗口改为 7 个自然日，仍会在整周缺失后失败关闭。该调整只影响来源健康告警，不放宽预测 OOS、点时、5% 或 55% 门禁。GitHub 复用检查只找到通用 FRED 客户端和业务日日历库；为一个静态健康窗口引入新依赖的集成与供应链成本不合理，沿用现有策略模型。
- 针对性 E2 为 Ruff 全绿、44/44 通过。生产库只读复核为 12 项任务：`success=8`、`ready=2`、`due=1`、`blocked=1`，`not_ready=2`、`manual_or_blocked=1`；唯一人工项是缺少双凭据的 DCE，OFAC 只是无需人工的 30 分钟自动到期。该结果尚待以新不可变 release 和真实日跑固化为 E1。

### 12.9 来源状态修正版的发布性能门禁

- 来源状态修正提交 `a48bcadf0661c6e273222e724fc8983d7a1636df` 已推送并生成不可变 release `20260901T030829Z-d7b89632bdac0010`；代码来源 clean、共享 v34 数据库未复制或替换、回滚目标为 `d8b6bf1d6a05de07`。本地 live/ready/deep 通过，但三次默认 20 秒公网 smoke 分别在 RAG 或 market-chain 冷请求超时，故该 release 当时诚实判为发布门禁未过。
- 后端结构化日志测得 market-chain 冷请求约 13.2–19.0 秒、热请求约 1.5–1.9 秒；生产库进程采样和 cProfile 将根因定位为 FX 历史换算：779 个 USD/吨价格点逐点重建整份 FRED 日期数组，并在同一工作台构建中重复读取 FX，形成约 927 万次字典访问。该问题影响默认 smoke 20 秒预算及 API p95，不是网络错误。
- 修正后一次工作台构建只读取一份一致的 FX 快照，并为全部价格点复用单次日期索引；不改变点值、结转上限、来源或展示合同。相同生产 v34 库的隔离冷构建由 17.24 秒降至 6.26 秒，函数调用由约 1,136 万降至 229 万；固定 `as_of_time` 的修正前后规范化响应均为 SHA-256 `9c65d3041c8403e37431d40f9afaaafd961337d199cb36ecfd7befd5cb090c7c`，逐字相等。定向回归 23/23 通过；仍须重新完成全量、提交、新 release 与默认 20 秒公网 smoke，才可转为 E1 通过。
- FX 修正提交 `4c136cfda4a373b9cf0e61a35faceff24c681123` 的 release `20260901T034516Z-6230d6b5d81dfb24` 已让冷态公网 market-chain 在默认门禁内通过；同轮唯一失败转为 RAG 可视化 20.19 秒。cProfile 显示首次本地 ONNX 模型/查询约 5.8 秒、活动向量矩阵首次载入约 3.1 秒，属于后端进程冷启动成本；原有前端预热不能覆盖“仅后端重启”。
- staging/production lifespan 因此在 readiness 前同步预热默认业务查询和当前活动向量矩阵，失败只记录状态或异常类型，不记录查询、证据或凭据，也不写数据库。真实生产 v34 只读测量为 9,694/9,694 向量成功预热，预热 7.705 秒，随后首个 RAG 构建 5.100 秒；定向回归 38/38。仍须全量、提交、再生成 clean release，并从全新后端进程通过默认 20 秒公网 smoke。
- 启动预热修正已作为提交 `44e973e7ff208ed5c81693f995f791396894625d` 推送；提交前精确工作树后端 `1750/1750`、覆盖率 82.39%，前端类型/资产/生产构建、隔离 quick review、Semgrep 260 目标、Gitleaks 47 commits、Trivy npm/uv/Docker/许可证均通过或为 0 finding。不可变 release `20260901T041320Z-7356c46f5c76f445` 来源 clean、release hash `7356c46f5c76f445`，未复制或替换共享数据库，回滚目标为 `20260901T034516Z-6230d6b5d81dfb24`。
- 只重启后端后等待同步预热完成，立即执行默认 20 秒公网 smoke，19/19 通过；随后常态第二轮仍为 19/19。两轮均命中新 release，匿名边界、live/ready/deep、1.44 MB market-chain、RAG、9,694 向量活动索引及七模块全部通过。后端/前端 PID 已更新，新闻调度 PID 未改变；服务启动日志没有 warmup incomplete/failed。FastEmbed 对当前模型均值池化的上游 warning 仍存在，但活动索引与查询 embedding mode 一致，未作为静默成功替代品。

### 12.10 修正版真实日跑、权限复核与终局复测

- 新 release 先以 backup-first 的 `--record-plan` 写入 12 条来源状态；报告 `/path/to/user/Library/Application Support/POY-DTY-Agent/shared/local-production/source-status-remediation/source-automation-latest.json`，SHA-256 `3e90a372f5cc48eef90293ef4f549a8d52ee89968641514ec032a50f6685805f`。备份 `agent.db.pre_source_automation_20260901_041633.sqlite` 为 474,734,592 bytes、SHA-256 `f4673ef50914f36942a9272f2bbf628d6f91cec7b46e0a0f5a14590e5022d46d`、`integrity_check=ok`；当时状态为 success 8、ready 2、due 1、blocked 1，人工或阻塞仅 DCE 1 项。
- 完整真实 `--apply` 日跑于 04:17:29Z→04:24:49Z 完成，退出码 0、`ready_with_warnings`、0 blocker；`latest-status.json` SHA-256 `317afd8e66e1a00c7c0a592cd170ea2b7898d029af891b19b310e70620dded10`，46,319 bytes。OFAC 到期抓取成功后来源为 success 9、ready 2、blocked 1、`not_ready=1`、`manual_or_blocked=1`；DCE 仍在缺凭据时 0ms、零请求 fail-closed。AkShare 原型全历史抓取 90 秒超时及 5 个新闻站点的 403/连接/超时被隔离，不构成关键来源失败；正式效果门禁仍为 `accuracy=55.44%`、2538/4008、覆盖率 63.32%，低于行动级 75%，继续标记 `needs_human_review`。
- 日跑末尾生成的备份与恢复演练副本均为 479,420,416 bytes，SHA-256 同为 `4c1b634051a178fcbdf0ff35ebfff1b9091a4f720f33dc91cde79567cd4baccd`，两者 `integrity_check=ok`。随后生产 SQLite 权限迁移覆盖 340 个当前数据库/sidecar 工件；`--apply` 前不安全项 0，最终只读复核为 clean、0 blocker，报告正文与 latest 文件 SHA-256 `be803ad63543bac450e7b3370f4e075f108f743119da3919653b4da934d1c6a7`，报告文件 0600。
- 正确从源码仓运行的正式源生产预检仅 1 blocker、0 warning：`dce_meg` 缺少 `DCE_API_KEY,DCE_SECRET`；报告 `/private/tmp/formal-source-preflight-repo.nyI0jL/latest-preflight.json`，SHA-256 `917cf336caf1d57c088b1659c71ec6f82814322abd713fc5360364a4dd3829f0`。从精简不可变 release 根运行预检会因开发/交付文件有意未打包而产生 27 个额外假阳性，该次结果已排除，不作为发布判断。
- 对日跑后完整性一致的恢复演练副本再次执行只读 21 格评测：合同 21/21 生成，但 `formal=0`、`reference=18`、`unavailable=3`、OOS `0/21`，运行器按设计退出 2。内容寻址报告 `/private/tmp/seven-product-evaluation-final.vxq89M/seven-product-evaluation-fadc3fa29dac690a.json` 与 latest 文件 SHA-256 均为 `f5bedbb16d1a5df3c6be444ed2cf6e18feb5fe8514b790c60486bc0858b498f3`，证据正文 SHA-256 `fadc3fa29dac690a7ba639d026b8c19e96db359e5e4bea6d1fb46c69e14407d2`。因此本轮发布门禁通过，但项目终局仍为 `INCOMPLETE`，不得换算成 100 分。

### 12.11 七品种真实逐日预测账本（schema v35）

- 终局复核发现此前 `/forecasts/seven-product` 每次请求都会重算“当前预测”，没有把每日发出时的 21 格结果冻结为不可变记录；因此即使代码持续运行，也无法诚实证明“当日预测→以后实际值→误差/命中”的真实样本外链路。schema v35 新增批次、21 格预测和实际值结算三张 append-only 表，并用数据库触发器禁止更新和删除。每天按上海业务日期至多冻结一个批次；相同日期重跑返回同一批次，不重算、不覆盖。
- 普通当前预测 API、工作台、预测页和导出现在读取最新已冻结批次；显式 `as_of_time` 仅保留为诊断预览。新增历史 API 和 UI 表格展示发出日、目标日、结算状态、实际值、误差与方向命中。若当天尚未成功冻结，当前预测接口返回 503，而不是即时拼装一份看似正式的结果。
- D1/D7/D30 结算只接受发出后才可见、来源身份与单位匹配、带原始证据哈希的真实后续观测；不回填历史预测，不用代理值替代，不把同批首次抓取的旧日期冒充当时可见。结算账本只积累真实运行证据，不自动绕过原 21 格 OOS 晋级门禁。
- 每日生产链已接入 `run_seven_product_forecast_lifecycle.py`：先结算已到期格，再冻结当天 21 格；报告使用 0700 目录、0600 原子内容寻址文件。报告中的 `formal=0/21` 为诚实 warning，来源身份或结算完整性失败才是 blocker。回滚必须成对执行：旧 release 不认识 v35，因此代码回滚时必须在停写后恢复迁移前 v34 备份。
- 本轮提交前精确工作树门禁：后端 `1759/1759`、覆盖率 82%；Playwright `77/77`；故障矩阵 `15/15`；AI `21/21`；RAG `9/9`；视觉 desktop 0.02% / mobile 0.37% perceptual，均 PASS；TypeScript、资产、生产构建、Ruff、Semgrep、Gitleaks、Trivy 与 `git diff --check` 全绿。生产正式源只读预检仍只有 DCE MEG 双凭据缺失这一 blocker；因此 v35 代码可发布，但正式预测仍不得宣称 100 分或 21/21。

### 12.12 v35 生产迁移、重跑缺陷闭环与当前 E1

- 主实现提交 `669976fad4e39b402c35ca851d1eaf3e9ce8533d` 与生产重跑补丁提交 `eabeb14e92a362a018d8cda15143bc517917ac0a` 均已推送至 `origin/codex/project-correction-100-v34`。当前不可变 release 为 `20260901T062807Z-ef00fa2a16925a63`，release hash `ef00fa2a16925a63`，Git SHA 为补丁提交，`source_tree_dirty=false`。
- 停写后创建 v34 独立备份 `shared/data/pre-v35-backups/agent-v34-pre-v35-20260901T0611Z.db`：479,490,048 bytes、0600、SHA-256 `cd77b89c0ef854165410b3e744244347ff43123b0e29a912563c7035cc4ee9eb`、`integrity_check=ok`、外键违规 0、最新迁移为 v34。生产主库随后成功迁移至 v35，迁移记录为 `append_only_seven_product_forecast_ledger_v35`，迁移后 `integrity_check=ok`、外键违规 0。
- 首次生产 lifecycle 成功冻结 2026-09-01 的 21 格批次 `seven-6334b9625ed5c72c6482f504`。第二次进程级重跑暴露两个同一根因链上的边界：官方日频 `YYYY-MM-DD` 观测身份被严格时间戳解析器拒绝，以及尚未到期的 MEG 参考格因正式来源缺失被过早当成结算 blocker。实现现只在观测顺序比较中把合法日期身份规范化为 UTC 午夜，`visible_at` 仍强制带时区；并且只有达到目标期后才执行结算来源/证据门禁，未到期格保持 pending，绝不提前写实际值。
- 补丁定向回归 14/14、补丁后全量后端 1761/1761（458.90s）通过。生产 v35 一致性副本连续两次 apply 均为 `ready_with_warnings`、0 blocker；随后生产原库也连续两次通过，批次数始终 1、格数始终 21、outcome 仍为 0。当前 18 格 pending maturity、3 格 issue-time unscoreable；这是真实时间尚未到期的状态，不得回填。
- 最新 lifecycle 报告正文 SHA-256 `aa42c23376ad8050cc3904d9c568c2eed6714e90e32900bb96f46f7391d740fd`；批次 payload SHA-256 `f2357e60811647467f726ddc7e5c6cfdf6a6dd1d0524b1e53c91ff189bc1e92f`。当前 API 与 history API 均返回同一批次和完整 21 格，`formal=0`、`reference=18`、`unavailable=3`。
- 本地 live/ready/deep 均为 200；重启后的鉴权公网 smoke 19/19 通过，命中上述 release，匿名 release 为 303、匿名 API 为 401、语义索引 ready 且 9,694 个向量、七模块均为 200。另行鉴权核验公网 current/history 两接口均为 200、batch id 一致、各含 21 格。
- 生产权限最终停写复核覆盖 342 个 SQLite/sidecar 工件，0 个不安全权限，状态 `clean`；报告正文 SHA-256 `c3446be08b2e2ff75e888ba79c4a952a4cf0c2a2a530f8dec1cbe8098978fed5`。后端、前端、隧道、新闻调度、日跑、晨报和健康探针均已恢复。
- 结论分层不变：v35 生产参考预测账本 **GO**；正式七品种预测仍为 **NO-GO / INCOMPLETE**，因为 MEG 当前正式来源为空且真实 D1/D7/D30 OOS 仍为 0/21。这两项不能用代码提交、历史回填或降低门槛替代。

### 12.13 DCE 免费边界与 TestClient 依赖闭环

- 2026-09-01 复核的公开说明表明，大商所门户 API 面向已注册的门户用户免费开放，覆盖通知公告、日/周/月行情、统计与业务参数；用户仍须在门户的“数据服务→数据工具→API服务”申请，并遵守账户级每分钟频率限制。该免费 API 不等同于实时行情、Level-2 或商业再分发授权，后者属于另行报价的数据服务。当时文字曾把用途概括为“个人、非商用”；这一定性已被 v1.26 修正：单操作者为生产经营与采购研判使用可以构成商业使用，是否适用免费门户范围仍以来源当时条款为准，不再以“个人”自动推导“非商用”。
- 项目代码、HTTPS-only 出站边界、严格 EG 解析、append-only revision、幂等与生产调度此前已经接入 DCE；本轮不新增替代抓取器，也不绕过门户登录。当前唯一外部动作仍是操作者注册/申请并取得 `DCE_API_KEY`、`DCE_SECRET`，之后通过本机私有生产环境安全录入；密钥不得粘贴到聊天、提交或日志。
- 经用户批准，测试依赖组新增 `httpx2>=2.12.0,<3`，生产运行时 `httpx` 保持不变。回归测试锁定 Starlette TestClient 继承 `httpx2.Client` 且模块选择不再回退；`python -W error` 导入检查、Ruff 与定向 7/7 均通过，DG01 严格串行后端全量为 `1762/1762`（491.25s），无 TestClient 弃用 warning。
- 该闭环回收缺口 11 的 0.4 分，当前严格评分由 79.4 提升为 **79.8/100**；剩余 20.2 分中，18 分仍取决于真实逐格 OOS 达到 21/21，约 2.2 分取决于为 MEG 选择并验收替代正式来源及七标签持续 point-in-time 历史。免费资格和代码存在均不能替代这些真实运行证据。

### 12.14 DCE 官方 HTTPS 关闭 VPN 后的复核（2026-09-01，NO-GO）

- 上一轮（12.13 前身任务包 `dce-live-credentials-20260901.md`）在 VPN 开启下记录 DCE HTTPS TLS reset；本轮在用户关闭 VPN 后按同一阶段门重测，凭据全程未读取、未消费。
- 代理/VPN 状态：进程代理环境变量与系统代理（scutil）均未启用。DNS：公共解析器 223.5.5.5 与系统解析一致返回真实公网地址 `218.25.154.72`、`59.44.106.20`（联通辽宁），无 Fake-IP/私网保留段命中。
- 无凭据 HTTPS 探测（正常证书校验、禁重定向、`trust_env=False`）：`/dceapi/cms/auth/accessToken` 与 `/dceapi/forward/publicweb/dailystat/dayQuotes` 均 `ConnectTimeout`（约 32s，多栈尝试）。
- TCP 层细分：两 IP 的 **443 全部静默超时**（8s 级），**80 端口 0.1s OPEN** 且返回 DCE 门户 WAF 特征（412 Precondition Failed、Server 头掩码、WAF cookie），证明 IP/主机身份正确、路由可达，阻断精确定位在 443/TLS 层。
- 判定 **NO-GO**：按阶段门"TLS reset/不可达不能通过"停止；未读取 Keychain、未写生产 env、未写数据库、未尝试 HTTP 凭据登录。工作树与生产状态均无变更。
- 需要外部动作：DCE 支持确认官方 443/API 网关对该网络出口的可达性或按出口 IP 放行；上一轮 VPN 下 TLS reset、本轮裸网静默丢弃，两种形态一致指向 DCE 侧 443 对本网络不可用，而非本机配置。双凭据仍安全保存在 Keychain（`com.poydty.agent.dce-api-key/-secret`），等待通道打通后进入阶段 C–F。

### 12.15 DCE 软移除与 MEG 显式缺口（2026-09-01，用户决定）

- 用户决定不再把 DCE 作为当前运行来源。`dce_meg` 采用与 CCF 一致的可逆软移除：注册项保留且精确查询可见，但默认来源清单、抓取 API、结构化采集参数、逐源调度、采集任务、人工/阻塞汇总、告警和正式凭据预检均排除 DCE；直接抓取返回 410 `source_soft_removed`。
- 七品种 × D1/D7/D30 合同保持 21 格不变。MEG 的冻结标签身份仅保留为历史合同，当前加载在读取 DCE 历史行或任何盘中代理前即返回空序列和 `dce_source_soft_removed`，因此三格为明确不可用来源缺口；没有替代源被静默升格。
- 新 DCE 期货 CSV/API 导入被稳定拒绝且不写库。已有 DCE 行、append-only revisions、适配器/解析器、安全与幂等测试均保留，用于历史复现和可逆恢复；生产数据库没有删除或迁移。Keychain 中的两个 DCE 凭据服务保持原状，未读取、未复制进 runtime env、未删除。
- 这一决定消除了“必须补 DCE 凭据/等待 DCE 443”这一运营任务，但没有填补 MEG 数据，也不提高项目得分。严格评分仍为 **79.8/100**，正式预测仍为 **NO-GO / INCOMPLETE**；剩余路径是另行选择并验收 MEG 正式来源，以及让 21 格真实 point-in-time/OOS 证据自然积累。
- 实现提交 `f0539f97ca8f5b051c122e721f23957375eb74c0` 已推送并部署为不可变 release `20260901T092324Z-7e3ae41e4c0d8ef9`；release 记录 `source_tree_dirty=false`，回滚目标为 `20260901T062807Z-ef00fa2a16925a63`。本次是纯代码发布，`database=null`，未复制、迁移或删除生产数据库，也未修改 runtime env 或 Keychain。
- 本轮精确验证：后端全量 `1770 passed`；定向 `323 passed`；Playwright `77 passed`；故障矩阵 `15/15`；AI `21/21`；RAG `9/9`；TypeScript/资产/生产构建、视觉 desktop/mobile、Ruff、Semgrep、Gitleaks、Trivy、npm audit 与 `git diff --check` 均通过。生产预检为 `ready`、0 blocker、0 warning，且不再检查 DCE 凭据。
- 发布后本地 live/ready/deep/frontend 均为 200，鉴权公网 smoke `19/19` 通过并命中新 release；匿名首页为 303、匿名 API 为 401。首次公网 smoke 遇到部署前已存在的 Cloudflare Tunnel 旧 Fake-IP `198.18.4.x` 连接状态并返回 530；只重启 tunnel 后连接到真实 Cloudflare edge `198.41.192.107`，未改 DNS 配置或应用数据。
- 生产 API 只读核验：活跃来源 22 个且无 `dce_meg`；来源任务 11 个且无 DCE；legacy 状态为 `soft_removed`、`historical_rows_read_only=true`、`scheduled=false`、`writes_database=false`；直接 DCE fetch 为 410。新代码对生产库的只读预览为完整 21 格、`formal=0`、`reference=15`、`unavailable=6`；MEG 三格均为 `model_unavailable`、`point_forecast=null`，并带 `dce_source_soft_removed` 与 `label_history_missing`。既有不可变预测批次不回写。

### 12.16 OOS 双时间资格修正（2026-09-01，待发布）

- 发现 `seven-product-oos-gate.v2` 将观测顺序中的相同 `visible_at` 视为整条序列永久泄漏。该规则能拒绝批量历史首采冒充旧 OOS，但也会在后续真实日采开始后继续永久阻断整条序列，不能自然恢复。
- 评测策略升级为 `seven-product-oos-gate.v3`：训练数据仍必须在预测原点信息截点前可见；目标实际值的业务日不得早于该截点日期，且 `actual.visible_at` 必须严格晚于 `origin.visible_at`。批量首采历史本身仍产生 0 个 OOS 样本，但可在首采后成为后续真实预测的训练数据。相同或非顺序的合法 revision 可见时间不再污染整条序列；观测乱序、重复身份、缺来源和 `visible_at < observed_at` 仍失败关闭。
- API 最近结果新增 `actual_visible_at`，使每个计分样本可直接审计“实际值晚于预测截点才可见”；由于这是新增必填响应字段，结构版本同步升级为 `seven-product-evaluation.v2`。模型、TypeScript、受控 OpenAPI、ADR 和 API 文档同步更新；通用 Purged CV/Jano 方案经复用评估后未引入，因为现有双时间合同只需局部资格规则，无需增加依赖和供应链面。
- 最终精确工作树验证：DG01 后端全量 `1772/1772`，评测/API 定向 `10/10`，工作台架构 Playwright `8/8`，TypeScript、资产、生产构建、Ruff 与 `git diff --check` 全绿。对生产 schema-v35 数据库的只读运行得到完整 21 格、`formal=0`、`reference=15`、`unavailable=6`、OOS `0/21`；18 个有历史的非 MEG 单元均为 `leakage_status=passed` 但有效样本仍为 0，MEG 三格为 `not_testable`。报告 `/private/tmp/seven-product-evaluation-v3-final/seven-product-evaluation-544ffa3529f00159.json`，证据正文 SHA-256 `544ffa3529f00159437f1430d4babbbc6d82019e52cd294c239cf916b6a71010`，评估报告 SHA-256 `86cef146acbc0003d555ee8a4f43c6600b87c641f92cbab9242021fdd75a27e9`；输入库 `integrity_check=ok`、SHA-256 `f60cbda7b2445fb84a52417eaec3e5714a7615d90908592c6532853452b4b1ef` 且运行中不变。
- 工作台治理详情现直接展示评测结构/策略版本，以及最近 OOS 的原点观测、原点可见、实际观测和实际可见四个原始 ISO 时间；用户可以在界面核验 `actual.visible_at > origin.visible_at`，不必只相信后端门禁结论。该 UI 补充后的 TypeScript、资产与生产构建全绿，定向 Playwright `1/1` 通过。
- 该修正关闭的是“未来永远无法累计”的代码缺陷，不创造历史绩效，也不回收评分。严格评分仍为 **79.8/100**、正式预测仍为 **NO-GO / INCOMPLETE**；MEG 替代正式来源仍需用户选择，所有单元仍需真实时间积累至少 20 个有效样本并逐格达到 5%/55% 门禁。

### 12.17 SunSirs MEG 候选点时链路（2026-09-01，待发布、未晋级）

- 当前仓库原本已把 SunSirs EG 公开页作为泛化的分钟级上下文刷新，但其来源身份仅为 `public_spot_page_refresh`，MEG 重抓也没有专属 append-only revision；因此即使页面可读也无法形成可审计的替代标签候选历史。
- GitHub 复用检索只找到 AkShare 旧 100ppi 历史接口故障讨论及低质量通用爬虫，没有维护良好且能保留本项目双时间、原始哈希和幂等修订合同的现成实现；不新增依赖，复用现有 HTML 解析器与 `source_capture_revisions` 原子写入。
- 现将该公开页规范为**候选而非正式标签**：来源 `sunsirs_public_commodity_assessment`，语义序列 `meg.sunsirs.china.spot_assessment.cny_mt`，合同 `meg-sunsirs-candidate.v1`，解析器 `sunsirs-meg-public-page.v1`。页面日期作为 `observed_at`，首次成功采集时间同时作为保守 `published_at/visible_at`；原始证据哈希相同的重抓只保留首个 revision，内容变化才追加 revision。
- 公开方法文件将口径定义为 GB/T 4649-2008 工业用乙二醇优等品、出库价/净水、库区自提、现款全额支付、每批 50-1000 吨；样本为 20-30 家生产商、经销商和终端用户，评估时段 09:00-10:30、计划 11:30 发布。但专项文件日期为 2013-11-01，当前适用性未独立复核，当前价格页也不给行级发布/修订时间。候选原始证据因此显式绑定上述标签定义、方法 URL、文件日期和 `current_applicability_unverified`，不用声称的 11:30 代替真实首次捕获时间。标签定义变化会改变原始证据哈希，避免方法漂移被当成同一证据。
- 正式边界保持不变：`load_current_label_series("meg", ...)` 在读取 DCE 历史、SunSirs 候选或任何盘中期货代理前仍立即返回空序列、`source_matches_label=false` 和 `dce_source_soft_removed`。候选现货不会静默替代 DCE 历史合同，不改变 21 格、预测数值、OOS 样本或评分。
- 真实网络 `apply=false` 验收从 `https://www.sunsirs.com/uk/prodetail-222.html` 取得 2026-09-01 的中国乙二醇现货评估 `5930.00 CNY/mt`，来源身份和候选序列正确。将方法元数据绑定进原始证据后，当前精确工作树的原始证据 SHA-256 为 `3657e7e6285b01ba421edb998431c544e06d7465babfbe6232f699a48b924b2a`，`stored=0`、`writes_database=false`，临时 SQLite 路径未产生数据库文件。同次返回的新浪 EG0 `5525.00 CNY/mt` 仍标为代理行情，不会进入候选修订链或正式标签。隔离回归证明解析/候选身份、方法定义哈希绑定、原子 revision、精确重抓幂等以及“候选绝不正式化”。
- 当前精确工作树追加发布就绪证据：全新 DG01 根下后端全量 `1775/1775` 通过（4 分 26 秒），候选/预测/评测/API 相关回归 `50/50` 通过；完整 Playwright `77/77` 通过（2.2 分钟）；冻结生产故障矩阵 `15/15` 通过，JUnit `/private/tmp/seven-product-fault-matrix-v3-final/fault-matrix-junit.xml` 为 2,291 bytes、SHA-256 `77c84b23b9191045c13d23b71688f2462fc7d835c1457b8aec1b7926f68ead9f`，证据正文 SHA-256 `46020ca08258eab828ea14f53590cd4c71152daa382610980883ee9884f413cf`。沙箱内后端首跑的 8 个失败全部为 `/bin/ps` 被禁；对应治理证据文件在正常权限下 `9/9` 通过，随后全量无失败。`npm run review:security` 在正常系统 CA 信任库环境下串行通过：Semgrep 扫描 262 个适用的 Git 跟踪目标、0 finding；Gitleaks 扫描 56 commits/约 9.72 MB、0 leak；Trivy 对 npm/uv 漏洞、Docker 误配、秘密与许可证均为 0 发现。沙箱内安全首跑仅因系统 CA trust anchors 为空而在 Semgrep 启动前失败，不是代码或扫描发现。
- 该链路让被接受后的候选能从首次生产采集开始积累真实 point-in-time 历史，但当前没有写生产、没有发布、没有获得操作者对现货标签定义的确认，因此仍不回收任何分数；严格评分保持 **79.8/100**。

### 12.18 SunSirs MEG 当前标签晋级（2026-09-01，已发布并完成首捕）

- 操作者确认接受 SunSirs 中国乙二醇现货评估作为当前 MEG 标签源，同时接受其非成交属性、2013-11-01 方法文件当前适用性未独立复核的风险，并明确授权提交、推送、部署和首次生产采集。该决定只冻结来源身份，不降低 20 个有效 OOS 样本、相对最佳朴素基线改善 5% 和方向准确率 55% 的逐格门槛。
- 标签合同升级为 `seven-product-labels.v3`：来源 `sunsirs_public_commodity_assessment`、序列 `meg.sunsirs.china.spot_assessment.cny_mt`、值字段 `last`。页面业务日期为 `observed_at`，首次成功捕获为保守 `published_at/visible_at`；方法元数据、接受日期和标签版本绑定进原始证据哈希，相同证据重抓幂等，内容变化追加 revision。
- DCE 保持软移除。历史 DCE revision 改用独立冻结身份 `meg.dce.main_continuous.settlement.cny_mt` / `historical-dce-meg.v1`，避免当前注册表变化把旧 DCE 数据误标成 SunSirs；当前 MEG 加载器只接受专属 SunSirs append-only revision，缺失时即使有 Sina/Eastmoney/DCE 行也保持非正式降级。
- 不需要数据库 schema 迁移。回滚只切回前一不可变 release；共享库中新追加的 SunSirs revisions 不删除，旧 release 的显式 MEG 缺口路径不会读取它们。
- 晋级后的直接隔离验收为 52/52 通过，覆盖合同身份、无 capture 失败关闭、专属 revision 加载、DCE 历史隔离、原始证据哈希绑定和重复采集幂等。完整发布门禁、生产首次采集和发布后只读证据将在本节继续记录；在这些证据形成前不提高 79.8 分基线。
- 提交前冻结工作树门禁：DG01 后端全量 `1776/1776`（489.28s）、Playwright 无并发复跑 `77/77`（4.1 分钟）、视觉 desktop `0.01% perceptual / 0.35% raw` 与 mobile `0.25% / 1.12%` 均 PASS、故障矩阵 `15/15`、AI `21/21`、RAG `9/9`、TypeScript/资产/生产构建、Ruff 与 `git diff --check` 全绿。Semgrep 扫描 262 个适用跟踪文件 0 finding，Gitleaks 扫描 56 commits/约 9.72 MB 0 leak，Trivy 的 npm/uv 漏洞、Docker 误配、秘密和许可证均 0 发现。首轮 Playwright 与多项重门禁并发时有 1 个无关事件格式化用例因 `Response disposed` 失败；该用例单跑通过，随后无并发完整复跑 77/77，判定为资源竞争型瞬时失败而非产品缺陷。
- 实际 macOS 生产只读预检使用 shared schema-v35 数据库、8000 backend、4173 frontend 和 0600 runtime env，结果 `ready`、0 blocker、0 warning；EIA 正式来源凭据仅按变量存在性检查，未输出值，SunSirs 不需要凭据。发布门禁分层判定：标签合同/采集链不可变发布为 **GO WITH WARNINGS**，警告是首次生产 revision 尚待部署后形成；七品种正式预测仍为 **NO-GO / INCOMPLETE**，因为真实 OOS 仍是 0/21。操作者已接受来源与方法时效风险并授权本次发布，但没有授权降低 OOS 门槛或虚报 100 分。
- 主实现提交 `161a70a354c51347fb594a964640b4a78fb9a5c5` 已推送至 `origin/codex/project-correction-100-v34`。首次生产写入前已创建一致性备份 `shared/data/backups/pre-sunsirs-161a70a.sqlite3`：479,895,552 bytes，SHA-256 `ff969dd2c8e9077032cc2f649cd3f5e6206e7d57f87fe58b6ee057b207d61eac`。不可变 release `20260901T115542Z-7d247784a3e1ff48` 已原子切换，`git_sha=161a70a...`、`source_tree_dirty=false`，回滚目标为 `20260901T092324Z-7e3ae41e4c0d8ef9`。重启瞬间的第一次 ready 请求返回 502；当前进程完成冷启动后 backend/frontend 均为 200，未触发回滚。
- 后台调度于 `2026-09-01T11:56:45.331405Z` 先于人工触发完成首次 SunSirs 生产捕获：业务日 `2026-09-01`、值 `5930.00 CNY/mt`、revision `472dcbfc-b9aa-4373-a3ed-836253cb0468`、原始证据 SHA-256 `a5fa8460629dcbc09c6ccac0f03e8592094e7ffc44effae68baf91cdf4bb0702`、合同 `seven-product-labels.v3`、解析器 `sunsirs-meg-public-page.v2`。随后人工 MEG collect 无错误；数据库仍只有一条同业务日/同哈希 SunSirs revision，证明精确重放未制造第二条标签修订。生产库 `integrity_check=ok`。
- 首捕后的显式诊断预览完整生成 21 格且统一使用 `seven-product-labels.v3`；MEG D1/D7/D30 均读取上述专属序列、`history_points=1`、`source_matches_label=true`、`formal_status=insufficient_data`，没有读取同时采集的新浪 EG0 特征作为标签。OOS 评测合同为 `seven-product-evaluation.v2` / `seven-product-oos-gate.v3`，MEG 三格泄漏检查通过但有效样本仍为 0，整体仍为 0/21。
- 首捕后再次执行生产 lifecycle 得到 `ready_with_warnings`、0 blocker，仍返回既有同日不可变批次 `seven-6334b9625ed5c72c6482f504`，21 格与 payload SHA-256 `f2357e60811647467f726ddc7e5c6cfdf6a6dd1d0524b1e53c91ff189bc1e92f` 未被回写；报告 SHA-256 `6275518ccda3951e60d0dce702fc5e77da801b38150b50bb6a416665f573ee49`。新标签会从下一业务日的新批次开始进入真实预测账本，不能改写 2026-09-01 已发出的旧合同批次。
- 发布后 Keychain 鉴权公网 smoke 全部通过：匿名 release 为 303、匿名 API 为 401，鉴权后的 release/live/ready/deep、delivery、market chain、event library、RAG、agent runs 和七模块均为 200；命中 release id/hash `20260901T115542Z-7d247784a3e1ff48` / `7d247784a3e1ff48`，语义索引 ready、9,694 vectors。
- 发布判断由 **GO WITH WARNINGS** 收敛为：SunSirs 标签合同、生产采集、幂等、发布与公网运行闭环 **GO**；正式七品种预测仍为 **NO-GO / INCOMPLETE**。评分表没有给“来源已选”与“持续点时历史”设置可独立拆分的小数权重，且后者及 18 分 OOS 均未完成，所以不主观补分，严格评分暂保持 **79.8/100**。当前已不再存在 MEG 来源选择或凭据人工缺口；剩余 20.2 分只能由七标签自然累积真实可见历史，并使 21 格各自达到至少 20 个有效 OOS、相对最佳朴素基线改善 5% 和方向准确率 55% 后按冻结表回收。

### 12.19 自然日期限、跨合同失效审计与隔离历史模拟（2026-09-01，未发布）

- 生产结算原先把 D1/D7/D30 解释成“第 1/7/30 条后续观测”，与 OOS 评测的自然日合同不一致。当前分支已统一为：原点业务日期加 1/7/30 个自然日，再选择不早于目标日的首个后续可见观测。结算同时绑定预测签发时的标签版本、语义序列、来源和单位，不再用结算时的当前注册表解释旧预测。
- 生产 v35 只读审计确认已有一条 MEG D1 结果跨越冻结合同：签发单元为 `seven-product-labels.v1` / `meg.dce.main_continuous.settlement.cny_mt`，实际值却来自 `seven-product-labels.v3` / `meg.sunsirs.china.spot_assessment.cny_mt`。它不是合法的 formal OOS，也不应继续显示为 scored。schema v36 因此只新增 append-only invalidation 表和不可变触发器，不更新、不删除旧 outcome；在隔离生产副本迁移中精确追加 1 条 `contract_mismatch`，API 状态为 `invalidated_contract_mismatch`，UI 不再把其实际值、误差或方向命中计入已结算展示。
- 本轮历史实验永久标记 `SIMULATION_ONLY`。输入来自生产库的 SQLite 只读在线备份，备份副本在 `/private/tmp` 迁移至 v36 后运行；生产源只以 `mode=ro/query_only` 打开，生产写入为 0。活跃调度使源主库哈希在备份期间和运行后变化，报告如实记录该外部事实；一致性门禁以 SQLite 在线备份、目标 `quick_check=ok` 和隔离路径为准，不把生产调度正常写入误报为实验写入。
- 完整实验使用历史真实值，不生成或重采样价格；使用来源特定的保守伪可见性，只用于研究回放，不冒充正式 OOS。合同分母固定 21/21：全历史可评估 9 格（Brent、POY、DTY 各三个期限），12 格因当前精确标签历史不足诚实标为 `not_testable`；PX/PTA/MEG/石脑油 proxy 共 12 格只作 `source_mismatched` 负对照，全部排除于效果分母。
- 完整运行包含 21 条边界窗、63 条 120 点/步长 20 滚动窗、30 个固定种子共 630 条缺失/延迟/修订可见性压力记录，以及一次冻结的 15 项故障矩阵。运行用时 110.516 秒；确定性复跑正文 SHA-256 均为 `ca9017c9496161110bdffe17e9bea5240be711ab82192e3d3c97c1fcb5731812`，故障矩阵 15/15 通过。内容寻址报告为 `.codex-run/seven-product-simulation/full/seven-product-simulation-ca9017c949616111.json`，manifest 为 `.codex-run/seven-product-simulation/full/simulation-manifest-de3aa9c38d1a8ce2.json`；对完成检查点执行 `--resume` 返回 `already_complete` 并复核报告哈希，不重复计算或写入。
- 研究性能结论为 9 个可评估格中 0 格同时通过两项门槛。Brent D1/D7/D30 的相对最佳朴素基线 MAE 改善分别约为 -0.16%/-0.28%/-2.39%，方向准确率约 30.33%/41.06%/33.68%；POY/DTY 六格方向准确率达到 88.71%-100%，但候选与最佳朴素基线 MAE 改善均为 0%，因此仍不通过 5% 门槛。协议禁止自动调参、换模型或降低阈值；该结果不晋级、不改变生产模型注册表。
- 最终验证：迁移/旧库专项 125/125；补充 invalidation 列投影深审计后，精确最终工作树的后端全量 1779/1779（611.83 秒）；前端 TypeScript、资产和生产构建通过，Ruff 与 `git diff --check` 通过。完整实验 manifest 明确 `formal_score_changed=false`、`formal_oos_changed=false`；严格评分仍为 **79.8/100**、正式 OOS 仍为 **0/21**。
- 本节代码与文档尚未 commit、push、部署，生产库仍为 schema v35，当前 release 仍为 `20260901T115542Z-7d247784a3e1ff48`。进入生产前必须另做 release readiness review，并获得新的提交、推送、部署和生产 v35→v36 迁移授权。

### 12.20 透明模型优化与上游特征证伪（2026-09-02，未发布）

- POY/DTY 来源中存在一条周六发布记录，因此把它们声明为 `business_day` 会让真实来源行为与合同矛盾。当前分支将标签注册表升级为 `seven-product-labels.v4`，仅把 POY/DTY 频率元数据修正为 `published_day`；原油、石脑油、PX、PTA、MEG 仍为 `business_day`，v1-v3 的来源/序列/单位身份保持冻结，旧预测不被重解释。
- 优化协议固定 21 格分母、自然日 D1/D7/D30、最终 50% 扩展窗、过去可见的内层选择、至少 20 个独立实际值、相对最佳朴素基线 MAE 改善 5% 与 MAD 方向准确率 55%。候选为 12 个透明单变量模型和一个闭式 NumPy Ridge 上游控制；不新增依赖、不生成价格、不降低门槛，batch-backfilled proxy 永久标注为伪可见研究特征。
- 单变量嵌套选择与 12 候选固定消融均为 0/9 通过。异常检测 v1 因把原油持续价格台阶误判为 51 个异常而被拒绝；只改为“相邻已接受发布跳变大于 35%”的 v2 后，原油误报降为 0，同时保留 POY 两个孤立来源异常。修复不改变原始存储，异常敏感性轨也不得成为晋级证据。
- 全量上游 Ridge 原始轨 0/6 通过。固定累积消融依次比较目标自身、加入原油、石脑油、PX、PTA、MEG，共 36 个单元，仍无一同时过线。POY 目标自身 D1/D7/D30 的 MAE 改善为 -135.62%/-90.74%/-64.17%，全上游为 -509.95%/-131.20%/-62.14%；DTY 目标自身为 -13.18%/-38.02%/-69.75%，全上游为 -28.00%/-13.71%/-65.56%。
- POY 两个公开异常值会放大短周期误差，但不是唯一根因：隔离敏感性把全模型 POY D1/D7/D30 改善收敛到 -22.49%/-11.38%/-50.17%，仍全部输给持平基线，最佳方向准确率 54.10% 也未达 55%；DTY 无隔离行且结果不变。低维目标自身模型同样失败，上游增量又不稳定，故当前证据支持“代理特征尚无可验证预测信号”，不支持继续调惩罚或引入黑盒。
- 诊断使用同一隔离数据库副本，输入 SHA-256 `cd89bfd64ed0c202cce68431f72869687a9840f68530d54a94a073905c32e266`。格式化后的精确工作树耗时 374.857 秒；报告 `.codex-run/seven-product-simulation/optimization-ablation-exact/seven-product-simulation-146b3e9b14b4f474.json`，正文 SHA-256 `146b3e9b14b4f474db1892b3db55a164a2e0eee5246267855bed623bc1db1de7`。manifest 为 `.codex-run/seven-product-simulation/optimization-ablation-exact/simulation-manifest-3c2ac547ccbbe315.json`，证明 `SIMULATION_ONLY`、生产写入 0、源库运行前后哈希不变、隔离库 `integrity=ok`。与前两轮逐项比较，原始轨、36 个消融单元与 6 个敏感性单元的指标数组字节级一致。结果为 0 个生产候选、正式晋级 0、正式 OOS 仍 0/21。
- 按预注册停止规则关闭单变量与 proxy-Ridge 两条历史优化分支；只有在真实点时上游历史积累后，或出现另行预注册、具业务因果假设的透明特征时才重开。未使用的实验预算不为追求过线而消耗。当前仍没有证据提高 **79.8/100**，也没有对生产模型注册表、生产库或 release 做任何写入。
- 精确工作树验证：七产品/结算/API/迁移定向 61/61，API 与公开基准 223/223，后端全量除沙箱禁止 `/bin/ps` 的同一组 8 项外为 1779/1779；该治理文件在正常系统权限下 9/9 通过。完整 Playwright 单 worker 77/77（5.0 分钟），TypeScript、资产、生产构建、Ruff 与 `git diff --check` 通过。以上只证明代码和治理机制就绪，不把 0/21 正式 OOS 变成模型完成度。

### 12.21 产业链专属非负传导假设证伪（2026-09-02，未发布）

- 上一轮全量 Ridge 同时混入多条相关上游序列，却没有把 POY 作为 DTY 的直接上游输入。本轮另行预注册一个更窄的业务因果假设：POY 只使用 Phase-A 已冻结的 `0.855×PTA + 0.335×MEG` 原料篮子，DTY 只使用精确 POY 标签；D1/D7/D30 各使用期限匹配的原点可见收益。
- 候选为零截距、非负单系数闭式 Ridge。零系数严格退化为 persistence，不能靠独立截距伪装原料贡献；惩罚仍固定为 `100/10/1/0.1` 并在过去可见的内层 walk-forward 中选择。成熟的 statsmodels、MLForecast 和 skforecast 均会增加 pandas/scikit 等依赖，也不原生执行本项目的双时间合同，因此继续复用 NumPy；没有新增依赖。
- 六格全部可评估但 0 格通过、0 个生产候选。POY D1/D7/D30 的 MAE 改善为 -55.06%/0.00%/0.05%，方向准确率 33.87%/25.86%/4.65%；DTY 为 -0.13%/0.19%/-1.05%，方向准确率 64.52%/55.17%/20.93%。DTY D7 虽刚过方向门槛，但 0.19% MAE 改善远低于 5%，不能晋级。
- 两次聚焦复跑正文 SHA-256 均为 `691fe7ebb8d37e6b8137ed60f5ed69952988d921ba97e3b69777e3fd8ec78f68`。统一完整模拟耗时 420.933 秒，21 格分母、9 格可测试、12 格不可测试与 0 格通过均不变；报告 `.codex-run/seven-product-simulation/chain-pass-through-exact/seven-product-simulation-8ccc7d87f7bb2203.json`，正文 SHA-256 `8ccc7d87f7bb22034eaadc3ec38219382f7e27a8573ca575a51a6b9a5a9dbf09`。manifest `.codex-run/seven-product-simulation/chain-pass-through-exact/simulation-manifest-0c553c64da945978.json` 证明源库 SHA-256 前后均为 `cd89bfd64ed0c202cce68431f72869687a9840f68530d54a94a073905c32e266`、隔离库 `integrity=ok`、生产写入 0、正式分数与 OOS 未变。
- 按预注册停止规则关闭该分支，不再在同一快照上调原料权重、允许负系数、搜索滞后、增加截距或降低门槛。它补充了“直接产业链遗漏不是此前失败主因”的否决证据，但不提高严格 **79.8/100**，正式 OOS 仍为 **0/21**。
- 精确当前工作树验证：新模拟定向 13/13，模拟/结算/标签/工作台相关回归 52/52；后端全量除沙箱禁止 `/bin/ps` 的同一治理组 8 项外为 1782 项通过，该治理文件在正常 macOS 权限下 9/9 通过。TypeScript、11 项资产检查与 Vite 生产构建、Ruff、`git diff --check` 均通过。该组合证据如实保留两种运行环境，不表述为一次单进程 1790/1790。

### 12.22 外部调度器指标闭环（2026-09-02，未发布）

- 生产只读审计确认 `com.poydty.agent.local-daily` 的 09:30 日任务及五分钟补跑机制正常，当前检查时刻尚未进入 2026-09-02 的运行窗口；9 月 1 日成功戳存在，launchd 最近退出码为 0。新闻调度器仍在运行。没有理由提前触发日任务或制造尚未成熟的 D1 结果。
- 同次审计发现 `/metrics` 只暴露进程内 `intraday_price`，没有 launchd 管理的 `local_daily` 与 `news_scheduler` 序列，违反本项目“每次尝试均更新 attempt/status/duration，失败不推进 last_success”的可观测合同。该缺口会把任务失败与“没有遥测”混为一谈。
- 当前分支新增 `scheduler_observability.v1` 原子状态投影：固定状态枚举，持久保存最后尝试、最后成功、耗时、积压及按终态划分的累计失败数。每日任务的 blocker 数作为积压；新闻任务按公开抓取错误与新闻失败/超时/摘要失败的非重复上界计算积压。`ready/ready_with_warnings/success/completed` 才推进最后成功；`degraded/blocked` 等状态不得推进。
- backend wrapper 只传入两个明确的非秘密 JSON 路径；scrape 时只读解析并覆盖内存快照，不扫描目录、不读取 stdout/stderr，也不会因重复 scrape 重复增加失败计数。缺失或损坏状态文件不伪造成功，指标保持缺失或陈旧以触发监控。
- Prometheus node_exporter 的 textfile collector/原子交接模式已作复用评估；现有 JSON 和自定义 exporter 足以满足相同进程边界，不新增依赖。代码尚未 commit、push、部署，当前生产 release 与 schema-v35 数据库均未改变；因此生产 `/metrics` 仍不会出现这两条序列，必须在后续 release readiness review 与发布后 smoke 中验收。
- 定向与相邻回归为 48/48。DG01 后端全量为 1786 项通过、8 项失败；8 项均来自同一个治理证据文件，失败堆栈全部是沙箱拒绝 `/bin/ps`，无产品断言失败；该文件在正常 macOS 权限下 9/9 通过。Ruff 与 `git diff --check` 通过。该修复提高运行可诊断性但不产生任何价格历史或正式 OOS，严格评分仍为 **79.8/100**，正式 OOS 仍为 **0/21**。

### 12.23 v36/v4 合并候选只读发布审查（2026-09-02）

- 当前判断为 **NO-GO**。审查对象仍是分支 `codex/project-correction-100-v34`、HEAD `df35abc38a13714215053e63ba24baa39327ce4f` 上的未提交工作树，共 34 个已修改/未跟踪文件、999 行新增和 122 行删除；tracked binary diff 的 SHA-256 为 `435770c3891534cd7e157e63474e4f2cc1ee6c7ca411233924b3e97749c6e5e5`。它不是不可变提交或可追溯 release artifact，不能直接发布。
- 新鲜机械证据均通过：前端 TypeScript/11 项资产/Vite 生产构建；完整 Playwright 77/77；桌面视觉 0.13% perceptual、移动视觉 0.27%，均 PASS；Ruff 与 `git diff --check`；15/15 冻结故障矩阵；AI 离线评测 21/21、RAG 9/9；Semgrep、Gitleaks、Trivy、`npm audit --omit=dev` 和 `pip-audit` 均为 0 已知问题。后端全量在 DG01 中为 1786 项通过、8 项因沙箱拒绝 `/bin/ps`，同一治理文件在正常 macOS 权限下 9/9 通过。
- 当前生产旧版本的只读预检为 `ready`、0 blocker、0 warning；数据库 `integrity=ok`、前后端健康。脱敏报告 `/private/tmp/release-preflight-v36-v4-20260902/latest-preflight.json`，SHA-256 `44997091e601cd3c3fecc725b53f3771a44ad8635c1d835b5b9e03370d0b4a15`，11,766 bytes。该证据只证明现行 schema-v35/release 健康，不证明脏工作树候选已发布就绪。
- 阻断顺序冻结为：先形成精确 commit/artifact 并对其复核；再停止写入、创建并完整性校验新鲜 v35 备份；执行 v35→v36；部署后验收 `local_daily` 与 `news_scheduler` 新指标以及 API/公网 smoke。v36 数据库会被旧 v35 程序以 `schema_version_newer_than_supported` 拒绝，因此回滚必须停掉全部 writer、恢复迁移前 v35 备份并完成只读 smoke，不能只切换 `current/previous` 软链接。
- 该 NO-GO 是发布制品/迁移/发布后证据不足，不推翻已通过的实验与代码门禁。即使上述运维门禁全部闭合，正式预测资格仍因 **0/21** 真实 OOS 而保持 NO-GO；模拟、历史 proxy 或参考冠军不得替代这一门槛，严格评分仍为 **79.8/100**。

### 12.24 逐格参考冠军与运行时回退闭环（2026-09-02，未发布）

- 用户确认“通过历史门禁的 proxy-history 单元可以成为生产参考冠军”，但正式资格仍由真实 OOS 独立控制。原实现只在研究报告里写了 `production_champion_candidate_allowed=true`，生产预测始终硬编码 `robust-drift-reference.v1`，所以“允许、特征门禁、三连败回退”尚未形成真实签发约束。
- 模型注册表升级为 `seven-product-model-registry.v2`，明确分开 `champions`（正式）与 `reference_champions`（参考）。参考批准必须同时满足：研究报告正文 SHA-256 可复算、精确产品×期限×模型列在 `promotion_candidates`、候选运行时已实现、操作者显式逐格批准。参考晋级历史和回退目标均生成新内容寻址 revision；任何参考冠军的状态上限固定为 `reference`，不会写入正式冠军格。
- 每日日跑在签发新批次前从不可变账本投影最近有效 outcome；本段当时按签发时冻结的 `model_version` 比较模型绝对误差与 `persistence.v1`。失效 outcome、其他模型 outcome、非有限或不完整误差均不计数；最新三个可比较结果连续严格输给 persistence 时使用已记录回退模型。缺少、陈旧或来源不匹配的必需特征也在签发前回退。选择结果、原因和 streak 进入 configuration hash，回退单元的 `model_version`、`key_drivers` 与 `data_gaps` 可在 API/UI/导出审计。该 persistence-only 运行时比较已由 **12.29** 的 persistence + lag-5 seasonal 最佳朴素基线合同取代。
- 当前只实现了既有 robust-drift 与不可变 persistence 基线；未实现的研究模型不能批准。默认注册表仍为 0 个正式冠军、0 个参考冠军，因此本次代码不会借“允许”自动激活任何失败候选，也不会改变当前 21 格数值。现有三轮历史实验仍为 0 个候选通过，正式 OOS 仍为 0/21。
- 复用核查比较了 MLflow alias/registry 与 Evidently 监控；两者会为当前单人工作台增加服务端注册表、追踪或数据框依赖，而现有版本化 JSON 注册表与 append-only 账本已覆盖所需身份、历史和回退证据，因此不新增依赖。
- 验证证据：治理/API/账本定向 20/20；预测、生命周期、API、模拟、工作台相邻集 278/278；全新 DG01 且正常 macOS 进程权限下完整后端 **1797/1797**（709.70 秒）；TypeScript、11 项资产与 Vite 生产构建通过；精确工作树完整 Playwright **77/77**（6.0 分钟）；Ruff 与 `git diff --check` 通过。Playwright 首次在沙箱中因无法访问 `~/.cache/uv` 而未启动服务器，正常权限复跑全绿，未把环境启动失败表述为产品通过。
- 本节仍未 commit、push、部署或写生产库，不关闭 12.23 的制品/迁移/发布后 smoke 门禁，不提高严格 **79.8/100**。

### 12.25 正式冠军签发真实性与运行优先级闭环（2026-09-02，未发布）

- 延续审计发现，原正式晋级函数只读取传入对象中的 `report_sha256`，没有像参考晋级一样重新计算正文身份；同时预测签发总是先执行参考运行选择器，未来即使存在正式冠军，参考特征/连败回退也可能改变实际模型。这会让“评估通过→显式晋级→生产签发”在 21/21 达成后仍不可靠。
- 正式晋级现在重新计算 `seven-product-evaluation.v2` 正文 SHA-256，校验派生 evaluation ID、21 格唯一性、passed count/overall status、逐格 gate 一致性和冻结的 v3 5%/55%/20 样本阈值；没有真实运行实现的候选直接拒绝。篡改指标但沿用旧哈希无法晋级。
- 运行选择顺序改为 formal 优先、reference 次之。正式冠军缺少必需特征或最近三个有效结算连续严格输给 persistence 时，只选择已记录回滚目标（无前任则 `persistence.v1`），并立即失去正式资格；失效 outcome 不计入连败，回退模型不能继承被替换冠军的审批。该 persistence-only 历史条款已由 **12.29** 的最佳朴素基线反超门禁取代，回滚目标本身仍按注册表冻结，不因当期朴素基线胜出者自动改变。
- 新增 `run_seven_product_model_governance.py`。它只读取内容寻址评估证据和当前注册表，要求逐格 `--cell`、actor、reason 与显式 `--approve`，输出 0600 原子内容寻址提案和精确候选注册表，并复核输入注册表运行前后不变；没有 mutating API，也不会直接改当前 release。晋级/回滚仍需把精确候选注册表纳入新 commit、复核和部署。
- 复用核查再次比较 MLflow champion alias 与 Evidently 监控；对当前单用户工作台，它们会引入额外服务或 dataframe/追踪依赖，现有版本化 JSON 注册表、内容寻址证据与 append-only 账本已覆盖本切片，因此未新增依赖。
- 定向治理/CLI 11/11、评测/账本/生命周期/API/工作台相邻回归 61/61、正常 macOS 进程权限下全量后端 **1802/1802**（767.25 秒）以及完整 Playwright **77/77**（6.6 分钟）通过；TypeScript、11 项资产、Vite 生产构建、Ruff 与 `git diff --check` 全绿。用当前真实 0/21 内容寻址证据尝试晋级 `crude:D1`，CLI 正确返回 `evaluation_gate_failed:crude:1`，且没有创建提案目录。该修复只保证未来通过的正式证据能被安全签发，不创造样本或绩效，严格评分仍为 **79.8/100**、正式 OOS 仍为 **0/21**。

### 12.26 正式冠军治理后发布就绪复核（2026-09-02，只读）

- 决定仍为 **NO-GO**。审查对象是分支 `codex/project-correction-100-v34`、HEAD `df35abc38a13714215053e63ba24baa39327ce4f` 上的未提交候选；当前共有 41 个已修改跟踪文件和 9 个未跟踪源码文件。工作树不是不可变 commit 或 release artifact，既有 v34 发布授权不能推定为本次 v36 发布授权。
- 当前候选的机械证据通过：后端全量 **1802/1802**、Playwright **77/77**、TypeScript/11 项资产/Vite 生产构建、Ruff 与 `git diff --check`；故障矩阵 **15/15**；AI 离线评测 **21/21**、RAG **9/9**；视觉 desktop `0.13% perceptual / 1.57% raw`、mobile `0.39% / 2.52%` 均 PASS。RAG 运行提示当前模型在 FastEmbed 0.7.4 使用 mean pooling；依赖已精确锁定为 `fastembed==0.7.4`，索引身份也冻结为 `fastembed-0.7.4-mean-pooling`，因此这是与当前合同一致的上游迁移提示，不是未声明的运行漂移。不得仅为消除提示而降级到 0.5.1 或隐藏 warning。
- 正常 macOS 权限下安全门禁通过：Semgrep 对 269 个适用跟踪文件运行 3 条项目规则，0 finding；Gitleaks 扫描 58 commits/约 9.77 MB，0 leak；Trivy 对 npm/uv 漏洞、Docker 配置、secret 和 license 均 0 发现；`npm audit --omit=dev` 与 `pip-audit` 均为 0 已知漏洞。沙箱内 Semgrep 首次因系统 CA trust store 为空退出 2，AI/RAG 首次因无权读取 `~/.cache/uv` 退出 2；两者均在正常权限下复跑通过，未把环境失败冒充产品通过。
- 当前生产只读预检在真实端口 backend `8000`、frontend `4173` 上为 `ready`、0 blocker、0 warning；脱敏报告 `/private/tmp/release-preflight-post-governance-20260902-v2/latest-preflight.json`，SHA-256 `03c6bb4b20a27f13eb1e41423d56be8b8874bf2cff62a8aceac4ab688e7da3de`，11,766 bytes。脚本默认的本地开发端口 `5173` 曾产生一次可解释的假阻断，不计为生产故障；公开生产手册已改为显式传入 `8000/4173` 和 `--require-services`，不改变本地默认值。
- 生产仍指向不可变 release `20260901T115542Z-7d247784a3e1ff48`（git SHA `161a70a354c51347fb594a964640b4a78fb9a5c5`），数据库仍为 schema v35 且 `integrity_check=ok`。生产 `/metrics` 只有 `intraday_price` 调度序列，没有未部署候选新增的 `local_daily`/`news_scheduler` 序列；两者必须在发布后 smoke 中出现并符合“失败不推进 last success”合同。
- 已有隔离生产副本再次只读核验为 v36、`integrity_check=ok`、精确 1 条跨合同 outcome invalidation，其迁移前备份为 v35 且完整性正常。本次尝试从持续写入的生产库制作更新在线副本未在评审窗口内完成，已中止且没有写生产；它不能替代正式发布前停止全部 writer 后创建并校验的新鲜 v35 备份。
- 解除运行发布 NO-GO 的顺序不变：形成精确 commit/artifact 并对该身份复核；获得本次 v36 提交、推送、部署和生产迁移授权；停止 writer、创建新鲜 v35 备份并演练可恢复性；迁移至 v36；完成 API/Keychain 公网 smoke、21 格首批签发、outcome invalidation 投影和两类调度器指标验收。回滚必须先停 writer、恢复 v35 备份再切旧 release，不能只切软链接。
- 该发布 NO-GO 不否定代码门禁，但正式预测完成度仍独立为 **NO-GO / INCOMPLETE**：真实正式 OOS 仍是 **0/21**，严格评分仍为 **79.8/100**。参考冠军、历史模拟和迁移演练均不得替代 21 格各自的真实 OOS 门槛。

### 12.27 v36 生产迁移与发布结果（2026-09-02）

- 经用户对本次 v36 明确授权，精确提交 `347c497a14c14a4298c064bcf9da2f93321c39dc` 已推送；不可变 release 为 `20260902T021900Z-50dffdcbfbb3d9f2`，当前分支与远端同步且发布后工作树 clean。
- writer 停止后创建并校验的新鲜 v35 回滚备份为 `/path/to/user/Library/Application Support/POY-DTY-Agent/shared/data/backups/pre-v36-347c497-20260902T021625Z.sqlite3`，491,839,488 bytes、0600、SHA-256 `09b114874097a8f8133776855e04ec28b3ea5b52b16317983219f372b9c92899`、`integrity_check=ok`。生产库随后迁移到 v36；迁移后 `integrity_check=ok`，自动迁移备份同时保留。
- v36 迁移精确识别并保留 1 条旧 MEG 跨标签合同结算，将其当前投影改为 `invalidated_contract_mismatch`；原 outcome 不重写，实际值和误差不再进入计分展示或连续败给基线的治理计数。
- 发布后本地服务、鉴权边界、live/ready/deep、交付、行情、事件、RAG、Agent runs 与七个前端模块的公网 HTTPS smoke 全部通过；活动语义索引为 9,694 vectors。`/metrics` 已出现 `local_daily` 与 `news_scheduler` 两类外部调度器序列，失败不推进 last success 的合同得到 E1 验收。
- 2026-09-02 自然 09:30 批次已冻结 21 格，`formal=0`、`reference=15`、`unavailable=6`；它发生在 v36 部署之前，因此不能冒充 v36 的首次自然日跑。v36 的一次确定性新闻 `--once` 运行退出 0，但 UKMTO/IEA/GDELT/中石化 4 个非关键来源故障保持隔离和显式 degraded。
- 以上关闭 12.26 的 v36 制品、迁移和发布后 smoke 阻断，不改变预测性能事实：严格评分仍为 **79.8/100**，正式 OOS 仍为 **0/21**，终局仍是 `INCOMPLETE`。

### 12.28 日度 OOS 内容寻址证据闭环（2026-09-02，未发布）

- v36 上线后的差距复核发现，Mac 生产日任务会结算成熟 outcome 并冻结当天 21 格，但没有自动调用既有内容寻址 OOS 运行器；Docker/cloud 外层调度器虽另行调用，却会重建一个稍晚的预览批次，无法保证与刚签发的账本批次同一身份。自然样本因此不能在两条部署路径上形成一致的自动晋级证据。
- 当前工作树把评测收口到日任务内部并固定顺序为“来源更新 → 成熟结算 → 当日账本签发 → 只读 OOS 评测”。Apply 模式必须用刚签发的 `business_date` 读取精确不可变账本批次，dry-run 才允许 `point_in_time_preview`；Docker/cloud 外层调度器只消费嵌入的 `seven_product_oos_evaluation`，不再重复评测或覆盖账本绑定证据。
- 日任务只接受 fresh、正文 SHA-256 可复算、合同 21/21、pass/fail 数一致且数据库运行前后不变的证据。`exit=2 + status=blocked` 表示评测健康执行但不足 21/21，只产生诚实 warning；崩溃、旧 latest、非法 JSON、哈希/状态/格数不一致、apply 使用预览或数据库变化均 fail-closed 并阻断 readiness。证据目录固定为 0700、文件为 0600。
- 复用核查比较 MLflow champion alias/registry 与 Evidently 监控；两者适合更大规模的服务化注册表和漂移面板，但会为本单用户工作台引入额外服务、数据库或 dataframe 依赖。既有 JSON 注册表、不可变账本和内容寻址运行器已经覆盖本缺口，因此不新增依赖。
- 定向与相邻回归覆盖日任务、外层调度器、生命周期、评测、模型治理与 API，共 **41/41** 通过；Ruff、Python 编译和 `git diff --check` 通过。首次正常 macOS 权限全量回归为 **1806/1807**、覆盖率 82.31%，唯一失败是本机可用磁盘 3.6GiB 低于真实生产预检固定 5GiB 门槛；用户随后明确授权按既定保留策略清理三个 `.codex-run/local-production` 本地证据目录。清理永久删除 141 份旧 SQLite 备份和 52 个对应 sidecar，共 27,948,716,032 bytes；`db-backups`、`restore-drill`、`foundation/db-backups` 分别保留最新 **3/1/5** 份，磁盘可用空间增至 30GiB。Library/Application Support 下 v35 回滚备份仍存在且 SHA-256 保持 `09b114874097a8f8133776855e04ec28b3ea5b52b16317983219f372b9c92899`，当前 v36 release 目录仍存在。唯一失败项隔离复跑 **1/1** 通过，随后完整 DG01 后端回归 **1807/1807** 通过、覆盖率 **82.31%**、耗时 **1455.19s**。本节仍是未提交、未推送、未部署的 E2 修正；没有运行生产日任务或写生产库，也不提高严格 **79.8/100** / 正式 **0/21**。

### 12.29 最佳朴素基线运行时反超闭环（2026-09-02，未发布）

- 延续审计发现正式 OOS 门禁以 `min(persistence_mae, seasonal_mae)` 作为最佳朴素基线，但不可变账本只投影 persistence 误差，formal/reference 三连败回退也只比较 persistence。候选即使连续输给 lag-5 seasonal，只要胜过 persistence 就不会回退，因此“正式评测通过后持续监控最佳朴素基线反超”尚未闭合。
- 统一合同新增 `CURRENT_NAIVE_SEASONAL_LAG=5`，正式评测与日度签发共同引用。新签发单元保留最近 6 个原点证据，足以冻结 origin 与 lag-5 seasonal 观测；结算历史只从不可变签发 payload 投影模型、persistence、seasonal 和逐 outcome 最佳朴素绝对误差及胜出模型。seasonal 证据还会重新校验冻结来源、单位、URL、64 位原文哈希、issue-time 可见性与有限正值；任一不完整即不可比较。旧批次若只保留 3 点且无法证明 issue-time seasonal 值，同样不从当前数据库事后重建。
- formal/reference 运行时回退改为最近三个**可比较** outcome 连续严格输给 `min(persistence_error, seasonal_error)`；失效、其他模型、缺字段、非有限/负误差均不计数，平局或获胜立即中断 streak。该逐 outcome 规则是保守的在线退化探测；正式晋级仍只由冻结窗口级最佳基线 MAE、20 个有效 OOS、5% 改善和 55% 方向准确率决定，阈值与 21 格分母均未改变。实际回滚模型仍取注册表预记录目标，不自动切换成当期 seasonal 胜出者。
- `key_drivers` 追加 `runtime_model_best_naive_loss_streak`，未触发和已触发状态都能由现有 API/UI/导出审计；触发原因稳定为 `*_three_consecutive_losses_to_best_naive` 并进入 configuration hash。GitHub 复用核查比较了 sktime NaiveForecaster、MLflow registry 和 Evidently monitoring；它们会引入 dataframe/服务生命周期且不执行本项目双时间与内容寻址账本合同，因此继续复用现有 NumPy、证据数组和 JSON 注册表，不新增依赖。
- 验证证据：核心预测/账本/治理/评测 **39/39**，日任务/生命周期/API/模拟/工作台相邻回归 **150/150**，streak 呈现补充回归 **22/22**，负例、seasonal 胜出与谱系 fail-closed **18/18**；最终正常 macOS 权限完整 DG01 后端 **1807/1807**、覆盖率 **82.32%**、耗时 **1402.00s**。`npm run check`、11 项资产与 Vite 生产构建通过，完整 Playwright **77/77**（8.6 分钟），Ruff、Python 编译和 `git diff --check` 通过。冻结发布故障矩阵 **15/15**（28.00s），内容寻址报告 `.codex-run/release-fault-matrix/fault-matrix-817ee88b8fe4c4b1.json` 为 2,629 bytes、文件 SHA-256 `a54fb203239c250ff899e4ca5708ef40944dbc397b6886efdd166cafe1afe5e7`、正文 SHA-256 `817ee88b8fe4c4b13f934a264c10d40773ea7ddfaa26633ae3b55d8fec3b0519`；JUnit 为 2,292 bytes、SHA-256 `d8512a77997f2d05cd31705381e39a4eae60ab3eb5e327d384582aea390249ce`。本节未提交、未推送、未部署，不运行生产日任务、不写生产库、不晋级任何模型，因此严格评分仍为 **79.8/100**、正式 OOS 仍为 **0/21**。

### 12.30 本地来源自动化备份保留清理（2026-09-02）

- 经用户明确确认，只对 `.codex-run/local-production/source-automation/db-backups` 执行项目默认的跨前缀保留数 7；生产数据库、不可变 release、正式证据正文以及独立的 v35 回滚备份均不在清理范围。
- 清理前精确识别 21 份 `agent.db.pre_*.sqlite`，保留最新 7 份并删除其余 14 份及对应 sidecar，共释放 2,686,750,720 bytes。目录总量由约 5.3 GiB 降至 2.8 GiB，系统可用空间由 33 GiB 增至 36 GiB。
- 保留的 7 份 SQLite 均逐份通过 `PRAGMA integrity_check=ok`，目录没有遗留 `-wal/-shm/-journal`。独立 v35 回滚备份仍存在且 SHA-256 保持 `09b114874097a8f8133776855e04ec28b3ea5b52b16317983219f372b9c92899`。该运维清理不修改代码、生产库、模型或 OOS 证据，也不改变严格 **79.8/100** 与正式 **0/21**。

### 12.31 日度 OOS 证据同一截点闭环（2026-09-02，未发布）

- 延续 12.28 的只读审计发现，`--issued-business-date` 虽会把证据中的 forecast 换成不可变账本批次，但 evaluator 仍按调用时刻独立加载标签。调用方若传入稍后 `--as-of`，同一内容寻址包可以同时包含批次截点 A 与评测截点 B，却仍被日报当成“绑定账本”的健康证据；这不会制造 21/21，但破坏了 point-in-time 身份与可重跑语义。
- 证据合同升级为 `seven-product-evaluation-evidence.v2`。账本模式唯一允许的评测截点是该批次冻结的 `as_of_time`；省略 `--as-of` 时自动使用该值，显式传入不同值则以 `issued_forecast_cutoff_mismatch` 失败且不落证据。预览模式也在运行开始时只捕获一次截点，forecast 和 evaluation 共用它。
- 内容寻址正文现保存 `evaluation_cutoff_source`、`forecast_as_of_time`、`evaluation_as_of_time` 与 `cutoff_matches_forecast`。日报除复算正文哈希、21 格、状态和数据库不变外，还要求两截点精确相等；即使有人绕过首层 schema validator，readiness 与 Docker/cloud 外层调度器仍会因 `cutoff_matches_forecast=false` fail-closed。
- 同轮复核还发现 evidence runner 此前直接 `write_text` 后才 `chmod`，不符合文档已声明的“0600 原子证据”。现复用项目治理提案的安全写法：临时文件在写入前设为 0600，flush+fsync 后 `os.replace`，再 fsync 目录；先发布不可变内容寻址文件，再推进 `latest`。若第二步失败，旧 `latest` 保持完整；若截断哈希路径已存在不同字节，则以 `evaluation_evidence_address_collision` 拒绝覆盖。
- GitHub 复用核查比较了 MLflow immutable evaluation dataset 议题及通用 point-in-time/backtest 库；前者的 OSS 不可变版本仍是未落地功能，后者不能执行本项目 SQLite 不可变批次、双时间标签和现有内容哈希合同，因此不新增服务或 dataframe 依赖。原子交换失败与地址冲突负例 **2/2**、runner/日报/外层调度定向与七产品评测/账本/生命周期/治理/API/模拟相邻回归合计 **86/86** 通过。本节仍未 commit、push、部署或写生产库，不改变严格 **79.8/100** 与正式 **0/21**。

### 12.32 OOS 运行输入的 SQLite WAL 身份闭环（2026-09-02，未发布）

- 继续对照 E2“数据库运行前后不变”声明发现，runner 只哈希 `agent.db` 主文件。SQLite 官方 WAL 语义明确：已提交但尚未 checkpoint 的事务只存在于 `agent.db-wal`，主文件可以完全不变；`agent.db-shm` 则是读写进程共享的 wal-index/锁缓存，不是持久内容。旧证据因此可能漏报并发 WAL 写入，或若直接哈希 SHM 又会被自身读锁制造假变化。
- 新增 `sqlite-state-fingerprint.v1`：分别稳定读取主文件、WAL 与 rollback journal 的存在性、大小、mtime 和 SHA-256，读取前后 inode/size/mtime 不一致会有限重试后 fail-closed；联合内容 SHA-256 只由 main/WAL/journal 的持久字节身份计算，SHM 明确排除。评测前后比较完整指纹，任一持久工件变化均返回 `evaluation_input_database_changed_during_run`。
- evidence v2 的 `database` 现同时保存联合 `sha256`、`main_sha256`、fingerprint schema 与逐工件身份。日报会重新计算联合指纹而非只信任字段；即使重算外层 evidence hash，只改联合 SHA 也会收到 `evaluation_database_fingerprint_invalid`。WAL-only 变化、SHM 排除和伪造联合指纹负例均通过；runner/日报定向为 **23/23**，七产品评测/账本/生命周期/治理/API/模拟与调度相邻回归为 **88/88**。该修复不取得自然 OOS、不改变阈值、模型或生产数据，评分仍为 **79.8/100**、正式 **0/21**。

### 12.33 长运行 E2E 会话隔离与当前候选门禁（2026-09-02，未发布）

- 当前精确工作树的首次完整 Playwright 在正常权限下为 **76/77**（7.3 分钟）。唯一失败用例“支撑端点不可用时观察报告保持非正式”尚未进入业务断言，页面为空白；证据截图与网络日志显示广域 `**/api/v1/**` 5xx 模拟同时拦截了 `/auth/local-session`。前面的同类测试仅因会话绝对有效期尚未结束而偶然通过，完整套件运行足够久后才暴露测试隔离缺陷。
- 七个广域 API 故障模拟现统一透传 `/api/v1/auth/**`，只对其声明的业务支撑端点注入失败；没有延长 timeout、共享会话或降低断言。原失败用例单跑 **1/1** 通过并实测重新认证 200、支撑端点 503；随后单 worker 完整 Playwright **77/77** 通过（4.9 分钟），覆盖七品种 21 格、证据、门禁和导出同源。
- 当前候选的全新 DG01 后端全量在沙箱中为 **1806 passed / 8 failed**（869.50 秒）；8 个失败全部来自 `test_agent_governance_runtime_evidence.py` 且堆栈均为沙箱拒绝 `/bin/ps`，无产品断言失败。该文件在正常 macOS 权限下 **9/9** 通过（14.41 秒），故结果如实分层，不表述为一次单进程全绿。`npm run check` 的 TypeScript、11 项资产和 Vite 生产构建通过。
- 冻结故障矩阵在当前候选重新通过 **15/15**（19.69 秒）；报告 `/private/tmp/fault-matrix-cutoff.YhOGkH/fault-matrix-d6ebb15368168bda.json` 为 2,611 bytes、文件 SHA-256 `d46e26bb2a957b615bf8754dd09d246ec4b7fc9d946416bcb2da13ade63e374b`、正文 SHA-256 `d6ebb15368168bda6970a32dc01a4623c59a4cefff5623a4c06eac2185772482`；JUnit 为 2,292 bytes、SHA-256 `4fb0e427289eefb4977ce379fe2ec69475fe15e106614d24a4ea1251447ff538`。这些均为 E2，不代替未发布候选的 E1 首次日跑，更不改变 **79.8/100** 与 **0/21**。

### 12.34 post-v36 候选发布门禁复核（2026-09-02，只读）

- 发布决定仍为 **NO-GO**，但代码候选已经达到“可冻结并申请新一轮发布授权”的 E2 边界。精确对象为分支 `codex/project-correction-100-v34`、已部署且与远端同步的 v36 HEAD `347c497a14c14a4298c064bcf9da2f93321c39dc` 之上的 19 个跟踪修改文件。脏工作树不是不可变提交或 release artifact，因此不得直接部署；评审时精确差异统计与哈希保存在 gitignored 任务包中，避免被写入受测差异本身造成递归身份变化。
- 当前候选安全与供应链门禁通过：Semgrep 269 个适用跟踪文件、3 条项目规则、0 finding；Gitleaks 扫描 59 commits/约 10.02 MB、0 leak；Trivy 对 npm/uv 锁文件与 Dockerfile 报告 0 HIGH/CRITICAL 漏洞、0 配置、secret 和 license 发现；`npm audit --omit=dev` 为 0 vulnerability，`pip-audit` 为 0 已知漏洞。Trivy 首次刷新漏洞库后因默认超时中止，以相同配置仅延长至 15 分钟复跑通过；隔离快速评审首次在沙箱因空 CA trust store 中止，正常 macOS 权限复跑全部钩子通过且确认源码未被评审器改写。
- 既有当前候选机械证据保持：七产品相邻回归 88/88、冻结故障矩阵 15/15、TypeScript/资产/生产构建通过、完整 Playwright 77/77、Ruff/Python 编译/`git diff --check` 通过；后端全量仍按 1806 通过 + 8 项沙箱 `/bin/ps` 限制、同一治理文件正常权限 9/9 如实分层，不表述成一次单进程全绿。
- 解除发布 NO-GO 必须先获得本候选的**新**提交、推送、prepare/activate、生产 writer/数据库操作与公网 smoke 授权；此前 v36 授权已消耗，不能自动外延。形成不可变提交后还需对该精确 SHA 复核、部署并取得首个自然 E1 日跑。无论发布是否完成，正式预测资格仍独立为 **0/21**，严格评分仍为 **79.8/100**；只有每格达到冻结真实 OOS 门槛才可提高终局完成度。

### 12.35 公网七产品门禁、探针启动竞态与 Brent 新鲜度闭环（2026-09-02，未发布）

- 原公网 smoke 对七产品正式接口只执行通用 `200 + 非空`，无法识别 SPA/错误 JSON、21 格缺失或重复、forecast/evaluation/registry 汇总自相矛盾、账本 outcome/失效状态错配。当前候选把 forecast、最近三批 immutable history、evaluation 与 model registry 纳入鉴权公网门禁，逐项要求精确唯一 7×3、合同完整、计数与逐格状态一致；诚实的正式 OOS `0/21` 仍可通过运行 smoke，不被伪装成性能达标。
- 每次 CLI smoke 现先以 0600 临时文件写入、flush+fsync、原子替换并 fsync 目录，先发布不可变内容寻址正文，再推进 `public-smoke-latest.json`；目录固定 0700，截断地址已有不同字节时失败关闭。失败的结构化门禁也先留证再退出，证据不保存 session cookie、密码或 Keychain 内容。
- 生产 health probe 的 `RunAtLoad=true` 会与独立 Tunnel job 竞争，旧实现单次失败即退出 1。当前候选在单次 launchd 调用内执行最多 6 次、间隔 5 秒的有界重试，并把逐次结果写入 bounded latest；一旦成功立即停止，持续故障仍非零退出且触发原有失败转移通知。
- 生产只读证据显示 EIA 适配器按既定 3 小时频率连续成功，最近多轮均取得 1,480 条、无新增；官方 RBRTE capture 最新观测为 2026-08-25，首次可见为 2026-09-01。EIA 官方历史页在 2026-09-02 显示周度 release，而预测层却用 5 日阈值按 observation date 判断，导致新发布周批次仍被标 stale。Brent 新鲜度现与既有来源 observation SLA 对齐为 10 个日历日：第 10 天可用，第 11 天明确 stale；没有改写历史、前移 visible_at、替换标签源或降低 OOS 门禁。
- GitHub 复用核查比较了 `atomic-jsonwrite`、`safeatomic` 与 EIA Python wrapper；现有项目已有同等严格的临时文件、fsync、原子替换和目录 fsync 合同，引入外部依赖只会扩大供应链面，因此复用内部模式和标准库。用户明确排除“下一结果何时成熟、为何未结算、预计何时可评估”，本节没有实现相关面板或推算。
- 定向与相邻回归覆盖发布管理器、health probe、七产品预测/API、EIA fetcher、来源调度策略和公开来源适配器，共 **99/99** 通过；冻结发布故障矩阵 **15/15**，报告正文 SHA-256 `32819a10be8d93243cc50f5e9224c5d3365f09bd23701fc3d040fbed09320f41`，JUnit SHA-256 `e9d754a59641415cd2577f37bb7f8aec1c9aa92a9d237acd8a3d39f729e1692c`。精确候选完整后端分层回归为主套件 **1810/1810**（828.21 秒）加正常 macOS 权限治理证据 **9/9**（26.73 秒），合计 **1819/1819**；TypeScript、11 项资产、Vite 生产构建、Ruff、Python 编译和 `git diff --check` 均通过。一次候选仍在编辑时启动的旧全量运行已主动中止且不计证据。本节未 commit、push、部署、加载 launchd 或写生产库，不能作为公网 E1，也不改变严格 **79.8/100** 与正式 OOS **0/21**。

### 12.36 非自然日工程缺口 1–4 闭环（2026-09-02，未发布）

- 七产品单元的 `configuration_sha256` 现绑定版本化 `seven-product-runtime-policy.v1`，包括新鲜度、最小历史点、回看长度、收益裁剪、证据点、seasonal lag 及置信度公式参数。相同数据和截点下，任何这些输出策略变化都会改变单元配置哈希与批次 ID；不再允许 Brent 新鲜度或资格策略改变却复用旧批次身份。
- 公网 history smoke 不再只校验 `payload[0]`：`limit=3` 返回的每一批都必须逐批满足唯一 7×3、合同完整、汇总一致、非空且不重复的 batch ID、64 位 payload hash，以及 settlement/outcome/invalidation 对应关系；第二或第三批损坏同样失败关闭。
- smoke 证据升级为 `public-production-smoke-evidence.v2`。origin、凭据来源、匿名鉴权边界、登录和鉴权后检查任一阶段失败都会先写稳定的 `failed_stage/error_code` 内容寻址报告；非法 origin 不回显，登录异常正文、密码、Cookie 和 Keychain 值不落证据。
- public health probe 的 bounded latest 写入补齐 0700 目录、0600 临时/最终文件、flush、文件 fsync、同目录原子替换、目录 fsync 和异常临时文件清理；替换失败时旧 latest 保持完整。
- GitHub 复用核查比较 `atomic-json-io` 与 `safeatomic`；仓库现有标准库实现已覆盖所需持久化协议，且本任务还需要项目特有的内容寻址与脱敏阶段合同，因此不新增依赖。最小回归 **59/59**、七产品/公网生产相邻回归 **140/140**、Ruff、Python 编译和 `git diff --check` 均通过。该闭环未读取生产库或 Keychain、未运行自然日采集、未 commit/push/deploy，不改变严格 **79.8/100** 与正式 OOS **0/21**。

### 12.37 EIA 轮换要求移除（2026-09-03，操作者决定）

- 操作者确认 2026-09-01 出现在本地评审工具输出中的 EIA API key 不需要轮换；"必须在 EIA 侧轮换并更新本机秘密存储后才能发布"这一外部动作要求自本日起撤销。12.6、12.7 与 §12 表第 7 行中要求轮换 EIA key 的语句已移除，事件事实记录保留。
- 生产 `.env.production`、macOS Keychain 与生产库状态均不变；本次为文档与缺口台账修正，不修改代码、凭据或任何生产数据。严格评分保持 **79.8/100**，正式 OOS 仍为 **0/21**；发布门禁不再包含 EIA 轮换证明。

### 12.38 日跑快照评测修复、发布门禁纠偏与候选发布（2026-09-03）

- 当日生产日跑自 09:30 起进入约 4.5 小时 blocked 重试循环（`/metrics` 计 blocked 37 次），根因是日跑内只读 OOS 评测运行期间 intraday/news 调度器并发写库，触发 12.32 的 `evaluation_input_database_changed_during_run`；14:08 的一次重试自然通过，当日批次 `seven-303f3f9ed0b21dce8dd40443` 未被重复签发，日跑最终 `ready_with_warnings`、0 blocker，OOS 证据 exit=2 诚实呈现 0/21。这是运行稳定性缺陷而非数据完整性缺陷：全程只读核验 3 个批次 63 格 = scored 18、pending 30、unscoreable 14、invalidated 1，四张账本表 `_no_update/_no_delete` 触发器齐全。
- 修复（经操作者"全做"授权执行）：apply 模式现于批次冻结后创建一致性 SQLite 在线备份快照（0700 目录、0600 文件、按业务日期仅保留一份并连带清理旧件），评测器指向该快照，12.32 指纹守卫对快照依然全额生效；apply readiness 门禁新增要求 `evaluation_input_mode=consistent_snapshot_after_issue`，快照创建失败以 `evaluation_input_snapshot_failed:<类名>` fail-closed，不回退直播库评测。同轮一次性清理 runtime 备份目录 59 个孤儿 `-shm/-wal`（12.28 人工清理遗留；审计确认现行保留代码均经 `remove_sqlite_artifacts` 连带删除伴随文件，无需改代码）。
- 精确验证：修复前候选工作树全新受控根全量后端 **1829/1829**（718.30s）；修复后 **1832/1832**（942.17s）。中途一次复用旧受控根的全量出现 3 个失败且隔离复跑仍失败，确认为残留全局库污染，改用全新受控根后全绿——"每轮全量用全新受控根"纪律再次被证实必要。Ruff（server 配置）、`git diff --check` 与编译通过。
- 发布 1（提交 `5717474af796…`，release `20260903T101456Z-ab3a9e3b4bd55b4a`，`source_tree_dirty=false`，回滚目标 `20260902T072740Z-1f1b986d7d33b42b`）：本地 live/ready/deep 与七产品 API 正常，但鉴权公网 smoke 在 `authenticated_checks` 失败——history 深门禁把 12.19 冻结的"失效格保留不可变 outcome 供审计"投影误判为 `seven_product_settlement_outcome_invalid`。裁定为门禁公式过严（ledger 合同要求失效格同时携带 outcome 与 invalidation，`seven_product_forecast_ledger.py:674/683-693`），非数据损坏；按诚实规则该 release 判发布门禁未过并立即纠偏。
- 门禁修正为 `scored|invalidated ⟺ 携带 outcome`、`invalidated ⟺ 携带 invalidation`，新增 3 个回归（真实生产载荷通过、pending 带 outcome 拒绝、失效缺 outcome 拒绝），`test_manage_public_production.py` 37/37。
- 发布 2（提交 `b7660d31ee0a…`，release `20260903T103704Z-ab3a9e3b4bd55b4a`，clean，回滚目标 `20260903T101456Z-ab3a9e3b4bd55b4a`）：重启后本地 live/ready/deep 200、七产品 API 返回同一冻结批次；鉴权公网 smoke 连续两轮 **23/23 通过**（证据正文 SHA-256 分别为 `cb52f56b51e9ab272714495a0f64b171bef93d43b55ac1220e187cc9fcaf58cb`、`0f8ebbea227dc12d1909dd60672a5031a462d4c40500e1d5f61571a55ff671cc`），均命中本 release id/hash；匿名边界 303/401，forecast/history/evaluation/registry 深门禁全过并诚实展示 0/21。期间一轮 smoke 因公网链路瞬时连接重置失败，连通性恢复后未改任何门禁即双轮通过。
- 结论分层：v1.25 候选（快照评测＋深门禁纠偏）发布与公网运行 **GO**；正式七品种预测仍 **NO-GO / INCOMPLETE**（真实 OOS 0/21），严格评分保持 **79.8/100**。待办：复核 2026-09-04 09:30 首个快照输入下的自然日跑 E1（首跑即 `ready_with_warnings`、0 blocker）后，§8 可交付即为完整达成。
- 发布后遗留项闭环：12.36 收紧的探针写盘合同要求输出目录 0700，而既有生产目录 `shared/public-health/` 为 0755，导致切换新 release 后探针连续 fail-closed（exit 1，约 40 分钟）且 5 分钟失败通知触发；已将该目录收紧为 0700，随后探针首轮即 `success`（`/healthz` 200、`/release.json` 303）并恢复 exit 0。此为既有目录的一次性权限迁移缺口，代码合同本身正确；未来同类收紧必须把既有生产目录的迁移纳入发布清单。

### 12.39 首个快照输入自然日跑复核与可诊断性加固（2026-09-04）

- 2026-09-04 自然日跑为首个在快照输入合同下运行的生产日跑：当日批次 `seven-f6d12365cb36d07f2c27349e` 于 10:41（as_of `02:41:41Z`）**仅冻结一次**，此后全部 attempt 均为 `stored_or_exact_replay` 且 payload SHA 一致（幂等合同完好）；评测证据 `evaluation_input_mode=consistent_snapshot_after_issue`、`cutoff_matches_forecast=true`、指纹校验通过、exit=2 诚实呈现 0/21；结算幂等正确（09-04 实际可见的 2 个 D1 outcome 插入，其余待来源当日值可见后自然成熟）。最终一轮 13:44 完成 `ready_with_warnings`、0 blocker。
- 但"首跑即成功"判据未达：09:43–13:22 之间 8 次 attempt blocked，且因 `latest-status.json` 每轮被覆盖，逐次 blocker 文本不可追溯——这本身是被证实的可观测性缺口（12.38 的快照输入修复已消除原有"评测输入变化"竞态：当日所有已发布证据的指纹与截点校验全部有效，失败形态已改变）。
- 加固（经操作者"复核"授权执行）：① 快照创建加 bounded 重试（3 次、退避 10 秒），瞬态 busy/IO 失败不再消耗整个日跑重试轮；② 快照临时库无论失败或替换成功都连带清理 `-shm/-wal/-journal`（当日曾累积 9 对残留，已一次性清理）；③ 日跑每次尝试额外持久化内容寻址 `status-history/daily-status-<sha>.json`（0700 目录、0600 文件、保留 30 份、同内容去重），今后任何 blocked 重试循环都可逐次诊断。
- 精确验证：全新 DG01 受控根全量后端 **1837/1837**（323.48s）；Ruff、`git diff --check` 通过。提交 `57b660eb2075…` 推送后发布不可变 release `20260904T085912Z-ab3a9e3b4bd55b4a`（`source_tree_dirty=false`，回滚目标 `20260903T103704Z-ab3a9e3b4bd55b4a`），重启后鉴权公网 smoke **23/23 通过**（证据正文 SHA-256 `b97aefea1da3052bc3920ada6cf75880d9058017ef19ff7cd6561e0caf7e9aa1`）并命中该 release。
- 结论分层：发布与公网运行 **GO**；"首跑即 ready_with_warnings"退出判据自 2026-09-05 起在完整可诊断性基础上重新计日（需连续 3 个自然日）；正式预测仍 **NO-GO / INCOMPLETE**（0/21），严格评分保持 **79.8/100**。

### 12.40 工业情报中心 D1–D6 本地候选闭环（2026-09-05，未发布）

- 当前工作树已完成独立的 v37 工业情报候选：六张 append-only 业务表与可重建 FTS、59 个来源身份目录、旧新闻单向投影、确定性成簇与研判、08:20 cutoff/09:30 冻结日报、全鉴权 API、第八前端模块、USGS 与本地地图资产、运行审计/指标/deep health/本地 runner。生产仍是 schema v36；本节没有 commit、push、迁移、部署或生产 writer 操作。
- 地图链路补齐来源 geometry/region/location precision 传播、bbox 在 limit 前过滤、按 zoom 服务端网格聚合和 `cluster_count`；地图查询改为最小投影，避免读取无关大字段。详情链路修复重复 `evidence_count` 导致的真实 500，并将 `supply_chain_paths` 从 `Inference` 中分离为唯一合同投影；`Claim` 的可空来源谱系与 OpenAPI 同步冻结。
- 新增隔离性能基准脚本，固定 2,000 items、2,000 events、30 次预热后采样。p95 分别为 events **152.554 ms**、items **128.587 ms**、event detail **51.402 ms**、search **229.690 ms**、低 zoom map **141.355 ms**、高 zoom map **267.419 ms**，全部通过 500/800 ms 预算。报告 `/private/tmp/intelligence-benchmark-final.MGwzvV/report.json` 为 0600，SHA-256 `ac4c9f38643ce2122edf561b774952f86a41f506fae1891b0c7fc7333fa4ce33`；它是本机临时证据，形成 release 时必须对精确 commit 重跑并归档。
- 新鲜门禁：全新 DG01 后端 **1900/1900**（1077.76 秒，coverage **82.88%**）；完整 Playwright **79/79**；TypeScript、11 项资产、Vite 生产构建、Ruff、Python 编译和 diff whitespace 均通过。Semgrep/Gitleaks/Trivy 0 finding，npm/pip audit 0 已知漏洞；AI **21/21**、RAG **9/9**、desktop/mobile 视觉门禁均通过。地图 lazy chunk 789.49 KB（gzip 211.62 KB）和 FastEmbed mean-pooling 提示保留为 P2，不构成 P0/P1。
- 状态裁决：工业情报 D1–D6 为 **Local Release Candidate / GO**；D7 仍需对精确候选另行授权并执行不可变提交、v36 备份、v37 迁移、部署、功能开关、发布后本地/公网 smoke 和首份准时非 `blocked` 日报；D8 仍需 20 个预注册业务日，不能由代码或模拟替代。预测轨评分与资格不因情报功能增加而变化，仍为严格 **79.8/100**、正式 OOS **0/21**。

### 12.41 工业情报中心 D7 生产部署（2026-09-06）

- 经操作者明确授权，D7 候选及上线期修正已形成提交链 `637d8fc`→`ec05f50`→`47e938d`→`688e107`→`2955396`→`38c5986`→`aa253f9` 并推送。最终不可变 release 为 `20260905T162117Z-e2f2f681480b032b`，git SHA `aa253f99f1838cdbeaae350c3bdc0b835aab0e43`、`source_tree_dirty=false`，自动回滚目标保持上一已验收生产 release `20260904T085912Z-ab3a9e3b4bd55b4a`。
- 停止生产 writer 后创建的新鲜 v36 备份为 `/path/to/user/Library/Application Support/POY-DTY-Agent/shared/data/backups/agent-pre-v37-20260905T152525Z-637d8fc.db`，534,446,080 bytes、0600，SHA-256 `66abe3077f0ec530cf17ccec196f72650ba9482a3c568ca90ffa726d7024ba7b`。隔离副本完成 v36→v37、重复打开、104 张旧表/380,177 行数据与旧 schema 指纹不变，并用 `restore-db --writers-stopped` 恢复到与原备份相同 SHA；生产迁移后 `integrity_check=ok`、`user_version=37`、迁移记录为 `append_only_industrial_intelligence_domain_v37`。
- 分阶段启用暴露并修复了四类真实发布缺口：生产 env 的 shell 自引用、来源注册表与托管 allowlist 漂移、不可变发布缺少 `.env.example`/后端地理资产、地图 smoke 缺少必填 bbox。每项都先保持或恢复 feature-off/停止服务，再以回归测试和新不可变提交修正；没有降低 source registry、deep health、资产哈希或 API 契约门禁。发布管理定向回归最终 **42/42**、Ruff 与 diff whitespace 通过；D1–D6 精确候选的全量门禁仍见 12.40。
- 最终启用后，本地 deep health 返回 `healthy`：source registry 22、storage/LLM 正常，工业情报 `schema=ok`、`fts=ok`、`geo_assets=ok`、schema version 37。五个本地鉴权 API（sources/events/brief/map/runs）均为 200 且 `industrial-intelligence.v1`；首次自然调度前 brief 诚实返回 `data_not_ready`，events/runs/brief 表计数为 0。`/metrics` 已暴露 `poy_dty_intelligence_*` 序列。
- 最终鉴权公网 smoke **29/29 通过**，命中精确 release id/hash，匿名边界保持 `/release.json` 303、deep health 401；旧预测/RAG/事件/交付接口、八个模块入口及五个情报 API 全部通过。证据正文 SHA-256 `01329805c0862d8e06f7ffe04f7d00cb0f93427dd1215aad53c9e4b4bccc5d91`；落盘 latest 文件 SHA-256 `08192644786482c837d5f21722bb6c739ac0b787629e3a80fe2b842b0f0e9ec0`。公网探针随后首轮 `success`，`/healthz` 200、`/release.json` 303，latest 文件 SHA-256 `c56496a94386f4aeabb3f217eaaa11de53775f1432c3583310ba03c29c408f62`。
- 当前服务：backend、frontend、Cloudflare tunnel、news scheduler 正在运行；local daily、morning brief、intelligence daily 已加载并在 09:30 日历/5 分钟补跑合同下正常空闲，周日 RunAtLoad 均 exit 0；event-summary worker 与 experience-settlement 继续未加载。由此可声明 **D7 已正式部署上线 / 运行技术门禁 GO**。
- 仍不得提前声明 `Production Implemented`：部署发生在周日，首份可归因于该 release 的自然工作日 09:30 前非 `blocked` 冻结日报最早于 2026-09-07 形成。这是单一真实时间门禁，不是代码缺口；D8 的 20 个预注册业务日仍未开始。预测轨仍保持严格 **79.8/100**、正式 OOS **0/21**。

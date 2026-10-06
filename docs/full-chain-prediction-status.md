# 全链路预测与 Agent 治理实施状态

- **2026-08-31 当前合同更正**：`docs/project-correction-100.md` 与 ADR-0001
  取代下文所有“单一 POY/DTY 成本压力正式目标”“14 节点/42 格为当前合同”
  或“旧 19 序列为当前完成线”的描述。当前合同严格为原油、石脑油、PX、
  PTA、MEG、POY、DTY × D1/D7/D30（21 格）。旧 Phase A、scalar ledger 与
  CCF 行只作历史审计，不具当前正式资格。下文保留的是实施历史，不是当前
  产品授权。
- **2026-08-30 当前运行合同**：CCF 人工渠道已软移除。历史 CCF 行与旧 19 系列实验只读保留以便复现，但当前采集、导入、队列、调度、告警、新鲜度、就绪门禁和交付口径均排除 CCF。当前 `public-benchmark.v2` 固定为 Brent、WTI、Naphtha、PX、PTA、MEG、POY、DTY、CFETS USD/CNY 九项公开输入，并派生 POY/DTY 上游成本目标；每项按实际发布频率判定新鲜度。重新启用 CCF 必须另立 ADR 并由操作人明确决定。
- **2026-09-01 DCE 运行合同更正**：`dce_meg` 已按用户决定软移除。默认来源清单、采集、导入、队列、调度、告警、新鲜度、凭据预检和当前正式资格均排除 DCE；精确来源查询、历史行、适配器代码和 Keychain 凭据仅为审计与可逆恢复保留。MEG 仍是七个目标之一，其 D1/D7/D30 明确返回 `dce_source_soft_removed` 缺口，不以盘中或第三方代理替代。本文后续任何“DCE 当前可调度/待补凭据”的旧记录均为实施历史，由本条取代。
- **2026-09-01 MEG 标签合同更正（较新）**：操作者已接受 SunSirs 中国乙二醇现货评估作为当前 MEG 标签源，并接受非成交属性及 2013 方法文件当前适用性未独立复核的风险。标签注册表升级为 `seven-product-labels.v3`；首次成功生产采集起形成专属 append-only point-in-time 历史。DCE 仍软移除且保持独立历史身份，Sina/Eastmoney 期货仅作特征。来源接受不创造历史 OOS，也不使任何预测格自动 formal；本条取代上一条中的“MEG 当前显式缺源”，不取代 DCE 软移除决定。
- 更新日期：2026-09-01（Asia/Shanghai）
- 治理依据：`docs/full-chain-prediction-governance.md` v1.0；**2026-08-28 起按"个人工作台 + 公开数据 + 非商用"定位执行去范围化（见下方权威结论），governance v1.0 中的来源授权/manifest 审批条款由本声明取代，其余条款继续有效**
- 定位声明：本项目是单人使用的个人工作台。数据仅使用公开来源且不绕过登录/付费墙/验证码；不再需要来源授权仪式。发布门槛改为三条：数据质量门禁、时间正确性（point-in-time）、基础安全（公网认证与数据完整性）
- 当前阶段：阶段 A 契约与正式序列纯eligibility evaluator/manifest信任根、真实 Assistant治理、Agent评估/聚合/观测、Experience v26/结算链及Shadow/顺序回放纯计算已实现；正式预测POST containment、完整assessment物化、batch正式写入、storage事务proof enforcement及legacy写入口排除已在当前工作树实现，v7定向复验及13文件独立静态/重建核验已通过；生产正式来源资格与发布证据链仍为当前P0
- 总体状态：`IN_PROGRESS`
- 阶段矩阵：`A=CONTRACT_V7_AND_ELIGIBILITY_EVALUATOR/FORMAL_WRITE_IMPLEMENTED/V7_TARGETED_VALIDATION_PASSED/IN_SCOPE_SOURCE_RECONSTRUCTION_PASSED/SOURCE_MANIFEST_0_OF_19_BLOCKED/RELEASE_EVIDENCE_PENDING`、`B=TRACE_EXECUTION_EVAL_OBSERVABILITY_AND_READ_ONLY_WINDOW_COLLECTION_ACCEPTED/CORE_DEPLOYMENT_ACCEPTED/RUNTIME_WINDOW_EVIDENCE_PENDING`、`C=PARTIAL`、`D=COMPUTE_PERSISTENCE_PLANNER_READ_API_AND_ATOMIC_ADAPTER_ACCEPTED/CUTOFF_INPUT_SELECTION_AND_DEFAULT_OFF_SCHEDULER_ACCEPTED/HTTP_AND_PRODUCTION_ENABLE_PENDING`、`E=SHADOW_LIFECYCLE_AND_SEQUENTIAL_REPLAY_ACCEPTED/PRODUCTION_WIRING_PENDING`、`F=NOT_CLOSED`

## 当前权威结论

- **2026-08-28 个人工作台去范围化（用户决定）**：manifest 信任根与来源授权仪式整体删除——任何结构合法的 approved-evidence manifest 直接作为证据投影参与质量评估，其 canonical digest 仅作为 provenance 绑定进 assessment 身份；`approved_evidence_manifest_untrusted` 不再产生。证据复核（evidence-queue）契约为最简 `{status,purpose,evidence_role,notes}`，`result` 服务端派生；`PERSONAL_MODE=1` 时 `reviewed` 即本人批准，`PERSONAL_MODE` 关闭时正式门禁对最简复核保持关闭。`POST /api/v1/predictions` 保持认证后零写，409 语义改写为 `formal_prediction_write_path_disabled`。formal evidence dossier 生成器（832 行）已删除。数据质量 `blocked_*`（单位/日历/可见性/新鲜度/换月）与 point-in-time 门禁不受影响且继续生效；schema 保持 v33 不变。 logout 修复、治理调度器逐轮隔离、回测假置信度移除、import 错误有界化等基础修复已完成。全量后端回归在每阶段后保持全绿（阶段 3 后为 1550 项通过）。
- Phase A 业务政策已由用户按推荐方案接受：正式期限为 D+1/D+7/D+30，D+14 仅兼容历史，不新增。
- 用户于 2026-08-06 决定将 INE/SC 从当前正式来源线移出：不再进行浏览器验证、抓取、下载、来源验收或正式资格提升。v1–v4 的 SC 合约和历史候选实现保留可审计；当前 v7 合同不再包含 SC，任何恢复都需要新的明确决定。
- Phase A v7 机器契约及正式序列纯eligibility evaluator/manifest信任根已实现：14个正式预测节点、42个节点/期限单元、19个逐序列定义（CFETS USD/CNY为换算证据、不是预测节点）以及事件/摘要/预测/经验Schema均有可执行验证；公开第一方数据按个人复用政策可进入候选来源，四条中国期货共享已冻结的主力连续和换月规则，六条工业品规格和 POY/DTY 压力公式也已冻结。CCTD 环渤海 5500K 日度参考已登记为煤炭候选，原 CCTD 周度来源仍只作背景；SunSirs 华东混二甲苯日度评估已登记为 MX 人工浏览器取证候选，保留范围和中点规则且不等同于成交价；两个候选均尚待逐条捕获、规格、可见性、日历和 manifest 审批，不能正式写入。生产批准manifest当前为空，新契约判定正式序列`eligible=0/19`并全部保持`blocked_*`。`POST /api/v1/predictions`现会在认证和模型校验后、summary/snapshot/旧gate/ledger之前固定返回409且零写。当前工作树已具备完整assessment物化、正式batch写入及同事务proof enforcement。**【2026-08-28 更正】该409现码为 `formal_prediction_write_path_disabled`（旧码 `formal_series_eligibility_not_trusted` 已随 manifest 授权删除而退役），本条其余历史描述保留原文。**
- 正式目标仅为 POY/DTY 上游成本压力；产业链节点分别判断，数据不足必须降级或不可评分。
- 08:20 冻结晨报数据、09:30 发布；夜盘按交易所交易日，价格后验按有效观测日。
- 历史回放范围为 2025-01-01 至 2026-07-01，首尾均包含。
- DeepSeek、人工确认、Shadow 因子晋级及晨报 revision 规则以治理规范第 1.2 节为准。

## 已完成并有证据

- 2026-08-31 已补齐五项非关键来源的无人值守链路：OPEC 新闻、OFAC 制裁增量、GACC 月度统计、UN Comtrade 月度进口以及用户 CSV/XLSX/PDF/图片收件箱。所有网络源均为免费公开入口，不新增付费服务；UN 优先使用工作区已有免费 key，失败时回退官方 preview。生产首次写入报告位于 `shared/source-automation-first-delivery/source-automation-latest.json`，数据库写前备份位于同目录 `db-backups/`。OPEC 索引遇到 browser check 时仅以公开 Google News RSS 发现候选，候选必须解析并校验为 OPEC 官方 `/pr-detail/` 页面后才可作为 A 级证据；未解析包装链接直接丢弃，不绕过登录、付费墙或验证码。

- DG-01 v25 时间戳隔离、不可变审计、恢复重算、双 Hash、批量回滚、并发迁移和下游排除代码候选已通过专项审查。
- DG-01 remediation 当时的证据快照：定向88项、后端全量736项通过，Fresh Ruff及diff检查通过；它不是当前全后端测试数量。
- 非空 v24→v25 仓库外演练证明 ID、link、correction pair、旧列rowset、legacy及verified/raw Hash、manifest、FK、integrity和重复审计。
- Memory、持久RAG、GraphRAG、context pack和Agent trace存储为真实实现，不是纯文档占位。
- 固定12角色、显式工具白名单及permission version拒绝审计已实现。
- Agent run/job/turn、IO、tool和handoff账本已实现原子写入、状态矩阵、显式引用、retry/handoff/attempt并发唯一性及失败回滚；独立监督复审无P0/P1/P2。
- 非preview Assistant 已真实接入治理账本：检索、判断、护栏和报告四阶段逐项校验权限并留痕，provider执行时不持有账本事务，失败会原子收束运行、工具和handoff状态；preview不写账本。
- Assistant公开响应与流式header已区分真实`agent_run_id`和preview响应标识；凭据在进入context、provider、响应和trace之前统一脱敏，fallback或质量降级只能进入人工复核。
- Agent纯评估器已实现生产trace四阶段/角色/tool/handoff完整性、终态残留、fallback/人工复核及凭据脱敏体检；仅输出稳定finding code、聚合计数及有界完成率，不回显原始trace。
- Agent纯聚合观测已实现eval版本/字段/跨字段不变量校验、去重、pass/review/fail与安全/残留指标；阈值未显式提供时只报告，绝不自行判定production-ready。
- Agent单次运行内部评估读取API已接入真实pipeline来源证明，只接受受治理的Assistant trace并返回有界稳定字段；它不回显原始trace，也不等同于自动窗口聚合或production-ready政策。
- Agent已具备内部只读窗口采集适配器：它先校验显式带时区窗口，再仅读取候选Assistant身份，并逐条复核完整来源证明后交给纯聚合器；运行ID、trace和自由文本均不回显。它不设置阈值、不提供公网API或调度。
- 预测账本、快照、到期复盘基础、时间安全检索、事件聚类、事实摘要、引用/反证和降级门禁已有实现与测试。
- `POST /api/v1/predictions` containment已通过完整`test_api.py` 175项验证（6条既有warning）及独立监督验收；非正式model-signal路径和GET/review行为不变。
- 正式预测写入期限契约已限制为D+1/D+7/D+30；D+14只能读取历史记录，storage直写入口也会拒绝新增D+14。该期限门禁和POST containment都不等于完整formal-series写入能力。
- Experience Card纯计算层已实现D1→D7→D30渐进成熟、预测/评估双as-of、不可变revision、POY/DTY独立指标及不可评分诊断；它不读取数据库、系统时间、文件或LLM。
- Experience Card v26持久化已实现append-only revision链、D1→D7→D30状态约束、同阶段修订、head CAS并发、canonical payload Hash和逐次连接深审计。
- Experience到期结算纯计划器已实现严格RFC3339双as-of、D1→D7→D30补跑、D30同阶段修订、POY/DTY独立及上游blocked/unavailable稳定拒绝；它不写库、不调用网络或系统时间。只读candidate adapter会用冻结的PTA/MEG 20日共同有效观测公式生成成本压力 target，并以POY 150D/48F、DTY 150D/48F低弹的独立日度评估路径作benchmark；每个派生点绑定其完整40条底层capture revision。持久化输入加载器要求调用方显式提供 prediction revision、评估 snapshot 和 cutoff，重新审计 revision 及其绑定的正式资格评估、拒绝晚于 cutoff 的 snapshot，并从三条已审计 horizon 结果导出状态；任何不一致的调用方状态覆盖均拒绝。它不自动选择生产输入、不提升series状态，当前正式资格仍为0/19。
- Experience revision/head内部只读API及同一candidate多action原子落库adapter已实现并验收；不同candidate独立提交，冲突或中途故障整组回滚。持久化候选加载器和显式参数内部settlement service均要求prediction revision、evaluation snapshot和cutoff，并重审绑定的资格评估和既有card head；它们不自行选择生产输入、不提升资格。只读selector会在明确cutoff内选择最新已复审prediction revision与snapshot，缺任一输入时返回blocked且零写；weekday 09:30 Asia/Shanghai scheduler已接入但默认关闭，错过cutoff不晚结算。内部 `POST /api/v1/experience-cards/settle` 已接入但默认关闭、仅接受内部认证且拒绝调用方时间或上游输入；生产启用和任何公网写入口仍未获批准。
- 顺序历史回放纯计划器已验收：严格覆盖2025-01-01至2026-07-01（首尾包含），按D1→D7→D30推进并将D14保持为只读兼容。显式point-in-time candidate loader现可把完全重审的正式预测revision/cell/subtarget及Experience revision链转换成纯计划器精确checkpoint；它不自动选择revision、snapshot、产品、节点、期限、日历或policy，其他持久种类只返回有界诊断且不会伪造Shadow。schema v32现已提供append-only run/event、精确重复幂等、严格顺序checkpoint、暂停/恢复/失败及只读resume审计；生产scheduler、动作执行和Shadow输入装配仍未接入。
- Shadow因子生命周期已实现120个可评分样本、3个滚动样本外窗口、逐窗口稳定增益、paired高置信错误非恶化、未来泄漏阻断和四状态转换；未冻结的数值阈值必须由带版本policy显式提供。
- 顺序回放`assemble_shadow_sample`现已具备纯函数安全装配边界：严格校验D1/D7/D30动作形状、身份、点时可见性、幂等键和policy版本，并可直接生成Shadow生命周期输入。当前没有verified action、factor/baseline projection、outcome target binding、lifecycle-state/retirement reader及误差metric contract，故调用方输入只能生成`blocked`，重建证据只能生成`unscorable`；状态固定为shadow且禁止退休，该边界不会伪造可晋级样本、写库或启用scheduler。
- 官方期货下载审计已修复“绝对父目录名称被误当成品种证据”的组合污染，回归测试2项通过；品种识别仅使用文件名和文件内容。
- 当前`server/tests`完整测试实际1481项通过、5条warning均为既有FastAPI/Starlette弃用提示；`server/app`与`server/tests`全量Ruff及diff检查通过。上述结果是受控源码验证，不是正式来源验收、完整发布证据或生产上线证明。
- 2026-08-06 已修复两条日常RAG评估测试在断言结束后可能被 FastEmbed 后台线程拖住退出的问题：测试期间明确使用离线 hash fallback，并在 finally 恢复原始 embedding 设置；当前完整 `test_api.py` 已实际退出，208项通过、5条既有弃用warning。生产 FastEmbed 默认配置未改变。
- 2026-08-27 已在全新临时库复验当前 v7 Phase A 正式写路径：Phase A契约、资格矩阵、assessment物化、42格batch正式写入、proof/legacy fail-closed及时间门禁共147项通过（5条既有弃用warning）。受控根：`/private/tmp/poy-dty-formal-v7-evidence.L4kPY4/`。该复验不替代正式来源批准、当前源码静态/独立重建证据或生产发布验收。
- 2026-08-27 已将13个正式写路径及其定向测试文件从`c860c3a`独立导出并逐文件与当前源码比较，全部`cmp=0`；同一范围Fresh Ruff通过。重建根：`/private/tmp/poy-dty-formal-v7-reconstruction.oYzOvw/`。这只证明该范围的当前源码身份和静态质量，不替代正式来源、完整发布证据或生产验收。
- `docs/phase-a-formal-series-readiness.md` 已把当前19条正式序列与候选来源逐条对齐，明确区分一一匹配、市场/报价口径不匹配和无已验收候选；它是来源验收清单，不是manifest或来源升格。
- PTA、MEG 与 CFETS 已新增只读六门证据 dossier 生成器：必须显式绑定 capture revision、raw/canonical Hash、内容寻址授权材料和日历材料，在同一 SQLite 读快照中逐项核对来源端点、授权范围、完整 canonical projection、新鲜度、可见性和交易日。缺失保持`unknown`、冲突或篡改fail-closed；输出仅为候选审核材料，不写库、不生成审批、不修改信任根，因此正式状态仍为`eligible=0/19`。
- 其余6条交易所/基准及8条国内评估序列已具备确定性的本地readiness记录；所有记录继续保持`blocked/observation_only`。中国期货主力连续候选会按冻结字典序、连续两交易日确认和次交易日换月规则计算，但调用方自报capture/hash/calendar永远不能自批为candidate，必须后续绑定独立append-only ledger与冻结日历证明。
- `npm run eval:ai` 已升级为仓库外临时库中的真实四阶段 Assistant 离线评测：运行实际pipeline并复核stage、handoff、版本化工具权限、引用、冲突/反证、安全降级、时延、token预算和可复现元数据；外部provider被禁止，旧内部关键词接口仅保留兼容，不再作为发布证据。
- 日度工作台只读快照已能携带经完整重审的正式批次payload；前端按D1/D7/D30展示POY/DTY成本压力方向、置信度、完整度、可评分状态及批次/修订/assessment/snapshot指纹。旧标量预测明确标为历史兼容复盘；旧快照只有元数据时页面明确缺网格且不补造结果。
- Experience 09:30 scheduler仍默认关闭；当前源码已隔离单轮失败并保留稳定失败结果，下一轮可继续。shutdown取消会等待底层settlement线程终态后才声明停止，避免残留写线程与后继调度重叠。这是本地可靠性闭合，不代表生产启用。
- 2026-08-27 当前源码在全新外部临时根`/private/tmp/poy-dty-mainline-final-proof.rSwLWE/`完成后端全量回归：1481项通过、5条既有弃用warning。正式预测/Experience顺序回放读取走`mode=ro/query_only`，缺库、旧schema和篡改均稳定fail-closed且不建库、不迁移。第三轮独立对抗复核未发现剩余P1/P2。
- 2026-08-27 schema v32顺序回放检查点包完成严格schema约束、控制事件容量预留、深层输入预算及迁移故障回滚加固；定向19项通过，随后当前源码在全新隔离根`/private/tmp/dg01-replay-v32-accepted-full.OZRXUm/`完成后端全量1500项通过。全量5条warning均为既有FastAPI/Starlette弃用提示；该结果不代表生产动作执行或scheduler已启用。
- 2026-08-28 Shadow安全装配边界经两轮实现与独立对抗复核，已封闭调用方驱动降级/退休、证据替换身份碰撞、预测后可见数据和非规范时间身份等问题；相关141项通过，随后当前源码在全新隔离根`/private/tmp/dg01-shadow-assembler-final-full.8GUsnr/`完成后端全量1525项通过。全量5条warning均为既有FastAPI/Starlette弃用提示；因正式factor/baseline verified projection与metric contract尚未冻结，该边界只生成`blocked/unscorable`，不代表Shadow已可晋级或已生产启用。

## 尚未闭环

- P0（生产仍阻断）：生产批准manifest仍为空、正式eligibility仍为`0/19`；历史release证据链仍为`INCOMPLETE`，且没有当前来源验收与发布证据。不得上线或批准生产使用。
- Retry05仅对其当时绑定的DG-01源码和production-derived copy产物通过；当前`storage.py`已发生获批变更，successor evidence尚待刷新。Retry05明确`release_go=false`，历史证据链仍为`INCOMPLETE`，不得据此宣称当前源码发布完成。
- 正式逐序列行情仍缺少足够的官方或已授权来源；19个当前正式证据序列均不得因“可连接”而自动升级为可评分。
- 首批运营级来源合同已冻结在`docs/initial-source-evidence-contract.md`：CCF 石脑油、PX、PTA、MEG先进入带证据和revision的内部观察链；EIA Brent/WTI与CFETS先作为语义明确的证据输入。CFETS现为当前第19条正式换算证据，但在受信manifest为空时仍为blocked；CCF的境外美元石脑油/PX观察不等同于国内人民币正式系列。该合同不改变正式manifest为空或`eligible=0/19`。
- 首批来源的 v28 append-only evidence ledger 和 CCF CSV 导入链已完成首批受控实测：导入前验证账本 schema 并备份正式库，随后保存原始文件哈希、URL、可见/捕获时间、授权范围和不可变 revision 链。2026-08-05 在用户已登录、完成页面验证的 CCF 会话中捕获 MEG、PX、PTA、日本石脑油共23行；导入后有3个新增日期点和20个同价证据修订，完整性检查为`ok`、外键检查为空。该批在08:20后获得，只能作为晚到候选 revision，未覆盖晨报，也未执行正式 manifest 审批。证据：`/private/tmp/ccf-late-candidate-import.EMSi1A/`。
- EIA 官方适配器已完成一次受控刷新：在 API key 已配置、`api.eia.gov` allowlist 生效时写入1,480条石油观测、记录 fetch audit，并将正式库迁移至当前 schema v29；完整性检查为`ok`、外键检查为空。当前 Brent/WTI 可得观察日仍早于运营新鲜度阈值，故它们仍是带来源的运营候选而不是正式预测价格或 ICE/CME 结算。备份与复核证据：`/private/tmp/eia-observation-bootstrap.CltPiO/`。
- CFETS USD/CNY 中间价已从静态契约升级为官方 JSON 候选适配器：只接受明确的`USD/CNY`、正值和可解析公布时间，并以`cny_per_usd`保存候选观测事实。候选观测与 v28 不可变 capture revision 同事务写入，证据失败会整体回滚，且内部证据不进入既有 fetch API 响应；真实官方读取验证为2026-08-05的6.7889。它尚未自动调度、未进入受信manifest，不能解除19条当前正式序列的`eligible=0/19`阻断，也不发布独立汇率预测。
- 2026-08-06 对 CFETS 官方 JSON 的只读候选复核返回 USD/CNY 中间价 6.7895、公布时间09:15，并保存原始响应 SHA-256；EIA 适配器只读复核返回 Brent/WTI 至2026-08-03。两者均仅补强候选证据，未写库、未改 manifest，也没有把 EIA 现货观察冒充为 ICE/CME 正式结算。证据：`/private/tmp/formal-source-normal-access-20260806/`。
- Agent账本、并发治理、真实Assistant四阶段执行、纯trace评估、纯聚合观测、单次运行内部评估GET及内部只读窗口采集已闭合；`agent-production-readiness.v1`已冻结并提供北京时间每日08:10的连续进程内只读窗口调度。非ready或窗口不可得时为`low_confidence`、不创建新的人工复核队列。进程在08:10后启动时，会为刚结束窗口持久化一份低置信度报告且不补跑采集；已有同窗口正常报告优先。每日报告现以不可变、同窗口冲突拒绝的记录持久化，重启后可读取；两个独立解释器对同一临时库、同一窗口并发结算时已收敛为同一报告且仅写入一行。当前新增的跨进程读取只输出`unsigned_snapshot_comparison`，不会把前后快照相同冒充为生产重启或exactly-once证明；实际08:10服务身份、签名restart event及append-only review event ledger仍待运行期闭合。
- 2026-08-06 已在全新隔离库复验 Agent 账本、trace 评估、聚合、窗口采集及每日政策：98项通过；其中包括同窗口双进程持久化测试，两个独立解释器最终只生成同一份不可变日报。该结果验证当前源码，不构成真实部署已验收的声明。证据根：`/private/tmp/agent-governance-current.f1xSHI/`与本轮临时根。
- 2026-08-27 已在全新隔离库刷新当前源码的 Agent治理/真实Assistant、事件摘要存储与worker、Experience计算/持久化/选择/服务/调度、Shadow生命周期及顺序回放验证：24个测试文件共424项通过，5条warning均为既有FastAPI/Starlette弃用提示。证据根：`/private/tmp/poy-dty-agent-experience-validation.jwDRZY/`。该结果不等于真实08:10/09:30运行窗口、生产启用或发布验收。
- 2026-08-06 已部署 release `20260806T105640Z-53d1923cf215378d`：先完成在线备份，再由真实v23库启动迁移到schema v31；只读核验确认migration 23–31均存在，`/api/v1/health/ready`与`/api/v1/health/deep`通过，source registry=23，backend与news scheduler均已恢复，event-summary worker继续停止。此前旧release缺少Agent治理调度的部署核验已失效；下一项是实际08:10窗口报告和跨重启持久性的运行证据。证据：`/private/tmp/dg01-agent-production-deploy-v23fix.XkaxIv/`。
- Experience Card纯计算、v26持久化、到期纯计划、revision/head内部只读API、原子落库adapter、持久化上游candidate loader及显式参数内部settlement service已闭合；新增的只读selector会在明确cutoff内选择最新已复审prediction revision与snapshot，缺任一输入时返回blocked且零写。`china-weekday-business-days.v1`在预测日之后生成30个周一至周五的预期观察日；weekday 09:30 Asia/Shanghai scheduler已接入但默认关闭，错过cutoff不晚结算。内部结算命令也默认关闭；生产启用尚未完成。
- Shadow生命周期判定、顺序历史回放纯计划器、正式预测/Experience的point-in-time只读candidate loader、schema v32检查点/暂停恢复持久层及Shadow安全装配骨架已闭合；正式policy阈值、factor/baseline verified projection、metric contract、受控动作执行和实时调度尚未串起预测结算、经验成熟、Shadow评估与09:30不可变revision发布。
- Shadow metric现已有内容寻址候选合同和无数据输入的透明三分类均匀基线；三分类Brier、confidence及不任意拆分tie的方向错误语义具备纯函数测试。该基线只有在`high_confidence_threshold <= 1/3`时进入paired cohort，未来批准必须把合同与兼容policy原子绑定；生产批准trust root保持为空，因此它尚不是获批metric contract，也不会产生可晋级样本。
- `docs/agent-runtime.md`、`docs/agent-evaluation.md`、`docs/experience-settlement.md`、`docs/experience-card-persistence-api.md`、`docs/api.md`和OpenAPI已随对应实现同步；生产运行文档仍须在真实调度接线时更新。

## 当前并行工作包

1. 汇总当前源码正式assessment/batch写路径的定向、静态、重建与来源Hash证据，补齐完整发布级证据包；不得把受控专项测试或固定409 containment称为生产正式写入获批。
2. 官方/授权逐序列数据源接入、单位/交易日/可见性验收及批准manifest发布；INE/SC 已按用户决定仅保留在历史合同。其余候选来源在此之前仍不得自动升级，v7 正式eligibility保持`0/19`，生产使用不得获批。
3. Experience内部 settlement 命令、受控输入选择、09:30 weekday cutoff和scheduler已实现但保持默认关闭；下一步是生产启用审核而不是新增公网写API。上游正式观测不可用时必须明确拒绝，不得启用启发式回退。
4. 验证部署中的`agent-production-readiness.v1`每日执行、窗口观测和低置信度姿态；不得把临时库测试或单次进程内状态当成跨部署持久证明。
5. 已验收顺序回放计划器、point-in-time candidate loader、append-only checkpoint/resume边界及只产出blocked/unscorable的Shadow安全装配骨架；下一步为factor/baseline verified projection、metric contract、受控动作执行及数值policy冻结。
6. 09:30 revision发布、Experience成熟和Shadow评估的生产调度。
7. DG-01当前源码的后继隔离证据包、生产派生副本门禁及最终文档同步。

## 冻结与非范围

- Cloudflare/应用密码登录候选为暂停支线，不属于当前主线，不部署、不扩展。
- 原DB/WAL/SHM、事故证据、Git index及来源不明工作区修改继续保护。
- 不stage、commit、stash、reset、restore或clean。
- 生产服务、自动化、CCF及数据库状态变更必须另行通过相应运行门禁。

## 当前证据索引

- DG-01定向验证：`/private/tmp/dg01-remediation-targeted-evidence.3ASKSb/`
- DG-01全量验证：`/private/tmp/dg01-remediation-full-suite-evidence.ozMXj2/`
- DG-01静态验证：`/private/tmp/dg01-remediation-static-evidence.iflOio/`
- DG-01最终演练包：`/private/tmp/dg01-remediation-final-evidence.XMSosF/`
- 命令溯源补件：`/private/tmp/dg01-remediation-command-provenance.d9916o/`，结论为`INCOMPLETE`
- Retry05当前产物/production-derived copy门禁：`/private/tmp/dg01-final-artifact-prod-derived-evidence-retry05.nUx448/`，结论为`PASS`，同时`historical_evidence_chain=INCOMPLETE`、`release_go=false`
- CCF 首批内部观察导入：`/private/tmp/ccf-late-candidate-import.EMSi1A/`，23行已导入；正式来源资格仍未批准。
- EIA 首次官方刷新：`/private/tmp/eia-observation-bootstrap.CltPiO/`，1,480条观测已写入；最新观察的新鲜度仍阻断正式资格。
- 当前任务包：`/private/tmp/poy-dty-mainline-completion.Kuu2BI/TASK_PACK.md`

## 下一门槛

- 首要门槛是完成当前源码的完整发布级证据包，并完成正式来源验收与批准manifest发布；在此之前不得宣称生产正式写能力完成、上线或批准生产使用。
- 当前定向验证与in-scope重建已经刷新；`storage.py`新增正式期限写门禁后，旧Retry05仅保留历史意义，不能直接代表当前源码。
- 后续实施包按互不重叠范围推进：正式来源验收与批准manifest、Experience真实候选/结算写入/调度、Agent自动窗口聚合与政策、回放/Shadow生产接线；每个包必须同步代码、测试、证据和本状态文档。

## DeepSeek Harness 接手

- Windows 接手入口为 `docs/deepseek-harness-handoff.md`；推荐使用 WSL2。
- GitHub 只保存源码、契约和可复现测试，不保存本机 `.env`、生产数据库、WAL/SHM、自动化运行态或 `/private/tmp` 证据包。
- 新 Harness 必须先以本文件和治理规范建立当前状态，不得把历史聊天、旧证据路径或已失效审批作为当前授权。

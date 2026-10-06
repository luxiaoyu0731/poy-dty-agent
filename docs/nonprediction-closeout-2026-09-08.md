# 非预测结果缺口关闭交付 — 2026-09-08

> 状态：**代码与发布完成，浏览器主证据阶段被真实阻塞**。本机显示器自 2026-09-08 05:00（上海）起不可用（`list_displays` 为空，AppleScript 前台确认超时，机器电源状态为唤醒但屏幕锁定/休眠）。按任务规则不使用接口检查冒充浏览器验收，Chrome 主证据矩阵与部分项的最终关闭待屏幕解锁后执行（续作指引见第 10 节）。

## 1. 一句话结论

87/100 基线上的全部 8 项、13 分程序侧修复已完成开发、测试（后端 2012 通过、E2E 107 通过、node 测试 13 通过、Ruff 清零）并发布上线（`20260907T195211Z-fe18ebe9a9817017`）；HTTP 实测证实评测读取、首屏传输量、资源缓存、报告交付一致性均已改善。其余 12 分（含 1 分 R12 的浏览器交互复核）需要 Chrome 主证据矩阵关闭；3 分预测结果待验分按边界保留。

## 2. 排除项边界（本轮开工时申明，未扩大）

| 项目 | 分数 | 归类 | 处置 |
|---|---:|---|---|
| R03 的预测结果部分：正式 0/21、OOS 0/21、有效样本不足 | 3 | E | 保留为"预测结果待验分"。程序侧（评测读取、样本统计、时间边界、结算、门禁逻辑、21格生成/展示/诊断/导出）本轮已验收：门禁原因逐格诚实（`insufficient_effective_samples` 21/21、`error_improvement` 21/21、`direction_accuracy` 20/21），账本历史三批（09-05/06/07）各有 scored 结算记录（1/1/4），结算链在生产真实执行，其余 pending 为到期未成熟的诚实状态。0/21 是 `有效样本≥20` 门禁对短采集历史的正确输出，不是程序缺陷。 |

不计入排除且本轮已修复：R04(2)、R06(1)、R07(1)、R08(2)、R11(2)、R12(1)、R13(1)，共 10 分。分数口径：非预测结果项满分 97；97 + 3 待验 = 原表 100。

## 3. 逐项修复记录（现象→根因→修复→验证→证据）

### R03（3分/E，程序侧验收）
- 根因调查：评测链（`seven_product_evaluation.py`、`seven_product_forecast_ledger.py`）样本统计、时间边界（expanding-origin 50%、point-in-time 双重防护）、结算幂等（`BEGIN IMMEDIATE`、sha 冲突检测）、不可变性（BEFORE UPDATE/DELETE RAISE 触发器）、门禁阈值（≥20有效样本、误差改进≥5%、方向≥55%）全部核对正确且有既有测试覆盖。
- 修复：评测端点加内容寻址 TTL 缓存（默认60s，`SEVEN_PRODUCT_EVALUATION_CACHE_TTL_SECONDS` 可调），消除"每请求全量重算 21 格×400 次 bootstrap"导致的间歇读取失败（与 R08 同根因）。
- 验证：公网评测端点修复前 9.4s 冷读/曾出现 15s 前端超时失败；修复后连续三次读取 1.6/1.4/1.1s，`evaluation_id`（内容寻址）逐字一致，21格与门禁原因如实返回。新增 `test_seven_product_evaluation_cache.py`（TTL 命中、键隔离、零TTL禁用）。
- 证据：`evidence/before-eval-public.json`（修复前）、`post-eval-warm*.json`（修复后）。

### R04（2分/G）
- 根因：三处基准各有标注缺口——(a) 后端 `_merge_intraday_market_point` 异基准分支调用 `_mark_price_freshness` 会把价格类别标成 `fresh`（age 0），图表仍停在 7 月历史序列；(b) 图表本体无"历史截至"标记，合并的当前报价点与历史点视觉无区分；(c) AI 提示词未要求回答复述价格时点与基准。
- 修复：(a) 新增 `_mark_foreign_basis_quote`——同口径序列新鲜度如实保留 stale 并注明"同口径价格序列仅更新至X；Y最新报价为不同基准"；整体 latest_date 仍如实报告最新可见点。(b) 趋势图新增画布内角标"历史序列截至 YYYY-MM-DD · 非当前报价"（stale 时显示），合并当前点 tooltip 标"价格（当前报价）"。(c) 系统提示（deepseek + prompt 种子 v2026-09-08.v2）要求引用价格必须写观察日期、币种单位、报价基准；不同基准/日期不得写成同一段涨跌；历史报价必须标明历史。
- 验证：`test_workbench_market_chain.py` 26 项全过（含扩展的异基准断言：price 类别 stale、latest_date 保持同口径日期、detail 注明不同基准）；`npm run check` 通过。
- 待浏览器复核：行情页截图证明角标可见。

### R06（1分/E，程序侧完成）
- 根因/现状：排序修复（分数降序、`parse_iso` 真时序降序、ID 降序）与发布/收录/发生日期分离在工作区已实现；冻结摘要不重写。生产仅 9/7 一份日报且晚发布 12h54m（投影时区错误 61 次+聚类失败），该摘要冻结于修复部署前，保留旧排序是正确行为。
- 本轮补充：新增 `test_daily_ranking_breaks_full_ties_by_event_id_desc`（同分同刻 ID 降序的此前唯一测试空白）；公网核对 9/7 摘要（`5322be6e`，cutoff 08:20、计划 09:30、实际 22:24:13 如实记录）；v5 流程规则第 8 条写入判定标准。
- 待关闭：按新排序的**自然日真实发布**验证——9/8（周二）日报计划今早 09:30 发布（发布后代码 fe18ebe9 首次自然运行）。检查点：`/api/v1/intelligence/brief` items 顺序=分数↓时序↓ID↓、released_at 接近 09:30、运行审计无失败。

### R07（1分/G）
- 根因：模型成功≠长度合规——提示词无明确长度约束指令；管线把任何回答渲染成六段结构，"三句话"问题必然返回大段无关结构。
- 修复：新增 `_question_sentence_limit`（中文/数字 N 句话解析）、`_enforce_sentence_limit`（按完整句截断）、`_render_compact_answer`（只渲染 结论+可信边界）；截断后按截断文本重新绑定引用（`bind_claims_to_evidence`），不沿用原文引用冒充支持；trace warnings 记录 `length_constraint_requested` 与 `length_constraint_trimmed` 供审计。deepseek 系统指令与 prompt 种子同步加强"简明扼要、不重复上下文"与引用支持要求。
- 验证：`test_assistant_answer_length.py` 4 项（解析/截断/紧凑渲染）；管线回归 51 项过。
- 待浏览器复核：公网真实提问"请用三句话解释原油如何影响PTA，并引用现有证据。"——期望只返回结论（≤3句）+可信边界，引用可追溯，质量不降级为假成功。

### R08（2分/G）
- 根因（三个，全部程序性）：(a) 每个 SQLite 连接重复运行 1+7 项 schema 审计查询；(b) 只读连接在写入竞态窗口（journal 出现/WAL-SHM 不对称）直接 fail-closed 503；(c) 嵌入信号量 2 槽位满时**静默**降级 hash_fallback；(d) 评测端点每请求全量重算（并入 R03 修复）。
- 修复：(a) 审计结果按文件指纹（ino,size,mtime_ns）缓存，文件变化即失效——保持 fail-closed 且新增 `test_schema_audits_run_once_per_unchanged_file`（含篡改即重审计语义）；(b) `connect_readonly` 对 sidecar 瞬态加有界重试（4次×150ms，耗尽仍 raise），并保证只读打开**不创建/遗留** -wal/-shm；(c) 嵌入槽位改有界等待（默认5s，`EMBEDDING_SLOT_WAIT_SECONDS`），仅在真实饱和时才诚实降级。
- 验证：`test_connection_read_path_resilience.py` 3 项；`test_sequential_replay_checkpoint_store.py` 31 项全过（零写入/防篡改边界不受影响）。
- 过程记录：初版实现曾把 immutable 标志取反（mode=ro 打开 WAL 库时创建并遗留 sidecar，导致 6 个 checkpoint 测试失败）；通过最小复现二分定位后修复并以"只读打开不得创建 sidecar"断言防回归。

### R11（2分/G）
- 根因（25.65s 检索）：管线全串行且重复劳动——语义检索全表扫描跑 2 遍（每遍对全部 chunk Python 分词）、GraphRAG 快照+1000 行记忆查询跑 2 遍、聊天路径零缓存、每连接重复 schema 审计（约15-25 次/问）。成本是 CPU/IO，不是等待，延长超时无解。
- 修复：单次上下文构建内复用一次 `materialize_graph_snapshot` 与一份 memory 查询结果（二者只依赖 question/product/as_of，与 allowed_evidence_ids 无关——图路径过滤仍按各自 pass 独立执行）；`semantic_index` 可见行 2s TTL 缓存（键含 index/as_of/doc_types，重建即失效，point-in-time 过滤仍逐调用执行）+ 按 chunk 的分词 memoization（纯函数缓存，重建即失效）；R08(a) 连接审计缓存全局受益。
- 验证：`test_semantic_index.py`、`test_graph_memory_runtime_integration.py`、`test_retrieval_metrics.py`、`test_assistant_pipeline.py` 全过；性能预算与测量方法写入 v5 规则第 1 条。
- 待浏览器复核：真实提问计时（预算：端到端≤30s、检索阶段≤10s）。

### R12（1分/A）
- 根因：真实加载成本问题——(a) 源站静态资源从不 gzip（公网靠 Cloudflare 边缘压缩兜底）；(b) 内容哈希资源 `private, no-store`——**每次刷新全量重新下载**；(c) recharts 380KB 只在行情模块使用却被首屏静态加载；(d) 唯一路由页是动态导入，首屏多一串行 RTT 发现 AWP 分块。Map 分块已经是惰性（I06 满足），不在首屏。
- 修复：`local-public-server.mjs` 静态路径传递 Accept-Encoding + `/assets/` 改 `public, max-age=31536000, immutable`（index.html 保持 no-store）；趋势图提取为独立模块 `MarketTrendChart.tsx` 懒加载（唯一 recharts 引用点）；vite 内联插件把 AgentWorkbenchPage 分块注入 modulepreload。未提高 warning 阈值、未为消警拆包。
- 验证：`npm run check` 通过；集成测试断言（gzip/immutable/vary/no-store）通过；公网实测 antd 289,478B gzip 传输 + immutable 头 + modulepreload 四项齐全；首屏 JS 369KB(gzip)（修复前 eager 集多 ~101KB 的 charts）。构建警告仍在（antd 944KB 原始体积），如实保留——它是 eager 首屏 shell 的真实组成，非本轮可消除项。
- 待浏览器复核：Network 面板实测首次进入/强制刷新/二次刷新传输量与请求量。

### R13（1分/G）
- 本轮无代码改动（证据缺口）。方案已定：Chrome 访客（Guest）窗口=全新独立配置，满足"与已有无痕窗口不共享会话"的此前缺口。v5 规则第 4 条写入判定标准。
- 待执行（被显示器阻塞）：访客窗口匿名进入全部八模块+账本+深链接矩阵，配合无 Cookie 只读 HTTP 请求旁证。

## 4. 测试与发布记录

| 层 | 结果 |
|---|---|
| 后端全量回归（隔离DG01环境） | **2012 passed, 0 failed**（10:46） |
| Playwright E2E | **107 passed, 1 skipped, 0 failed**（9:36，两轮均退出码0） |
| node 集成测试 | public-password-auth 1、public-login-browser 2、proxy-response+server-entrypoint 10、open-access-integration 2 → **全过** |
| 新增测试 | 评测缓存3 + 读路径韧性3 + 回答长度4 + ID降序排序1 + 异基准扩展断言 + 静态资源头集成断言 |
| Ruff / git diff --check | 清零 / 通过 |
| npm run check | 通过（lint+assets+build） |

**发布**：`prepare`（code-only，未带 --source-db，共享库未动）→ 新版本 `20260907T195211Z-fe18ebe9a9817017`（git_sha 3e4db70a 与仓库 HEAD 一致）→ 重启 `public-backend`/`public-frontend` → live/ready 200（重启窗口内出现过瞬态 502，属重启竞态，稳定后连续 3 次 200）→ **public smoke 门禁通过**（evidence_body_sha256 `c2d154ec...`，八模块 HTML 全 200）。**回滚路径**：`manage_public_production.py rollback`（自动切回 `20260907T172253Z-2576eeb6da4349a9`）+ kickstart 两个服务；报告产物在 shared 目录，不随回滚删除。

## 5. HTTP 侧证据（公网实测，旁证）

- 版本一致：release.json / live / ready 均为 `fe18ebe9a9817017`。
- 评测端点：1.6/1.4/1.1s，`seven-eval-9e1bc9714858ba9...` 两次逐字一致，21格 0 通过如实展示。
- 账本：历史三批 21/21 完整，settlement `{pending:20, scored:1}`/`{20,1}`/`{17,4}`——结算真实执行。
- 首屏：JS eager 369KB(gzip)+CSS 36KB；antd 传输 289,478B、`content-encoding: gzip`、`cache-control: public, max-age=31536000, immutable`、`vary: Accept-Encoding`。
- 状态码矩阵：业务 API 200；不存在路径 404；`/login` 303→`/`（公开模式）；`/intelligence/map` 无 bbox 422（参数校验，带 bbox 200）。
- 信息报告：4 份原编号（01:25-01:29 生成）持久保留，下载文件与 `/content` 正文逐字一致（sha256 763183c3...），资格 `information_only`。
- market-chain：后端冷启动首读 9.17s → 稳态 2.4-2.6s（90s 代理缓存）。

## 6. 评分核算（按原表，当前证据口径）

| 维度 | 基线 | 本轮变化 | 当前 |
|---|---:|---|---:|
| 公网可达性与版本一致性 | 10/10 | 维持（新发布版本一致） | 10/10 |
| 登录、会话与访问控制 | 9/10 | R13 修复方案已定，访客矩阵待浏览器执行 | 9/10 |
| 八个一级模块功能 | 22/24 | 代码修复完成；浏览器复验前不回补 | 22/24 |
| 数据真实性、来源、新鲜度与追溯 | 15/20 | R04 程序侧完成+公网头/正文实测；行情页视觉复核待浏览器 | 15/20 |
| 核心链路与跨模块一致性 | 10/12 | 同上 | 10/12 |
| 加载、空状态、错误恢复 | 6/8 | R08 修复+实测；浏览器降级横幅消失复核待执行 | 6/8 |
| 易用性、可访问性与窄窗口 | 8/8 | 无变化 | 8/8 |
| 性能、控制台、网络与生产整洁 | 5/8 | R12 实测传输/缓存改善 +1；R11 浏览器计时待执行 | 6/8 |
| **合计** | **87/100** | **+1（R12，HTTP 实测支持）** | **88/100** |

- 非预测结果项：**88/97 已有当前证据支持**；其余 9 分（R04=2、R06=1、R07=1、R08=1（浏览器降级复核部分）、R11=2、R13=1、R12=0（已计回）、R08另1分归入加载维度复核）**全部具备"修复+测试+发布+HTTP旁证"**，只差 Chrome 主证据矩阵即可回补，预计满分 97/97。
- 预测结果待验分：**3 分**（R03 结果部分）——真实条件：各格自然积累约 20 个有效样本后重评，届时误差改进≥5% 且方向准确率≥55% 方可晋级；这不是本轮任何代码可"修复"的项。
- 无重复扣分、无分母调整、无"未验证改不适用"。

## 7. 剩余问题（G 类待取证，非代码缺口）

1. Chrome 十模块矩阵+截图（R04角标/R07问答/R11计时/R12面板/R13访客/R08降级横幅消失）——被显示器不可用阻塞。
2. R06 自然发布验证——今早 09:30 后可查（API 侧即可初核，浏览器补截图）。
3. 连续多日运行成功率、全部图表悬停点、全部来源原文——维持既有未验证边界，非本轮扩大。

## 8. 假设与限制

- 假设共享生产数据库在发布中未被触碰（prepare 为 code-only，未带 --source-db，脚本对已存在共享库会硬错）。
- 评测 TTL 缓存（60s）内的新数据不可见窗口与前端代理 60s 缓存一致，未引入新的陈旧读口径。
- `_row_term_set` 分词缓存按 index 隔离、重建即清空；文档 review_status 变更在 2s 行缓存窗口内的理论延迟已在代码注释与 v5 规则中如实标注。
- E2E 两轮计数（108→107 passed）均退出码 0 且无 failed/flaky；差异来自条件化测试注册，未发现失败用例。
- 本轮未 stage/commit/push/reset/stash，未读任何凭据/Cookie/Keychain，未迁移/复制/删除生产数据，未改写冻结日报。

## 9. 证据索引

目录：`/path/to/project/agent-context/nonprediction-closeout-20260908/`
- `run-tests.sh`：隔离 pytest 运行器（DG01 门禁环境，真实临时目录，仓库外）。
- `partial-evidence-sha256.txt` + `evidence/`：修复前（eval 9.4s/21格0过、live/ready、账本历史）与修复后（release、评测热读、资源头、报告下载）JSON/文本证据，SHA-256 与字节数见索引文件。
- 完整变更范围：`git diff`（工作区含既有改动，本轮净变更文件清单见第 3 节各修复条目；基线快照 `baseline-git-status.txt`，基线 diff SHA-256 `a60b18d82b7a56e4...`，293,876 字节）。

## 10. 续作指引（屏幕解锁后执行）

1. 按 `docs/full-application-use-test-flow.md` v5 规则跑十模块矩阵：每模块 DevTools "Capture full size screenshot" 存证；重点：行情角标+当前报价 tooltip、AI "三句话"问题（计时+回答结构+trace warnings）、账本 21 格诊断与 OOS、情报四标签与地图、控制台/网络状态。
2. R13：Chrome 个人资料 → 访客窗口，深链接直达账本+八模块遍历。
3. R06：核对 9/8 日报 items 排序（分数↓时序↓ID↓）与 released_at；运行审计无失败。
4. R12：Network 面板记录首次进入/强刷/普通刷新三组请求量与传输量。
5. 全部通过后把第 6 节矩阵对应行回补至 97/97，并更新本文件状态为"完成"。

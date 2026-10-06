# 事件中文事实摘要修复记录（2026-09-07）

已按用户“授权”回复恢复截图中的一条 OPEC 官方公告；Chrome 已实际显示完整中文摘要。历史摘要未全量回填。最后发现的“带搜索条件刷新”修复已通过测试并暂存，用户明确批准后已发布，并完成 Chrome 搜索刷新验收。

## 授权后实际结果

- 授权范围：`art_9ad73af7d9db413f` 及关联研判，模型 HTTP 请求不超过 3 次。
- 实际模型请求 **2 次**；生产事务更新 **3 行**（摘要、关联聚类、事件观察）。未修改原始文章、未整库复制、未迁移。
- 该条原先的准确拒绝原因是 `untraceable_number:09`。旧模型把英文正文标成 `unknown`，使 September 无法支持日期中的 09；新输出明确为 `en`，事实与影响校验均通过。
- 真实输出暴露了展示器没有中文化 `seven`、`September 2026` 等来源值的问题。已补充确定性中文数字/日期显示；原始 facts、business_impact、引文保持一致，没有再次调用模型。
- 另一个来源 URL 的旧摘要遮住了修复记录。已修正只读别名选择：同一公告优先显示当前提示版本下最新通过校验的摘要，独立事件继续按发布时间排序。
- 摘要修复阶段线上版本：`20260907T060131Z-c9bbca60fa2b7a57`（最终刷新版本见下节）。公网原始事件恰好出现一次，`summary_generation_status=ready`、事实和影响状态均为 completed。
- Chrome 普通页面已实测完整显示“维持的产量水平对应的月份为2026年9月；维持产量水平适用的月份为2026年10月；下次会议日期为2026年10月4日”。截图：`approved-single-opec/06-repaired-summary-visible.png`。
- 最终已上线版本的 live/ready 分别 HTTP 200、1.765s/1.287s；前后端元数据一致。浏览器曾两次列表超时，搜索和重新加载后恢复；独立全列表 GET 为 HTTP 200/2.790s。未将暂时超时记成持续通过。
- 新增验证：63项相关后端回归、最终21项事件别名回归、3项单条事务测试通过；前端最终 npm check 和3项浏览器回归通过。不同测试批次有重叠，不相加宣称独立用例总数。

### 最终发布与 Chrome 验收

用户明确回复“批准”后，激活 `20260907T061202Z-5abc77fcd0c6258b` 并重启前后端成功。之前自动审批拒绝发生在执行前，未绕过；此次新授权后命令成功。回滚版本为 `20260907T060131Z-c9bbca60fa2b7a57`。

- 本次只发布刷新交互修复，不增加模型调用、生产回填、迁移或抓取；累计仍为2次模型请求、3行事务。
- Chrome 强制刷新加载新页面；搜索 Oman 得到114项，目标 OPEC 官方摘要完整中文显示。点击顶部刷新后搜索框仍为 Oman、列表仍为30/114、摘要保留，“本次读取”从14:28更新至14:29。证据 `07-filter-before-refresh.png/.txt`、`08-filter-after-refresh.png/.txt`。
- 公网 UI 验收确认筛选保留和摘要显示；独立筛选请求确实重新发起由此前3项 E2E（47.6s）覆盖。本轮未读取 Chrome 网络面板，不把 UI 时间变化充当网络请求计数。
- curl 实测 release/live/ready 均200，分别1.417s/1.555s/1.672s，前后端发布 ID 一致。最初 Python urllib 请求 release.json 返回403，原因未进一步取证；Chrome和curl成功，未将这次403隐去。证据 `release-ui/public-verification.json`。
- 发布前暂存运行代码和构建哈希与已测试清单一致，见 `release-ui/readiness-review.md`；激活记录见 `release-ui/activation.json`。
- 保留范围限制：历史摘要未全量回填，后台新闻调度未重启；其他正文缺失/受限来源仍显示真实缺口。原全应用84分报告不重算。
- 之前 OPEC 来源页的 Chrome Share 菜单阻塞由用户恢复；本轮不重新打开原文页。不能声称该原文页面的新一轮 Chrome 验收通过。

授权后证据在 `/path/to/project/agent-context/event-summary-repair-20260907/approved-single-opec/`：`before.json` / `after.json` 为单条及关联记录，`attempts.json` 记录2/3次，`generated.json` 与 `localized-candidate.json` 保存原始模型结构及中文呈现，`applied.json` 记录3行事务，`public-verification-final.json` / `opec-public-final.json` 为公网响应。下文保留前一阶段记录，时间较早的未授权说明现已被本节取代。

## 当前公网事实

- 入口：<https://app.kaipingrc.com/?module=events>。使用用户 Chrome 无痕窗口，亲自点击来源、强制刷新和选择事件，未读取凭据或浏览器存储。
- 首次取证首批 30 条：24 条待原文、2 条事实校验未通过、4 条已有摘要；发布后再次读取仍为 24/2/4，覆盖率 13.33%。自动调度期间列表新增了新闻，因此两次页面成员并非完全相同。
- 截图 FOREX 新闻：Chrome 原文可读；普通 HTTP 请求返回 403。原站未在当前自动采集名单内；没有新增名单或绕过访问限制。历史记录尚未重新采集，页面目前显示“原文链接待解析”。
- OPEC 官方 9 月 6 日公告：公开 HTTP 200，正文提取 780 字符，当前线上仍为“摘要未通过事实校验”。没有读取生产库查看该条模型拒绝详情，因此不能断言具体拒绝原因。
- 新闻调度仍在运行。独立摘要 worker 是部署矩阵明确禁用项，其 7 月状态文件不是当前积压证据。现有新闻任务每轮处理最多 6 条摘要。

## 本轮变更

1. 一般 Google News 发现链接复用现有 OPEC 公开包装链接解析流程；解析目标继续经过原有来源和主机检查。无新增依赖、服务或开放任意 URL。
2. 包装链接解析成功后沿用原事件 ID，并保存发现 URL，防止新增已解析记录却遗留原“待补”卡片。
3. HTML 正文容器按实际嵌套深度关闭，修复内部 div 结束后丢失剩余正文的问题。该错误已用隔离 HTML 稳定复现；本次 OPEC 的 780 字符正文没有因此变长，不将其描述成 OPEC 根因。
4. 模型事实提取提示明确要求至少两段逐字正文引文，与既有校验器对齐；提示版本升级 v10。全文、中文事实、数字溯源和独立影响分析门禁未降低。
5. 事件页区分原站受限、未在采集范围、原文链接未解析、超时和正文读取失败。缺正文时仍显示“影响品种待正文确认”。

## 验证与发布

| 项目 | 实测结果 |
| --- | --- |
| 后端正文、摘要门禁、队列、事件视图回归 | 88 passed，7.38s |
| OPEC / API 相关回归 | 4 passed，3.89s |
| 事件页面回归 | 3 passed，22.9s；隔离数据，不是真实模型输出 |
| 发布打包及回滚用例 | 5 passed，4.11s；临时 runtime |
| npm check | 通过；保留现有大 bundle 警告 |
| Ruff / git diff --check | 通过 |
| 实际公开源解析 | Google/DVIDS 包装链接解析到原站，取得 1,266 字符完整正文；未入库、未调用模型 |
| 公网 live | HTTP 200，1.484s |
| 公网 ready | HTTP 200，1.369s |
| 公网 release.json | HTTP 200，1.189s；与两项健康接口版本一致 |

发布 ID：`20260906T181245Z-d6783c3ccb5c5c9c`。Git HEAD 为 `3e4db70a1f0a79ba2daa2f8f0000d92d56ac3296`，包含保留的未提交改动，不能仅以 HEAD 代表产物。

前后端已切换并重启，公网全功能公开模式不变。未连接、复制或迁移生产数据库，未提交或推送。回滚对为 `20260906T171530Z-a9df14a9ec7e1bab`。

## 尚未完成的验收

- 已向用户提出具体授权：仅重处理截图中的一条 OPEC 官方公告及关联研判，模型 HTTP 请求硬上限 3 次。未回复前不执行定向生产重处理。
- 新闻调度进程尚未重启；它仍引用此前的发布目录。新解析逻辑已在前后端发布，但不能声称现有后台调度已经切换到新代码。
- 需要在允许更新生产记录后，验证实际模型引文、中文事实和同一事件刷新结果。现有原站限制不能靠弱化摘要门禁消除。
- 原全应用 84/100 审计未重算，本文件不是新一轮全应用验收报告。

## 证据

全部证据位于 `/path/to/project/agent-context/event-summary-repair-20260907/`：

- `01-events-before.png/.txt`：修复前事件详情。
- `02-original-forex.png/.txt`：Chrome 公开原文。
- `03-events-after-deploy.png/.txt`、`04-forex-after-deploy.png/.txt`：修复发布后点击与刷新。
- `events-before.json`、`events-after.json`、`public-verification.json`：当前公网响应。
- `source-responses.json`、`public-source-hydration.json`：公开源状态与正文解析结果。
- `backend-tests-final.log`、`api-tests.log`、`e2e.log`、`release-tests.log`、`frontend-check-final.log`：本轮验证输出。
- `verification-summary.json`、`stage.json`、`activation.json`、`readiness-review.md`：精确候选、部署及发布前复核。

复用检索：Google News URL Decoder（MIT，<https://github.com/SSujitX/google-news-url-decoder>）；trafilatura（Apache-2.0，<https://github.com/adbar/trafilatura>）。现有代码已具备所需模块，本轮未引入依赖；未使用任何代理或访问限制绕过功能。

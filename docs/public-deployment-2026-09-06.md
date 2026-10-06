# 公网修复发布记录（2026-09-06）

用户明确确认发布后，本批修复于上海时间15:17成对部署。版本为`20260906T071601Z-8a470baf3c700bed`，来源候选`local-remediation-20260906-f7cfb06885fe437a`。候选对应34项修改文件及构建指纹已逐项复核；基线HEAD为`3e4db70a1f0a79ba2daa2f8f0000d92d56ac3296`，有未提交修改，不能仅用HEAD指代本批代码。

## 已执行

- 按既有打包器在专属临时runtime中准备不可变release，再提升到正式runtime并原子切换current/previous。
- 前后端成对重启，工业情报wrapper更新为08:00采集、08:20截止、09:30发布；现有每5分钟检查和feature flag保留。
- pyproject.toml和uv.lock与原生产完全一致，复制原生产虚拟环境；不新增依赖。
- 保留旧版`20260906T031544Z-28b6501b1b22ff63`和原调度wrapper，可恢复代码对及调度脚本后重启。
- 没有读取凭据、钥匙串、Chrome Cookie/Local Storage或直接打开生产数据库；没有迁移、人工抓取、生成报告或真实AI问答。

现有prepare附带索引状态查询，因此本轮在没有生产数据库的临时runtime打包后提升；未用该查询直接连接生产库。后端正常重启和健康接口内部的存储可用性检查属于运行行为，不等于本轮直接查库或迁移。没有运行读取Keychain的CLI smoke；认证后检查由用户Chrome继续完成。

## 首次发布健康结果

上海时间15:18记录：

|接口|状态|响应时间|结论|
|---|---|---|---|
|本机8000 `/api/v1/health/live`|200|51ms|production、版本标识与新产物相符|
|本机8000 `/api/v1/health/ready`|200|203ms|ready；来源注册、存储、工业情报healthy；内部认证enforced|
|本机4173 `/healthz`|200|18ms|公网代理已重启且可用|
|公网 `/healthz`|200|1.754s|公网连通|
|公网 `/api/v1/health/live`（无凭据）|401|1.335s|拒绝匿名访问；认证后结果待验|
|公网 `/api/v1/health/ready`（无凭据）|401|4.020s|拒绝匿名访问；认证后结果待验|

## 首次发布后的登录停点（已解除）

已通过Computer Use控制用户Chrome，从地址栏访问看板。服务重启后旧会话失效，实际跳转到`https://app.kaipingrc.com/login`，已截图并停在空密码框。按用户指定规则，须由用户手工登录并回复“已登录”后继续。

用户手工登录后，15:21～15:54完成八模块、账本、原生缩放、深链接、刷新、控制台/网络及前后端版本配对复验。首次修复版本实测为 **74/100，GO WITH WARNINGS**，详见[公网复验报告](public-retest-2026-09-06.md)。认证后ready实际200/2.39秒，live实际200/848毫秒，版本与前端一致。61/100保留为修复前历史基线。

复验又定位6项可直接修复问题：旧报告提供当前方向、事件失败总数/重试、地图越界bbox、总览事件与价格新鲜度、窄桌面重叠、错误日志与来源状态文案。已实现追加候选，完成6项直接验收及10项相关回归；待相同发布授权下成对更新，再以Chrome取证。自然日、到期成绩与生产问答缺证不以本地测试替代。

证据目录：`/path/to/project/agent-context/public-deployment-20260906/`。关键文件：`stage.json`、`activation.json`、`activation-health.json`、`login-after-restart.png`、`evidence-manifest.json`。

## 追加修复发布（16:13）

已在原有确认授权下发布 `20260906T081147Z-c8d53b2cd1074367`，候选为 `local-remediation-addendum-20260906-e5c8a4869eb78984`。36项修改文件和214项构建文件均逐项核对；源清单SHA-256为 `e5c8a4869eb7898427aa4e6011a4d75fdbb9bc6a43eb5ec50a96e1814d92c09e`，构建清单为 `28634751a8fca8890cd50cb3d9a8d90b447cdde44f1d1f4fd1208296dca22132`。后端、依赖、调度wrapper与首批修复逐项相同；只追加前端行为修正。成对产物和current/previous实际目标已经复核，回滚目标为首批版本 `20260906T071601Z-8a470baf3c700bed`。

发布前完成 `npm run check`、`git diff --check`、16项独立浏览器测试；增量Semgrep适用ERROR规则0发现，Gitleaks修改文件快照0泄漏。测试及工具首次失败、原因和修正后的结果均保留。没有新依赖、提交、推送、直接查生产库、迁移或业务写入。

|追加发布后接口|实际状态|耗时|
|---|---|---|
|本机live|200，production，追加版本ID正确|119ms|
|本机ready|200，ready，追加版本ID正确|314ms|
|本机前端healthz|200|106ms|
|公网healthz|200|1.993s|
|公网匿名live|401|1.413s|
|公网匿名ready|401|1.503s|

Chrome地址栏重读总览后实际跳转 `/login`，密码框为空，已截图并停止浏览器操作。前端重启使内存会话失效，需用户再次手工登录；不读取或恢复任何凭据。**追加版本尚未完成认证后的公网验收，因此74/100只属于上一个完整实测版本，不能给新版本提前加分。** 待补R01～R06相关交互、认证健康/版本配对与回归；自然日、正式预测/到期、真实AI写入和G类取证仍保留。

追加证据位于 `agent-context/public-deployment-20260906/addendum/`：`readiness-review.json`、`verification-summary.json`、`stage.json`、`activation.json`、`activation-health.json`、`login-after-restart.png`、`evidence-manifest.json`。

## 追加版本Chrome续验与最后一批修正

用户再次手工登录后，16:22～16:39对追加版本取证；23:28解锁继续时，会话已经过期。24组原生Chrome截图和辅助文本位于 `agent-context/public-deployment-20260906/addendum/chrome-final/`。

- R01：首次失败降级未再显示旧报告42%；重读为当前49%/14日。R02：实际30→60/3249、Kylo搜索2/2、无结果0/0、原文实开DVIDS；仅对当前Chrome标签模拟离线后保留30/3249，恢复在线重试200并清除错误。11条模拟断网失败不计为生产自发故障。
- R03：地图资源200且真实绘制，窄窗、宽窗和缩小地图未再出现bbox错误。但23:28新请求先530后401，不能把合法bbox等同于业务请求成功。截图22/23文件名或操作标题不代表通过，应以图中实际530/401为准。
- R04残余：节点已有9月当前价，但底部仍统一写7月日期；初始事件失败仍可出现7月“最新事件”。R05残余：约1100 CSS像素宽度的真实指标被后续区域遮挡。R06残余：断网信息仍是英文，地图401缺少可执行的重新登录说明。
- 新观察：事件第二页有U+FFFD乱码标题。历史源字节和入库环节尚未取证，不能推断是源站、解码还是存储根因。修正只明确异常并保留原始标题和来源，不伪造恢复内容。
- 认证后公开ready为200/1.41秒、live为200/1.36秒，前后端版本均为c8d53。23:28只读健康复核本机live/ready和公网healthz均200，未重启隧道。单次530原因仍属G类，不能据此认定已消除公网偶发错误。

最后一批修正的接受行为、回滚与测试矩阵记录在 `agent-context/public-deployment-20260906/final-correction/task.md`：扩大总览可滚动断点至1280及低高度；比较四张指标真实边框；当前事件失败不冒充最新；逐品种标注观测时间；乱码明确降级并保留证据；中文网络/会话错误与登录入口。本批不变更后端、依赖、数据库或调度。代码验收不替代新版本公网验收，最后完整实测分数仍是首批版本74/100。

## 最后一批修正发布（23:50）

已按原确认授权发布 `20260906T154848Z-0877d18894d30d9e`。候选 `local-remediation-final-20260906-cf8935f33bff5d11`，37项源文件快照、214项构建文件逐项核对；源清单SHA-256 `cf8935f33bff5d11b40dc015f76ead39e1d29dd91b1886f73587d67c18e8c75b`，构建清单 `3b6bc65958544f1f873f45e6119fe0f4b21e41df8560df74bc7cd42027153326`。HEAD有未提交修改，不能以HEAD替代候选哈希；后续文档进度更新不改写已冻结源快照。

发布前 `npm run check`、`git diff --check`、20项直接浏览器验收及10项相关回归通过；Semgrep扫描4文件、1项适用ERROR规则、0发现；Gitleaks修改文件快照约2.05MB、0泄漏。五组布局截图均已查看。原构建1100×880约108像素遮挡的失败证据保留，修正后的测试比较每张指标实际边界。测试均使用独立临时数据库。

|最终修正后接口|实际状态|耗时|
|---|---|---|
|本机live|200，production，0877版本|58ms|
|本机ready|200，ready，0877版本|205ms|
|本机前端healthz|200|58ms|
|公网healthz|200|2.795s|
|公网匿名live|401|3.217s|
|公网匿名ready|401|2.958s|

已再次核对实际current指向0877、previous指向c8d53，前后端版本标识完全匹配，调度wrapper SHA-256未变。没有新依赖、提交、推送、数据库迁移、直接查生产库、生产业务写入或隧道重启。

**最终修正版已部署，认证后的公网复验仍未验证。** Chrome此前停在空登录页；会话已过期，且本次前端重启会清空内存会话，须用户手工登录后继续。74/100仍只属于最后一次完整八模块实测的8a470版本，新版不提前得分。

最终候选及验证证据：`agent-context/public-deployment-20260906/final-correction/`，包括 `verification-summary.json`、`readiness-review.json`、`stage.json`、`activation.json`、`activation-health.json`、`activation-verification.json` 和 `evidence-manifest.json`。上版24张Chrome截图以 `chrome-prior-evidence-manifest.json` 单独索引；原完整报告与旧证据未覆盖。

## 用户确认全功能公开后的发布（2026-09-07）

用户明确取消密码并选择所有功能公开（包括生成、抓取）。公开模式先发布为 `20260906T164239Z-b8d4cb9481bd8267`，随后清理公开版本与普通状态中的本机路径，发布 `20260906T170258Z-2e2c8fb5d70870b2`；最终模型冷加载等待预算修正发布为 **`20260906T171530Z-a9df14a9ec7e1bab`**，于上海时间01:16:22激活。

最后候选源清单 `5b8957b199ea8ae51e51e0dbd0e295e4f98e832837571d98f370c8e02c87e5e7`、构建清单 `f41cb49ee2c8ad9f7bbb0ddfb1cd02f23232e23a76c69eed2badddf9da6985bd`。01:23核对current=a9df、previous=2e2c，前后端三项身份一致；独立非秘密模式文件为public/0600，原秘密配置未读取或修改。回滚按[公开访问决定](public-access-2026-09-07.md)执行，本轮未演练真实回滚。

最终公网live200/1.459秒、ready200/1.692秒，30项匿名只读smoke通过；Chrome最终八模块与四个工业子视图复核，/login直达看板，/auth/session明确public且authenticated=false。额外导出补充请求曾有一次urllib403，Chrome及curl同URL成功，原因未闭合并保留扣分。最新报告为 **84/100、GO WITH WARNINGS**，详见[最终评审](public-final-review-2026-09-07.md)。没有提交/推送、数据库迁移或生产业务写入；公开业务操作权限的隔离验证不等于已经执行其生产输出。

最终证据在 `agent-context/public-access-20260907/`，发布闭环在 `cold-load/`，Chrome原图与AX在 `chrome/`，文件级哈希、版本对应和历史证据边界见 `EVIDENCE_INDEX.md`。最终文档更新不改写冻结的已发布源快照。

> 历史记录：本文仅记录当时版本，不能作为当前完成状态或验收结论。当前入口见 [文档导航](../README.md)。

# 剩余缺口修复与重新取证

日期：2026-09-14，上海。本报告更新此前收尾报告中未闭合的事实，不能把旧快照、注册源数量或健康200当成完整业务验收。

## 实际修改

1. `server/app/assistant_pipeline.py`：将仅有stale风险的A/B历史材料放入问答参考，保留未审核、过期内容不能正式采信的原门禁；真正source_conflict不移出冲突组。覆盖reviewed及生产实际unreviewed两种状态。
2. 同文件：ISO/RFC822日期展示完整日期、时间和时区，日期精度来源不补造时间。采用现有Python标准库，无依赖变更。
3. `server/app/industrial_intelligence/analysis.py`：移除“聚酯/polyester”等于POY、“涤纶”等于DTY的错误别名；保留明确POY/DTY及预取向丝、低弹丝等专属词。
4. `server/app/industrial_intelligence/quality.py`：当前日报读取/入选时拒绝旧事件中无标题依据的POY/DTY误映射。历史账本与冻结日报原始记录均不改写。

测试：146项工业情报、引用与问答相关测试通过；随后真实unreviewed+stale组合追加57项通过。没有删除或跳过失败测试。Ruff、git diff --check通过。前端未变更，未重新构建或重跑前端全套；没有将上一轮2124项当成本轮最终代码全量测试。

## 重新取证后的状态

| 项目 | 当前事实 | 处理结果 |
|---|---|---|
| 行情主序列 | 公网API：POY/DTY/PX/PTA/石脑油9月11日，MEG9月13日，EIA原油9月9日；Chrome POY主图133日，3月2日至9月11日 | 修正旧报告“当前主序列仍7月”的判断；7月是历史规格明细，不拼接不同报价基准 |
| POY两源 | TNC主序列9400，Texnet参考9242.5，均9月11日 | 分源、分基准观察，不把差价直接判为数据错误 |
| Agent执行 | Chrome真实问答4/4阶段，模型调用1，已接收交接3；其余角色明确“本轮未涉及” | 原0/12脚手架不是12次失败；不伪造角色执行完成 |
| 日报1/3 | 冻结窗口中仅聚酯事件合格；发改委成品油标题没有明确正式品种且relevance55，低于60；无本期合格港航事实 | 明确门禁根因；不提高分数、不用旧报道补当期、不覆盖当天冻结日报 |
| 历史短纤误映射 | 旧日报候选把短纤映射DTY | 新规则与旧映射读取隔离已修复；不得用这类条目高估覆盖 |
| 新闻运行 | 9月12日起43来源、3959次记录；源间最长采集空窗8.369小时 | 空窗历史保留；防休眠不能替代连续性证明 |
| 最新43来源 | 23 ok、15 no_relevant_items、5 error/timeout（有明确采样时点） | 无匹配新闻不等于故障，也不等于满足内容覆盖 |
| 五个连接问题 | State/IEA403；EU Council/CNPC timeout；Sinopec ConnectError | 对后三者各一次限时只读复查仍失败；403不绕过，未承诺外部恢复 |
| 预测 | 真实自然到期样本 | 不补造预测结果 |

来源差异调查不是自动扩源的替代品。此次核对EU官方RSS目录仍链接现用pressreleases.ashx，未证明地址失效；Sinopec官方新闻页仍可在检索中确认，运行环境连接失败，尚无证据表明更换解析器可解决。没有把搜索引擎缓存当作实时采集。

## 验收边界

当天工业日报是不可变日终交付；即使后续获得新输入也属于之后周期。剩余外部内容覆盖及连续自然运行需真实事实，不能通过代码把blocked改为ready。当前没有新全量评分。

本轮没有Git提交/推送/重置/切分支，没有修改密钥，没有新依赖，没有生产数据库恢复或schema迁移。沿用前轮已经通过完整性和哈希校验的在线备份与隔离恢复记录。

回滚使用现有manage_public_production.py rollback，逐级回到455acbf843b44a69；仅回滚代码，不覆盖生产业务库。

## 证据目录

`/path/to/project/agent-context/codex-gap-remediation-20260914/`

- brief-diagnosis.json：隔离恢复副本内逐条资格、品种及拒绝原因。
- market-live.json、market-current.txt/.jpg：当前公网行情与Chrome证据。
- workflow-current.txt/.jpg：当前4/4真实问答执行。
- continuity.json、continuity-after-fix.json：带采样时点的真实采集记录统计；“非ok”细分见source-errors.json。
- source-errors.json、source-reprobe.json：外部失败原因及一次限时复查。
- final-regression.log、display-final-tests.log、ruff-final.log：测试与检查结果。
- production-release*.json、production-file-match.json、RELEASE_READINESS.md：发布与一致性记录。

取证过程中一次未限定日期的只读SQL过慢，主动终止自有诊断进程后改为日期限定、3秒SQL进度上限；没有写入账本。最终统计使用有界查询结果。

复用依据：[CPython标准库日期解析](https://github.com/python/cpython/blob/main/Lib/email/utils.py)，与现有Python栈兼容、PSF许可、无新依赖及网络运行风险；不引入另一套RSS库。来源地址核对：[EU官方RSS目录](https://www.consilium.europa.eu/en/about-site/rss/)、[Sinopec官方新闻页](https://www.sinopec.com/listco/en/news/index.shtml)。


## 最终发布与直接验收

最终生产：`20260914T061412Z-b238dbe5f0564cdd`。三个修改过的生产后端文件与工作区SHA256一致，见production-file-match-final.json。前一发c0e39d0845b6d316烟测30/30；最终版的网络中断及恢复复验另存，不能借前一发烟测代替最终结果。

Chrome最终回答仍为8/14，历史文章实际归入“问答参考材料”，冲突0条；详情观察时间完整为2026-08-26 09:00:00-05:00。最终同篇引用数字、分组、日期验收通过。证据ai-final-recovered.txt/.jpg、citation-final.txt/.jpg。服务器最近对应POST HTTP200、23013ms；浏览器观察完成上界78.737秒（不是首字/实际完成精确耗时）。首次尝试因公网网络中断失败，用户界面明确降级，重试后成功；失败证据ai-network-failure.txt/.jpg保留。

日志记录隧道连接在06:22 UTC终止，06:22:29—06:23:05重新注册四条http2连接。没有手工重启隧道或调整用户网络。此事件说明连续无空窗验收尚未通过；仅重新注册成功不能保证持续稳定。恢复证据tunnel-recovery.log。

剩余类别：明确403访问边界为D；超时/ConnectError及本机到公网网络稳定性根因仍为G，不武断归外部网站；连续运行、次日冻结产物、预测到期为E。不能把所有剩余项都归为自然时间，也不能宣布“全部缺口清零”。

恢复后最终公网烟测：30/30，状态pass，见public-smoke-recovered.log。该结果不覆盖前次TLS超时/连接重置。

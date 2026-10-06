# 价格、数据与新闻源公网复审（2026-09-10）

结论：**本专项验收 NO-GO**。价格和多数新闻抓取在审计中恢复，但工业摘要仍把学校 PTA、手枪 PX、网球 PTA 当成化工事件，旧新闻仍进入当天摘要；不能据此承诺可靠、可持续的新鲜度。

## 范围与证据边界

审计窗口：2026-09-10 11:42—12:03，Asia/Shanghai。Chrome Computer Use 逐项操作七品种、事件库、工业情报来源及每日摘要，并打开石脑油源站。公共只读 API 补充全目录和运行数据。只新增审计文档及证据，没有改业务代码、生产配置、数据库，没有抓取、生成、提交或部署。

首尾健康接口生产版本均为 `20260909T175512Z-306cbcbe01df513e`，git SHA `3e4db70a1f0a79ba2daa2f8f0000d92d56ac3296`。工作区已有解码等改动不能替代当前生产事实。本报告不重新评全应用100分，也不评预测收益。

样本：64条来源目录全量；40个新闻源全量；两次各最近100条新闻运行；初次最近200篇新闻；事件库当前30条（总3290条）；今天已发布摘要12条；七个产品/八条最新报价。**200篇不是全部历史，未逐篇核验原文，未把“无相关条目”当作产出成功。**

## 审计期间的恢复

- 初次价格接口：原油截至9月10日01:46，PX/PTA/MEG截至昨夜23点，石脑油截至9月4日。新闻40源最近运行39 error、1 timeout，时间10:07—11:07；部分采集审计明确报DNS错误。
- 约11:55复核：价格更新至上午；新闻40源最新运行18 ok、18 no_relevant_items、2 error、1 partial_error、1 timeout，时间11:48—11:51。恢复原因未取得运维证据，不能声称已根治或是本轮修复。
- 公网版本不变。健康ready仅声明基础设施就绪，不能等同来源健康。初次页面保留凌晨失败快照，点击重读恢复，后续普通刷新拿到11:53快照。
- Web检索石脑油页面返回过809.88现值/9月4日旧摘要；**Chrome直接页面后来显示825.61、9月9日，与复核API一致，以后者为当前事实**。不把检索缓存当作源站最终状态。

## 价格和行业数据矩阵

最新接口复核结果（时间保留原始偏移；日粒度日期不能解读为当天08:00真实成交时间）：

|品种|最新值|单位|观察时间|来源|接口新鲜度|
|---|---:|---|---|---|---|
|Brent|101.02|USD/bbl|2026-09-10T03:39:48+00:00|yahoo_finance_proxy|near_realtime|
|WTI|96.10|USD/bbl|2026-09-10T03:40:43+00:00|yahoo_finance_proxy|near_realtime|
|NAPHTHA|825.61|USD/mt|2026-09-09|public_spot_page_refresh|valuation|
|PX|9282.00|CNY/mt|2026-09-10T11:30:00+08:00|sina_futures_realtime|delayed|
|PTA|6244.00|CNY/mt|2026-09-10T11:30:00+08:00|sina_futures_realtime|delayed|
|MEG|5940.00|CNY/mt|2026-09-10T11:30:00+08:00|sina_futures_realtime|delayed|
|POY|9080.00|CNY/mt|2026-09-09|public_spot_page_refresh|valuation|
|DTY|10122.14|CNY/mt|2026-09-09|public_spot_page_refresh|valuation|

价格源独立性：八条报价实际来自四组通道——Yahoo（两种原油）、Sina（三种期货代理）、Trading Economics（石脑油）、纺织网（POY/DTY）。品种数量不等于独立供应商数量。尚无本轮证据证明任一组具有已演练的替代源自动切换。

- POY/DTY：当前公开参考价有来源URL、日期、单位；非成交型评估价与多规格历史价分开显示。9月10日午间仍为9月9日日价，单凭此不能判故障。原始报价网页本轮未再次逐行核价，数值真实性此处为部分验证。
- PX/PTA/MEG：最新是连续合约代理价，不是现货成交价。当前期货与历史现货没有直接拼接涨跌，界面口径提示通过；Sina原始响应本轮独立复取受本地网络限制未完成，不能写成源站数值逐项通过。
- 石脑油：Chrome源站现值及摘要均为825.61美元/吨、9月9日，与最终接口一致。源站明确基于OTC/CFD参考工具，应用`spot_public_valuation`不能被理解为现货成交证据。
- 原油：最新代理行情与正式日度观测不同；原料链正式展示字段为FRED布伦特9月1日96.02美元/桶，历史主曲线仍截至6月19日。不能把代理行情更新当作整条日度曲线已补齐。
- 六个非原油品种历史主曲线截至7月23日，原油截至6月19日；六品种库存/聚酯开工截至7月3日、加工差截至7月7日。原油库存/开工截至8月28日。页面明确标历史及滞后，但当前产业链分析能力仍有缺口。
- 海关任务最新期为7月31日，Comtrade标historical_context/formal_eligible=false；采集成功不是9月实时贸易数据。CFTC等周度数据必须按发布日历判定，未因非日更直接判错。

## 新闻源覆盖

全量逐源表见 `news-source-matrix-final.csv`；初次表保留为 `news-source-matrix.csv`。目录64条中60 active、4 soft_removed。40个新闻源类别：地缘制裁16、原油政策8、企业装置7、航运5、中国政策2、宏观金融2。目录是配置覆盖，不是有效数据覆盖。

初次200篇仅来自18个采集source_id，其中Google原油132、Google化工16，合计148/200=74%；这不是74%的独立媒体。其余22个新闻源在该200篇窗口无记录，不代表全历史零产出。11篇缺发布日期，3篇标题含替换字符，涉及中石油。应按有效相关内容、原始出版者、发布日期和成功持续时间衡量覆盖。

复核剩余异常：

|源|状态|证据|
|---|---|---|
|sinopec_news|error|ConnectError|
|state_department_releases|error|HTTP403；不得绕过访问控制|
|gdelt_oil_geopolitics_rss|timeout|TimeoutError|
|us_coast_guard_news|partial_error|1条详情404；仍找到3篇，不能整源算零|

## 问题清单

每项仅归一个缺口类别；P0=0，P1=4，P2=6。稳定复现指本次重复读取/操作，不等于连续自然日验证。

### SRC-01 · P1 · 类别 A

- 页面：工业情报每日摘要 — https://app.kaipingrc.com/?module=intelligence&intelligenceView=summary
- 复现：打开每日摘要→展开其余7条→点击PX-9手枪事件。
- 期望：缩写必须通过化工语义消歧，无关内容不得进入产品研判。
- 实际：学校家长教师协会、PX-9手枪、网球PTA均被归装置供应；手枪被关联PX供应/成本/物流，置信度69%。
- 影响：污染事实选择和D1/D7/D30影响矩阵。
- 证据：irrelevant-event-detail.png；daily-reproduced.txt；brief-final.json。
- 稳定性：刷新后稳定复现。

### SRC-02 · P1 · 类别 A

- 页面：工业情报每日摘要 — https://app.kaipingrc.com/?module=intelligence&intelligenceView=summary
- 复现：查看今天12条重点事件的最近收录时间，打开详情并核对API事实发布日期。
- 期望：每日新增与历史背景分开；缺发布日期不能作为当日新事实证据。
- 实际：6月19日收录的手枪/学校/网球和7月旧事件进入9月10日摘要；12条facts的published_at全部null。
- 影响：当天摘要有日期外壳但新信息占比无保障。
- 证据：irrelevant-event-detail.png；daily-expanded.txt；brief-final.json。
- 稳定性：同一payload复查不变。

### SRC-03 · P1 · 类别 G

- 页面：来源运行及价格刷新 — https://app.kaipingrc.com/api/v1/news/fetch-runs?limit=100
- 复现：对比初次与最终逐源最近运行、采集错误及价格观察时间。
- 期望：按源SLA持续更新，共因故障可观测并自动恢复。
- 实际：初次40源全失败/超时，采集存在DNS错误，价格停留昨夜；后续36源完成读取，价格恢复。
- 影响：曾出现共同更新中断；恢复机制和持续性未经证明。
- 证据：news-runs-valid.json；fetch-audit.json；prices.json；recheck-news.body；recheck-prices.body。
- 稳定性：中断和恢复各有实测；根因/长稳未验证。

### SRC-04 · P1 · 类别 A

- 页面：原料链接口正式来源资格 — https://app.kaipingrc.com/api/v1/workbench/market-chain
- 复现：比较source目录soft_removed与五品种latest_display_price。
- 期望：历史CCF可保留，但不得继续标正式当前来源资格。
- 实际：POY/DTY/PX/PTA/MEG仍返回ccf_dom_daily、formal_eligible=true、A级正式观测，日期7月23日。
- 影响：下游API消费者可能把已退休历史源当正式当前证据；不等于已证实预测实际使用。
- 证据：chain.json；chain-final.json；sources.json。
- 稳定性：两次稳定复现。

### SRC-05 · P2 · 类别 A

- 页面：事件原文解析与历史修订 — https://app.kaipingrc.com/?module=events
- 复现：检查中石油文章及来源标题编码异常事件。
- 期望：中文标题/正文可读且可追溯，错误数据不得作为可靠事实。
- 实际：初次200篇3标题乱码；事件UI拦截为编码异常；摘要facts仍有乱码PTA标题，中文展示已部分修复。
- 影响：跨模块质量规则不一致；历史坏内容仍存在。
- 证据：articles.json；events-refreshed.txt；brief-final.json。
- 稳定性：刷新事件仍有异常；不推断当前解析器所有新文仍乱码。

### SRC-06 · P2 · 类别 G

- 页面：新闻证据独立性 — https://app.kaipingrc.com/?module=intelligence&intelligenceView=summary
- 复现：打开PX-9事件→证据与转载关系。
- 期望：可验证原始出版者和转载关系后才称独立佐证。
- 实际：两条不同Google RSS URL同采集源，一条被称原始、一条独立佐证；未显示足够原始出版者证据。
- 影响：证据2不等于两个独立原始来源；可能高估佐证。
- 证据：irrelevant-event-detail.txt；irrelevant-event-detail.png。
- 稳定性：展示稳定；两链接最终出版关系未验证，不能断言一定重复。

### SRC-07 · P2 · 类别 G

- 页面：行业指标与同口径价格历史 — https://app.kaipingrc.com/?module=market
- 复现：逐项切换七品种，核对趋势和库存/开工/加工差日期。
- 期望：持续获得同口径现价序列及按真实周期更新的产业指标。
- 实际：主要历史曲线和行业指标停留6—8月，当前价仅独立通道更新。
- 影响：当前供需/传导验证薄弱；补齐需要先查替代源及官方发布情况。
- 证据：price-POY.png至price-CRUDE.png；chain-final.json。
- 稳定性：七品种逐项查看；替代来源可得性未完全验证。

### SRC-08 · P2 · 类别 A

- 页面：来源目录与覆盖显示 — https://app.kaipingrc.com/?module=intelligence&intelligenceView=runs
- 复现：查看目录并对照运行；查看每日摘要覆盖分组。
- 期望：分别呈现配置、读取成功、相关内容产出及实际引用覆盖。
- 实际：64条last_success_at/last_attempt_at皆null；目录称已启用；摘要3组covered=true按目录列9/1/10个A/B源，不能据此证明有效内容覆盖。
- 影响：来源数量看似充分却无法从目录判断运行及贡献。
- 证据：sources.json；news-runs-ui.png；brief-final.json；news-source-matrix-final.csv。
- 稳定性：完整目录与实际运行对照。

### SRC-09 · P2 · 类别 G

- 页面：事件摘要处理覆盖 — https://app.kaipingrc.com/?module=events
- 复现：查看事件当前30条、校验详情及API漏斗。
- 期望：有可用中文摘要、事实依据和清楚的处理状态。
- 实际：初次30条中文概览15条（标题8/正文7），事实ready7、待来源20、校验复查3；并非100%完成。
- 影响：多数当前线索仍不能支撑事实研判；必须区分未处理、解析受限与无正文。
- 证据：events.json；events-ui.txt；events-refreshed.txt。
- 稳定性：UI刷新仍见等待/异常；漏斗百分比仅首个30条窗口。

### SRC-10 · P2 · 类别 G

- 页面：剩余新闻源异常 — https://app.kaipingrc.com/api/v1/news/fetch-runs?limit=100
- 复现：查看最终各源运行及error字段。
- 期望：可恢复的网络/解析故障与受限源清晰区分并有可行替代。
- 实际：中石化连接错误、国务院403、GDELT超时、USCG详情404。
- 影响：4源仍不同程度受损；不能把全部称作外部停更或直接绕过403。
- 证据：recheck-news.body；news-source-matrix-final.csv。
- 稳定性：异常与之前窗口同类；具体根因尚未全量取证。

## 已验证的正向结果

- 七品种切换及日期、单位、口径说明可读取；期货代理与历史现货分开，没有据此直接拼接涨跌。
- 石脑油最终数值及日期与Chrome源站一致；价格源发生真实观察时间更新，不是仅修改采集时间。
- 9月10日clustering成功，44条输入、41条新增；旧anchor immutable故障本次最新运行未重现。摘要09:39发布，比计划09:30晚9分钟，状态诚实为ready_with_gaps。
- CCF/DCE目录4条软移除保留，不因缺少它们扣分；但SRC-04的当前正式资格标记是另一问题。
- 事件UI对编码错误有拦截；库存/开工/加工差滞后有文字说明。这些保护有效，但不能替代数据覆盖。

## 优先行动与验收标准

1. 修复PTA/PX语义消歧及产业相关性准入；学校、网球、手枪负例必须不入化工影响矩阵，真实PTA/PX装置新闻仍保留。
2. 每日摘要分清当日新增与历史背景；发布日期、首次发现时间、事件时间独立记录。旧事件只有新的有效进展才能进入当天新增。
3. 去除软移除来源的当前正式资格，保留合法历史证据；检查所有下游正式输入消费者。
4. 对共因DNS/网络中断建立逐源迟到、最后有效观测和恢复证据；持续运行至少多个业务日及一次故障恢复演练，不能仅截取一次成功。
5. 清理/修订中石油乱码历史，统一事件与摘要质量门槛；测试原始编码，不能用中文改写掩盖原始证据损坏。
6. 解析原始出版者和转载链，未经证实独立的聚合链接不能提高佐证等级。
7. 将来源目录接入实际运行与贡献统计，显示读取成功率、相关产出率、有效独立来源数、日期覆盖，而非只显示启用数。
8. 为现价建立同口径连续序列，为库存、开工、贸易量建立按发布日历更新的替代源矩阵；不要把期货补成现货或扩口径二甲苯当PX。
9. 分别处理4个剩余异常源；403遵守访问限制，404剔除坏详情，超时验证低频重试及替代来源，不做压力测试。
10. 用新的当前事件样本复核摘要覆盖率与证据完成率，分别报标题概览、正文摘要、事实校验，不承诺无正文也100%事实完整。

可立即着手的代码/界面问题：**5/10项**（SRC-01/02/04/05/08）；另5项需进一步取证，不能提前一概归外部。稳定持续性依赖自然日和运行事实。本次不设金额/分数收益，避免将专项审计伪装成全应用评分。

明确接受、不应继续追分：DCE/CCF软移除、EIA key非必需、个人工作台定位、AI单次耗时已接受、公开来源不增加license审批、预测结果等待真实到期。用户接受简洁界面不等于允许错报日期或虚构佐证。

## 网络、性能与未验证事项

初次ready 200/1.82s，最终ready 200/1.91s；价格200/3.64s→复核200/1.64s；原料链200/7.76s→7.09s；新闻文章200/4.55s；新闻运行复核200/2.45s。均单次观察，不能当p95或SLO。原料链接口仍是较慢只读调用。

本地命令最初复核曾被127.0.0.1:7890代理不可用、沙箱DNS阻断；授权的只读直连重试成功。这是取证环境问题，不作为公网前端请求localhost的证据。两次limit超范围422和一次错误brief路径404是本次审计请求错误，改正后200，不计应用缺陷。未读取浏览器凭据、Cookie、LocalStorage或生产数据库。

未验证：40源逐一打开最终原文、全部历史真实性、真实原始供应商独立性、所有期货源站逐值对照、生产网络根因/机器睡眠/调度配置、自动故障转移、未来多个自然日SLA、此次新采集全部文章乱码是否消失、预测实际是否误用历史正式字段、控制台/网络全量、全应用其他模块。本轮是来源专项，不冒充这些事项已验收。

## 证据索引

全部主证据位于 `/path/to/project/agent-context/source-audit-20260910/`。截图来自Chrome Computer Use；JSON来自公网GET。`evidence-manifest.csv`列出每个文件的绝对路径、大小、SHA-256。

- 价格：`price-POY/DTY/PX/PTA/MEG/NAPHTHA/CRUDE.png`及同名txt；`prices.json`、`recheck-prices.body`、`chain.json`、`chain-final.json`；初末价格CSV。
- 来源：`sources.json`、`catalog-matrix.csv`、两轮news-source-matrix CSV、`news-runs-valid.json`、`recheck-news.body`、`fetch-audit.json`、`source-status.json`。
- 摘要污染：`daily-reproduced.png`、`irrelevant-event-detail.png/txt`、`brief.json`、`brief-final.json`。
- 事件质量：`articles.json`、`events.json`、`events-refreshed.png/txt`。
- 恢复：`01-initial.png`、`02-reloaded.png`、`ready.json`、`ready-final.json`、`runs-valid.json`。
- 源站核对：`naphtha-origin.png/txt`；原始网页 https://zh.tradingeconomics.com/commodity/naphtha 。

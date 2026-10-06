# 价格、数据与新闻来源矩阵及补齐（2026-09-10）

核验窗口约14:14—14:35上海；公网当前版本628b10124752d539。采用当前公网GET、Chrome页面、来源原文和既有正常采集API，未直接连接生产数据库。

## 价格与行业数据

|品种/指标|来源和口径|实测最新值/观察日|缺口|
|---|---|---|---|
|Brent / WTI|Yahoo期货代理|100.49 / 95.57 USD/bbl，09-10 13:59|不是EIA现货；原油历史主图06-19|
|原油现货|EIA/FRED|Brent96.02 / WTI91.48 USD/bbl，09-01|凭据已配置；官方本次未返回更新日期|
|石脑油|Trading Economics公开评估|825.61 USD/mt，09-09|旧人民币历史曲线07-23，不拼接|
|PX|新浪期货；生意社评估|9260 CNY/mt，09-10 14:16；9000 CNY/mt，09-09|期现分别存储；旧现货主图07-23|
|PTA|新浪期货；生意社评估|6224 CNY/mt，09-10 14:16；6839.75 CNY/mt，09-10|同上|
|MEG|新浪期货；生意社评估|5883 CNY/mt，09-10 14:16；6591.67 CNY/mt，09-10|同上；不当作同规格同基准|
|POY|纺织网公开评估|9080 CNY/mt，09-09|旧主图07-23、库存/开工07-03|
|DTY|纺织网公开评估|10122.14 CNY/mt，09-09|同上|
|美国原油库存/炼厂利用率|EIA周度|424460千桶/98%，08-28|不是今天的观察值|
|聚酯库存/开工/加工差|既有行业数据|库存/开工07-03，加工差07-07|缺少持续更新的当前来源|

六品种一次正常补采HTTP200/7.906秒，9条记录、0采集错误；按observation_id公网回读确认9/9存在。没有证明9条全部是新增唯一事实。生意社独立curl原文返回200但内容是安全检查页，没有绕过；其价格只有应用采集/回读证据，原文交叉核验受阻。

来源目录另显示：CFETS、郑商所、FRED、海关、TNC、UN Comtrade有成功读取；CFTC最近成功09-07。CME/ICE结算、INE盘中、AkShare及部分煤炭/生意社登记项没有读取记录。目录未执行不等于没有其他实时通道：Yahoo及生意社另有intraday记录。这是运行归因缺口。CCF/DCE保持软移除，不算修复目标。USGS独立工业提供方最近失败，未纳入40个新闻源分母。

## 新闻40源完整矩阵

首轮18 ok、18 no_relevant_items、2 error、2 timeout；随后中石油单源重试成功。因此基于该截面及单源结果是19有相关、18无相关、3未恢复；不是第二次全源同步轮询。

|来源|首轮状态|相关条目|本轮后续|
|---|---|---:|---|
|OPEC Press Releases|ok|1|—|
|EIA Press Room|no_relevant_items|0|—|
|EIA Weekly Petroleum Status Report|ok|1|—|
|OFAC Recent Actions|ok|2|—|
|U.S. Treasury Press Releases|ok|1|—|
|U.S. State Department Releases|error|0|—|
|EU Council Press Releases|no_relevant_items|0|—|
|NDRC News|ok|1|—|
|National Energy Administration|ok|3|—|
|UKMTO Recent Incidents|no_relevant_items|0|—|
|MARAD Advisories|ok|2|—|
|White House News|no_relevant_items|0|—|
|White House Briefings and Statements|no_relevant_items|0|—|
|UN Press Releases|no_relevant_items|0|—|
|UN Security Council Press Releases|no_relevant_items|0|—|
|IEA News|ok|2|—|
|Federal Reserve Press Releases|ok|1|—|
|CFTC Press Releases|ok|3|—|
|IMO Press Briefings|no_relevant_items|0|—|
|Maritime and Port Authority of Singapore Press Releases|ok|3|—|
|Google News Oil and Geopolitics RSS|ok|3|—|
|Google News Polyester Chain RSS|ok|1|—|
|GDELT Oil and Geopolitics Article RSS|timeout|0|—|
|Shanghai Stock Exchange Listed Announcements|no_relevant_items|0|—|
|Shenzhen Stock Exchange Listed Announcements|no_relevant_items|0|—|
|HKEX Listed Company Announcements|no_relevant_items|0|—|
|CNINFO Listed Company Announcements|no_relevant_items|0|—|
|U.S. Central Command Public Affairs RSS|ok|3|—|
|U.S. Department of Defense / War News RSS|ok|2|—|
|NATO Press Releases|no_relevant_items|0|—|
|European Commission Press Corner|no_relevant_items|0|—|
|UK Government News and Communications|no_relevant_items|0|—|
|UK FCDO News and Communications|ok|2|—|
|U.S. Coast Guard News|ok|3|—|
|Saudi Aramco News|no_relevant_items|0|—|
|ADNOC News|ok|3|—|
|QatarEnergy News|no_relevant_items|0|—|
|Sinopec News|error|0|—|
|CNPC News|timeout|0|单源重试成功，3条，2摘要成功/1失败|
|EIA Today in Energy|no_relevant_items|0|解析器本地修复，0→11，未发布|

最近200条新闻中146条来自Google News两个聚合源（73%）；不等同于完整原文或独立事实覆盖。IEA已出现09-10T05:00:00Z出版记录，但标题附带导航噪声，未在本轮修复。中石油公开回读有09-10新闻，仍保留旧乱码及栏目记录，本次未直接修改历史。无相关条目可能是真无更新、主题不匹配或解析漏收，本轮未逐一实测18源所有栏目。

## 补齐与修复结果

1. 已执行六品种正常报价采集，9条回读成功。
2. 中石油单源重试HTTP200/62.409秒，运行ok、3条相关新闻、2摘要成功/1失败；不宣称稳定恢复。
3. 中石化原入口独立请求40秒连接超时，仍未恢复；未绕过国务院403、GDELT限流/超时。
4. EIA Today in Energy官方页HTTP200，Chrome确认日期与文章链接。旧解析器对同一75KB页面识别0条。修复卡片识别，并对EIA detail.php规范URL保留id参数、防止不同文章被合并；新解析器保留11条独立相关新闻，日期07-31至09-09。本地代码修复尚未部署，生产仍不能算补齐。

变更仅server/app/news.py及新增server/tests/test_eia_today_listing.py。更改前news.py与当前生产文件SHA一致；未覆盖工作区其他既有改动。无新增依赖、外部服务、数据迁移或Git操作。GitHub复用检索没有发现适配的EIA卡片专用实现，沿用标准HTMLParser和现有采集流程。

验收：58项新闻/调度/摘要输入/重定向/来源回归通过，Ruff通过，真实官方页面重放0→11。首次pytest因未指定安全测试环境被拒绝，随后在专用临时测试目录按DG01隔离规则重跑通过；未访问生产数据库。未运行全仓测试、全量UI回归或本地代码发布后的公网验证。

回滚：本地本轮前文件news-before.py及精确news-change.patch可区分本轮改动；不要用git restore覆盖用户其他工作。生产未发布，因此本轮没有代码回滚动作。未来发布应从当前不可变版本构建只替换此运行文件的候选，失败切回旧release；已经通过正常API写入的观察/新闻不随代码回滚删除。

## 发布边界与剩余事项

本轮不宣称来源覆盖验收完成。EIA解析候选文件及SHA见candidate.json，生产版本未变。尚需限定代码发布、EIA正常补采、公网回读，才能将这项记为生产解决。持续多日稳定性、18源零条目逐源复核、聚酯供需更新、历史曲线整合、聚合正文缺口、USGS和其他外部故障继续保留。

证据目录：/path/to/project/agent-context/source-matrix-20260910/。market/sources/eia-official的png与txt为Chrome证据；price-replenish/cnpc-replenish及readback响应为补齐证据；external-http与原始HTML为来源证据；regression.log、candidate.json和news-change.patch为候选证据。evidence-index.json记录绝对路径、SHA-256、大小。


## 批准后的发布与公网验证（2026-09-10 14:38起）

用户批准后，已发布20260910T063558Z-dcdc6fc3565731a7。生产运行代码仅news.py变化，原有前端资源、依赖及配置保留；回滚目标628b10124752d539。三个进程重启均退出0，无直接数据库操作。

live、ready、release.json均HTTP200，三处身份一致；耗时分别2.265、2.345、2.166秒。正常EIA单源采集HTTP200/58.089秒，运行ok，取得11篇、更新11个新闻聚类；6条摘要被选中，5成功/1失败，没有宣称摘要100%完成。

公网按来源回读总计14条（含原有3条），本次对应11篇全部存在、11个独立canonical URL、发布日期全部匹配官方页面。最新文章09-09，未改成采集日09-10。来源目录EIA Today in Energy已为active/ok，最近成功14:38。Chrome强制刷新并检查来源目录，证据post-eia-source.png/txt。

原来的EIA新闻卡片漏收与文章ID误去重问题已在生产验证解决。剩余1条摘要失败、其他外源故障、行业供需及连续历史缺口不因本次发布而关闭；未验证连续多日运行。相关58项测试和Ruff通过证据保持有效，候选运行文件与批准SHA一致。

补充证据：activation.json、post-health-http.json、eia-post-fetch.json、eia-post-articles.json、post-verification.json、post-catalog.json及artifact-check.log。

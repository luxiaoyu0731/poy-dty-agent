# POY/DTY 上游原材料工业情报与成本压力预测 Agent 项目文档

版本：v0.2
日期：2026-09-05
状态：工业情报中心产品方向已确认；实施待完成，20 日价值验证未开始
视觉参考目录：`视觉参考图/`

详细实施规格：[`docs/industrial-intelligence-center.md`](docs/industrial-intelligence-center.md)

架构决策：[`docs/adr/0005-industrial-intelligence-center.md`](docs/adr/0005-industrial-intelligence-center.md)

双轨完成定义：[`docs/project-correction-100.md`](docs/project-correction-100.md)

## 1. 项目定位

本项目建设一个面向 POY/DTY 生产经营者的上游工业情报与预测工作台。产品主价值是把分散的全球公开信息压缩成可追溯、可筛选、可用于晨会研判的产业情报；现有预测、复盘和治理能力完整保留，作为独立参考模块继续运行。

本轮产品修正只做加法：不得改变现有预测合同、数值结果、评分口径、API、历史账本和原七个工作台模块的语义。新增能力使用独立情报数据域、接口和页面，不得写入或影响正式预测、事件提升、现有 RAG 白名单及模型晋级流程。

首要使用场景是：POY/DTY 生产商或采购负责人在每天经营会议前，用不超过 10 分钟看清过去 24 小时发生了什么、为什么与原料采购和供应风险有关、证据是否可靠、存在哪些冲突与缺口，以及下一步需要观察什么。

核心研究对象：

```text
原油 / 煤炭
-> 石脑油 / MX / PX / 乙烯 / EO / 甲醇
-> PTA / MEG
-> 聚酯熔体 / 聚酯切片
-> POY/DTY 上游原料成本压力
```

系统优先回答五类问题：

```text
1. 过去 24 小时有哪些真正新增、修订、升级或失效的信息？
2. 哪些变化与原油、PX、PTA、MEG、POY/DTY、采购或供应风险有关？
3. 这些判断由哪些独立原始来源支持，事实、推断、反证和缺口分别是什么？
4. 影响可能通过哪条产业链路径传导，在 D1、D7、D30 应继续观察什么？
5. 独立的现有预测模块如何判断未来价格或成本压力，历史表现是否支持使用？
```

产品不承诺利润提升，不替代采购决策，不把公开新闻数量伪装成独立证据，也不让大模型补造免费数据无法回答的事实。

## 2. 核心输出

前端和业务侧按以下顺序交付。工业情报输出是新增主价值；原有预测及其他能力不删除、不改写：

| 输出 | 说明 |
| --- | --- |
| 每日工业情报摘要 | 以 08:20 数据截止、09:30 发布为默认节奏，交付 Top 事件、相关原因、证据、影响路径、期限、反证、缺口和观察项 |
| 全球雷达 | 广泛收集全球经济、军事安全、航运、天气和新闻信号；全部内容可浏览，但只有产业高相关事件进入每日摘要 |
| 事件簇与证据链 | 把同一事件的转载、聚合和原始来源合并，分别展示独立原始来源数与聚合命中数 |
| 产业链影响视图 | 把事件映射到原油→石脑油/PX→PTA/MEG→POY/DTY，并明确区分事实和研判 |
| 全球态势地图 | 展示有可靠地理信息的事件、港口和关键节点；地图用于理解空间关系，不充当证据本身 |
| 来源与运行状态 | 展示来源等级、权利边界、刷新时间、运行状态和明确缺口，不暴露密钥或内部原始字段 |
| 操作者反馈 | 支持相关、无关、重复、继续/取消观察，以及对来源/主题静音或取消静音；反馈只调整未来排序或显示，不篡改证据，也不触发提醒 |
| 现有行情、证据、Agent、助手和报告 | 保留当前七个工作台模块的合同和语义，情报中心仅作为第八个追加模块 |
| 七品种 D1/D7/D30 预测 | 原油、石脑油、PX、PTA、MEG、POY、DTY 的现有预测结果独立展示，不由新增情报自动改写 |
| 预测账本与复盘 | 继续按原合同留痕、到期结算并与朴素基线比较；预测成绩与情报价值指标完全分账 |

每日摘要的核心事件结构固定包含：事实、证据等级、为何相关、传导路径、受影响品种、可能方向、D1/D7/D30、置信度、反证、信息缺口和下一观察项。方向、期限和影响均属于研判字段，必须显式标注，不能冒充已确认事实。

## 3. 重点监控品种和变量

### 3.1 最高优先级

```text
Brent 原油
WTI 原油
上海原油 SC
石脑油
PX
PTA
MEG
煤炭
乙烯
人民币汇率
美元指数
```

### 3.2 次级优先级

```text
MX
EO 环氧乙烷
甲醇
聚酯切片 / 聚酯熔体
钛白粉
纺丝油剂
色母粒
海运费
EIA 库存
OPEC+ 政策
装置检修
港口库存
地缘政治事件
制裁和贸易政策
```

### 3.3 影响链条

```text
原油上涨
-> 石脑油成本上升
-> PX 成本支撑增强
-> PTA 成本抬升
-> POY/DTY 上游原料成本压力上升
```

```text
煤炭 / 乙烯 / 甲醇变化
-> MEG 成本或供应变化
-> 聚酯原料成本压力变化
```

```text
美元指数上升
-> 大宗商品承压
-> 原油/PX/PTA 上行动能减弱
```

```text
人民币贬值
-> 进口原料成本上升
-> PX/PTA/MEG 人民币计价成本压力增强
```

## 4. 信息来源设计

信息来源分为四级，系统必须在输出中标记来源等级。

| 等级 | 类型 | 用途 | 示例 |
| --- | --- | --- | --- |
| A | 政府、交易所、监管机构、官方组织或事件直接责任方 | 作为其直接发布事实的锚点和价格基准 | EIA、OPEC、ICE、CME、INE、ZCE、CFETS、GACC；DCE 为软移除历史候选 |
| B | 具有明确编辑、研究、方法论或一手企业责任的专业来源 | 行业数据、研报、企业公告和经许可的直接媒体报道 | 卓创、隆众、ICIS、Argus、S&P Global Commodity Insights，以及直接授权的 Reuters/Bloomberg/AP/FT 等；CCF 为软移除历史候选 |
| C | 聚合器、搜索/RSS 发现源、普通二手媒体或尚未还原原始出处的线索 | 事件发现、背景和待交叉验证线索 | GDELT、Google News RSS、来源身份尚待确认的普通媒体 |
| D | 操作者材料、社交媒体、论坛、传闻或来源身份不足的输入 | 仅供复核和观察，不直接进入结论 | 用户文件、X、Telegram、微信群、论坛、单一自媒体 |

结论生成规则：

```text
A/B 级来源可以支持事实或高质量行业背景，但不能仅凭等级自动获得正式预测资格。
C 级来源需要独立交叉验证或官方后续确认，单独只能形成发现候选。
D 级来源只能触发观察任务，不能单独形成高置信情报或预测依据。
是否进入当前正式预测由独立的七品种标签、point-in-time、特征、OOS 和模型晋级合同决定；v37 可输出的 item/event/brief 内容记录一律 prediction_eligible=false 且 instruction_eligible=false。
```

## 5. 数据源清单

本章是来源候选目录，不代表所有条目已经接入或当前启用。运行状态以版本化 Source Registry 和部署配置为准；DCE 与 CCF 当前均为软移除，历史记录只读保留，不参与新抓取、调度、情报 readiness 或当前预测。重新启用必须由新的明确决策和 ADR 覆盖。

### 5.1 原油、能源和国际供需

| 来源 | 链接 | 数据内容 | 采集方式 | 频率 | 备注 |
| --- | --- | --- | --- | --- | --- |
| EIA Open Data | https://www.eia.gov/opendata/documentation.php | 原油价格、库存、产量、进口、消费等 | API JSON | 每日/每周 | A 级，优先使用 API |
| EIA Petroleum Browser | https://www.eia.gov/opendata/index.php/browser/petroleum/ | 石油分类数据浏览和序列确认 | API/网页辅助 | 每日/每周 | 用于校验 series id |
| OPEC MOMR | https://www.opec.org/monthly-oil-market-report.html | 月度油市报告、供需、产量、需求展望 | PDF/HTML 抓取 | 月度 | A 级，适合政治和供需推演 |
| OPEC Press Releases | https://www.opec.org/press-releases.html | OPEC+ 决议、会议声明 | HTML/RSS 监控 | 15-30 分钟 | 重大事件信号源 |
| IEA Oil Market Report | https://www.iea.org/reports/oil-market-report-december-2025 | 全球油市供需、库存、炼厂、贸易流 | HTML/PDF/订阅 | 月度 | 部分内容需订阅 |
| Baker Hughes Rig Count | https://bakerhughesrigcount.gcs-web.com/ | 美国/加拿大/国际钻机数 | HTML/PDF/下载 | 周度/月度 | 供应预期辅助因子 |
| ICE Brent | https://www.ice.com/products/219/brent-crude-futures/data | Brent 原油期货 | 官方延迟行情/授权 API | 分钟级/日度 | 实时需数据授权 |
| CME WTI CL | https://www.cmegroup.com/markets/energy/crude-oil/light-sweet-crude.quotes.html | WTI 原油期货 | 官方延迟行情/授权 API | 分钟级/日度 | 实时需数据授权 |
| INE 上海原油 | https://www.ine.cn/eng/market/futures/energy/sc/index.html | SC 原油期货 | HTML/交易所数据/授权 | 分钟级/日度 | 中国进口成本参考 |
| INE Daily Data | https://www.ine.cn/eng/reports/statistical/daily/index.html?paramid=js | 日行情、结算、成交、仓单等 | HTML/接口探测/Playwright | 日度 | 交易所日度数据 |

### 5.2 PX、PTA、MEG 和化纤链条

| 来源 | 链接 | 数据内容 | 采集方式 | 频率 | 备注 |
| --- | --- | --- | --- | --- | --- |
| 郑州商品交易所 ZCE | https://english.czce.com.cn/ | PTA、PX、甲醇、瓶片等期货 | HTML/下载/接口探测 | 分钟级/日度 | A 级，重点跟 TA、PX、MA |
| ZCE PTA 页面 | https://english.czce.com.cn/en/Products/PTA/H081003015index_1.htm | PTA 合约规则和日度数据入口 | HTML | 日度 | 交易所确认口径 |
| 大连商品交易所 DCE | https://www.dce.com.cn/ | MEG 乙二醇期货、仓单、公告 | 当前禁用 | 不调度 | 历史候选；当前软移除 |
| CCFGroup | https://www.ccfgroup.com/informs/index_prod.php | PTA、MEG、PX、聚酯链价格、日报、开工、库存 | 当前禁用 | 不调度 | 历史候选；当前软移除 |
| CCF Tracker | https://app.ccfgroup.com/ | 实时快讯、价格、报告 | 当前禁用 | 不调度 | 历史候选；当前软移除 |
| 卓创资讯 SCI99 | https://corp.sci99.com/default.aspx | 大宗商品价格、库存、产业数据、数据集成 | 授权 API/数据超市 | 日内/日度 | B 级，建议采购数据服务 |
| 卓创指数 | https://index.sci99.com/commodityindex.aspx | 商品指数、价格指数 | HTML/授权 | 日度 | 可做趋势辅助 |
| 隆众资讯 OilChem | https://www.oilchem.net/ | 化工价格、库存、开工、装置动态 | 授权 API/网页 | 日内/日度 | B 级，适合装置检修和库存 |
| 百川盈孚 Baiinfo | https://www.baiinfo.com/ | 化工品价格、供需、装置、研报 | 授权 API/网页 | 日度 | 可作交叉验证 |
| ICIS PTA Methodology | https://www.icis.com/compliance/documents/purified-terephthalic-acid-pta-methodology-5-november-2020/ | PTA 价格评估方法 | HTML/PDF | 低频 | 用于价格口径理解 |
| Argus | https://www.argusmedia.com/ | 原油、芳烃、化工品价格评估 | 授权 API/报告 | 日度/周度 | 国际价格评估源 |
| S&P Global Commodity Insights | https://www.spglobal.com/commodityinsights/ | Platts 原油、石脑油、PX、化工品价格 | 授权 API/报告 | 日度 | 国际价格评估源 |

商业源原则：

```text
1. 不绕过登录、验证码、付费墙和授权限制。
2. 优先采购 API、数据导出或企业数据集成服务。
3. 如只能网页读取，必须经过账号授权并遵守频率限制。
4. 价格评估源之间存在口径差异，必须保存 source_id 和 assessment_method。
```

### 5.3 汇率、宏观和贸易数据

| 来源 | 链接 | 数据内容 | 采集方式 | 频率 | 备注 |
| --- | --- | --- | --- | --- | --- |
| SAFE 人民币中间价 | https://www.safe.gov.cn/safe/rmbhlzjj/index.html | 人民币汇率中间价 | HTML | 工作日 | 数据来源为中国外汇交易中心 |
| CFETS 中间价 | https://www.chinamoney.org.cn/english/bmkcpr/index.html?tab=2 | CNY Central Parity Rate | HTML/接口探测 | 工作日 9:15 后 | A 级汇率源 |
| PBOC 中间价公告 | https://www.pbc.gov.cn/zhengcehuobisi/125207/125217/125925/index.html?aisiteOutPageId=105752 | 人民币汇率中间价公告 | HTML | 工作日 | 官方公告备份 |
| Federal Reserve H.10 | https://www.federalreserve.gov/RELEASES/h10/ | 美元汇率、美元指数相关数据 | HTML/Data Download | 周度/日度 | A 级国际汇率源 |
| GACC 海关统计 | https://english.customs.gov.cn/Statistics/Statistics?ColumnId=6 | 中国进出口统计 | HTML/下载 | 月度 | 原油、PX、MEG 进口量辅助 |
| 国家统计局 | https://data.stats.gov.cn/ | PPI、工业数据、宏观指标 | API/HTML | 月度 | 需求和宏观背景 |

### 5.4 政治、制裁和地缘事件

| 来源 | 链接 | 数据内容 | 采集方式 | 频率 | 备注 |
| --- | --- | --- | --- | --- | --- |
| White House Releases | https://www.whitehouse.gov/releases/ | 美国总统行政命令、声明、政策 | HTML/RSS 监控 | 15-30 分钟 | 影响制裁、能源和贸易预期 |
| U.S. Treasury OFAC | https://ofac.treasury.gov/ | 制裁公告、SDN 列表、制裁项目 | API/XML/HTML | 15-30 分钟 | 影响俄罗斯、伊朗、航运、能源贸易 |
| U.S. State Department | https://www.state.gov/press-releases/ | 外交声明、制裁、地区冲突 | HTML/RSS | 30 分钟 | 地缘政治信号 |
| UN Documents | https://documents.un.org/ | 联合国官方文件 | 搜索/API/HTML | 低频/事件触发 | 国际决议与冲突确认 |
| UN Web TV RSS | https://www.un.org/webcast/rss/ | 安理会和发布会视频标题 | RSS | 30 分钟 | 事件发现辅助 |
| EU Sanctions | https://www.consilium.europa.eu/en/policies/sanctions/ | 欧盟制裁政策 | HTML/RSS | 30 分钟 | 能源和贸易制裁 |
| 主流媒体授权源 | Reuters、Bloomberg、AP、FT、财新等 | 快讯、政治分析、突发事件 | 授权 API/RSS | 5-15 分钟 | C 级，需要交叉验证 |

政治事件输出必须区分：

```text
事实：官方公告、制裁文件、交易所数据、公开报告。
推断：基于利益相关方、历史模式和市场反应的判断。
低证据假设：只作为可能性，不作为结论。
反证：可能削弱该判断的事实或市场数据。
```

### 5.5 扩展信息源目录：保持 A/B/C/D 四级

本节是对现有核心 `source_registry` 的候选扩展目录和差异评审输入，不是另建注册表。原则是：宁可保留待评审候选，也不要让不同来源在系统里混成一团。正式接入前必须在核心注册表中标记 `tier`、`category`、`crawl_type`、`auth_type`、`freshness_sla` 和 `license_note`。

#### 5.5.1 A 级：官方、一手、交易所、法定披露源

A 级源是事实锚点，可成为独立预测合同评估的候选输入，也可支持复盘账本和高置信度事件事实；来源等级本身不授予预测资格。

| 来源 | 链接 | 重点数据 | 建议采集方式 | 频率 | 适用场景 |
| --- | --- | --- | --- | --- | --- |
| EIA Open Data | https://www.eia.gov/opendata/documentation.php | 原油、成品油、库存、产量、进口、炼厂开工 | API | 日/周/月 | 美国能源基本面 |
| EIA Weekly Petroleum Status Report | https://www.eia.gov/petroleum/supply/weekly/ | 美国原油库存、成品油库存、炼厂开工 | HTML/PDF/API | 周度 | 原油短期波动 |
| EIA STEO | https://www.eia.gov/outlooks/steo/ | 原油供需预测、价格展望 | HTML/PDF/API | 月度 | 中期能源判断 |
| EIA Today in Energy | https://www.eia.gov/todayinenergy/ | 能源专题解释、趋势图 | HTML/RSS | 日度 | 晨报背景材料 |
| OPEC Monthly Oil Market Report | https://www.opec.org/monthly-oil-market-report.html | OPEC 供需、产量、需求预测 | PDF/HTML | 月度 | 油市中期判断 |
| OPEC Press Releases | https://www.opec.org/press-releases.html | 会议、减产、增产、声明 | HTML/RSS | 15-30 分钟 | 重大政策信号 |
| OPEC Annual Statistical Bulletin | https://www.opec.org/annual-statistical-bulletin.html | 原油产量、储量、贸易、炼油 | PDF/HTML | 年度 | 长期背景 |
| IEA Oil Market Report | https://www.iea.org/reports/oil-market-report-december-2025 | 全球油市供需、库存、炼厂、贸易流 | HTML/PDF/订阅 | 月度 | 与 OPEC 观点交叉验证 |
| IEA Data & Statistics | https://www.iea.org/data-and-statistics | 能源供需、燃料结构、国家数据 | API/下载 | 月/月度以上 | 宏观能源结构 |
| JODI Oil Database | https://www.jodidata.org/oil/database/overview.aspx | 90 多个国家原油、石脑油、油品供需和库存 | 在线库/CSV | 月度 | 全球油品供需 |
| JODI Gas Database | https://www.jodidata.org/gas/ | 天然气产量、进口、需求、库存 | 在线库/CSV | 月度 | 气制化工、能源替代 |
| International Energy Forum | https://www.ief.org/focus/energy-markets-data-transparency | JODI、库存和能源透明度报告 | HTML/PDF | 月度/事件 | 全球油市解释 |
| CFTC COT | https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm | 原油、美元、利率等期货持仓 | 下载/CSV | 周度 | 资金情绪、拥挤度 |
| Baker Hughes Rig Count | https://bakerhughesrigcount.gcs-web.com/ | 美国、加拿大、国际钻机数 | HTML/PDF | 周/月 | 原油供应弹性 |
| CME WTI CL | https://www.cmegroup.com/markets/energy/crude-oil/light-sweet-crude.quotes.html | WTI 期货、期权、合约信息 | 授权行情/API/HTML | 分钟/日 | WTI 价格基准 |
| ICE Brent | https://www.ice.com/products/219/brent-crude-futures/data | Brent 期货、结算、合约 | 授权行情/API/HTML | 分钟/日 | Brent 价格基准 |
| ICE Dollar Index | https://www.ice.com/market-data/indices/currency-indices | DXY 美元指数 | 授权行情/API/HTML | 分钟/日 | 美元和商品压力 |
| ICE US Dollar Index Futures | https://www.ice.com/products/194/US-Dollar-Index-Futures/data | DXY 期货 | 授权行情/API/HTML | 分钟/日 | 宏观金融因子 |
| INE 上海国际能源交易中心 | https://www.ine.cn/eng/ | 上海原油、低硫燃料油、集运指数期货 | HTML/接口探测/授权 | 日/分钟 | 中国进口成本 |
| INE 原油 SC | https://www.ine.cn/eng/market/futures/energy/sc/index.html | SC 原油行情和合约 | HTML/授权行情 | 日/分钟 | 国内原油基准 |
| INE Daily Data | https://www.ine.cn/eng/reports/statistical/daily/index.html?paramid=js | 日行情、结算、仓单、成交 | HTML/Playwright | 日度 | 结算与仓单 |
| SHFE 上海期货交易所 | https://www.shfe.com.cn/eng/index.html | 金属、能源相关品种、仓单、公告 | HTML/下载 | 日/事件 | 宏观商品联动 |
| ZCE 郑州商品交易所 | https://english.czce.com.cn/ | PTA、PX、甲醇、瓶片、短纤等 | HTML/下载/授权 | 日/分钟 | PTA/PX 主力期货 |
| ZCE PTA | https://english.czce.com.cn/en/Products/PTA/H081003015index_1.htm | PTA 合约、日行情入口 | HTML | 日度 | PTA 口径确认 |
| DCE 大连商品交易所 | https://www.dce.com.cn/ | MEG、LPG、焦煤、焦炭等 | 当前禁用 | 不调度 | 历史候选；当前软移除 |
| CSRC 中国证监会 | https://www.csrc.gov.cn/ | 期货品种批准、监管、政策 | HTML/RSS | 事件 | 监管风险 |
| 中国期货业协会 | https://www.cfachina.org/ | 期货市场研究、培训、公告 | HTML/PDF | 周/月 | 行业研究参考 |
| 中国期货市场监控中心 | http://www.cfmmc.com/ | 期货市场监控、统计信息 | HTML | 低频 | 国内期货资金背景 |
| CFETS 中国外汇交易中心 | https://www.chinamoney.org.cn/english/ | 人民币中间价、CFETS 指数、Shibor、LPR | HTML/接口探测 | 工作日 | 汇率和资金面 |
| CFETS CNY Central Parity | https://www.chinamoney.org.cn/english/bmkcpr/index.html?tab=2 | 人民币中间价 | HTML/接口探测 | 工作日 9:15 后 | 人民币因子 |
| SAFE 国家外汇管理局 | https://www.safe.gov.cn/ | 汇率、外储、国际收支 | HTML | 工作日/月 | 汇率政策背景 |
| SAFE 人民币中间价 | https://www.safe.gov.cn/safe/rmbhlzjj/index.html | 中间价公告 | HTML | 工作日 | CFETS 备份源 |
| PBOC 中国人民银行 | https://www.pbc.gov.cn/ | 货币政策、LPR、汇率公告 | HTML/RSS | 工作日/事件 | 宏观政策 |
| PBOC 中间价公告 | https://www.pbc.gov.cn/zhengcehuobisi/125207/125217/125925/index.html?aisiteOutPageId=105752 | 人民币汇率公告 | HTML | 工作日 | 汇率源备份 |
| 国家统计局 | https://data.stats.gov.cn/ | PPI、工业增加值、进出口、制造业 | API/HTML | 月度 | 宏观需求 |
| 海关总署 GACC | https://english.customs.gov.cn/Statistics/Statistics?ColumnId=6 | 原油、PX、MEG、煤炭进出口 | HTML/下载 | 月度 | 进口成本和贸易流 |
| 商务部 MOFCOM | http://www.mofcom.gov.cn/ | 贸易政策、外贸数据、反倾销 | HTML/RSS | 事件/月 | 贸易政策 |
| 发改委 NDRC | https://www.ndrc.gov.cn/ | 能源、煤炭、价格调控、产业政策 | HTML/RSS | 事件 | 国内政策风险 |
| 国家能源局 NEA | http://www.nea.gov.cn/ | 能源生产、消费、电力、煤炭政策 | HTML/RSS | 月/事件 | 能源政策 |
| 生态环境部 MEE | https://www.mee.gov.cn/ | 环保、限产、排放政策 | HTML/RSS | 事件 | 装置和开工约束 |
| 工信部 MIIT | https://www.miit.gov.cn/ | 工业政策、制造业运行 | HTML/RSS | 月/事件 | 终端工业背景 |
| 财政部 MOF | https://www.mof.gov.cn/ | 税费、关税、财政政策 | HTML/RSS | 事件 | 进口成本和政策 |
| 中国政府网 | https://www.gov.cn/ | 国务院政策、会议、公告 | HTML/RSS | 事件 | 政策总入口 |
| 上交所公告 | https://www.sse.com.cn/ | 上市石化企业公告 | HTML/接口探测 | 事件 | 装置、项目、产能 |
| 深交所公告 | https://www.szse.cn/ | 上市化工企业公告 | HTML/接口探测 | 事件 | 装置、项目、产能 |
| 巨潮资讯 CNINFO | http://www.cninfo.com.cn/ | A 股上市公司公告 | HTML/接口探测 | 事件 | 恒力、荣盛、恒逸等公告 |
| 港交所披露易 | https://www.hkexnews.hk/ | H 股和港股公告 | HTML/接口探测 | 事件 | 中石化、中石油等 |
| SEC EDGAR | https://www.sec.gov/edgar | 国际上市公司公告 | API/HTML | 事件 | 海外炼化和航运企业 |
| UN Comtrade | https://comtradeplus.un.org/ | 全球商品贸易，HS 编码进出口 | API/下载 | 月/年 | 原油、PX、MEG 贸易流 |
| IMF Primary Commodity Prices | https://www.imf.org/en/research/commodity-prices | 大宗商品价格指数、燃料指数 | 下载/API | 月度 | 宏观商品背景 |
| World Bank Pink Sheet | https://www.worldbank.org/en/research/commodity-markets | 能源、煤炭、化肥等商品价格 | XLS/PDF | 月度 | 全球商品背景 |
| Federal Reserve H.10 | https://www.federalreserve.gov/RELEASES/h10/ | 汇率和美元指数 | HTML/下载 | 周/日 | 美元因子 |
| FRED | https://fred.stlouisfed.org/ | 利率、汇率、油价、宏观数据 | API | 日/周/月 | 宏观金融 |
| U.S. Treasury Rates | https://home.treasury.gov/resource-center/data-chart-center/interest-rates/TextView?type=daily_treasury_yield_curve | 美债收益率 | CSV/XML/HTML | 日度 | 利率和美元环境 |
| White House Releases | https://www.whitehouse.gov/releases/ | 行政命令、制裁、能源政策 | HTML/RSS | 15-30 分钟 | 美国政策 |
| U.S. State Department | https://www.state.gov/press-releases/ | 外交声明、制裁、冲突 | HTML/RSS | 30 分钟 | 地缘政治 |
| U.S. Treasury OFAC | https://ofac.treasury.gov/ | SDN、制裁公告、能源/航运制裁 | API/XML/HTML | 15-30 分钟 | 制裁事件信号 |
| EU Sanctions | https://finance.ec.europa.eu/eu-and-world/sanctions-restrictive-measures/overview-sanctions-and-related-resources_en | 欧盟制裁和金融制裁清单 | HTML/下载 | 事件 | 俄油、航运、保险 |
| Council of EU Press | https://www.consilium.europa.eu/en/press/press-releases/ | 欧盟政策和制裁新闻 | HTML/RSS | 事件 | 欧盟政治风险 |
| UK Sanctions | https://www.gov.uk/government/collections/uk-sanctions | 英国制裁和指定船舶 | HTML/下载 | 事件 | 保险、航运、俄油 |
| UK Sanctions Search | https://search-uk-sanctions-list.service.gov.uk/ | 英国制裁名单查询 | HTML/API 探测 | 事件 | 制裁筛查 |
| UN Security Council Sanctions List | https://main.un.org/securitycouncil/en/content/un-sc-consolidated-list | 联合国制裁名单 | XML/HTML | 事件 | 国际制裁确认 |
| UN Official Documents | https://documents.un.org/ | 联合国正式文件、决议 | 搜索/HTML | 事件 | 冲突确认 |
| UN Web TV RSS | https://www.un.org/webcast/rss/ | 安理会会议和发布会 | RSS | 30 分钟 | 事件发现 |
| Canada Sanctions | https://www.international.gc.ca/world-monde/international_relations-relations_internationales/sanctions/consolidated-consolide.aspx | 加拿大制裁名单 | HTML | 事件 | 交叉验证 |
| Australian Sanctions | https://www.dfat.gov.au/international-relations/security/sanctions | 澳大利亚制裁 | HTML/下载 | 事件 | 交叉验证 |
| Japan MOFA | https://www.mofa.go.jp/ | 日本外交、制裁、能源外交 | HTML/RSS | 事件 | 东亚政策 |
| Saudi Press Agency | https://www.spa.gov.sa/ | 沙特官方新闻 | HTML/RSS | 事件 | OPEC/中东 |
| Saudi Ministry of Energy | https://www.moenergy.gov.sa/ | 沙特能源政策 | HTML | 事件 | OPEC 核心方 |
| UAE Ministry of Energy | https://www.moei.gov.ae/ | 阿联酋能源政策 | HTML | 事件 | OPEC 核心方 |
| Iraq Oil Ministry | https://oil.gov.iq/ | 伊拉克产量、出口、声明 | HTML | 事件 | OPEC 供应 |
| Iran Oil Ministry / Shana | https://en.shana.ir/ | 伊朗油气官方新闻 | HTML/RSS | 事件 | 伊朗供应和制裁 |
| Russia Energy Ministry | https://minenergo.gov.ru/ | 俄罗斯能源政策 | HTML | 事件 | 俄油供应 |
| 上海航运交易所 SCFI | https://www.sse.net.cn/index/singleIndex?indexType=scfi | 上海出口集装箱运价指数 | HTML | 周度 | 运费和红海影响 |
| 上海航运交易所英文站 | https://en.sse.net.cn/indices/scfinew.jsp | SCFI/CCFI/SCFIS 指数说明 | HTML | 周度 | 指数口径 |
| 上海航运交易所公告 | https://en.sse.net.cn/eninfo/Bulletin/index.shtml | 航运指数发布日期和公告 | HTML | 事件 | 运价数据日历 |
| IMO 国际海事组织 | https://www.imo.org/ | 航运安全、海事政策、红海相关 | HTML/RSS | 事件 | 航道和规则 |
| US MARAD Advisories | https://www.maritime.dot.gov/msci-advisories | 海事安全警告 | HTML/RSS | 事件 | 红海、霍尔木兹 |
| UKMTO | https://www.ukmto.org/ | 海上安全事件通报 | HTML/RSS/API 探测 | 事件 | 红海/中东航运 |

#### 5.5.2 B 级：授权商业数据、价格评估机构、行业数据库

B 级源适合做行业价格、现货成交、库存、开工、装置检修、船期、运费和专业判断。原则：优先签 API 或企业数据服务，不绕过付费墙。

| 来源 | 链接 | 重点数据 | 建议采集方式 | 频率 | 适用场景 |
| --- | --- | --- | --- | --- | --- |
| CCFGroup | https://www.ccfgroup.com/informs/index_prod.php | PX、PTA、MEG、聚酯、长丝、开工、库存、日报 | 当前禁用 | 不调度 | 历史候选；当前软移除 |
| CCF Tracker | https://app.ccfgroup.com/ | 实时快讯、报价、报告 | 当前禁用 | 不调度 | 历史候选；当前软移除 |
| 卓创资讯 SCI99 | https://corp.sci99.com/default.aspx | 化工、能源、价格、库存、装置、数据集成 | 授权 API/数据超市 | 日内/日 | 国内大宗商品数据 |
| 卓创指数 | https://index.sci99.com/commodityindex.aspx | 商品价格指数 | HTML/授权 | 日 | 趋势辅助 |
| 隆众资讯 OilChem | https://www.oilchem.net/ | 石油化工、价格、开工、库存、船期、装置 | 授权 API/账号 | 日内/日 | 化工装置和库存 |
| 百川盈孚 Baiinfo | https://www.baiinfo.com/ | 化工价格、产业数据、进出口、研报 | 授权 API/账号 | 日 | 交叉验证 |
| 金联创 315i | https://chem.315i.com/ | 石油、化工价格、估价、装置、船期 | 授权 API/账号 | 日内/日 | 化工和油品 |
| 金联创数据中心 | https://buy.315i.com/ | 数据订阅、指数、船期、工程项目 | 授权 API/账号 | 日 | 数据接入 |
| Mysteel 我的钢铁 | https://www.mysteel.com/ | 煤炭、化工煤、钢联数据、宏观商品 | 授权 API/账号 | 日内/日 | 煤炭和宏观商品 |
| Mysteel 煤炭 | https://coal.mysteel.com/ | 煤炭价格、港口库存、煤焦数据 | 授权 API/账号 | 日 | MEG 煤制路线 |
| SMM 上海有色 | https://www.metal.com/ | 金属、能源化工相关、宏观快讯 | 授权 API/账号 | 日内/日 | 宏观商品情绪 |
| S&P Global Platts | https://www.spglobal.com/energy/en/products-solutions/platts | 原油、石脑油、PX、PTA、MEG、航运、新闻 | 授权 API/FTP/账号 | 日内/日 | 国际价格评估 |
| Platts Market Data | https://commodityinsights.spglobal.com/plattsmarketdata.html | 市场数据、价格、前曲线 | 授权 API/FTP | 日内/日 | 企业级行情 |
| Argus Media | https://www.argusmedia.com/en/ | 原油、成品油、芳烃、化工、煤炭、运费 | 授权 API/报告 | 日内/日 | 国际价格评估 |
| Argus Toluene/Xylenes Methodology | https://www.argusmedia.com/-/media/project/argusmedia/mainsite/english/documents-and-files/methodology/argus-toluene-xylenes-and-derivatives.ashx | 甲苯、二甲苯、PX、PTA、MEG 评估口径 | PDF | 低频 | 价格口径 |
| ICIS | https://www.icis.com/ | 化工价格、供需、新闻、方法论 | 授权 API/账号 | 日内/日 | 化工国际源 |
| ICIS PTA Methodology | https://www.icis.com/compliance/documents/purified-terephthalic-acid-methodology-17-january-2020/ | PTA 价格评估方法 | HTML/PDF | 低频 | 方法论 |
| OPIS | https://www.opisnet.com/ | 油品、燃料、物流、炼化 | 授权 API/报告 | 日 | 成品油和原油 |
| Chemical Market Analytics | https://chemicalmarketanalytics.com/ | 化工供需、价格预测、装置 | 授权报告/API | 周/月 | 中长期基本面 |
| ChemOrbis | https://www.chemorbis.com/ | 塑料和化工价格、供需、新闻 | 授权账号 | 日 | 下游塑料参考 |
| Fastmarkets | https://www.fastmarkets.com/ | 商品价格评估、林浆、金属、化工相关 | 授权 API/报告 | 日/周 | 交叉验证 |
| Wood Mackenzie | https://www.woodmac.com/ | 能源、化工、炼化、供需预测 | 授权报告/API | 周/月 | 中长期研究 |
| Rystad Energy | https://www.rystadenergy.com/ | 油气供应、项目、成本、能源转型 | 授权 API/报告 | 周/月 | 原油供应 |
| Energy Aspects | https://www.energyaspects.com/ | 原油、成品油、宏观能源研究 | 授权报告/API | 日/周 | 油市研究 |
| Kpler | https://www.kpler.com/ | 油品、LNG、船货、贸易流、库存 | 授权 API/平台 | 日内/日 | 真实贸易流 |
| Vortexa | https://www.vortexa.com/ | 原油、油品、水上库存、油轮追踪 | 授权 API/平台 | 日内/日 | 供应和库存领先指标 |
| MarineTraffic | https://support.marinetraffic.com/en/articles/9552659-api-services | AIS、港口事件、历史航线 | 授权 API | 实时/日 | 航运和船期 |
| Lloyd's List Intelligence | https://www.lloydslistintelligence.com/ | AIS、船舶、港口、制裁、海事情报 | 授权 API/平台 | 实时/日 | 航运风险 |
| VesselFinder API | https://api.vesselfinder.com/docs/vessels.html | 船舶位置、ETA、目的港 | 授权 API | 实时/日 | AIS 辅助 |
| Clarksons Research | https://www.clarksons.com/services/research/ | 航运、船队、运价、造船 | 授权报告/API | 周/月 | 航运基本面 |
| Drewry WCI | https://www.drewry.co.uk/WCI | 世界集装箱运价指数 | 授权/网页摘要 | 周 | 集运成本 |
| Freightos Baltic Index | https://terminal.freightos.com/ | 集装箱现货运价 | 授权 API/平台 | 日/周 | 海运费 |
| Baltic Exchange Data Services | https://www.balticexchange.com/en/data-services.html | 干散货、油轮、LPG/LNG 运价指数 | 授权 API/平台 | 日 | 油轮和煤运费 |
| Xeneta | https://www.xeneta.com/ | 海运和空运费率 | 授权 API/平台 | 日/周 | 运费参考 |
| Alphaliner | https://alphaliner.axsmarine.com/ | 集装箱船队、航线、班轮供给 | 授权平台 | 周 | 集运供给 |
| Wind 万得 | https://www.wind.com.cn/ | 国内期货、宏观、公告、研报 | 授权终端/API | 日内/日 | 综合金融数据 |
| 同花顺 iFinD | https://www.51ifind.com/ | 期货、宏观、公告、研报 | 授权终端/API | 日内/日 | 国内金融数据 |
| Choice 东方财富 | https://choice.eastmoney.com/ | 行情、公告、宏观、行业 | 授权终端/API | 日内/日 | 国内金融数据 |
| Bloomberg Data License | https://professional.bloomberg.com/products/data/data-management/data-license/ | 行情、新闻、宏观、企业数据 | 授权 API/SFTP | 日内/日 | 企业级金融数据 |
| LSEG / Refinitiv | https://developers.lseg.com/ | Reuters 新闻、行情、宏观、商品 | 授权 API | 日内/日 | 新闻和行情 |
| FactSet | https://www.factset.com/ | 金融数据、公司、宏观 | 授权 API | 日/周 | 企业和金融 |
| Nasdaq Data Link | https://data.nasdaq.com/ | 第三方经济和商品数据集 | API/授权 | 日/周 | 备用数据市场 |
| Trading Economics | https://tradingeconomics.com/ | 宏观、商品、国家数据 | API/授权 | 日/月 | 宏观数据备份 |

#### 5.5.3 C 级：主流媒体、专业媒体、研究机构和公开二手分析

C 级源主要用于事件发现、语境解释和交叉验证。不能单独触发高置信度结论，除非被 A/B 级源确认。

| 来源 | 链接 | 重点内容 | 建议采集方式 | 频率 | 备注 |
| --- | --- | --- | --- | --- | --- |
| Reuters | https://www.thomsonreuters.com/en/products-services/reuters-news-agency.html | 能源、地缘、OPEC、金融快讯 | 授权 API/RSS | 5-15 分钟 | 优先媒体源 |
| Bloomberg News | https://professional.bloomberg.com/products/data/data-management/data-license/ | 能源、金融、政策、航运 | 授权 API/终端 | 5-15 分钟 | 需授权 |
| Dow Jones Newswires / Factiva | https://www.dowjones.com/professional/factiva/ | 全球新闻和公司新闻 | 授权 API | 5-15 分钟 | 需授权 |
| AP News | https://apnews.com/ | 国际政治、冲突、政策 | RSS/API/授权 | 15-30 分钟 | 交叉验证 |
| AFP | https://www.afp.com/ | 国际政治、能源事件 | 授权 API/RSS | 15-30 分钟 | 交叉验证 |
| Financial Times | https://www.ft.com/ | 国际金融、地缘、能源 | 授权/RSS | 30 分钟 | 解释性强 |
| Wall Street Journal | https://www.wsj.com/ | 能源、金融、企业 | 授权/RSS | 30 分钟 | 需授权 |
| Nikkei Asia | https://asia.nikkei.com/ | 亚洲能源、制造业、贸易 | 授权/RSS | 30 分钟 | 亚洲视角 |
| Caixin 财新 | https://www.caixin.com/ | 中国宏观、产业、政策 | 授权/RSS | 30 分钟 | 国内高质量新闻 |
| 第一财经 | https://www.yicai.com/ | 宏观、商品、产业新闻 | RSS/HTML | 30 分钟 | 国内财经 |
| 证券时报 | https://www.stcn.com/ | A 股、商品、政策 | RSS/HTML | 30 分钟 | 上市公司和政策 |
| 中国证券报 | https://www.cs.com.cn/ | 金融、期货、产业 | RSS/HTML | 30 分钟 | 国内金融 |
| 上海证券报 | https://www.cnstock.com/ | 金融、上市公司、商品 | RSS/HTML | 30 分钟 | 国内金融 |
| 新华社 | https://www.news.cn/ | 官方新闻、政策、外交 | RSS/HTML | 30 分钟 | 政策确认辅助 |
| 央视新闻 | https://news.cctv.com/ | 官方新闻、突发事件 | RSS/HTML | 30 分钟 | 国内官方媒体 |
| 人民日报 | http://www.people.com.cn/ | 政策和官方口径 | RSS/HTML | 30 分钟 | 政策氛围 |
| Al Jazeera | https://www.aljazeera.com/ | 中东政治、冲突 | RSS/HTML | 15-30 分钟 | 中东事件发现 |
| BBC | https://www.bbc.com/news | 国际政治、冲突 | RSS/HTML | 30 分钟 | 交叉验证 |
| CNBC | https://www.cnbc.com/energy/ | 能源、市场、宏观 | RSS/HTML | 30 分钟 | 市场情绪 |
| OilPrice.com | https://oilprice.com/ | 原油、地缘、能源评论 | RSS/HTML | 30 分钟 | 观点源，需验证 |
| Energy Intelligence | https://www.energyintel.com/ | 原油、OPEC、能源政治 | 授权/RSS | 日内 | 专业能源新闻 |
| MEES | https://www.mees.com/ | 中东油气和政治 | 授权/RSS | 日/周 | 中东深度 |
| RBN Energy | https://rbnenergy.com/ | 北美能源、管道、炼化 | RSS/授权 | 日 | 北美能源解释 |
| Oxford Institute for Energy Studies | https://www.oxfordenergy.org/ | 能源政策研究 | HTML/PDF | 周/月 | 深度研究 |
| CSIS Energy Security | https://www.csis.org/programs/energy-security-and-climate-change-program | 能源安全和地缘分析 | HTML/RSS | 周/月 | 政治解释 |
| Brookings | https://www.brookings.edu/ | 宏观、国际关系 | RSS/HTML | 周 | 背景分析 |
| Carnegie Endowment | https://carnegieendowment.org/ | 地缘政治分析 | RSS/HTML | 周 | 背景分析 |
| Hellenic Shipping News | https://www.hellenicshippingnews.com/ | 航运、油轮、运价、港口 | RSS/HTML | 日 | 航运事件 |
| Lloyd's List News | https://www.lloydslist.com/ | 海运、油轮、制裁、保险 | 授权/RSS | 日内 | 航运专业新闻 |
| TradeWinds | https://www.tradewindsnews.com/ | 船东、油轮、航运 | 授权/RSS | 日 | 航运深度 |
| Splash247 | https://splash247.com/ | 航运突发、红海、港口 | RSS/HTML | 日 | 航运事件发现 |
| Seatrade Maritime | https://www.seatrade-maritime.com/ | 航运、港口、班轮 | RSS/HTML | 日 | 航运事件 |
| Hydrocarbon Processing | https://www.hydrocarbonprocessing.com/ | 炼化装置、项目、检修、事故 | RSS/HTML | 日 | 炼化装置 |
| Oil & Gas Journal | https://www.ogj.com/ | 油气、炼化、管道 | RSS/HTML | 日 | 原油供应 |
| Chemical Week | https://chemweek.com/ | 化工企业、价格、供需 | 授权/RSS | 日/周 | 国际化工 |
| PROCESS Worldwide | https://www.process-worldwide.com/ | 化工装置和项目 | RSS/HTML | 周 | 装置项目 |
| China Chemical Reporter | http://www.ccr.com.cn/ | 中国化工新闻 | 订阅/HTML | 周 | 国内化工 |
| 期货日报 | https://www.qhrb.com.cn/ | 国内期货和产业新闻 | RSS/HTML | 日 | 期货市场 |
| 中国化工报 | http://www.ccin.com.cn/ | 化工行业新闻 | HTML/RSS | 日 | 国内化工 |
| 中国煤炭市场网公开新闻 | https://www.cctd.com.cn/ | 煤炭新闻、价格指数说明 | HTML | 日/周 | 煤炭背景 |
| 秦皇岛煤炭网公开信息 | http://www.cqcoal.com/ | 环渤海动力煤指数相关公开信息 | HTML | 周 | 煤炭价格背景 |

#### 5.5.4 D 级：弱信号、社交媒体、社区、传闻和人工输入

D 级源只用于发现线索和触发观察任务。必须经过 A/B/C 级源确认后才能进入结论。

| 来源 | 重点内容 | 建议采集方式 | 频率 | 使用规则 |
| --- | --- | --- | --- | --- |
| X/Twitter 能源账号 | OPEC、油轮、制裁、冲突、交易员观点 | API/授权/人工订阅 | 实时 | 只做线索 |
| X/Twitter 官方账号 | 政府、交易所、能源机构账号 | API/授权/人工订阅 | 实时 | 若为官方账号，可升级为 A 级候选 |
| Telegram 能源/航运频道 | 红海、油轮、港口、制裁线索 | 机器人/人工 | 实时 | 需交叉验证 |
| 微信公众号 | 化工、期货、煤炭、航运行业观察 | 授权/人工/RSS 转发 | 日 | 注意版权和转载 |
| 微信群/朋友圈 | 贸易商报价、装置传闻、市场情绪 | 人工录入 | 实时 | 只做人工备注 |
| Reddit r/oil | 原油市场讨论、情绪 | API/网页 | 日内 | 不进入结论 |
| Reddit r/energy | 能源政策和数据讨论 | API/网页 | 日内 | 不进入结论 |
| Reddit r/geopolitics | 地缘政治讨论 | API/网页 | 日内 | 不进入结论 |
| Reddit r/shipping | 航运线索 | API/网页 | 日 | 不进入结论 |
| Reddit r/Maritime | 船舶、AIS、港口讨论 | API/网页 | 日 | 不进入结论 |
| Reddit r/Commodities | 商品交易讨论 | API/网页 | 日 | 不进入结论 |
| Reddit r/FuturesTrading | 期货市场情绪 | API/网页 | 日 | 不进入结论 |
| TradingView Ideas | 原油、PX/PTA 期货技术观点 | API/网页 | 日 | 情绪辅助 |
| Investing.com 评论区 | 原油和美元市场情绪 | 网页/人工 | 日 | 噪声高 |
| 东方财富股吧 | 国内化工股和期货情绪 | 网页/人工 | 日 | 噪声高 |
| 雪球 | 化工公司、期货、宏观讨论 | 网页/人工 | 日 | 情绪辅助 |
| 百度指数 | “原油”“PTA”“乙二醇”等搜索热度 | 网页/授权 | 日 | 情绪和关注度 |
| Google Trends | 国际油价、OPEC、Red Sea 搜索热度 | API/网页 | 日 | 情绪辅助 |
| YouTube 专业频道 | 能源、地缘、航运分析 | RSS/人工 | 日 | 摘要后验证 |
| 播客/访谈 | 能源交易员、行业专家观点 | RSS/人工 | 周 | 只做观点 |
| 供应商报价单 | 上游供应商口头/邮件报价 | 人工录入/邮件解析 | 实时 | 作为内部私有源 |
| 客户反馈 | 采购询价、成交意愿、补货节奏 | CRM/人工 | 实时 | 内部需求信号 |
| 业务员市场笔记 | 华东/华南市场反馈 | Obsidian/表单 | 日 | 内部知识沉淀 |
| 工厂库存口径 | 自家库存、采购节奏、资金压力 | ERP/API/人工 | 日 | 仅内部使用 |
| 截图/图片线索 | 报价单、群聊、船期截图 | OCR + 人工复核 | 实时 | 不自动入结论 |

#### 5.5.5 企业一手公告源：纳入统一来源目录

这些源介于 A 级和 B 级之间：如果来自企业官网、上市公告或交易所披露，应视为一手源；如果来自媒体转载，则降为 C 级。不得另建可独立编辑的 `company_source_registry`；企业源必须进入核心 Source Registry，并通过内容分类或 capability 标记企业公告角色。

| 企业/平台 | 关注内容 | 采集入口 | 频率 | 关联 |
| --- | --- | --- | --- | --- |
| 中国石化 | 原油采购、炼化、PX/PTA/MEG 项目 | 官网、上交所、港交所 | 事件/月 | 原油、芳烃 |
| 中国石油 | 原油、炼化、乙烯、MEG 相关 | 官网、上交所、港交所 | 事件/月 | 原油、乙烯 |
| 中海油 | 原油产量、油气项目 | 官网、港交所、上交所 | 事件/月 | 原油供应 |
| 恒力石化 | PX、PTA、聚酯、炼化项目 | 上交所、官网 | 事件/月 | PX/PTA |
| 荣盛石化 | 炼化、PX、PTA、聚酯 | 深交所、官网 | 事件/月 | PX/PTA |
| 恒逸石化 | PTA、聚酯、文莱炼化 | 深交所、官网 | 事件/月 | PTA/PX |
| 桐昆股份 | PTA、聚酯、长丝链条 | 上交所、官网 | 事件/月 | PTA/聚酯 |
| 新凤鸣 | PTA、聚酯、长丝链条 | 上交所、官网 | 事件/月 | PTA/聚酯 |
| 东方盛虹 | 炼化、PX、PTA、聚酯 | 深交所、官网 | 事件/月 | PX/PTA |
| 卫星化学 | 乙烯、EO、MEG 相关链条 | 深交所、官网 | 事件/月 | MEG/乙烯 |
| 宝丰能源 | 煤化工、烯烃、MEG 相关 | 上交所、官网 | 事件/月 | 煤制 MEG |
| 新疆天业 | 乙二醇、煤化工相关 | 上交所、官网 | 事件/月 | MEG |
| SABIC | 石化、MEG、乙烯 | 官网/公告 | 事件/月 | MEG 国际 |
| Reliance Industries | 炼化、PX、PTA、聚酯 | 官网/公告 | 事件/月 | 亚洲 PX/PTA |
| Indorama Ventures | PTA、PET、聚酯全球产能 | 官网/公告 | 事件/月 | PTA/PET |
| Lotte Chemical | MEG、乙烯、化工 | 官网/公告 | 事件/月 | MEG |
| Formosa Plastics / Nan Ya | PTA、MEG、塑化 | 官网/公告 | 事件/月 | PTA/MEG |
| Sinopec Yizheng / 仪征化纤 | 聚酯、PTA 相关 | 上市公告/官网 | 事件/月 | PTA/聚酯 |
| Saudi Aramco | 原油、炼化、化工项目 | 官网/公告 | 事件/月 | 原油/PX |
| ADNOC | 原油、石化、物流 | 官网/公告 | 事件/月 | 原油/PX |
| QatarEnergy | LNG、化工、乙烯链条 | 官网/公告 | 事件/月 | 能源/MEG |
| Maersk | 航线、红海绕航、运价附加费 | 官网/公告 | 事件 | 航运 |
| MSC | 航线、红海绕航、附加费 | 官网/公告 | 事件 | 航运 |
| CMA CGM | 航线、附加费、港口延误 | 官网/公告 | 事件 | 航运 |
| COSCO Shipping | 航线、港口、运价政策 | 官网/公告 | 事件 | 航运 |

#### 5.5.6 推荐 HS 编码和关键词库

贸易数据抓取时要按 HS 编码和关键词双轨检索。

| 对象 | 关键词 | 常用 HS 编码候选 |
| --- | --- | --- |
| 原油 | crude oil, petroleum oils, 原油 | 2709 |
| 石脑油 | naphtha, 石脑油 | 2710 相关子目 |
| PX | para-xylene, p-xylene, PX, 对二甲苯 | 290243 |
| PTA | purified terephthalic acid, PTA, 精对苯二甲酸 | 291736 |
| MEG | mono ethylene glycol, ethylene glycol, MEG, 乙二醇 | 290531 |
| 乙烯 | ethylene, 乙烯 | 290121 |
| 甲醇 | methanol, methyl alcohol, 甲醇 | 290511 |
| EO | ethylene oxide, 环氧乙烷 | 291010 |
| PET 切片 | PET resin, polyester chips, 聚酯切片 | 390761 / 390769 |
| 钛白粉 | titanium dioxide, TiO2, 钛白粉 | 320611 / 282300 |
| 煤炭 | coal, steam coal, thermal coal, 动力煤 | 2701 |

注意：HS 编码会因国家、年份、子目和统计口径变化而不同，最终入库前需要由人工确认一版 `hs_code_mapping.yaml`。

## 6. 爬虫和数据采集架构

### 6.1 总体数据流

```text
Source Registry
-> Scheduler
-> Fetcher
-> Parser
-> Normalizer
-> Validator
-> Raw Store + Structured DB
-> 现有行情 / 新闻 / 观测存储

现有预测链（冻结）：
合格观测 -> Point-in-time Snapshot -> Prediction Engine -> Ledger + Review Engine

新增情报链（单向隔离）：
来源与新闻只读投影
-> append-only Intelligence Revisions
-> Event Clustering + Relevance
-> Global Radar + Daily Brief + Spatial View

现有 RAG / AI Research Assistant 保持当前白名单；v37 情报不自动进入。
```

### 6.2 Source Registry

每一个正式数据源都必须先登记到核心源注册表，不能为情报中心再建立第三份手工来源真相。现存 `NEWS_SOURCES` 在过渡期只提供新闻 connector 和内容分类；情报目录由核心 registry 与该 connector 定义只读派生、按 `source_id` 去重，并显式报告等级或分类漂移。

建议字段：

```json
{
  "source_id": "eia_petroleum_api",
  "source_name": "EIA Petroleum API",
  "tier": "A",
  "category": "energy",
  "url": "https://www.eia.gov/opendata/documentation.php",
  "crawl_type": "api_json",
  "auth_type": "api_key",
  "frequency": "daily_or_weekly",
  "rate_limit": "follow_provider_policy",
  "products": ["crude", "naphtha"],
  "fields": ["date", "series_id", "value", "unit"],
  "parser": "eia_v2_parser",
  "freshness_sla_minutes": 120,
  "license_note": "official API; comply with EIA terms",
  "reliability_score": 0.95,
  "intelligence_capabilities": {
    "display_internal": true,
    "store_metadata": true,
    "store_raw": false,
    "redistribute": false,
    "attribution_required": true
  }
}
```

Source Registry 作用：

```text
1. 控制采集频率。
2. 记录访问方式、使用条款和机器可执行的存储/展示边界。
3. 统一源可信度。
4. 支持源失效状态进入运维面和下一份日报。
5. 支持结论溯源。
6. 保留治理分类与内容分类，显式暴露定义漂移。
```

### 6.3 Fetcher 类型

| Fetcher | 适用对象 | 技术方案 |
| --- | --- | --- |
| API Fetcher | EIA、授权行情源、商业数据 API | Python httpx/requests，带重试、签名、API key 管理 |
| Static HTML Fetcher | OPEC 新闻、交易所公告、SAFE 页面 | httpx + selectolax/BeautifulSoup |
| Browser Fetcher | JS 渲染页面、交易所动态表格 | Playwright，无头浏览器，截图/HTML 双保存 |
| PDF Fetcher | OPEC MOMR、IEA 报告、研报 PDF | 下载 PDF，保存 hash，进入 PDF Parser |
| RSS Fetcher | UN Web TV、媒体 RSS、公告 RSS | feedparser，按 guid 去重 |
| File Fetcher | CSV/XLSX/ZIP 下载 | 下载后校验 hash 和日期 |
| Vendor Connector | 经单独接入的商业数据服务 | 仅使用明确允许的 API/导出；当前 CCF 软移除 |

### 6.4 Parser 设计

Parser 分为三层。

第一层：确定性解析。

```text
JSON API -> schema parser
CSV/XLSX -> pandas/openpyxl
HTML 表格 -> lxml/selectolax
RSS -> feedparser
```

第二层：半结构化解析。

```text
PDF 表格 -> pdfplumber / tabula / camelot
PDF 正文 -> pymupdf / marker
公告正文 -> rule-based section splitter
```

第三层：LLM 结构化抽取。

适用于新闻、公告、研报摘要，使用当前配置的 DeepSeek-compatible 模型输出固定 JSON；模型不可用时必须显式降级：

```json
{
  "event_title": "OPEC+ 释放延长减产信号",
  "event_type": "opec_policy",
  "entities": ["OPEC+", "crude", "naphtha", "px", "pta"],
  "impact_direction": "upward_pressure",
  "impact_strength": 0.78,
  "horizons": ["D1", "D7"],
  "evidence_level": "B",
  "summary": "减产信号强化原油供应收紧预期，可能抬升PX/PTA成本。",
  "counter_evidence": ["若需求走弱或EIA库存大增，利多可能削弱"]
}
```

LLM 抽取要求：

```text
1. 必须引用 source_id、url、published_at。
2. 必须区分事实和推断。
3. 不允许把低证据假设写成确定结论。
4. 对“背后势力”类问题，用利益相关方和动机分析替代阴谋式断言。
```

### 6.5 Normalizer 设计

统一所有数据的字段、单位、时区和品种映射。

核心字段：

```json
{
  "observed_at": "2026-05-22T08:30:00+08:00",
  "source_id": "ice_brent_delayed",
  "instrument": "Brent",
  "instrument_type": "futures",
  "contract": "BRN2607",
  "value": 104.21,
  "unit": "USD/bbl",
  "change_pct": 2.88,
  "currency": "USD",
  "timezone": "UTC",
  "quality_flag": "valid"
}
```

品种映射表示例：

| 标准品种 | 别名 | 上游/下游关系 |
| --- | --- | --- |
| crude_oil | Brent、WTI、SC、原油 | -> naphtha |
| naphtha | 石脑油 | -> PX |
| px | PX、对二甲苯、paraxylene | -> PTA |
| pta | PTA、精对苯二甲酸、TA | -> polyester_cost |
| meg | MEG、乙二醇、EG | -> polyester_cost |
| coal | 动力煤、煤炭 | -> coal_based_meg |
| usd_cny | 美元人民币、USDCNY、中间价 | -> import_cost |

### 6.6 Validator 和质量控制

每批数据入库前做校验：

```text
1. 字段完整性：date、value、unit、source_id 不可缺。
2. 单位一致性：USD/bbl、RMB/ton、USD/ton 等必须标准化。
3. 异常值检测：单日价格波动超过阈值触发证据复核或二级源验证。
4. 交叉验证：同一品种至少保留官方/商业/行情三个维度。
5. 版本控制：同一来源修订数据时保留历史版本。
6. 源健康监控：连续失败、延迟、页面结构变化进入运维状态和下一份日报，不发送即时提醒。
```

异常样例：

```text
如果 PX 单日上涨超过 8%，但原油、石脑油、ZCE PX 期货均无对应波动，系统标记为 suspect，不直接参与预测。
```

### 6.7 去重和事件合并

事件型数据容易重复。系统需要按以下方式合并：

```text
canonical_url hash
title similarity
published_at time window
entity overlap
event_type
```

合并后的事件展示投影如下；稳定 ID、revision、证据边和状态的规范合同以 `docs/industrial-intelligence-center.md` 第 11 节为准：

```json
{
  "event_id": "5a0aeb3e-6bf1-5d6f-a723-8abf3ea9d2ef",
  "event_revision_id": "7f20c87a-6b60-51ae-a37b-5caef822dd71",
  "revision_no": 1,
  "title": "OPEC+ 释放延长减产信号",
  "first_seen_at": "2026-05-22T06:10:00+08:00",
  "last_seen_at": "2026-05-22T08:20:00+08:00",
  "source_tiers": ["A", "B"],
  "origin_group_count": 2,
  "status": "open",
  "payload_sha256": "<64-hex>"
}
```

## 7. 存储设计

### 7.1 Raw Store

原始内容按来源权利和业务需要保存，不再采用“所有正文永久留存”的笼统规则：

```text
允许原始保存：只在策略明确允许时，将有界 HTML/PDF/JSON/CSV/XLSX 保存到服务端内容寻址对象区，并记录原始 hash、捕获时间、解析器版本和保留期限。
只允许元数据：仅保存标题、URL、来源、时间、内容 hash 和权利快照；来源 excerpt/正文必须为空。若策略允许基于这些元数据形成派生事实摘要，只能写入独立事件修订并标记 derivation，不能冒充来源原文。
只允许临时缓存：按来源期限从对象区清除内容，同时保留允许的审计指针、hash 和追加式过期/tombstone 修订。
不允许保存：只展示原站链接和不可得原因。
```

现有本地部署继续使用受权限保护的运行目录和 SQLite 附属工件；未来若引入对象存储，也必须使用不可变路径与内容寻址：

```text
服务端对象键：raw/{source_id}/{yyyy}/{mm}/{dd}/{content_hash}
对外只暴露 opaque object ID，不返回或接受可解析的文件系统路径
```

目的：

```text
1. 在来源允许时复盘原文。
2. 页面结构变化时可以重跑 parser。
3. 结论可追溯。
4. 避免“Agent 事后改口”。
5. 不因为追溯需求突破来源的保存和再展示边界。
```

### 7.2 Structured DB

当前系统使用 SQLite、编号迁移、在线迁移前备份、`PRAGMA user_version` 和精确 schema 身份校验。现有行情、新闻、事件、RAG、预测和审计表继续按各自合同运行。计划中的 v37 只新增隔离情报域：

```text
intelligence_item_revisions
intelligence_event_revisions
intelligence_event_evidence
intelligence_daily_briefs
intelligence_runs
intelligence_feedback
intelligence_search_fts（可重建派生索引，不是业务事实表）
```

证据、事件修订和日报为 append-only；UPDATE/DELETE 由数据库 trigger 禁止。投影、纠错、合并、拆分和失效均追加 revision 或 invalidation。可输出的 item/event/brief 内容记录由数据库约束固定 `prediction_eligible=false` 和 `instruction_eligible=false`；证据边、运行审计和反馈不承载这两个无意义字段。

v37 不回填旧数据；部署后由独立投影任务按有界 keyset 批次读取现有新闻。旧版本无法读取 v37 时，回滚必须停 writer 并恢复已校验的 v36 备份，不能只改 `user_version` 或删除新表。

### 7.3 RAG 文档库

下列内容描述现有 RAG 能力。v37 情报首期使用自己的有界 FTS，只索引权利允许展示的标题、来源摘录或明确标为派生的事实摘要与研判；`metadata_only` 来源的 excerpt 必须为空。权利收紧或到期时，不可变业务 revision 保留审计身份，但必须立即从派生 FTS 删除受限文本，清理完成前搜索失败关闭。不得直接写入现有 `semantic_documents`、事件情报快照或 Assistant 上下文白名单。未来若要打通，必须单独评审证据资格、提示注入、point-in-time 和预测污染风险。

RAG 保存：

```text
OPEC/IEA/EIA 报告
交易所公告
行业研报
每日晨报
事件推演
预测账本
复盘报告
AI 研究助手问答
人工备注
```

文档元数据：

```json
{
  "doc_id": "doc_opec_momr_202605",
  "source_id": "opec_momr",
  "title": "OPEC Monthly Oil Market Report",
  "published_at": "2026-05-15",
  "entities": ["OPEC", "crude", "supply", "demand"],
  "products": ["crude", "naphtha", "px", "pta"],
  "evidence_level": "A",
  "url": "https://www.opec.org/monthly-oil-market-report.html"
}
```

### 7.4 知识图谱

现有知识图谱继续由当前版本化 RAG/持久化边界维护，本轮不引入 Neo4j、PostgreSQL 或第二套图数据库。v37 情报只在自己的不可变事件与证据边中表达关系，不自动写入现有知识图谱；未来合并必须另做架构和证据资格评审。

节点类型：

```text
Commodity：原油、石脑油、PX、PTA、MEG、煤炭、乙烯、甲醇
Event：OPEC 减产、EIA 库存、地缘冲突、制裁、装置检修
Organization：OPEC、EIA、IEA、OFAC、产油国、炼厂、贸易商
Indicator：库存、开工率、加工费、价差、汇率、美元指数、运费
Output：晨报、事件推演、预测记录、复盘报告
```

关系类型：

```text
affects
drives
pressures
offsets
benefits
hurts
transmits_to
verified_by
contradicted_by
reviewed_by
```

示例：

```text
OPEC 减产 -> affects -> 原油供应预期
原油供应预期 -> drives -> Brent
Brent -> transmits_to -> 石脑油
石脑油 -> transmits_to -> PX
PX -> transmits_to -> PTA
PTA -> pressures -> POY/DTY 上游原料成本压力
```

## 8. AI / DeepSeek-compatible 模型使用方式

当前配置的 DeepSeek-compatible 模型不直接承担“数据源”角色，而承担受证据约束的推理、抽取、解释和对话角色。模型名称可以随部署配置变化，项目合同不依赖尚未验证的具体版本名称。

### 8.1 主要任务

```text
1. 新闻和公告结构化抽取。
2. 事件影响路径推理。
3. 政治事件利益相关方分析。
4. RAG 问答。
5. 每日晨报生成。
6. 预测复盘原因总结。
7. AI 研究助手对话。
```

### 8.2 不应让大模型单独完成的任务

```text
1. 实时价格获取。
2. 库存和开工率计算。
3. 预测账本改写。
4. 未经来源支持的事实判断。
5. 高置信度“幕后操纵”断言。
```

### 8.3 政治推演防幻觉规则

输出必须包含：

```text
事实
利益相关方
动机推断
可能受益方
可能受损方
证据等级
反证
对原料链条的影响
置信度
```

禁止输出：

```text
1. 无证据断言某一势力操纵事件。
2. 把单一媒体传闻写成事实。
3. 忽略反向风险。
4. 不给出处的地缘政治结论。
```

## 9. 预测和复盘闭环

### 9.1 当前正式预测合同

当前预测以 `seven-product-forecast.v1` 为权威合同：原油、石脑油、PX、PTA、MEG、POY、DTY 七个目标各自产生 D1、D7、D30，共 21 格。每格保存连续点预测、区间、方向、置信度、数据状态、关键驱动、证据、标签/模型/特征/评测版本和数据快照哈希。

旧的单一 `cost_pressure_index` 与“POY/DTY 上游成本压力”标量只作 `historical_legacy_contract` 审计/参考，不能映射、回填或展示为当前 21 格 formal 预测。情报中心中的影响方向也只是研判，不能替代这一合同。

### 9.2 预测账本

每个业务日的冻结批次与 21 个 cell 追加写入现行不可变账本。下列为单个 cell 的简化展示投影，不是可由客户任意写入的请求模型：

```json
{
  "schema_version": "seven-product-forecast.v1",
  "batch_id": "seven-<content-id>",
  "as_of_time": "2026-05-22T08:20:00+08:00",
  "target": "pta",
  "horizon_days": 7,
  "point_forecast": null,
  "interval_low": null,
  "interval_high": null,
  "direction": "uncertain",
  "confidence": 0.0,
  "formal_status": "insufficient_data",
  "formal_eligible": false,
  "data_status": "insufficient",
  "evaluation_status": "not_evaluated",
  "label_series_id": "<frozen-label-id>",
  "model_version": "<version>",
  "feature_version": "<version>",
  "data_snapshot_sha256": "<64-hex>"
}
```

示例故意展示数据不足格：真实无数值时必须使用 `null` 与 `model_unavailable | insufficient_data`，不能以 0 填补。完整模型以当前 OpenAPI 和 ADR-0001 为准。

### 9.3 到期复盘

复盘只在目标日和真实标签已到期可见后进行，并保留事实结果或 invalidation，不改写已发出的批次。每格必须分开报告：

```text
点预测与真实值、绝对/相对误差
区间是否覆盖真实值
三分类方向是否命中
与价格不变、冻结季节性两个朴素基线的误差差异
样本量、不确定性、最差阶段、极端行情和结构变化
标签、数据快照、模型、特征和评测哈希
数据滞后、修订、泄漏或基线反超等失败原因
```

模型或权重调整必须另行预注册候选、保留冻结选择/最终测试窗并通过治理晋级；不得看完本周结果就直接改生产参数。未成熟格仍诚实保持 `reference | degraded | insufficient_data | model_unavailable`。

## 10. 风险信号设计：只进入日报，不即时推送

系统继续识别价格异动、库存变化、装置检修、政策/制裁、汇率破位、事件升级和来源失效，但本轮明确不建设即时提醒能力：不使用短信、微信、邮件、浏览器通知、WebSocket、SSE 或定时轮询推送。即使高严重度事件也只在下一份每日情报摘要中出现，或由使用者主动打开全球雷达查看。

风险识别是内部排序字段，不是采购指令。触发条件可以包括：

```text
Brent 单日涨跌超过 3%
PX 连续 3 日上涨
PTA 主力合约突破近 20 日高点
MEG 港口库存连续下降且价格上行
人民币中间价连续贬值
OFAC 新增能源/航运相关制裁对象
OPEC 发布临时会议或产量声明
关键港口、航线、炼厂或化工节点附近发生地震、火灾或极端天气
```

日报中的风险信号最小结构：

```json
{
  "signal_level": "high",
  "trigger": "Brent单日上涨超过3%，PX连续3日上涨",
  "affected_products": ["crude", "naphtha", "px", "pta"],
  "inference": {
    "expected_impact": "上游原料成本压力可能上升",
    "time_horizon": {"D1": "upward_pressure", "D7": "upward_pressure", "D30": "unclear"},
    "confidence": 0.68
  },
  "counterevidence": ["库存或开工数据尚未确认"],
  "watch_items": ["PX亚洲报价", "PTA装置开工", "MEG港口库存", "人民币汇率"],
  "evidence": ["independent-origin-1", "independent-origin-2"],
  "delivery": "next_daily_brief_only",
  "action_boundary": "information_only"
}
```

同一原始消息被多个聚合器或媒体转载时只计一份独立证据。C/D 级发现信号不得单独进入高置信结论；数据缺失时方向必须为 `unknown`，不能由模型补造。

## 11. 前端信息架构

现有七个工作台模块的名称、顺序、URL、数据依赖和页面语义保持不变，只在末尾追加第八个模块：

```text
1. 总览看板
2. 行情与原料链
3. 事件与风险
4. 证据图谱
5. Agent 系统
6. AI 研判助手
7. 研判报告
8. 情报中心（新增）
```

情报中心使用独立 URL 状态 `?module=intelligence`，内部包含四个可深链视图：

| 视图 | 参数 | 作用 |
| --- | --- | --- |
| 每日摘要 | `intelView=brief` | 默认入口；只展示服务端冻结并发布的情报摘要 |
| 全球雷达 | `intelView=radar` | 浏览和筛选广泛收集后的事件簇 |
| 全球态势 | `intelView=map` | 在地图上理解事件、港口、航线和关键节点的空间关系 |
| 来源与运行 | `intelView=operations` | 查看来源等级、权利、健康、采集运行与缺口 |

事件详情由摘要、雷达和地图共用：桌面端使用右侧详情区，移动端使用可访问抽屉。详情必须展示原始来源和聚合来源、首次发现和最后确认时间、事实、推断、反证、D1/D7/D30、内容哈希和使用边界。

地图首期采用 `MapLibre GL JS + react-map-gl`，只在进入全球态势页时懒加载；默认使用随应用发布的固定版本 Natural Earth 数据，不调用 MapLibre 演示瓦片或 OSM 公共瓦片。地图不可用时，同一数据必须仍能通过键盘可访问列表完整查看。

情报中心不加入现有 `WorkbenchLiveData` 或预测快照。刷新情报不能触发预测链全量刷新；打开现有任一模块也不能预取情报或地图代码。

## 12. 增量情报中心实施计划

原有数据、预测、复盘、RAG 和 Agent 能力继续维护。本节只描述 2026-09-05 确认的新增情报能力，技术完成与20个工作日的价值验证分开计算。

### 阶段 0：文档、隔离合同与回归基线

```text
固化产品修正、ADR、数据/API/UI/权限合同
冻结预测、现有事件提升和 Assistant 上下文边界
记录现有七模块、预测 API、数据库和测试基线
定义迁移备份、失败停止和恢复路径
```

### 阶段 1：现有来源的单向情报投影

从现有 Source Registry、新闻来源、`news_articles` 和 `news_event_clusters` 只读投影到独立 `intelligence_*` 域。投影不得调用旧新闻写入、事件提升、预测写入或现有 RAG 索引函数。

```text
派生并去重来源目录，不创建第三份手工来源真相
保留 collector / aggregator / origin 三层血缘
按内容哈希追加修订，重复运行不生成重复事实
保存采集时的来源权利快照
所有可输出的 item/event/brief 内容记录固定 prediction_eligible=false 且 instruction_eligible=false
```

### 阶段 2：事件簇、产业关联与每日摘要

```text
把转载与同源聚合结果归并为一个事件簇
独立原始来源数与聚合命中数分开计算
形成事实、推断、反证、缺口、D1/D7/D30 和观察项
生成宽全球雷达和窄产业摘要
08:20 冻结，09:30 发布；同一业务日幂等且不可覆盖
```

### 阶段 3：零强制付费的新增来源

| 波次 | 来源 | 角色 | 边界 |
| --- | --- | --- | --- |
| 1A | 现有 `gdelt_oil_geopolitics_rss` | 全球新闻发现 | 复用并收紧现有 C 级 connector；metadata-only，不创建重复 source ID/抓取器，不单独支持高置信结论 |
| 1A | USGS Earthquake GeoJSON | 全球地震事实 | 地震事实可为 A 级；产业影响仍需地理关联和其他证据 |
| 1B | Natural Earth + Natural Earth Ports | 固定全球底图、行政范围和港口空间参照 | 固定版本、随应用发布、哈希清单、无运行时网络请求；港口坐标不作航行数据 |
| 后置 | NASA FIRMS | 关键节点附近火灾 | 需免费 Key，只查关键节点缓冲区，不抓全球全量 |
| 后置 | AISStream | 船舶位置弱信号 | Beta/条款风险；短缓存，不推断货种、装卸量或完整贸易流 |
| 禁用能力 | OSM 公共瓦片 | 可替换详细底图 | 当前 CSP、Referer、缓存和 SLA 条件不满足，默认禁用 |

飞机、军机、卫星、CCTV、广播、共享单车等视觉效果强但产业价值低的 God’s Eye View 图层不进入首期。

首版另生成只读、版本化的 `industrial_nodes.v1.geojson`：港口从固定 Natural Earth Ports 派生；运河和炼化/PX/PTA/MEG 装置仅在 A/B 一手来源提供可核验位置时加入。地名重名、坐标未知或只有国家级位置时保持 unresolved；地图不得用首都、中心点或模型猜测补齐。节点邻近只能形成待核实的空间线索，不能单独确认供应影响。

### 阶段 4：全球态势与操作者反馈

```text
新增懒加载 MapLibre 全球态势页
只展示具有可审计坐标或区域精度的事件
提供同数据列表、键盘操作、移动端详情抽屉和 WebGL 降级
相关/无关/重复、继续/取消观察，以及来源/主题静音或取消静音，只影响未来排序与显示；观察状态不产生提醒
不改变原始证据、事件修订或预测
```

### 阶段 5：20个工作日价值实验

上线不等待实验窗口。技术验收完成后独立运行20个工作日，验证节省研究时间、Top 事件相关性、重大事件召回、重复暴露和日报可用性；实验不改变预测 OOS 完成条件。

## 13. 当前技术栈与新增边界

| 层 | 当前技术 | 本轮新增或约束 |
| --- | --- | --- |
| 前端 | React 18、Vite 8、TypeScript、Ant Design、Recharts、React Flow | `MapLibre GL JS + react-map-gl` 仅在全球态势页懒加载；新样式使用独立前缀 |
| 后端 | Python 3.11+、FastAPI、Pydantic、httpx | 新增薄路由、情报服务、来源目录、投影和 provider 模块；外部 URL 必须固定构造并通过 allowlist |
| 存储 | SQLite、编号迁移、在线备份、精确 schema 校验 | 计划以 v37 新建隔离、append-only 的 `intelligence_*` 表和独立 FTS；不回填或改写预测表 |
| 检索 | 当前独立版本化 RAG/FTS/向量索引 | 情报首期使用独立 FTS，不能进入现有 Assistant 白名单或 `semantic_documents` |
| AI | 结构化输出、证据约束和本地安全回退 | AI 只生成有引用的研判字段；缺失数据返回 `unknown`，不能生成采购指令 |
| 地图数据 | 无现行地图依赖 | 首期使用同源 Natural Earth GeoJSON；不默认访问第三方瓦片、字体或浏览器私密密钥 |

God’s Eye View 是参考实现，不是依赖底座。只选择性复用其 MIT 代码中的数据层生命周期、`live/stale/degraded/unavailable` 状态表达、请求合并、serve-stale、署名和空间交互思想；不复制其第三方数据包，不整仓合并，也不运行第二套生产服务。

## 14. 合规和风控

本项目是单操作者工作台，但用于生产经营和采购研判时仍可能构成商业用途。网页公开可见不自动授予复制、长期保存、再分发或商业使用权；同时，本项目不恢复已经废止的内部 license/manifest 人工审批仪式。来源在接入时按机器可执行的能力合同自动判断，条件不满足就降为链接/元数据、禁用或明确缺口。

每条情报修订必须冻结当时的来源权利快照：

```text
允许内部展示
允许保存元数据
允许保存原始内容
允许长期缓存
允许再分发
是否要求署名、署名文本和链接
条款检查时间、策略版本和备注
```

采集和投影必须遵守：

```text
1. 遵守 robots.txt、网站 ToS、数据授权协议。
2. 不绕过登录、验证码、付费墙和反爬限制。
3. 未知权利默认只保存有界元数据、标题、链接、时间和哈希，不再次复制全文。
4. 所有请求使用固定 HTTPS 端点、出站域名白名单、逐跳重定向校验、超时、响应大小限制和保守限流。
5. 保存 collector、aggregator 和 origin 的独立身份；聚合器不得冒充原始来源或独立交叉验证。
6. 条款变化不改写历史 rights snapshot，但当前与未来的采集、展示、索引、缓存和再分发必须立即按新策略收敛；需要清除原始对象时按精确保留策略执行并追加 tombstone/invalidation。
7. 浏览器不保存私密 API Key；地图只消费同源后端 API 和静态资产。
8. 所有情报和预测输出标注“仅供经营研判，不构成采购、交易或投资指令”。
```

政治分析必须遵守：

```text
1. 不输出无证据的阴谋论。
2. 对“背后势力”使用利益相关方、动机、能力、受益路径、证据等级和反证框架。
3. 将事实、推断、假设分开。
4. 低证据内容不得进入高置信度结论。
5. 外部新闻和 feed 文本均是不可信证据，不能成为系统指令。
6. 情报反馈只调整排序和显示，不能修改原始事实或来源血缘。
```

全部 Intelligence API 复用现有内部鉴权或本地会话边界。写入投影、日报物化和反馈必须鉴权、限流、幂等并记录审计；不新增任意 URL 抓取接口、即时通知接口或前端可读取的服务端密钥。

## 15. 成功指标

情报价值主要指标（上线后独立运行20个工作日）：

```text
从打开系统到形成可引用 Top 3/Top 5 的中位时间：<= 10 分钟
相对当前人工流程的研究时间下降：>= 50%
每日 Top 5 人工相关率：>= 80%
预先定义的重大事件进入全球雷达的发现召回率：>= 90%
截止前满足证据门禁的重大事件进入日报高优先区的召回率：>= 90%
日报可直接用于晨会或只需小改的比例：>= 80%
```

情报质量护栏：

```text
具有原始 URL、来源、发布时间/首次发现时间和证据等级：100%
事实、推断、反证和缺口明确区分：100%
重复事件暴露率：<= 5%
09:30 日报按时生成率：>= 95%
旧闻或修订被误报为全新事件：0
弱来源单独形成高置信结论：0
禁止进入预测的数据被写入预测/RAG正式链：0
```

来源和运行指标：

```text
来源定义漂移、权利未知、解析失败和数据截断显式可见
采集运行成功/部分成功/失败、耗时、积压和最近成功时间可观测
重复投影不产生新 item/revision，内容变化只追加新 revision
所有可输出的 item/event/brief 内容记录 prediction_eligible=false 且 instruction_eligible=false
```

预测指标继续沿用现有七品种正式合同，并与上述情报指标分账：

```text
逐格 OOS 样本量和相对朴素基线改善
D1/D7/D30 方向准确率和价格误差
正式、参考、降级和不可用状态
到期结算、复盘和模型版本记录
```

阅读量、地图浏览量和情报相关率不能替代预测质量；预测得分也不能证明情报工作台节省了研究时间。

## 16. 一句话总结

这个系统不是新闻瀑布或炫技地图，而是一个面向 POY/DTY 生产经营者的工业情报工作台：广泛收集全球信号，将其去重、溯源并压缩成与原料采购和供应风险有关的每日研判；现有七品种预测继续作为独立、可复盘且诚实标注证据边界的参考能力。

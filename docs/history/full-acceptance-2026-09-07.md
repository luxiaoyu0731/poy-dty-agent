> 历史记录：本文仅记录当时版本，不能作为当前完成状态或验收结论。当前入口见 [文档导航](../README.md)。

# 公网全应用验收报告 — 2026-09-07


## 1. 一句话结论

本轮验收不通过：公网主体可用，但每日情报、Agent实际执行和正式报告多个核心产出未跑通，不能把界面修复或部署成功当作产品验收完成。

## 2. 当前总分

**59/100**。这是当前功能与实测证据分，不是预测准确率。

## 3. 发布判定

**NO-GO（全产品验收）**。未发现P0，但多个核心交付链路不可用；已有行情阅读和非正式参考预测可以使用，不等于完整产品通过验收。

本轮范围：2026-09-07晚间，Asia/Shanghai；用户Chrome的Computer Use为主证据，亲自点击、滚动、刷新、键盘、地址栏、缩放、调整窗口、原生DevTools。HTTP仅补充健康与版本证据。未改应用代码、部署、重启、抓取、生成、改设置、读凭据或操作主库。

报告完成表示60个流程编号均有判定，**不表示全部功能都被成功执行**。未做的分支明确扣除证据分，不以旧报告或代码测试抵消。

## 4. 实际覆盖矩阵

| 页面 | 判定 | 实测结论 |
|---|---|---|
| 总览看板 | 部分通过 | 观察结论/快捷/展开可用；健康范围提示不足 |
| 行情与原料链 | 部分通过 | 三套七品种选择、日期窗口可用；历史覆盖与窄窗问题 |
| 事件与风险 | 部分通过 | 搜索/详情/外链/追加可用，但有超时、追加丢失 |
| 证据图谱 | 部分通过 | 8步5过滤已用；节点和平移恢复有效，引用链不完整 |
| Agent系统（运行状态） | 失败 | 执行和固化均0/12 |
| AI研判助手 | 部分通过 | 输入展开可用；证据卡仅标题；真实回答未测试 |
| 研判报告 | 部分通过 | 四类型可切换；正式文件未生成 |
| 工业情报中心 | 失败 | 日报缺失、投影失败，雷达后续空，地图仅底图 |
| 预测账本与到期复盘 | 部分通过 | 21组合、CSV和一条MEG结算可核对；正式0/21 |
| 登录/会话/刷新/深链/历史 | 部分通过 | 当前公网可进入和恢复；完整访问控制未验证 |

| 情报二级页 | 判定 | 证据事实 |
|---|---|---|
| 每日摘要 | 失败 | 09:30计划发布时间后仍无摘要；刷新无产物 |
| 全球雷达 | 部分通过 | 初次503；品种8项类别9项均操作，后来恢复空列表 |
| 运行与来源 | 部分通过 | 来源7页、审计3页均翻页，技术详情/刷新已用 |
| 地图及惰性加载 | 部分通过 | 专属资源随后加载200，平移缩放有效；无事件坐标 |

60项统计：**通过9、部分通过39、失败3、未验证9**。编号统计不等于模块通过率。完整逐项记录见文末。

## 5. P0/P1/P2/P3问题清单

P0：0；P1：3；P2：10（其中2项是证据缺口）；P3：1。每条只归属一个缺口类别。


### F01 · P1 · 工业情报日报缺失、投影任务重复失败

- 页面与URL：[打开](https://app.kaipingrc.com/?module=intelligence&intelligenceView=summary)
- 复现：每日摘要→刷新→查看运行与来源→翻运行页。
- 期望：发布时间后有真实日报和可追溯引用。
- 实际：21时后仍无日报，legacy_news_articles多条时区错误intelligence_timestamp_timezone_required；雷达后续为空。
- 用户影响：无法使用每日情报和事件详情。
- 证据：[08-summary.png](/path/to/project/agent-context/full-acceptance-20260907/08-summary.png)，[08-run-failures.png](/path/to/project/agent-context/full-acceptance-20260907/08-run-failures.png)
- 稳定性：缺失状态稳定；多条已发生失败记录，本轮未重新运行投影。
- 类别：**G**；扣分：**8**。

### F02 · P1 · 事件检索间歇超时，雷达曾返回503

- 页面与URL：[打开](https://app.kaipingrc.com/?module=events)
- 复现：搜FOREX、等待结果、重试；切供需；进入雷达。
- 期望：返回匹配结果，重试有可预期恢复。
- 实际：FOREX首次超时后复测23条；供需超时；雷达初次503后恢复空。
- 用户影响：日常检索被中断，稳定性未达标。
- 证据：[03-search-result.png](/path/to/project/agent-context/full-acceptance-20260907/03-search-result.png)，[03-supply-final.png](/path/to/project/agent-context/full-acceptance-20260907/03-supply-final.png)，[10-console-record.png](/path/to/project/agent-context/full-acceptance-20260907/10-console-record.png)
- 稳定性：间歇复现，不宣称每次都失败。
- 类别：**G**；扣分：**4**。

### F03 · P1 · Agent真实执行和正式报告未跑通

- 页面与URL：[打开](https://app.kaipingrc.com/?module=workflow)
- 复现：点12流程节点、刷新；报告切日报周报复盘专题。
- 期望：真实交接记录与对应正式报告文件。
- 实际：执行0/12、固化0/12、调用0；正式文件未生成，预览复制下载禁用；日报列表日期7月24日、观察摘要使用9月数据。
- 用户影响：无法完成自动研判到正式报告交付。
- 证据：[05-workflow.png](/path/to/project/agent-context/full-acceptance-20260907/05-workflow.png)，[07-reports.png](/path/to/project/agent-context/full-acceptance-20260907/07-reports.png)
- 稳定性：刷新与类型切换后稳定；根因不能只归为等待自然日。
- 类别：**G**；扣分：**5**。

### F04 · P2 · AI证据卡只弹标题

- 页面与URL：[打开](https://app.kaipingrc.com/?module=assistant)
- 复现：滚动到问答参考材料，点原油卡。
- 期望：实质详情、时间和原文或图谱跳转。
- 实际：只弹“问答参考材料：原油”。
- 用户影响：证据问答核验路径中断。
- 证据：[06-evidence-click.png](/path/to/project/agent-context/full-acceptance-20260907/06-evidence-click.png)
- 稳定性：本轮点击一次，未单独重复复现。
- 类别：**A**；扣分：**3**。

### F05 · P2 · 图谱完成步骤与对应节点不完整

- 页面与URL：[打开](https://app.kaipingrc.com/?module=evidence)
- 复现：逐一点8个检索步骤。
- 期望：对应节点可聚焦，或解释为何不映射到节点。
- 实际：部分步骤恢复全图，无对应可聚焦节点；不再白屏。
- 用户影响：无法逐步核对完整检索路径。
- 证据：[04-steps-complete.png](/path/to/project/agent-context/full-acceptance-20260907/04-steps-complete.png)，[04-step-4.txt](/path/to/project/agent-context/full-acceptance-20260907/04-step-4.txt)
- 稳定性：遍历8步观察，未重做整组。
- 类别：**A**；扣分：**1**。

### F06 · P2 · 历史覆盖不足且行情返回曾显示旧报价

- 页面与URL：[打开](https://app.kaipingrc.com/?module=market)
- 复现：读取七品种日期，快速切模块再返回行情。
- 期望：基准与时间可核对，降级变化可解释。
- 实际：观察窗口PX/PTA/MEG/POY/DTY不足；库存开工多为7月；切换返回曾显示7月价格，与先前9月报价不同。
- 用户影响：传导依据不足，需区分来源缺口、基准差异及回退。
- 证据：[02-description.png](/path/to/project/agent-context/full-acceptance-20260907/02-description.png)，[10-rapid-switch.png](/path/to/project/agent-context/full-acceptance-20260907/10-rapid-switch.png)
- 稳定性：历史缺口多页一致；报价回退一次，根因未定。
- 类别：**G**；扣分：**3**。

### F07 · P2 · 正式预测与样本外证据未成熟

- 页面与URL：[打开](https://app.kaipingrc.com/?module=reports&reportView=ledger)
- 复现：查看21格、展开依据、核对CSV及结算表。
- 期望：正式资格有真实到期评测依据。
- 实际：正式0/21，参考21/21；原油有效样本0；已有MEG1日结算未命中，其余多等待到期。
- 用户影响：仅可观察，不能认定正式预测质量通过。
- 证据：[09-ledger-settled.png](/path/to/project/agent-context/full-acceptance-20260907/09-ledger-settled.png)，[09-csv-validation.json](/path/to/project/agent-context/full-acceptance-20260907/09-csv-validation.json)
- 稳定性：界面导出一致；等待不保证最终过模型门禁。
- 类别：**E**；扣分：**3**。

### F08 · P2 · 行情窄窗口及放大裁切

- 页面与URL：[打开](https://app.kaipingrc.com/?module=market)
- 复现：1302px下125%/200%；还原后拖窄至850px，尝试横向滚动。
- 期望：七品种、单位和时间完整可访问。
- 实际：125%日期被下方区域裁切，200%及850px右端链条超出，所试横向滚动未恢复。
- 用户影响：窄窗或放大用户无法读全信息。
- 证据：[10-zoom125.png](/path/to/project/agent-context/full-acceptance-20260907/10-zoom125.png)，[10-zoom200.png](/path/to/project/agent-context/full-acceptance-20260907/10-zoom200.png)，[10-narrow-chain-scroll.png](/path/to/project/agent-context/full-acceptance-20260907/10-narrow-chain-scroll.png)
- 稳定性：三个条件均出现；并非所有模块全部断点覆盖。
- 类别：**A**；扣分：**3**。

### F09 · P2 · 标签切换后隐藏面板保留焦点

- 页面与URL：[打开](https://app.kaipingrc.com/?module=intelligence&intelligenceView=runs)
- 复现：每日摘要点查看运行与来源，查看Chrome Console。
- 期望：焦点进入新视图，隐藏面板不保留焦点。
- 实际：Blocked aria-hidden警告，焦点button仍在隐藏summary tabpanel。
- 用户影响：辅助技术焦点状态不一致。
- 证据：[10-console-record.png](/path/to/project/agent-context/full-acceptance-20260907/10-console-record.png)
- 稳定性：Console一条，未清日志重复测试。
- 类别：**A**；扣分：**1**。

### F10 · P2 · 健康提示未区分进程与业务产出

- 页面与URL：[打开](https://app.kaipingrc.com/?module=overview)
- 复现：看总览运行正常，对照任务失败及ready响应。
- 期望：健康文字明确限定范围，并提示业务任务失败。
- 实际：总览运行正常、系统提醒正常；ready intelligence=healthy；同时日报缺失、投影失败。
- 用户影响：用户易误认业务任务已成功。
- 证据：[01-overview.png](/path/to/project/agent-context/full-acceptance-20260907/01-overview.png)，[08-run-failures.png](/path/to/project/agent-context/full-acceptance-20260907/08-run-failures.png)，[health-ready.json](/path/to/project/agent-context/full-acceptance-20260907/health-ready.json)
- 稳定性：UI与HTTP同轮存在；ready不能代表业务产出。
- 类别：**A**；扣分：**2**。

### F11 · P2 · 事件追加结果在快照更新后丢失

- 页面与URL：[打开](https://app.kaipingrc.com/?module=events)
- 复现：点加载更多30到60，滚动，跨本轮21:10到21:15快照更新。
- 期望：保留已加载结果与阅读位置，或提示更新。
- 实际：60/3261回30/3261，刚出现的Rosneft不再在已加载列表。
- 用户影响：打断连续阅读。
- 证据：[03-list-middle.png](/path/to/project/agent-context/full-acceptance-20260907/03-list-middle.png)，[03-current-list.png](/path/to/project/agent-context/full-acceptance-20260907/03-current-list.png)
- 稳定性：跨一次实际更新观察，未等第二周期。
- 类别：**A**；扣分：**1**。

### F12 · P3 · JSON导出直接替换当前页

- 页面与URL：[打开](https://app.kaipingrc.com/?module=reports&reportView=ledger)
- 复现：分别点CSV与JSON导出。
- 期望：下载文件，或文案明示打开JSON。
- 实际：CSV下载；JSON在当前标签显示原始响应，需后退。
- 用户影响：轻度打断工作流，不能记成JSON文件下载成功。
- 证据：[09-json-response.txt](/path/to/project/agent-context/full-acceptance-20260907/09-json-response.txt)，[09-csv-inspection.json](/path/to/project/agent-context/full-acceptance-20260907/09-csv-inspection.json)
- 稳定性：本轮一次，响应可读，无数据丢失。
- 类别：**A**；扣分：**1**。

### U01 · P2 · 写入、真实AI回答和全量网络访问控制未验证

- 页面与URL：[打开](https://app.kaipingrc.com/?module=assistant)
- 复现：检查发送、快捷提问、观察级生成；本轮不新增业务写入。
- 期望：生成失败重试和访问控制有独立证据。
- 实际：未提交真实计费问答、观察级生成，未完整HAR与全新浏览器配置验证。
- 用户影响：这些功能不能签署通过。
- 证据：[06-assistant.png](/path/to/project/agent-context/full-acceptance-20260907/06-assistant.png)，[09-export-json.txt](/path/to/project/agent-context/full-acceptance-20260907/09-export-json.txt)
- 稳定性：证据缺口，不是已确认程序故障。
- 类别：**G**；扣分：**4**。

### U02 · P2 · 悬停、全量引用和无数据详情分支未验证

- 页面与URL：[打开](https://app.kaipingrc.com/?module=evidence)
- 复现：核对控件与数据前置条件。
- 期望：所有关键分支可实测。
- 实际：未完成曲线首尾hover、21格全部诊断、全部来源原件、雷达地图点详情、隔离故障注入。
- 用户影响：不可用静态截图代替功能证据。
- 证据：[04-evidence.png](/path/to/project/agent-context/full-acceptance-20260907/04-evidence.png)，[08-radar-recovered-empty.txt](/path/to/project/agent-context/full-acceptance-20260907/08-radar-recovered-empty.txt)
- 稳定性：未验证，不判已知程序故障。
- 类别：**G**；扣分：**2**。


## 6. 数据真实性与跨模块一致性

- 总览、AI、报告和旧账本观察摘要一致为14日、49%低置信、偏强，并明确非正式；与七产品1/7/30日预测分开。不把库存、采购或成交价当作预测目标。
- 三套七品种选择均实际使用。PX末30日为2026-06-23至07-23共22交易日，末90日61点，全部390点。历史曲线与最新报价不同基准已有说明，不直接拼接算涨跌。
- 本轮先看到9月原油96.28美元/桶、石脑油788.59美元/吨、PX8716元/吨等报价；快速切换返回行情曾出现7月报价。截图保留，未确定是降级、基准差异还是回退，因此不判跨模块全部一致。
- 事件中文概述可见“依据：标题”，未冒充正文事实或正式方向判断。Bloomberg跳转标题匹配，遇条款弹窗停止。没有全量核验3261条中文覆盖率。
- 实际下载CSV 16796字节，21个唯一组合，formal_eligible全部False。MEG1日发布6486.368、实际6400、误差86.368，与界面6486.37/86.37一致；仅证明此条显示和导出相符，不证明所有实际价格独立真实性、结算时序或模型准确率。
- 当前七产品不可变账本已有一条结算，旧历史兼容账本0条。不能笼统写“完全没有复盘”，也不能用一个结算样本声称样本外门禁通过。
- 图谱为7月历史材料，采用3、排除4，不能充作今天正式证据数。AI参考材料有泛化业务说明，点击只弹标题。
- CCF来源为软移除；DCE/CCF退役、EIA可选、无多租户要求、非库存采购预测均不扣分。没有新增公开数据license/manifest审批。

## 7. 公网健康、网络与性能

| 只读请求 | HTTP | 单次耗时 |
|---|---:|---:|
| /api/v1/health/live | 200 | 2.638146秒 |
| /api/v1/health/ready | 200 | 2.042616秒 |
| /release.json | 200 | 1.350493秒 |

前后端release_id均为20260907T121210Z-a45a5a88fc473156，release_hash与git_sha也一致。ready的intelligence=healthy与业务任务成功不是同一概念。首次shell沙箱无法连接本机代理，已使用获准只读网络路径重测；该本机失败不算公网故障。

Chrome Console亲见 /api/v1/intelligence/events?limit=30 的503和隐藏标签页焦点警告。雷达后续恢复空列表，按间歇故障记录。FOREX复测请求200约2.40秒，事件列表某次200约5.81秒；这是用户链路耗时，不是纯后端耗时。

首次地图脚本226kB传输、810kB资源，6.50秒；CSS5.36秒；同源Natural Earth GeoJSON约840kB、7.20秒；map API约0.931秒200。平移后可见再次请求，地图交互实际生效。

可见Network窗口为https app.kaipingrc.com同源资源及Cloudflare统计；该窗口内未见localhost、内网、旧域名、混合内容、404/401/403/500。**未保存覆盖所有模块的完整HAR，不能声称全应用没有这些错误。**Computer Use自身注入的cursor-chat资源不算应用残留。

## 8. 距离100分的扣分明细

| 评分项 | 得分 | 扣分归属 |
|---|---:|---|
| 公网可达性与版本一致性 | 9/10 | F10 1 |
| 登录/会话/访问控制 | 8/10 | U01 2 |
| 八模块功能完整性 | 13/24 | F01 3、F02 1、F03 3、F05 1、F07 1、F12 1、U01 1 |
| 数据真实性/来源/新鲜度/追溯 | 10/20 | F01 2、F04 2、F06 3、F07 2、U02 1 |
| 核心链路与一致性 | 7/12 | F01 2、F03 2、F04 1 |
| 加载/空/错误/恢复 | 4/8 | F01 1、F02 1、F10 1、F11 1 |
| 易用性/可访问性/窄窗 | 3/8 | F08 3、F09 1、U02 1 |
| 性能/控制台/网络/整洁 | 5/8 | F02 2、U01 1 |
| **合计** | **59/100** | **扣41，无重复计分** |

## 9. 立即优化与外部缺口

| 分类 | 项目 | 扣分 |
|---|---|---:|
| A 代码/界面可优化 | F04/F05/F08/F09/F10/F11/F12 | 12 |
| B 已确定仅需部署/配置 | 无；未证实重启即可恢复产出 | 0 |
| C 已确定需密钥/账号/输入 | 无；不因EIA可选或既有授权重复设门槛 | 0 |
| D 已确定外部来源恢复/官方发布 | 无足够证据单列；不把程序失败推给来源 | 0 |
| E 自然日/连续运行/到期 | F07 | 3 |
| F 已接受产品边界 | 单人工作台、公网模式、退役来源、非采购库存预测等 | 0 |
| G 需进一步取证 | F01/F02/F03/F06/U01/U02 | 26 |

- **当前可立即修复：7/14项**，12分；14项中含2项未验证证据缺口。若只数已观察产品问题，为7/12。
- **不依赖自然日、已明确可追回：12分**，修复后仍需实测才加分。
- **依赖真实到期/外部事实证据：3分**；等待本身不保证模型能过门禁。
- **其余26分未确定**，不能全部算“立即修复”或“再等几天”。需查失败运行、调度和版本、缺失数据来源，并补做未验证分支。
- 不继续追分：公众注册/多租户、强制密码、库存采购成交价加工利润预测、恢复DCE/CCF、EIA必备key、公开数据额外审批、即时提醒。

## 10. 下一步行动（按收益/成本）

1. 恢复真实投影到每日摘要链；以成功任务、当日日报、真实雷达事件为验收证据，不能只看修复代码。
2. 定位Agent0/12和正式报告无文件的原因，分开检查未调度、执行失败与正式门禁。
3. 取事件检索/雷达对应时刻的安全日志和耗时，解决超时，并复测搜索与正常重试。
4. 给AI证据卡提供实质来源详情，补齐图谱步骤到节点的解释。
5. 修复125%/200%及850px原料链裁切，逐品种核验单位和时间。
6. 将业务产物健康与进程/存储健康分开显示，投影失败进入总览提醒。
7. 保留事件追加页和阅读锚点，跨实际后台更新验收。
8. 修复隐藏标签页焦点，统一JSON文件导出行为。
9. 补真实AI回答、观察级生成、隔离错误恢复、图表hover和全量引用验证；按已明确授权范围执行，不从只读检查擅自推定生产写入。
10. 连续形成七产品真实到期结果并做样本外评测；先有有效样本，再判断模型质量，不把自然日流逝当作验证完成。

## 11. 截图与响应证据索引

以下为本轮新证据；旧测试和旧部署材料不替代本次实测。


- 总览：[01-overview.png](/path/to/project/agent-context/full-acceptance-20260907/01-overview.png)，[01-expanded.png](/path/to/project/agent-context/full-acceptance-20260907/01-expanded.png)

- 行情：[02-market.png](/path/to/project/agent-context/full-acceptance-20260907/02-market.png)，[02-description.png](/path/to/project/agent-context/full-acceptance-20260907/02-description.png)

- 事件：[03-events.png](/path/to/project/agent-context/full-acceptance-20260907/03-events.png)，[03-search-result.png](/path/to/project/agent-context/full-acceptance-20260907/03-search-result.png)，[03-search-forex-settled.png](/path/to/project/agent-context/full-acceptance-20260907/03-search-forex-settled.png)，[03-supply-final.png](/path/to/project/agent-context/full-acceptance-20260907/03-supply-final.png)

- 图谱：[04-evidence.png](/path/to/project/agent-context/full-acceptance-20260907/04-evidence.png)，[04-steps-complete.png](/path/to/project/agent-context/full-acceptance-20260907/04-steps-complete.png)，[04-node-pan.png](/path/to/project/agent-context/full-acceptance-20260907/04-node-pan.png)

- Agent：[05-workflow.png](/path/to/project/agent-context/full-acceptance-20260907/05-workflow.png)，[05-refreshed.txt](/path/to/project/agent-context/full-acceptance-20260907/05-refreshed.txt)

- AI：[06-assistant.png](/path/to/project/agent-context/full-acceptance-20260907/06-assistant.png)，[06-evidence-click.png](/path/to/project/agent-context/full-acceptance-20260907/06-evidence-click.png)

- 报告与账本：[07-reports.png](/path/to/project/agent-context/full-acceptance-20260907/07-reports.png)，[09-ledger-settled.png](/path/to/project/agent-context/full-acceptance-20260907/09-ledger-settled.png)，[09-csv-validation.json](/path/to/project/agent-context/full-acceptance-20260907/09-csv-validation.json)

- 工业情报：[08-summary.png](/path/to/project/agent-context/full-acceptance-20260907/08-summary.png)，[08-run-failures.png](/path/to/project/agent-context/full-acceptance-20260907/08-run-failures.png)，[08-radar.png](/path/to/project/agent-context/full-acceptance-20260907/08-radar.png)，[08-map-network-settled.png](/path/to/project/agent-context/full-acceptance-20260907/08-map-network-settled.png)

- 横向网络：[10-console-record.png](/path/to/project/agent-context/full-acceptance-20260907/10-console-record.png)，[10-zoom125.png](/path/to/project/agent-context/full-acceptance-20260907/10-zoom125.png)，[10-zoom200.png](/path/to/project/agent-context/full-acceptance-20260907/10-zoom200.png)，[10-narrow.png](/path/to/project/agent-context/full-acceptance-20260907/10-narrow.png)，[http-timings.json](/path/to/project/agent-context/full-acceptance-20260907/http-timings.json)，[health-ready.json](/path/to/project/agent-context/full-acceptance-20260907/health-ready.json)，[release.json](/path/to/project/agent-context/full-acceptance-20260907/release.json)

完整记录：[coverage.json](/path/to/project/agent-context/full-acceptance-20260907/coverage.json)；绝对路径/大小/SHA-256清单：[evidence-manifest.json](/path/to/project/agent-context/full-acceptance-20260907/evidence-manifest.json)


## 12. 未验证事项与原因

未执行真实计费问答/快捷问答、观察级材料生成和生产抓取导入设置删除；本轮没有新增业务写入。未用全新浏览器配置完整核验访问控制，未覆盖全部模块的完整HAR。

无当日摘要、雷达事件、地图点位、旧历史记录，无法打开对应引用、抽屉、筛选和复盘分支。未破坏生产注入故障；未重做隔离AI/地图失败场景。曲线首尾hover、每张证据卡/外部原件、全部21预测诊断、全模块全部断点未完成。

这些已在矩阵和分数中体现。**当前生产验收报告已形成，产品仍未通过全量验收。**本轮只优化了检测流程与验收文档，没有实施问题修复，不把问题清单当作修复完成。

### 60项执行记录

证据列文件均位于本轮证据目录。


| ID | 组件 | 判定 | 实际与限制 | 证据 |
|---|---|---|---|---|
| G01 | 入口/启动加载 | 通过 | 首次打开公网及地址栏深链接均进入主体；前后端发布标识相同。 | 01-overview.png, release.json, health-ready.json |
| G02 | 公共导航/标题 | 通过 | 八项导航均进入，标题与内容对应；已回到总览。 | 01-overview.png, 02-market.png, 03-events.png, 04-evidence.png, 05-workflow.png, 06-assistant.png, 07-reports.png, 08-runs-settled.png |
| G03 | 公共刷新/读取时间 | 通过 | 顶部刷新、普通刷新、强制刷新均执行；后两者最终恢复账本。 | 01-global-refresh.txt, 10-refresh-settled.txt, 10-hard-refresh-settled.txt |
| G04 | 深链接/历史 | 通过 | 地址栏直达账本和摘要；后退回事件、前进回摘要，URL与内容一致。 | 10-deeplink-ledger.txt, 10-deep-summary-settled.txt, 10-history-back-settled.txt, 10-history-forward-settled.txt |
| G05 | 快速切换 | 部分通过 | 连续行情→事件→图谱→行情没有白屏；返回行情曾显示7月历史报价，后续数据一致性未完成解释。 | 10-rapid-switch.png, 10-zoom125.png |
| G06 | 登录/会话 | 部分通过 | 当前Chrome新页、深链、刷新无需密码；匿名curl健康及release均200；未在全新浏览器配置独立验证所有业务接口访问控制。 | 01-overview.txt, 10-hard-refresh-settled.txt, http-timings.json |
| O01 | 总览结论/KPI | 部分通过 | 49%低置信和14日观察有依据；运行正常不能概括工业情报失败。 | 01-overview.png, 10-keyboard-focus.png, 08-run-failures.png |
| O02 | 七产品/压力链 | 部分通过 | 七产品链完整，库存采购等未作为预测目标；历史链和当前报价有不同日期，部分信息截断。 | 01-overview.txt, 09-ledger-settled.txt |
| O03 | 趋势/提示 | 未验证 | 当前native接口没有hover方法；未完成图表首尾点悬停，不能凭静态曲线判通过。 | 01-overview.png |
| O04 | 快捷导航 | 通过 | 数据状态、报告、事件与证据快捷入口均实际使用并保存目标页。 | 01-data-shortcut.txt, 07-reports.txt, 01-shortcut-events.txt, 01-shortcut-evidence.txt |
| M01 | 七品种选择 | 通过 | 顶部、链路、侧栏三套七品种选择均操作；DTY/PX顶部补等稳定状态。 | 02-top-DTY-settled.txt, 02-top-PX-settled.txt, 02-chain-DTY.txt, 02-side-原油.txt, 02-products.json |
| M02 | 时间窗口 | 通过 | PX末30日22个交易日、末90日61点、全部390点；窗口边界匹配。 | 02-window-末30日数据.txt, 02-window-末90日数据.txt, 02-window-全部.txt |
| M03 | 价格趋势 | 部分通过 | 曲线可显示且有单位日期描述；首尾点hover未验证，真实无曲线品种未出现。 | 02-market.png, 02-window-全部.txt |
| M04 | 价格/来源详情 | 部分通过 | 阅读七品种历史和最新基准、库存开工加工差及完整说明；未逐一打开全部来源；窄窗口截断。 | 02-description.png, 02-side-DTY.txt, 10-narrow.png |
| E01 | 事件分类 | 部分通过 | 七分类均操作；航运稳定586条、宏观146条，供需出现超时；部分中间态旧列表不能作成功证据。 | 03-filter-shipping-settled.png, 03-macro-final.txt, 03-supply-final.png, 03-stable-category-装置.txt |
| E02 | 搜索/清空 | 部分通过 | FOREX第一次超时，复测23条；聚酯0条、随机词0条，清空恢复全部。筛选刷新保持未验证。 | 03-search-result.png, 03-search-forex-settled.png, 03-search-cn-settled.txt, 03-search-none-settled.png, 03-search-cleared.txt |
| E03 | 事件详情 | 部分通过 | 首条、第二条、已加载末条及相似入口实际操作；中部Rosneft在快照更新后被移出，未完成该条核对。 | 03-events.txt, 03-second-detail.txt, 03-last-detail.txt, 03-current-list.png |
| E04 | 核验展开/链接 | 部分通过 | 核验展开/收起有效；外链转至匹配的Bloomberg页面，条款弹窗未接受，媒体不能播放，未继续全文核验。 | 03-verification-open.txt, 03-external-source.png |
| E05 | 分页/相似事件 | 部分通过 | 加载更多30→60；快照更新又回30；全部与相似查看全部按钮已用。未遍历3261条全库及全量去重。 | 03-list-middle.png, 03-current-list.png, 03-similar-all.txt, 03-all-events.txt |
| V01 | 证据路径 | 部分通过 | 8个步骤逐项选择，不再因缺失节点整片空白；部分完成步骤没有对应可聚焦节点。 | 04-step-1.txt, 04-step-8.txt, 04-steps-complete.png |
| V02 | 关系筛选 | 通过 | 全部/支撑/反证/冲突/引用五过滤均切换并保存状态。 | 04-filter-只看支撑.txt, 04-filter-只看反证.txt, 04-filter-只看冲突.txt, 04-filter-只看引用.txt, 04-filter-全部.txt |
| V03 | 图谱画布 | 部分通过 | 节点详情、平移、放大缩小、适配、重置已用；边选择与每张卡片未逐项验证。 | 04-node-detail.txt, 04-node-pan.png, 04-evidence.png |
| V04 | 证据详情/刷新 | 部分通过 | 读取采用3排除4及历史日期；未逐条打开7张材料原件，刷新后全部引用稳定性未完成。 | 04-evidence.txt, 04-steps-complete.txt |
| W01 | Agent系统状态 | 失败 | 实际执行0/12、固化0/12、模型调用0；页面诚实显示任务未执行，主链路产出验收失败。 | 05-workflow.png, 05-refreshed.txt |
| W02 | Agent交接/回合 | 部分通过 | 12节点逐一点击；没有实际回合输入输出，实质交接分支未验证。 | 05-node-1.txt, 05-node-12.txt |
| W03 | 状态恢复 | 部分通过 | 刷新状态可用；未触发抓取，刷新不能恢复实际执行结果。 | 05-refreshed.txt |
| A01 | 输入组件 | 部分通过 | 空输入与空格发送禁用，正常文字发送可用，清空恢复禁用；换行规则未专测。 | 06-whitespace.txt, 06-input.txt, 06-controls.txt |
| A02 | 快捷问题/发送【写入/计费】 | 未验证 | 本轮未点击快捷问题发送或提交真实计费问题；不得把默认提示当作生成回答。 | 06-assistant.txt |
| A03 | 回答/证据 | 部分通过 | 核验信号展开可用；证据卡只弹“问答参考材料：原油”，无实质来源详情。 | 06-controls.txt, 06-evidence-click.png |
| A04 | 失败与重试【隔离】 | 未验证 | 未注入故障、未在隔离环境重演AI超时/重试；公网不人为破坏服务。 | 06-assistant.txt |
| R01 | 报告类型/列表 | 部分通过 | 日报周报复盘专题四标签均切换；正式报告均无文件，列表无法覆盖真实多报告首中末。 | 07-type-日报.txt, 07-type-周报.txt, 07-type-复盘报告.txt, 07-type-专题报告.txt |
| R02 | 预览/导出/复制 | 部分通过 | 预览/文件/复制禁用且有原因；查看证据跳转已用。真实文件内容、剪贴板未验证。 | 07-reports.png, 07-evidence-route.txt |
| R03 | 二级导航 | 通过 | 报告与账本切换可用；深链刷新返回账本，浏览器历史在事件与情报二级页验证。 | 09-ledger-settled.txt, 10-hard-refresh-settled.txt |
| L01 | 七产品预测 | 部分通过 | 21组合可读，0正式/21参考；展开原油1日依据，未逐格展开21组诊断及点击刷新预测。 | 09-ledger-settled.png, 09-first-basis.png, 09-export-json.txt |
| L02 | 历史账本筛选 | 未验证 | 历史兼容账本0条，未出现状态/周期筛选和可选历史记录。 | 09-export-json.txt |
| L03 | 到期复盘 | 部分通过 | 七产品不可变账本有MEG1日6486.368→6400、误差86.368，界面已结算未命中；其他组合等待到期。旧账本0条。 | 09-export-json.txt, 09-csv-validation.json |
| L04 | 生成观察级材料【写入】 | 未验证 | 生成观察级材料可见，本轮未触发生产写入。 | 09-export-json.txt |
| I01 | 每日摘要 | 失败 | 计划09:30之后仍无当日摘要；刷新仍无，查看运行与来源可导航。 | 08-summary.png, 08-summary-refreshed.txt, 08-summary-runs-route.txt |
| I02 | 摘要事件/引用 | 未验证 | 没有已形成摘要，摘要事件/引用/聚合表格不可进入。 | 08-summary.png |
| I03 | 全球雷达筛选 | 部分通过 | 初入503；品种8项、类别9项均实际选择，后来恢复诚实空列表，未有真实数据验证筛选准确性。 | 08-radar.png, 08-radar-product-全部品种.txt, 08-radar-category-全部类别.txt, 08-radar-recovered-empty.txt |
| I04 | 雷达详情面板 | 未验证 | 雷达无事件，无详情抽屉可打开；Esc/焦点恢复无前置数据。 | 08-radar-recovered-empty.txt |
| I05 | 运行与来源 | 部分通过 | 来源7页、运行3页均翻页，技术详情及刷新已用；投影重复时区错误。未逐行展开全部来源技术标识。 | 08-source-page-7.txt, 08-audit-page-3.txt, 08-source-detail.txt, 08-run-failures.png |
| I06 | 地图惰性加载 | 部分通过 | 先查看摘要/雷达/运行再首次进地图；观察到地图专属资源随后加载并200。未保存打开地图前完整资源清单，不能证明所有预加载均不存在。 | 08-map-loading.png, 08-map-network-settled.txt |
| I07 | 地图交互 | 部分通过 | 地图平移缩放成功、同源底图200；无可信坐标事件，点位与详情分支未验证。 | 08-map-network-settled.png, 08-map-panzoom.png |
| I08 | 地图失败【隔离】 | 未验证 | 未在隔离环境注入地图资源失败，重试路径未实测。 | 08-map-network-settled.txt |
| O05 | 模型/风险展开及事件概述一致性 | 部分通过 | 模型、风险展开及键盘Enter已用；最新事件中文概述可见，未对全事件进行100%中文事实质量核验。 | 01-expanded.png, 01-model-keyboard.txt, 03-events.txt |
| L05 | CSV/JSON实际下载与内容 | 部分通过 | CSV实际下载且21个唯一组合可解析；JSON打开原始响应而非下载文件，未另存全量JSON核对。 | 09-csv-inspection.json, 09-csv-validation.json, 09-json-response.txt |
| C01 | 按钮/图标按钮/链接 | 部分通过 | 核心导航/快捷/刷新/筛选实际使用；证据卡仅标题提示，未测试全部实例。 | 06-evidence-click.png, 01-shortcut-events.txt |
| C02 | 输入/搜索/选择/单选标签 | 部分通过 | 英文中文空值搜索及原生筛选已用；事件筛选超时，换行/Escape并未全组件测试。 | 03-search-none-settled.png, 03-supply-final.png, 08-radar-product-DTY.txt |
| C03 | 标签页/二级导航 | 部分通过 | 鼠标标签和二级深链验证；全套标签键盘方向键切换未验证。 | 10-history-forward-settled.txt |
| C04 | 卡片/指标/Tag/Tooltip | 部分通过 | 未见NaN/Infinity；125%/200%时间和品种存在遮挡，hover提示未验证。 | 10-zoom125.png, 10-zoom200.png |
| C05 | 表格/列表/分页 | 部分通过 | 两张来源运行表全部分页、事件追加已用；全库去重未验证，追加遇快照更新丢失。 | 08-source-page-7.txt, 08-audit-page-3.txt, 03-current-list.png |
| C06 | 图表/画布/地图 | 部分通过 | 图谱和地图平移缩放可用；曲线hover/图例、图谱边、真实地图点位未完成。 | 04-node-pan.png, 08-map-panzoom.png |
| C07 | Drawer/Modal/Collapse/详情 | 部分通过 | 模型风险品种和事件核验展开已用；无数据抽屉/Modal的关闭/Esc/焦点分支未验证。 | 01-expanded.png, 02-description.png, 03-verification-open.txt |
| C08 | Alert/Spin/Empty/错误提示 | 部分通过 | 见加载、成功、空、503/超时及重试；雷达后续恢复空；未覆盖全部故障注入。 | 03-search-result.png, 03-search-forex-settled.png, 08-radar.png, 08-radar-recovered-empty.txt |
| C09 | 复制/下载 | 部分通过 | 真实CSV通过21组合校验；JSON直开响应；正式报告复制/下载因无文件未验证。 | 09-csv-validation.json, 09-json-response.txt, 07-reports.txt |
| C10 | 键盘可访问性 | 部分通过 | Tab遍历7后续导航、刷新、数据状态、模型展开，焦点可见；Enter有效。控制台发现隐藏标签页保留焦点警告。 | 10-keyboard-trail.json, 10-keyboard-focus.png, 10-console-record.png |
| C11 | 窄窗口/缩放 | 失败 | 1302px与850px、125%及200%均检查；行情链截断、右端溢出，横向滚动尝试未恢复。窗口比例已还原。 | 10-narrow.png, 10-narrow-chain-scroll.png, 10-zoom125.png, 10-zoom200.png, 10-window-restored.png |
| C12 | 公网网络/资源 | 部分通过 | live/ready200且版本相同；真实Console503与aria警告、Network同源地图和事件请求200。未导出完整HAR，不能保证所有模块都无网络错误/错误域名。 | http-timings.json, release.json, 10-console-record.png, 08-map-network-settled.txt, 03-search-forex-settled.txt |
| C13 | 写入组件【隔离或授权】 | 未验证 | 只记录可见发送、快捷问答、观察级生成；不新增生产写入。抓取、导入、删除、设置保存未在本轮界面出现可执行入口。 | 06-assistant.txt, 09-export-json.txt |

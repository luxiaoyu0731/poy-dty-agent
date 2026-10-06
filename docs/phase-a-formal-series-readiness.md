# Phase A 正式序列来源就绪清单

更新日期：2026-08-27（Asia/Shanghai）
适用契约：`phase-a.v7`、`formal-series-eligibility.v1`

## 目的和边界

本表把当前 19 条正式价格/换算证据序列与已知候选来源逐条对齐。它是
来源验收的工作清单，不是审批记录、受信 manifest 或生产开关。

- 当前 `APPROVED_MANIFEST_DIGESTS` 为空；所有序列仍是 `blocked`，总计
  `eligible=0/19`。
- 相近品种、不同市场、不同报价类型或不同时间含义的数据不能替代正式
  序列。候选数据只能进入内部观察链。
- 每条序列仍须获得来源权限、规格、单位/币种、新鲜度、可见性和交易
  日历六项独立证据，并由审批后的 digest 进入受信 manifest。
- 本文件不授予抓取、登录、导出、写库、发布或来源升格权限。

## 序列对照

| # | 正式序列 ID | 当前可定位候选 | 当前结论 |
| --- | --- | --- | --- |
| 1 | `crude.brent.ice.front_month.settlement.usd_bbl` | EIA Europe Brent 现货日度 | 口径不同：EIA 现货不是 ICE 近月结算；无正式候选。 |
| 2 | `crude.wti.cme.front_month.settlement.usd_bbl` | EIA Cushing WTI 现货日度 | 口径不同：EIA 现货不是 CME 近月结算；无正式候选。 |
| 3 | `coal.benchmark.unresolved.assessment.cny_mt` | CCTD 环渤海 5500K 日评 | 规格/日期/发布时间与六项证据待逐条验收。 |
| 4 | `naphtha.ccf.domestic.daily_assessment.cny_mt` | CCF 日本 CFR 石脑油日评 | 市场和币种不一一对应；不能替代国内 CNY/mt 正式序列。 |
| 5 | `mx.domestic.spot_assessment.cny_mt` | SunSirs 华东混二甲苯区间评估 | 非成交价；须冻结区间中点、市场、可见性和日历证据。 |
| 6 | `px.czce.main.settlement.cny_mt` | 无已验收候选 | 需要郑商所主力连续/换月与结算口径证据。 |
| 7 | `px.ccf.domestic.daily_assessment.cny_mt` | CCF CFR 中国 PX 日评 | CFR 中国与国内 CNY/mt 不一一对应；不可自动换算或替代。 |
| 8 | `ethylene.domestic.spot_assessment.cny_mt` | 无已验收候选 | 缺正式来源、规格和日历。 |
| 9 | `eo.domestic.spot_assessment.cny_mt` | 无已验收候选 | 缺正式来源、规格和日历。 |
| 10 | `methanol.czce.main.settlement.cny_mt` | 无已验收候选 | 需要郑商所主力连续/换月与结算口径证据。 |
| 11 | `pta.czce.main.settlement.cny_mt` | 无已验收候选 | 需要郑商所主力连续/换月与结算口径证据。 |
| 12 | `pta.ccf.domestic.daily_assessment.cny_mt` | CCF 内盘 PTA 日评 | 已有内部观察候选；六项证据和 manifest 未完成。 |
| 13 | `meg.dce.main.settlement.cny_mt` | 无已验收候选 | 需要大商所主力连续/换月与结算口径证据。 |
| 14 | `meg.ccf.domestic.daily_assessment.cny_mt` | CCF 内盘 MEG 日评 | 已有内部观察候选；六项证据和 manifest 未完成。 |
| 15 | `polyester_melt.domestic.assessment.cny_mt` | 无已验收候选 | 缺正式来源、规格和日历。 |
| 16 | `polyester_chip.domestic.assessment.cny_mt` | 无已验收候选 | 缺正式来源、规格和日历。 |
| 17 | `poy.upstream_cost_pressure.index` | 派生目标 | 不是外部价格；先要求上游正式证据和完整派生 proof。 |
| 18 | `dty.upstream_cost_pressure.index` | 派生目标 | 不是外部价格；先要求上游正式证据和完整派生 proof。 |
| 19 | `fx.usd_cny.cfets.central_parity.cny_per_usd` | CFETS USD/CNY 官方 JSON | 语义候选已验证；六项证据和 manifest 未完成。 |

## 推荐的验收顺序

1. 先完成一一对应的 PTA、MEG、CFETS 候选事实审计；它们不能因存在内部
   捕获记录而自动通过。
2. 对交易所序列先冻结主力连续、换月、结算、交易日归属和发布时间，再采集。
3. 对 CCF 境外/人民币口径不一致的序列，先作业务合同决策：新建匹配序列，
   还是取得真正匹配的国内人民币报价；不得把换算假设静默写入数据。
4. 对仍无来源的工业品和派生目标，先定义权威来源/公式与责任人，再申请
   六项证据复核。
5. 只有全部相关证据、有效期与审批记录齐备后，才可生成 manifest、审阅其
   digest，并通过单独发布流程将其加入信任根。

## 首批候选 dossier 工具（已删除，2026-08-28）

个人工作台去范围化后，来源授权仪式整体退役；本节所述生成器
`server/app/formal_source_evidence_dossiers.py` 已删除（git 历史可查）。
下列描述仅作历史记录。

原始描述：只为 PTA、MEG、CFETS 三条
最接近闭合的序列生成候选复核材料。调用方必须显式提供 capture revision
及预期 Hash、内容寻址授权材料和日历材料；构建器在单一只读事务快照内核对
完整 projection，并输出六门的 `passed`、`failed`、`conflict` 或 `unknown`。

它不会写库、生成 approval 或修改 `APPROVED_MANIFEST_DIGESTS`。即使六门
候选材料全部为 `passed`，在独立审批和受信 manifest 发布前，正式资格仍为
`blocked`。

## 当前阻断

本项目当前最重要的生产阻断不是预测算法，而是正式来源契约尚未得到逐条
验证。任何未通过的序列必须保持 `blocked`，预测写入路径继续以零写失败。

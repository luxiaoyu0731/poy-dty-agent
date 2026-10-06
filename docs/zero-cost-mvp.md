# 0 成本真实数据 MVP 方案

本方案的边界是：除 AI token 外，不采购行情、新闻、化纤/化工商业数据、航运油轮数据或终端授权。系统只使用免费公开源、免费 API key、本地 CSV/OCR/人工笔记。

## Product Boundary

可以做到：

- 原油、库存、供需、宏观、汇率、政策、制裁、交易所日度、贸易流、企业公告的真实数据闭环。
- 基于公开源和内部手工输入生成晨报、事件解释、成本压力方向和预测账本。
- 每次预测绑定数据快照，并在到期后复盘。

不能承诺：

- 合法稳定的 Brent/WTI/国内期货分钟级自动行情。
- CCF、隆众、卓创、Reuters、Bloomberg、Kpler、Vortexa 等商业源实时自动接入。
- PX/PTA/MEG 现货、库存、开工率的商业级实时覆盖。

## Source Matrix

| Priority | Source | Data | Frequency | Cost | Ingestion |
| --- | --- | --- | --- | --- | --- |
| P0 | EIA Open Data | 原油价格、库存、产量、炼厂开工 | 日/周 | 0 | 免费 API key |
| P0 | EIA WPSR | 原油/成品油库存、炼厂开工 | 周 | 0 | HTML/PDF/API |
| P0 | OPEC Press | OPEC/OPEC+ 声明 | 事件 | 0 | HTML/RSS |
| P0 | OPEC MOMR | 油市供需、产量、需求 | 月 | 0 | PDF/HTML |
| P0 | INE/ZCE/DCE | SC、PTA/PX、EG 日度行情/仓单 | 日 | 0 | 公开页面/下载 |
| P0 | CFETS/SAFE/PBOC | 人民币、汇率政策、资金背景 | 工作日/月 | 0 | HTML |
| P0 | FRED | 美元、利率、宏观序列 | 日/周/月 | 0 | 免费 API key |
| P0 | CFTC COT | 原油期货持仓 | 周 | 0 | CSV/HTML |
| P0 | OFAC/White House/State/EU/UN | 制裁、政策、地缘事件 | 事件 | 0 | XML/CSV/RSS/HTML |
| P0 | 内部 CSV/OCR/笔记 | 现货、库存、开工、报价线索、复盘标签 | 手工 | 0 | CSV/OCR/Form |
| P1 | GACC/UN Comtrade | 原油、PX、MEG、煤炭贸易流 | 月 | 0/free tier | HTML/API |
| P1 | 巨潮/SSE/SZSE/HKEX | 化工企业公告、装置、产能 | 事件 | 0 | HTML/PDF |
| P1 | Gov.cn/NDRC/NEA/MEE/MIIT | 国内产业、能源、环保政策 | 事件/月 | 0 | HTML/RSS |
| P2 | 上海航运交易所/IMO/MARAD/UKMTO | 航运指数、海事安全事件 | 周/事件 | 0 | HTML/RSS |

## Data Flow

```text
Source Registry
-> Fetch/API/Download/OCR/CSV
-> Raw payload + structured observations
-> Quality checks
-> Data snapshot
-> Cost pressure reasoning
-> Local RAG evidence retrieval
-> AI explanation
-> Prediction ledger
-> Review labels
-> Weight adjustment notes
```

## Required Tables

- `market_observations`: EIA、交易所日度、汇率、宏观、CFTC 等结构化时序。已实现。
- `industry_observations`: 手工录入的 PX/PTA/MEG/POY/DTY 现货、库存、开工率、加工费、报价线索。已实现。
- `event_observations`: OPEC、OFAC、政策、企业公告、航运事件。已实现。
- `data_snapshots`: 每次预测绑定的证据快照。已实现。
- `prediction_ledger`: 预测记录。
- `prediction_reviews`: 到期复盘。
- `RAG evidence documents`: 由上述表、新闻表、知识图谱和 Source Registry 临时构建；不需要外部向量数据库或付费 embedding。

## Import Templates

Use `docs/data-templates/`:

- `public_observations.csv`: 免费公开时序和交易所日度数据。
- `industry_chain_observations.csv`: 内部手工化纤链输入。
- `events_and_alerts.csv`: 公开事件和人工事件。
- `prediction_review_labels.csv`: 复盘标签。
- `hs_code_mapping.yaml`: 贸易流 HS 编码候选。

## Implementation Order

1. Done: create `market_observations`, `industry_observations`, `event_observations`, `data_snapshots`.
2. Done: add CSV import endpoints for public observations, industry observations, and events.
3. Done: bind `data_snapshot_id` to prediction creation; missing values create an automatic snapshot.
4. Done: replace fixture-driven `/overview`, `/factors`, `/morning-brief`, `/events`, and review summaries with observation-backed calculations.
5. Done: add frontend one-click EIA/FRED fetch and CSV paste import for public, industry, and event data.
6. Implemented: AIHOT-style POY/DTY news radar can ingest public official pages or manual/Computer Use news CSV into `news_articles`, `news_event_clusters`, and high-scoring `event_observations`.
7. Implemented: Brent/WTI price comparison and forced/due prediction review can compare the prediction ledger with stored EIA/FRED price observations.
8. Optional future enhancement: add parsers for exchange daily downloads, GACC manual downloads, and optional UN Comtrade monthly API.

## Operating Rules

- No paid vendor source is part of the current MVP.
- No scraping around login, paywalls, CAPTCHA, hidden APIs, or commercial terminal restrictions.
- C/D-level data cannot create high-confidence conclusions without A-level confirmation.
- Missing commercial data must lower confidence instead of being filled with fake numbers.
- RAG answers must cite local `doc_id` evidence and disclose low-evidence, stale, or prompt-injection-risk material.

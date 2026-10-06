# POY/DTY HOT 新闻抓取系统设计

本设计参考 AIHOT/数字生命卡兹克的核心产品方法：多信源扫描后先筛选、去重、打分、分类，再用干净时间线和日报呈现。迁移到本项目时，目标不是做通用新闻站，而是做一个服务 POY/DTY 上游成本判断的事件雷达。

## Design Principles

1. 默认展示精选，不默认展示全量。
   - 用户打开系统时看到的是高价值事件流，不是原始抓取日志。
   - 只有用户明确选择“全部”时才展示低分候选。

2. 新闻必须变成可复盘的结构化事件。
   - 每条事件保留原文 URL、来源、发布时间、抓取时间、摘要、影响链、反证和证据等级。
   - AI 摘要不能替代原文引用。

3. 信息源分层决定默认可信度。
   - A/B 级官方源可以作为事实锚点。
   - C 级公开媒体只做发现和交叉验证。
   - D 级人工/Computer Use 观察只做弱信号。

4. 打分必须解释给人看。
   - 每条精选必须给出推荐理由，例如“OFAC 官方源 + 涉及能源运输 + 影响霍尔木兹航线”。
   - 分数不是黑箱排序，而是帮助个人研究者快速判断优先级。

5. 输出隐藏基础设施细节。
   - 前端展示“最近 24 小时精选”“OPEC/制裁/航运”等人话分类。
   - 不向普通页面暴露 cursor、抓取参数、缓存 TTL、HTTP 状态码。

## Product Surface

系统提供三层阅读入口：

| 入口 | 默认范围 | 适用问题 |
| --- | --- | --- |
| 即时精选 | 最近 24 小时、精选事件 | 今天有哪些会影响成本压力的事 |
| 事件日报 | 北京时间每天固定生成 | 今天原油、政策、航运、化工企业公告怎么总结 |
| 全量候选 | 最近 7 天、可筛选 | 我要看所有抓到的事件和误报 |

用户问“今天有什么新东西”时默认返回即时精选；用户明确问“日报”才返回日报；用户明确问“全部/完整/所有”才返回全量候选。

## Source Tiers

| Tier | 来源 | 例子 | 默认用途 |
| --- | --- | --- | --- |
| A | 官方/交易所/政府 | OPEC、EIA、OFAC、Treasury、State Department、White House、UN、EU Council、IEA、NDRC、NEA、Fed、CFTC、CENTCOM、DoD、NATO、European Commission、UK Gov/FCDO | 高置信事实锚点 |
| B | 准官方/行业组织/企业公告 | IMO、MARAD、UKMTO、MPA、U.S. Coast Guard、SSE/SZSE/CNINFO/HKEX、Aramco、ADNOC、QatarEnergy、Sinopec、CNPC、行业协会、港口/海事公告 | 航运、供应链和行业扰动 |
| C | 公开媒体/RSS | GDELT、Google News RSS、RSSHub 路由、公开媒体 RSS | 事件发现和交叉验证，不单独抬高置信度 |
| D | 人工/Computer Use | 券商终端手工笔记、网页行情观察、微信群整理、内部笔记 | 弱信号和补充线索 |

源是否真正可无人值守抓取，不能只看 tier。当前统一用 `configured/fetchable/parsed_to_observation/licensed/manual_only` 描述 readiness，canonical 矩阵见 `docs/source-readiness.md`。

## Pipeline

```text
Source Registry
-> Fetch Run
-> Raw Article Store
-> Text Extraction
-> Candidate Event Extraction
-> Deduplication and Clustering
-> Scoring
-> Classification
-> AI Impact Reasoning
-> Human Review Queue
-> Featured Timeline / Daily Brief
-> Data Snapshot / Prediction Ledger
```

### 1. Fetch Run

每次抓取都形成一个 `fetch_run`，包含来源、开始/结束时间、状态、抓取数量、失败原因和延迟。

Fetcher 类型：

| Fetcher | 技术 | 适用源 |
| --- | --- | --- |
| RSS Fetcher | `feedparser` | 官方 RSS、Google News RSS、RSSHub |
| Static HTML Fetcher | `httpx` + `trafilatura` | OPEC、EIA、OFAC、政府公告 |
| PDF Fetcher | `httpx` + hash + PDF parser | OPEC MOMR、EIA/IEA 报告 |
| Browser Fetcher | Playwright，只读公开页 | JS 渲染表格和公告页 |
| Manual Observation | CSV/Form/Computer Use 辅助 | D 级人工观察 |

### 2. Raw Article Store

已实现表：`news_articles`。

核心字段：

```json
{
  "article_id": "art_...",
  "source_id": "opec_press",
  "tier": "A",
  "url": "https://...",
  "canonical_url": "https://...",
  "title": "OPEC+ ...",
  "published_at": "2026-06-12T08:30:00+08:00",
  "first_seen_at": "2026-06-12T08:33:00+08:00",
  "content_hash": "sha256...",
  "language": "en",
  "raw_text": "...",
  "summary": "...",
  "raw_payload_path": "data/raw/news/..."
}
```

### 2.1 News Detail Evidence Audit

当前仓库没有独立的 `news_detail_extract.py` 入口。正文补强应走现有新闻抓取 API 或现存年度补量脚本：

- `POST /api/v1/news/fetch-runs?...&include_details=true`：对 A/B 源候选文章抓取详情页；C-tier discovery 不追第三方正文。
- `server/scripts/public_news_backfill_v2.py`：年度 V2 public news 补量入口，只使用公开 RSS/发现源，不抓第三方正文全文。
- `POST /api/v1/imports/news-observations`：人工或 Computer Use 辅助整理后的公开证据导入。

安全边界：

- 只访问公开 `http/https` 页面。
- 不绕过登录、验证码、付费墙、浏览器校验或授权限制。
- 保留 `url/canonical_url`，不把抓取跳转结果覆盖为新的来源 URL。
- 正文不足、只能看到摘要、或来源需要浏览器/人工判断时，标记为 summary-only/manual-review，不把它当作 A/B 级完整正文证据。

### 3. Candidate Extraction

先用规则和关键词做低成本筛选，再把高价值候选交给 LLM。

关键词族：

| 事件族 | 关键词 |
| --- | --- |
| OPEC 政策 | OPEC, OPEC+, production cut, output quota, voluntary cut |
| 制裁 | sanction, OFAC, SDN, vessel, tanker, insurance, Russia, Iran |
| 航运 | Red Sea, Hormuz, Gulf of Oman, attack, piracy, advisory, transit |
| 原油供需 | inventory, refinery run, crude export, production, WPSR |
| 化工装置 | PX, PTA, MEG, polyester, outage, maintenance, restart, capacity |
| 国内政策 | 成品油调价、能耗、环保、出口、反倾销、关税、油气管网 |

### 4. Deduplication and Clustering

同一事件可能由多个来源重复报道。系统先保留所有原文，再聚合成一个 `event_cluster`。

合并依据：

- canonical URL hash
- title similarity
- published_at 时间窗口
- entity overlap
- event_type
- affected_products

聚合后结构：

```json
{
  "cluster_id": "evt_20260612_ofac_energy_shipping",
  "title": "OFAC 新增能源运输相关制裁对象",
  "first_seen_at": "2026-06-12T09:10:00+08:00",
  "last_seen_at": "2026-06-12T09:28:00+08:00",
  "source_ids": ["ofac_recent_actions", "google_news_oil_rss"],
  "article_ids": ["art_1", "art_2"],
  "status": "official_confirmed"
}
```

## Scoring Model

每条候选事件计算 `heat_score`，范围 0-100。

```text
heat_score =
  source_score * 0.25
+ relevance_score * 0.25
+ impact_score * 0.20
+ novelty_score * 0.10
+ cross_source_score * 0.10
+ freshness_score * 0.10
```

### Score Dimensions

| 维度 | 说明 |
| --- | --- |
| `source_score` | 来源等级和可靠度，A 级官方最高 |
| `relevance_score` | 是否直接影响 crude_oil、naphtha、PX、PTA、MEG、POY/DTY |
| `impact_score` | 对成本压力的传导强度 |
| `novelty_score` | 是否是新事件，还是旧闻重复 |
| `cross_source_score` | 是否被多个独立来源确认 |
| `freshness_score` | 发布时间和首次抓取时间 |

精选阈值建议：

| 分数 | 动作 |
| --- | --- |
| 80-100 | 进入首页精选和预警 |
| 60-79 | 进入精选候选，等待交叉验证 |
| 40-59 | 只进入全量候选 |
| < 40 | 入库但默认隐藏 |

## Categories

前端和日报统一使用以下分类：

| category | 中文名 | 说明 |
| --- | --- | --- |
| `oil_policy` | 原油/OPEC 政策 | 产量、配额、减产、油价政策 |
| `sanctions_geopolitics` | 制裁与地缘 | OFAC、EU、UN、战争、外交冲突 |
| `shipping_security` | 航运与海事安全 | 红海、霍尔木兹、港口、海事警告 |
| `china_policy` | 国内政策 | 发改委、能源局、工信部、环保、关税 |
| `company_capacity` | 企业公告与装置 | 炼化、PX/PTA/MEG、聚酯项目和事故 |
| `macro_finance` | 宏观与汇率 | 美元、利率、金融条件 |
| `market_signal` | 资金和市场弱信号 | 石油股、人工观察、公开网页行情 |

## AI Reasoning Contract

LLM 只处理已经筛出的候选事件。输出必须是固定 JSON，便于测试和复盘。

```json
{
  "event_type": "sanctions_geopolitics",
  "entities": ["OFAC", "shipping", "crude_oil", "PX"],
  "affected_products": ["crude_oil", "naphtha", "px", "pta"],
  "direction": "利多",
  "impact_strength": 0.72,
  "time_horizon": "1-7d",
  "facts": [
    "OFAC 发布制裁更新",
    "事件涉及能源运输或相关实体"
  ],
  "inference": [
    "若影响油轮保险或运输，可能抬升原油风险溢价",
    "原油风险溢价可能沿石脑油、PX、PTA 传导"
  ],
  "counter_evidence": [
    "若实体与主流能源运输无关，影响可能有限",
    "若库存或需求数据走弱，风险溢价可能被抵消"
  ],
  "evidence_level": "A",
  "requires_human_review": true,
  "recommendation_reason": "A 级官方制裁源，且命中能源运输和原油传导链。"
}
```

约束：

- 必须区分 `facts` 和 `inference`。
- C/D 级来源默认 `requires_human_review=true`。
- 不允许输出“幕后势力操纵”式结论；只能写利益相关方、动机路径和可验证证据。
- 没有 A/B 级确认时，不得把影响方向标成高置信。

### LLM-Only Direction Gate

事件方向判断正在从历史关键词/规则版切换为 DeepSeek + RAG 版。规则层仍可用于低成本候选筛选、来源分层、去重和审计，但不得再作为最终预测方向、权重回测或命中率统计依据。

新口径：

- `rule_direction` 可保留为历史审计字段。
- `llm_direction` 是事件预测方向的唯一有效字段。
- DeepSeek 不可用、输出无效或证据不足时，记录 `fallback=true`，该事件不进入 LLM-only 回测。
- 每条非 fallback 判断必须绑定 `cited_doc_ids`，并且每个证据文档时间必须满足 `published_at <= as_of_time`。
- RAG 上下文可以包含当时之前的新闻、事件、市场观测和行业观测；不能包含未来价格、未来新闻或回测命中结果。

LLM-only 输出最小字段：

```json
{
  "event_id": "evt_...",
  "as_of_time": "2026-06-12T09:10:00+08:00",
  "llm_direction": "利多",
  "confidence": 0.72,
  "evidence_level": "A",
  "reasoning": "事实与推断分开说明。",
  "counter_evidence": "需求走弱、库存增加或风险溢价消化可能削弱影响。",
  "cited_doc_ids": ["news:art_...", "event:evt_..."],
  "risk_premium_decay": true,
  "demand_weakness_offset": false,
  "supply_recovery_offset": false,
  "should_enter_backtest": true,
  "provider": "deepseek",
  "model": "deepseek-...",
  "fallback": false
}
```

LLM 判断与回测验收细则见 `docs/rag-event-direction.md`。

2026-06-16 本地完成结果：

- DeepSeek 实际调用成功：`provider=deepseek`，`model=deepseek-v4-pro`。
- 事件方向判断覆盖 `410` 条新闻事件，`fallback=0`。
- `94` 条非中性/证据足够事件进入 LLM-only 回测。
- 真实价格曲线可评分事件 `45` 条，命中 `23`，错判 `22`，命中率 `0.5111`。
- RAG as-of audit：`future_leak_count=0`。
- 正式报告：`server/data/backfill_reports/llm-backtest-2025-06-16-to-2026-06-15.json`。

## API Surface

已实现新闻域接口，全部挂在 `/api/v1`。

| Method | Path | Meaning |
| --- | --- | --- |
| `GET` | `/news/sources` | 新闻源列表、状态、抓取频率 |
| `POST` | `/news/fetch-runs` | 手动触发一轮新闻抓取，支持 `mode=live|archive`、`cursor_pages`、`include_details` |
| `GET` | `/news/fetch-runs` | 抓取任务历史 |
| `GET` | `/news/articles` | 原始文章列表，支持 source/category/tier/search/published 时间窗 |
| `GET` | `/news/events` | 结构化事件流，支持 source/category/tier/status/search |
| `GET` | `/events/{event_id}/reasoning` | 事件详情、来源、推理链、反证 |
| `POST` | `/imports/news-observations` | 手工/Computer Use 新闻观察导入 |

查询规则：

- `POST /news/fetch-runs?mode=live` 是增量抓取入口，只抓当前公开页面/RSS 可见项。
- `POST /news/fetch-runs?mode=archive&start_date=2026-01-01&end_date=2026-12-31&cursor_pages=3` 会启用已实现的 source-specific archive URL 适配器并限制翻页深度。
- `include_details=true` 会对 A/B 源候选文章抓取详情页正文；C 级 RSS/发现源不追第三方正文。
- 当前 archive 适配器覆盖 OFAC、Treasury、EU Council 查询 URL、Federal Reserve 年度页、IEA 公共分页、Google News 日期围栏 RSS、GDELT 分日 `startdatetime/enddatetime` 慢队列、White House 分页、UN 年度过滤、UK Gov/FCDO 搜索页；其他源会回退到当前页。
- 自动升为正式事件要求：A/B 级来源、分数达标、且存在可靠发布时间。缺失发布时间的高分官方条目只进候选，避免“当前页入口”污染预测链路。
- `GET /news/articles` 可用 `published_after=2026-01-01&published_before=2026-12-31` 核验回填结果。
- `GET /news/events` 可用 `status=featured|candidate` 区分已提升事件和候选事件。
- 2026 全年回填应优先使用 archive adapter；对页面结构复杂、JS 渲染或阻断的源，使用 `POST /imports/news-observations` 导入已整理语料。

### Adapter Status

完整 readiness 口径见 `docs/source-readiness.md`。本节只保留新闻雷达的操作性摘要。

最近一次小批量验证后的源级状态：

| 状态 | 来源 |
| --- | --- |
| 可自动抓取并入库 | OFAC、Treasury、NEA、IEA News、Federal Reserve、MPA、CENTCOM/DVIDS、UK Gov/FCDO、OPEC 首页 fallback、Google News RSS |
| 可访问但本轮无相关条目 | State Department、UKMTO、White House、EU Council、UN Press、EIA Press、EIA WPSR、EIA Today in Energy、European Commission、HKEX、CNINFO、NATO、IMO、ADNOC、QatarEnergy |
| 慢队列/需强退避 | GDELT（archive 已按日切分，但必须低频运行） |
| 站点保护/需手工或 alternate feed | MARAD、U.S. Coast Guard、Saudi Aramco、部分 OPEC detail 页 |
| 网络/协议或 endpoint 待适配 | UN Security Council、SSE、SZSE、Sinopec、CNPC |

GDELT 当前仅作为 C 级发现源；它有严格请求频率/查询限制，archive 只能分日、低并发、可恢复运行，失败时不影响 A/B 官方事实锚点。

### 2026 Backfill Rehearsal

当前仓库的年度新闻补量入口是 `server/scripts/public_news_backfill_v2.py`。它面向 V2 评估的 raw news 覆盖补齐，不是生产新闻爬虫；默认应先 dry-run 或小窗口运行，并保留报告。

推荐执行顺序：

```bash
cd /path/to/project/server
uv run python scripts/public_news_backfill_v2.py \
  --db data/agent.db \
  --providers google \
  --start 2025-06-16 \
  --end 2026-06-15 \
  --target-raw-news 2000 \
  --window-days 30 \
  --sleep-seconds 0.2 \
  --timeout-seconds 10 \
  --attempts 2 \
  --max-backoff-seconds 3 \
  --output-dir data/backfill_reports
```

脚本行为和边界：

- 使用公开 RSS/发现源补 raw news 覆盖。
- 不调用 DeepSeek。
- 不抓第三方详情页正文。
- 不绕过登录、付费墙、验证码或浏览器校验。
- GDELT/Google News 均为 C-tier discovery feed，进入预测或高置信结论前必须做 RAG/citation 过滤和人工/规则复核。

### Historical Fill and Weight Loop

历史新闻只用于构建当时可见的事件证据和到期复盘，不能把后验新闻回填到预测时点。事件候选、快照和聚类工具必须保留 `visible_at`、来源等级与引用关系。

历史价格或行业链补数必须来自真实公开页、官方/API 源、CSV/人工核验或内部自有记录；不得使用代理价、插值、外推或模型生成数据填评分或复盘缺口。任何复盘指标必须同屏披露 scored/total、coverage、leaks 和用途限制。

## Frontend UX

### 首页即时精选

卡片字段：

- 标题
- 分类
- 影响方向
- 热度分
- 证据等级
- 推荐理由
- 影响链摘要
- 来源数量
- 最新更新时间

### 事件详情页

分四栏：

1. 事实：官方原文、发布时间、来源链接。
2. 推断：成本传导链。
3. 反证：可能削弱影响的证据。
4. 操作：确认、误报、降级、加入预测快照。

### 日报页

结构：

```text
今日一句话
原油/OPEC 政策
制裁与地缘
航运与海事安全
国内政策
企业公告与装置
市场弱信号
明日观察清单
```

## Observability

关键指标：

| Metric | Meaning |
| --- | --- |
| `news_fetch_total{source_id,status}` | 抓取次数和结果 |
| `news_fetch_latency_seconds{source_id}` | 抓取耗时 |
| `news_articles_ingested_total{source_id}` | 原文入库数量 |
| `news_events_created_total{category}` | 事件生成数量 |
| `news_featured_total{category}` | 精选数量 |
| `news_dedup_ratio` | 去重比例 |
| `news_llm_classification_latency_seconds` | AI 结构化耗时 |
| `news_human_review_pending` | 待人工复核数量 |

健康状态：

- `ready`: 关键 A/B 源可抓取，最近一次成功在 SLA 内。
- `degraded`: 部分 C/D 源失败，但不影响核心判断。
- `blocked`: A 级官方源连续失败或解析结构变化。

## Evaluation Suite

至少覆盖 20 条回归样例：

1. OPEC 减产声明。
2. OPEC 增产或配额放松。
3. OFAC 新增能源运输制裁。
4. EU 对俄油保险限制。
5. White House 能源/关税行政令。
6. EIA 库存超预期增加。
7. EIA 炼厂开工下降。
8. 红海船只遇袭。
9. 霍尔木兹航行警告。
10. 国内成品油调价。
11. 发改委/能源局油气管网政策。
12. 工信部/环保限产政策。
13. 恒力/荣盛/PX/PTA 投产公告。
14. 装置事故或检修公告。
15. 公司财报但无供需影响。
16. 媒体重复报道同一事件。
17. C 级媒体未被官方确认。
18. D 级人工石油股异动观察。
19. 标题党但正文无关。
20. 旧新闻被重复抓取。

验收目标：

- 事件分类准确率 >= 85%。
- 重大 A/B 源事件漏报率逐月下降。
- 重复事件合并准确率 >= 90%。
- C/D 级来源误升为高置信事件次数为 0。

## Implementation Phases

### Phase 1: News Core

- 已增加 `news_articles`、`news_event_clusters`、`news_fetch_runs` 表。
- 已接入 RSS/HTML fetcher 和手工/Computer Use CSV 导入。
- 首批源：OPEC、EIA Press、EIA WPSR、OFAC/Treasury、State Department、EU Council、NDRC、NEA、UKMTO/MARAD。
- 已实现去重、基础打分，并将高分 A/B 级聚类提升为 `event_observations`。

### Phase 2: AI HOT-style Curation

- 增加 `featured` 默认视图。
- 增加 heat_score 和 recommendation_reason。
- 增加日报生成。
- 增加前端即时精选、日报、全量候选三个入口。

### Phase 3: Human Review and Feedback

- 增加人工确认/误报/降级。
- 人工反馈回写评分规则。
- C/D 级来源进入待复核队列。
- 事件可一键加入预测快照。
- 事件方向调参前，使用现有事件/新闻 API、`docs/data-templates/events_and_alerts.csv`、`docs/data-templates/prediction_review_labels.csv` 或 RAG evidence queue 做人工复核记录。

复核规则：

- 只复核 `current_direction`、建议方向、证据 URL、反证和人工结论。
- 复核记录不应自动固化权重；进入策略前必须经过 eval/gate 脚本重新验证。
- 只有证据文本存在明确的原油供需、航运、制裁、政策或化纤链传导路径时，才应把中性事件改成方向性事件；宏观背景和泛新闻默认保持中性。
- 不联网读取付费源，不根据事后价格倒推标签，不用 C/D 级来源单独生成高置信方向。

### Phase 4: Domain Expansion

- 增加上市公司公告专项 parser。
- 增加 PDF 报告解析。
- 增加 Computer Use 辅助录入模板。
- 增加周报复盘：哪些事件真的影响了成本压力。

## Hard Boundaries

- 不绕过登录、验证码、付费墙或商业终端限制。
- 不从券商终端无人值守复制行情。
- 不把商业新闻全文再分发。
- 不用 C/D 级来源单独生成高置信结论。
- 所有 AI 摘要都必须能回到原始 URL 或人工录入证据。

## V2 Public News Backfill Run

2026-06-19 本地完成年度 V2 raw news 覆盖补齐。脚本入口：

```bash
cd /path/to/project/server
uv run python scripts/public_news_backfill_v2.py \
  --db data/agent.db \
  --providers google \
  --start 2025-06-16 \
  --end 2026-06-15 \
  --target-raw-news 2000 \
  --window-days 30 \
  --sleep-seconds 0.2 \
  --timeout-seconds 10 \
  --attempts 2 \
  --max-backoff-seconds 3 \
  --output-dir data/backfill_reports
```

本轮只使用公开、无需登录/付费/验证码的数据源；没有调用 DeepSeek，没有改 VPN/代理，没有 push。GDELT 公共 DOC API 先做了 1 请求 dry-run 探针，但当前返回失败，因此实际补量使用 Google News 公开 RSS 作为 C-tier discovery feed。脚本仍保留 GDELT provider、rate limit、timeout、retry/backoff 和失败查询报告。

报告：

- GDELT 探针：`server/data/backfill_reports/public-news-backfill-v2-2025-06-16-to-2026-06-15-dry-run.json`
- Google RSS dry-run：`server/data/backfill_reports/public-news-backfill-v2-2025-06-16-to-2026-06-15-dry-run-20260618T211210Z.json`
- 实际入库：`server/data/backfill_reports/public-news-backfill-v2-2025-06-16-to-2026-06-15-run-20260618T211246Z.json`

实际入库报告：`before_raw_news=1786`，`after_raw_news=2016`，新增 `230` 条 `news_articles`，请求 `15` 次，失败 `0` 次。最终只读核验：`news_articles=2059`，V2 窗口 raw_news `2016`，窗口发布时间覆盖 `2025-06-16..2026-06-15`，不可解析 `published_at=0`。

新增 source breakdown 前几项：

- `google_news_v2_oil_policy`: 475
- `google_news_v2_middle_east_shipping`: 440
- `google_news_oil_rss`: 293
- `google_news_v2_sanctions`: 267
- `google_news_v2_polyester_chain`: 161

噪声警告：GDELT/Google News 都是 C-tier public discovery feed，可能包含标题重复、通讯社转载、宽查询误入和 headline-only 记录。脚本不会抓第三方详情页正文；无正文时 `raw_text` 只保留公开索引/RSS 摘要并在 `raw.body_status=summary_only`、`raw.body_note` 标记。后续进入预测或高置信结论前，必须做 RAG/citation 过滤和人工/规则复核。

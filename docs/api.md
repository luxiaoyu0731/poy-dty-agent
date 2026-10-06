# API Contract

Primary base path: `/api/v1`

## Pipeline resource observations

`GET /api/v1/pipeline/graph` retains eighteen backend nodes and adds two non-node
blocks. `memory` (`pipeline-memory.v1`) contains `recall_enabled`, its fixed
`capability_source`, nullable `index_docs` / `lessons_active` with separate fixed
`*_source` fields, `data_as_of`, and independent `status` / `status_evidence`.
An enabled flag or nonzero resource count cannot establish successful recall.
Historical business-date requests do not borrow current resource counts.

`chain_budget` contains nullable `attempts_used`, `cap`, Beijing `business_date`,
`basis` (HTTP 尝试级，含 schema 重试), fixed `source`, `status`, and `status_detail`.
Only a valid same-day, already-visible chain-report budget snapshot establishes
attempts. Stage sums and `llm_traces` are never substitutes. Without that snapshot,
usage is unknown; the cap comes from effective configuration, or is null if that
configuration is invalid. Existing reports lacking the snapshot remain unknown.

The versioned contract is maintained in `docs/openapi.yaml`. Clients must use `/api/v1`; the unversioned `/api` compatibility surface is not part of the supported product contract.

## Health

- `GET /api/v1/health` returns process liveness and environment.
- `GET /api/v1/health/live` returns process liveness and environment.
- `GET /api/v1/health/ready` returns readiness checks for source registry, storage, LLM mode, and internal auth mode.
- `GET /api/v1/health/deep` returns dependency details for release checks and troubleshooting.
- `GET /metrics` returns Prometheus-style counters.

## Intelligence

- `GET /api/v1/overview`
- `GET /api/v1/full-chain/summary`
- `GET /api/v1/factors`
- `GET /api/v1/morning-brief`
- `GET /api/v1/events`
- `GET /api/v1/events/{event_id}/reasoning`
- `GET /api/v1/predictions`
- `GET /api/v1/forecasts/seven-product?as_of_time=<iso-datetime>`
- `GET /api/v1/forecasts/seven-product/history?limit=30`
- `GET /api/v1/forecasts/seven-product/evaluation?as_of_time=<iso-datetime>`
- `GET /api/v1/forecasts/seven-product/model-registry`
- `GET /api/v1/forecasts/seven-product/export?format=json|csv&as_of_time=<iso-datetime>`
- `GET /api/v1/predictions/model-signal?target=POY%2FDTY%20上游成本压力&horizon_days=14`
- `POST /api/v1/predictions`
- `POST /api/v1/predictions/review-due?force=true|false`
- `GET /api/v1/predictions/reviews`
- `GET /api/v1/price-comparison?product=crude_oil&start=&end=`
- `POST /api/v1/price-comparison/fetch`
- `GET /api/v1/prices/latest`
- `GET /api/v1/prices/intraday?instrument=PTA&limit=200`
- `GET /api/v1/benchmarks/public-v2`
- `POST /api/v1/prices/collect-now`
- `GET /api/v1/futures/daily-bars?product=PTA&start=2025-01-01&end=&contract_role=main&limit=2000`
- `POST /api/v1/futures/import/daily-bars` with `Content-Type: text/csv`
- `GET /api/v1/knowledge/graph`
- `GET /api/v1/knowledge/search?q=<query>`
- `GET /api/v1/knowledge/retrieval?q=<query>&limit=8&as_of_time=<iso-datetime>`
- `GET /api/v1/knowledge/evidence-queue?status=unreviewed|reviewed|rejected|all&q=&limit=`
- `PATCH /api/v1/knowledge/evidence-queue/{doc_id}` accepts `{status, purpose, evidence_role, notes}`; `result` is derived server-side (`reviewed→approved`, `rejected→rejected`, `unreviewed→inconclusive`). `PERSONAL_MODE=1` treats a `reviewed` record as the operator's own approval for the formal gate; with `PERSONAL_MODE` off, the formal gate stays closed for these minimal reviews.

`price-comparison/fetch` is an internal write operation. It fetches configured EIA/FRED Brent/WTI history for the requested date window and stores observations before comparison.

`prices/collect-now` is an internal write operation. It stores only public no-login exchange/proxy quotes or public page valuation refreshes. MEG/POY/DTY rows are marked as non-transaction public spot valuations, not minute-by-minute trade prices. The accepted SunSirs MEG source additionally writes an append-only `meg.sunsirs.china.spot_assessment.cny_mt` revision under `seven-product-labels.v3`, with first successful capture as `published_at/visible_at`; a replay with the same evidence hash is idempotent. Other public quote paths remain monitor-only, and no proxy can substitute for the current label or strict OOS scoring.

`benchmarks/public-v2` is the current unattended **input-health/context contract**, not the current prediction contract. It returns
exactly nine public inputs (Brent, WTI, Naphtha, PX, PTA, MEG, POY, DTY and
CFETS USD/CNY), evaluates freshness with each source's declared cadence, and
returns POY/DTY upstream-cost ratios as derived targets. Historical CCF rows do
not qualify, block or degrade this response. Formal forecast status is governed
only by the seven-product endpoints and their per-cell OOS evidence.

`futures/import/daily-bars` is an internal import path for active INE SC and CZCE PTA/PX/methanol daily futures bars from 2025-01-01 onward. DCE rows are rejected with `dce_source_soft_removed` and never written. The path accepts normal-access official exchange or user-provided files, normalizes actual contracts and continuous contracts into `futures_daily_bars`, assigns near-month/next-month by delivery order, and assigns the internal main contract by largest open interest after close with volume as the tie-breaker. The import preserves `source_publish_time`, `visible_at`, `source_note`, `revision_note`, and `license_scope` so backtests and reports can enforce as-of visibility and applicable usage limits. It must not be wired to any connector that bypasses login, paywalls, CAPTCHAs, or technical access controls. Template: `docs/data-templates/futures_daily_bars.csv`; runbook: `docs/futures-daily-data-plan.md`.

`predictions/review-due` is an internal write operation. It marks due predictions as reviewed; `force=true` is useful for personal research runs where the user wants an immediate backtest against currently stored price observations.

`POST /api/v1/predictions` is an internal formal-write boundary that stays zero-write. Authentication and request-model validation run first; every otherwise valid request then returns `409 formal_prediction_write_path_disabled` with a stable message. The handler does not build a full-chain summary, create a snapshot, read legacy snapshot/judgement gates, call the formal batch domain, or persist a prediction. Formal results are published only through the assessment-backed batch chain once data-quality gates pass. `GET /api/v1/predictions`, downstream review/read behavior, and the non-formal model-signal endpoints are unaffected.

`predictions/model-signal` returns a forward-only strategy signal with `entry_decision=enter|abstain`. Only `enter` signals should be written to the prediction ledger; `abstain` means the direction is retained for monitoring but excluded from formal high-confidence prediction accounting.

`full-chain/summary` is a truthful readiness boundary for the full-chain summary. Until a formal snapshot exists it returns `status=data_not_ready`, an empty `summary` list, and empty `oil`, `transmission`, and `poy_dty_gate` objects. Clients must render that state as data not ready and must not synthesize a chain conclusion.

## Daily Workbench Snapshot

- `GET /api/v1/workbench/snapshot?business_date=YYYY-MM-DD` returns the latest immutable daily judgement when `business_date` is omitted, or the snapshot for the requested Shanghai business date. It returns `404` when no matching snapshot exists and `422` for an invalid date.
- `POST /api/v1/workbench/snapshot/materialize` accepts an optional JSON object with `business_date` and `source_run_id`. It is idempotent for an already materialized business date, returns `409` when no completed Agent run is ready for that date, and returns `422` for invalid input.

Materialization is an internal write operation. When `ENFORCE_INTERNAL_TOKEN=1`, callers must provide a valid loopback local-session cookie or `X-Internal-Token`. The read endpoint does not materialize data and does not require internal authorization.

## Local Session

- `POST /api/v1/auth/local-session` issues a loopback-only local session cookie when local session auth is enabled; otherwise it returns `403`.
- `POST /api/v1/auth/local-session/logout` clears the local session cookie. It mirrors the login gating (`403` when local session auth is disabled or the request is not from loopback); clearing an absent cookie is still a `200`.

Successful responses expose the persisted snapshot identity, business date, generation/as-of timestamps, publication status, source run and data snapshot identifiers, payload SHA-256, idempotent replay state, the authoritative `workbench` payload, and a `live` policy object. Consumers must treat `workbench` as the locked daily conclusion; live observations do not silently rewrite it.

Prediction-ledger response fields:

- `StoredPredictionRecord.horizon_days`: normalized horizon in days when the stored horizon is recognized.
- `StoredPredictionRecord.due_at`: ISO timestamp at which the record becomes due, or `null` for an invalid schedule.
- `StoredPredictionRecord.lifecycle_status`: `pending_due`, `due_pending_data`, `reviewed`, or `invalid_schedule`.
- `PredictionReview.due_at`: the same due boundary used for review eligibility.
- `PredictionReview.review_status`: lifecycle-oriented review state (`pending_due`, `due_pending_data`, `reviewed`, or `invalid_schedule`).
- `PredictionReview.scoreability`: `not_due`, `waiting_for_data`, `scorable`, `scored`, or `invalid`.
- `PredictionReview.scored_count`, `total_count`, and `coverage`: the scored sample count, eligible total and their 0–1 ratio. Display them together.
- `PredictionReview.leakage_check`: `not_run`, `passed`, or `failed`; a weight adjustment remains advice and is not automatically applied.

## Sources

- `GET /api/v1/sources?tier=A|B|C|D`
- `GET /api/v1/crawler/pipelines`
- `GET /api/v1/crawler/source-readiness`
- `GET /api/v1/delivery/status`
- `POST /api/v1/sources/fetch-configured`
- `POST /api/v1/sources/{source_id}/fetch`
- `GET /api/v1/sources/fetch-audit`

`fetch` is guarded by source registry configuration. In the 0-cost MVP, API-key sources mean free provider keys such as EIA, FRED, or UN Comtrade. Paid vendor-license sources are intentionally not part of the active source registry. The canonical readiness vocabulary is in `docs/source-readiness.md`; a source can be configured without being fetchable or parsed into normalized observations.

Each `SourceConfig` also exposes additive role metadata. `data_role` distinguishes current candidates, historical context, event evidence and review-only inputs; `current_formal_eligible=false` prevents a source from entering current formal selection even when its provenance tier is A/B; `product_roles` can narrow a specific product (for example, a broad xylenes context proxy that is not pure PX). Older clients may ignore these optional fields, whose defaults preserve the previous response contract.

`fetch-configured` currently fetches the configured EIA/FRED structured adapters and creates a data snapshot after storing observations.

`delivery/status` is a read-only readiness endpoint for the research workbench. It summarizes authorized-source state, coverage gaps, replenishment tasks, data-quality gates, update cadence and guardrails. It must not include credentials, raw vendor exports, mock business conclusions or execution recommendations.

Customer-facing delivery fields:

- `coverage_gaps`: whether current public upstream-chain inputs and target indicators are covered.
- `review_metrics`: cost-pressure ledger review totals, scored coverage, pending rows, leakage checks and use limitations. Any hit rate must be displayed with scored/total and coverage.
- `quality_gates`: coverage, freshness, unit, anomaly, evidence and leakage gates.
- `update_schedule`: daily/weekly source refresh cadence and trigger rules.
- `client_reports`: current research brief and public quality-gate report.

`fetch`, `fetch-configured`, and `fetch-audit` are internal routes. When `ENFORCE_INTERNAL_TOKEN=1`, clients must send:

```http
X-Internal-Token: <deployment-secret>
```

## 0-Cost Data Ingestion

The current product target is a fully 0-cost MVP excluding AI token cost. It uses free public sources, free API keys, and local CSV/OCR/manual notes. The canonical plan lives in `docs/zero-cost-mvp.md`; CSV/YAML templates live in `docs/data-templates/`.

Implemented contracts:

- `GET /api/v1/market-observations?source_id=&product=&indicator=&limit=`
- `GET /api/v1/industry-observations?product=&metric=&limit=`
- `GET /api/v1/events/observations?limit=`
- `POST /api/v1/imports/public-observations` with `Content-Type: text/csv`
- `POST /api/v1/imports/industry-observations` with `Content-Type: text/csv`
- `POST /api/v1/imports/events` with `Content-Type: text/csv`
- `POST /api/v1/imports/news-observations` with `Content-Type: text/csv`
- `POST /api/v1/data-snapshots`
- `GET /api/v1/data-snapshots/{snapshot_id}`

Imports are internal write operations. They return accepted row count, rejected row count, validation errors, and a `data_snapshot_id` when at least one row is accepted.

API-key sources:

- `EIA_API_KEY` powers `eia_petroleum_api` and writes latest WTI/Brent spot observations into `market_observations`.
- `FRED_API_KEY` powers `fred_macro_api` and writes selected macro/oil observations into `market_observations`.
- `UN_COMTRADE_API_KEY` is optional for the current MVP and remains a future monthly trade-flow enhancement.
- `dce_meg` is soft-removed. Exact registry lookup remains available for audit, but list, fetch, import, scheduler, alert, preflight and current-formal paths exclude it; direct fetch returns HTTP 410 `source_soft_removed`. Historical rows, adapter code and locally retained credentials are not deleted.

`/overview`, `/factors`, `/morning-brief`, `/events`, and `/predictions/reviews` are observation-backed. When observations are missing, they return low-confidence empty-state outputs instead of fixture market data.

`/factors` now includes an `EVENTS` factor when official/manual event observations are present. The event factor is capped and directional, so A/B-tier geopolitical, shipping, sanction, or policy events can move the cost-pressure index while still requiring price and industry-data confirmation. `/overview.confidence` is capped when key data layers are missing; for example, with no POY/DTY/PX/PTA/MEG industry observations, confidence will not exceed the industry-gap cap even if public market and event data are present.

## Seven-Product Forecast (Current Contract)

`seven-product-forecast.v1` returns exactly 21 cells: crude, naphtha, PX, PTA,
MEG, POY and DTY, each at D1, D7 and D30. Every cell exposes a point forecast,
interval, direction, confidence, evidence/provenance, data gaps and immutable input
hashes. A cell remains `reference` or `blocked` unless an active model-registry
approval binds the exact passed evaluation result; merely producing a number never
makes it formal.

The read-only model-registry response uses `seven-product-model-registry.v2` and
keeps two independent per-cell grids. `champions` is the formal grid and still
requires an explicitly approved rolling OOS result. `reference_champions` accepts
only a hash-valid research report whose exact cell is listed as a production
candidate and whose model has an implemented runtime. Reference approval never
changes the formal grid and has a hard status ceiling of `reference`.

An active formal champion is resolved before the reference grid. Formal approval
recomputes the canonical evaluation report SHA-256, checks the derived evaluation
ID/count/status, requires the frozen 5%/55%/20-sample policy, and rejects a model
without an implemented runtime. Missing required features or three latest valid
losses to `persistence.v1` select the recorded formal rollback target (or the
immutable persistence baseline when no prior champion exists). That runtime cell
is explicitly degraded and cannot inherit the displaced champion's approval.

At daily issue time, an activated reference model is checked against its declared
feature series and the immutable settled ledger. Missing/stale/mismatched required
features fall back before issue. Three latest consecutive valid outcomes with
strictly larger absolute error than `persistence.v1` also select the recorded
fallback model; invalidated outcomes never count. The selected model and decision
reason are frozen into the cell configuration hash and visible through
`model_version`, `key_drivers` and `data_gaps`. The default registry has zero active
reference champions, so this contract does not manufacture a promotion from the
current failed simulations.

There is no mutating promotion API and no automatic promotion. The operator CLI
`server/scripts/run_seven_product_model_governance.py` consumes the content-addressed
evaluation evidence, requires explicit actor/reason/cell/`--approve` arguments and
emits a 0600 content-addressed proposal plus the exact proposed registry. It verifies
that the input registry did not change and never edits the current release. Applying
the proposal remains a separately reviewed commit and immutable deployment.

The current label registry is `seven-product-labels.v5`. It retires the crude OOS
label: the EIA Europe Brent Spot Price FOB assessment drops to an evidence-only
series and the crude label becomes the ICE Brent front-month daily close
(`BZ=F`, `yahoo_futures_daily_proxy`, USD/bbl, daily). The EIA assessment ran
15-25 USD/bbl away from every market-facing series and its history arrived in one
bulk backfill, which made direction scoring against it meaningless. Crude
freshness tightened from 10 days to 4 days to match the daily cadence. Frozen
v1-v4 identities remain valid for audit and settlement: a crude cell issued
before v5 still settles on the EIA series its own contract named, and v5 never
rewrites an already issued cell.

Without `as_of_time`, the current endpoint returns the latest hash-audited batch
that the daily production lifecycle actually froze. It returns `503
seven_product_forecast_ledger_empty` before the first successful production
issue; a read never creates or silently recomputes a batch. An explicit
`as_of_time` remains a read-only point-in-time preview for audit and diagnosis.

`GET /api/v1/forecasts/seven-product/history` returns up to 366 immutable issued
batches. Each batch contains the exact issued cells plus a settlement state and,
when a later observation on or after the D1/D7/D30 natural-day maturity is visible, the actual value,
absolute/percentage error and direction hit. D1/D7/D30 maturity is counted by
calendar days from the origin observation date, followed by the first qualifying
published observation; it is not counted by observation ordinal. Actuals visible at the issue
cutoff, source/unit mismatches, incomplete evidence identity and missing raw
hashes fail closed and are never scored. Settlement resolves the source, semantic
series and unit from the registry version frozen on the issued cell. A legacy
outcome later proven to cross that frozen contract is retained append-only but
returned as `invalidated_contract_mismatch`; its actual/error fields are excluded
from scored display instead of rewriting historical rows.

`GET /api/v1/forecasts/seven-product/evaluation` runs the same frozen target matrix
through expanding-window out-of-sample evaluation. It reports sample count, MAE,
best-naive baseline, relative improvement, direction accuracy, bootstrap confidence
intervals, worst regimes and leakage checks per cell. The hard formal gates are at
least 20 scored samples, at least 5% MAE improvement over the best naive baseline,
at least 55% direction accuracy and no leakage or source-identity failure.
Each cell also returns up to five latest rolling OOS outcomes with the origin
identity/time, predicted value and direction, later actual value and its first-visible
time, absolute error and direction hit. Schema `seven-product-evaluation.v2` adds
that audit field. Policy `seven-product-oos-gate.v3` admits an
outcome only when its business date had not elapsed at the origin information cutoff
and its value became visible strictly after that cutoff. A same-capture archive may
become training data for later forecasts, but contributes no historical OOS samples.
These rows come from the same frozen evaluation samples and do not relax promotion
thresholds. Forecast evidence includes the original artifact
SHA-256 when the source adapter provides it.

The export endpoint returns JSON or a 21-row CSV from the same issued ledger,
evaluation and registry readers used by the UI. JSON includes `issued_history`;
CSV includes the latest issued business date, settlement status, actual, error
and direction hit. Operator inventory, realized
processing profit, quotes, procurement decisions and trading instructions are not
model inputs or outputs.

## Legacy Cost-Pressure Outlook (Historical Contract)

The former `POY/DTY upstream cost pressure` scalar ledger, D14 compatibility rows,
CCF-derived rows and Phase A formal-batch graph remain readable for historical audit.
They are never upgraded implicitly into `seven-product-forecast.v1` and do not count
toward current formal coverage.

## News Radar

- `GET /api/v1/news/sources`
- `POST /api/v1/news/fetch-runs?source_id=<optional>&limit_per_source=20&mode=live|archive&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD&cursor_pages=3&include_details=true`
- `GET /api/v1/news/fetch-runs?limit=`
- `GET /api/v1/news/articles?category=&source_id=&tier=&q=&published_after=&published_before=&limit=`
- `GET /api/v1/news/events?category=&source_id=&tier=&status=candidate|featured&q=&limit=`

The news radar is 0-cost and compliance-first. It only reads public official or quasi-official pages configured in the local news source list, stores article metadata in `news_articles`, clusters relevant items in `news_event_clusters`, and promotes high-scoring A/B-tier clusters into `event_observations`.

`mode=live` reads the currently visible public page/RSS items for configured sources. `mode=archive` uses source-specific archive/date-window URL builders where available. `cursor_pages` controls archive pagination depth, bounded to 1-10 pages per source. `include_details=true` fetches detail pages for A/B sources when the detail host is in `OUTBOUND_FETCH_HOSTS`; C-tier discovery sources are not expanded into third-party article bodies.

Current archive builders cover EIA Press yearly releases, EIA Today in Energy yearly archives, OFAC, Treasury, EU Council query URLs, Federal Reserve yearly pages, IEA public pagination, Google News date-fenced RSS queries, GDELT daily date windows, White House paginated lists, UN/UN Security Council listing cursors, and UK government search pages. Sources without an adapter fall back to their live URL and should be treated as shallow backfill until a dedicated parser is added.

Operational notes:

- IEA uses a source-specific listing parser that extracts title, detail URL, and `DD Month YYYY` publication dates from the public news page.
- EIA Press and EIA Today in Energy use source-specific dated-list parsers so archive mode can filter by release date before writing articles.
- GDELT is treated as a slow C-tier discovery source. Archive mode slices requests by day with `startdatetime`/`enddatetime` and strong backoff; keep it in a slow queue, and keep C-tier items as candidates until human review or A/B confirmation.
- Browser-check or anti-bot protected public pages such as OPEC or EU Council are recorded with readable fetch errors and should be handled through manual/Computer Use workflows or alternate official feeds; the system must not bypass those controls.

The active news registry now has three layers:

- A/B official anchors: OPEC, EIA, OFAC, Treasury, State Department, EU Council, NDRC, NEA, White House, UN, IEA, Federal Reserve, CFTC, CENTCOM, DoD, NATO, European Commission, UK government/FCDO, IMO, MARAD, MPA, U.S. Coast Guard, and exchange/company-announcement portals.
- C discovery sources: Google News RSS and GDELT RSS for oil, geopolitics, and polyester-chain clues.
- Manual/CSV backfill: `POST /imports/news-observations` with `docs/data-templates/news_observations.csv`, useful when a site blocks archive traversal or needs Computer Use assisted collection.

News clustering deduplicates by canonical article URL and groups event clusters by date, category, affected products, and matched entity/keyword signals. When a related article variant is ingested later, the existing cluster merges `source_ids`, `article_ids`, and affected products instead of replacing them.

Manual or Computer Use assisted news notes can use:

```csv
source_id,tier,title,source_url,published_at,summary,raw_text,language,category
ofac_recent_actions,A,OFAC sanctions Iranian tanker network,https://ofac.treasury.gov/recent-actions/20260605,2026-06-05,shipping sanctions,OFAC sanctions Iran tanker shadow shipping crude oil LPG network,en,sanctions_geopolitics
```

`news/fetch-runs`, `imports/news-observations`, `price-comparison/fetch`, `predictions`, and `predictions/review-due` are internal routes protected by a loopback local session or `X-Internal-Token` when enforcement is enabled.

## Assistant

显式 `N 句话` 请求返回 `length_constraint_sentences: N`，前端只呈现结论与可信边界；普通请求为 `null` 并保留分段回答。结论中的数值陈述必须通过对应材料的引用支持检查，未通过的数值句不作为“推断”输出，审计 warnings 记录 `unverified_numeric_claims_removed` 数量。

- `POST /api/v1/assistant/chat`
- `GET /api/v1/assistant/chat?q=<query>`
- `POST /api/v1/assistant/chat/stream`

  Returns `text/plain` chunks. `X-Assistant-Stream-Mode` and
  `X-Assistant-Provider` describe transport/provider behavior. Governed runs
  also return `X-Agent-Run-ID`, equal to both `agent_run_id` and `answer_id`;
  preview responses have `agent_run_id=null` and omit that header.
- `GET /api/v1/assistant/traces`
- `POST /api/v1/assistant/evals`
- `GET /api/v1/agent-runs/{run_id}/evaluation`

Request:

```json
{
  "question": "string, max 1200 chars by default",
  "context_event_id": "optional event id"
}
```

Response:

```json
{
  "answer": "string",
  "cited_source_ids": ["doc id"],
  "evidence_level": "A|B|C|D",
  "confidence": 0.72,
  "citation_coverage": {
    "factual_sentence_count": 4,
    "covered_sentence_count": 3,
    "coverage_ratio": 0.75,
    "missing_sentences": ["string"],
    "sentence_bindings": []
  },
  "evidence": [
    {
      "doc_id": "news_event:...",
      "doc_type": "news_event_cluster",
      "source_id": "ofac_recent_actions",
      "tier": "A",
      "title": "string",
      "summary": "string",
      "url": "https://...",
      "observed_at": "2026-06-12",
      "score": 12.4,
      "snippet": "string",
      "risk_flags": [],
      "review_status": "unreviewed"
    }
  ],
  "warnings": ["string"]
}
```

`chat`, `chat/stream`, `traces`, `evals`, and `rag-evals` are internal routes protected by a loopback local session or `X-Internal-Token` when enforcement is enabled. Provider-backed chat is not an anonymous public endpoint; exposed deployments must put it behind user auth, a private reverse proxy, or server-to-server token enforcement.

`agent-runs/{run_id}/evaluation` is also internal-only and read-only. Eligibility
requires the production `assistant_pipeline` source and trace type, the fixed
governance/stage contract, and versioned server-reserved provenance bound to the
same persisted run ID. Caller-supplied source, trace type, and ordinary metadata
are insufficient. `POST /api/v1/agent-runs` returns `422` if its metadata tries
to set `server_reserved_provenance`. Missing runs return `404`; runs without the
complete creator proof return `409`; trace/request run-ID mismatch or another
evaluation integrity failure returns a safe `500`. Running, failed, and
structurally incomplete eligible runs are evaluated as-is with `200` and a
fail-closed result rather than normalized into success. A successful response
is the bounded `agent-run-eval.v1` result only and omits the run ID, trace, raw
content, and all free text. Calls do not persist results and do not imply
production readiness.

`chat` and `chat/stream` create a versioned Context Pack from the shared
persistent hybrid Retriever. The complete question is the primary query.
GraphRAG expansion and Memory are time/review filtered and cannot bypass the
final evidence whitelist. The model only receives the bounded Context Pack.

`chat/stream` currently returns local chunks after the complete validated model
response. `X-Assistant-Stream-Mode: simulated` makes this explicit; it is not
provider-native token streaming.

`evidence-queue` is an internal human-review workflow. Evidence starts as `unreviewed`; reviewed items get a retrieval boost, and rejected items are excluded from normal RAG retrieval without deleting the raw source record.

Assistant responses expose separate citation presence, validity, reproducible
local support/entailment, and conflict checks. Conclusion/evidence claims without
support are downgraded and prevent a successful formal answer. This local
support check is auditable but is not a general-purpose NLI proof.

Daily RAG smoke/regression checks can be run with:

```bash
npm run eval:rag
```

or through:

- `POST /api/v1/assistant/rag-evals`

## Prediction Ledger and Legacy Phase A Write Boundary

`POST /api/v1/predictions` does not persist predictions. It remains an internal, authenticated zero-write placeholder: the scalar write path is disabled and formal results flow only through the assessment-backed batch chain. When `ENFORCE_INTERNAL_TOKEN=1`, a request without valid loopback local-session or `X-Internal-Token` authorization returns `401`; a deployment with enforcement enabled but no configured internal token returns `503`. Request-model errors return `422`. Only after those checks does a valid request return the stable `409 formal_prediction_write_path_disabled` containment response, with no automatic snapshot or prediction-ledger write.

`POST /api/v1/internal/formal-prediction-batches` is the contained historical
Phase A batch operation; it is not a current seven-product write path and cannot
promote a 42-cell batch into the current 21-cell contract. It is internal-only and authenticates before reading any body
bytes. After authentication it consumes the request stream once with an exact
1 MiB (1,048,576 byte) pre-parse ceiling, requires `application/json`, rejects
duplicate keys at every nesting level, and validates the exact strict request
shape before delegating once to the governed synchronous persistence domain.

Success is `201` only, for either a new atomic revision or a historically
audited, byte-identical replay. The response contains exactly
`prediction_batch_id`, `revision_id`, `previous_revision_id`, `assessment_id`,
`data_snapshot_id`, the ordered D1/D7/D30 `proof_ids`, `policy_version`,
`contract_version`, `as_of_time`, and `idempotent_replay`. One successful new
write atomically persists the complete 42-cell grid and six terminal POY/DTY
subtargets; there is no partial-success response.

Malformed requests return the non-enumerating `422
FORMAL_PREDICTION_BATCH_REQUEST_INVALID`. Current empty or revoked trust roots,
unknown/blocked assessments, binding conflicts, conflicting revision reuse, and
pre-commit response projection failures return the generic `409
FORMAL_PREDICTION_BATCH_NOT_AUTHORIZED`; unexpected failures return generic
`500 FORMAL_PREDICTION_BATCH_FAILED`. Error responses never expose payload
fragments, duplicate keys, validation input, domain codes, or exception text.
The current production trust eligibility remains `0/19` and therefore fails
closed for new writes. A historically valid exact replay does not become a new
write when the current root is later revoked.

The browser has no scalar or formal-batch write helper or control. Formal
prediction batches can only be generated by governed internal workflows.

Validated request shape (currently contained with `409`):

```json
{
  "target": "POY/DTY 上游成本压力",
  "horizon": "7d",
  "direction": "中性",
  "confidence": 0.5,
  "rationale": "真实数据依据",
  "counter_evidence": "反证",
  "source_status": "awaiting_real_market_data",
  "tags": ["personal-research"],
  "data_snapshot_id": "optional existing snapshot id; omission does not create a snapshot while containment is active"
}
```

The request model accepts only `1d`, `7d`, and `30d`; invalid horizons (including a new `14d` request) return `422` before containment. Existing `14d` ledger records remain readable and keep their 14-day due/review schedule. This model restriction does not change the generic `horizon_days` parameter on model-signal APIs. `direction` must be a formal forecast direction such as `利多`, `利空`, `中性`, `偏强`, or `偏弱`; waiting/pending phrases are rejected during model validation. Enabling successful formal writes requires a separately reviewed implementation of the trusted 19-series/57-record assessment plus request-to-storage proof binding; no current snapshot, judgement, or digest alone is sufficient.

## Experience Card Internal Reads

- `GET /api/v1/experience-cards/revisions/{revision_id}` returns one immutable historical revision.
- `GET /api/v1/experience-cards/{experience_card_id}/head` returns the current append-only head.

Both endpoints are internal-only and use the same loopback local-session or
`X-Internal-Token` contract as other internal routes. They return the complete
persisted card without changing the meanings of
`as_of_time` (prediction time), `evaluation_as_of` (evaluation cutoff), source
observation/revision IDs, scoreability, visibility, or diagnostic fields. The
response requires and strictly types the same 25 core fields accepted by the
storage contract. Known builder fields are optional, and any other valid
canonical persisted extensions are returned unchanged (`additionalProperties:
true`) rather than rejected or silently removed. Both read paths validate the
complete persisted payload inside the route error boundary before response
serialization, so a corrupt core type or known optional field cannot escape as
an unstructured framework error. The
`POST /api/v1/experience-cards/settle` is an internal-only operational command.
It is disabled unless `EXPERIENCE_SETTLEMENT_API_ENABLED=1`, accepts neither a
body nor query parameters, and uses the server's Asia/Shanghai clock at the
already-frozen 09:30 weekday cutoff. It delegates only to the selected-input
settlement boundary; callers cannot supply a prediction revision, snapshot,
as-of time, series status, or calendar. Outside the exact cutoff it returns
`409 EXPERIENCE_SETTLEMENT_NOT_DUE`; while disabled it returns
`503 EXPERIENCE_SETTLEMENT_COMMAND_DISABLED`. A blocked selected-input result
remains zero-write. This endpoint does not enable the scheduler, promote a
source, or make Experience settlement public.

Missing revisions return `404` with code
`EXPERIENCE_CARD_REVISION_NOT_FOUND`; missing heads return `404` with code
`EXPERIENCE_CARD_HEAD_NOT_FOUND`. A persisted payload, identity, canonicalization,
or storage-contract integrity failure returns a stable `500` with code
`EXPERIENCE_CARD_INTEGRITY_ERROR`; underlying SQLite messages are not exposed.
Both operations explicitly document `401`, `404`, `422`, `500`, and `503` with
the standard error envelope. All responses, including errors, include
`X-Request-ID`.

## Error Model

All HTTP and validation errors use a standard envelope:

```json
{
  "error": {
    "request_id": "uuid-or-client-supplied-id",
    "code": "HTTP_404",
    "message": "event not found",
    "details": {}
  }
}
```

The response also includes `X-Request-ID`.

## Rate Limits

- `POST /api/v1/assistant/chat` and `/assistant/chat/stream`: controlled by `CHAT_RATE_LIMIT_PER_WINDOW`.
- `POST /api/v1/sources/{source_id}/fetch`: controlled by `SOURCE_FETCH_RATE_LIMIT_PER_WINDOW`.
- Window size is controlled by `RATE_LIMIT_WINDOW_SECONDS`.

## Industrial Intelligence Center (`/api/v1/intelligence/*`)

Status: **Local Implementation Complete / Release Candidate (schema v37)**. Not
yet deployed to production; production enablement requires an explicit
operator authorization plus the v36→v37 migration (see `docs/deployment.md`).
The whole surface stays mounted but returns 404 `intelligence_module_disabled`
while `INDUSTRIAL_INTELLIGENCE_ENABLED=0`.

All endpoints require the standard internal auth (`X-Internal-Token` or the
loopback HttpOnly local-session cookie). Every response carries
`X-Request-ID`; errors use the shared ErrorEnvelope. The frozen payload
contract version is `industrial-intelligence.v1`; the frozen model set lives
in `docs/openapi.yaml` (`SnapshotPage*`, `ItemRevision`, `ItemDetail`,
`EventSummary`, `EventDetail`, `EvidenceLink`, `SearchHit`, `BriefEnvelope`,
`DailyBrief`, `RunSummary`, `FeedbackCreate`/`FeedbackReceipt`,
`ProjectionRunReceipt`, `BriefMaterializationReceipt`, `SourceCatalogEntry`,
`MapResponse`/`IntelligenceMapProperties`). Every timestamp field is an ISO-8601 `date-time` with an
explicit UTC offset; naive timestamps are rejected at the response-model
boundary.

Read endpoints (strictly side-effect free):

| Path | Purpose | Key bounds |
| --- | --- | --- |
| `GET /intelligence/sources` | Derived catalog (core registry ∪ NEWS_SOURCES, 59 active baseline + soft-removed display entries + provider-declared USGS) | `limit<=200`; drift returns 200 with per-entry `metadata_drift` |
| `GET /intelligence/items` | Item revisions (radar raw layer) | `limit<=100`; filters `category,source_id` |
| `GET /intelligence/items/{item_id}` | Current valid revision | 404 `intelligence_item_not_found` |
| `GET /intelligence/items/{item_id}/revisions` | Revision history | `limit<=100` |
| `GET /intelligence/events` | Event cluster radar | `limit<=100`; filters `product,category,region,status,min_relevance` |
| `GET /intelligence/events/{event_id}` | Fact/inference-separated detail | 404 `intelligence_event_not_found` |
| `GET /intelligence/events/{event_id}/revisions` | Event revision history | `limit<=100` |
| `GET /intelligence/events/{event_id}/evidence` | Evidence edges | `limit<=200`; optional `event_revision_id` |
| `GET /intelligence/brief` | Frozen daily brief | No date → latest or 200 `data_not_ready`; explicit missing date → 404 `intelligence_brief_not_found` |
| `GET /intelligence/search` | FTS over display-allowed text | `q` 2–160 chars; `limit<=50` |
| `GET /intelligence/runs` | Run audit trail | `limit<=100`; filters `provider_id,status` |
| `GET /intelligence/map` | Same-origin GeoJSON features | Valid `bbox` required (422 otherwise); ≤1,000 features; strict trailing 48 hours by evidence occurrence time (or explicit publication time); unaggregated members (`cluster_count=1`) at every zoom, browser clusters without dropping members; accepted summary locations resolve to bundled site coordinates or explicitly coarse country points; unknown locations omitted. `applied_filters` records time basis, bounds and truncation |

Snapshot pagination: the first page freezes `snapshot_at` plus per-table
`MAX(append_seq)` high-water marks and returns an opaque `snapshot_id`; the
HMAC-signed cursor binds the manifest, filter hash, sort key, and contract
version (`intelligence-cursor.v1`). Late backfills never leak into an open
snapshot. Tampered, filter-mismatched, or exhausted cursors return 422
`intelligence_cursor_invalid` / `intelligence_cursor_filter_mismatch`.

Write endpoints (internal writes, rate limited via
`INTELLIGENCE_WRITE_RATE_LIMIT_PER_WINDOW`):

| Path | Purpose | Idempotency / errors |
| --- | --- | --- |
| `POST /intelligence/feedback` | Append operator feedback (event-stream semantics) | `Idempotency-Key` must equal `client_request_id`; replay returns `replayed:true`; unknown target 404; schema unavailable 503 |
| `POST /intelligence/projection/news` | Bounded read-only projection of legacy `news_articles` into v37 items | Resumable run; never writes legacy tables; 409 while another run holds the single-flight lock |
| `POST /intelligence/brief/materialize` | Freeze the unique daily brief for `business_date` | Same input replays the frozen brief; different content → 409 `intelligence_daily_brief_conflict`; before 09:30 Asia/Shanghai → 409 `intelligence_brief_not_due` |

`POST /intelligence/projection/news?limit=N` treats `N` as a real maximum
number of legacy rows scanned during that request; it is not merely a timeout
hint. Both write endpoints hold the same cross-process single-flight locks used
by the local runner and return 409 `intelligence_run_in_progress` on overlap.

The supported complete local DAG is the CLI documented in the runbook. The two
HTTP write endpoints remain bounded maintenance operations and do not create an
arbitrary provider-fetch or scheduler surface.

When v37 schema validation fails, both read and write routes fail closed with
503 `intelligence_schema_not_ready`; database integrity/type failures are not
misreported as unhandled 500 responses.

`Claim` keeps the required `claim_id`, `text`, and `evidence_link_ids`, plus
nullable source provenance (`source_tier`, `origin_group_id`, `canonical_url`,
`published_at`) when the stored fact supplies it. `Inference` never embeds a
duplicate path object; paths are returned once through `supply_chain_paths`.

Domain invariants enforced at the database layer: every
item/event/brief row carries `prediction_eligible=0` and
`instruction_eligible=0`; all six business tables are append-only
(UPDATE/DELETE aborted); JSON array references are trigger-validated; each
item revision freezes a rights snapshot and the API re-applies the current
(more strict) policy at read time via `presentation_status`/`redactions`.

## Remaining Contract Work

- Add paginated envelopes before list endpoints become large. (The
  intelligence surface already ships the snapshot-cursor envelope; legacy
  endpoints are pending.)
- Add user/session auth and RBAC before exposing admin operations to non-internal clients.
- Add compatibility tests if `/api` legacy paths are retained beyond staging.

### Immutable release identity (2026-09-06)

`GET /api/v1/health`, `/health/live` and `/health/ready` add `release` without
changing readiness semantics. Packaged builds return `status: available` and
`release_id`, `release_hash`, `git_sha`; development or older packages return
`status: unavailable`. The backend reads the manifest from its immutable code
checkout at process startup, not from the moving `current` symlink. Compare all
three fields with authenticated `/release.json`; unknown identity is unverified,
never an inferred match. No filesystem paths, branch names or credentials are
included in health output. The manifest is produced by release packaging, not
written by a health request.

### 事件中文概述（2026-09-07）

`GET /api/v1/workbench/event-library` 新增 `events[].overview_text` 和 `overview_basis`（`title` / `body` / null）。正文事实摘要优先；标题概述来自独立、按原标题哈希绑定的持久缓存，不覆盖 factual_summary、summary_generation_status 或 analysis_available。没有有效概述时文本为空，不虚报完成。

`overview_coverage` 为当前页统计，含 scope=current_page、total、completed、title、body。与正式事实摘要的 `summary_funnel` 分开。全库验收需遍历各页并核对唯一事件数与 total_events 一致，不能把首批页覆盖率作为全库覆盖率。GET 不生成内容、不消费模型额度。

`overview_source_title` 可提供经公开原站编码核对后的标题；原始 factual_title 保持不变，修正标题仍由原始标题哈希绑定。公开原站采用 GB2312/GBK 声明时按 GB18030 解码；已有两条中石油乱码标题已保留原文响应和哈希证据。

### 2026-09-07 验收修正补充

- `/health/ready` 的 `scope=infrastructure_readiness` 表示基础设施就绪；`details.intelligence_delivery.latest_brief` 单独报告最近可用摘要。它不证明当日完整交付或预测门禁通过。
- 情报 `EventSummary.overview_text` 为可空的中文阅读概述，读取已有、通过校验的标题概述；原文 `title`、冻结事件及其哈希保持原样。详情保留原文和来源证据。
- 日报 `horizon_impact` 按品种与期限合并展示，保留所有依据及缺口；反向证据显示多空交织，含方向未明证据时不自动升级为同向判断，重复报道不增加置信度。
- 观察生成与交互问答使用独立于普通读取的 60 秒浏览器预算。生成按钮在请求期间防重入；收到不确定响应后回读观察记录，不自动重复生产写入。观察记录本身仍为非正式材料。

### Information and evidence reports (2026-09-08)

`POST /api/v1/information-reports` accepts `{ "kind": "日报" | "周报" | "复盘" | "专题" }` and returns a persisted report's metadata. This is an information deliverable, independent of formal prediction promotion. It reads the latest issued price observations and at most 20 intelligence events collected in the last 1/7/7/30 days respectively. Occurrence, publication, collection and observation dates remain distinct. No LLM call or forecast write is performed. The review report additionally freezes valid scored outcomes from batches issued in the latest seven Shanghai calendar days, includes their original values/errors/source IDs, and excludes pending or invalidated outcomes from scoring.

`GET /api/v1/information-reports` returns `{items: [...]}` (latest 100). `GET /api/v1/information-reports/{id}/content` returns metadata plus frozen Markdown content; `GET /api/v1/information-reports/{id}/download` serves the same UTF-8 content with an attachment filename. Metadata: `id`, `kind`, `title`, `summary`, `generated_at`, `qualification=information_only`, `sha256`. Hash covers the canonical JSON record excluding `sha256`; it is validated before every read/download. Missing/invalid IDs return 404, integrity mismatch 409, persistence/read failures 503, invalid kind 422. Generation is limited to 10 requests per rate-limit window.

The existing internal authentication applies, and the user-authorized public proxy exposes these functions. Reports are atomic JSON artifacts in `information-reports/` beside the configured SQLite file; no schema migration occurs. Releases/rollbacks retain these files. An uncertain POST must be reconciled by reading the list before retrying. List reads bypass proxy cache. Formal report APIs and all 21 prediction qualification gates are unchanged.

### Source quality correction (2026-09-10)

`GET /api/v1/intelligence/brief` applies `source-quality.2026-09-10.v1` to the stored selection without rewriting archival rows. Rejected stale/undated/irrelevant/unverified-publisher selections are excluded from the displayed events, facts and impacts; the envelope uses `presentation_status=redacted` and `redactions[].reason_code=source_quality_review`. `payload_sha256` identifies the original frozen archive, not the filtered JSON presentation. Coverage `source_ids` now lists actual cited A/B sources, not configured capabilities.

`GET /api/v1/intelligence/sources` joins completed source runs as of the cursor snapshot. `quality_status` distinguishes `ok`, `unchanged`, `no_relevant_items`, `error`, `partial_error`, `timeout`, `overdue`, `not_observed`, and `historical_only`. Successful reading does not imply independent relevant articles. Current formal source eligibility excludes soft-removed sources. `/prices/intraday` provides a separate same-basis reference history; no futures-to-spot stitching is authorized.

### 2026-09-10 出版日期精度补充
工业 ItemRevision/ItemDetail/Claim 增加可空 published_date（YYYY-MM-DD），与带时区 published_at 分开；不把日粒度补成午夜。投影parser和新增payload manifest使用v2-date-precision，旧payload及其hash保留；旧调用方可忽略新增字段。每日准入对日期粒度按业务日期窗口检查，同时要求visible_at不晚于截止；这不是精确小时级新鲜度证明。

### 日报当前展示状态（2026-09-11）

`GET /api/v1/intelligence/brief` 的 `brief.status` 与本次返回的已筛选事件、事实及领域覆盖一致。历史日报在当前质量检查后不足两个领域时返回 `blocked`，并追加 `displayed_evidence_insufficient` 缺口。冻结记录及 `payload_sha256` 保持原样；该哈希标识归档内容，不是当前展示结果的哈希。此行为不修改数据库或历史日报。

### Market analysis report presentation (2026-09-19)

`POST /api/v1/information-reports` keeps the existing request/response and immutable
report hash/download contract. Newly generated reports lead with an evidence-bound
market conclusion, seven-product impact judgments, drivers, counterevidence,
watch conditions and conditional scenarios. Opposing directional evidence stays
mixed; missing direction is not inferred from a single quote. Only non-retracted
events with facts and usable source URLs support conclusions. Snapshot metadata,
price benchmarks and source details follow as appendices. Existing report files
remain unchanged; the UI folds appendices without changing downloadable content.
No formal forecast eligibility is granted by this report generation path.

`GET /api/v1/intelligence/map` reports `window_hours` (`"48"`), `candidate_count` and
`mapped_count` as strings within the existing `applied_filters` map.
`candidate_count` is the number of candidates read under the existing
time/category/product query and candidate cap; `mapped_count` is the number of
trusted-location features within the
requested bounding box. Neither field changes the 48-hour source-time rule or
promotes an unvalidated summary's location. `truncated` remains authoritative when
the candidate cap is hit.

### 2026-09-19 前端审计修复口径

- `/intelligence/search` 在原始 FTS 之外匹配校验通过的中文概览；命中仍绑定原事件，
  按业务对象去重、使用同一快照游标分页。中文概览生成时间晚于查询快照时不参与该页。
  搜索读取不产生模型调用、投影写入或正文改写。
- 来源目录补充现有盘中价格采集通道（`source_type=price_channel`）。历史价格存在但
  无采集审计时返回 `observations_without_run`，不伪造成功时间。之后的真实采集才记运行结果。
- 运行起止时间保留亚秒精度；历史零毫秒不作为“无耗时”证据。
- 观察判断与信息报告使用同来源、同单位、同序列的价格窗口，列示起止日期及证据。
  该适配不改变正式预测资格，也不将已经冻结的报告重新写成当前行情。
# 主预测权威与向后兼容（2026-09-26）

`GET /api/v1/forecasts/seven-product` 不带 `as_of_time` 时读取已发行的唯一主批次。
首页使用同批次 POY、DTY 的 1 天格，不把两种价格预测合成为成本压力预测；分歧显示为分化。
报告保存同批次 21 格；助手上下文带批次及截止时间，预测不能充当市场事实证据。
旧 `/predictions/model-signal` 保留兼容，但不再作为当前首页输入。

每格新增可选 `forecast_contract`（旧记录默认为 `observation-horizon.v1`）、`target_date`、
`input_snapshot_sha256`、`confidence_kind`、`candidate_status`、`candidate_error` 和 `candidates`。
新日度批次为 `issue-calendar.v1`，N 天从发报输入截止所在上海自然日计算。
旧批次与旧成绩保持原口径。新合同不继承旧合同的正式资格或概率校准。
`confidence_kind=heuristic_score` 是模型参考评分，不是正确率。
内部候选绑定相同输入，使用主格冻结中性带和主格唯一实际结果；不提供另一份对外预测。

## Business evidence dossier

`GET /api/v1/forecasts/seven-product/evidence` is read-only. Query: `target=crude|naphtha|px|pta|meg|poy|dty`, `horizon_days=1|7|30`, `view=issued|current` (default issued), `offset=0` (0–10000), `limit=50` (1–100). An optional 64-hex `input_sha256` pins pagination; an expired or evicted pin returns 422 with `error.code=evidence_view_changed_reload_first_page` and the caller restarts from page 1 of the new snapshot. `refresh=true` (only with `view=current`, no context) forces a new capture instead of the cached snapshot; with any context or `view=issued` it returns 422 `refresh_only_for_current_product_view`. The current preview keeps a 60-second fresh cache; an expired snapshot is served immediately with `revalidating=true` while one shared background rebuild runs, and concurrent cold reads share a single in-flight build per key (the join is deadline-bounded). Up to 3 immutable snapshot objects stay in memory for 10 minutes to preserve pagination across refreshes. The evidence endpoint bypasses the public proxy stale-response cache. It never calls a model, persists an input or reissues a prediction. Source capture uses the existing bounded raw/summary and price contracts.

`issued` verifies the latest immutable main batch binding and its content-addressed input archive, rebuilding the dossier. Pre-dossier batches return `legacy_input`; absent history returns `unavailable`, not a fabricated empty success. `current` reads present sources with a fresh cutoff and no batch identity; it does not change the issued forecast. `capture_failed` explicitly means source capture failed; `available_with_gaps` means the dossier exists, not that every information requirement is covered. Malformed input/archive, contention or read failure returns sanitized503. Invalid query values return422.

The response separates four evidence families, mixed signals, planned/unconfirmed/in-progress/unknown material, and retrospective neutral/unmatured outcomes. Each exact quote has source, date and review status. A/B source rank and matching hashes do not prove truth. The explicit hypothesis is price-up pressure under otherwise equal conditions; contrary drivers are not automatically factual denials. Historical reaction is explanatory context, not causal proof or an old issued forecast. Prices remain a separate baseline and never count as independent event votes. Material lists are paginated together; episode counts and mechanism coverage refer to the whole dossier, while relation IDs/history rows reference the returned page only. Full original bodies, provider payloads and private paths are not returned.

Publication of this business dossier does **not** require demonstrated forecast accuracy improvement. `model_effect=context_only` remains explicit; numerical model promotion is a separate contract.

### Current-only semantic conditions (2026-10-03)
`EvidenceDossierResponse.semantic_reviews` defaults to an empty array. Only the current, product-wide view may expose operator-generated cached reviews; issued, event, report, answer and batch scopes never import later analysis. API reads never invoke a model. Every row is source-bound and has `counts_as_evidence=false`: it explains conditional directional pressure, not verified real-world causality, forecast direction or a vote. Downstream rows describe conditional upstream transmission, not new independent events.

Rows retain literal quotes, source identity/hash, publisher time, first-known time, review time, mechanism, physical driver/change, direction, conditions and time kind. An announced decision is not completed execution; current-state reports do not invent an occurrence date. `report_period` may represent a completed month or publisher-calendar week through publication day. Optional `binding_method=ai_coreference` adds the literal connecting span, critic explanation and model; a separate reviewed, sealed association is required. Absence/corruption of the private cache yields no semantic rows and does not change deterministic evidence or historical snapshots. The operator worker shares a cumulative 350 RMB task ceiling, reserves before each HTTP attempt (including failures), and has no automatic daily paid schedule.

### Context-bound evidence consumers (2026-09-28)
The same `GET /api/v1/forecasts/seven-product/evidence` accepts at most one context:
`event_id` (optional exact `event_revision_id`), `report_id`, `context_pack_id`, or `batch_id`.
No context preserves existing product behavior. Context selectors are optional/additive,
1–120 chars; combinations and an orphan revision return 422, unknown contexts 404,
failed capture/integrity 503, expired page pins 422 `evidence_view_changed_reload_first_page`.
`horizon_days` remains 1/7/30.
Response adds `scope`, `context_id`, `context_revision`, `scope_note`, and
`matched_source_claims`; product responses have safe defaults.

Event scope binds source URLs and the stored event revision cutoff. Only exact source
material is direct evidence; historical analogues additionally require same subject,
mechanism, location, expected direction, and an earlier event date. Analogue selection
does not depend on outcome sign. Its own later reaction is not its historical analogue.
Fact-denial links remain in the event's existing `counterevidence` contract; dossier
counter-drivers are separate from factual denials. No product-wide evidence is silently
represented as proving one event. A title-only/C-tier record may honestly have zero
qualified dossier materials while retaining its original source links.

New information reports freeze the projection in their existing hash-checked JSON record
and render a business-evidence section in both content and download. Existing reports
are never rewritten. Assistant context packs freeze a source- and exact-quote-bound
projection in metadata, and include analysis classifications in their existing model
context, without adding model calls or bypassing RAG citation gates. The chat response's
existing `context_pack_id` connects UI inspection to that exact answer input.
Reading either old record returns `legacy_input` when no projection was stored; it never
substitutes today's materials. Capture failure is explicit, not a healthy empty dossier.

`batch_id` reads the specified audited forecast batch and immutable input archive,
never the latest batch by accident. Product selectors and all three horizons use the
same underlying archive. Current market dossiers remain separate from issued forecasts.
Scoped page pins hash the scoped content, not only the broader source capture.
All dossier reads are read-only; report/answer snapshots are written only by their
already-existing generation flows. No schema, schedule, prediction or model gate changes.


### Pipeline implementation inspection

`GET /api/v1/pipeline/nodes/{node_id}` may return `implementation_profile`
for political_analysis, historical_analog, product_synthesis and skeptic_review.
Fields: basis/source, prompt_version/system_prompt/prompt_sha256,
configured_model/model_source, stage_cap, context/tools/output/fallback,
historical_request. This read-only block describes current deployed code and
configured model, not the actual parameters or context of a historical call.
Other nodes omit it; clients must retain the unavailable state. Existing graph
identities, prediction semantics and budgets are unchanged. No credentials,
provider URL or historical user request is included.


### 消费者冻结语义复核（2026-10-04，待发布）

新报告/回答/事件消费者内部冻结包 `consumer-evidence.v2` 包含按品种分组的 `semantic_reviews`，与档案一起进入内容哈希。证据接口仍使用现有 `EvidenceDossierResponse.semantic_reviews` 字段：对象范围返回其冻结列表，旧v1对象返回空列表，不读取当前复核缓存来补写历史。来源范围按URL绑定；有输入正文时还要求引句包含在该正文。复核可解释条件压力但 `counts_as_evidence=false`，不改变发牌、直接正反计数或计票。当前品种范围保持按当前快照时刻读取缓存。无新端点及响应形状变更。

当前档案核验版本升级为 `business-evidence-dossier.v5-sentence-boundaries`：英文单句限定词按原文句界绑定，邻句预测不污染已报告事实。冻结v4/v3/v2/v1继续按原规则重建，新增核验不补写旧发行。该修正只影响解释档案，价格特征及发牌规则不变。

雷达事件详情 `EventDetail` 新增可选 `semantic_reviews` 与 `semantic_review_as_of`（契约修订v1.2-source-review）：仅最新详情返回当前缓存中与该事件来源URL及品种匹配的条件复核。当前复核不属于原事件修订、不进入原工件哈希、不能当历史发牌依据；修订历史仍返回空列表。过期/撤权来源及已撤回或脱敏事件不补入复核。读取不发模型请求，失败不得伪造成功或改写记录。

语义条件材料的报告期与发生单日分开显示：明确周结束日按七日区间核验，月内报告按来源时刻的已覆盖区间核验；周/月报告有发布滞后上限，来源新鲜度仍按发布时点检查。战略储备减少不套用商业库存方向，进口作为需求代理保留消费/补库条件。报告中同一原文解释只列一次，各品种通过条件依据编号引用，不把投影数量当独立证据数量。

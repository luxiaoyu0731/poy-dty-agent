# Observability

This plan is scoped for a single-researcher deployment. It favors lightweight
health checks, metrics, logs, and manual review over team paging or on-call
processes.

## Signals

Metrics are exposed at `GET /metrics` in Prometheus text format.

Core metrics:

- `poy_dty_http_requests_total{method,route,status}`
- `poy_dty_http_request_duration_seconds_bucket{method,route,status,le}`
- `poy_dty_llm_calls_total{provider,model,status}`
- `poy_dty_llm_latency_seconds_bucket{provider,model,status,le}`
- `poy_dty_llm_tokens_est_total{provider,model,type}`
- `poy_dty_source_fetch_total{source_id,status}`
- `poy_dty_source_freshness_seconds{source_id}`
- `poy_dty_scheduler_last_success_unixtime{scheduler}`
- `poy_dty_scheduler_last_attempt_unixtime{scheduler}`
- `poy_dty_scheduler_last_attempt_status{scheduler,status}`
- `poy_dty_scheduler_failures_total{scheduler,status}`
- `poy_dty_scheduler_last_duration_seconds{scheduler}`
- `poy_dty_scheduler_backlog{scheduler}`
- `poy_dty_rate_limited_total{bucket}`

Every response includes `X-Request-ID`. If the client provides `X-Request-ID`, the server preserves it and writes it to structured logs.

## Health Checks

- `GET /api/v1/health/live`: process liveness.
- `GET /api/v1/health/ready`: readiness for source registry, storage, LLM mode, and internal auth mode.
- `GET /api/v1/health/deep`: dependency inventory for release checks and troubleshooting.

## SLO Draft

| Area | SLI | Local/Staging Target | Personal Production Target |
| --- | --- | --- | --- |
| API availability | non-5xx requests / total requests | 99.0% | 99.9% |
| API latency | p95 HTTP latency | < 800 ms | < 500 ms |
| Assistant latency | p95 LLM latency including fallback | < 15 s | < 10 s |
| Assistant fallback | fallback calls / total LLM calls | visible, investigated | < 5% when the provider is expected to be available |
| Source freshness | sources inside SLA / enabled sources | 90% | 98% |

## Alert Rules

Optional Prometheus rule sketch for self-hosted monitoring:

```yaml
- alert: PoyDtyApiHighErrorRate
  expr: sum(rate(poy_dty_http_requests_total{status=~"5.."}[5m])) / sum(rate(poy_dty_http_requests_total[5m])) > 0.01
  for: 5m
  labels: { severity: critical }
  annotations:
    summary: "POY/DTY API 5xx rate is above 1%"

- alert: PoyDtyApiHighLatency
  expr: histogram_quantile(0.95, sum by (le) (rate(poy_dty_http_request_duration_seconds_bucket[5m]))) > 0.8
  for: 10m
  labels: { severity: warning }
  annotations:
    summary: "POY/DTY API p95 latency is high"

- alert: PoyDtyLlmFallbackSpike
  expr: sum(rate(poy_dty_llm_calls_total{status="fallback"}[10m])) > 0.1
  for: 10m
  labels: { severity: warning }
  annotations:
    summary: "LLM fallback mode is active"

- alert: PoyDtySourceStale
  expr: max by (source_id) (poy_dty_source_freshness_seconds) > 3600
  for: 15m
  labels: { severity: warning, owner: single-researcher }
  annotations:
    summary: "A source is older than its one-hour operating threshold"
    runbook: "docs/runbook.md#daily--weekly-data-update"

- alert: PoyDtySchedulerMissed
  expr: time() - poy_dty_scheduler_last_success_unixtime > 90000
  for: 15m
  labels: { severity: critical, owner: single-researcher }
  annotations:
    summary: "A scheduler has not completed successfully in 25 hours"
    runbook: "docs/runbook.md#daily--weekly-data-update"

- alert: PoyDtySchedulerBacklog
  expr: poy_dty_scheduler_backlog > 0
  for: 30m
  labels: { severity: warning, owner: single-researcher }
  annotations:
    summary: "A scheduler backlog has remained non-zero"
    runbook: "docs/runbook.md#daily--weekly-data-update"
```

The deployment owner is the single researcher operating this installation.
They own acknowledgement, diagnosis, and disabling affected write jobs. Alert
threshold changes require dated release evidence or explicit risk acceptance.
Each scheduler integration must update the attempt/status/duration gauges after
every attempt, including failures. A failed attempt increments the bounded
failure counter and must not advance `last_success`. Absence of a series is
missing telemetry, not a successful run. The macOS public runtime also runs a
credential-free HTTPS probe every five minutes and writes its atomically
replaced state to `shared/public-health/latest.json`; without remote alert
credentials, only a local macOS notification is emitted on a healthy-to-failed
transition.

The macOS backend imports the atomically replaced status documents for
`local_daily` and `news_scheduler` on every scrape. Each document carries a
bounded `scheduler_observability.v1` projection so attempt time, last successful
completion, duration, backlog, and cumulative failure counts survive backend
restarts. `ready`, `ready_with_warnings`, `success`, and `completed` advance
`last_success`; `degraded`, `blocked`, and other unsuccessful states do not.
Missing or malformed documents produce no new series and therefore remain
detectable as missing or stale telemetry. Scraping never increments a counter.

The local daily status also embeds `seven_product_oos_evaluation`. This projection
records the content-addressed evidence path/hash, issued-versus-preview forecast
identity, the forecast/evaluation cutoff identity, 21-cell pass count, evaluator exit code, and proof that the database was
unchanged during evaluation. Database identity is `sqlite-state-fingerprint.v1`: main, WAL, and rollback journal
content are covered, while the non-persistent SHM lock/index cache is excluded. A healthy exit code 2 is intentionally distinct from
an evaluator failure: it means the frozen formal gate ran and remains below 21/21.
Stale, malformed, non-ledger apply evidence, a mixed forecast/evaluation cutoff, or any database mutation blocks the
daily status and therefore appears through the existing scheduler backlog/attempt
metrics and alert path.

## Dashboard Panels

- Request rate by route and status.
- HTTP p50/p95/p99 latency.
- 4xx/5xx error rate.
- LLM latency and fallback rate.
- Estimated LLM prompt/completion tokens.
- Source fetch status by source id.
- Source freshness by source id.
- Scheduler last success, last duration, and backlog.
- Rate limited requests by bucket.

## Troubleshooting

1. Check `/api/v1/health/ready` and `/api/v1/health/deep`.
2. Search structured logs by `request_id`.
3. Check source fetch status and freshness.
4. Check whether LLM is in fallback mode.
5. Disable source fetch jobs before retrying failed ingestion.
6. Keep fallback answers enabled during provider outages.

## Industrial Intelligence Metrics (schema v37, Local Implementation Complete)

`/metrics` additionally renders the bounded `poy_dty_intelligence_*` families
(labels are frozen enums or declared source ids; never URLs, titles, or item
IDs):

- `poy_dty_intelligence_runs_total{run_type,provider,status}`
- `poy_dty_intelligence_run_duration_seconds{run_type,provider,status}`
- `poy_dty_intelligence_items_total{provider,result}`
- `poy_dty_intelligence_events_total{result}`
- `poy_dty_intelligence_projection_lag_seconds{provider}`
- `poy_dty_intelligence_brief_last_success_unixtime`
- `poy_dty_intelligence_brief_status{status}`
- `poy_dty_intelligence_brief_event_count`
- `poy_dty_intelligence_rights_blocked_total{provider,reason}`
- `poy_dty_intelligence_source_drift{provider}`
- `poy_dty_intelligence_feedback_total{action}`
- `poy_dty_intelligence_map_features_returned_bucket{bucket}`

`GET /api/v1/health/deep` adds an `intelligence` section (enabled flag, run
totals, brief status counts, brief last-success time, rights-blocked and
source-drift counters) while `INDUSTRIAL_INTELLIGENCE_ENABLED=1`. Runbook
notes: `blocked` is an honest terminal state, not a success signal; failed
runs append terminal audit rows in `intelligence_runs` and never advance the
brief last-success gauge.

The API process reconstructs durable counters from the v37 append-only
`intelligence_runs`, item, event, brief and feedback tables on every scrape.
This preserves provider/CLI activity across process boundaries and backend
restarts; process-local counters are only used when no v37 database is
available. Labels remain bounded to frozen enums and declared source IDs.

### Source continuity probe (2026-09-10)

In public mode the existing five-minute public probe also reads `/api/v1/news/fetch-runs?limit=100`. A majority failure/stall (fewer than 80% recent completed reads, or fewer than 10 observed sources) fails the probe even when live/ready are HTTP 200. Recent means two hours, including weekends because the configured news feeds are continuous. No-relevant-items is a completed read, not article production. This triggers only the existing failure-transition notification, never collection or business writes. Per-source failures remain visible even below the global threshold. Multi-day availability is not established by a passing probe.

## Reliability acceptance sampling (2026-09-14)

`server/scripts/capture_reliability_acceptance.py --runtime-root <runtime> --output-dir <evidence>`
performs bounded read-only HTTP and SQLite queries, retaining a separate timestamped JSON sample per run.
Use the release-gate user agent to avoid stale proxy snapshots. Samples include the live/ready/index/brief
responses, worker and scheduler receipts, cumulative HTTP attempts, failed task IDs, retained index versions,
and hourly source-fetch audit counts. Do not infer continuous uptime from isolated successful samples.

Transient summary failures may resume after at least 15 minutes, targeting the exact failed article IDs,
up to six lifetime attempts for that content/model/prompt version. Authentication failures and rejected
source content do not enter this recovery lane. Counts are never cleared to force retries; the normal
per-batch provider-attempt budget and concurrency limits remain effective.

# Runbook

## Local Startup

### Persistent macOS services (recommended)

Use the checked-in service manager instead of starting Vite/Uvicorn in a temporary terminal:

```bash
scripts/manage_local_services.sh install
scripts/manage_local_services.sh status
```

The install command builds the frontend, installs two per-user LaunchAgents, starts them immediately, and verifies
`http://127.0.0.1:5173/` plus the backend liveness endpoint. They start again at login and launchd restarts either
process after an unexpected exit. Logs persist under `.codex-run/local-services/logs/`.

```bash
scripts/manage_local_services.sh restart
scripts/manage_local_services.sh logs
scripts/manage_local_services.sh uninstall
```

Re-run `install` after code changes that affect the production frontend bundle. Backend Python changes take effect after
`restart`. Installation is idempotent. `.env.local-production` is optional for the basic local service; if present, it
is loaded by both processes and must contain real deployment values rather than placeholders.

macOS protects Desktop, Documents, and Downloads with privacy controls. If service logs contain `Operation not permitted`
for a project stored in one of those folders, launchd cannot bypass that consent. In System Settings, open **Privacy &
Security → Full Disk Access**, press **+**, use `Command-Shift-G` to enter `/bin/bash`, and enable it; then run `install`
again. Moving the repository to a non-protected development directory is the alternative. The manager reports this
condition explicitly instead of treating it as an application crash.

Frontend:

```bash
npm install
npm run dev
```

Backend:

```bash
cd server
uv sync --all-groups
uv run uvicorn app.main:app --reload --port 8000
```

LLM:

```bash
cp .env.example .env
# Fill only this value to enable real LLM answers:
# DEEPSEEK_API_KEY=...
```

## Verification

```bash
npm run check
cd server && uv run ruff check .
cd server && uv run pytest tests
# Playwright starts its own backend (port 18000, seeded DB) and Vite dev server
# (port 15173); starting a backend on 127.0.0.1:8000 first is only needed with
# PLAYWRIGHT_REUSE_EXISTING=true.
npm run test:e2e
npm audit --registry=https://registry.npmjs.org --omit=dev
cd server && uv run pip-audit
npm run eval:ai
```

Monthly maintenance cadence (pick a fixed day each month):

1. `brew upgrade cloudflared node python@3.14` and reboot-free `launchctl kickstart -k` the tunnel job.
2. `npm audit` / `cd server && uv run pip-audit` even if CI has been green.
3. Review `shared/logs/*.log` sizes and `shared/local-production/source-automation/db-backups/` growth.
4. Confirm `df -h` free space exceeds the preflight disk threshold (default 5 GB).

Backend foundation spot checks:

```bash
cd server
uv run pytest tests/test_backend_foundation.py
sqlite3 "${SQLITE_PATH:-data/agent.db}" 'PRAGMA user_version; SELECT version,name,applied_at FROM schema_migrations ORDER BY version;'
```

## Health Checks

- Liveness: `GET /api/v1/health/live`
- Readiness: `GET /api/v1/health/ready`
- Deep health: `GET /api/v1/health/deep`
- Delivery status: `GET /api/v1/delivery/status`
- Metrics: `GET /metrics`

### Agent 08:10 cross-restart evidence gate

Do not restart a deployed service merely to exercise this check. After a
separate maintenance approval, freeze one completed 08:10 Asia/Shanghai window
and capture the backend process identity immediately before and after the
approved restart. Execute the checkpoint collector inside each process so it
captures PID/PPID, the resolved Python executable and Hash, process start time
and observation time directly. Pass both checkpoints and the explicit
requested/completed restart boundary through
`agent_governance_runtime_evidence.py`.

The local result requires a different PID and runtime instance, the same
executable Hash, the same immutable single report row, an unchanged review
queue snapshot and a valid interval with the second process starting after the
recorded completion boundary. The unsigned result is only
`unsigned_snapshot_comparison`: it cannot prove exactly-once execution or rule
out a review row inserted and deleted between snapshots. A signed outer
launchd/service-manager event and an append-only review event ledger are still
required for production acceptance. Missing windows, changed rows or process
ambiguity fail closed. Store only bounded evidence; do not export Agent traces,
prompts, free text, credentials or database paths.

## CCF Soft Removal

CCF is a historical, read-only source as of 2026-08-30. Existing rows and old
experiment contracts are retained for reproducibility, but current capture,
imports, acquisition queues, schedulers, freshness/readiness gates, alerts and
`public-benchmark.v2` exclude CCF. Do not run the legacy CCF scripts. Re-enabling
the channel requires a new ADR and an explicit operator decision.

## Daily / Weekly Data Update

Local production preflight:

```bash
npm run local:preflight
```

This read-only check verifies required files, commands, package scripts, SQLite integrity, generated local production
artifacts, service health, and production env template coverage. It does not write business data and is safe to run
before enabling local unattended operation.

Local production daily bundle:

```bash
npm run local:daily
```

This command is the local unattended-readiness entry point. It runs the source automation plan, public-source refresh
dry-run, news/event refresh dry-run, quality gate, backup integrity probe, local health
checks, and writes reports to `.codex-run/local-production/`. It does not write business data by default.

Apply writes only after confirming the local run is allowed to update SQLite:

```bash
npm run local:daily -- --apply
```

The apply mode delegates database writes to scripts that require a backup before updating the main database. If the final
gate report is `NO-GO`, fix the listed blockers before relying on the daily output for customer-facing wording.
In apply mode, the same bundle also runs the configured public news/event sources. Successful items are normalized into
`news_articles`, `news_event_clusters`, and eligible `event_observations`; source-specific failures such as browser
checks, rate limits, or official-site errors are recorded in the automation report instead of stopping the entire run.
After source refresh, it settles mature previously issued seven-product cells and
then freezes exactly one immutable 21-cell batch for the Shanghai business date.
Its report is `.codex-run/local-production/seven-product-lifecycle/seven-product-lifecycle-latest.json`.
A settlement provenance failure blocks the daily lifecycle; a 0/21 promotion
state remains an explicit warning and does not prevent honest reference issuance.
Immediately afterward, the same bundle runs the read-only OOS evaluator and writes
`.codex-run/local-production/seven-product-evaluation/seven-product-evaluation-latest.json`.
Apply mode first takes a consistent post-issue SQLite online-backup snapshot under
`evaluation-input/` (0700 directory, 0600 file, exactly one retained) and evaluates
the immutable issued batch against that snapshot, so concurrent scheduler writes to
the live database cannot invalidate the run's input fingerprint; the readiness gate
requires `evaluation_input_mode=consistent_snapshot_after_issue`. Dry-run mode labels
its forecast as `point_in_time_preview`. Exit code 2 plus a
fresh, hash-valid 21-cell artifact means “evaluation ran but one or more cells did
not pass” and is a warning. A crash, stale artifact, hash/contract mismatch,
database mutation, or preview artifact in apply mode blocks daily readiness.

### Seven-product formal champion proposal

Do not run promotion merely because the evaluator produced 21 rows. First require
the exact approved cells to show `promotion_eligible=true` under the frozen
5%/55%/20-sample gate. Generate a read-only evidence package, then create a proposal:

```bash
server/.venv/bin/python server/scripts/run_seven_product_evaluation.py \
  --db /path/to/read-only-evaluation.db \
  --output-dir .codex-run/seven-product-evaluation

server/.venv/bin/python server/scripts/run_seven_product_model_governance.py promote \
  --evaluation-evidence .codex-run/seven-product-evaluation/seven-product-evaluation-latest.json \
  --model-version robust-drift-reference.v1 \
  --cell crude:1 \
  --actor operator \
  --reason "frozen per-cell OOS gate passed" \
  --approve
```

Repeat `--cell` for each independently accepted cell. The command revalidates the
evidence and writes a content-addressed proposal plus an exact candidate registry;
it never edits the active registry. Review that proposal, then commit and deploy the
exact registry in a new immutable release. To propose a recorded rollback without
editing the active release:

```bash
server/.venv/bin/python server/scripts/run_seven_product_model_governance.py rollback \
  --registry /path/to/current/seven_product_registry.json \
  --cell crude:1 \
  --actor operator \
  --reason "runtime performance regression" \
  --approve
```

Formal champions take precedence over reference champions. A missing formal feature
or three consecutive valid losses to persistence causes a non-formal runtime fallback
and must be reviewed; invalidated outcomes do not count toward the streak.

### Public-source and user-file cadence

- OPEC press releases continue through the existing 30-minute news scheduler. That same short-cadence process now calls
  the due-aware public-source loop, so OFAC is actually checked every 30 minutes while slower sources remain gated by
  their own policy frequency. If the OPEC index returns a browser check, a public Google News RSS query is used only for
  discovery; every item must resolve to an allowlisted OPEC `/pr-detail/` page before ingestion. Unresolved discovery
  wrappers are discarded. OFAC's private state file contains only snapshot hashes, counts and per-entity fingerprints;
  only energy/shipping add, remove or modify deltas enter `event_observations`.
- GACC and UN Comtrade are checked daily but retain their monthly observation dates. Monthly publication lag does not
  become a false daily-freshness failure.
- EIA Brent is checked every three hours, while the daily observations are released as a weekly batch. Forecast
  freshness therefore uses the same ten-calendar-day observation SLA as source automation: a newly released official
  batch is usable, but day 11 without a newer observation is still explicitly stale. Do not replace it with WTI or a
  futures proxy.
- CZCE PTA/PX/methanol official daily files are checked by the structured-source loop with a bounded 10-day lookback. The
  projection table is upserted for current reads while each distinct official file is preserved as an append-only
  capture revision. A one-time historical backfill never counts as historical point-in-time OOS evidence.
- DCE MEG is soft-removed. Do not schedule, fetch, import, alert on, or use it for current qualification. Its historical
  rows/revisions keep the frozen DCE historical identity; never relabel them as SunSirs. The current MEG label is the
  SunSirs China EG spot assessment captured by the public-price scheduler into a dedicated append-only series. Use
  first successful capture as visibility, preserve the 2013-methodology applicability caveat, and do not substitute
  Sina/Eastmoney futures or a browser/WAF bypass. DCE Keychain credentials remain untouched unless separately deleted.
- TNC POY/DTY public history is included in the daily structured-source loop. Its archived rows keep the first local
  capture time; a one-time archive backfill is not accepted as historical point-in-time OOS evidence.
- UN Comtrade uses `UN_COMTRADE_API_KEY` when present and falls back to the official preview endpoint when the free API
  is unavailable or rate-limited. Do not log the key or a URL containing it.
- `.codex-run/user-files/` is scanned daily. Standard CSV/XLSX templates are imported idempotently; PDF/images go to
  `.codex-run/user-files/review/` with JSON reports under the run output. An empty inbox is healthy.
- Per-source operational state is kept under `.codex-run/source-state/`. Remove only the affected source JSON to
  re-baseline that source; for OFAC this intentionally suppresses deltas on the next successful baseline run.

Run a focused dry-run without database writes:

```bash
server/.venv/bin/python server/scripts/run_source_automation.py \
  --fetch-public --fetch-news --import-user-files --run-quality
```

Apply only with the built-in online backup guard:

```bash
server/.venv/bin/python server/scripts/run_source_automation.py \
  --apply --backup-db --fetch-public --fetch-news --import-user-files --run-quality
```

Run the frozen 15-scenario release fault matrix without touching the production database:

```bash
server/.venv/bin/python server/scripts/run_release_fault_matrix.py
```

The runner creates a fresh DG01 SQLite root under `/private/tmp` and writes a JUnit file plus a content-addressed JSON
report under `.codex-run/release-fault-matrix/`. A 15/15 E2 result is required but does not replace the first real
production-run E1 evidence.

After an approved public deployment, run the authenticated smoke in `docs/public-production-macos.md`. It validates
the forecast, every returned immutable history batch, evaluation, and model-registry grid in addition to generic
health, and writes immutable evidence under `$RUNTIME/shared/reports/public-smoke/`. Invalid origin, missing credential
source, anonymous-boundary and login failures also leave sanitized stage-coded evidence. A missing history batch,
duplicate/missing cell, inconsistent identity/count/status, automatic model promotion, or evidence-address collision
is a release failure. The five-minute anonymous public health job performs a bounded startup retry window and writes
its latest status with owner-only crash-durable atomic replacement; inspect `shared/public-health/latest.json` for all
attempts when it remains failed.

Inventory production SQLite file and sidecar permissions without changing production:

```bash
npm run ops:sqlite-permissions -- \
  --root "$HOME/Library/Application Support/POY-DTY-Agent/shared" \
  --output-dir .codex-run/sqlite-permission-migration
```

Exit code 2 means owner-only hardening is required. The content-addressed report records path, prior mode, size and
SHA-256 for every `.db`/`.sqlite`/`.sqlite3` file and `-wal`/`-shm`/`-journal` sidecar. It rejects symlinks and owner
mismatches. `--apply` only changes containing directories to 0700 and artifacts to 0600; it never deletes a file and
rechecks device/inode/owner immediately before `chmod`. Treat `--apply` as a production migration: obtain the separate
authorization, preserve the dry-run report, then rerun without `--apply` and require status `clean`.

Generate the local alert digest after any daily run:

```bash
npm run local:alerts
```

To send the same alert digest to personal WeChat, use a bridge provider outside git before running the alert command:

```bash
export ALERT_PUSH_PROVIDER=serverchan
export SERVERCHAN_SENDKEY='SCT...'
npm run local:alerts

# Alternative personal WeChat bridge:
export ALERT_PUSH_PROVIDER=pushplus
export PUSHPLUS_TOKEN='...'
npm run local:alerts
```

Enterprise WeChat compatible webhooks are still supported with `ALERT_PUSH_PROVIDER=wechat_webhook` and
`WECHAT_WEBHOOK_URL`. Tokens, sendkeys, and webhook URLs are secrets. Do not paste them into reports, screenshots, code,
tests, or committed env files.

One-command local production bundles:

```bash
npm run local:prod:dry-run
npm run local:prod:apply
```

`local:prod:dry-run` runs preflight, local daily dry-run, and alerts. `local:prod:apply` requires local backend/frontend
services to be reachable, then runs backed-up data updates and alerts. Review generated reports before loading launchd
jobs or using customer-facing wording.

Alert outputs:

- `.codex-run/local-production/alerts/latest-alerts.json`
- `.codex-run/local-production/alerts/latest-alerts.md`

Local production helper reports and launchd templates are generated under `.codex-run/local-production/`:

- `09-final-local-unattended-production-report.md`
- `LOCAL_PRODUCTION_README.md`
- `launchd/com.poydty.agent.backend.plist`
- `launchd/com.poydty.agent.frontend.plist`
- `launchd/com.poydty.agent.local-daily.plist`

Review the launchd templates before loading them. The checked-in helper scripts install and optionally load them:

```bash
cp .env.local-production.example .env.local-production
# Fill INTERNAL_API_TOKEN and alert provider keys.
scripts/install_launchd_local_production.sh
scripts/install_launchd_local_production.sh --load

# Roll back local launchd services:
scripts/uninstall_launchd_local_production.sh
```

The generated launchd jobs load `.env.local-production`, use production-like environment defaults, write logs under
`.codex-run/local-production/logs/`, serve the frontend with `npm run build && npm run preview`, and run the daily job
at 18:00.

Public delivery handoff is tracked in `docs/public-delivery-handoff.md`. It separates repository work from user-owned
domain, hosting, TLS, authentication, public-source technical boundaries, secret-management, backup-target, alert-channel, and SLA decisions.

One-command automation status check:

```bash
npm run source:automation
```

By default this is read-only and includes the configured news/event source plan. Review
`.codex-run/source-automation/source-automation-latest.json` for `news_fetch` before enabling backed-up writes.
With `--fetch-public`, the same run also refreshes the eight public price inputs
(Brent, WTI, Naphtha, PX, PTA, MEG, POY and DTY) plus the scheduled official
sources including CFETS USD/CNY. Dry-run fetches and validates these values
without storing them; apply mode stores them only after the unified SQLite
backup succeeds.

Apply writes only after confirming the run should update local state. This command backs up SQLite first, records the
current public acquisition queue and runs the delivery quality gate:

```bash
python3 server/scripts/run_source_automation.py \
  --apply \
  --backup-db \
  --record-plan \
  --run-quality \
  --output-dir .codex-run/source-automation
```

Fetch configured public sources as part of the same run:

```bash
python3 server/scripts/run_source_automation.py \
  --apply \
  --backup-db \
  --record-plan \
  --fetch-public \
  --run-quality \
  --output-dir .codex-run/source-automation
```

Fetch configured news/event sources as part of the same run:

```bash
python3 server/scripts/run_source_automation.py \
  --apply \
  --backup-db \
  --record-plan \
  --fetch-news \
  --news-limit-per-source 20 \
  --news-cursor-pages 3 \
  --run-quality \
  --output-dir .codex-run/source-automation
```

Daily business-day checklist:

1. Run `npm run source:automation` and review `.codex-run/source-automation/source-automation-latest.json`.
2. Refresh public sources when API keys are configured.
3. Refresh news/event sources and review per-source errors in `source-automation-latest.json`.
4. Run the delivery data quality gate through the automation script.
5. Confirm the lifecycle report froze exactly one 21-cell batch and did not report settlement blockers.
6. Confirm `GET /api/v1/forecasts/seven-product` returns that frozen batch and reports the honest formal count.
7. Confirm `GET /api/v1/forecasts/seven-product/history` exposes the same batch, settles by natural-day maturity, and does not count any `invalidated_contract_mismatch` outcome.
8. Confirm `GET /api/v1/forecasts/seven-product/evaluation` reports 21 independent cells; any failed cell remains non-formal.
9. Confirm `GET /api/v1/delivery/status` reports `source_mode=live` and contains no CCF work item.
10. Confirm the workbench shows evidence grade, confidence boundary and unresolved gaps; it must not show execution wording.

Full-automation data posture (since 2026-08-30): `public-benchmark.v2`
contains Brent, WTI, Naphtha, PX, PTA, MEG, POY, DTY and CFETS USD/CNY.
The gate evaluates each source against its declared publication cadence rather
than assuming every source is daily. POY/DTY are public aggregate assessment
benchmarks, not per-spec transaction prices. CCF is legacy metadata only and
cannot degrade or block a current run.

Weekly checklist:

1. Review per-source freshness and public-source collection failures.
2. Refresh the seven-product D1/D7/D30 forecast after material public updates.
3. Review per-cell OOS coverage, leakage checks, naive-baseline comparison and failure reasons.
4. Review the current product brief and source limitations before any customer checkpoint; do not reuse historical price-backtest or execution-strategy material.

Quality gate:

```bash
python3 server/scripts/run_delivery_data_quality_gate.py \
  --db server/data/agent.db \
  --output .codex-run/delivery-data-quality-latest.json \
  --warn-only
```

Inspect current input health and the seven-product forecast/evaluation:

```bash
curl -sS http://127.0.0.1:8000/api/v1/benchmarks/public-v2 | python3 -m json.tool
curl -sS http://127.0.0.1:8000/api/v1/forecasts/seven-product | python3 -m json.tool
curl -sS 'http://127.0.0.1:8000/api/v1/forecasts/seven-product/history?limit=5' | python3 -m json.tool
curl -sS http://127.0.0.1:8000/api/v1/forecasts/seven-product/evaluation | python3 -m json.tool
```

Run or diagnose the lifecycle directly:

```bash
server/.venv/bin/python server/scripts/run_seven_product_forecast_lifecycle.py \
  --dry-run --db server/data/agent.db
server/.venv/bin/python server/scripts/run_seven_product_forecast_lifecycle.py \
  --apply --db server/data/agent.db
```

`--dry-run` never appends a batch or outcome. `--apply` is exactly-once per
Shanghai business date. Never manufacture prior dates to accelerate D1/D7/D30
OOS counts.

Generate the same 21-cell forecast and OOS gate as a read-only, content-addressed evidence artifact. The command
returns exit code 2 while any cell is blocked, refuses a database that needs migration, and verifies that the input
database hash did not change during evaluation:

```bash
npm run eval:forecast -- --db server/data/agent.db \
  --output-dir .codex-run/seven-product-evaluation
```

The daily bundle runs this evidence command after settlement and issue. The Docker/cloud wrapper consumes the same
embedded `seven_product_oos_evaluation` status instead of launching a duplicate preview evaluation. Its run summary
exposes `formal_prediction_status`, `formal_prediction_passed_count` and the evidence path separately from service
health; an operationally healthy workbench therefore cannot be confused with a 21/21 formally qualified model.
In apply mode the runner resolves the issued business date to its immutable batch and always evaluates at that batch's
frozen `as_of_time`; a different explicit `--as-of` is an error, not a second snapshot. Review
`evaluation_cutoff_source`, `forecast_as_of_time`, `evaluation_as_of_time`, and `cutoff_matches_forecast` in the
content-addressed evidence. They must identify one cutoff before the daily status is accepted.
Also verify `database.fingerprint_schema_version=sqlite-state-fingerprint.v1`: the combined SHA covers the main
database, WAL, and rollback journal. A main-file-only hash is insufficient in WAL mode; SHM is intentionally excluded
because readers update its lock/index cache.

Customer-facing output is limited to seven-product point/interval forecasts, direction, confidence, evidence,
counter-evidence, data gaps and next observation signals. It must never contain procurement, quotation, order-taking,
inventory, processing-profit or trading execution instructions.

## Staging / Production Required Environment

```bash
APP_ENV=staging
ENFORCE_INTERNAL_TOKEN=1
INTERNAL_API_TOKEN=<managed-secret>
CORS_ALLOW_ORIGINS=https://staging.example.com
DEEPSEEK_API_KEY=<managed-secret>
SQLITE_PATH=/data/agent.db
```

Do not deploy staging or production with an empty `INTERNAL_API_TOKEN`. The Docker image also refuses to start unless
`APP_ENV` is `staging` or `production`, `ENFORCE_INTERNAL_TOKEN=1`, and `INTERNAL_API_TOKEN` is non-empty.

## Docker Startup

```bash
cp .env.production.example .env
# Edit .env and set INTERNAL_API_TOKEN before starting.
docker compose up --build
```

Compose binds the backend service to `127.0.0.1:${BACKEND_PORT:-8000}` and stores SQLite data at `/data/agent.db` on
the `agent-data` volume. It also starts the `scheduler` service, which runs the production daily automation loop and
alert delivery. Expose the service through a reverse proxy rather than publishing backend port `8000` directly.

Scheduler operations:

```bash
docker compose logs -f scheduler
docker compose logs -f event-summary-worker
docker compose run --rm event-summary-worker python server/scripts/run_event_summary_worker.py --once
docker compose run --rm scheduler python server/scripts/run_production_scheduler.py --once --skip-health
docker compose restart scheduler
```

The event-summary worker applies both a UTC daily request cap and a conservative
per-request RMB reservation before selecting work. Inspect
`/data/event-summary-worker/latest.json` and `daily-budget.json` for queue and
budget state. For the macOS launchd runtime, worker heartbeat stdout is sent to
`/dev/null`; the bounded `latest.json` remains the operational status source, so
the worker cannot recreate the prior unbounded-log disk exhaustion condition.

Scheduler reports and alerts are written to `/data/local-production` inside the shared volume.

## Backup And Restore

Create a release backup:

```bash
docker compose exec backend sh -lc 'set -eu; mkdir -p /data/backups; cp "$SQLITE_PATH" "/data/backups/agent-$(date -u +%Y%m%dT%H%M%SZ).db"'
docker compose cp backend:/data/backups ./backups
```

Restore a known-good backup:

```bash
docker compose stop backend
docker compose cp ./backups/agent-YYYYMMDDTHHMMSSZ.db backend:/data/agent.db
docker compose start backend
```

After restore, run `/api/v1/health/ready` and a read-only smoke check before source fetches or provider-backed chat.

## Troubleshooting

1. Check readiness and metrics.
2. Confirm source registry and outbound host allowlist.
3. Check whether LLM is in configured or fallback mode.
4. Review structured request logs by request id.
5. Disable risky source fetches before retrying failed ingestion.

For source or price collection failures, confirm the target host is explicitly present in `OUTBOUND_FETCH_HOSTS`. The
backend should report blocked hosts as `blocked_by_allowlist` or provider errors mentioning `OUTBOUND_FETCH_HOSTS`; do
not bypass this by adding ad hoc `httpx` calls.

## Daily Direction AI Review Emergency Rollback

The server-side direction counter-review is controlled only by `AI_DIRECTION_REVIEW_ENABLED`. Its production limits are
one invocation and CNY 5 per Asia/Shanghai business day. The owner is the system administrator and the recovery-time
objective is 10 minutes. Disabling it preserves the internal audit ledger and makes the next daily run use the original
rule observation without any provider call.

Emergency disable:

```bash
# In the managed production environment file:
AI_DIRECTION_REVIEW_ENABLED=0

# The daily job reads the environment on every run; do not rebuild or delete an existing daily snapshot.
python3 server/scripts/materialize_agent_foundation.py --dry-run --disable-ai-direction-review
```

Verification:

```bash
python3 -m pytest \
  server/tests/test_materialize_agent_foundation.py \
  server/tests/test_hybrid_direction_cost_gate.py \
  server/tests/test_hybrid_direction_acceptance.py -q
```

Expected evidence: provider attempts are zero while disabled, the rule direction remains observation-only, existing
snapshot bytes do not change, and the cost/audit ledger is retained. Alert the system administrator when provider
failure, timeout, budget exhaustion, or `counter_review_abstained` persists for two daily runs. Re-enable only after a
test run demonstrates provider failure followed by a disabled next-day rule-only success within the 10-minute RTO.

For prediction replay questions, fetch the linked data snapshot and inspect `metadata.collections.*.truncated`,
`total_available`, `returned`, and `order` before treating the snapshot as complete historical context.

## Local Data Source and Authority Audit

Run the complete database inventory and reconciliation audit locally:

```bash
server/.venv/bin/python server/scripts/audit_authoritative_data.py \
  --db server/data/agent.db \
  --output-dir .codex-run/data-source-audits
```

The command scans every local database table, snapshots source-governance status into SQLite, and writes:

- `.codex-run/data-source-audits/authoritative-data-audit-latest.json`
- `.codex-run/data-source-audits/authoritative-data-audit-latest.md`

Exit code `2` means the audit found missing, unknown, or unreconciled provenance. This is an expected governance
blocker, not a reason to relabel data as trusted. A registered source is not considered value-reconciled until a
record-level result with evidence exists in `authoritative_reconciliations`.
Each audit also stores every observed source in `data_governance_source_results`, keyed by `run_id`, including its
registry/authority/authorization status, observed row count, and per-table row counts. The Markdown report explicitly
lists every source that was observed but not registered, so historical runs retain both the total and the identities.

Historical CCF rows remain internal, read-only provenance. They are not current
source authority and must not appear in new acquisition tasks, freshness gates
or delivery claims. Current public observations remain separately labelled and
must never be rewritten as legacy CCF series.

## Common Failure Modes

| Symptom | First Check | Mitigation |
| --- | --- | --- |
| 401 on internal routes | `X-Internal-Token` and `INTERNAL_API_TOKEN` | Rotate or reconfigure secret |
| 429 on chat/fetch | rate limit settings and client behavior | reduce retry pressure; tune limits |
| LLM fallback spike | provider health and `DEEPSEEK_API_KEY` | keep fallback enabled; avoid high-confidence outputs |
| Source fetch blocked | `OUTBOUND_FETCH_HOSTS` and source registry | update allowlist only for authorized sources |
| High 5xx rate | logs by `X-Request-ID` | rollback latest deploy if regression is confirmed |

## Industrial Intelligence Center Operations (schema v37)

Local validation and execution use the explicit runner; dry-run is the default:

```bash
python3 server/scripts/run_industrial_intelligence_daily.py \
  --dry-run --business-date YYYY-MM-DD

python3 server/scripts/run_industrial_intelligence_daily.py \
  --apply --db /absolute/path/to/isolated-v36-or-v37.sqlite \
  --output-dir /absolute/path/to/private-report-dir \
  --business-date YYYY-MM-DD
```

`--apply` is required for database writes. `--skip-usgs` is available only for
an explicitly offline run; otherwise the fixed official USGS M4.5+ past-week
provider runs before legacy-news projection. The runner then freezes one cutoff
manifest, performs clustering against that exact item high-water mark, and
materializes the immutable daily brief. It never schedules itself and is not
wired into production until the separately authorized D7 rollout.

The authorized public-production rollout uses
`com.poydty.agent.intelligence-daily`. It runs on weekdays at 09:30 Shanghai
time, catches up every five minutes after wake/login, writes one success stamp
per business date, and exits immediately while the feature flag is disabled.
Configure the production env without exposing secrets, then regenerate the
launchd definitions:

```bash
python3 server/scripts/manage_public_production.py configure-intelligence --disable
python3 server/scripts/manage_public_production.py generate-launchd \
  --tunnel poy-dty-agent \
  --cloudflared-config "$HOME/.cloudflared/config.yml"
```

After v37 migration and legacy smoke, run `configure-intelligence --enable`,
restart the backend, load the new scheduled plist, and require
`manage_public_production.py smoke --require-intelligence`.

Before freezing a release candidate, run the fixed-scale API latency gate in
an empty private directory. It creates only an isolated benchmark database and
a 0600 JSON report; it never reads or writes the production database:

```bash
python3 server/scripts/benchmark_industrial_intelligence.py \
  --work-dir /private/tmp/intelligence-benchmark/work \
  --output /private/tmp/intelligence-benchmark/report.json
```

Release evidence requires at least 1,000 item/event pairs and 20 measured
iterations after warmup. The default is 2,000 pairs and 30 iterations; list and
detail p95 must be below 500 ms, while search and both map zoom paths must be
below 800 ms.

Daily contract (Asia/Shanghai): the pipeline runs projection → 08:20 cutoff
input manifest → clustering/analysis → one immutable brief released at 09:30
(`ready | ready_with_gaps | no_material_events`; `blocked` is honest failure,
never retried by overwriting — corrections go through later event revisions
and the next brief).

- Status: run pages (`运行与来源` subview), `GET /intelligence/runs`, the
  `poy_dty_intelligence_*` metric families, and the `intelligence` section of
  `/api/v1/health/deep`.
- Feature flag: `INDUSTRIAL_INTELLIGENCE_ENABLED=0` is the functional
  kill-switch; v37 data stays in place read-only and the legacy seven modules
  are unaffected. There is no partial-enable mode.
- Single-flight: `projection:legacy_news_articles`, `clustering:<date>`, and
  `brief:<date>` file locks live under the run directory
  (`INTELLIGENCE_RUN_DIR`, default `data/intelligence-runs`, 0700/0600). The
  OS releases `flock` on crash; a missing terminal `intelligence_runs` row is
  reported as `prior_run_missing_terminal` context, never fabricated success.
- Late arrivals: items that become visible after the 08:20 cutoff appear in
  the radar immediately and in the next brief. A frozen brief is never
  rewritten; re-materializing a frozen date returns the stored record.
- Rights tightening: when a source policy narrows, append the new revision,
  purge the blocked text from `intelligence_search_fts`
  (`rebuild_search_index`), and only then re-enable search. Reads project the
  stricter policy (`presentation_status=redacted`) without touching the
  immutable history.
- Diagnosing `blocked` briefs: check the brief `gaps` list —
  `coverage_domain_uncovered` means fewer than two of the three core domains
  (energy_feedstock, polyester_supply, logistics_geopolitics) had an active
  A/B capability source in that day's catalog snapshot.

### 方向复核上游就绪门禁（2026-09-18）

日度链先同步完成本轮来源采集并通过来源/质量检查，再进入 foundation。
新闻入库时已同步入摘要队列；方向复核等待队列没有 pending、可重试的
failed/rejected 或 processing（包括过期租约），且使用与复核相同过滤条件
检索到至少一份证据后，才允许 AI 调用。已耗尽摘要重试的失败条目不代表
成功，但不继续等待不可执行的任务；证据质量门禁仍独立生效。

- `DIRECTION_UPSTREAM_GATE_MAX_WAIT_SECONDS`：默认 1800 秒（30 分钟），范围 0–1800。
  0 表示只检查一次，仍不绕过就绪条件。
- `DIRECTION_UPSTREAM_GATE_POLL_SECONDS`：默认 30 秒，范围 1–60。
  非数值/非有限配置回退默认值，避免死循环或无限等待。
- foundation 子进程超时为原步骤预算 + 门禁等待预算 + 60 秒；探测本身
  的 I/O 也受该外层超时保护，门禁等待预算不是单次检索耗时的硬取消机制。
- 摘要状态未知、表缺失或检索异常均不放行。超时跳过 AI，不占模型调用；
  `direction_review_audit.upstream_gate` 和日度报告记录状态、耗时、次数、
  待处理数、过期租约数、证据数量及原因，日度总报告增加 warning。
- 关闭模型调用时只探测、不等待。dry-run 不触发正式复核。
- 成功时复用最后一次探测的证据对象和 as-of，避免二次检索产生不一致。
- 当前策略等待全局可执行摘要队列。持续入队可能导致有界超时；该情况
  必须保留 warning，不能将其解释为业务事实“没有反证”。恢复消费者或
  索引后，在有正式写入授权的补跑窗口重新执行日度链，不自动无限补跑。

本次不更改备份/索引保留、预测目标、证据质量标准或模型预算。

### 日度链来源锁竞争补偿（2026-09-19）

固定时间日度链可能与新闻/来源调度器同时启动。来源脚本返回其明确的
`locked`（退出码 75）时，日度链只对这一类竞争做有界等待并重试；来源抓取、
质量门禁或数据库错误不会被重试或改写为成功。默认最多等待 1800 秒、每 30 秒
重试一次，可用以下环境变量调整（均有硬上限）：

- `LOCAL_DAILY_SOURCE_LOCK_RETRY_MAX_WAIT_SECONDS`：默认 1800 秒；
- `LOCAL_DAILY_SOURCE_LOCK_RETRY_POLL_SECONDS`：默认 30 秒，实际限制在 1–60 秒。

恢复后，`latest-status.json` 的 `source_automation.lock_retry` 记录尝试次数、
等待时间和 `recovered`。超时则记录 `timed_out`，foundation/方向复核继续保持
fail-closed 的 skipped/blocked 状态，避免使用陈旧来源结果。排查时同时查看
`source-automation/source-automation-latest.json` 的锁状态和日度报告中的
`source_automation` 字段。

### 常驻摘要消费者与发布目录（2026-09-19 修订）

`event-summary-worker` 是公开工作台的常驻消费者，和后端、新闻调度器一起由
`manage_public_stack.sh` 管理。切换 `current` 后必须更新这些常驻进程；仅切换符号链接
不会更新已运行进程加载的 Python 环境。定时日度任务不得因此被强制重跑。

发布目录保留最新三个日期版本，额外保护 `current`、`previous` **以及仍被本应用
launchd 进程使用的版本**；实际数量可超过三个。无法通过 launchctl/lsof 确认使用情况时
跳过清理并报警告，不依据目录数量猜测可删除对象。删除失败不得报告为已删除。

摘要每轮领取任务前检查当前运行环境及 TLS 证书包。发布目录被误删时先退出，交由
launchd 从 `current` 重启，避免把基础设施错误记成整批文章内容失败。已耗尽常规重试的
明确文件缺失错误，仅在环境检查通过后进入现有延迟恢复通道；累计最多六次，不清零
历史次数、不重置预算、不绕过正文或数字核验。

## 外部信息覆盖治理（2026-09-20）

- 使用业务需求矩阵与来源链路矩阵核验产出；成功请求/进程/戳不等于新事实。
- `server/scripts/recover_czce_history.py` 默认只准备官方缺日候选，`--apply`仅在具体批次获准并完成在线备份后执行；不覆盖已有主力日期，不倒填visible_at。
- `server/scripts/recover_verified_spot_history.py --db ... --output ...` 只读准备现有MEG/石脑油原文校验结果。批准后使用`--candidate ... --apply`，只追加缺失的capture，不更改旧行情。候选内容与执行时原行再次比对。
- 每小时正文恢复脚本为`server/scripts/backfill_news_article_bodies.py`，默认dry-run。自动任务只取近7日的有界候选，20篇/轮、并发2、40秒/篇、同版本最多3次、退避1小时。输出旁的body-recovery-journal保留修改前行；失败不清零摘要旧重试数。
- 修复正文或摘要后核对同一article_id在投影、搜索、索引及引用的版本；不得只看摘要表completed。原文hash不一致、摘要晚于查询截止时点时，派生结果必须拒用。
- 新鲜度检查按品种/来源节奏查看真实观察值、有效文章及摘要/索引落差；检查器失联为unknown。48小时地图没有地理证据时允许为空，非地图入口应保留无坐标有效事件。
- 所有操作保留已确认的模型预算、备份及索引保留策略；本轮不恢复DCE/CCF正式来源，不强制重跑历史预测。

# Deployment

Current production is the Tencent Cloud runtime; see [Cloud daily operations](cloud-daily-operations.md).
The former macOS runtime is retired. Historical atomic releases and launchd procedures are documented in [Public production runtime on macOS](public-production-macos.md); do not restart its writers alongside the cloud.

## Local

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

Connect frontend to backend:

```bash
VITE_API_BASE_URL=http://localhost:8000 VITE_API_PREFIX=/api/v1 npm run dev
```

## Docker

The Dockerfile builds the backend service for staging/production-like smoke tests. It intentionally fails closed:
the container refuses to start unless `APP_ENV` is `staging` or `production`, `ENFORCE_INTERNAL_TOKEN=1`, and
`INTERNAL_API_TOKEN` is non-empty.

```bash
cp .env.production.example .env
# Edit .env and set INTERNAL_API_TOKEN plus any provider/source credentials.
docker compose up --build
```

Compose binds the backend to loopback only by default:

```text
http://localhost:8000/api/v1/health/live
```

For a real exposed deployment, put a reverse proxy in front of the service rather than publishing `8000` on all
interfaces. Compose defaults `SQLITE_PATH` to `/data/agent.db` and mounts the durable `agent-data` volume at `/data`.

Compose also starts a `scheduler` service. It uses the same durable `/data` volume, runs
`server/scripts/run_production_scheduler.py`, executes the daily automation in apply mode, writes reports under
`/data/local-production`, and sends configured alerts after each run. The daily bundle itself settles and issues the
immutable seven-product ledger batch, then generates one read-only, content-addressed OOS evidence package bound to
that exact business-date batch and its frozen `as_of_time`, evaluated against a consistent post-issue snapshot of
the database so concurrent scheduler writes cannot invalidate the evidence input fingerprint. A conflicting evaluator cutoff is rejected before evidence
is written. Addressed evidence is fsync-backed and published before the atomically replaced `latest` pointer, with
owner-only permissions throughout. The read-only run fingerprints the SQLite main file plus persistent WAL/rollback
journal bytes before and after evaluation; SHM is excluded because it is a lock/index cache. The outer scheduler
consumes this status instead of running a second preview
evaluation. The scheduler is protected by file locks, step timeouts, backups before writes, and Docker
`restart: unless-stopped`.

Compose also starts `event-summary-worker` as an independent DeepSeek queue
consumer. It recovers expired processing leases, queues eligible full-text
articles, and drains bounded batches without coupling provider latency to news
acquisition. Configure `EVENT_SUMMARY_WORKER_CONCURRENCY`,
`EVENT_SUMMARY_DAILY_REQUEST_LIMIT`, and `EVENT_SUMMARY_DAILY_BUDGET_RMB` in the
secret-backed environment. Its durable, atomically replaced status is
`/data/event-summary-worker/latest.json`.

The 30-minute `news-scheduler` also invokes the due-aware public-source loop. This provides the OFAC 30-minute cadence;
EIA, FRED, CFTC, CFETS, GACC and UN Comtrade are skipped until each source's own policy says it is due.
DCE EG is soft-removed: production does not schedule or request it. The existing public-price scheduler captures the accepted SunSirs China MEG spot assessment into its dedicated append-only current-label series; DCE/Sina/Eastmoney data cannot substitute for that series.

For personal WeChat alerts, set one of these in `.env`:

```bash
ALERT_PUSH_PROVIDER=serverchan
SERVERCHAN_SENDKEY=<serverchan-sendkey>

# or
ALERT_PUSH_PROVIDER=pushplus
PUSHPLUS_TOKEN=<pushplus-token>
```

Enterprise WeChat compatible webhooks remain supported with `ALERT_PUSH_PROVIDER=wechat_webhook` and
`WECHAT_WEBHOOK_URL=<secret-url>`.

## Staging

Required environment:

```bash
APP_ENV=staging
ENFORCE_INTERNAL_TOKEN=1
INTERNAL_API_TOKEN=<managed-secret>
CORS_ALLOW_ORIGINS=https://staging.example.com
DEEPSEEK_API_KEY=<set-in-gitignored-env>
SQLITE_PATH=/data/agent.db
```

Staging deploy steps:

1. Build frontend with `VITE_API_BASE_URL` pointing to the staging backend and `VITE_API_PREFIX=/api/v1`.
2. Deploy backend image with a durable `/data` volume.
3. Run `/api/v1/health/ready` and `/api/v1/health/deep`.
4. Run smoke tests against the deployed frontend and backend.
5. Verify `/metrics` is scraped.

## Production

For this project, production means a stable personal deployment for one
researcher, not a team-operated SaaS. Production must not start unless:

- `APP_ENV=production`,
- `ENFORCE_INTERNAL_TOKEN=1`,
- `INTERNAL_API_TOKEN` is a managed secret,
- current inputs are public and outbound hosts are allowlisted,
- lightweight monitoring and a rollback path are configured.

Production rollout:

1. Deploy backend first behind the existing frontend-compatible `/api` and new `/api/v1` paths.
2. For the macOS public runtime, inspect the durable shared database with
   `manage_public_production.py index-status` and run
   `manage_public_production.py rebuild-index` when the semantic index is
   missing or stale. This backs up the shared database, builds a shadow index,
   and atomically activates it; never copy the repository database over an
   existing shared database.
3. Verify health checks, semantic-index readiness, and metrics.
4. Deploy frontend targeting `/api/v1`.
5. Run the backed-up daily apply once; require `public-benchmark.v2` freshness
   to report 9/9 before treating the release as delivered.
6. Confirm OPEC uses the existing news scheduler; OFAC has a successful baseline/delta check; GACC and UN Comtrade have
   a successful monthly-data due-check; and the user-file inbox reports `idle` or a successful import/review result.
7. Monitor error rate, latency, LLM fallback, and source fetch status during the first live research session.
8. Roll back independently if frontend or backend health degrades.

For the current macOS production matrix, event-summary and experience-settlement
workers remain disabled; CCF has no capture, import or scheduler job. The
credential-free public-health probe is enabled every five minutes. This matrix
is narrower than the optional Docker Compose topology described above.

Production scheduler smoke test:

```bash
docker compose run --rm scheduler python server/scripts/run_production_scheduler.py --once --skip-health
docker compose logs -f scheduler
```

## Storage And Backup

SQLite is acceptable for local and staging trace/audit persistence. Production should either mount durable storage with backups or migrate the storage adapter to a managed database before multi-instance deployment.

For the compose deployment, back up the SQLite file before releases:

```bash
backup_name="agent-$(date -u +%Y%m%dT%H%M%SZ).db"
docker compose exec backend python server/scripts/manage_public_production.py backup-db \
  --source /data/agent.db \
  --destination "/data/backups/$backup_name"
docker compose cp backend:/data/backups ./backups
```

`backup-db` uses SQLite's online backup API and runs `PRAGMA integrity_check`
before publishing the backup, so committed WAL transactions are included.

### Automatic Pre-Migration Backups

Before `connect()` applies any schema migration or legacy upgrade script to an
existing database, the backend writes an online-backup snapshot to
`<db dir>/migration-backups/<stem>.pre-migration.v<from>-to-v<to>.<timestamp>.sqlite`,
verifies it with `PRAGMA integrity_check`, and keeps the last 5 snapshots. Any
backup or integrity failure aborts the upgrade before the first schema change
(fail closed), so a failed migration cannot destroy the only copy of the data.
Startup stays down until the operator resolves the underlying failure (for
example disk space or permissions), then the next launch retries the backup
before migrating. Restoring one of these snapshots is a deliberate operator
procedure: stop all writers, verify the snapshot with `PRAGMA integrity_check`,
restore it with `manage_public_production.py restore-db --writers-stopped`, and
rerun deep health before re-enabling writes.

Migration 35 creates the append-only seven-product forecast, cell and outcome
ledger. After upgrading, run the lifecycle once in apply mode and verify one
21-cell batch before exposing the current forecast endpoint:

```bash
server/.venv/bin/python server/scripts/run_seven_product_forecast_lifecycle.py \
  --apply --db /data/agent.db --output-dir /data/local-production/seven-product-lifecycle
```

Do not seed earlier business dates. The absence of historical live outcomes is
an honest waiting condition, not a migration defect.

Restore from a known-good backup during rollback. Every writer must remain
stopped until the restore integrity check and read-only smoke test pass:

```bash
docker compose stop backend scheduler news-scheduler event-summary-worker
docker compose run --rm --no-deps backend python server/scripts/manage_public_production.py restore-db \
  --backup /data/backups/agent-YYYYMMDDTHHMMSSZ.db \
  --destination /data/agent.db \
  --writers-stopped
docker compose start backend
# Verify readiness and read-only API smoke here.
docker compose start scheduler news-scheduler event-summary-worker
```

The restore validates the backup, preserves the pre-restore database under
`/data/pre-restore/`, atomically replaces the database, and validates the
result. After restore, require `/api/v1/health/ready` and a read-only smoke test
before re-enabling source fetches or provider-backed LLM answers.

## Industrial Intelligence Center Rollout (schema v37, Local Release Candidate — production gate required)

Production enablement is a separate, explicitly authorized operation:

1. Freeze the exact commit/artifact; add `earthquake.usgs.gov` to
   `OUTBOUND_FETCH_HOSTS` in the managed env file (see `.env.example`).
   Use `manage_public_production.py configure-intelligence --disable` to add the
   host, persist a cursor-signing secret and keep the feature closed without
   printing secret values.
2. Stop all SQLite writers and take a verified v36 backup (same procedure as
   the v35→v36 migration).
3. Run the v36→v37 migration (`append_only_industrial_intelligence_domain_v37`).
   It only creates six append-only `intelligence_*` tables plus one rebuildable
   FTS5 index and sets `user_version=37`; it never backfills data or touches
   legacy tables. Re-open and repeat-run are safe; name/version conflicts fail
   closed.
4. Deploy with `INDUSTRIAL_INTELLIGENCE_ENABLED=0`, complete the legacy smoke,
   then enable the flag and smoke the new module (auth, sources, radar,
   brief `data_not_ready`, map same-origin assets, `/metrics` families). Enable
   with `configure-intelligence --enable` and run public smoke with
   `--require-intelligence`.
5. Schedule the daily pipeline: projection → 08:20 Asia/Shanghai cutoff
   manifest → clustering/analysis → 09:30 unique brief release
   (`ready | ready_with_gaps | no_material_events`; `blocked` is an honest
   terminal state).
   The entry point is `server/scripts/run_industrial_intelligence_daily.py`.
   `generate-launchd` creates `com.poydty.agent.intelligence-daily`; load it only
   as part of an explicitly authorized D7 rollout. Its weekday 09:30 wrapper
   uses a daily stamp, five-minute catch-up and the same frozen service stages.
6. Rollback: functional rollback first (flag off; v37 data stays read-only;
   legacy modules unaffected). Version rollback requires stopping writers,
   preserving a read-only copy of the v37 database, restoring the verified
   v36 backup, and switching to the matching v36 release. An old v36 release
   must never run against a v37 database; flipping the release symlink alone
   is not a rollback.

# Source Automation Audit

Date: 2026-07-07
Scope: information sources, data sources, news sources, unattended operation
Mode: local tests, dry-runs, and real network fetch smoke tests

## Verdict

The project can run several source-automation paths unattended, but it is not yet fully production-grade unattended automation for all configured sources.

Unattended today:

- EIA petroleum API, FRED macro API, and CFTC COT petroleum fetchers can make real network calls and parse structured observations.
- News/event fetching can attempt all configured public news sources and persist results, but individual sites fail or return no relevant items.
- Local preflight, daily dry-run, alerts, quality gate, backup probe, and report generation are wired.

Not fully unattended today:

- CCF/vendor data remains authorization-first and depends on licensed login/export or Computer Use-assisted capture.
- INE/CZCE/DCE action-grade futures sources are still capture-package/manual-file workflows, not finished connectors.
- UN Comtrade, GACC, CFETS, and several registry HTML sources lack structured adapters.
- Long-term scheduler/supervisor installation is generated as launchd templates, but templates are not loaded automatically.

## Verification

Commands run:

- `npm run check`: passed.
- `python3 -m pytest server/tests/test_fetchers.py server/tests/test_source_acquisition.py server/tests/test_futures_daily.py server/tests/test_public_news_backfill_v2.py server/tests/test_local_production_preflight.py server/tests/test_local_production_alerts.py server/tests/test_discover_public_chain_prices.py -q`: 26 passed.
- `npm run local:preflight`: ready, 0 blockers, 0 warnings.
- `npm run source:automation -- --record-plan --fetch-public --fetch-akshare-futures-daily --run-quality --news-skip-details --news-limit-per-source 3 --news-cursor-pages 1`: passed dry-run; public fetches made real network calls without DB writes.
- Isolated apply smoke using a copied SQLite DB and explicit `SQLITE_PATH`: passed with backup.
- `npm run local:daily -- --dry-run --skip-health`: ready_with_warnings.
- `npm run local:alerts`: produced 2 warning alerts; WeChat notification skipped because webhook is not configured.
- `npm audit --json`: 0 high/critical vulnerabilities.
- `cd server && uv run pip-audit --format json`: no known vulnerabilities found.

Isolated apply smoke evidence:

- EIA: status ok, 1,480 observations stored.
- FRED: status ok, 4,088 observations stored.
- CFTC: status ok, 5,160 observations stored.
- AkShare futures daily prototype: 18,366 rows stored in the isolated DB, latest trade date 2026-07-07, with future-contract parsing errors.
- News live run: 40 source runs, 24 articles found, 24 clusters upserted, 6 events created; 15 ok, 21 no_relevant_items, 4 error.
- News errors included OPEC browser check/403, GDELT 429 or parse error, Sinopec 404, and CNPC 412.
- Yahoo trade futures proxy apply timed out after 90 seconds in isolated smoke.

## Key Findings

1. Public structured fetch coverage is narrower than the readiness labels imply. `source_acquisition.py` marks more public sources as ready/full than the actual batch fetcher can structure; the real structured batch is EIA, FRED, and CFTC.

2. `run_source_automation.py --db` is not consistently honored by public fetch writes. `bulk_create_market_observations()` uses `storage.connect()`, which follows `SQLITE_PATH`; this caused `database is locked` against the main DB while local `uvicorn` had the DB open.

3. The system has partial retries/timeouts, but not a complete unattended runtime model. Source automation has per-step timeouts and news retries; `local:daily` subprocesses do not have a global timeout, lock file, or resumable state machine.

4. News runs can leave stale `running` rows. The isolated smoke left `news_fetch_runs.status='running'` rows, including a latest `us_centcom_press` run with no `finished_at`, despite the overall command completing.

5. News and HTML fetch security needs hardening. Initial host allowlist checks exist, but redirect final URLs and cross-host detail links need per-source enforcement to avoid source/tier pollution.

6. Alerts work locally but are not a full alerting loop. They generate JSON/Markdown and can send WeChat if configured, but there is no persisted ack/escalation/dedup lifecycle.

7. CCF and action-grade official futures data should not be described as unattended. They remain authorized/manual or semi-automatic until licensed export/download connectors are finished and reviewed.

## Recommended Priority

1. Fix DB routing and locking: make all storage writes accept the resolved `--db` connection or set `SQLITE_PATH` from `args.db` before imports; add a single-run lock around apply jobs.
2. Add stale-run cleanup: mark old `news_fetch_runs.status='running'` as timeout/abandoned and alert on them.
3. Add global daily timeout and per-step hard timeout for `local:daily`.
4. Split source readiness labels into `network_fetchable`, `structured_parser`, `db_write_apply`, and `action_grade`.
5. Harden redirects and detail-link host policies with per-source allowed detail hosts.
6. Add alert lifecycle: dedupe key, first_seen, last_seen, ack status, and escalation target.
7. Finish adapters or downgrade claims for UN Comtrade, GACC, CFETS, and official futures sources.

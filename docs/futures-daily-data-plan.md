# POY/DTY Upstream Futures Daily Data Plan

## Scope

Required window: from 2025-01-01 through the latest China trading day after exchange close.

Required products:

| Product | Exchange | Contract root | Unit |
| --- | --- | --- | --- |
| INE SC crude oil futures | INE | SC/sc | CNY/bbl |
| PTA futures | CZCE | TA | CNY/mt |
| PX futures | CZCE | PX | CNY/mt |
| MEG futures | DCE | EG/eg | CNY/mt |

## Implemented Local Contract

SQLite table: `futures_daily_bars`

API:

```text
GET  /api/v1/futures/daily-bars?product=PTA&start=2025-01-01&end=2026-07-03&contract_role=main
POST /api/v1/futures/import/daily-bars
```

CSV import template: `docs/data-templates/futures_daily_bars.csv`

CLI import:

```bash
cd /path/to/project
PYTHONPATH=server python3 server/scripts/import_futures_daily_bars.py \
  authorized-export.csv \
  --json-output server/data/futures_daily_import_latest.json
```

Daily automation import directory:

```text
.codex-run/futures-daily/*.csv
```

`server/scripts/run_local_daily.py` passes this directory through `server/scripts/run_source_automation.py --import-futures-daily`. In dry-run mode it parses and reports only; in `--apply` mode it writes rows after the existing backup guard.

The import path accepts official public files and user-provided vendor exports. Publicly visible data needs no separate approval ceremony for this personal non-commercial workbench. The system does not log in for the user or bypass paywalls, CAPTCHAs, access controls, or technical blocks.

Temporary AkShare prototype fetch:

```bash
cd /path/to/project
cd server && uv run python scripts/fetch_akshare_futures_daily.py \
  --start 2025-01-01 \
  --json-output data/akshare_futures_daily_latest.json
```

Daily automation includes `--fetch-akshare-futures-daily`. In dry-run mode it fetches and validates without writing; in `--apply --backup-db` mode it writes to `futures_daily_bars` as `source_id=akshare_prototype`.

## Field Mapping

| Field | Meaning |
| --- | --- |
| `trade_date` | Exchange trading date. |
| `exchange` | `INE`, `CZCE`, or `DCE`. |
| `product` | Normalized product: `SC`, `PTA`, `PX`, `MEG`. |
| `contract_code` | Actual contract or vendor continuous symbol. |
| `contract_role` | `near_month`, `next_month`, `main`, `main_continuous`, `secondary_continuous`, `index_continuous`, or `listed_contract`. |
| `term_structure_rank` | 0 for nearest listed month, 1 for next month, etc. |
| `is_main` | Main contract flag after close. |
| `is_continuous` | Vendor or synthetic continuous-contract flag. |
| `open/high/low/close/settle` | Daily OHLC and settlement. |
| `volume/open_interest` | Daily volume and open interest. |
| `change_pct` | Daily percentage change when supplied by the source. |
| `unit` | `CNY/bbl` for SC, `CNY/mt` for PTA/PX/methanol/MEG. |
| `source_publish_time` | Source-published or vendor-updated timestamp. |
| `visible_at` | Earliest time the row may be used by the system for as-of backtests. |
| `source_note` | Human-readable source description. |
| `license_scope` | Compatibility provenance field recording the applicable personal-use or vendor-export policy; it is not a public-data approval gate. |
| `revision_note` | Historical correction or vendor revision notes. |

## Main/Continuous Rule

Default internal rule:

1. For actual listed contracts on each trade date and product, assign the main contract to the contract with the largest open interest.
2. If open interest ties, use the larger volume.
3. Mark near-month and next-month from listed contract delivery-month order.
4. Preserve vendor-supplied continuous series as separate rows instead of overwriting actual contracts.
5. Use `visible_at`, not `created_at`, for historical as-of filtering and backtests.

If a user-provided vendor export supplies a main-continuous mapping, store it as `main_continuous` rows and keep this internal rule for audit comparison.

## Source Assessment

| Source | Use | Notes |
| --- | --- | --- |
| Official INE/CZCE pages or APIs | A-tier formal candidates. | CZCE PTA/PX/methanol uses the verified fixed-file adapter. INE SC remains technically blocked. |
| Official DCE pages or APIs | Historical audit only. | DCE MEG is soft-removed and new DCE imports are rejected; its historical rows and adapter remain audit-only. |
| Wind Client API | Preferred production vendor if company has license. | Supports API access and historical daily/tick data playback; confirm redistribution/report rights in contract. |
| iFinD Quant API | Preferred production vendor if company has license. | Covers futures, daily/time-series, and high-frequency data; requires formal account and entitlement. |
| Choice/EmQuant | Preferred production vendor if company has license. | Supports futures continuous contracts, real contracts, and explicit main/secondary/index rules. |
| Tushare Pro | Low-cost secondary candidate. | `pro_bar` supports futures daily bars; requires token and points/permission. Confirm commercial/internal use terms. |
| AkShare | Development/prototype candidate. | Useful for field mapping and cross-checking, but it is not an exchange-certified label and cannot replace first-party provenance. |

## Daily Update

Recommended production schedule:

1. Trigger after China futures final daily settlement is visible, for example 17:30 Asia/Shanghai on trading days.
2. Pull fixed official adapters and any configured user-provided vendor export/API rows for `SC`, `TA`, `PX`, and `EG`.
3. Put user-provided CSV exports in `.codex-run/futures-daily/` or import through `POST /api/v1/futures/import/daily-bars`.
4. Query `GET /api/v1/futures/daily-bars?start=2025-01-01` for downstream chain analysis.
5. If a trade date has no source rows, do not synthesize data. Leave a freshness gap and retry on the next run.

## Remaining Data Caveat

This repository has the data contract, database table, import path, API, template, CZCE adapter, and update script. It does not include vendor credentials or a bypassing scraper. Vendor exports, when voluntarily supplied by the user, must preserve their source identity and may not be relabelled as exchange-certified data. Historical backfill also cannot prove pre-capture point-in-time visibility.

# CCF Authorized Update Runbook

> **Soft-removed on 2026-08-30. Do not execute this historical runbook.**
> Existing CCF rows are retained read-only for reproducibility. Current capture,
> import, queue, scheduler, freshness/readiness and alert paths exclude CCF.
> Re-enabling any procedure below requires a new ADR and explicit operator
> approval. The remaining text is historical reference only.

## Purpose

This runbook fixes the CCF acquisition loop for POY/DTY Agent delivery. CCF is an authorized commercial source. Use only user-authorized pages, official exports, uploaded files, or visible page tables. Do not bypass login, CAPTCHA, paywalls, license limits, robots.txt, or anti-bot controls.

## Cadence

| Flow | Cadence | Owner | Trigger | Destination |
| --- | --- | --- | --- | --- |
| CCF price freshness check | Every business day 09:30 | Data Ingestion Agent | latest CCF price older than 5 days or coverage audit task appears | `forecast_price_points` |
| CCF industry indicator check | Every business day after the CCF page update window; weekends skipped | Data Ingestion Agent | authorized page has not been checked/captured today, target metric missing, or quality gate alert appears | `industry_observations` |
| Public source refresh | Every business day 08:30 | Data Ingestion Agent | EIA/FRED/CFTC/GDELT or official source updates | market/news tables |
| Post-import validation | Immediately after every import | Evaluation Agent | any CCF/user-file import | `.codex-run`, delivery status |

## Authorized CCF Collection

1. Confirm the browser session is logged in and licensed for the requested page.
2. Prefer the page's download/export function.
3. If export is unavailable but the page table/chart is visible, use Computer Use to read the authorized page and save raw JSON/CSV under `.codex-run`.
4. If CAPTCHA, 2FA, permission denial, or export limit appears, stop and ask the user to handle it.
5. Never write credentials to logs, tests, reports, screenshots, or code.

## Standard Import Flow

```bash
python3 server/scripts/import_ccf_authorized_csvs.py --dry-run \
  --input-dir .codex-run/ccf-authorized-capture \
  --db server/data/agent.db \
  --summary-output .codex-run/ccf-authorized-import-summary-latest.json
```

For industry indicators:

```bash
python3 server/scripts/import_ccf_industry_observations.py --dry-run \
  --input .codex-run/complete-51-backfill-YYYYMMDD_HHMMSS/industry/ccf_industry_observations.csv \
  --db server/data/agent.db \
  --summary-output .codex-run/ccf-industry-import-summary-latest.json
```

Apply only after backup:

```bash
python3 server/scripts/import_ccf_industry_observations.py --apply --backup-db \
  --input .codex-run/complete-51-backfill-YYYYMMDD_HHMMSS/industry/ccf_industry_observations.csv \
  --db server/data/agent.db \
  --summary-output .codex-run/ccf-industry-import-summary-latest.json
```

## Validation Gates

Run after every import:

```bash
python3 server/scripts/audit_ccf_authorized_source_gaps.py \
  --db server/data/agent.db \
  --start 2025-01-01 \
  --end 2026-06-23 \
  --output .codex-run/ccf-authorized-source-gap-audit-latest.json \
  --task-output .codex-run/ccf-authorized-acquisition-tasks-latest.csv \
  --task-json-output .codex-run/ccf-authorized-acquisition-tasks-latest.json

python3 server/scripts/run_delivery_data_quality_gate.py \
  --db server/data/agent.db \
  --output .codex-run/delivery-data-quality-latest.json \
  --warn-only
```

Then refresh `/api/v1/cost-pressure/outlook` and review the prediction ledger. CCF POY/DTY posterior values may validate transmission but must not become forecast targets.

## Alert Rules

| Alert | Threshold | Action |
| --- | --- | --- |
| CCF price stale | latest date older than 5 days | open CCF price page or export, import after backup |
| CCF industry capture stale | no same-day CCF industry page capture on a business day; weekends skipped | open CCF data center, collect or confirm the industry indicator page, and import after backup if new rows are available |
| Coverage audit task count | `task_count > 0` | generate CCF acquisition task list |
| Unit mismatch | any target unit mismatch | stop scoring, fix parser mapping |
| Non-positive price / non-positive inventory or operating rate | any row | compare with raw page/export and repair import |
| Review leakage | `leaks > 0` | invalidate the affected review and fix the as-of boundary |
| Evidence coverage | key upstream layer missing | lower confidence and display the gap |

## Customer-Facing Rule

Do not present a single hit rate as a production claim. Always show:

- horizon and target
- hit / miss / scored / total
- scored ratio
- pending count
- leak count
- evidence coverage and confidence boundary
- explicit limitation that the result is not an execution instruction

## Cost-Pressure Review

After every material CCF import:

1. Refresh `/api/v1/delivery/status`.
2. Refresh the 1/7/30-day outlook.
3. Confirm evidence grade, confidence and data gaps are present.
4. Review due prediction-ledger entries and bias reasons.
5. Turn the top failed evidence group into one of:
   - data replenishment task,
   - abstain / downgrade rule,
   - horizon-specific evidence request,
   - accepted risk with review date.

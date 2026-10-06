# Industry context and historical summary recovery

Status: approved production activation and bounded recovery completed on 2026-09-27; current release `20260927T140952Z-93290f6ff342df0c`. Historical body coverage remains incomplete; see closeout below.

## Verified problem and decision

A complete 14,444-character Treasury article reached the model, but v10 replaced oil-export
and shipping identities with a generic list of persons/entities/vessels. Impact received only
these compressed facts. v11 retains source-backed industry identities and qualifications in
both Chinese facts and original quotes; relevance is separate from direction certainty.

Preserve exact words, values and negation in evidence. Normalize only extraction whitespace
before punctuation, require two distinct quotations, and allow Chinese entity names with
parenthetical original names. Standard sanctions boilerplate remains in structured records
but does not crowd the reading summary. Provider-cutoff outputs remain rejected.

Known complete Texnet briefs require a matching headline, dated publisher header and
article-end marker. Strip navigation/related articles before model input. A proved complete
short brief is usable; a short feed excerpt is not. Check old 5,000/8,000-character boundaries
before whitespace cleanup, so a final space cannot hide a legacy cutoff.

## Historical replay contract

User explicitly includes previously irrelevant articles. Freeze all current historical IDs,
source hashes and summary revisions in a hashed manifest, without a 365-day cutoff. Preserve
same-source successful relevant summaries. Recover other records or explicitly record the
remaining source/quality boundary; historical index identities reuse the existing guarded
fetch path without reinstating removed sources or scheduling a new collector.

Archive previous summary results and attempts durably before a prompt/source revision reset.
No schema migration: content-addressed private sidecars, one current summary per article.
If archival fails, the database update must fail. Active processing leases and mismatched
source/revision hashes cannot be overwritten. Body updates use an atomic expected-hash check.

The batch queues the ordinary consumer; it never calls a second unmetered model client.
Retain current budgets, concurrency, per-revision retries, index retention and backup rules.
Record body attempts and terminal outcomes in resumable state. Keep first discovery times;
new body/summary visibility is now, never backdated into previous forecasts.

## Validation and limits

Root-cause tests cover layout-only quote differences versus changed facts, distinct quotes,
Chinese names, full source input, wrong body identity, legacy cutoff whitespace, brief
boundaries, revision archives, failed archive rollback, concurrent changes, idempotent replay,
and inaccessible bodies. Small live positive/negative controls are evidence of repaired
examples, not proof that all industry relevance or translated entailment is correct.

No new package/provider/model, forecast issuance or frontend redesign. Build the narrow
runtime overlay against the actual cloud image; do not deploy unrelated dirty files.
Evidence: `/path/to/project-context/summary-context-recovery-20260927/`.
Production activation requires its separate concrete backup/recovery/release plan. Routine
rollback restores code/images and reconciles only changed rows whose hashes still match;
never replace the live database wholesale over newer unrelated data.

## Production closeout (2026-09-27)

The approved3696-article recovery was classified and processed without clearing exhausted attempts or replacing concurrent changes. Follow-up source/body repairs restored74 additional articles (38 usable analyses,21 irrelevant,15 quality rejections). All74 matched the current index and intelligence projection;42 older articles had initially been excluded by the latest1200 corpus cap, now removed for offline index construction only. Online fallback remains bounded.

Latest frozen cohort:364 usable analyses,360 irrelevant,226 fact rejections,9 impact rejections,2733 unavailable bodies,1 exhausted failure,3 concurrent-version skips;150 previously related articles are separately preserved. Missing publisher identity/configuration is not automatically an external boundary. Two temporary repair jobs finished normally; normal workers and08:00 schedule remain. Backups, prior revisions and rollback are retained; predictions were not reissued.

Evidence and limitations: `/path/to/project-context/article-source-recovery-20260927/FINAL_REPORT.md`. Browser interaction was unverified after tool timeouts; public version/health and exact search/detail APIs passed. This replaces the earlier pending-deployment status; it does not assert complete coverage or improved forecasting accuracy.

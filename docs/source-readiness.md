# Source Readiness

Last reviewed: 2026-09-01

This is the canonical source-readiness vocabulary for the single-operator personal workbench. Publicly visible data may be used for this non-commercial workflow without a separate license/manifest approval ceremony. The system still must not bypass login, paywalls, CAPTCHAs, access controls, or technical rate limits.

## Readiness Fields

| Field | Meaning |
| --- | --- |
| `configured` | The source is listed in registry/config/docs and can be shown as a known source. |
| `fetchable` | The current code path can request the source without bypassing access controls. |
| `parsed_to_observation` | Data is normalized into a business table with source id, observation/event time, metric and evidence URL. |
| `scheduled` | The source is included in an unattended due-check or an existing dedicated scheduler. |
| `manual_only` | The source still requires user entry or review instead of unattended ingestion. |

`licensed`, manifest approval, and source-by-source permission review are retired readiness fields. Data quality, point-in-time correctness, provenance, technical access boundaries, and basic security remain mandatory.

## Current Structured Source Matrix

| Source | configured | fetchable | parsed_to_observation | scheduled | manual_only | Cost / operational note |
| --- | --- | --- | --- | --- | --- | --- |
| EIA Open Data | yes | yes with free key | yes, market projection plus append-only Brent capture revisions | yes | no | No paid source required. EIA v2 exposes the observation period but no row-level publication timestamp; the Brent label therefore uses first successful capture as conservative `published_at/visible_at`. Exact row replays are idempotent and changed row evidence appends a revision. |
| FRED | yes | yes with free key | yes, market observations | yes | no | No paid source required. |
| CFTC COT | yes | yes | yes, market observations | yes | no | Weekly public report. |
| CFETS CNY parity | yes | yes | yes, market observations | yes | no | Public macro context. |
| CZCE PTA/PX/methanol daily | yes | yes | yes, official futures bars plus append-only series revisions | daily | no | Fixed official HTTPS text files; bounded lookback, source hash, first-capture visibility and frozen main-contract rule. Historical one-time backfill is not point-in-time OOS proof. |
| DCE MEG daily | retained for exact historical lookup | no; soft-removed | historical rows/revisions only, under a frozen DCE historical series id | no | no | Soft-removed on 2026-09-01 after the official HTTPS path remained unreachable. It is excluded from source lists, fetch/import paths, scheduling, alerts, preflight and current qualification. Adapter code and Keychain credentials are retained only for audit and reversible restoration; DCE, Sina and Eastmoney prices cannot substitute for the accepted current MEG label. |
| SunSirs China MEG spot assessment | yes | yes | yes, intraday projection plus current-label append-only capture revision | 5-minute public-price scheduler | no | Accepted by the operator on 2026-09-01 as the current MEG label source. It is a public daily China ethylene-glycol non-transactional spot assessment in CNY/mt. Published methodology defines GB/T 4649-2008 industrial premium-grade EG, ex-warehouse/net-water, tank-farm self-pickup, cash/full payment and a 50-1000 tonne standard lot, with a 09:00-10:30 assessment window and scheduled 11:30 publication. That document is dated 2013-11-01 and current applicability is unverified; the page lacks row-level publication/revision timestamps, so first successful capture is the conservative visibility boundary. Source acceptance does not bypass prospective OOS qualification. |
| Trading Economics public naphtha page | yes | current dated value only | yes, projection plus append-only capture revision | 5-minute public-price scheduler | no | Public OTC/CFD assessment in USD/mt. Exact replays are idempotent and changed same-day evidence appends a revision. Historical download/API access is premium and is not used; formal OOS history must accumulate prospectively. |
| TNC POY/DTY history | yes | yes | yes, market projection plus append-only capture revisions | daily | no | Public valuation/assessment history, not exchange transaction prices. Each dated POY/DTY row carries the page hash and first local visibility; exact replay is idempotent and changed page evidence appends a revision. One-time history capture is usable for current reference forecasting but is not historical point-in-time OOS proof. |
| OPEC press releases | yes | yes | yes, news/events | existing news scheduler | no | Uses public Google News RSS for discovery when the official index returns a browser check, then resolves and admits only `https://www.opec.org/pr-detail/...` official pages. Unresolved wrappers are never stored as Tier-A evidence. No paid source or duplicate scheduler. |
| OFAC sanctions | yes | yes | yes, delta events plus private state snapshot | every 30 minutes | no | Business DB stores only counts/hashes and energy/shipping deltas, never the full names/address list. |
| GACC customs statistics | yes | yes | yes, market observations | daily due-check for monthly releases | no | China world-level coal/crude/naphtha/xylene/MEG quantity and value. The official English page is HTTP-only in the current environment; preserve URL and cross-check detailed trade data with UN Comtrade. |
| UN Comtrade | yes | yes with free key; official preview fallback | yes, market observations | daily due-check for monthly releases | no | China imports for crude, broad naphtha, PX, PTA and MEG; world plus top 10 partners; HS revisions preserved. No paid tier is required for this scope. |
| User CSV/XLSX | yes | local inbox | yes, selected business table | daily | no | Standard templates are validated and imported idempotently. |
| User PDF/image | yes | local inbox | no automatic extraction | daily | review only | Produces a review report and moves the file to `review`; it does not write business data. |
| Internal market notes | yes | local entry | yes after entry | n/a | yes | D-tier weak signal. |
| Public quote proxies | experimental | source-dependent | monitor-only when supported | source-dependent | sometimes | Not exchange-grade or transaction-grade data. |

## Product Claim Rules

- A source can be `configured` without being fetchable or scheduled.
- A successful request is not sufficient: parsing must preserve source id, observed time, metric, unit, classification and evidence URL.
- Public pages may be delayed, revised or rate-limited; readiness follows each source's real publication frequency.
- C-tier discovery and D-tier manual notes require corroboration before high-confidence conclusions.
- Public quote proxies are monitor inputs, not substitutes for exchange settlement or audited transaction data.
- Empty user-file inbox is a healthy `ready/idle` state, not a blocker.
- A soft-removed source remains addressable by exact id for audit, but cannot be scheduled, fetched, imported, alerted on, or used to qualify a current forecast.

## Operational Boundaries

- Preserve attribution, evidence URL, fetch time, observed time and classification revision.
- Use conservative request rates and bounded retries.
- Never bypass login, CAPTCHA, paywalls or access controls.
- Do not silently convert monthly data into daily observations or forward-fill missing publication periods.
- Label MEG/POY/DTY public spot rows as valuation/assessment observations, not exchange-traded transaction prices.

## Related Docs

- `docs/api.md`
- `docs/news-ingestion-system.md`
- `docs/price-freshness-policy.md`
- `docs/release-checklist.md`
- `server/source_registry.json`

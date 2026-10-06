# ADR 0004: Public Benchmark v2 and CCF Soft Removal

## Status

Accepted — 2026-08-30

Amended — 2026-08-31: `public-benchmark.v2` remains the current unattended
**input-health/context** contract. ADR-0001 seven-product is the current prediction
contract; the derived cost-pressure targets below are historical/context outputs and
cannot receive current formal forecast status.

Amended — 2026-09-05: “single operator” no longer implies “non-commercial.” The
workbench may support production and procurement decisions. Internal license/manifest
approval ceremony remains retired. The current system enforces access and eligibility
boundaries and records source terms in metadata, but it does not yet encode every
storage, display, attribution, commercial-use, and redistribution limit as a structured
capability. ADR-0005 requires that stronger contract for the planned intelligence domain.

## Context

This repository is a single-operator personal workbench used for production and procurement research. Publicly visible inputs do not require an internal license, manifest, authorization, or approval ceremony before technical evaluation. Public visibility does not itself grant copying, retention, redistribution, or commercial-use rights; applicable source terms remain constraints even where the current registry records them only as notes. Existing access controls and eligibility gates must still be respected, and richer machine-enforced rights capabilities belong to the planned ADR-0005 domain rather than the current implementation.

The legacy 19-series contract and historical CCF rows are already part of recorded experiments. Reinterpreting or deleting them would destroy reproducibility. New unattended runs need a smaller public-only contract and must not depend on a manual CCF channel.

## Decision

### Current input-health contract

Add `public-benchmark.v2` as the only current unattended benchmark input contract. Its nine inputs are:

1. Brent public futures proxy (`USD/bbl`)
2. WTI public futures proxy (`USD/bbl`)
3. Naphtha public assessment/proxy (`USD/mt`)
4. PX public main-futures proxy (`CNY/mt`)
5. PTA public main-futures proxy (`CNY/mt`)
6. MEG public main-futures proxy (`CNY/mt`)
7. POY public assessment (`CNY/mt`)
8. DTY public assessment (`CNY/mt`)
9. CFETS USD/CNY reference rate (`CNY per USD`)

It exposes two derived targets: POY upstream-cost pressure and DTY upstream-cost pressure. Derived values must identify their formula version and input observation timestamps.

Each input declares its source cadence, collection polling interval, expected availability, and maximum tolerated calendar age. Freshness is evaluated per input; there is no universal “daily” threshold. A missing or stale required input blocks the current benchmark, while a source returning no new observation within its declared cadence is not automatically a collection failure.

### CCF lifecycle

CCF is soft-removed:

- Existing database rows and legacy artifacts remain unchanged and readable for historical reproduction.
- Registry entries remain addressable by exact ID and are marked `soft_removed`.
- Default registry listings, crawler pipelines, acquisition plans, fetch/import entrypoints, daily jobs, scheduler status, alerts, freshness gates, readiness, and `public-benchmark.v2` exclude CCF.
- Compatibility summaries may report `soft_removed`; they must not enqueue work, request human action, write CCF data, or degrade/block a current run.
- Re-enabling CCF requires a new ADR and an explicit operator decision; rollback of this release does not delete history.

### Governance fields

Legacy fields that represented an internal permission, manifest, or manual authorization workflow remain parseable for one compatibility version and historical artifacts. That internal ceremony is deprecated and ignored by benchmark qualification; this does not waive applicable external source terms. Current execution enforces existing access and qualification boundaries and records source terms in registry metadata; it does not claim complete structured enforcement of storage, display, attribution, commercial-use, or redistribution rights. Current outputs use technical provenance: source ID and URL, retrieval/observation timestamps, content or release hashes where available, transformation/formula version, recorded source terms, and quality/freshness status. ADR-0005 defines the stronger future capability model.

`PERSONAL_MODE` controls deployment warnings and operator-facing behavior only. It never changes source eligibility or benchmark qualification.

### Scheduler matrix

Production enables backend, frontend, public intraday collection, public source automation, daily orchestration, morning brief catch-up, and an external/local public-health probe. Experience settlement and event-summary workers remain disabled until separately accepted. CCF has no enabled job.

All schedulers record every attempt. Success, no-new-data, degraded, timeout, and failure are distinct states. Retry work must fit inside the caller's outer deadline.

## Interfaces and Data

- Add a versioned read API for the `public-benchmark.v2` contract and its current qualification snapshot.
- Add an operational-status field to source registry responses, defaulting to `active`; exact-ID lookup retains soft-removed records.
- Delivery-quality reports move to a new schema version, contain public per-series freshness, and list CCF only under legacy/soft-removed metadata.
- No production database migration or rewrite is required for CCF history.

## Rollback

Deploy as an immutable release with git SHA and CI provenance. Keep the previous release and environment backup. On failed health, daily acceptance, migration, or rollback verification, switch the `current` symlink to the previous release and reload the prior launchd definitions. Database restoration is not part of normal rollback because this change does not migrate or delete historical rows.

## Acceptance Matrix

| Requirement | Direct evidence |
| --- | --- |
| CCF is soft-removed | Unit/API tests prove exact lookup remains, default listing and all new run plans exclude it, and compatibility status is non-blocking/no-write. |
| Nine-input contract | Contract tests assert exact IDs, units, cadence metadata, two derived targets, and OpenAPI parity. |
| Freshness by cadence | Boundary tests cover fresh, stale, no-new-data, weekend/weekly cadence, and missing input. |
| Safe rerun | Same-day daily/automation integration test runs twice without duplicate logical output or CCF work. |
| Deadline/retry | Tests prove retry attempts stop before the outer daily deadline and timeouts are reported. |
| Honest metrics | Failure and timeout tests update last-attempt/status/failure counters without advancing last-success. |
| Public listener | Node test executes through a symlink and proves the main-module check starts rather than exits zero. |
| Release provenance | Management tests assert git SHA, branch, CI run ID, tree hash, and rollback target metadata. |
| Production delivery | Fresh CI gates plus loopback/public health, login, API, metrics, first successful daily run, snapshot, quality report, and morning brief. |

## Consequences

The old 19-series and CCF-backed results remain reproducible but are not evidence that the current unattended benchmark is ready. Public data can be technically evaluated without an internal approval ceremony, subject to applicable source terms, existing qualification rules, technical access controls, paywalls, CAPTCHAs, login boundaries, and source availability.

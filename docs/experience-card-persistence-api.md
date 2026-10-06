# Experience Card Persistence v26

This package adds local persistence primitives only. It does not expose HTTP routes, run a scheduler, or connect to a production database.

## Contract

- Formal checkpoints are exactly D1, D7, and D30. D14 remains historical and read-only.
- Maturity names are `d1_preliminary`, `d7_intermediate`, and `d30_mature`.
- A revision chain starts at D1. A successor may be a same-stage correction with a new calculation fingerprint, or the next checkpoint. Skips and regressions are rejected.
- POY and DTY have distinct card identities and revision chains.
- Prediction-time inputs use `as_of_time`; posterior visibility uses the separately persisted zoned `evaluation_as_of`.
- Pending and transition-blocked computations have no card and are not persisted.
- An actual unscorable card may be stored only as diagnostic evidence: `diagnostic_only=true`, non-empty `exclusion_reasons`, `mechanism_support_status=inconclusive`, no retrieval eligibility, and no reusable experience. A scorable card requires the inverse diagnostic state, empty exclusions, and a conclusive mechanism status.

## Storage

Schema migration v26 creates `experience_card_revisions`. Rows are append-only. Database triggers reject updates, deletes, non-D1 roots, cross-card predecessors, forks, maturity skips, maturity regressions, and evaluation-time regressions. Table checks bind each horizon to its maturity name and reject D14.

`save_experience_card_revision` uses `BEGIN IMMEDIATE`. The exact retry key is `(experience_card_id, calculation_fingerprint)`. A retry is idempotent only when its canonical payload, SHA-256, and frozen identity are identical. A competing successor fails with `stale_experience_revision` and must be recomputed from the new head.

`save_experience_card_revision_batch` is the candidate-level write primitive used by deterministic settlement plans. It requires one non-empty, ordered revision chain with one frozen Experience identity and one `evaluation_as_of`. While holding `BEGIN IMMEDIATE`, it preflights every revision ID and checks the actual database head; all new actions commit together or all roll back. Complete exact replay returns `unchanged`. A mixed existing prefix or concurrent losing successor returns `stale_experience_revision`; a revision ID already bound to different content returns `experience_revision_id_conflict` instead of leaking a SQLite uniqueness error. Both require a full candidate reload and replan.

Batch atomicity intentionally stops at the candidate boundary. POY and DTY are different card identities and use independent transactions; a failure in one candidate does not roll back a candidate already committed by the caller. Pending, blocked, unavailable, and unchanged settlement items bypass the write primitive entirely.

Payloads use UTF-8 canonical JSON with sorted keys, compact separators, and no non-finite numbers. `payload_sha256` is a lowercase 64-character hexadecimal SHA-256. Every read recomputes the hash, verifies canonical encoding, and compares indexed identity fields to the payload.

The read primitives are:

- `get_experience_card_revision(revision_id)`
- `get_experience_card_head(experience_card_id)`
- `list_experience_card_revisions(experience_card_id)`

There are intentionally no update or delete primitives.

## Migration and operations boundary

Migration v26 first verifies the frozen v25 governance manifest and executes the full v25 recovery-proof audit before applying DDL in one immediate transaction. It records exactly one migration row, validates foreign keys and the v26 Experience schema manifest, and then commits. Every connection to a current v26 database reruns the v25 recovery proof and revalidates every Experience payload hash, frozen column, chain transition, and head inside `BEGIN IMMEDIATE`; this happens even when the process-local migration cache is warm.

Production migration requires a separate approval, backup, maintenance, and rollback plan. Tests use only temporary SQLite files. Internal revision/head read routes already exist; settlement/write HTTP, real candidate assembly, and deterministic external scheduling remain later packages. They must pass an explicit zoned `evaluation_as_of` and must never add a `force` path that bypasses checkpoint maturity.

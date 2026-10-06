# Deterministic Experience Settlement Planner

`server/app/experience_settlement.py` is a pure application-layer planner. It has no database, HTTP, network, file, LLM, scheduler, or system-clock dependency. A caller must supply `evaluation_as_of` and prediction `as_of_time` as canonical, directly persistable RFC3339 strings: uppercase `T`, seconds, and either `Z` or an offset containing a colon.

## Required adapter input

Each candidate contains:

- the structured Phase A prediction bundle;
- target and benchmark observation revision envelopes;
- at least 30 ordered effective observation dates;
- the current immutable Experience revision, or null;
- explicit prediction, target-observation, and benchmark-observation availability statuses;
- explicit target and benchmark series eligibility statuses.

The candidate collection and all point/date collections must be non-string sequences. Every candidate, prediction bundle, target point, and benchmark point must be a mapping; effective dates must be strings. Malformed adapter values always raise `ExperienceSettlementInputError` rather than leaking `TypeError` or relying on mapping coercion.

The planner never derives a target from rationale text, legacy prediction rows, calendar-day distance, proxy data, or missing observations. It does not accept `force`.

Only `available` prediction/observation inputs and explicitly `eligible` target and benchmark series may produce settlement actions. `unavailable` produces a stable unavailable result. Any blocked, contractible-only, unknown, or otherwise non-eligible status produces a blocked result. Both cases have an empty action list.

## Planning behavior

Candidates are sorted by `(prediction_batch_id, node_id, subtarget, target_series_id)`, exactly matching `experience-identity.v1`, and duplicate identities are rejected. POY and DTY therefore remain independent candidates and cards. `benchmark_series_id` is a frozen calculation input but is not part of the currently accepted Card identity; changing it may create a same-card revision, not a second Card.

The planner delegates price visibility and metrics to the accepted pure Experience runtime:

- prediction anchors resolve at prediction `as_of_time`;
- posterior revisions resolve at `evaluation_as_of`;
- effective observation dates, not calendar distance, determine maturity;
- D14 is rejected before availability handling.

When a run is late, the plan emits ordered storage-ready actions without skipping revisions:

```text
no head → D1 → D7 → D30
D1 head → D7 → D30
D7 head → D30
D30 head → D30 re-evaluation for a possible same-stage correction
```

A mature D30 head with unchanged selected inputs returns `unchanged` and no action. A newly visible observation revision produces exactly one D30 action whose expected previous revision is the current D30 head.

An action includes the checkpoint, expected previous revision ID, and complete card payload. Before any write, the storage adapter parses the RFC3339 cutoff as a real calendar timestamp, requires all five planned-item upstream statuses to remain `available`/`eligible`, and rejects a revision ID reused anywhere else in the complete plan. The adapter then appends every action for one candidate in a single `BEGIN IMMEDIATE` transaction. A late D1→D7→D30 catch-up therefore commits as one candidate chain or leaves that candidate unchanged. POY, DTY, and other candidate identities use separate transactions and remain independent.

Exact replay of a complete candidate batch returns `unchanged` only when every canonical payload and frozen identity matches. A mixed existing prefix or competing head rolls back the candidate and raises `stale_experience_revision`; an already-used revision ID with different content raises `experience_revision_id_conflict` before that candidate inserts anything. The caller must reload the head and all upstream inputs and create a new plan. The adapter does not hide retries or patch a stale plan in place.

`evaluation_as_of` remains an explicit caller-supplied cutoff. The adapter never obtains it from the system clock and requires every action card to carry the exact plan value. Pending, unchanged, blocked, and unavailable items have no actions and perform no writes. A built unscorable card may still be persisted as diagnostic evidence under the existing v26 non-retrieval contract. The provisional maturity calendar is `china-weekday-business-days.v1`: Monday through Friday only, strictly after the prediction date. It intentionally does not model statutory holidays or make-up workdays; any later official-calendar replacement must use a new version.

## Output boundary

The `experience-settlement-plan.v1` output contains per-candidate status, stable reason codes, explicit upstream statuses, ordered actions, and pending checkpoint information. `server/app/experience_candidate_adapter.py` is a pure read-only adapter for one explicit verified prediction payload, one evaluation snapshot, an explicit common-effective-date calendar, and explicit series statuses. `server/app/experience_candidate_loader.py` is the narrow persisted-input boundary: callers supply the prediction revision, evaluation snapshot and cutoff; it re-audits the revision and its bound eligibility assessment, rejects a snapshot after that cutoff, and then delegates to the pure adapter. It derives the series statuses from all three audited formal horizons; a caller-supplied status map must exactly agree or is rejected. It also resolves the existing immutable card head by stable identity rather than accepting a caller-supplied predecessor. Neither layer chooses persisted inputs nor upgrades their statuses. The adapter derives the PTA/MEG target with immutable capture lineage and uses independent POY/DTY assessment benchmarks. `server/app/experience_settlement_storage.py` provides the internal plan-to-storage boundary. The explicit service entrypoint remains available, while `server/app/experience_settlement_selection.py` now chooses the latest re-audited prediction revision and snapshot that were both available no later than a caller-supplied cutoff. It never reads the system clock or upgrades eligibility. Its selected-service entrypoint returns an explicit blocked, zero-write result if either input is unavailable by that cutoff; otherwise it delegates the immutable IDs to the existing service using the versioned weekday calendar. `experience_settlement_scheduler.py` can invoke that path on weekdays at 09:30 Asia/Shanghai, but is disabled by default and intentionally skips a missed cutoff rather than settling with a later clock time. No HTTP write route or production authorization exists.

Current Phase A formal target and price series remain blocked, so production inputs correctly yield blocked plans until governance explicitly marks the required prediction, observations, target series, and benchmark series usable and eligible.

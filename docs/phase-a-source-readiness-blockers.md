# Phase A unresolved source readiness

This note records the locally auditable portion of GitHub issues #20 and #21. It does not approve a
source, publish a manifest, change the production trust root, or authorize collection.

## Machine-readable readiness

`server/app/formal_source_readiness.py` emits deterministic, read-only readiness records for exactly
six exchange/benchmark series and eight domestic industrial assessment series. Every record remains
`blocked`, is observation-only, and carries its candidate source contracts, contract digest, owner,
lineage, freshness, visibility/calendar fields, and retention/revision policy. Candidate registration
is not capture evidence and cannot emit an eligibility bundle.

The six exchange/benchmark series remain blocked on immutable captures and instrument, settlement,
visibility, calendar, and roll evidence. EIA spot data is not substituted for ICE/CME settlement data,
and INE/SC is outside the current formal closure.

The domestic CCF naphtha and PX candidates remain observation-only because their captured international
USD/CFR basis does not prove the requested domestic CNY assessment. Coal and MX retain named candidates
but remain blocked pending exact capture/specification/visibility/calendar evidence. Ethylene, EO,
polyester melt, and polyester chip remain `no_exact_source` pending an accepted exact source and grade.

Range assessments preserve both bounds. Their arithmetic midpoint is explicitly derived and is never
labelled as a transaction price.

## Frozen Chinese futures roll audit

`server/app/formal_continuous_roll.py` is a pure candidate audit for ZCE PX/MA/TA and DCE EG. It applies
`china-futures-main-continuous.v1`: open-interest/volume/near-expiry ranking, two consecutive trade-day
confirmation with next-trade-day switching, final-month forced switching, official settlement only,
and no back adjustment or synthetic fill. Inputs bind immutable capture IDs and raw/canonical hashes.

The audit reports stable blockers for missing contract days, late publication, source/instrument/unit
mismatch, duplicate or retroactively revised contract-day captures, and missing roll contracts. Its
output is candidate evidence only and always leaves formal eligibility false. Caller-supplied capture
hashes, calendars, and cutoffs cannot self-approve: until they are bound to an independently audited
append-only ledger and frozen calendar proof, the audit reports `append_only_ledger_proof_missing` and
`calendar_cutoff_proof_missing` even when the roll calculation itself is complete.

## Remaining external blockers

- Obtain exact immutable captures and content-addressed authorization evidence from each accepted source.
- Freeze exchange calendars and prove instrument/settlement mappings against those captures.
- (Retired 2026-08-28) Per-series evidence dossier approval is no longer required; data-quality gates and point-in-time correctness remain mandatory.
- Resolve exact domestic sources and grade/basis definitions for all still-unresolved assessment series.

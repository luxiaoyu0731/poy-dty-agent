---
name: release-gate-review
description: Produce a read-only, evidence-based release gate decision for this project. Use before local, staging, or production release; when auditing CI evidence; or when deciding GO, GO WITH WARNINGS, or NO-GO.
---

# Release Gate Review

Read `docs/release-checklist.md`, `.review/policies/production-review-policy.yml`, `AGENTS.md`, and relevant deployment/runbook documents. Never deploy, modify code, accept risk, or mark a checklist item complete on the user's behalf.

1. Define the exact artifact, commit, environment, and release scope. If unknown, record the decision as blocked.
2. Record `git rev-parse HEAD`, worktree cleanliness, target environment, artifact identity, and evidence timestamps. A dirty tree is not equivalent to the commit under review.
3. Collect fresh mechanical evidence before semantic conclusions. Prefer `npm run review:full`, then add `npm run test:e2e`, `npm run visual:check`, relevant AI/RAG evals, API-contract evidence, and current CI status.
4. Verify environment security, licensed source authorization, migrations, durable storage/backup and restore, observability, fallback behavior, rollback artifacts, and customer-facing boundaries.
5. Require every checklist claim to link to a command result, report, configuration location, or explicit human acceptance with owner/date. Distinguish fresh evidence from inherited or stale reports.
6. Apply severity consistently: any P0/P1, confirmed secret, auth bypass, failing required gate, unknown target artifact, unreviewed migration, or untested rollback yields `NO-GO`.
7. Use `GO WITH WARNINGS` only for bounded P2/P3 risks with explicit owner, acceptance, and revisit date. Missing evidence is not acceptance.

Output the decision first, followed by blocking findings, warnings, passed gates, unavailable or stale evidence, rollback readiness, and required next actions. Include timestamps and commit identity when available.

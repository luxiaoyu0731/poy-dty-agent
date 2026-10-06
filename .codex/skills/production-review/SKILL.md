---
name: production-review
description: Orchestrate a read-only, evidence-based production code review across architecture, security and supply chain, tests, API contracts, performance and observability, and release readiness. Use for full repository reviews, pull-request readiness, production audits, or ship/no-ship assessments.
---

# Production Review

Review without modifying code, configuration, baselines, or generated artifacts. Ask before any fix.

## Workflow

1. Read `AGENTS.md`, `.review/policies/production-review-policy.yml`, the requested diff or scope, and the relevant project documentation.
2. Establish the baseline and review range with `git status --short`, `git diff --stat`, and the user-specified base. Preserve unrelated worktree changes.
3. Run available deterministic checks first. Prefer `npm run review:quick` for changed-code feedback, `npm run review:security` for security gates, and `npm run review:full` for release evidence. Record commands, exit codes, and concise output; a missing CLI or skipped suite is unavailable evidence, not a pass.
4. Invoke or apply these project skills as relevant:
   - `$architecture-review`
   - `$security-supply-chain-review`
   - `$test-adequacy-review`
   - `$api-contract-review`
   - `$performance-observability-review`
   - `$release-gate-review`
5. Trace changed behavior through callers, persistence, API boundaries, tests, operational signals, data provenance, AI/RAG evaluation, and rollback paths.
6. Deduplicate findings and distinguish defects, policy violations, accepted baseline debt, and missing evidence. Do not suppress, baseline, or accept risk without explicit authorization.

## Severity

- P0: active compromise, destructive data loss, or unsafe release requiring immediate stop.
- P1: likely security, correctness, compliance, or availability failure; block release.
- P2: material maintainability, coverage, contract, or operational weakness; fix before normal release unless explicitly accepted.
- P3: bounded improvement that does not presently invalidate release.

## Output

Lead with findings ordered P0 to P3. For each, provide a short title, impact, reproducible evidence, and the smallest safe remediation. Cite exact file and line when possible. Then list checks run, checks unavailable, assumptions, and residual risks. End with `GO`, `GO WITH WARNINGS`, or `NO-GO`; never infer success from missing evidence.

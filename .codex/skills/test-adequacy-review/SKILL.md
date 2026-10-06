---
name: test-adequacy-review
description: Assess whether frontend, backend, API, AI, E2E, and visual tests adequately cover changed behavior and failure modes. Use for pull requests, regressions, coverage gaps, flaky tests, test-quality reviews, or production-readiness audits.
---

# Test Adequacy Review

Review only; do not add or update tests unless separately asked.

1. Read the diff and map every behavior change to observable outcomes and failure modes.
2. Run the narrowest relevant deterministic tests first. Use `uv run pytest tests/<focused-file>` from `server/` when `uv` is available; otherwise use `python3 -m pytest server/tests/<focused-file>`. Broaden proportionately with `npm run check`, `npm run assets:check`, `npm run test:e2e`, `npm run visual:check`, and `python3 -m pytest server/tests`.
3. Verify backend behavior changes have tests in `server/tests/` and user-visible flows have E2E coverage in `tests/`, per `AGENTS.md`.
4. For AI or retrieval behavior, run the relevant `npm run eval:ai` or `npm run eval:rag` suite and inspect `docs/evals.md`; do not substitute snapshot agreement for factual or citation quality.
5. Check happy paths, invalid input, auth boundaries, empty/stale/fallback states, persistence and rollback, timeouts, retries, concurrency, scheduler idempotency, temporal leakage, and evidence/citation downgrade behavior.
6. Evaluate assertions, fixtures, isolation, determinism, test data realism, and whether mocks conceal integration failures. Require regressions to fail before the fix when feasible.
7. Treat coverage percentages as supporting evidence, not proof. Identify uncovered changed branches and assertions capable of detecting plausible mutations.

Report P0–P3 gaps with the behavior at risk, exact source/test evidence, and a concrete missing test scenario. Record commands, failures, skipped suites, and environmental limits.

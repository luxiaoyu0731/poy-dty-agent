---
name: performance-observability-review
description: Review performance, reliability, and observability readiness for the React/FastAPI application. Use for latency or throughput changes, database and retrieval work, external calls, bundles, metrics, logs, health checks, SLOs, alerts, or operational-readiness reviews.
---

# Performance and Observability Review

Read `docs/observability.md` and affected hot paths. Review without changing instrumentation or thresholds.

1. Run focused tests and `npm run check`; use `npm run visual:check` for rendering-sensitive changes. Search for project benchmarks or load tests before claiming they exist, and report unavailable measurements.
2. Examine algorithmic growth, request fan-out, database query/index behavior, payload size, rendering cost, cache bounds, connection reuse, timeouts, retries, backoff, cancellation, and resource cleanup.
3. Require measurement before asserting a regression. Distinguish measured, inferred, and hypothetical risks.
4. Verify request IDs, structured logs, bounded cardinality, error context without secrets, and metrics for HTTP, LLM, retrieval, source fetch, scheduler, fallback, and rate limiting. Ensure prompts, retrieved documents, tokens, and credentials are not logged verbatim.
5. Check live, ready, and deep health semantics; ensure readiness reflects required dependencies without making liveness fragile. Verify scheduled jobs expose last success, duration, backlog, and freshness.
6. Review frontend chunk and asset growth from build output, long-list rendering, request waterfalls, and polling cleanup; distinguish transfer size from parsed/runtime cost.
7. Compare behavior with documented p95, availability, fallback, and freshness targets. Verify alerts have an owner, actionable threshold, runbook path, and rollback evidence.

Report P0–P3 findings with file/line or measurement evidence, affected SLI/SLO, and a testable remediation. Include commands and measurement limitations.

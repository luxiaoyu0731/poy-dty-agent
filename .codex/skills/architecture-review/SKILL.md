---
name: architecture-review
description: Perform a read-only architecture and maintainability review of this React/FastAPI system. Use for design reviews, boundary violations, dependency direction, persistence and RAG changes, oversized modules, concurrency, data-flow, or architectural regression analysis.
---

# Architecture Review

Read `docs/architecture.md`, relevant ADRs in `docs/adr/`, and the affected implementation before judging structure. Do not edit files.

1. Run available mechanical checks first: type checks, import or dependency checks, and focused tests.
2. Map entry points, callers, state ownership, persistence, external I/O, and failure paths.
3. Verify boundaries among React UI, `src/services/api.ts`, FastAPI routes/services, SQLite storage, source registry/fetchers, retrieval/GraphRAG, forecasts, and LLM execution. Keep policy and evidence decisions server-side.
4. For agent or RAG changes, also read the relevant `docs/agent-*.md`, `docs/graphrag.md`, `docs/rag-*.md`, and `docs/evals.md`. Ensure retrieved or model-generated content remains untrusted and cannot become instructions or unsupported facts.
5. Check dependency direction, hidden coupling, duplicated policy, circular imports, global mutable state, transaction boundaries, idempotency, timeout/cancellation handling, scheduler overlap, and migration compatibility.
6. Trace data time semantics (`as_of`, freshness, event time versus ingestion time) and provenance across storage, retrieval, API, and UI.
7. Flag large modules only when evidence shows mixed responsibilities, high change coupling, unsafe test seams, or ownership ambiguity; propose an incremental seam rather than a wholesale rewrite.

Report P0–P3 findings with exact file/line evidence, impact, and a minimal remediation. Separate confirmed defects from architectural debt and missing evidence. List commands run and unverified assumptions.

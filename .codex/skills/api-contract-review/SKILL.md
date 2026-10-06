---
name: api-contract-review
description: Review API implementation, documentation, and OpenAPI consistency without modifying files. Use for FastAPI route or schema changes, client changes, contract drift, breaking-change analysis, authentication requirements, error envelopes, or API release readiness.
---

# API Contract Review

Read `docs/api.md` and `docs/openapi.yaml`, then inspect affected FastAPI routes, Pydantic models, clients, and tests.

1. Parse `docs/openapi.yaml`, inspect the generated FastAPI schema when the app can start, and run focused tests in `server/tests/test_api.py` plus affected client/E2E tests before semantic review. If no automated diff command exists, report that gap rather than inventing one.
2. Compare paths, methods, operation IDs, parameters, request/response schemas, status codes, content types, security requirements, and deprecations.
3. Trace frontend/client assumptions to runtime responses. Verify optionality, nullability, enums, timestamps, pagination, streaming behavior, and standard errors.
4. Check `/api/v1` versioning, internal route protection by local session or `X-Internal-Token`, `X-Request-ID`, documented rate limits, streaming termination/error semantics, and browser CORS expectations.
5. Verify time and evidence fields preserve documented meaning: `as_of`, source identity, freshness, citations, confidence, and fallback/degraded status must not be silently dropped or reinterpreted.
6. Classify removals, narrowing schemas, new required fields, semantic changes, status-code changes, and formerly synchronous behavior becoming asynchronous as potential breaking changes.
7. Require implementation, `docs/openapi.yaml`, `docs/api.md`, frontend client types, and contract tests to agree. Do not infer compatibility only because TypeScript compiles.

Report P0–P3 findings with exact locations on both sides of each mismatch. State the compatibility impact, affected consumers, checks run, and uncertainties. Do not regenerate the contract automatically.

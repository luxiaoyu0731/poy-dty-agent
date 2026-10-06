# ADR 0003: Production Readiness Boundary

## Status

Proposed

## Context

The prototype now has versioned APIs, internal token enforcement, LLM fallback, evals, source registry guardrails, and Prometheus-style metrics. Moving beyond staging requires a clear boundary between local convenience and production safety. This project is a single-researcher oil-price research tool, so readiness should be evaluated as stable personal deployment rather than team-operated SaaS.

## Decision

Use `/api/v1` as the production API boundary. Staging and production must enforce internal tokens, explicit CORS, rate limits, OpenAPI contract coverage, eval gates, dependency/security scans, health checks, and metrics scraping before release.

SQLite remains acceptable for local/staging trace and audit data, but production multi-instance deployments require durable storage or a managed database adapter.

## Consequences

- Frontend clients should target `/api/v1`.
- Legacy `/api` remains only as a compatibility path during migration.
- Deployments with `APP_ENV=staging|production` fail fast unless internal auth is configured securely.
- The personal release check must include managed secrets, data licensing, observability, backup/restore awareness, and a rollback path.

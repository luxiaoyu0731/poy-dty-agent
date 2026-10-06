# ADR 0001: API Versioning And Security Boundary

## Status

Proposed

## Context

The current API is a prototype under `/api` with no authentication. Production use will include source fetching, internal notes, LLM answers, and prediction records.

## Decision

Introduce `/api/v1` before external release and require authentication for any state-changing, source-fetching, or internal-data endpoint. Public health endpoints remain unauthenticated.

## Consequences

- Frontend API clients must target versioned routes.
- Tests should lock response contracts before migration.
- Source and assistant routes can receive route-level RBAC and rate limits.

# Release Plan

## Environments

- Local: demo fixtures and optional backend.
- Staging: real API process, restricted CORS, seeded data, eval traces enabled.
- Production: managed secrets, source licenses, monitoring, alerting, rollback plan.

## Release Gates

- `npm run check`
- `cd server && uv run pytest tests`
- `npm run test:e2e`
- npm audit with the official npm registry
- Python dependency audit with `uv run pip-audit`
- CodeQL or equivalent static analysis in CI
- Visual regression review for UI changes
- AI eval fixture pass for prompt or LLM changes
- `/api/v1/health/ready` and `/api/v1/health/deep` pass in staging
- `ENFORCE_INTERNAL_TOKEN=1` and managed `INTERNAL_API_TOKEN` in staging/production

## Rollback

- Keep frontend and backend deploys independently reversible.
- Feature-flag external source fetches.
- Keep LLM fallback available during provider outages.

## Ship Status

This repository should be considered staging-ready only when the release checklist in `docs/release-checklist.md` is complete. Production here means a stable personal deployment for one researcher; it requires managed secrets, confirmed data-source authorization, basic observability, backup awareness, and a rollback path, not a team release process.

# Release Checklist

This checklist is scoped for a single-researcher deployment. It keeps the release
discipline needed for a stable research tool, but does not require team on-call,
formal cross-team approval, or support rotation.

## Preflight

- [ ] `npm run local:preflight` completed and `.codex-run/local-production/preflight/latest-preflight.md` reviewed
- [ ] `npm run local:prod:dry-run` completed before enabling unattended apply mode
- [ ] `npm run local:daily` completed and `.codex-run/local-production/10-final-local-production-gate.md` reviewed
- [ ] `npm run local:alerts` completed and `.codex-run/local-production/alerts/latest-alerts.json` reviewed
- [ ] `.codex-run/local-production/09-final-local-unattended-production-report.md` reviewed for local GO/GO WITH WARNINGS/NO-GO
- [ ] `.codex-run/local-production/LOCAL_PRODUCTION_README.md` reviewed before enabling launchd templates
- [ ] `npm run check`
- [ ] `cd server && uv run ruff check .`
- [ ] `cd server && uv run pytest tests`
- [ ] backend is running on `127.0.0.1:8000`, then `npm run test:e2e`
- [ ] `npm run visual:check` passes for UI changes (CI fails on unaccepted differences)
- [ ] `npm run eval:ai`
- [ ] `npm audit --registry=https://registry.npmjs.org --omit=dev`
- [ ] `cd server && uv run pip-audit`
- [ ] CI CodeQL or equivalent static analysis completed

## Environment

- [ ] `APP_ENV=staging` or `APP_ENV=production`
- [ ] `ENFORCE_INTERNAL_TOKEN=1`
- [ ] `INTERNAL_API_TOKEN` comes from a managed secret and is not empty
- [ ] Active-source runtime preflight passes; EIA key is optional under the current product scope and DCE/CCF are not active sources; no credential value appears in reports
- [ ] Public proxy uses `PUBLIC_AUTH_MODE=single_user_password` with a non-empty approved scrypt verifier
- [ ] `PUBLIC_CANONICAL_ORIGIN` is the exact HTTPS production origin
- [ ] Anonymous `/`, static assets, `release.json`, `/api/**`, and `/metrics` requests are rejected; only `/healthz` is public
- [ ] Login, expiry, CSRF, logout, restart invalidation, rate limit, and spoofed-header tests pass
- [ ] `DEEPSEEK_API_KEY` comes from a managed secret when provider-backed answers are enabled
- [ ] `CORS_ALLOW_ORIGINS` is restricted to deployed frontend origins
- [ ] `OUTBOUND_FETCH_HOSTS` contains only authorized source hosts
- [ ] Docker/compose binds backend to loopback or a private network, not public `0.0.0.0:8000`
- [ ] SQLite uses `SQLITE_PATH=/data/agent.db` on a durable `/data` volume and has a fresh online, integrity-checked backup

## API And Security

- [ ] Clients use `/api/v1`
- [ ] `docs/openapi.yaml` matches implemented API surface
- [ ] Internal routes require `X-Internal-Token`
- [ ] Errors include `X-Request-ID` and standard error envelope
- [ ] Chat and source fetch rate limits are configured
- [ ] No connector bypasses login, paywalls, CAPTCHAs, access controls, or source rate limits
- [ ] `/api/v1/delivery/status` contains no CCF acquisition, freshness, alert or readiness work item
- [ ] OPEC is healthy through the existing news scheduler; no duplicate OPEC fetch loop is enabled
- [ ] OFAC baseline/delta fetch succeeds without storing full names or addresses in business tables or RAG
- [ ] GACC and UN Comtrade monthly observations preserve source period, unit, partner and classification semantics
- [ ] User CSV/XLSX replay is idempotent; PDF/image inputs produce review reports without business-data writes
- [ ] Empty `.codex-run/user-files/` is reported as healthy `ready/idle`
- [ ] `GET /api/v1/forecasts/seven-product` returns exactly crude/naphtha/PX/PTA/MEG/POY/DTY × D1/D7/D30 and never promotes legacy records
- [ ] `GET /api/v1/forecasts/seven-product/history` matches the immutable issued batch and exposes no actual visible at or before issue time
- [ ] Seven-product lifecycle is exactly-once for the Shanghai business date; a retry creates 0 batches and 0 duplicate outcomes
- [ ] `GET /api/v1/forecasts/seven-product/evaluation` reports 21 independent OOS cells and all 21 pass the frozen thresholds before formal release
- [ ] Every formal registry change comes from a hash-valid governance proposal; the exact proposed registry is reviewed and deployed in an immutable release
- [ ] Active formal champions take precedence over reference champions; feature/three-loss fallback is non-formal and visible in model/gap/configuration fields
- [ ] `server/scripts/run_delivery_data_quality_gate.py --warn-only` has been run after the latest import
- [ ] Frontend shows coverage, evidence grade, confidence boundary, update schedule and data gaps
- [ ] Frontend shows observation signals only and contains no procurement, quotation, order-taking or inventory instruction

## AI Agent

- [ ] Eval suite passes
- [ ] LLM traces persist provider, model, latency, token estimates, cited sources, fallback, and errors
- [ ] Fallback mode is enabled and tested
- [ ] Human review policy is accepted for high-impact or low-evidence conclusions

## Observability

- [ ] `/api/v1/health/live` passes
- [ ] `/api/v1/health/ready` passes
- [ ] Authenticated `/release.json` and both health endpoints expose identical `release_id`, `release_hash`, and `git_sha`; `release.status=unavailable` does not prove a version match
- [ ] `/api/v1/health/deep` passes
- [ ] `/metrics` is reachable or scraped by the chosen local/staging monitor
- [ ] Dashboard panels or saved queries exist for requests, latency, errors, LLM fallback, token estimates, and source fetches
- [ ] Source freshness and scheduler last-success/duration/backlog series are present; missing series are failed evidence
- [ ] Lightweight alerts or manual checks are defined for high error rate, high latency, and LLM fallback spike
- [ ] Freshness/scheduler alerts have the `single-researcher` owner and a working runbook path

## Rollback

- [ ] Previous frontend artifact is available
- [ ] Previous backend image is available
- [ ] Source fetches can be disabled without redeploying
- [ ] LLM provider mode can fall back safely
- [ ] SQLite restore command/path is tested against a temporary database with all writers stopped
- [ ] Rollback command/path is documented and tested once

## Ship Decision

- [ ] Local production gate is `GO` or all `GO WITH WARNINGS` items are explicitly accepted with owner/date
- [ ] Staging smoke test completed
- [ ] Secrets are configured outside git
- [ ] All current benchmark inputs are public and their technical access/rate-limit boundaries are confirmed
- [ ] DCE EG is absent from active source lists, schedules, fetch/import paths, alerts and credential gates; SunSirs is the only current MEG label source, its append-only first-capture evidence is present, and no futures proxy substitutes for it
- [ ] Seven-product forecast exposes D1/D7/D30 point, interval, direction, confidence, evidence and data-gap boundaries
- [ ] Authenticated public smoke validates the exact seven-product forecast, every returned history batch,
      evaluation and registry 7×3 contracts; pre-auth failures are stage-coded and sanitized; its `0600`
      content-addressed report matches `public-smoke-latest.json`
- [ ] Public health probe survives the bounded Tunnel startup window, writes owner-only crash-durable latest status,
      and a sustained outage still exits non-zero
- [ ] Frozen release fault matrix passes 15/15 and its content-addressed report is reviewed
- [ ] The current release has completed its first real daily production run; no historical waiting window is substituted
- [ ] Schema v35 pre-v36 backup is verified, v36 outcome invalidations are readable, and the first real 21-cell issued batch is present
- [ ] The current product brief and public-benchmark contract are reviewed; no historical CCF/backtest or execution-strategy material is presented as current
- [ ] Backup/restore path is understood for trace and audit storage
- [ ] Remaining accepted risks are written down with a revisit date

## Public Delivery Inputs

- [ ] Domain and final customer URL confirmed
- [ ] Hosting target confirmed
- [ ] Access-control mode confirmed: VPN, basic auth, named accounts, or customer SSO
- [ ] TLS/reverse proxy owner confirmed
- [ ] Public-source technical boundaries (login/paywall/CAPTCHA and host allowlist) confirmed for the deployment environment
- [ ] Production secrets configured outside git
- [ ] Backup target and retention confirmed
- [ ] Alert channel and response owner confirmed
- [ ] Customer SLA and accepted-warning policy confirmed

## Industrial Intelligence Center gates (schema v37)

Before enabling `INDUSTRIAL_INTELLIGENCE_ENABLED` in production:

- [ ] v37 migration rehearsal passed on a copy of the production database (old-table fingerprints unchanged, `user_version=37`, repeat-run idempotent)
- [ ] `pytest server/tests` includes the intelligence suites (migration, catalog/projection, brief, API, providers, isolation, OpenAPI parity)
- [ ] `docs/openapi.yaml` parity tests green for `/api/v1/intelligence/*`
- [ ] Ruff green; `npm run check`, `npm run assets:check`, production build green; map chunk is a separate lazy bundle
- [ ] `earthquake.usgs.gov` present in the managed `OUTBOUND_FETCH_HOSTS`; no other new outbound hosts
- [ ] Forecast track untouched: strict score remains 79.8/100, formal OOS remains 0/21; intelligence tables rejected from prediction/RAG read paths by test
- [ ] First production-day evidence targets a 09:30 non-`blocked` frozen brief; until then the module status stays "Local Implementation Complete / Release Candidate", never "Production Implemented"

# Agent Instructions

## Package Manager
- Use npm for the frontend: `npm install`.
- Use Python 3.11+ for the backend; prefer `uv run` when uv is available.

## Commands
| Task | Command |
| --- | --- |
| Frontend dev | `npm run dev` |
| Frontend check | `npm run check` |
| Asset check | `npm run assets:check` |
| Visual diff | `npm run visual:diff` |
| E2E smoke | `npm run test:e2e` |
| Backend dev | `cd server && uvicorn app.main:app --reload --port 8000` |
| Backend tests | `python3 -m pytest server/tests` |

## External References
| Need | File |
| --- | --- |
| Product scope | `docs/product-scope.md` |
| Setup | `README.md` |
| Visual regression | `docs/visual-regression.md` |
| Figma rebuild | `docs/figma-rebuild-guide.md` |
| Architecture | `docs/architecture.md` |
| API contract | `docs/api.md` |
| OpenAPI contract | `docs/openapi.yaml` |
| Security | `docs/security.md` |
| Observability | `docs/observability.md` |
| Deployment | `docs/deployment.md` |
| Release checklist | `docs/release-checklist.md` |
| Operations | `docs/runbook.md` |

## Key Conventions
- Do not edit `dist/`, `.visual-regression/`, `.review-screenshots/`, or generated screenshots by hand.
- This is a single-operator personal workbench. Connectors must never bypass logins, paywalls, or CAPTCHAs; license-approval ceremony for public data was retired on 2026-08-28 (see `docs/full-chain-prediction-status.md`).
- Add tests with behavior changes: backend API tests in `server/tests/`, E2E tests in `tests/`.

## AI Context Workflow
- Read `docs/ai-context.md` before non-trivial work; load only the references relevant to the current task.
- Start searches in the task's allowlisted files, then `src/`, `server/app/`, `server/tests/`, `tests/`, and relevant `docs/` paths.
- Do not scan `.codex-run/`, `node_modules/`, `server/.venv/`, generated outputs, evidence, screenshots, caches, or archives unless the task explicitly requires them.
- For non-trivial work, create a short task pack from `agent-context/TASK_TEMPLATE.md`; keep completed packs in `agent-context/` (gitignored, never committed).
- Keep task packs under 80 lines and separate verified facts, assumptions, open questions, scope, non-scope, and acceptance criteria.
- Reference large logs, patches, manifests, and evidence by absolute path, SHA-256, size, and summary; do not paste their full contents into chat.
- Treat audits, historical plans, and prior evidence as opt-in context, not current authority.
- End each task with a plain-language summary of actions, changes, verification, deviations, and remaining approvals.

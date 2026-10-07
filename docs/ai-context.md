# AI Project Context

This file is a compact navigation index for coding agents. It does not replace user decisions, API contracts, architecture documents, security policies, or task-specific approvals.

## Runtime Map

- Public frontend: `https://app.kaipingrc.com/`. Production moved to Tencent Cloud Hong Kong on 2026-09-22; runtime rechecked 2026-09-26 at `/opt/agent` (Docker Compose and systemd timers).
- The old Mac launchd installation is retired. Do not diagnose stopped Mac jobs as a production outage or restart Mac writers; verify the cloud runtime first. See `docs/cloud-daily-operations.md`.
- Frontend entry: `src/main.tsx`; application shell: `src/App.tsx`; API client: `src/services/api.ts`.
- Backend entry: `server/app/main.py`; settings: `server/app/settings.py`; persistence: `server/app/storage.py`.
- Backend tests: `server/tests/`; frontend and E2E tests: `tests/`.
- Frontend package manager: npm. Backend runtime: Python 3.11+ with the repository virtual environment or `uv` when authorized.
- Health surface: `/data/public-health/` holds `latest.json` (public probe), `chain-alert.json` (node A/B degradation), and `evidence-alert.json` (evidence-page starvation, streak-based; raised by `scripts/cloud/health-probe.sh` every 10 min). Daily governance posture is readable via `GET /api/v1/agent-governance/report/latest` (internal token).
- Prediction mainline (ADR-9/ADR-10, see `docs/multi-agent-prediction-plan.md`): one issuance line; the event chain never blocks issuance (baseline fallback), 60 HTTP-attempt/day default cap (ADR-11; stage request quotas 16/16/7/7/2) enforced in `DeepSeekJsonPort.complete_json`, replay experiments write only to `/data/replay/`.

## Source-of-Truth References

| Need | Read first |
| --- | --- |
| Product scope and setup | `README.md`, `docs/product-scope.md` |
| Architecture | `docs/architecture.md` |
| API and OpenAPI | `docs/api.md`, `docs/openapi.yaml` |
| Security | `docs/security.md` |
| Deployment and operations | `docs/deployment.md`, `docs/runbook.md` |
| Observability | `docs/observability.md` |
| Release gate | `docs/release-checklist.md` |
| Visual regression | `docs/visual-regression.md` |
| Windows / DeepSeek Harness handoff | `docs/deepseek-harness-handoff.md` |

Read historical plans, audits, and evidence only when the task explicitly names them or current behavior cannot be established from source and current contracts.

## Default Working Set

Search task-approved files first. If broader discovery is needed, expand only through:

- `src/`
- `server/app/`
- `server/tests/`
- `tests/`
- the specific `docs/` references listed for the task

Do not recursively scan these paths by default:

- `.codex-run/`
- `node_modules/`
- `server/.venv/`
- `dist/`, `output/`, `deliverables/`
- `.playwright-cli/`, `playwright-report/`, `test-results/`
- `.visual-regression/`, `.review-screenshots/`
- caches, generated screenshots, evidence packages, incident traces, and archived proposals

Exclusion means “load only with explicit task need,” not permission to delete or modify.

## High-Risk Boundaries

- Never connect to, copy, migrate, checkpoint, move, or delete the production database or its WAL/SHM files without explicit authorization.
- CCF is soft-removed: preserve historical rows read-only, but never schedule capture/import/freshness/readiness work for a current run.
- Do not infer authority from an old evidence package, handoff, audit, or superseded approval.
- Preserve unrelated staged, unstaged, and untracked work. Do not stage, commit, stash, reset, restore, or clean unless explicitly requested.
- DG-01 implementation, evidence, design, and handoff remain frozen unless a task explicitly reopens that scope.

## Task Protocol

For non-trivial work, copy `/path/to/project-context/TASK_TEMPLATE.md` to a new repository-external task directory and complete it before implementation.

At task start, state in plain language:

- the goal and acceptance criteria;
- files to inspect or modify;
- explicit non-scope and frozen areas;
- verification to be run.

At task end, report only:

- actions and changed files;
- verification results;
- deviations, unresolved questions, and required approvals;
- paths and hashes for large evidence instead of full contents.

Start a fresh conversation when the objective changes, a stage is accepted, or prior attempts begin to dominate the active context. Load the task pack instead of inheriting the full conversation history.

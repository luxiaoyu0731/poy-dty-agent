# Public Delivery Handoff

This document separates the local production engineering closure from the external resources needed for public customer delivery.

## Current Boundary

The repository can be operated as a local production-like system on one machine after the local production gate passes. Public delivery still requires user-owned infrastructure decisions: domain, server, TLS, access policy, and data-source authorization scope.

## Current User Decisions

Recorded on 2026-07-01 for the next public-delivery phase.

| Topic | Decision | Delivery implication |
| --- | --- | --- |
| Domain | Flexible; no fixed domain requirement yet. | Choose any practical domain or subdomain during server provisioning, then update CORS and TLS config. |
| Hosting | Rent a cloud server. | Prepare a small Linux host first, then migrate local production commands into systemd/Docker. |
| Access control | Username/password is required. | Do not expose the app publicly until reverse-proxy basic auth or app login is configured. |
| HTTPS | Required. | Use Caddy or Nginx plus Let's Encrypt; backend stays private behind the proxy. |
| Alerts | Send operational alerts to WeChat. | Configure `WECHAT_WEBHOOK_URL` outside git and test `npm run local:alerts`. |
| Backup target | Keep backup copies on the local machine. | Maintain local SQLite backup retention and add a pull/copy job from the server after public deployment. |

## What Codex Can Complete In This Repository

| Area | Repository deliverable | Command or file |
| --- | --- | --- |
| Local preflight | Read-only launch precheck for files, commands, SQLite, services, env templates, and generated artifacts. | `npm run local:preflight` |
| Daily unattended gate | Data-source automation, quality gate, foundation materialization, backup probe, health checks, and reports. | `npm run local:daily` |
| Alert digest | Local alert JSON/Markdown from the latest daily gate. | `npm run local:alerts` |
| Dry-run bundle | Preflight plus daily dry-run plus alerts. | `npm run local:prod:dry-run` |
| Apply bundle | Requires local services, then applies backed-up data updates and alert generation. | `npm run local:prod:apply` |
| Backend container | Fail-closed backend image requiring production-like env and internal token. | `Dockerfile`, `docker-compose.yml` |
| Health and metrics | Liveness, readiness, deep health, and Prometheus metrics. | `/api/v1/health/*`, `/metrics` |
| Backup/restore evidence | SQLite backup copy, integrity check, and restore-copy drill. | `.codex-run/local-production/06-backup-restore-drill-report.md` |
| Operator docs | Local runbook, release checklist, CCF runbook, acceptance pack. | `docs/runbook.md`, `docs/release-checklist.md` |

## What The User Must Decide Or Provide

| Topic | Needed decision | Why it matters |
| --- | --- | --- |
| Domain | Final domain and subdomain, such as `agent.example.com`. | Sets frontend origin, CORS, TLS certificate, and customer URL. |
| Hosting | Cloud server, local server, NAS, or customer internal machine. | Determines deployment method, storage, backup, and networking. |
| Access control | Single shared password, named users, VPN-only, or customer SSO. | The current internal-token/loopback model is not enough for public multi-user access. |
| Data-source licenses | Whether CCF and other commercial sources may be accessed from the deployment host. | Avoids violating authorization boundaries. |
| Secrets | Production `INTERNAL_API_TOKEN`, provider API keys, source API keys. | Must live outside git in `.env`, secret manager, or host environment. |
| Backup target | Local disk, external disk, cloud object storage, or managed DB snapshot. | Defines recovery point and retention. |
| Alert channel | Local file only, email, webhook, chat tool, or customer operations queue. | Defines how unattended failures become visible. |
| Customer SLA | Business hours only or 24/7, response time, accepted warnings. | Defines which warnings are acceptable at launch. |

## Public Delivery Blockers

The system should not be exposed to customers on the public internet until these are resolved:

1. A reverse proxy with HTTPS is configured.
2. Public access authentication is implemented or the deployment is restricted to VPN/internal network.
3. Production secrets are configured outside git.
4. Backend is not published directly on `0.0.0.0:8000`.
5. SQLite has a durable backup target and restore drill.
6. Monitoring and alert routing are selected and tested.
7. Data-source authorization is confirmed for the deployment environment.
8. `npm run local:prod:dry-run`, `npm run check`, backend tests, E2E, and security audits pass on the release candidate.

## Recommended Next Sequence

1. Run `npm run local:preflight`.
2. Start backend and frontend locally.
3. Run `npm run local:prod:dry-run`.
4. Review `.codex-run/local-production/preflight/latest-preflight.md` and `.codex-run/local-production/10-final-local-production-gate.md`.
5. Decide domain, host, access mode, and alert channel.
6. Prepare production `.env` without committing it.
7. Add reverse proxy/TLS and public authentication.
8. Run a staging smoke test before customer cutover.

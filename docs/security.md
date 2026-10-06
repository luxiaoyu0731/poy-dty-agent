# Security Plan

## Current Controls

- Positioning: single-operator personal workbench. Vendor-license ceremony is retired; connectors still never bypass logins, paywalls, or CAPTCHAs, and captured data keeps raw/canonical hashes for provenance.
- `PERSONAL_MODE=1` marks single-operator semantics: reviewed evidence records self-approve and manifest authorization is removed entirely. Data-quality gates and point-in-time correctness always stay on. Never enable it for a shared deployment.
- Startup logs a prominent warning when internal token enforcement is disabled or `PERSONAL_MODE` is on.
- API-key sources return `requires_api_key` until credentials are configured.
- Outbound source fetches are restricted by `OUTBOUND_FETCH_HOSTS`.
- Chat requests have a default max input size.
- CORS origins, methods, and headers are explicit environment values.
- Internal endpoints require server-side authorization when `ENFORCE_INTERNAL_TOKEN=1`.
- `POST /api/v1/auth/local-session/logout` clears the loopback-only session cookie (mirrors login gating).
- Browser management actions use a loopback-only, HttpOnly local session cookie issued by `POST /api/v1/auth/local-session`; the frontend must not read or ship an internal token.
- Server-to-server automation may still use `X-Internal-Token`, but this header is not part of the default browser CORS allowlist.
- The public Node proxy uses a salted scrypt password verifier and an in-memory opaque single-user session. It protects static files and APIs before adding the backend service token.
- `APP_ENV=staging|production` fails startup unless internal token enforcement is enabled and `INTERNAL_API_TOKEN` is a deployment secret.
- Chat and source-fetch endpoints have in-memory rate limits suitable for single-process staging and local smoke tests.
- LLM traces and source-fetch audits are persisted in SQLite.
- HTTP errors use a standard envelope with `request_id`, `code`, `message`, and `details`.

## Required Before Production

- Add user/session authentication for all non-public user routes.
- Add RBAC for source fetching, ingestion operations, trace access, eval execution, and admin dashboards.
- Move secrets to a managed secret store.
- Keep npm, Python, CodeQL, and GitHub Actions dependency/security checks enabled in CI.
- Replace single-process in-memory rate limits with gateway or shared-store limits before horizontal scaling.
- Keep prompt-injection evals passing before enabling tool-using agents.
- Keep a private note for security findings, fixes, and revisit dates.

## Environment Variables

| Name | Purpose |
| --- | --- |
| `APP_ENV` | Runtime environment label |
| `CORS_ALLOW_ORIGINS` | Comma-separated allowed browser origins |
| `CORS_ALLOW_METHODS` | Comma-separated allowed HTTP methods |
| `CORS_ALLOW_HEADERS` | Comma-separated allowed request headers |
| `OUTBOUND_FETCH_HOSTS` | Comma-separated host allowlist for source fetches |
| `MAX_CHAT_QUESTION_CHARS` | Chat input limit |
| `MAX_SOURCE_PREVIEW_CHARS` | Source preview truncation limit |
| `ENFORCE_INTERNAL_TOKEN` | Enforces authorization on internal routes |
| `PERSONAL_MODE` | Single-operator semantics for evidence reviews; never for shared deployments |
| `INTERNAL_API_TOKEN` | Deployment secret for internal routes |
| `ENABLE_LOCAL_SESSION_AUTH` | Enables loopback-only local session cookies for browser management actions |
| `LOCAL_SESSION_SECRET` | Optional signing secret for local session cookies; falls back to `INTERNAL_API_TOKEN` or an in-memory dev secret |
| `LOCAL_SESSION_TTL_SECONDS` | Lifetime for local browser management sessions |
| `PUBLIC_AUTH_MODE` | Must be `single_user_password` for the public proxy |
| `PUBLIC_LOGIN_PASSWORD_HASH` | Secret scrypt verifier for the public user; plaintext passwords are unsupported |
| `PUBLIC_CANONICAL_ORIGIN` | Exact HTTPS Origin accepted for login and state-changing requests |
| `PUBLIC_SESSION_IDLE_TTL_SECONDS` | Public session idle timeout |
| `PUBLIC_SESSION_ABSOLUTE_TTL_SECONDS` | Public session absolute timeout |
| `RATE_LIMIT_WINDOW_SECONDS` | Rate limit window |
| `CHAT_RATE_LIMIT_PER_WINDOW` | Chat requests allowed per window per client |
| `SOURCE_FETCH_RATE_LIMIT_PER_WINDOW` | Source fetches allowed per window per client |
| `INDUSTRIAL_INTELLIGENCE_ENABLED` | Enables the isolated v37 intelligence API and UI; defaults off |
| `INTELLIGENCE_WRITE_RATE_LIMIT_PER_WINDOW` | Intelligence write requests allowed per window per client |
| `INTELLIGENCE_CURSOR_SECRET` | Optional HMAC secret for intelligence snapshot cursors; derives from the internal token when unset |
| `INTELLIGENCE_RUN_DIR` | Private directory for cross-process intelligence locks and run artifacts |
| `DEEPSEEK_API_KEY` | LLM provider key |
| `DEEPSEEK_BASE_URL` | LLM provider base URL |
| `DEEPSEEK_MODEL` | LLM model name |

## Internal Route Policy

Internal routes accept either:

- a valid server-to-server `X-Internal-Token`, or
- a valid `poy_dty_local_session` HttpOnly cookie from a loopback client when local session auth is enabled.

The browser frontend should use the local session flow and must not include `VITE_INTERNAL_API_TOKEN` or any other build-time internal secret.

The public proxy has a separate trust boundary. It accepts the single-user password only over
HTTPS, issues a `Secure`, `HttpOnly`, `SameSite=Strict`, `__Host-` session cookie, and stores
only the opaque session-token hash in process memory. Restarting the proxy or rotating the
password verifier invalidates all sessions. State-changing requests also require the exact
public Origin and a session-bound CSRF header. Caller-provided internal-token, forwarded,
Cloudflare identity, authorization, cookie, and CSRF headers are stripped before proxying.

Internal routes include:

- `POST /api/v1/sources/{source_id}/fetch`
- `POST /api/v1/sources/fetch-configured`
- `GET /api/v1/sources/fetch-audit`
- `POST /api/v1/news/fetch-runs`
- `POST /api/v1/imports/*`
- `POST /api/v1/data-snapshots`
- `POST /api/v1/predictions`
- `POST /api/v1/predictions/review-due`
- `POST /api/v1/price-comparison/fetch`
- `GET/PATCH /api/v1/knowledge/evidence-queue*`
- `POST/GET /api/v1/assistant/chat`
- `POST /api/v1/assistant/chat/stream`
- `GET /api/v1/assistant/traces`
- `POST /api/v1/assistant/evals`
- `POST /api/v1/assistant/rag-evals`

Prediction ledger writes and provider-backed assistant chat are internal because they mutate research records, can consume LLM quota, and persist traces.

These routes are convenient in local development, but staging and production must set `APP_ENV=staging|production`, `ENFORCE_INTERNAL_TOKEN=1`, and a non-default `INTERNAL_API_TOKEN`. Exposing local session auth beyond loopback is not supported.

## Secret Rotation

Rotate on suspicion of leakage, after any operator change, and at least yearly. All rotation is
offline and reversible; no data migration is involved.

| Secret | Where it lives | Rotation steps | Effect of rotation |
| --- | --- | --- | --- |
| `INTERNAL_API_TOKEN` | `shared/.env.production` (0600) | Generate a new random token, replace it in the env file, `launchctl kickstart -k` backend, update any wrapper that inlines it (morning brief reads it from the same env file) | Invalidates local sessions signed with the old token (they fall back to it as signing key) and breaks in-flight automation until wrappers restart |
| `LOCAL_SESSION_SECRET` | env file (optional) | Replace value, restart backend | Immediately invalidates all local-session cookies |
| `PUBLIC_LOGIN_PASSWORD_HASH` | verifier in env file; plaintext in macOS Keychain service `com.poydty.agent.public-login` | Run `manage_public_production.py provision-public-password`, then restart `public-frontend` | Kills every public session; log in again with the new Keychain password |
| `DEEPSEEK_API_KEY`, `EIA_API_KEY`, `FRED_API_KEY`, `UN_COMTRADE_API_KEY` | env file / provider console | Revoke at provider, issue new credentials, update env file, restart backend | The affected provider-backed feature or fetch fails until restarted |
| Retained `DCE_API_KEY`, `DCE_SECRET` | macOS Keychain only; source is soft-removed | Do not copy into the runtime env. Delete or rotate only after separate operator authorization. | No production effect while DCE remains soft-removed; retained solely for reversible restoration |
| `SERVERCHAN_SENDKEY` / `PUSHPLUS_TOKEN` | env file of the daily bundle | Revoke at provider console, update env file | Alert pushes fail silently until updated; digest files are still written |

`APP_ENV=production` refuses to start with `PERSONAL_MODE=1`: reviewed-evidence self-approval is a
single-operator semantic and must never be combined with a shared deployment.

## Industrial Intelligence Center Security Boundary (schema v37, Local Implementation Complete)

The `/api/v1/intelligence/*` surface inherits every global rule above and adds:

- All routes sit behind the existing internal auth (`X-Internal-Token` or loopback
  local-session cookie); the browser never receives an internal token. While
  `INDUSTRIAL_INTELLIGENCE_ENABLED=0` the routes answer 404
  `intelligence_module_disabled`.
- Snapshot pagination cursors are HMAC-SHA256 signed
  (`intelligence-cursor.v1`). The key comes from `INTELLIGENCE_CURSOR_SECRET`,
  falling back to a derived `INTERNAL_API_TOKEN` value, and never leaves the
  server. Tampered or mismatched cursors fail closed with 422.
- Rights execution: every item revision freezes a rights snapshot; projected
  news is stored `metadata_only` (no bodies, no excerpts). Reads re-apply the
  current (stricter) policy and return `presentation_status=redacted` with
  field-level `redactions` instead of blocked content. Rights-blocked text is
  removed from the FTS index before search becomes available again.
- Source text, GeoJSON properties, and feedback free text are untrusted data:
  they are never treated as instructions and never rendered as HTML (the
  frontend has no `dangerouslySetInnerHTML` path; map popups bind to text).
- Outbound fetches for the USGS provider use only the fixed official HTTPS
  endpoint `earthquake.usgs.gov`, require the host to be present in
  `OUTBOUND_FETCH_HOSTS`, disable redirects, and cap response size and depth.
  There is no caller-controlled URL fetch anywhere in the module.
- No instant-alert path exists: no WebSocket/SSE/polling push, no email or
  messaging integrations, no webhooks (statically asserted by
  `tests/test_intelligence_isolation.py`).
- Intelligence tables cannot write into the prediction, news, event, or
  evaluation domains; `prediction_eligible=0` and `instruction_eligible=0` are
  enforced by CHECK constraints that the application cannot relax.
# 当前公网产品边界（2026-09-07）

用户已明确授权所有既有功能匿名公开，包括 AI、生成与抓取。具体合同见 [公开访问决定](public-access-2026-09-07.md)。本环境不再以密码门槛为发布条件；保留服务端内部 token、loopback 绑定、浏览器凭据头隔离及同源写入检查。匿名访客具备原有业务操作权限是接受的产品行为，不能按未授权绕过登录计为漏洞。

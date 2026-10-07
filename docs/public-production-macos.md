# Public production runtime on macOS

This runbook prepares a reversible runtime for `app.kaipingrc.com` outside Desktop. It solves the macOS background-service failure caused by launchd being unable to use a Desktop working directory. It does not make a laptop equivalent to an always-on cloud host: sleep, shutdown, loss of power, or loss of internet still takes the public site offline.

The durable runtime is stored under:

```text
~/Library/Application Support/POY-DTY-Agent/
├── current -> releases/<release-id>
├── previous -> releases/<previous-release-id>
├── releases/<release-id>/
└── shared/
    ├── .env.production
    ├── data/agent.db
    ├── data/backups/
    ├── launchd/
    ├── logs/
    └── reports/
```

Each release is immutable. `current` is switched with an atomic symlink replacement. The database is outside releases. `--source-db` is initial-seed-only: `prepare` refuses to replace an existing shared production database. This prevents a repository database from silently replacing newer public data.

## 1. Prepare without activating

The first command builds the frontend, creates a release, installs an isolated Python environment, verifies and copies SQLite, then switches `current`. It does not start services or connect Cloudflare.

```bash
python3 server/scripts/manage_public_production.py prepare \
  --source-db server/data/agent.db
```

On later code-only releases, omit `--source-db` so a newer live database is never overwritten by a repository copy:

```bash
python3 server/scripts/manage_public_production.py prepare
```

If the shared database already exists, passing `--source-db` is a hard error.
Restoring production data is a separate operator procedure; it is never part of
a normal code release.

### Build or refresh the shared semantic index

Inspect the shared database without applying migrations or writing data:

```bash
python3 server/scripts/manage_public_production.py index-status
```

After preparing a release, explicitly build the production semantic index:

```bash
python3 server/scripts/manage_public_production.py rebuild-index
```

This command always targets
`~/Library/Application Support/POY-DTY-Agent/shared/data/agent.db`; it never
copies `server/data/agent.db`. Before changing the shared database it creates a
consistent SQLite online backup under
`shared/reports/semantic-index/db-backups/`. The builder writes a versioned
shadow index and switches `active_index_id` only after the document, chunk, and
vector build completes. If embedding or index construction fails, the failed
shadow is recorded and the prior active index remains selected.

Do not use `--limit` for a normal production build. It exists only for explicit
diagnostics and produces a deliberately partial candidate.

Copy the environment template and set secrets outside the repository:

```bash
cp deploy/examples/.env.public-production.example \
  "$HOME/Library/Application Support/POY-DTY-Agent/shared/.env.production"
chmod 600 "$HOME/Library/Application Support/POY-DTY-Agent/shared/.env.production"
```

Before a formal seven-product release, audit the deployed file without sourcing
it or printing any value:

```bash
npm run local:preflight -- \
  --backend-url http://127.0.0.1:8000 \
  --frontend-url http://127.0.0.1:4173 \
  --require-services \
  --runtime-env "$HOME/Library/Application Support/POY-DTY-Agent/shared/.env.production" \
  --require-formal-source-credentials
```

The public frontend wrapper is fixed to port `4173`; the preflight program's
default `5173` remains the local development port. Always pass the explicit
public URLs with `--require-services` when this command is used as a public
release gate, otherwise a healthy public frontend can be mistaken for a
missing development server.

This gate requires `EIA_API_KEY` for the active crude formal label. DCE MEG is
soft-removed and therefore is not a credential gate. The accepted SunSirs MEG
label is public and requires no credential; its first production capture starts
the prospective point-in-time history. The check reports only configured/missing variable
names and rejects symlinks, non-owner files, loose permissions, malformed
assignments, and files larger than 1 MiB. FRED and UN Comtrade remain optional
context credentials.

Provision the public login with a random password stored in macOS Keychain.
Only its scrypt verifier is written to the `0600` runtime environment file;
the plaintext is never printed or placed in git:

```bash
python3 server/scripts/manage_public_production.py provision-public-password
```

Generate review-only service definitions:

```bash
python3 server/scripts/manage_public_production.py generate-launchd \
  --tunnel poy-dty-agent \
  --cloudflared-config "$HOME/.cloudflared/config.yml"
```

The eight enabled jobs generated for the current production matrix are:

- `com.poydty.agent.public-backend`: loopback Uvicorn using the durable database.
- `com.poydty.agent.public-frontend`: static frontend plus same-origin `/api` proxy.
- `com.poydty.agent.cloudflare-tunnel`: Cloudflare Tunnel forced to HTTP/2 to avoid networks that block QUIC/UDP 7844.
- `com.poydty.agent.news-scheduler`: long-running news/event acquisition loop (writes `news-automation/` status).
- `com.poydty.agent.local-daily`: 09:30 daily Agent bundle with a five-minute catch-up check.
- `com.poydty.agent.morning-brief`: 09:30 morning brief with a five-minute catch-up check.
- `com.poydty.agent.public-health-probe`: five-minute credential-free HTTPS `/healthz` and login-redirect probe. Each
  invocation allows six bounded attempts at five-second intervals (five-second request timeout) so launchd loading the
  probe before Cloudflare Tunnel is ready does not create a false outage; all six failures still exit non-zero. Its
  bounded latest status is written through an owner-only `0700` directory and `0600` temp/final files, with file fsync,
  atomic replace, directory fsync, and failed-replace cleanup.
- `com.poydty.agent.intelligence-daily`: weekday 08:00 input collection (retry window ends at 08:20),
  then 09:30 industrial-intelligence publication with five-minute catch-up,
  a per-day success stamp, single-flight lock, post-write WAL checkpoint, and backend recycle. It exits without work
  while `INDUSTRIAL_INTELLIGENCE_ENABLED=0`.

`com.poydty.agent.event-summary-worker` and experience settlement are explicitly
disabled in this production matrix and are not managed by `manage_public_stack.sh`. CCF has no generated or enabled job.

The generator never calls `launchctl`, changes DNS, or starts a Tunnel. Review the files under `shared/launchd` before activation. The backend wrapper loads secrets at runtime; secrets are not embedded in plist files.

The backend wrapper also exports explicit, non-secret paths for the local daily
and news scheduler status documents. `GET /metrics` reads their bounded
`scheduler_observability.v1` projections; it does not scan the runtime tree or
expose scheduler stdout/stderr. The status documents are atomically replaced by
the scheduler processes, and cumulative failure counts are preserved in those
documents across backend restarts.

The daily Agent and the morning brief are both scheduled for 09:30 in
`Asia/Shanghai` (the morning brief additionally waits for that day's daily
Agent success marker, so a long-running or retried daily job cannot cause the
09:30 output to freeze yesterday's data). Both jobs also run a lightweight
check every five minutes and at launch. Before their scheduled time the
wrappers exit without doing work; after the scheduled time they catch up a
missed run. A per-job lock prevents concurrent executions, and a dated success
marker/output makes retries idempotent. Failed attempts do not create the
success marker, so the next five-minute check retries automatically. This
covers login, service reload, and wake-after-schedule cases, but the Mac must
be awake for the catch-up to start.

## 2. Explicit local activation

Activation is an operator action. Load backend and frontend first, verify loopback health, and only then load the Tunnel:

```bash
RUNTIME="$HOME/Library/Application Support/POY-DTY-Agent"
mkdir -p "$HOME/Library/LaunchAgents"
cp "$RUNTIME/shared/launchd/com.poydty.agent.public-backend.plist" "$HOME/Library/LaunchAgents/"
cp "$RUNTIME/shared/launchd/com.poydty.agent.public-frontend.plist" "$HOME/Library/LaunchAgents/"
cp "$RUNTIME/shared/launchd/com.poydty.agent.cloudflare-tunnel.plist" "$HOME/Library/LaunchAgents/"

launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.poydty.agent.public-backend.plist"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.poydty.agent.public-frontend.plist"
curl --fail http://127.0.0.1:8000/api/v1/health/ready
curl --fail http://127.0.0.1:4173/healthz
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/com.poydty.agent.cloudflare-tunnel.plist"
```

Those two loopback checks only prove that the backend and password-gated proxy are alive.
Do not use anonymous `release.json` or API requests as content checks: the proxy intentionally
returns a login redirect or `401`. Validate the deployed release and APIs with the authenticated
password-file smoke gate below.

Do not expose the application without the proxy's single-user password gate. Production
must set `PUBLIC_AUTH_MODE=single_user_password`, an approved scrypt verifier in
`PUBLIC_LOGIN_PASSWORD_HASH`, and the exact HTTPS origin in `PUBLIC_CANONICAL_ORIGIN`.
The proxy refuses to start when these values are missing or malformed. It protects static
files and API routes with a server-side revocable session; the browser never receives the
backend internal token. Keep the populated environment file mode `0600`, and treat the
password verifier as a secret. The only anonymous endpoint is the proxy's minimal `/healthz`.

## 3. Mandatory public release gate

The login page uses `Referrer-Policy: same-origin`: a native browser form POST
under `no-referrer` sends `Origin: null` and fails the exact-origin login guard
before password verification. Other responses retain `no-referrer`. Do not
allow null/missing origins or remove CSRF checks to address this failure.
Run `node --test tests/public-login-browser.test.mjs` with the installed
Playwright Chromium as well as the HTTP auth tests. The isolated browser test
submits the real login form using a fixture password and browser-generated
request headers; the HTTP smoke below supplies Origin explicitly and cannot
detect this regression alone.

After an explicitly approved deployment, run the fail-closed smoke gate against the public origin:

```bash
python3 server/scripts/manage_public_production.py smoke \
  --base-url https://app.kaipingrc.com \
  --password-keychain-service com.poydty.agent.public-login
```

After schema v37 is present and the intelligence feature flag is enabled, add
`--require-intelligence`. That mode validates the source catalog, event radar,
brief availability envelope, map FeatureCollection, run history, and the eighth
module in addition to every legacy gate:

```bash
python3 server/scripts/manage_public_production.py smoke \
  --base-url https://app.kaipingrc.com \
  --password-keychain-service com.poydty.agent.public-login \
  --require-intelligence
```

The legacy `--password-file` alternative still requires an owner-only regular
file with mode `0600`. Prefer Keychain so the smoke gate does not materialize a
plaintext file. Neither mode accepts the plaintext password as a command-line
argument or prints it.

The gate requires:

- deployed `release.json` and release hash;
- live, ready, and deep backend health;
- delivery status, market chain, event library, evidence graph, and agent-run APIs;
- `rag-index/status` to report `status=ready` with a non-empty active semantic index;
- the authenticated seven-product forecast, every immutable history batch returned by `limit=3`, OOS evaluation, and
  model-registry APIs to expose an exact unique 7×3 grid with internally consistent identities/counts/statuses; `0/21`
  is valid when reported honestly and is not converted into a release-smoke failure;
- HTML access to all eight module routes; `--require-intelligence` additionally requires the enabled v37 API contracts.

Every CLI smoke writes a `public-production-smoke-evidence.v2` report under
`shared/reports/public-smoke/`. The immutable `public-smoke-<body-sha-prefix>.json` is published and fsynced before the
`public-smoke-latest.json` pointer advances; the directory is `0700`, both files are `0600`, and an address collision
with different bytes fails closed. Origin validation, credential-source validation, anonymous-boundary, login, and
authenticated-check failures are persisted with a stable stage/error code before the command exits non-zero. Invalid
origins are never echoed into evidence, and exception text, session cookies, passwords, and Keychain values are never
included in either report.

Do not capture customer-manual screenshots from the public URL until this gate passes. A Cloudflare `1033`, stale release hash, missing/stale semantic index, missing API data, or a failed module is a release failure.

## 4. Rollback

Switch back atomically, then restart the backend and frontend jobs. The database remains in `shared` and is not rolled back with code:

```bash
python3 server/scripts/manage_public_production.py rollback
launchctl kickstart -k "gui/$UID/com.poydty.agent.public-backend"
launchctl kickstart -k "gui/$UID/com.poydty.agent.public-frontend"
```

Database restoration is a separate, deliberate operation. Verify a backup with `PRAGMA integrity_check`, stop all writers, restore it, and rerun deep health before reconnecting the Tunnel.

## 5. Cloud-host end state

This macOS runtime is a reversible bridge. The reliable production end state remains an always-on Linux host with the same immutable release or Docker image, durable storage, TLS/reverse proxy, customer authentication, monitored backups, and alerting. The Mac can then retain backup copies instead of being the public origin.

### Public audit remediation release (2026-09-06)

The revised intelligence wrapper uses separate `collect` and `publish` daily
stamps. `--collect-only` collects/project inputs and records terminal runs without
freezing a brief. Publication still cannot occur before 09:30 Shanghai, and the
08:20 cutoff compares actual instants across UTC and Shanghai offsets. Missing
the collection window is not repaired by backdating newly fetched information;
the resulting brief must retain its real gaps. The frozen calendar remains
Monday–Friday (no inferred Chinese holiday adjustments).

Refreshing templates is necessary for the collection schedule to take effect;
a frontend-only deployment is insufficient. Keep the existing intelligence
feature switch as the off switch. These instructions do not authorize a live
run, database migration, or replacement of an already frozen historical brief.

Release packaging now writes the same non-secret identity into the frontend
and backend manifests. After an authorized deployment, compare `release_id`,
`release_hash`, and `git_sha` from authenticated `/release.json` against the
`release` object on `/api/v1/health/live` and `/api/v1/health/ready`. Restart the
backend from its immutable release before testing; `unavailable` is not a match.

The public CSP allows same-origin map workers and data images/fonts. It permits
only the existing Cloudflare beacon origins for its script and connection;
there is no `unsafe-eval`, wildcard script origin, inline script allowance or
blob-worker allowance. MapLibre's CSP worker is packaged as a same-origin asset.
# 当前访问方式（2026-09-07）

用户已确认全部功能免密码公开，依据 [公开访问决定](https://github.com/luxiaoyu0731/poy-dty-agent/blob/64ce86b10dc9acb487959bba970a790298e389dd/docs/public-access-2026-09-07.md)。公网前端与健康探针从 `shared/public-access-mode` 读取非敏感模式覆盖值 `public`；原 `.env.production` 中的密钥与密码校验器不读取、不复制、不改写。下文密码部署流程仅适用于显式恢复 `single_user_password` 的环境。

公开模式下首页、深链接、业务 API 和 live/ready 应匿名返回 200，`/login` 应跳转首页。发布烟测使用 `smoke --access-mode public --base-url https://app.kaipingrc.com`，不提供密码文件或钥匙串参数。五分钟健康探针读取相同模式，检查 release/live/ready，不再把登录重定向当成公开模式成功。

回滚时先将模式覆盖文件恢复为 `single_user_password`，再切回上一前后端发布对并重启两个服务；不改数据库。模式覆盖文件需为运行用户所有、权限 0600。后端继续 loopback，访客不能获得内部 service token；所有现有业务写入已按用户决定开放。

### 2026-09-13 audit closeout: backup and assistant freshness

The daily bundle's backups live in `shared/local-production/db-backups`, not
only `shared/data/backups`. Check `shared/local-production/latest-status.json`
and `06-backup-restore-drill-report.md` before declaring backups absent.
Daily backups use SQLite's online backup API so committed WAL pages are included;
a successful `integrity_check` alone does not prove a raw main-file copy is current.
The restore drill checks an isolated copy and never replaces the live database.

An unchanged embedding configuration does not establish index freshness.
Semantic snapshots older than 24 hours are marked `snapshot_expired`; Assistant
retrieval then reads the current corpus with the same point-in-time, rejected
material, and allowed-document-type checks. This fallback does not rebuild the
index or make a new prediction. It preserves source IDs and observation dates.
A separately authorized `rebuild-index` can restore semantic retrieval; a build
failure must leave current-corpus fallback available. Verify both the public
`/api/v1/rag-index/status` response and an actual dated AI answer after release.

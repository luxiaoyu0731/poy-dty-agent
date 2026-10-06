## 2026-10-05 mobile desktop viewport release (Codex)

- Mobile change commit `20a02228b10330bb333a5b91ddb0484e22e690fb`: exact four-file list `index.html`, `public/mobile-desktop-viewport.js`, `src/fixed-viewport.css`, `tests/mobile-desktop-viewport.spec.ts`. Published frontend built from the actual public `48e7874` baseline plus this overlay, deliberately excluding unpublished `1e8d95c` desktop evidence changes. Desktop source/style hashes match the previous public version. No push, DB write/migration, prediction-rule, budget, model, credential or timer changes.
- Paired release `20261005T010820Z-fc4d217cdafebf96`, content SHA `fc4d217cdafebf96f16091706af284daf8b7caab70fca4742654ee3d8236acbc`. Four rebuilt service image tags share digest `sha256:49af915cd7bb0f52ef50a66e21e8949f29feaaa9f5474da833b299c9b96202af`; among 8,638 runtime files only generated `server/release.json` changed, dependencies and backend app code stayed identical. Only backend/news-scheduler/event-summary-worker recreated; scheduler remains stopped. Approved source files synchronized and checked by SHA-256.
- Fresh candidate checks: frontend check exit0; `211 passed (5.1m), 1 skipped` with retries disabled; unchanged-backend full suite `3374 passed in 245.38s (0:04:05)` using DG01 private cache paths/0700 TMPDIR; authoritative root-record validation exit0; npm production audit zero; candidate source Gitleaks zero; JS/TS Semgrep 24 rules/2 files zero; actual candidate image Debian AND Python scans zero HIGH/CRITICAL vulnerabilities or secrets. Raw Git history still has the retired-key finding and is not called clean; history is not included in this artifact.
- Artifact-bound gate GO WITH WARNINGS. Legacy `visual:check` times out on a now-hidden title; replacement base/candidate comparison with identical API responses shows zero changed pixels in six desktop modules and 26/1,296,000 pixels in workflow (0.002006%). Native physical-phone/WebKit review remains pending, explicitly why the operator requested publication. Existing bundle-size warning retained. Public Chromium phone/desktop checks passed after completed activation: mobile frame 1440x900, initial scale 0.270833 on 390px; desktop original viewport/class preserved; frontend/backend identity and public script checksum match. A check started during service replacement saw the old page/temporary 502; completed-release checks were rerun and passed.
- Saved rollback `/opt/agent/releases/mobile-desktop-20261005/` contains old dist/source/four images; saved image load and isolated uid10001 no-network/no-DB import passed. Restore with `bash /opt/agent/releases/mobile-desktop-20261005/activate.sh rollback`; environment and data untouched. Prior snapshots retained. Private evidence and artifact-bound gate: `/path/to/project-context/mobile-desktop-20261005/`.

# Cloud daily operations

## 2026-10-04 bounded model-output repair follow-up (Codex)

- Runtime commit `b3b06d0e8c15a4c670e1b6dd22033ff2e96212e5`: accept only one complete JSON object, reject duplicate/non-finite values and damaged-parent/valid-child parsing, share the existing single repair attempt across JSON and checked field-shape failures, and remove rejected case IDs before building artifact citations. Connection failures still fall back directly. R1–R5/O1, stage quotas and actual HTTP hard cap are unchanged. Six precisely staged source/test/ADR files and generated identity synchronized; no migration, daily trigger, timer, old lesson or forecast rewrite.
- Release `20261004T073909Z-ddbd553dbfdff425`; exact content SHA `ddbd553dbfdff4254852257b6468236ab62956083c3ed8061536b61cafa39e27`. Four rebuilt service tags share OCI manifest `sha256:bd97062b6e0aa5c18336e741a8a197755c8fd346f90e5c5418f82e7e0121560d`, config digest `sha256:a44da849d554b2b586b766465b6d47ebf0e8a29c0631d731242740cf03e80033`. Among 8,636 runtime files, only the chain module, isolated replay port and release metadata changed; dependencies were not reinstalled. Only backend/news-scheduler/event-summary-worker restarted; scheduler remains stopped.
- Fresh checks: `3361 passed in 249.45s (0:04:09)`; `207 passed (4.9m), 1 skipped`; frontend check exit0; five changed Python targets parsed with zero Semgrep findings. Actual-image Trivy explicitly includes Python packages and finds zero HIGH/CRITICAL vulnerabilities or secrets. History Gitleaks remains exit1 with exactly the operator-approved retired-key fingerprint, expires `2026-10-11T05:51:33+00:00`; it is not a clean scan. Artifact-bound readiness is GO WITH WARNINGS, including the existing frontend chunk advisory and unconfirmed native visual review.
- Saved rollback `/opt/agent/releases/chain-output-repair-20261004-b3b06d0/`: old dist/source and four images/tags. Saved-image load and isolated uid10001 old-app import passed. Run `bash /opt/agent/releases/chain-output-repair-20261004-b3b06d0/activate.sh rollback`; DB, credentials, HTTP journals and timers are preserved. Prior saved release remains available.
- Public frontend/backend identities match. Graph has 18 backend nodes, recall disabled, unknown current total (`—/40`). POY retains 4 up/5 down non-voting conditional materials from eight URLs, direct support/counter zero. HTTP200 observations: dossier cold 7.841s, scoped event cold/warm 7.604/0.463s, radar list cold/warm 2.236/0.404s. These are individual observations, not p95 or an efficacy claim.
- A separately preregistered format pilot reused the original malformed receipt and made one new physical repair request, costing at most 0.035550 CNY; cumulative campaign bound `92.446113/350` CNY. It passed the current political-output checks, not independent semantic verification or full nested-field validation. Original 60-date results remain sealed and NO-GO; new RAG voting and reflection policy stay OFF.
- Today's 08:00 run preceded this release and failed with `OperationalError: database is locked`; its saved status does not locate the transaction/writer. The USD/CNY quality gate also rejected 2026-09-30 parity at age 346,058s against 345,600s. These remain open findings; no forced rerun, backdated issuance or freshness relaxation. Private gate/activation/public receipts: `/path/to/project-context/evidence-experiment-acceptance-20261004/chain-repair-*` and `public-chain-repair-*`.

## 2026-10-04 bounded grounding capture follow-up (Codex)

- Commits: `95b4c49` (reuse identical pure source checks within one bounded readonly capture; reconstruction validation retained) and `975c779` (isolated outcome-blind recall comparability audit and regression tests). Seven exact source/test/doc paths synchronized; no migration, daily trigger, voting, settlement, old lesson, credential or timer change. Unrelated main-worktree drafts remain excluded.
- Release `20261004T065100Z-275e2cb2ff2182b4`, content SHA `275e2cb2ff2182b4f57debdab654622422b916212738cb29e55fb68140cfeb19`, paired public git SHA `975c7798becb0921f55ae6ccfc8a0f6bf0dbe763`. All four rebuilt runtime tags use OCI manifest `sha256:ff7f57f0a1a27db695b50ce64eafc3b37f1271e3f4b445c434a0cf733db866ff`; config digest is `sha256:7df9c403d746a20bb69e13f3b37a8919a7cb14907122671965f7fb6c62274fb0`. These are different OCI identities, not conflicting builds. The 8,636-file runtime comparison found only two app modules, generated release identity and the isolated audit helper changed; dependencies were not reinstalled. Backend, news scheduler and summary worker restarted; scheduler remains stopped.
- Fresh checks: `3348 passed in 263.32s`, `207 passed/1 skipped`, frontend check successful, five changed Python targets parsed by Semgrep with zero findings. Initial project-config Trivy excluded `.venv`; that result was insufficient for Python dependencies. The second actual-image scan explicitly included `.venv`, parsed Debian and Python packages, and found zero HIGH/CRITICAL vulnerabilities or secrets. Raw Git-history Gitleaks still exit1 with exactly the previously approved retired-key fingerprint; no new finding. The operator's exception remains restricted to this dated release batch, expires `2026-10-11T05:51:33+00:00`, and is not a clean history scan.
- Saved rollback `/opt/agent/releases/grounding-capture-20261004-975c779/`: old dist, exact source backup, four saved images and `rollback-grounding-975c779` tags; saved-image load and no-network/no-volume uid10001 old-app import passed. Restore with `bash /opt/agent/releases/grounding-capture-20261004-975c779/activate.sh rollback`. DB and budget journals are preserved; source changes are restored from the saved artifact.
- Public reads returned HTTP200 with matching frontend/backend release identity, 18 backend nodes, recall disabled and an honestly unknown current chain total (`—/40`). One dossier cold request took 7.540s; radar list cold/warm 2.375/0.693s; event-scoped evidence cold/warm 7.521/1.544s. These are individual observations after restart, not HTTP p95 or a completed SLO test. POY retains 4 up/5 down source-bound conditional materials from eight URLs; all are non-voting, direct support/counter remain zero. Native visual verification is still unavailable because the computer-use controller timed out; this follow-up does not change frontend layout.
- Fixed-input readonly server measurement (462 articles, three interleaved pairs): capture medians 11.281s before / 6.118s after, identical output hashes across all runs. Memo lifetime is one capture, bounded to 1,024 entries and 32MiB serialized payload; mutated source or consumer output cannot bypass reconstruction checks.
- Scientific exposure remains rejected: 25 new source-classification attempts, 10 literal-anchor labels accepted and 15 rejected; no existing recall group has three mechanism-comparable accepted labels. No forced references or production voting. Conservative cumulative campaign bound `92.410563/350` CNY. Reflection still has no lesson exposure. Separately, today's pre-release 08:00 run ended blocked by public-market freshness and formal-governance gates; no new daily chain was forced and those gates were not bypassed. Private evidence and artifact-specific gate are under `/path/to/project-context/evidence-experiment-acceptance-20261004/`.

## 2026-10-04 shared evidence and strict acceptance release (Codex)

- Commit list: `d35a769` (remove the retired unused key literal; explicit unprivileged runtime), `ed6e8af` (shared source-bound report/radar/dossier/graph consumers, additive memory/budget contract, durable attempt accounting, canvas gutters, isolated experiment tools and regression tests). Existing uncommitted primary-worktree drafts were not included. Exact file lists were staged; no push or rsync delete.
- Artifact identity: release `20261004T060511Z-540af44bb8c39cce`, content SHA `540af44bb8c39cce564af0ad9f0d68c1f89f0b361c1f7d002c82e41f49b9f95b`. Paired public frontend/backend identity points to `ed6e8afdc8a2b63e66aa3feb09bb5676d22cb1c5`. Frontend check rebuilt the published artifact; all 96 approved source/test/contract/doc files and generated release identity were synchronized and hash-checked.
- Four rebuilt runtime images share `sha256:02fc23e5205fb0e181dd10c753f8b74b04ece59703a0273de99f90ae89d61ab8`. They derive from the saved pinned runtime; no dependency reinstall. All 8,635 manifest file hashes verified; only approved code and release metadata differ. The first candidates were rejected: legacy Docker did not execute a multiline build script; broad copying also introduced eight unapproved replay files. Final image uses explicit approved overlays and an exec-form Python build helper, removed after use. Only backend/news-scheduler/event-summary-worker restarted; scheduler remains stopped.
- Readiness: GO WITH WARNINGS after fresh backend `3341 passed`, frontend check, e2e `207 passed/1 skipped`, Semgrep zero findings, npm audit zero vulnerabilities, candidate-image Trivy zero HIGH/CRITICAL vulnerabilities/secrets. Raw historical Gitleaks remains exit1 with one retired FRED key. Official read-only API confirms it invalid; source literal removed and local/server configuration matches the new environment-only key. Sole operator explicitly approved this fixed historical finding for this release, expiring `2026-10-11T05:51:33+00:00`; this is not a clean history scan or a general baseline waiver.
- Saved rollback: `/opt/agent/releases/shared-evidence-20261004-ed6e8af/` contains `dist-before`, `source-before.tar`, `images-before.tar`, before/after image digests and four `rollback-shared-ed6e8af` tags. Saved image load, no-network/no-volume old-app import under uid10001 and old index comparison passed. Run `bash /opt/agent/releases/shared-evidence-20261004-ed6e8af/activate.sh rollback` to restore saved artifacts. Budget journals, DB, new credentials and timers are preserved; do not run a legacy budget-unaware chain again on a day that a newer chain has already spent attempts without reconciling its journal.
- Public reads: 18 backend nodes plus frontend-only memory aggregation card; memory recall remains disabled. Today's authoritative chain budget is unknown (`—/40`), not inferred from stage metrics. POY current-view conditional materials are 4 up/5 down (8 URLs), all non-voting; direct support/counter remain 0. Radar list measured cold 5.470s/warm 0.764s; the formerly failing scoped event evidence now HTTP200, cold 17.872s/warm 1.528s. These are individual observations, not an SLO claim. Native computer-use review was attempted but its controller timed out three times; public visual verification is unconfirmed, local DOM/geometry e2e is green.
- New information-only daily report `41abd204-3c73-4dc4-814e-179a0eb8a9df` generated at `2026-10-04T06:18:30+00:00` from a `consumer-evidence.v2` frozen snapshot. It appends a new record, preserves old report hashes, excludes the two operator-rejected sections, and is independent of the stopped formal customer-report pipeline. No forecast issuance, rule, settlement, old lesson, migration or timer change.
- Scientific gate: RAG 60 dates/180 cells per arm, 79 qualified recall group instances but zero actual recall citations; one retained degraded date, gate rejected. Reflection 60 dates/180 cells per arm, four switched cells, no weekly distillation or lesson exposure; effect not established. New voting/reflection policies remain off. Cumulative conservative campaign bound `92.045883/350` CNY. Detailed versioned evidence: `docs/experiments/20261004-strict-rag-reflection-acceptance.md`; private execution evidence `/path/to/project-context/evidence-experiment-acceptance-20261004/`.

Production is `/opt/agent` on the Tencent Hong Kong host, serving
`https://app.kaipingrc.com/`. Verified 2026-09-26. The Mac launchd runtime is retired;
starting its writers would create a second production authority.

## 2026-10-02 single-mainline launch day (all commits deployed, verified by container hash)

Full-day record; every item below is live in production unless noted.

**Issuance rehearsal (zero production writes)** — backed up `/data/agent.db`
via sqlite backup API to `/data/replay/rehearsal-20261002.db`, ran the exact
lifecycle command with `--as-of 2026-10-03T08:00:00+08:00` against the copy:
settlement inserted 5 outcomes (quality gate clear), fusion produced all 21
audit rows (quiet evening → all R4, zero rewrites, byte-identical baseline —
correct no-`-fused` behavior). Copy deleted after verification.

**Bug fixed from the rehearsal** (`490a84a`): `run_lifecycle` unconditionally
clobbered `fusion_summary` with the `no_chain_report` sentinel after the apply
branch computed the real outcome — the daily report would have lied about
fusion forever. Sentinel now initialized pre-branch; test locks honest
reporting when a chain report is passed.

**Evidence page starvation** (`9885d56`): 277 crude claims, zero admitted —
generator/verifier mismatch (date and normalized subject sit beside the quote,
verifier only looked inside the quote span). Binding now falls back to the
enclosing sentence (verbatim raw slice, ≤600 chars, cross-paragraph borrowing
and relative dates still rejected). Honest finding: this week's pool is mostly
market commentary, which correctly stays `needs_review` by design — zero
conversion is truthful, not a bug. The `issued` view 503s
(`main_evidence_reconstruction_mismatch`, fail-closed guard) until the
lifecycle re-seals inputs; heals at the next 08:00 run; `current` view serves.

**Standing alert** (`7058520`): health-probe now checks all seven targets'
current-view dossier every 10 min; zero admitted claims for two consecutive
probes writes `/data/public-health/evidence-alert.json` (streak + starved_since),
self-clears on recovery. First run fired immediately — starvation had been
silent.

**Conversation-2 hardening reviewed & deployed** (`4b9881a`, `f53006d`): the
40/day HTTP-attempt cap was dead code (`_spend_attempt` never called) — now
enforced in `DeepSeekJsonPort.complete_json` (verified: single spend site, no
double-charging, template fallback intact). Handoff files distinguish
missing/corrupt/stale on the canvas; unreadable chain report surfaces as a
lifecycle warning; new read-only `GET /api/v1/agent-governance/report/latest`.
Missed contract registration for that endpoint fixed in `9f59de2`
(`docs/openapi.yaml`; bidirectional test green).

**Frontend** (`ca99521`→`dac65d9`): single-mainline renames (当日事件精选 →
复盘校准, 8 nodes), 经验回灌 feedback edges, evidence card in 定案 voice,
then three fullness iterations ending at full-bleed framing + three
equal-width rows (operator-verified on 2560 canvas). Live bundle
`index-D8CFB8FQ`.

**Deploy record**: 4 images rebuilt twice (backend/scheduler/news-scheduler/
event-summary-worker), services restarted, container file hashes verified
against local (`agent_chain e8b6e331e868`, `main f1d295219f6b`,
`pipeline_graph 2f8272614ddd`, lifecycle `181d350116cf`, dossier
`11ba57c654884096`); governance endpoint returns today's posture; disk
83% after rehearsal-copy cleanup. Backend suites: 3043 → 3052 green across
the day; frontend check + e2e 172 green.

**Day-1 preflight (2026-10-02 22:40, caught a real breaker)** —
`/opt/agent/state/logs/` had become root-owned (root-run experiment logs on
10-01 daytime), so every ubuntu-user service died on its first log redirect:
**both poydty-daily (08:00) and poydty-brief (09:31) silently failed on
2026-10-02**; the day's data only existed because of the manual P0-1 runs.
Fix: `chown -R ubuntu:ubuntu /opt/agent/state/logs`, verified ubuntu write,
then ran poydty-brief end-to-end via systemd (rc=0, log written, no failed
units). daily.sh shares the same redirect pattern. Without this fix the
first normal-operation day would have failed at 08:00 sharp. Other preflight
items green: 10-03 batch absent (fresh issuance tomorrow), all six timers on
calendar (daily 08:00 / brief 09:31 / probe 10min / body 23:00 / vacuum Sun
05:00 / distill Mon 09:05), backend healthy on current images, disk 85%.
Note: the evidence-starvation alert may still be firing tomorrow — this
week's pool is market commentary that correctly stays needs_review; that is
honest, not an outage.

**Token spend context**: DeepSeek bill ~¥359 for 09-27→10-02 is dominated by
the one-shot 25y optimization replays (20,327 replay-ledger calls,
59.4M tokens) plus T2/T2Q case backfill; daily pipeline steady state is
¥1-3/day. Operator has topped up; full-replay acceptance runs stay funded.

## Release manifest practice (2026-10-03, co-owner consensus with the frontend GPT session)

Every deploy now records provenance here; server source tree is synced with the
deployed bundle's build inputs (source-behind-bundle divergence caught & fixed
today: /opt/agent/src was stale vs the live dist after rsync-only-dist routine).

**Rollback hierarchy (co-owner consensus 2026-10-03, refined by the frontend
session)**: restore SAVED artifacts first — `/opt/agent/releases/dist-*`
snapshots and recorded image digests below; rebuild-from-git is the last
resort only (dependencies/build env/uncommitted config make it non-reproducible).
Frontend rollbacks are independent (swap dist only); backend image digests are
restored only when backend changed. Keep the last two releases.

- Digests current as of 2026-10-03: backend `d73daf24…`, scheduler `fe197bf6…`,
  news-scheduler `1c4e5d47…`, event-summary-worker `c87400c8…`;
  dist snapshot `releases/dist-index-Br8c-JwX-20261003/`.
- **2026-10-03 audit-wiring release** — backend commits through `9f59de2` +
  wiring batch (rag 7209, /agent-lessons, governance badges, lessons drawer,
  revisions UI, dead-file removal, enum fix); frontend artifact
  `index-Br8c-JwX.js`; images rebuilt backend/scheduler/news-scheduler/
  event-summary-worker; /opt/agent/src synced to match (incl. the frontend
  session's delivered-but-uncommitted viewport/spec work, adopted as-is).
  Rollback: restore `releases/dist-*` snapshot first; image digests above
  (backend only if backend changed); rebuild-from-git is the fallback path.
  DB untouched (no migrations this release).
- **2026-10-02/03 earlier same-day deploys** (renames, fullness, distill fixes,
  conversation-2 hardening) share the same rebuild-from-git rollback path.

### Runtime-audit fix release (2026-10-03 15:45, GPT commits + ZCode review & release)

Content: f5e33d1 (frontend F1-F5 runtime-audit fixes + canvas edge routing),
f12e4d8 (graph node metrics preserved — the "调用 —/16" skeleton root cause),
334c1e3 (industrial runner test isolation), c5e121d (ZCode: three leaking
fixtures restored — blackboard was the session-long polluter behind the
evaluation trio's full-suite-only failures). Verification: backend 3054
passed, check clean, e2e 184/1 skipped (both re-run fresh by ZCode).
Artifacts: dist `index-B4O14J0K.js` (rollback snapshot `releases/dist-prev-1539/`,
prior pair also kept); images rebuilt ×4 (digests re-recorded at next
release); /opt/agent/src synced. Live check: /pipeline/graph metrics now
carry per-stage calls/fallback/rejected (预算卡数据源就位).

## 2026-10-03 operator decisions after the operation-sim report

- **Reflection module stays** (operator directive 2026-10-03 morning): despite the
  sim's -1.27pp net finding, poydty-distill.timer stays enabled and Monday 09:05
  will run as scheduled — the operator accepts the risk knowingly; the redesign
  path (switch-cell-only corpus, per-horizon settle band, advisory injection,
  re-pass the same A/B gate) is the way to make it help. Do not disable the
  timer without a fresh operator instruction.
- **Sim mainline cleaned to the final pair**: /data/replay keeps only
  sim-2025-lessons-{on,off}.json + their .days.jsonl (the A/B evidence the
  report cites); smoke outputs, DONE markers, and the three discarded lesson
  legs' strays removed. No sim screens/containers remain. The 25y challenger
  artifacts (25y-*.json) stay — they are the benchmark evidence the ledger
  cites by name.

### Lesson library imported (2026-10-03, operator directive "教训库也带上")

All 30 lessons from the 21-month operation sim were imported into production
`agent_lessons` via `insert_agent_lesson` inside the backend container
(ids prefixed `sim2025-`, metadata carries source/report/operator-decision).
`list_active_agent_lessons` returns all 30 to tomorrow's 08:00 chain, so
政局解读/历史经验 prompts now carry 21 months of simulated operating
experience. Import done knowingly against the sim's -1.27pp A/B finding;
Monday's distillation will merge live lessons on top (cap 30 active).

## 2026-10-01 deployment incident and standing rules (read before any rsync/DB touch)

The `agentchain-r1-20261001` release succeeded, but the deploy process caused
two self-inflicted outages that were fully recovered:

1. `rsync --delete` from the Mac tree deleted server-only directories
   (`bin/`, `cloudflared/`, parts of `state/`) because the local tree does not
   contain them. Recovery: tunnel credentials existed on the Mac
   (`~/.cloudflared/`, tunnel `YOUR_TUNNEL_ID`);
   `bin/` scripts were re-deployed from `scripts/cloud/` or rebuilt from the
   launchd templates; `health-probe` was smoke-verified (`status: success`).
2. Running ad-hoc `sqlite3` inside a **root** alpine container against
   `/data/agent.db` raced the backend's WAL: `user_version` writes had lived in
   the WAL since v24, the on-disk header never advanced past 24, and a
   root-side checkpoint dropped the WAL carrying the v25→v38 DDL and header.
   The database then failed closed exactly as designed (`migration*_conflict`).
   Recovery: the v39 migration's own pre-migration backup
   (`/data/migration-backups/agent.pre-migration.v38-to-v39.20261001T051550Z.sqlite`,
   integrity ok, same-day batches intact) was restored, the header repaired to
   38 and v39 applied in one single-process script with `wal_checkpoint(TRUNCATE)`.

Standing rules from this incident:

- **Never `rsync --delete` to `/opt/agent`.** Server-only paths include
  `bin/`, `cloudflared/`, `state/`, `data-agent.db`, `.env`, `playwright-report`.
  Mirror without `--delete`.
- **Never open `/data/agent.db` from a root container or host sqlite3 while
  services run.** Any DB inspection goes through `docker compose exec backend`
  (uid 10001) so WAL/SHM ownership stays correct.
- `PRAGMA user_version` writes live in the WAL until a checkpoint; a bare file
  copy or foreign-user open can observe a stale header. Treat
  `migration*_conflict` on startup as "someone opened the DB as the wrong user
  or a checkpoint raced", not as schema drift — check `/data/agent.db-wal`
  ownership before touching anything.
- Pre-migration backups under `/data/migration-backups/` are the recovery
  point of record; gzip them (never delete) when disk is tight. The 2026-10-01
  golden copies: `*.051550Z.sqlite.gz` and `/data/agent.db.corrupt-20261001.gz`
  (forensic copy of the damaged state).

Current release: `20261001T051525Z-07bf1e914fa54aed` (git `901edab`), v39 schema,
18-node pipeline graph live. Rollback image tags: `agentchain-r1-rollback:backend`,
`agentchain-r1-rollback:scheduler`. The daily chain at 08:00 on 2026-10-02 is the
first full live run of the agent chain (node A/B + fusion in shadow mode).

## Scheduling and diagnosis

- `poydty-daily.timer`: 08:00 Asia/Shanghai, every day. The seven-product daily
  chain still runs on weekends. `scripts/cloud/daily.sh` is installed as
  `/opt/agent/bin/daily.sh`.
- `poydty-brief.timer`: 09:31. The 08:00 intelligence step defers the brief
  before this scheduled publish time (`brief_status=brief_deferred_to_schedule`,
  exit 0; collection still runs). Clustering and the brief belong to this timer. `scripts/cloud/brief-publish.sh` publishes the
  industrial brief after its existing cutoff. Python uses the frozen Monday–Friday
  calendar; scheduled weekend calls record `skipped_non_business_day` before any
  database or provider access. An explicit invalid business date remains blocked.
- Inspect `systemctl status`, `/opt/agent/state/logs/`, and **actual**
  `/data/local-production/latest-status.json` inside the backend container.
  A healthy process or zero exit from a final echo is not evidence of a daily result.
- SQLite BUSY/LOCKED failures publish an explicit retryable status. Source automation uses the existing bounded 30-minute wait; the forecast lifecycle tries at most three times. The summary consumer records lock failures, retries on its normal polling loop (at least 30 seconds), and exits nonzero after three consecutive contentions. Durable model request reservations survive the failure. Corruption and unrelated errors remain terminal.
- Full backup integrity checks use a connection-local 64 MiB page cache; all integrity and restore checks remain enabled.
- Shell wrappers preserve daily/brief/snapshot/retention errors. Materialization is
  skipped after a failed daily chain. Existing 30-minute upstream readiness limits,
  budgets, calendar, formal-prediction gates, and retention policies are unchanged.
- Direction review accepts a daily observation label only when an independent, timezone-aware visibility timestamp proves it was available by the cutoff. Future observations and ambiguous naive timestamps remain excluded; 30-minute upstream waiting cannot repair a date-format mismatch.
- Do not start the long-running Compose `scheduler` service as well as the systemd
  daily timer. Build it for `compose run --rm scheduler ... --once` only.

## Required evidence in a migration

The delivery quality gate currently reads these historical regression artifacts
from `${CODEX_RUN_DIR}/full-chain-delivery/`:

- `full-chain-leakage-audit-latest.json`
- `full-chain-backtest-latest.json`
- `full-chain-75-acceptance-status-latest.json`

Preserve their exact contents, original timestamps and provenance. Their July 2026
results are historical regression evidence, **not current model qualification**.
Missing evidence blocks observation/report materialization. Never substitute a
fabricated passing report or relabel old evidence as today's evaluation.
The separate current 21-cell OOS evaluation remains authoritative for formal
promotion. Inspect both gates after migration; observation readiness does not
imply any of the 21 cells has passed.

## Narrow releases

1. Back up only affected source/configuration files and tag the previous Docker
   images. Preserve unrelated cloud changes; do not sync the whole Mac tree.
2. Install reviewed cloud wrappers, and generate paired metadata:
   `python3 scripts/cloud/write_release_identity.py --root /opt/agent --base-git-sha <verified-base> --frontend-output <staged-release.json>`.
   The content digest covers backend, frontend, contracts, registries, runtime
   scripts, geo assets and Compose definitions. `git_sha` identifies the base;
   `source_tree_dirty=true` explicitly records uncommitted cloud changes.
3. Build backend/scheduler images from the cloud source. Start only explicitly
   selected services with `--no-deps`; never use bare `compose up -d`.
4. Publish the staged frontend metadata only after the backend is healthy. Check
   frontend `/release.json`, backend live and ready identities agree. Preserve
   the previous metadata for rollback.
5. Test the previous image starts with an isolated, no-database import probe.
   Rollback restores the old file copies and image tags and recreates only the
   affected services. No database downgrade is involved in the 2026-09-26 patch.

On 2026-09-26 the running backend already used v5 while the disk checkout and
scheduled workers still used v4. The repair aligns workers with the existing
backend v5 code, and fixes default settlement to select each forecast's frozen
series identity. Existing v1–v4 crude forecasts continue to settle on EIA; new v5
forecasts use their futures contract. Never rewrite historical issue identities.

## Single prediction authority (2026-09-26 evening release)

`20260926T132603Z-d582fef5e4ac889b` makes the issued seven-product ledger the
current forecast for the overview, reports and assistant context. The normal
08:00 timer issues the next batch under `issue-calendar.v1`, with three internal
comparisons frozen against the same inputs. Today's batch is not reissued.
Calendar drift corrects observation gaps; it does not establish forecast skill.

Deployment evidence and both exact artifacts are retained on the host under
`/opt/agent/state/releases/prediction-main-20260926/`. The main image is
`sha256:9bfdbadfe213a6b450b37d513ebf43894c7a8df77b9cd697c222fab73b6d636b`.
The tested compatible rollback image is
`sha256:18c1cacf4dad36dd187c035e1f165188b8e655681a744baf3f1e20980e0bb09f`.

Emergency rollback, only while the daily service is inactive:

```bash
sudo bash /opt/agent/state/releases/prediction-main-20260926/activate-release.sh rollback
```

This rollback reads both formats and preserves ledger hashes. It deliberately
sets `PREDICTION_WRITES_PAUSED=1`: issuance/settlement fail explicitly until the
main release is restored. Do not rebuild the emergency image from the old disk
sources or clear the pause while it is running. Use the pinned artifact; no
database restore or downgrade is part of this rollback.

The production Dockerfile now assembles a minimal Python runtime, retaining
actual package metadata, certificates, timezone data and per-file fingerprints
in `/runtime-manifest.json`. Build tools and unrelated executables are omitted;
existing host operations call Python or their separate Alpine utility image.

## Disk retention (2026-09-29)

`bin/retention.sh` runs at the end of the daily chain (source of record:
`scripts/cloud/retention.sh`). It keeps the newest anchor per class, removes
`*.sqlite` backups inside one-off `/data/*-recovery-20*` and
`/data/*-integration-20*` directories older than 14 days (manifests, reports
and hashes are kept), bounds content-addressed report files, prunes dangling
docker images / stopped containers / build cache older than 7 days, keeps the
newest 4 non-rollback service image tags per repository plus all rollback
tags, and keeps the newest 8 release artifact directories. One-off cleanup on
2026-09-29 reclaimed the stale recovery sqlite copies: /data 26.5G -> 10.6G,
host disk 86% -> ~53%.

### Report/evidence display repair release (2026-10-03, Codex)

- Published source: `77f3a3719979520ee717f1a945061c4c8f63f963`. Commits: `27d13e9` (hide two report sections; preserve system analysis and frozen originals; price-report date/nylon corrections), `5d8c291` (record existing production viewport baseline, same SHA-256 as prior live source), `3cc96f4` (urllib3 2.8.0 security fix), `9a48531` (lint formatting), `77f3a37` (concurrent-read test exception capture). No P2 draft or paid replay included.
- Release identity: `20261003T085758Z-c229866754b2e92a`; content SHA-256 `c229866754b2e92a1698ee05bb2bd07c0d782cd0a05bc6eb39a7a4c5d0db61bd`. Tracked inputs from the commit plus generated dist/identity; dependencies and untracked P2 files excluded. The existing identity generator’s `source_tree_dirty=true` is a fixed provenance label, not a claim that tracked build inputs differ from this commit. Dist index SHA-256 `cbd244af83f11992fea1951f431b9c730f70f96159e078367721a960f9b65e7a`.
- Fresh checks: npm check exit 0; patched-dependency backend `3059 passed in 287.63s`; E2E `185 passed (5.2m), 1 skipped`, no retries; final affected-backend focus `61 passed`; changed-file Ruff/Semgrep and release-commit Gitleaks clean. urllib3’s two HIGH vulnerabilities resolved in the patched lock scan. Historical `Dockerfile.prediction-shadow` warning is outside this built/runtime artifact.
- Four images rebuilt: backend `sha256:5dfe93813abbcefa960f1ec04acb3ad13da796df41c9acf2af450781370ec9b8`; scheduler `sha256:f4b7e5be6374ece40a1ff8a3647b1d50388646e2b7b1d42122ea89998b6ee9d7`; news-scheduler `sha256:5504a45cb276ea055e763d6f78b0f450993b25a651a45164e7f2ba53d13bf77a`; event-summary-worker `sha256:16c7aa8129e85f8b82772c41f68311e910cb33326312ef69a16d35e12669ea3e`. Only the three previously active services restarted; scheduler remains stopped.
- Source tree synchronized (server/src/public/scripts, API contract/package metadata). No `rsync --delete`, database operation, migration, timer/config/provider/cap/direction change. Public `release.json`, health/live and health/ready all HTTP 200 and share this release ID/hash/SHA. Default Python UA gets edge 403; ordinary browser UA passes. Runtime urllib3 confirmed 2.8.0.
- Rollback: `/opt/agent/releases/report-evidence-20261003-3cc96f4/` retains `dist-before`, `source-before.tar`, `images-before.tar`, `images-before.txt`, and four `agent-<service>:rollback-20261003-3cc96f4` tags. Saved-image load rehearsal and old index comparison passed. Retag saved images, restore source archive, restart only backend/news-scheduler/event-summary-worker, restore saved dist. `activate-public.sh` contains the same automatic rollback on activation failure. Previous release snapshots also retained; no database rollback required.
- Two report sections are omitted from newly generated report text and hidden by the old-report reader. Underlying counterevidence/watch_items and analysis modules remain; frozen old downloads/checksums are preserved. Real source gaps stay highlighted; unsupported price-direction inference remains unverified.

### Agent canvas card release (2026-10-03, Codex)

- Commit `4bb2b64e12b1af1b91f5fe90ade71bc605456f35`: six frontend/test files only, no P2/backend semantic changes. Release `20261003T094451Z-ad5353b50003fbd2`, SHA-256 `ad5353b50003fbd286529ceea73899109f40e839b75ff6318003c4d9c574c9c1`.
- Fresh full frontend: `188 passed (4.7m), 1 skipped`, no retries; isolated build exit 0 and affected `53 passed (1.2m)`. Security clean (Gitleaks/Semgrep/npm audit; lock HIGH/CRITICAL scan). Unchanged backend reused same-session 3059-pass evidence.
- Four rebuilt images share digest `sha256:edbe9af0dc811812350d52375ed4e9e1b2e505855ed7a98d39871de9ae0f3aa6` through build-cache reuse. Source src/tests and release identity synchronized; only backend/news-scheduler/event-summary-worker restarted, scheduler remains stopped.
- Public release/live/ready HTTP 200 agree on identity. Lazy workflow chunk SHA-256 `ec99605ed5d054fa99859a9a46b2f95cb7dc92b28326cca30f24e994074fa837` matches the built artifact. Runtime fingerprint and urllib3 2.8.0 checked.
- Rollback `/opt/agent/releases/card-nodes-20261003-4bb2b64/`: saved dist/source/four images and digests; saved-image load, old-dist comparison and no-network/no-volume app import rehearsal passed. No DB/migration/provider/config/timer changes, no rsync delete.

### Event evidence and candidate source-clock repair release (2026-10-03, Codex)

- Published runtime/source commit `78888aea039cf9e530fd263675ac839b1e37e6c0`: `9f90eb0` source parsing/conditional graph/selector gates, `666dde7` mechanical lint (all prompt/SQL strings unchanged), `4743159` precise refusal-text test allowance, `78888ae` workbench-ready fixture waits. Unpublished P2 draft, paid RAG replay, ranking/16/40 quotas and R1-R5/O1 excluded. ADR: `docs/adr/20261003-event-evidence-time-and-transmission.md`.
- Release `20261003T103937Z-5e0df688bb91eb8b`; content SHA-256 `5e0df688bb91eb8ba90a7e5ff9ae935913e498715c6459eb5248282b559b6343`. Exact isolated tracked inputs; whole lazy workflow chunk SHA-256 `9e0b597cc86a4a401f82fbcf9e146caa7ceaaa6f771061209adaaa129cfa4282` matches public asset.
- Fresh acceptance: `npm run check` exit 0; full backend `3098 passed in 397.88s (0:06:37)`, mechanical-after-import `54 passed in 3.25s`; full E2E `189 passed (5.9m), 1 skipped`, zero retries. Prior repeated runs exposed one false-positive assertion and two fixture races; fixes retain contract assertions, dedicated repeats pass. Whole server Ruff clean; offline AI 21/21 and deterministic RAG 9/9 (not a paid historical experiment); Gitleaks/Semgrep/npm audit and locked-dependency HIGH/CRITICAL scan clean.
- Four runtime images rebuilt, common final digest `sha256:dc6beb57f50e5133907ac0b346a3f0e6b8f706b9bc17ab46f3d76067ee784ab3`. Full Dockerfile build at `666dde7`; later three test-only changes have byte-identical runtime inputs, final paired identity and its manifest fingerprint rebuilt as an identity layer. All 8,601 runtime file fingerprints verified; runtime urllib3 2.8.0. Only prior backend/news-scheduler/event-summary-worker services restarted; scheduler stays stopped. Tested server/src/tests/contracts and operational source files synchronized. No database, migration, provider/environment/timer or old forecast rewrite.
- Independent release GO before activation. Public release/live/ready HTTP 200 share final identity; static lazy chunk matches. Frozen post-release snapshot `2026-10-03T10:50:17.838158+00:00`: POY displays 16 conditional non-quotation paths, each matches its original crude claim quote and URL; support/counter episode counts remain 0/0 for POY and crude. Product material counts POY 3 / crude 304. This is improved material visibility, not evidence promotion or a claim of healthy direction verification. Logistics/policy direction and unlinked geopolitical entity attribution remain unimplemented. Browser-control live navigation timed out; public API/static checks and local desktop/mobile E2E completed.
- Rollback `/opt/agent/releases/event-evidence-20261003-9f90eb0/`: old dist, source archive, four saved images/digests and rollback tags. Saved image load, old index comparison and no-network/no-production-volume app import passed. Activation checks exact candidate digests and traps errors to restore saved artifacts. No DB rollback needed; preserved previous release snapshots.

### Source-bound evidence and progressive loading release (2026-10-03, Codex)

- Operator explicitly approved commit/publication. Runtime commit `9043ba7be9a58d21251bc201d5f51142dfaf08da` contains 21 precisely staged files; unpublished P2, paid RAG replay and prediction rules/ledger/quota changes excluded. ADR: `docs/adr/20261003-source-bound-semantic-reviews.md`. Commit created in the isolated checkout `/path/to/project`; main checkout and its P2 work remain intact. No push.
- Release `20261003T143127Z-a15d04a21adf6255`, exact-input content SHA-256 `a15d04a21adf625542c6b84fa4626d78658e7b5078c5aa3d259bc41901b46e08`. Paired release/live/ready identities match the runtime commit. The fixed provenance label `source_tree_dirty=true` describes the deployed tree metadata, not mismatched reviewed inputs. Runtime/source outside the 21 approved files had no drift before activation.
- Fresh checks: `npm run check` passed; full backend `3157 passed in 368.92s (0:06:08)`; full E2E `190 passed (6.1m), 1 skipped`, retries disabled; whole-server Ruff clean; offline AI 21/21, provider calls 0; npm production audit 0 vulnerabilities; this commit Gitleaks 0; tracked Semgrep and separately scanned untracked new files 0. Whole-history invalid, unused FRED constant and unrelated experimental Dockerfile USER warning remain documented scan findings, not suppressed or represented as an all-green repository scan.
- Four images rebuilt with common digest `sha256:44b829f8d20b99829b6a08b2761734e7977dc751b74f10ed1ba498dea2ec01a6`. All 8,606 copied runtime file hashes verified, urllib3 2.8.0. Only backend/news-scheduler/event-summary-worker restarted; scheduler remains stopped. Exact approved server/src/tests/contracts/docs files plus identity synchronized; existing dist restored/published as saved/built artifacts, no rsync delete.
- Current-only semantic cache installed by production uid 10001 at `/data/evidence-semantic-review` (700 directory, 600 JSON), with `EVIDENCE_SEMANTIC_REVIEW_DIR` in the existing env file (operator-readable 600 retained). No model call is made by reads, no new timer/paid scheduler. Task cumulative reserve 72.44418/350 RMB over 266 HTTP attempts, not a verified actual invoice. Six source-bound materials: three upward-pressure / three downward-pressure conditional analyses; downstream displays these same upstream materials, not additional independent votes. Two cross-sentence associations retain sealed critic/model/source receipts. All original page quotes matched HTTP200 sources. Real-world effects remain unverified; direct POY episode counts remain 0/0.
- Public force-cold capture 14.635s vs pre-release 18.84s (~22% faster), warm capture 1.824s. This is an end-to-end observation, separate from the exact-equivalence 14k-price-record fixture's 7.15→0.53s speedup. Issued dossier claims and input hash unchanged, and later semantic rows absent from issued view. Browser verification/evidence paths: `agent-context/evidence-capability-20261003/public-final-*` in operator workspace (gitignored); public page dwell samples cover 10/35/80/120 seconds.
- Rollback `/opt/agent/releases/evidence-capability-20261003-9043ba7/`: saved dist, affected source archive, env backup (600), four saved images and rollback tags, candidate identity/digests, private source cache (not Git/static). Prefer saved artifacts: retag `agent-<service>:rollback-capability-9043ba7`, restore source/env, restart only prior three services, restore saved dist. New-path cleanup is an explicit approved-file list, not a recursive delete. Remove env cache setting to disable semantic projection; no DB rollback required. Earlier release snapshots retained.
- Activation first failed before source/image switch because the minimal old runtime lacked shell mkdir/tar/chmod. Automatic saved-artifact rollback ran; the cache-copy step was replaced with existing Python under uid 10001, and retry passed. Candidate manifest probe separately fixed its array/dict inspection mistake before activation; no integrity assertion was omitted. No production DB connection/copy/migration/rewrite or additional replay experiment performed.
- Post-release actual-browser acceptance: evidence has all six conditional cards by the 10-second sample and six corresponding nodes/edges after 120 seconds; source receipts open normally, no API failures or blurred loading surfaces at 10/35/80/120 seconds. Intelligence completes by the 35-second sample (still slow, not claimed instant); reports complete by 10 seconds, stable through 120. Previously hidden report sections remain absent from rendered report text. Public screenshots are genuine runtime captures, not fixture overlays.

### Frontend reading repair publication (2026-10-04, Codex)

- Published runtime commit `8346530a743470dd691626768856e2ea4bd7fb8a`: `9102c6a95fc59c16e472035671b6a49a9d824d54` applies the exact `6df397d` 24-file reading repair on the current production baseline; `8346530` waits for outstanding E2E route callbacks at teardown without weakening assertions. The isolated checkout preserves already-published source-bound semantic evidence capability; unrelated main-checkout P2 drafts excluded. No push.
- Release `20261003T164837Z-ac8a59c2a44f8195`; exact-input content SHA-256 `ac8a59c2a44f81951a5587c11c38bece549c8ed9fe955928ccd33e3c70e665a8`. Fixed `source_tree_dirty=true` provenance metadata identifies the deployed build tree; reviewed inputs are archived separately. Public release/live/ready identities agree; deep health healthy. Public index and every referenced entry asset match candidate bytes exactly.
- Fresh validation: `npm run check` exit 0; backend `3168 passed in 306.00s (0:05:06)`; full E2E `193 passed (4.8m), 1 skipped`, retries disabled. Initial E2E teardown race was repaired, repeated five times successfully, then the full suite rerun. Whole-server Ruff clean, offline AI 21/21 (provider calls 0), Semgrep 0 ERROR findings, release-range Gitleaks 0 leaks, production npm audit 0 vulnerabilities, lock and final runtime image scans 0 HIGH/CRITICAL. All 8,608 candidate runtime fingerprints verified. Existing build chunk-size warning remains.
- Four images rebuilt with digest `sha256:601f6a0193a6360072ae19f0b1790789c4ceed1731daaa4a163268a7569dc3c8`. Only backend/news-scheduler/event-summary-worker restarted; backend healthy and workers running, scheduler remains stopped. Approved source/test/contract/doc files plus release identity synchronized. Six prior conditional semantic cache materials preserved under production uid 10001. No DB access/migration, provider/config/budget/direction/timer change, paid calls or rsync delete.
- Rollback `/opt/agent/releases/frontend-reading-20261004-9102c6a/` holds `dist-before`, `source-before.tar`, `images-before.tar`, recorded digests and four `agent-<service>:rollback-reading-9102c6a` tags. Saved-image load and isolated no-network/no-volume old-app import rehearsal passed. `bash /opt/agent/releases/frontend-reading-20261004-9102c6a/activate.sh rollback` restores saved artifacts; no database downgrade required. Earlier snapshots retained.
- Public four-stage node details return real current code configuration/system prompts with matching source hashes; these are explicitly not historical call snapshots. Actual browser DOM confirms all 19 rendered cards and new role labels. Browser tool repeatedly times out on drawer clicks/screenshot capture, so complete post-release visual acceptance is unverified. Reports preserve frozen originals/download hashes; reader separates price observations from event judgments. Radar missing-analysis gaps remain honest rather than fabricated content.
- Explicit pre-activation gate GO: `/path/to/project-context/frontend-public-20261004/release-gate.json`. Fresh check/security/activation/public acceptance logs and candidate identity are retained in that directory. Release documentation is a follow-up only and does not change the runtime identity above.

### Execution relay cards publication (2026-10-04, Codex)

- User selected prototype 2 and authorized implementation, exact-file commits and public publication. Runtime commit `73aacef934c9df83320041fe5ea1d3734a2d5927`; commit range `4b50ed2`, `444a478`, `acf8f72`, `73aacef`, based on the current public `8346530` runtime and `0ad1b14` documentation checkout. Main-checkout unpublished P2/evidence drafts preserved; no push.
- Changed files: `src/components/PipelineRoutedEdge.tsx`, `src/components/pipelineCardGeometry.ts`, `src/components/pipelineRelayContracts.ts`, `src/fixed-viewport.css`, `src/pages/AgentWorkbenchPage.tsx`, `tests/app.spec.ts`, `tests/pipeline-relay-cards.spec.ts`, `tests/workflow-card-design.spec.ts`. All 19 cards use numbered “收到 / 执行 / 交出” rails, solid blue stage badges and colored status strips. Static inputs/outputs explicitly say “约定”; observed execution remains graph status_detail. No fabricated demo figures or request fan-out. Body text 18px, titles 22px in canvas coordinates. Shared card geometry keeps interaction gutters clear of taller cards; existing three-row positions, fitView and portal layering retained.
- Release `20261003T175826Z-034645b6fa503831`; content SHA-256 `034645b6fa503831ed6c7194e6a1b130eef12f5feb6c1e4e7c83de4371e99c49`. Public release/live/ready identities agree; deep health healthy. Every public index entry asset matches candidate bytes. Nine approved source/test/identity files hash-verified on `/opt/agent`; release documentation follows separately.
- Validation: `npm run check` exit 0 (existing chunk-size advisory); full browser E2E `195 passed (15.1m), 1 skipped`, retries disabled, all `tests/*.spec.ts`; proxy Node unit tests `30 passed / 0 failed` in isolated workers. Targeted relay/design and routing checks 4/4 each. Native Chrome public screenshot and actual drawer click verified. Topology assertions now match exact node titles rather than entire card text; rule/issuance assertions match handoff contracts. Long state test verifies real metrics and no paragraph overlap. Old mixed-framework runner polluted unit-test environment; complete proxy suite passed using its own Node runner without changing tests.
- Backend/script/build source is byte-identical to the same-conversation tested `8346530` runtime: `3168 passed in 306.00s`; evidence and source hashes in `backend-evidence.json`. A redundant extra run was interrupted after 1,832 passes to avoid duplicating unchanged-backend checks; it is not claimed as complete. Ruff clean, offline AI 21/21 with provider calls 0. Semgrep 0 ERROR findings, four-commit Gitleaks 0 leaks, production npm audit 0 vulnerabilities, frozen lock and fresh saved-runtime image scans 0 HIGH/CRITICAL.
- Initial lock-based image reinstall produced unexpected byte differences in py_mini_racer and an extra evaluation fixture, so that candidate was rejected. Final four images rebuilt from saved, pinned `sha256:601f6a0193a6360072ae19f0b1790789c4ceed1731daaa4a163268a7569dc3c8` runtime, using release-local `Dockerfile.release`. Final digest `sha256:71073f6d530b36fd373ae5d5cb344bd05dcfe37c4f8a60719e9287166f18862a`. All 8,609 runtime/OS metadata fingerprints checked; only `/app/server/release.json` differs from the freshly scanned baseline, plus its manifest bookkeeping. Third-party source/OS metadata preserved byte-for-byte. Do not treat lock reinstall as equivalent to the saved runtime in a future backend release without investigating that packaging drift.
- Only backend/news-scheduler/event-summary-worker restarted. Backend healthy; workers running; scheduler remains stopped. Six previous conditional semantic materials preserved using production uid 10001. No database migration, forecast/verification rule, API contract, provider configuration, timer or paid-call request change.
- Rollback `/opt/agent/releases/relay-cards-20261004-4b50ed2/`: `dist-before`, `source-before.tar`, `images-before.tar`, recorded before/after image digests and four `rollback-relay-4b50ed2` image tags. Saved image load and isolated no-network/no-production-volume old-app import passed. Run `bash /opt/agent/releases/relay-cards-20261004-4b50ed2/activate.sh rollback`; no database downgrade. Earlier release snapshot retained.
- Known differences from prototype: inputs/outputs are contracts because per-run artifacts require the drawer; demo counts were not copied. Type badges share the status strip to fit narrow cards. Long state clips at two lines on budget cards and three elsewhere; full state remains in tooltip/accessibility label/drawer. Full-canvas fitView scales cards smaller than the three-card prototype; existing zoom remains available.
- Pre-activation readiness decision GO and fresh mechanical evidence: `/path/to/project-context/relay-cards-public-20261004/`; `release-gate.json`, `public-check.log`, `source-verified.log`, `post-runtime.log`, `public-final.png`, `public-drawer.png`. Release-local recipe and manifest comparison retained with cloud snapshot. Packaging drift investigation is outside this front-end publication; the published artifact retains the known validated runtime.

## 2026-10-04 Agent relay card space follow-up

- User request: prioritize the crowded middle prediction row and compress surrounding rows. Runtime commit `b9ec12ecf0d7ab998434945c48ab4ede4d7e14e1`; five exact source/test files committed, pending main-worktree P2/evidence drafts excluded.
- Card height in graph coordinates: middle 380 → 480; upper/lower 380 → 330. Lane header reservation 120 → 100; body text remains 18px. Mainline execution supports four lines; surrounding cards two lines, full state retained in drawer/tooltip/accessibility. Card identities/widths, contracts, API and prediction semantics unchanged.
- Fresh check passed; browser E2E `195 passed (13.8m)`, `1 skipped`; Node proxy tests 30/30. Routing at 1920/1440 and drawer passed. Final card acceptance 2/2. Initial narrower title reservation crossed feedback keywords and was corrected before release; an initial concurrent fixture wait exceeded 5 seconds, retested after aligning wait with page loading time.
- Backend source: 624 fingerprints unchanged from the preceding same-conversation full 3168-pass backend validation. No redundant backend run claimed. Fresh Semgrep 0 ERROR, npm production 0 vulnerabilities, Trivy HIGH/CRITICAL 0; Gitleaks full history flagged an unchanged unused declared placeholder, manually adjudicated with evidence (not claimed raw zero).
- Release `20261003T182814Z-62729aad7fca084a`; content SHA `62729aad7fca084a854be6679733e014345d8d7bb379d3efa938c079c7cac925`. Four images rebuilt from pinned current runtime; digest `sha256:11d465dd7e7e82e029a628cacad41d6f76faf0645b8cb90de564eaa838ed6d75`. All 8,609 actual file/OS fingerprints checked; only release metadata differs. No dependency reinstall or backend behavior/config/DB/timer change.
- Source tree sync: five approved source/test files plus generated paired server release identity; six hashes verified. Public release/live/ready identities agree, deep health healthy, all entry assets byte-match candidate. Native Chrome public canvas and drawer visually checked after hard refresh.
- Rollback snapshot `/opt/agent/releases/relay-space-20261004-95d4749/`: saved dist, approved source tar, four current images and digests. Saved image reload and isolated old-app import passed. Restore via `bash /opt/agent/releases/relay-space-20261004-95d4749/activate.sh rollback`; previous snapshot retained.
- Pre-activation release readiness GO. Evidence: `/path/to/project-context/relay-space-public-20261004/` (release-gate.json, e2e.log, runtime-diff.json, activation.log, public-check.log, public-final.png, public-drawer.png). Remaining visual limit: eight cards still share one horizontal row; fitView scales the complete diagram, zoom remains available. Existing build chunk advisory unchanged.

## 2026-10-05 RAG 与背景反思接入及对外文案发布

- 操作员取消效果回放，讨论风险后明确“都接上”；批准影响后续发行。机制提交 `c83b378b7719d4bb353772ea58a2f95928592fff`，展示提交 `5a7e04ef25f30495b9ddbac818e7fb9a8f704294`、`48e787408c452382eb726c2562abf93b0a1c1642`。与取消的实验相关的脏文件未发布，未 push；无数据库迁移、旧发牌重跑、客户正式报告重启或定时器变更。
- 三个独立开关在 backend 与 scheduler 配置均为1：召回、召回计票、背景教训。来源及时刻合格的召回进入历史经验候选，只能作用于被引用的同品种/期限；定案重核验凭据。反思使用到期改写格和发行时冻结的分析带，少于20个合格样本跳过；教训以背景假设进入推理，保存来源、样本和准确可用时刻。R1–R5/O1、40 HTTP尝试硬顶和21格账本语义保持不变。
- 首批机制发布 `20261004T215209Z-f2c53bd50607806f`，镜像 `sha256:966781ac7ae3f8023d5cc21a857cd53888af8f82cad5957e642ad216c9476275`；随后按操作员要求移除业务画布的内部实验待办与效果验收措辞，保留真实失败/未知、来源和治理资格。中间文案版 `20261004T221241Z-74a87af5694b5514`，镜像 `sha256:bff74128769f154bebaeeaca80328ed5569c05a4a9856b206f10b01bbff1ee58`。
- 最终发布 `20261004T222208Z-a36349e5574c635f`（UTC），content SHA-256 `a36349e5574c635f6c4b00c23b691284da68d487c1042bfb02c9e8093fc95936`，源码基准 `48e7874`。四镜像 backend/scheduler/news-scheduler/event-summary-worker 同 digest `sha256:cea0b9821eeb7429fd20c7ede5aa2f9c8a34d5a7cb281677370840a869af0148`。两次文案构建均从已验证当前运行镜像派生；实际运行指纹仅 release.json 改变，未重装依赖。
- 新鲜验证：后端 `3410 passed in 472.75s (0:07:52)`，DG01隔离目录与TMPDIR均0700；机制版浏览器 `209 passed (6.0m), 1 skipped`，文案版 `209 passed (5.0m), 1 skipped`，最终版 `209 passed (10.4m), 1 skipped`，全部 retries=0；每次 npm check exit0，保留原有 chunk-size 提醒。AI离线21/21、RAG离线9/9，模型API调用0；这些不是效果回测。契约映射和根因记录结构检查通过；根因记录保持open等待真实运行与持续观测。
- 安全：Semgrep无ERROR、新增两次文案提交Gitleaks无发现，npm生产审计0；实际完整运行镜像（含.venv）Trivy HIGH/CRITICAL及secret 0。全历史Gitleaks原始exit1，只有已失效FRED历史记录 `d4ac534...:scripts/experiments/build_replay_25y.py:generic-api-key:52`，沿用操作员对本批的单条豁免，2026-10-11 05:51:33 UTC到期。无扩大匹配/延期/改写历史，不能称原始扫描零告警。
- 发布前显式readiness gate `GO WITH WARNINGS`，风险接受与原始证据存放 `/path/to/project-context/system-chain-resume-20261005/`、`system-chain-copy-20261005/`、`system-chain-copy-final-20261005/`。新效果实验已取消，新增RAG/反思改善未被证明；旧模拟−1.27pp保留在内部实验文档，不能冒充新版结果。公开页只说明能力、规则和运行依据，不添加增益承诺。
- 发布后uid10001只读核对：live/ready均与静态及后端release身份一致，ready；18后端节点、召回与计票及背景教训能力启用。21个交付源文件哈希、8638个运行文件指纹匹配。发行前后34批/714格/392个结算记录内容哈希一致。当前尚未到08:00，不能把开关启用当作已产生召回改写或新教训。原有治理状态未晋级。
- Native Chrome公网实际导航、RAG抽屉及记忆卡核对通过：“统一研判主线 · v2”“召回计票已启用”“同向 ≥3 条独立互证才有计票权”可见，无新增试运行/效果未验收文案。已有浏览器页面需重新加载制品；版本查询参数用于实际验收，未绕过访问控制。
- 备份回滚：`/opt/agent/releases/live-memory-20261005/`、`memory-copy-20261005/`、最终 `memory-copy-final-20261005/` 分别保存前版dist、source-before.tar、env-before(0600)、四镜像tar/digests/tags。每版保存镜像加载、无网络无生产卷uid10001 app导入和index字节比较通过。最终回滚 `bash /opt/agent/releases/memory-copy-final-20261005/activate.sh rollback`；撤回机制则用首批回滚或关闭独立开关，保留已发行数据。仅重启backend/news-scheduler/event-summary-worker，scheduler保持停止，08:00按现有systemd调用；未rsync --delete、未root直连生产库。


## 2026-10-06 日链定案/来源日历/预算修复发布

- 操作员确认提交并发布。机制提交 `a5fd4f43f29cae31bd8c25c2e6fa58a8f83b7f9d`，依赖补丁与静态整理提交 `470fd9242b0d4ac09e4c278bb6be2e737873d14f`；包含此前 `b6b268d` 证据图谱标签重叠修复。未提交、未发布无关回放实验文件；未push。
- 发布 `20261006T055545Z-b3aa978195050d7d`，内容SHA-256 `b3aa978195050d7dcb72f4ebb59d9e7f7ea5f5f7047fcdb738c1c648d30c6a93`；前后端代码身份470fd92一致。四镜像同digest `sha256:ad07f1f1abfaa022179151c7a63908db3e49e321d93f8896f6872288423a669c`，从保存运行镜像派生，无后端依赖重装。8640文件指纹核对无越界变化。
- 新鲜验证：后端 `3453 passed in 330.48s (0:05:30)`；E2E `214 passed (5.9m), 1 skipped`；npm check与全server Ruff通过；离线AI评估通过，无手动provider实验；Semgrep ERROR=0、发布范围Gitleaks=0、npm漏洞0、Trivy锁文件与最终镜像HIGH/CRITICAL=0。修补间接source-map-js至1.2.2；历史review脚本仅格式/导入排序（排除imports后的AST相等）。
- 39个交付源码/文档/测试/身份文件逐字节同步；本运维及根因记录为发布后单独同步。公网标识和live/ready一致，deep healthy；15个入口资源与候选完全一致。公网HTML仅有已知Cloudflare统计脚本注入，剔除这一精确注入后与候选一致，非旧缓存。
- uid10001只读复核：今天21格发行API内容哈希前后完全一致；没有重跑日链、修改发行/结算账本、数据库迁移或定时器变更。保存的九输入在新逻辑下重评9/9；严重定案告警能识别今天旧报告。后续默认HTTP60/日、阶段16/16/7/7/2；今天旧budget快照仍为38/40，不重置。同日改帽失败关闭保护保留，下一业务日正常使用60。
- 回滚位 `/opt/agent/releases/daily-fix-20261006/`：dist-before、source-before.tar、env-before(0600)、四镜像归档/digests/tags；保存镜像加载和无网络无生产卷导入演练通过。回滚 `bash /opt/agent/releases/daily-fix-20261006/activate.sh rollback`，不回写旧账本；原有前版快照保留，未rsync --delete。
- 发布门GO WITH WARNINGS：保留既有分包提示/设计跳过；今日石脑油D7旧异常记录保留，10/7 08:00真实日链恢复未验证。首次发布门包装脚本误将预期API失败日志当测试失败，缺门文件触发保留制品回滚保护（未切换候选）；修正为测试摘要核验后重过门并发布。公网完整identity/原始HTML断言分别因受限公开字段/Cloudflare注入不成立，改为公开标识一致及精确注入排除，不豁免未知差异。
- 证据 `/path/to/project-context/daily-fix-public-20261006/`：release-gate.json、identity.json、backend.log、e2e.log、trivy-image.json、image-integrity.json、runtime-diff.json、post-runtime.json、public-check.json、source-after.json、activation-final.log。镜像扫描使用内容寻址blob逐一校验的候选重建归档（OCI manifest digest匹配），仅传输约570KB差异，避免反复传输相同底层。代码已上线不等于新真实日链已经成功；根因记录保持mechanism_verified。


## 2026-10-06 反证扫描/日报解读输出路径修复发布

- 操作员确认提交发布，机制提交 `8c29ed94b21572bbbb4c05c5f165d3742dfeff5e`。两模块复用共享路径解析，Docker默认落 `/data/local-production/{counter-scan,daily-interpretation}`；共享及单模块覆盖优先级保留。无方向、预算、引用规则、依赖、数据库迁移或定时器改动，无关实验文件未提交。
- 发布 `20261006T072334Z-f9ef2e575e7b24ef`，content SHA `f9ef2e575e7b24ef482d489882b64957cbf2a0b42ee9be5528e6f9c82ec489b5`；四镜像重新构建，同digest `sha256:bd3adb1c35bfd4bb88a80f20c28064253a736723b8a7158adbaf00c0ec2f42d8`。8640运行文件仅两模块及release身份变化，无依赖重装。四个提交文件及身份文件同步；本记录与RCA为发布后文档同步。
- 新鲜验证：修复前4失败/3通过，修复后针对性109通过；后端 `3457 passed in 263.85s (0:04:23)`；E2E `214 passed (5.1m), 1 skipped`；npm check、全server Ruff、根因结构检查通过；离线AI21/21，无provider调用。Semgrep ERROR=0，发布范围Gitleaks=0，Trivy锁与精确最终镜像HIGH/CRITICAL=0。
- 最终镜像隔离探针：UID10001、根目录只读、无网络、仅tmpfs、无生产卷，两个模块实际原子写入及读取成功、权限0600。线上只读核对实际路径正确，公网/live/ready版本一致、deep healthy；今天21格发行API SHA `44d5ca48de36645128bd580336ffe3f3d47c1b4f03987ff93eea322b11f55cb2` 前后一致。未补跑日链、未重置预算、未修改旧降级报告。
- 保存回滚位 `/opt/agent/releases/post-snapshot-20261006/`：dist-before、source-before.tar、env-before(0600)、四镜像tar/digests/rollback标签，旧镜像保存加载与无生产卷导入验证通过。回滚 `bash /opt/agent/releases/post-snapshot-20261006/activate.sh rollback`。仅重启backend/news-scheduler/event-summary-worker，scheduler保持停止。
- 发布门 GO WITH WARNINGS：既有分包提示/设计跳过保留；下一次真实日链产出尚未观察，RCA保持mechanism_verified。新发布不等于今天旧报告自动恢复。证据 `/path/to/project-context/post-snapshot-release-20261006/`，包括release-gate.json、runtime-diff.json、path-smoke.json、安全报告和activation.log。


## 2026-10-06 管道状态读取隔离发布

- 操作员确认提交并发布，机制提交 `f44322d3dd360dd8764a6f2ae1a0a2443bb38575`。九个修复/测试/根因文件精确提交，无关回放实验保持未提交、未发布；未push。graph/detail/文件事件依据脱离重检索锁，索引状态及其摘要校验使用只读连接；刷新失败保留上次数据并明确时间，首次失败仍为未知。未变更合同、方向、预算、迁移、定时器或provider配置。
- 发布 `20261006T075721Z-e63ec7f8bd4621ee`，content SHA `e63ec7f8bd4621ee7d2b258e8791c2f1098eb4ab0fab7f950067f3601191074b`。四镜像重新构建，同digest `sha256:b0fa516d16fcd6b630d0a18e75fc50d7caef341be2095d50a4b94226754a9472`。8640个运行文件指纹无越界变化，仅main/semantic_index/storage与release身份变化，无依赖重装。九源码/测试/记录及身份文件同步；发布后的本文/主史/RCA另提交同步。
- 新鲜验证：后端 `3461 passed in 247.49s (0:04:07)`；最终E2E `216 passed (5.1m), 1 skipped`, retries=0；npm check、全server Ruff、根因结构通过。旧刷新失败断言与新需求冲突，改为验证保留和时间标记，固定文件后全套重跑绿；早两次失败日志保留而非称其通过。离线AI21/21、provider调用0；Semgrep ERROR0、新提交Gitleaks0、npm漏洞0、Trivy锁文件及精确最终镜像HIGH/CRITICAL与secret0。发布范围扫描不声称全历史无问题，既有失效历史key记录未扩展豁免范围。
- 最终镜像探针UID10001、根只读、无网络无生产卷：重检索持锁不阻塞三观测函数，独立临时数据库写事务期间索引与摘要只读查询通过。候选导入/旧镜像加载导入通过。
- 公网首次测量命中旧代理响应，排除其作为新代码证据；新查询与新容器日志对应：RAG28.082秒，graph5.444秒（后端4.100秒）并发HTTP200/18节点，文件依据3.700秒；此前graph36.662秒。仅一次并发样本，不声称p95。公网Playwright真实页面19卡/未知0/管道警告0，四阶段38、总账38/40保持今天旧账本。原生CUA连续两次超时，未声称原生工具通过；截图及DOM证据留存。
- live/ready/公网版本一致，deep healthy；今天发行API SHA `44d5ca48de36645128bd580336ffe3f3d47c1b4f03987ff93eea322b11f55cb2` 前后一致；无手动日链补跑、预算重置或生产库直连。RAG仍约28秒，后台行情写锁具体来源仍待调查，不能将本批称为全部数据库争用根治。发布门GO WITH WARNINGS，根因保持mechanism_verified。
- 回滚位 `/opt/agent/releases/pipeline-read-20261006/`：上一版dist/source-before.tar/env-before(0600)/四镜像tar及标签/digests；恢复 `bash /opt/agent/releases/pipeline-read-20261006/activate.sh rollback`，不回写数据库。只重启backend/news-scheduler/event-summary-worker，scheduler保持停止；未rsync --delete。证据 `/path/to/project-context/pipeline-read-release-20261006/`（release-gate.json、public-concurrency-fresh.json、public-browser.json/public-workflow.png、after-runtime.json、runtime-diff.json、安全报告及activation.log）。


## 2026-10-06 guarded radar body recovery

- 提交范围：前端40cbea3与雷达b139d2f；从f44322基线仅同步精确32文件，无关实验保持未提交、未发布。三站公开原文准入和pypdf依赖由操作员批准；ArabNews403继续阻挡。
- 制品：20261006T101232Z-a485876655aa9cc1，四镜像共用sha256:9b5ebb8a24522b0ed2af495717ffff8f54e82def9fd1de85f7ad61e68a0598f0。现有运行时保留，仅加锁文件哈希校验的pypdf6.19.0；实际67包审计无已知漏洞、镜像高危/严重/密钥0、运行时非批准差异0。Git历史失效密钥记录仍存在，不把制品扫描0称为历史全绿。
- 源码树与三站显式OUTBOUND_FETCH_HOSTS同步；只重启backend/news-scheduler/event-summary-worker，scheduler保持停止，未触发日链、未改预算/账本/定时器。公网release/live/ready三者身份一致。
- 操作员批准固定14篇恢复；UID10001在现有容器内完整预检并取得body-acquisition锁后，14篇原版本哈希匹配、保存旧article/summary再CAS写入，真实新可见时间保留，未重置重试状态。14完整正文、14已核验事实摘要、14篇材料对应13个当前证据关联事件的公网事实字段已验证；事实通过不等于传导或方向通过。
- 新鲜验证：后端3524passed247.28s，前端226passed5.2m/1skipped，check/Ruff/离线AI21项通过。追查中补百分比单位等价的防复发规则（原文percent可与%对应，必须数值严格相等且保留原文引句），最终后端3534passed238.35s；相关67项通过。其发布身份在后续条目登记。
- 回滚位：/opt/agent/releases/radar-recovery-20261006-b139d2f/，保存旧dist/source-before.tar/env-before0600/四镜像tar及标签。保存镜像加载、无网络无生产卷UID10001导入、旧index字节比对通过。运行activate.sh rollback恢复制品/源码/配置，不回写已发行账本；可变正文旧记录另保留于/data/reports/radar-recovery-20261006-b139d2f/body-recovery-journal，恢复数据须另明确范围，禁止拿整库回滚代替。
- 证据：工作树agent-context/radar-release-b139d2f/（release-gate、actualimage/runtimediff、public-current-linked14、public-radar截图、productionapply和journals）。ComputerUse超时，页面用真实Playwright浏览器验证；首次雷达列表30条9.6秒是单次样本，非延迟分位或所有事件完备性声明。


## 2026-10-06 雷达百分比核验防复发发布

- 机制提交 d670d2c5373f1666bb7608e3d5927ecf9253677b；仅数字单位等价核验及测试/记录变更。原文80 percent与80%可严格等价，180%、80.1%、无百分比单位的80仍拒绝；引句绑定不变，无追加正文写入、模型调用或预算调整。
- 发布20261006T104446Z-fd52dac85a64e30a，content SHA fd52dac85a64e30a03a3d771f604de055af54464e521de18d8fccd5e0bf933af；四镜像重建为sha256:b82475b6b0f94f184999eb7fe69fdc0b4145217c0c9259ce7438f056cef9e094。运行时仅event_summary_quality.py和release身份变化，依赖/OS无额外差异。精确源码与身份同步，发布后两份验收文档另提交同步。
- 新鲜后端3534 passed in238.35s，相关67 passed；全server Ruff、Semgrep ERROR0、新提交Gitleaks0、镜像HIGH/CRITICAL与全等级secret0。前端内容未再变化，沿用同批完整226 passed/1 skipped及check通过证据。首轮中文数字边界测试失败已修正后全量重跑，不计为通过。
- 实际容器UID10001数字核验正反例通过，backend healthy；公网release/live/ready均HTTP200且精确匹配新身份。发布后再次读取14材料对应的13个当前事件，全部HTTP200且有事实内容；基础设施ready不等于所有方向核验通过。未触发日链，既有news/summary服务运行。原生ComputerUse超时，实际浏览器验证替代并保留限制。
- 回滚位/opt/agent/releases/radar-percentage-20261006-d670d2c/保留上一版本dist/source-before.tar/env-before0600/四镜像归档及标签；保存镜像加载、无生产卷UID10001导入及index字节验证通过。bash该目录activate.sh rollback恢复制品，不回写数据库或已发行账本。发布门GO WITH WARNINGS，历史失效key告警未被称为全历史扫描通过。
- 验收证据在工作树agent-context/radar-release-d670d2c/：activation.log、identity.json、public-identity-browser.json、public-current-linked14.json、安全及运行时差异报告。范围外来源/403/短报价继续诚实标注；正文完整不等于因果判断或预测增益已验证。

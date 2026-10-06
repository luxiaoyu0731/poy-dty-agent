from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
SCRIPTS_ROOT = SERVER_ROOT / "scripts"
DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_CODEX_RUN = REPO_ROOT / ".codex-run"
DEFAULT_OUTPUT_DIR = DEFAULT_CODEX_RUN / "source-automation"

if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from runtime_guards import (  # noqa: E402
    RunLockError,
    configure_runtime_sqlite_path,
    exclusive_run_lock,
    expire_stale_news_fetch_runs,
    is_sqlite_contention,
)

from app.fetchers import Fetcher  # noqa: E402
from app.foundation_utils import safe_summary  # noqa: E402
from app.news import fetch_news_sources  # noqa: E402
from app.official_downloads import import_observations  # noqa: E402
from app.price_intraday import CHAIN_INSTRUMENTS, collect_intraday_prices  # noqa: E402
from app.public_source_adapters import backup_source_state, write_source_state  # noqa: E402
from app.source_acquisition import build_source_automation_status, record_acquisition_run  # noqa: E402
from app.source_automation_policy import (  # noqa: E402
    SourceCriticality,
    get_source_policy,
    select_due_source_ids,
    source_run_succeeded,
)
from app.source_registry import get_source  # noqa: E402
from app.sqlite_permissions import (  # noqa: E402
    remove_sqlite_artifacts,
    secure_private_directory,
    secure_sqlite_artifacts,
)
from app.storage import (  # noqa: E402
    bulk_upsert_futures_daily_bars_with_capture_revisions,
    record_source_fetch,
    upsert_event_observation,
)
from app.user_file_import import process_user_files  # noqa: E402

PUBLIC_FETCH_SOURCE_IDS = (
    "eia_petroleum_api",
    "fred_macro_api",
    "cftc_cot_petroleum",
    "czce_pta_px",
    "cfets_cny_parity",
    "ofac_sanctions",
    "gacc_trade_statistics",
    "un_comtrade_api",
    "tnc_polyester_history",
)
SOURCE_STATE_SCHEMAS = {
    "ofac_sanctions": "ofac-snapshot.v1",
    "un_comtrade_api": "un-comtrade-backfill.v1",
}
SUBPROCESS_TIMEOUT_SECONDS = 90
BACKUP_TIMEOUT_SECONDS = 180
RUN_LOCK_STALE_SECONDS = 6 * 60 * 60
NEWS_RUNNING_STALE_MINUTES = 45
# Final backup state (disk consolidation 2026-09-17, DISK-MODEL §7): apply runs
# no longer take their own pre-write full backup. Recovery for source/news
# writes is covered by three layers -- SQLite transactional rollback, the
# unified daily anchor (RPO <= 24h), and re-fetchable news/price data (minutes).
# SOURCE_PREWRITE_BACKUP=1 (or --backup-db) restores the legacy pre-write backup
# for emergencies such as a risky manual apply outside the daily chain.
SOURCE_PREWRITE_BACKUP_ENV = "SOURCE_PREWRITE_BACKUP"


def prewrite_backup_enabled(args: argparse.Namespace) -> bool:
    if args.backup_db:
        return True
    return os.getenv(SOURCE_PREWRITE_BACKUP_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=("Run current public-source automation, public imports, news collection, and quality gates.")
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--codex-run", type=Path, default=DEFAULT_CODEX_RUN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--apply", action="store_true", help="Write imports/fetch results to the main database.")
    parser.add_argument(
        "--backup-db",
        action="store_true",
        help=(
            "Emergency only: back up the database before any write (default off; "
            "recovery is covered by the unified daily anchor, DISK-MODEL §7)."
        ),
    )
    parser.add_argument("--backup-reuse-seconds", type=int, default=86400)
    parser.add_argument("--backup-retention-count", type=int, default=3)
    parser.add_argument(
        "--backup-timeout-seconds",
        type=float,
        default=BACKUP_TIMEOUT_SECONDS,
        help="Hard timeout for the SQLite online backup before the run is blocked.",
    )
    parser.add_argument("--fetch-public", action="store_true", help="Fetch configured public API/official sources.")
    parser.add_argument(
        "--public-deadline-seconds",
        type=float,
        default=600.0,
        help="Outer budget for all public fetch attempts; must fit inside the daily caller timeout.",
    )
    parser.add_argument(
        "--force-public", action="store_true", help="Fetch every schedulable public source even when not due."
    )
    parser.add_argument(
        "--public-source-id",
        action="append",
        choices=PUBLIC_FETCH_SOURCE_IDS,
        default=[],
        help="Limit structured public fetching to one or more source ids; repeat the flag for multiple sources.",
    )
    parser.add_argument(
        "--skip-public-benchmark-refresh",
        action="store_true",
        help="Run only due structured sources; used by the short-cadence scheduler.",
    )
    parser.add_argument(
        "--import-trade-futures-proxy",
        action="store_true",
        help="Fetch and import the public Yahoo futures proxy independently of official-source scheduling.",
    )
    parser.add_argument(
        "--import-ccf",
        action="store_true",
        help="Deprecated compatibility flag. CCF is soft-removed and no import is performed.",
    )
    parser.add_argument(
        "--import-futures-daily",
        action="store_true",
        help="Import authorized futures daily CSVs from .codex-run/futures-daily.",
    )
    parser.add_argument(
        "--import-user-files",
        action="store_true",
        help="Process standard CSV/XLSX files and quarantine review-only documents from user-files.",
    )
    parser.add_argument(
        "--fetch-akshare-futures-daily",
        action="store_true",
        help="Fetch AkShare/Sina prototype futures daily bars for SC/PTA/PX; DCE/MEG is soft-removed.",
    )
    parser.add_argument("--fetch-news", action="store_true", help="Fetch configured public news/event sources.")
    parser.add_argument("--news-limit-per-source", type=int, default=20)
    parser.add_argument("--news-cursor-pages", type=int, default=3)
    parser.add_argument("--news-retries", type=int, default=2)
    parser.add_argument("--news-source-timeout-seconds", type=float, default=30.0)
    parser.add_argument("--news-skip-details", action="store_true")
    parser.add_argument("--run-quality", action="store_true", help="Run delivery data quality gate after data updates.")
    parser.add_argument("--as-of", default="")
    parser.add_argument(
        "--record-plan",
        action="store_true",
        help="Persist current acquisition plan to source_acquisition_runs.",
    )
    parser.add_argument("--lock-file", type=Path, default=None, help="Override the automation lock file path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.db = args.db.expanduser().resolve()
    args.codex_run = args.codex_run.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    configure_runtime_sqlite_path(args.db)
    lock_file = (
        args.lock_file.expanduser().resolve() if args.lock_file else args.codex_run / "locks" / "source-automation.lock"
    )
    try:
        with exclusive_run_lock(lock_file, stale_after_seconds=RUN_LOCK_STALE_SECONDS):
            return run_with_lock(args)
    except RunLockError as exc:
        payload = {"status": "locked", "lock": exc.payload}
        (args.output_dir / "source-automation-latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 75
    except Exception as exc:
        if not is_sqlite_contention(exc):
            raise
        # A busy writer is not a terminal source failure. The daily wrapper
        # already retries exit 75 within its bounded lock-wait window.
        payload = {
            "status": "locked", "error_code": "sqlite_contention",
            "lock": {"kind": "sqlite", "message": "SQLite writer busy; bounded retry required"},
        }
        (args.output_dir / "source-automation-latest.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(json.dumps(payload, ensure_ascii=False))
        return 75


def run_with_lock(args: argparse.Namespace) -> int:
    writes = args.apply and (
        args.fetch_public
        or args.import_trade_futures_proxy
        or args.import_futures_daily
        or args.import_user_files
        or args.fetch_akshare_futures_daily
        or args.fetch_news
        or args.record_plan
    )
    backup_path = ""
    if writes:
        if not args.db.exists():
            print(f"--apply requires existing database: {args.db}")
            return 2
        if prewrite_backup_enabled(args):
            try:
                backup_path = str(
                    backup_database(
                        args.db,
                        args.output_dir / "db-backups",
                        reuse_seconds=max(0, args.backup_reuse_seconds),
                        retention_count=max(1, args.backup_retention_count),
                        timeout_seconds=max(1.0, args.backup_timeout_seconds),
                    )
                )
            except OSError as exc:
                payload = {
                    "schema_version": "source_automation_run.v1",
                    "status": "blocked",
                    "writes_database": True,
                    "db_path": str(args.db),
                    "error": f"backup_failed:{exc.__class__.__name__}",
                    "detail": _safe_exception(exc, max_chars=500),
                    "guards": {
                        "backup_required_before_apply": False,
                        "backup_covered_by": "daily_anchor",
                        "prewrite_backup_requested": True,
                        "backup_created": False,
                    },
                }
                (args.output_dir / "source-automation-latest.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                print(json.dumps(payload, ensure_ascii=False))
                return 2

    started_at = now()
    stale_news_before = expire_stale_news_fetch_runs(args.db, stale_after_minutes=NEWS_RUNNING_STALE_MINUTES)
    before = build_source_automation_status(db_path=args.db, codex_run=args.codex_run)
    records_written = []
    records_planned = len(before["tasks"]) if args.record_plan else 0
    if args.record_plan and args.apply:
        records_written = record_plan(before["tasks"], db_path=args.db)

    public_fetch = {}
    if args.fetch_public:
        public_fetch = asyncio.run(
            run_public_fetches(
                apply=args.apply,
                db_path=args.db,
                force=args.force_public,
                deadline_seconds=max(1.0, args.public_deadline_seconds),
                source_state_dir=args.codex_run / "source-state",
                requested_source_ids=args.public_source_id or None,
            )
        )
        public_fetch["public_benchmark_refresh"] = (
            {"status": "skipped", "reason": "short_cadence_structured_sources_only"}
            if args.skip_public_benchmark_refresh
            else asyncio.run(run_public_benchmark_refresh(apply=args.apply))
        )
    if args.import_trade_futures_proxy:
        if not public_fetch:
            public_fetch = {"status": "completed", "items": [], "errors": []}
        public_fetch["trade_futures_proxy"] = run_trade_futures_proxy_import(args)

    ccf_import = {
        "status": "soft_removed",
        "exit_code": 0,
        "requested_by_legacy_flag": bool(args.import_ccf),
        "writes_database": False,
        "historical_rows_read_only": True,
    }

    futures_daily_import = {}
    if args.import_futures_daily:
        futures_daily_import = run_futures_daily_import(args)

    user_files = {}
    if args.import_user_files:
        user_files = process_user_files(
            inbox=args.codex_run / "user-files",
            report_dir=args.output_dir / "user-file-reports",
            apply=args.apply,
        )

    akshare_futures_daily = {}
    if args.fetch_akshare_futures_daily:
        akshare_futures_daily = run_akshare_futures_daily_fetch(args)

    news_fetch = {}
    if args.fetch_news:
        news_fetch = asyncio.run(run_news_fetches(args))
        news_fetch["stale_running_runs_marked_before"] = stale_news_before
        news_fetch["stale_running_runs_marked_after"] = expire_stale_news_fetch_runs(
            args.db,
            stale_after_minutes=NEWS_RUNNING_STALE_MINUTES,
        )

    quality = {}
    if args.run_quality:
        quality = run_quality_gate(args)

    after = build_source_automation_status(db_path=args.db, codex_run=args.codex_run)
    run_status = evaluate_run_status(
        public_fetch=public_fetch,
        ccf_import=ccf_import,
        news_fetch=news_fetch,
        quality=quality,
        user_files=user_files,
    )
    summary = {
        "schema_version": "source_automation_run.v1",
        **run_status,
        "started_at": started_at,
        "finished_at": now(),
        "writes_database": bool(writes),
        "backup_path": backup_path,
        "db_path": str(args.db),
        "codex_run": str(args.codex_run),
        "before": summarize_status(before),
        "after": summarize_status(after),
        "records_planned": records_planned,
        "records_written": len(records_written),
        "public_fetch": public_fetch,
        "ccf_import": ccf_import,
        "futures_daily_import": futures_daily_import,
        "user_files": user_files,
        "akshare_futures_daily": akshare_futures_daily,
        "news_fetch": news_fetch,
        "quality_gate": quality,
        "guards": {
            "credentials_logged": False,
            "calls_external_llm_provider": False,
            "ccf_operational_status": "soft_removed",
            "dce_operational_status": "soft_removed",
            "meg_current_source_gap": False,
            "meg_current_label_source": "sunsirs_public_commodity_assessment",
            # Final backup state (DISK-MODEL §7): pre-write backups are off by
            # default; recovery for these writes is covered by the daily anchor.
            "backup_required_before_apply": False,
            "backup_covered_by": "daily_anchor",
            "backup_created": bool(backup_path) if writes else False,
        },
    }
    output = args.output_dir / "source-automation-latest.json"
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"output": str(output), "writes_database": writes, "backup_path": backup_path},
            ensure_ascii=False,
        )
    )
    return 1 if run_status["status"] == "blocked" else 0


def record_plan(tasks: list[dict[str, Any]], *, db_path: Path) -> list[dict[str, Any]]:
    rows = []
    for task in tasks:
        rows.append(
            record_acquisition_run(
                db_path=db_path,
                source_id=str(task["source_id"]),
                task_name=str(task["task_name"]),
                dataset_type=str(task["dataset_type"]),
                status=str(task["status"]),
                automation_level=str(task["automation_level"]),
                requires_computer_use=bool(task["requires_computer_use"]),
                requires_human_action=bool(task["requires_human_action"]),
                target_table=str(task["target_table"]),
                expected_fields=list(task.get("expected_fields", [])),
                blocking_reason=str(task.get("blocking_reason", "")),
                next_step=str(task.get("next_step", "")),
                summary={"coverage_scope": task.get("coverage_scope", "")},
            )
        )
    return rows


async def run_public_fetches(
    *,
    apply: bool,
    db_path: Path,
    force: bool = False,
    deadline_seconds: float = 600.0,
    source_state_dir: Path | None = None,
    requested_source_ids: list[str] | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    change_totals = {"inserted": 0, "updated": 0, "unchanged": 0}
    errors: list[str] = []
    fetcher = Fetcher(source_state_dir=source_state_dir)
    states = load_public_fetch_states(db_path)
    if requested_source_ids:
        requested = list(dict.fromkeys(requested_source_ids))
        selected_ids = (
            requested if force else [source_id for source_id in requested if source_id in select_due_source_ids(states)]
        )
    else:
        selected_ids = list(PUBLIC_FETCH_SOURCE_IDS) if force else select_due_source_ids(states)
    selected_ids = [source_id for source_id in selected_ids if source_id in PUBLIC_FETCH_SOURCE_IDS]
    deadline_monotonic = perf_counter() + max(1.0, deadline_seconds)
    for source_id in selected_ids:
        policy = get_source_policy(source_id)
        source = get_source(source_id)
        if source is None:
            errors.append(f"{source_id}: source not found")
            continue
        started = perf_counter()
        source_started_at = datetime.now(UTC).isoformat()
        try:
            result, _attempts = await fetch_public_source_with_retries(
                fetcher,
                source,
                timeout_seconds=policy.timeout_seconds,
                deadline_monotonic=deadline_monotonic,
            )
        except Exception as exc:  # noqa: BLE001 - per-source isolation; transient errors were retried above.
            duration_ms = round((perf_counter() - started) * 1000)
            if isinstance(exc, TimeoutError):
                status = "timeout"
                content_type = "timeout"
                if "public_fetch_outer_deadline_exhausted" in str(exc):
                    error = f"public_fetch_outer_deadline_exhausted_after_{deadline_seconds:g}s"
                else:
                    error = f"source_timeout_after_{policy.timeout_seconds:g}s"
            else:
                status = "error"
                content_type = "error"
                error = _safe_exception(exc)
            errors.append(f"{source_id}: {error}")
            if apply:
                record_source_fetch(
                    audit_id=str(uuid4()),
                    source_id=source_id,
                    status=status,
                    content_type=content_type,
                    preview_chars=0,
                    duration_ms=duration_ms,
                    error=error,
                    started_at=source_started_at,
                )
            items.append(
                {
                    "source_id": source_id,
                    "status": status,
                    "criticality": policy.criticality.value,
                    "observations": 0,
                    "inserted": 0,
                    "updated": 0,
                    "unchanged": 0,
                    "content_type": content_type,
                    "duration_ms": duration_ms,
                    "error": error,
                }
            )
            continue
        duration_ms = round((perf_counter() - started) * 1000)
        result_observations = list(getattr(result, "observations", []) or [])
        result_events = list(getattr(result, "events", []) or [])
        result_capture_revisions = list(getattr(result, "capture_revisions", []) or [])
        result_futures_daily_bars = list(getattr(result, "futures_daily_bars", []) or [])
        state_backup_path = ""
        pending_state_update: dict[str, object] | None = None
        if apply and result.state_update is not None:
            try:
                if source_state_dir is None:
                    raise RuntimeError(f"{source_id}: state update has no configured state directory")
                state_path = source_state_dir / f"{source_id}.json"
                backup = backup_source_state(
                    state_path,
                    backup_dir=source_state_dir / "backups" / source_id,
                    retention_count=10,
                    expected_schema=SOURCE_STATE_SCHEMAS.get(source_id),
                )
                state_backup_path = str(backup or "")
                pending_state_update = dict(result.state_update)
                pending_state_update["last_run_started_at"] = source_started_at
                pending_state_update["next_due_at"] = (
                    datetime.fromisoformat(source_started_at) + timedelta(seconds=policy.frequency_seconds)
                ).isoformat()
            except Exception as exc:  # noqa: BLE001 - corrupt state must block before any database write.
                error = _safe_exception(exc)
                errors.append(f"{source_id}: {error}")
                record_source_fetch(
                    audit_id=str(uuid4()),
                    source_id=source_id,
                    status="error",
                    content_type=result.content_type,
                    preview_chars=len(result.content_preview),
                    duration_ms=duration_ms,
                    error=error,
                    observations_fetched=len(result_observations) + len(result_events),
                    started_at=source_started_at,
                )
                items.append(
                    {
                        "source_id": source_id,
                        "status": "error",
                        "criticality": policy.criticality.value,
                        "observations": len(result_observations),
                        "events": len(result_events),
                        "inserted": 0,
                        "updated": 0,
                        "unchanged": 0,
                        "content_type": result.content_type,
                        "duration_ms": duration_ms,
                        "error": error,
                        "state_backup_path": state_backup_path,
                    }
                )
                continue
        changes = {"inserted": 0, "updated": 0, "unchanged": 0}
        if result_observations:
            try:
                import_result = import_observations(
                    result_observations,
                    capture_revisions=result_capture_revisions,
                    apply=apply,
                )
            except Exception as exc:  # noqa: BLE001 - isolate parser/import failures to this source.
                error = _safe_exception(exc)
                errors.append(f"{source_id}: {error}")
                if apply:
                    record_source_fetch(
                        audit_id=str(uuid4()),
                        source_id=source_id,
                        status="error",
                        content_type=result.content_type,
                        preview_chars=len(result.content_preview),
                        duration_ms=duration_ms,
                        error=error,
                        observations_fetched=len(result_observations),
                        started_at=source_started_at,
                    )
                items.append(
                    {
                        "source_id": source_id,
                        "status": "error",
                        "criticality": policy.criticality.value,
                        "observations": len(result_observations),
                        **changes,
                        "content_type": result.content_type,
                        "duration_ms": duration_ms,
                        "error": error,
                    }
                )
                continue
            changes = {key: int(import_result.get(key, 0) or 0) for key in changes}
            capture_changes = {
                "market_capture_revisions": int(import_result.get("capture_revisions", 0) or 0),
                "market_capture_revisions_inserted": int(import_result.get("capture_revisions_inserted", 0) or 0),
                "market_capture_revisions_unchanged": int(import_result.get("capture_revisions_unchanged", 0) or 0),
            }
            for key, value in changes.items():
                change_totals[key] += value
        else:
            capture_changes = {
                "market_capture_revisions": 0,
                "market_capture_revisions_unchanged": 0,
            }
        futures_bars_stored = 0
        if result_futures_daily_bars:
            try:
                if apply:
                    stored_bars = bulk_upsert_futures_daily_bars_with_capture_revisions(
                        result_futures_daily_bars,
                        result_capture_revisions,
                        id_factory=lambda: str(uuid4()),
                        capture_revision_id_factory=lambda: str(uuid4()),
                    )
                    futures_bars_stored = len(stored_bars)
                else:
                    futures_bars_stored = len(result_futures_daily_bars)
            except Exception as exc:  # noqa: BLE001 - isolate futures persistence failures per source.
                error = _safe_exception(exc)
                errors.append(f"{source_id}: {error}")
                if apply:
                    record_source_fetch(
                        audit_id=str(uuid4()),
                        source_id=source_id,
                        status="error",
                        content_type=result.content_type,
                        preview_chars=len(result.content_preview),
                        duration_ms=duration_ms,
                        error=error,
                        observations_fetched=len(result_futures_daily_bars),
                        started_at=source_started_at,
                    )
                items.append(
                    {
                        "source_id": source_id,
                        "status": "error",
                        "criticality": policy.criticality.value,
                        "observations": len(result_observations),
                        "events": len(result_events),
                        "futures_daily_bars": len(result_futures_daily_bars),
                        "futures_daily_bars_stored": 0,
                        **changes,
                        "content_type": result.content_type,
                        "duration_ms": duration_ms,
                        "error": error,
                    }
                )
                continue
        event_changes = {"inserted": 0, "updated": 0, "unchanged": 0}
        if result_events:
            try:
                for event in result_events:
                    event_id = stable_event_id(event)
                    if apply:
                        _, created = upsert_event_observation(event_record_id=event_id, payload=event)
                        event_changes["inserted" if created else "unchanged"] += 1
                    else:
                        event_changes["inserted"] += 1
            except Exception as exc:  # noqa: BLE001 - isolate event persistence failures per source.
                error = _safe_exception(exc)
                errors.append(f"{source_id}: {error}")
                if apply:
                    record_source_fetch(
                        audit_id=str(uuid4()),
                        source_id=source_id,
                        status="error",
                        content_type=result.content_type,
                        preview_chars=len(result.content_preview),
                        duration_ms=duration_ms,
                        error=error,
                        observations_fetched=(
                            len(result_observations) + len(result_events) + len(result_futures_daily_bars)
                        ),
                        started_at=source_started_at,
                    )
                items.append(
                    {
                        "source_id": source_id,
                        "status": "error",
                        "criticality": policy.criticality.value,
                        "observations": len(result_observations),
                        "events": len(result_events),
                        "futures_daily_bars": len(result_futures_daily_bars),
                        "futures_daily_bars_stored": futures_bars_stored,
                        **changes,
                        "content_type": result.content_type,
                        "duration_ms": duration_ms,
                        "error": error,
                    }
                )
                continue
            for key, value in event_changes.items():
                changes[key] += value
                change_totals[key] += value
        if apply and pending_state_update is not None:
            try:
                write_source_state(source_state_dir / f"{source_id}.json", pending_state_update)
            except Exception as exc:  # noqa: BLE001 - isolate state persistence failures per source.
                error = _safe_exception(exc)
                errors.append(f"{source_id}: {error}")
                record_source_fetch(
                    audit_id=str(uuid4()),
                    source_id=source_id,
                    status="error",
                    content_type=result.content_type,
                    preview_chars=len(result.content_preview),
                    duration_ms=duration_ms,
                    error=error,
                    observations_fetched=(
                        len(result_observations) + len(result_events) + len(result_futures_daily_bars)
                    ),
                    **changes,
                    started_at=source_started_at,
                )
                items.append(
                    {
                        "source_id": source_id,
                        "status": "error",
                        "criticality": policy.criticality.value,
                        "observations": len(result_observations),
                        "events": len(result_events),
                        "futures_daily_bars": len(result_futures_daily_bars),
                        "futures_daily_bars_stored": futures_bars_stored,
                        **changes,
                        "content_type": result.content_type,
                        "duration_ms": duration_ms,
                        "error": error,
                        "state_backup_path": state_backup_path,
                    }
                )
                continue
        result_error = "" if source_run_succeeded(result.status) else result.content_preview[:500]
        if result_error:
            errors.append(f"{source_id}: {result.status}")
        if apply:
            record_source_fetch(
                audit_id=str(uuid4()),
                source_id=result.source_id,
                status=result.status,
                content_type=result.content_type,
                preview_chars=len(result.content_preview),
                duration_ms=duration_ms,
                error=result_error,
                observations_fetched=len(result_observations) + len(result_events) + len(result_futures_daily_bars),
                **changes,
                started_at=source_started_at,
            )
        items.append(
            {
                "source_id": result.source_id,
                "status": result.status,
                "criticality": policy.criticality.value,
                "observations": len(result_observations),
                "events": len(result_events),
                "futures_daily_bars": len(result_futures_daily_bars),
                "futures_daily_bars_stored": futures_bars_stored,
                **capture_changes,
                **changes,
                "content_type": result.content_type,
                "duration_ms": duration_ms,
                "error": result_error,
                "state_backup_path": state_backup_path,
            }
        )
    return {
        "status": component_status(items),
        "selected_source_ids": selected_ids,
        "skipped_not_due": sorted(set(PUBLIC_FETCH_SOURCE_IDS) - set(selected_ids)),
        "items": items,
        **change_totals,
        "errors": errors,
    }


def stable_event_id(payload: dict[str, Any]) -> str:
    raw = payload.get("raw") if isinstance(payload.get("raw"), dict) else {}
    identity = {
        "source_id": payload.get("source_id"),
        "event_type": payload.get("event_type"),
        "entity_id": raw.get("entity_id"),
        "action": raw.get("action"),
        "snapshot_sha256": raw.get("snapshot_sha256"),
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"source-event-{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


async def run_public_benchmark_refresh(*, apply: bool) -> dict[str, Any]:
    try:
        result = await collect_intraday_prices(apply=apply)
    except Exception as exc:  # noqa: BLE001 - current benchmark refresh must fail closed without crashing the run.
        return {
            "status": "blocked",
            "writes_database": apply,
            "attempted": len(CHAIN_INSTRUMENTS),
            "collected": 0,
            "stored": 0,
            "missing_instruments": list(CHAIN_INSTRUMENTS),
            "errors": [{"error": _safe_exception(exc, max_chars=500)}],
        }
    items = result.get("items", []) if isinstance(result, dict) else []
    collected = {
        str(item.get("instrument"))
        for item in items
        if isinstance(item, dict) and str(item.get("instrument") or "") in CHAIN_INSTRUMENTS
    }
    missing = [instrument for instrument in CHAIN_INSTRUMENTS if instrument not in collected]
    return {
        **result,
        "status": "blocked" if missing else "completed",
        "missing_instruments": missing,
    }


async def run_news_fetches(args: argparse.Namespace) -> dict[str, Any]:
    if not args.apply:
        return {
            "status": "dry_run_skipped",
            "writes_database": False,
            "reason": (
                "news fetch writes news_articles/news_event_clusters/event_observations; rerun with --apply"
            ),
            "planned": {
                "mode": "live",
                "limit_per_source": args.news_limit_per_source,
                "cursor_pages": args.news_cursor_pages,
                "include_details": not args.news_skip_details,
                "retries": args.news_retries,
                "source_timeout_seconds": args.news_source_timeout_seconds,
            },
        }
    try:
        result = await fetch_news_sources(
            limit_per_source=args.news_limit_per_source,
            mode="live",
            cursor_pages=args.news_cursor_pages,
            include_details=not args.news_skip_details,
            source_timeout_seconds=args.news_source_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "writes_database": True,
            "articles_found": 0,
            "clusters_upserted": 0,
            "events_created": 0,
            "errors": [_safe_exception(exc, max_chars=500)],
            "retry_attempts": 0,
        }
    runs = result.get("runs", []) if isinstance(result, dict) else []
    run_errors = [
        f"{run.get('source_id', 'unknown')}: {run.get('error')}"
        for run in runs
        if isinstance(run, dict) and run.get("error")
    ]
    return {
        "status": "degraded" if run_errors else "completed",
        "writes_database": True,
        "mode": result.get("mode", "live"),
        "source_runs": len(runs) if isinstance(runs, list) else 0,
        "articles_found": int(result.get("articles_found", 0) or 0),
        "clusters_upserted": int(result.get("clusters_upserted", 0) or 0),
        "events_created": int(result.get("events_created", 0) or 0),
        "summaries_selected": int(result.get("summaries_selected", 0) or 0),
        "summaries_completed": int(result.get("summaries_completed", 0) or 0),
        "summaries_failed": int(result.get("summaries_failed", 0) or 0),
        "errors": run_errors[:20],
        "retry_attempts": 0,
        "run_status_counts": count_run_statuses(runs if isinstance(runs, list) else []),
    }


def run_ccf_import(args: argparse.Namespace) -> dict[str, Any]:
    summary_path = args.output_dir / "ccf-authorized-import-summary.json"
    command = [
        sys.executable,
        str(SERVER_ROOT / "scripts" / "import_ccf_authorized_csvs.py"),
        "--input-dir",
        str(args.codex_run / "ccf-authorized-capture"),
        "--db",
        str(args.db),
        "--summary-output",
        str(summary_path),
    ]
    if args.apply:
        command.extend(["--apply", "--backup-db", "--backup-dir", str(args.output_dir / "db-backups")])
    else:
        command.append("--dry-run")
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": 124,
            "summary_path": str(summary_path),
            "accepted_rows": 0,
            "stored_rows": 0,
            "errors": [f"timeout_after_{SUBPROCESS_TIMEOUT_SECONDS}s"],
            "stderr": safe_summary(str(exc), max_chars=1000),
        }
    payload = read_json(summary_path)
    return {
        "exit_code": completed.returncode,
        "summary_path": str(summary_path),
        "accepted_rows": payload.get("accepted_rows", 0),
        "stored_rows": payload.get("stored_rows", 0),
        "errors": payload.get("errors", [])[:10],
        "stderr": safe_summary(completed.stderr, max_chars=1000),
    }


def run_trade_futures_proxy_import(args: argparse.Namespace) -> dict[str, Any]:
    summary_dir = args.output_dir / "trade-futures-proxy"
    end_date = args.as_of or datetime.now(UTC).date().isoformat()
    command = [
        sys.executable,
        str(SERVER_ROOT / "scripts" / "import_trade_futures_proxy.py"),
        "--start",
        "2025-01-01",
        "--end",
        end_date,
        "--output-dir",
        str(summary_dir),
    ]
    if args.apply:
        command.append("--apply")
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": 124,
            "summary_path": str(summary_dir / "trade-futures-proxy-summary.json"),
            "accepted_rows": 0,
            "stored_rows": 0,
            "errors": [f"timeout_after_{SUBPROCESS_TIMEOUT_SECONDS}s"],
            "stderr": safe_summary(str(exc), max_chars=1000),
        }
    payload = read_json(summary_dir / "trade-futures-proxy-summary.json")
    return {
        "exit_code": completed.returncode,
        "summary_path": str(summary_dir / "trade-futures-proxy-summary.json"),
        "accepted_rows": payload.get("accepted_rows", 0),
        "stored_rows": payload.get("stored_rows", 0),
        "errors": payload.get("errors", [])[:10],
        "stderr": safe_summary(completed.stderr, max_chars=1000),
    }


def run_futures_daily_import(args: argparse.Namespace) -> dict[str, Any]:
    input_dir = args.codex_run / "futures-daily"
    csv_paths = sorted(input_dir.glob("*.csv")) if input_dir.exists() else []
    if not csv_paths:
        return {
            "status": "skipped",
            "reason": "no authorized futures daily CSV files found",
            "input_dir": str(input_dir),
            "accepted_rows": 0,
            "stored_rows": 0,
            "errors": [],
        }

    summaries: list[dict[str, Any]] = []
    accepted_rows = 0
    stored_rows = 0
    errors: list[str] = []
    for csv_path in csv_paths:
        summary_path = args.output_dir / f"futures-daily-{csv_path.stem}-summary.json"
        command = [
            sys.executable,
            str(SERVER_ROOT / "scripts" / "import_futures_daily_bars.py"),
            str(csv_path),
            "--sqlite-path",
            str(args.db),
            "--json-output",
            str(summary_path),
        ]
        if not args.apply:
            command.append("--dry-run")
        try:
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            errors.append(f"{csv_path.name}: timeout_after_{SUBPROCESS_TIMEOUT_SECONDS}s")
            summaries.append(
                {
                    "csv_path": str(csv_path),
                    "summary_path": str(summary_path),
                    "exit_code": 124,
                    "accepted": 0,
                    "stored": 0,
                    "latest_trade_date": "",
                    "dry_run": not args.apply,
                    "stderr": safe_summary(str(exc), max_chars=1000),
                }
            )
            continue
        payload = read_json(summary_path)
        accepted_rows += int(payload.get("accepted", 0) or 0)
        stored_rows += int(payload.get("stored", 0) or 0)
        errors.extend(str(item) for item in payload.get("errors", [])[:10])
        if completed.returncode not in {0, 1}:
            errors.append(f"{csv_path.name}: importer exited {completed.returncode}")
        summaries.append(
            {
                "csv_path": str(csv_path),
                "summary_path": str(summary_path),
                "exit_code": completed.returncode,
                "accepted": payload.get("accepted", 0),
                "stored": payload.get("stored", 0),
                "latest_trade_date": payload.get("latest_trade_date"),
                "dry_run": payload.get("dry_run", not args.apply),
                "stderr": safe_summary(completed.stderr, max_chars=1000),
            }
        )
    return {
        "status": "completed",
        "input_dir": str(input_dir),
        "files": summaries,
        "accepted_rows": accepted_rows,
        "stored_rows": stored_rows,
        "errors": errors[:20],
    }


def run_akshare_futures_daily_fetch(args: argparse.Namespace) -> dict[str, Any]:
    summary_path = args.output_dir / "akshare-futures-daily-summary.json"
    end_date = args.as_of or datetime.now(UTC).date().isoformat()
    command = [
        sys.executable,
        str(SERVER_ROOT / "scripts" / "fetch_akshare_futures_daily.py"),
        "--start",
        "2025-01-01",
        "--end",
        end_date,
        "--sqlite-path",
        str(args.db),
        "--json-output",
        str(summary_path),
    ]
    if not args.apply:
        command.append("--dry-run")
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": 124,
            "summary_path": str(summary_path),
            "accepted_rows": 0,
            "stored_rows": 0,
            "latest_trade_date": "",
            "products": {},
            "roles": {},
            "errors": [f"timeout_after_{SUBPROCESS_TIMEOUT_SECONDS}s"],
            "stderr": safe_summary(str(exc), max_chars=1000),
        }
    payload = read_json(summary_path)
    return {
        "exit_code": completed.returncode,
        "summary_path": str(summary_path),
        "accepted_rows": payload.get("accepted_rows", 0),
        "stored_rows": payload.get("stored_rows", 0),
        "latest_trade_date": payload.get("latest_trade_date"),
        "products": payload.get("products", {}),
        "roles": payload.get("roles", {}),
        "errors": payload.get("errors", [])[:20],
        "stderr": safe_summary(completed.stderr, max_chars=1000),
    }


def run_quality_gate(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output_dir / "delivery-data-quality.json"
    command = [
        sys.executable,
        str(SERVER_ROOT / "scripts" / "run_delivery_data_quality_gate.py"),
        "--db",
        str(args.db),
        "--codex-run",
        str(args.codex_run),
        "--output",
        str(output),
        "--warn-only",
    ]
    if args.as_of:
        command.extend(["--as-of", args.as_of])
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": 124,
            "output": str(output),
            "overall_status": "timeout",
            "alerts": 0,
            "stderr": safe_summary(str(exc), max_chars=1000),
        }
    payload = read_json(output)
    return {
        "exit_code": completed.returncode,
        "output": str(output),
        "overall_status": payload.get("overall_status", "missing"),
        "alerts": len(payload.get("alerts", [])) if isinstance(payload.get("alerts"), list) else 0,
        "stderr": safe_summary(completed.stderr, max_chars=1000),
    }


def summarize_status(status: dict[str, Any]) -> dict[str, Any]:
    tasks = status.get("tasks", []) if isinstance(status.get("tasks"), list) else []
    not_ready = [
        task
        for task in tasks
        if isinstance(task, dict) and str(task.get("status", "")) not in {"success", "ready", "ready_to_import"}
    ]
    non_ccf_manual_or_blocked = [
        task
        for task in tasks
        if isinstance(task, dict)
        and not str(task.get("source_id", "")).startswith("ccf")
        and (
            bool(task.get("requires_human_action"))
            or str(task.get("automation_level", "")).startswith("manual")
            or str(task.get("status", "")) == "blocked"
        )
    ]
    return {
        "summary": status.get("summary", {}),
        "automation_ready": status.get("automation_ready", 0),
        "manual_or_blocked": status.get("manual_or_blocked", 0),
        "not_ready": status.get("not_ready", len(not_ready)),
        "non_ccf_manual_or_blocked": len(non_ccf_manual_or_blocked),
        "task_count": len(tasks),
    }


def count_run_statuses(runs: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for run in runs:
        if not isinstance(run, dict):
            continue
        status = str(run.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def load_public_fetch_states(db_path: Path) -> dict[str, dict[str, Any]]:
    states = {source_id: {} for source_id in PUBLIC_FETCH_SOURCE_IDS}
    if not db_path.exists():
        return states
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "market_observations" in tables:
            for row in connection.execute(
                "SELECT source_id, MAX(observed_at) latest_observed_at FROM market_observations GROUP BY source_id"
            ):
                if row["source_id"] in states:
                    states[row["source_id"]]["latest_observed_at"] = row["latest_observed_at"]
        if "futures_daily_bars" in tables:
            for row in connection.execute(
                "SELECT source_id, MAX(trade_date) latest_observed_at FROM futures_daily_bars GROUP BY source_id"
            ):
                if row["source_id"] in states:
                    current = str(states[row["source_id"]].get("latest_observed_at") or "")
                    candidate = str(row["latest_observed_at"] or "")
                    states[row["source_id"]]["latest_observed_at"] = max(current, candidate)
        if "source_fetch_audit" in tables:
            rows = connection.execute("""
                SELECT audit.source_id, audit.status last_run_status, audit.created_at last_run_started_at,
                       success.last_success_at
                FROM source_fetch_audit audit
                JOIN (SELECT source_id, MAX(created_at) latest_at FROM source_fetch_audit GROUP BY source_id) latest
                  ON latest.source_id = audit.source_id AND latest.latest_at = audit.created_at
                LEFT JOIN (
                  SELECT source_id, MAX(created_at) last_success_at FROM source_fetch_audit
                  WHERE status IN (
                    'ok', 'success', 'completed', 'succeeded', 'no_new_data',
                    'no-new-data', 'up_to_date', 'unchanged'
                  )
                  GROUP BY source_id
                ) success ON success.source_id = audit.source_id
                """).fetchall()
            for row in rows:
                if row["source_id"] in states:
                    states[row["source_id"]].update(dict(row))
    return states


def component_status(items: list[dict[str, Any]]) -> str:
    failures = [item for item in items if not source_run_succeeded(item.get("status"))]
    if any(item.get("criticality") == SourceCriticality.CRITICAL.value for item in failures):
        return "blocked"
    return "degraded" if failures else "completed"


def evaluate_run_status(
    *,
    public_fetch: dict[str, Any],
    ccf_import: dict[str, Any],
    news_fetch: dict[str, Any],
    quality: dict[str, Any],
    user_files: dict[str, Any] | None = None,
) -> dict[str, Any]:
    public_items = public_fetch.get("items", []) if isinstance(public_fetch, dict) else []
    failed_items = [
        item for item in public_items if isinstance(item, dict) and not source_run_succeeded(item.get("status"))
    ]
    critical = [str(item.get("source_id")) for item in failed_items if item.get("criticality") == "critical"]
    degraded = [str(item.get("source_id")) for item in failed_items if item.get("criticality") != "critical"]
    benchmark_refresh = public_fetch.get("public_benchmark_refresh", {}) if isinstance(public_fetch, dict) else {}
    if benchmark_refresh and benchmark_refresh.get("status") not in {"completed", "skipped"}:
        critical.append("public_benchmark_refresh")
    if (user_files or {}).get("status") in {"degraded", "error"}:
        degraded.append("user_files")
    if critical or quality.get("overall_status") == "blocked":
        status = "blocked"
    elif degraded or news_fetch.get("status") in {"degraded", "error"} or "user_files" in degraded:
        status = "degraded"
    else:
        status = "completed"
    return {"status": status, "critical_failures": critical, "degraded_components": degraded}


PUBLIC_FETCH_ATTEMPTS = 3
PUBLIC_FETCH_RETRY_BACKOFF_SECONDS = (2.0, 6.0)
_TRANSIENT_FETCH_ERROR_MARKERS = (
    "connecterror",
    "connecttimeout",
    "readtimeout",
    "connectionerror",
    "remoteprotocol",
    "nodename nor servname",
    "temporary failure in name resolution",
    "network is down",
    "dce_api_transient_error",
)


def is_transient_fetch_error(exc: BaseException) -> bool:
    """Classify network-level failures worth retrying; data errors stay fatal."""

    if isinstance(exc, TimeoutError):
        return True
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int):
        return status_code in {408, 425, 429} or 500 <= status_code <= 599
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    return any(marker in name or marker in text for marker in _TRANSIENT_FETCH_ERROR_MARKERS)


def _safe_exception(exc: BaseException, *, max_chars: int = 300) -> str:
    detail = safe_summary(str(exc), max_chars=max_chars)
    return f"{exc.__class__.__name__}: {detail}" if detail else exc.__class__.__name__


async def fetch_public_source_with_retries(
    fetcher: Any,
    source: Any,
    *,
    timeout_seconds: float,
    deadline_monotonic: float | None = None,
) -> tuple[Any, int]:
    attempts = 0
    while True:
        attempts += 1
        try:
            remaining = float("inf") if deadline_monotonic is None else deadline_monotonic - perf_counter()
            if remaining <= 0:
                raise TimeoutError("public_fetch_outer_deadline_exhausted")
            result = await asyncio.wait_for(fetcher.fetch(source), timeout=min(timeout_seconds, remaining))
            return result, attempts
        except Exception as exc:  # noqa: BLE001
            if attempts >= PUBLIC_FETCH_ATTEMPTS or not is_transient_fetch_error(exc):
                raise
            backoff = PUBLIC_FETCH_RETRY_BACKOFF_SECONDS[min(attempts, len(PUBLIC_FETCH_RETRY_BACKOFF_SECONDS)) - 1]
            remaining = float("inf") if deadline_monotonic is None else deadline_monotonic - perf_counter()
            if remaining <= backoff:
                raise TimeoutError("public_fetch_outer_deadline_exhausted") from exc
            await asyncio.sleep(backoff)


def backup_database(
    db_path: Path,
    backup_dir: Path,
    *,
    reuse_seconds: int = 0,
    retention_count: int = 3,
    timeout_seconds: float = BACKUP_TIMEOUT_SECONDS,
) -> Path:
    secure_private_directory(backup_dir)
    candidates = sorted(
        backup_dir.glob(f"{db_path.name}.pre_source_automation_*.sqlite"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if candidates and reuse_seconds > 0:
        age_seconds = datetime.now(UTC).timestamp() - candidates[0].stat().st_mtime
        if age_seconds <= reuse_seconds and sqlite_integrity_ok(candidates[0]):
            secure_sqlite_artifacts(candidates[0])
            _prune_database_backups(backup_dir, db_path.name, retention_count=retention_count)
            return candidates[0]
    # Prune before allocating another full SQLite copy. Waiting until after the
    # copy can deadlock recovery when stale backups have already filled the disk.
    _prune_database_backups(backup_dir, db_path.name, retention_count=max(0, retention_count - 1))
    backup_name = f"{db_path.name}.pre_source_automation_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.sqlite"
    backup_path = backup_dir / backup_name
    sqlite_cli = shutil.which("sqlite3")
    if not sqlite_cli:
        raise OSError("backup_unavailable: sqlite3 CLI is required for isolated backups")
    try:
        completed = subprocess.run(
            [sqlite_cli, str(db_path), f'.backup "{str(backup_path).replace(chr(34), chr(34) * 2)}"'],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=max(1.0, timeout_seconds),
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-500:]
            raise OSError(f"backup_failed: {detail or completed.returncode}")
        if not sqlite_integrity_ok(backup_path):
            raise OSError("backup integrity check failed")
        secure_sqlite_artifacts(backup_path)
    except subprocess.TimeoutExpired as exc:
        remove_sqlite_artifacts(backup_path)
        raise OSError(f"backup_timeout: exceeded {timeout_seconds:.0f}s") from exc
    except Exception:
        remove_sqlite_artifacts(backup_path)
        raise
    # Retention spans every writer's backup prefix (pre_source_automation_*, pre_ccf_authorized_import_*,
    # ...) in this directory; per-prefix pruning let older prefixes accumulate without bound.
    _prune_database_backups(backup_dir, db_path.name, retention_count=retention_count)
    return backup_path


def _prune_database_backups(backup_dir: Path, database_name: str, *, retention_count: int) -> int:
    candidates = sorted(
        (path for path in backup_dir.glob(f"{database_name}.pre_*.sqlite") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    stale_candidates = candidates[max(0, retention_count) :]
    for stale_path in stale_candidates:
        remove_sqlite_artifacts(stale_path)
    return len(stale_candidates)


def sqlite_integrity_ok(path: Path) -> bool:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0]) == "ok"
    except sqlite3.Error:
        return False


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def now() -> str:
    return datetime.now(UTC).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())

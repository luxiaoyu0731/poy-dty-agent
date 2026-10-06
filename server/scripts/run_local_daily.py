# ruff: noqa: F811 - final report projections intentionally override legacy renderers.

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
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
SCRIPTS_ROOT = SERVER_ROOT / "scripts"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from runtime_guards import RunLockError, configure_runtime_sqlite_path, exclusive_run_lock  # noqa: E402

from app.daily_decision import DailyJudgementNotReadyError, materialize_daily_judgement  # noqa: E402
from app.direction_upstream_gate import gate_limits  # noqa: E402
from app.scheduler_observability_state import build_scheduler_observability  # noqa: E402
from app.sqlite_permissions import (  # noqa: E402
    remove_sqlite_artifacts,
    secure_private_directory,
    secure_sqlite_artifacts,
)

DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "local-production"
DEFAULT_CODEX_RUN = REPO_ROOT / ".codex-run"
RUN_LOCK_STALE_SECONDS = 8 * 60 * 60
SOURCE_AUTOMATION_TIMEOUT_SECONDS = int(os.getenv("LOCAL_DAILY_SOURCE_TIMEOUT_SECONDS", "900"))
# A source scheduler may already own its lock when the fixed-time daily chain
# starts.  Retry only that contention, for a bounded window; other failures
# remain fail-closed.  This prevents a transient 09:30 race from skipping the
# entire foundation/direction-review stage without masking a real outage.
SOURCE_LOCK_RETRY_MAX_WAIT_SECONDS = int(
    os.getenv("LOCAL_DAILY_SOURCE_LOCK_RETRY_MAX_WAIT_SECONDS", "1800")
)
SOURCE_LOCK_RETRY_POLL_SECONDS = int(os.getenv("LOCAL_DAILY_SOURCE_LOCK_RETRY_POLL_SECONDS", "30"))
SUBPROCESS_TIMEOUT_SECONDS = int(os.getenv("LOCAL_DAILY_STEP_TIMEOUT_SECONDS", "300"))
# Direction-review upstream gate (fix-direction-review 2026-09-18): the
# foundation step may now wait, bounded by the same env variable the gate
# reads (DIRECTION_UPSTREAM_GATE_MAX_WAIT_SECONDS, default 1800), for the
# event-summary worker to drain the overnight queue before the direction
# review retrieves evidence. The step timeout must cover the base budget
# plus the full gate wait plus a safety margin, or a legitimate wait would
# be killed mid-materialization by the subprocess timeout.
DIRECTION_UPSTREAM_GATE_MAX_WAIT_SECONDS = int(gate_limits()["max_wait_seconds"])
FOUNDATION_STEP_TIMEOUT_MARGIN_SECONDS = 60
FOUNDATION_STEP_TIMEOUT_SECONDS = (
    SUBPROCESS_TIMEOUT_SECONDS + DIRECTION_UPSTREAM_GATE_MAX_WAIT_SECONDS + FOUNDATION_STEP_TIMEOUT_MARGIN_SECONDS
)
# Unified daily backup anchor (disk consolidation 2026-09-17, DISK-MODEL §3.2):
# exactly one online-backup snapshot per business day, integrity-gated, kept
# for DAILY_ANCHOR_RETENTION days. It replaces the per-step full copies the
# daily chain used to take (foundation pre-backup, probe backup, restore drill).
DAILY_ANCHOR_DIR_NAME = "daily-anchor"
DAILY_ANCHOR_RETENTION = 7


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local production daily automation bundle and write readiness reports."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write fetched/imported data after creating backups.")
    mode.add_argument("--dry-run", action="store_true", help="Do not write business data. This is the default.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--codex-run", type=Path, default=DEFAULT_CODEX_RUN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--as-of", default="")
    parser.add_argument("--health-base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--skip-health", action="store_true")
    parser.add_argument("--lock-file", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    output_dir = args.output_dir.expanduser().resolve()
    secure_private_directory(output_dir)
    status_path = output_dir / "latest-status.json"
    previous_status = read_json(status_path)
    scheduler_started_at = utc_now()
    args.db = args.db.expanduser().resolve()
    args.codex_run = args.codex_run.expanduser().resolve()
    configure_runtime_sqlite_path(args.db)
    lock_file = (
        args.lock_file.expanduser().resolve() if args.lock_file else args.codex_run / "locks" / "local-daily.lock"
    )
    try:
        with exclusive_run_lock(lock_file, stale_after_seconds=RUN_LOCK_STALE_SECONDS):
            exit_code = run_daily_with_lock(args, output_dir=output_dir)
            current = read_json(status_path)
            _attach_scheduler_observability(
                current,
                previous_payload=previous_status,
                started_at=scheduler_started_at,
            )
            write_json(status_path, current)
            return exit_code
    except RunLockError as exc:
        locked = {
            "schema_version": "local_production_daily.v1",
            "overall_status": "blocked",
            "blockers": ["local daily automation lock is already held"],
            "warnings": [],
            "lock": exc.payload,
            "finished_at": utc_now(),
        }
        _attach_scheduler_observability(
            locked,
            previous_payload=previous_status,
            started_at=scheduler_started_at,
            status="locked",
        )
        write_json(status_path, locked)
        print(json.dumps({"output": str(status_path), "status": "locked"}, ensure_ascii=False))
        return 75
    except Exception as exc:  # noqa: BLE001
        failed = {
            "schema_version": "local_production_daily.v1",
            "overall_status": "blocked",
            "blockers": [f"local daily automation crashed: {exc.__class__.__name__}"],
            "warnings": [],
            "error": str(exc)[:500],
            "finished_at": utc_now(),
        }
        _attach_scheduler_observability(
            failed,
            previous_payload=previous_status,
            started_at=scheduler_started_at,
        )
        write_json(status_path, failed)
        print(json.dumps({"output": str(status_path), "status": "blocked"}, ensure_ascii=False))
        return 1


def _attach_scheduler_observability(
    payload: dict[str, Any],
    *,
    previous_payload: dict[str, Any],
    started_at: str,
    status: object | None = None,
) -> None:
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    finished_at = payload.get("finished_at") or utc_now()
    payload["scheduler_observability"] = build_scheduler_observability(
        scheduler="local_daily",
        status=status if status is not None else payload.get("overall_status"),
        started_at=started_at,
        finished_at=finished_at,
        backlog=len(blockers),
        previous_payload=previous_payload,
    )


def run_daily_with_lock(args: argparse.Namespace, *, output_dir: Path) -> int:
    started_at = utc_now()
    mode = "apply" if args.apply else "dry_run"
    business_date = _business_date_from_stamp(started_at)

    source_summary = run_source_automation_with_lock_retry(args, output_dir=output_dir)
    industry_import = {
        "status": "soft_removed",
        "exit_code": 0,
        "writes_database": False,
        "historical_rows_read_only": True,
    }
    quality_refresh = run_quality_gate(args, output_dir=output_dir)
    quality = read_json(output_dir / "source-automation" / "delivery-data-quality.json")
    customer_quality = customer_quality_report(quality)
    if source_summary.get("fresh_output"):
        source_latest = read_json(output_dir / "source-automation" / "source-automation-latest.json")
    else:
        source_latest = {
            "schema_version": "source_automation_run.v1",
            "status": "blocked",
            "critical_failures": ["source_automation_no_fresh_report"],
            "degraded_components": [],
            "error": "source automation did not publish a fresh report for this daily run",
        }
    source_status = str(source_latest.get("status") or "missing")
    critical_failures = source_latest.get("critical_failures", [])
    quality_status = str(customer_quality.get("overall_status") or "missing")
    observation_grade_source = (
        source_status == "blocked" and not critical_failures and quality_status == "needs_human_review"
    )
    can_materialize = (
        (source_summary.get("exit_code") == 0 or observation_grade_source)
        and (source_status != "blocked" or observation_grade_source)
        and not critical_failures
        and quality_refresh.get("exit_code") in {0, None}
        and quality_status not in {"blocked", "failed", "missing"}
    )
    # Node A (multi-agent chain, plan §6.4): assemble the frozen event candidate
    # set BEFORE issuance. Degrades to a warning; never blocks the lifecycle.
    event_signal = run_event_signal_assembly(args, output_dir=output_dir, business_date=business_date)
    # Node B: the agent reasoning chain over node A's frozen candidates (≤40 calls).
    # Degrades to a warning; never blocks the lifecycle.
    agent_chain = run_event_agent_chain_step(args, output_dir=output_dir, business_date=business_date)
    # Sequence (final backup state 2026-09-17, DISK-MODEL §7): issue FIRST, then
    # the unified daily anchor, so the anchor is a post-issue consistent snapshot
    # the evaluation input can hard-link zero-copy. Foundation materialization
    # still receives the same-run anchor as its fail-closed recovery gate, and
    # the anchor remains the only full database copy written per business day.
    seven_product_lifecycle = run_seven_product_lifecycle(
        args, output_dir=output_dir, business_date=business_date
    )
    lifecycle_forecast = (
        seven_product_lifecycle.get("forecast")
        if isinstance(seven_product_lifecycle.get("forecast"), dict)
        else {}
    )
    backup_report = verify_backup(args.db.expanduser().resolve(), business_date=business_date)
    if can_materialize:
        foundation_materialization = run_foundation_materialization(
            args, output_dir=output_dir, anchor_report=backup_report
        )
    else:
        foundation_materialization = {
            "status": "skipped",
            "exit_code": None,
            "reason": "source or quality gate did not permit Agent/report materialization",
        }
    seven_product_evaluation = run_seven_product_evaluation(
        args,
        output_dir=output_dir,
        issued_business_date=str(lifecycle_forecast.get("business_date") or "") if args.apply else None,
        anchor_report=backup_report,
    )
    db_snapshot = inspect_database(args.db.expanduser().resolve())
    health_report = {} if args.skip_health else check_health(args.health_base_url)
    readiness = build_readiness(
        mode=mode,
        started_at=started_at,
        finished_at=utc_now(),
        source_process=source_summary,
        source_latest=source_latest,
        industry_import=industry_import,
        quality_refresh=quality_refresh,
        foundation_materialization=foundation_materialization,
        seven_product_lifecycle=seven_product_lifecycle,
        seven_product_evaluation=seven_product_evaluation,
        quality=quality,
        db_snapshot=db_snapshot,
        backup_report=backup_report,
        health_report=health_report,
    )
    readiness["event_signal"] = event_signal
    if event_signal.get("status") == "degraded":
        readiness["warnings"].append(
            f"event signal assembly degraded: {event_signal.get('failure_reason') or 'unknown'}"
        )
    elif event_signal.get("status") == "empty":
        readiness["warnings"].append(
            "event signal assembly found no recent candidates; agent chain will be skipped"
        )
    readiness["agent_chain"] = agent_chain
    if agent_chain.get("status") == "degraded":
        readiness["warnings"].append(
            f"agent chain degraded: {agent_chain.get('failure_reason') or 'unknown'}"
        )

    # The daily chain is the judgement authority in scaffold mode: once the
    # bundle itself succeeds, the same-day judgement snapshot is materialized
    # through the exact implementation behind the manual materialize endpoint
    # (no duplicated payload logic). The wrapper's success stamp is written
    # only after this script exits 0, so stamp-idempotency and the snapshot's
    # per-business-date uniqueness stack on top of each other.
    judgement_snapshot = run_daily_judgement_snapshot(args, readiness=readiness)
    if judgement_snapshot is not None:
        readiness["daily_judgement_snapshot"] = judgement_snapshot
        if str(judgement_snapshot.get("status") or "") in {"blocked", "failed"}:
            readiness["blockers"].append(
                f"daily judgement snapshot materialization {judgement_snapshot.get('status')}"
            )
            readiness["overall_status"] = "blocked"

    # Post-snapshot observation-only LLM steps (DESIGN §2.6 方案A): after the
    # customer deliverables are frozen in the snapshot, before write_reports.
    # Every failure is captured into readiness warnings; overall_status and the
    # exit code stay untouched (dispatch_alerts "must never fail" precedent).
    readiness["counter_scan"] = run_post_snapshot_counter_scan(args, readiness=readiness)
    if str(readiness["counter_scan"].get("status") or "") == "degraded":
        readiness["warnings"].append(
            f"counter scan degraded: {readiness['counter_scan'].get('failure_reason') or 'unknown'}"
        )
    readiness["daily_interpretation"] = run_post_snapshot_daily_interpretation(args, readiness=readiness)
    if str(readiness["daily_interpretation"].get("status") or "") == "degraded":
        readiness["warnings"].append(
            f"daily interpretation degraded: {readiness['daily_interpretation'].get('failure_reason') or 'unknown'}"
        )

    write_json(output_dir / "latest-status.json", readiness)
    readiness["status_history"] = persist_daily_status_history(output_dir, readiness)
    write_json(output_dir / "latest-status.json", readiness)
    write_reports(output_dir, readiness)
    readiness["alert_dispatch"] = dispatch_alerts(output_dir=output_dir)
    write_json(output_dir / "latest-status.json", readiness)
    write_launchd_templates(output_dir)
    print(json.dumps({"output": str(output_dir / "latest-status.json"), "status": readiness["overall_status"]}))
    return 1 if readiness["overall_status"] in {"blocked", "failed"} else 0


DAILY_STATUS_HISTORY_RETENTION = 30


def persist_daily_status_history(output_dir: Path, readiness: dict[str, Any]) -> dict[str, Any]:
    """Publish a content-addressed copy of each attempt's status so blocked retry
    loops remain diagnosable after latest-status.json is overwritten."""

    try:
        body = dict(readiness)
        body.pop("status_history", None)
        body_sha256 = _canonical_sha256(body)
        history_dir = output_dir / "status-history"
        secure_private_directory(history_dir)
        addressed = history_dir / f"daily-status-{body_sha256[:16]}.json"
        if not addressed.exists():
            rendered = (
                json.dumps({**body, "status_body_sha256": body_sha256}, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
            temporary: Path | None = None
            try:
                with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=history_dir, delete=False) as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(rendered)
                    stream.flush()
                    os.fsync(stream.fileno())
                    temporary = Path(stream.name)
                os.replace(temporary, addressed)
                temporary = None
                _fsync_directory(history_dir)
            finally:
                if temporary is not None:
                    _remove_temporary_sqlite(temporary)
        pruned = prune_file_retention(history_dir, "daily-status-*.json", keep=DAILY_STATUS_HISTORY_RETENTION)
        return {"status_history_dir": str(history_dir), "status_body_sha256": body_sha256, "pruned": pruned}
    except (OSError, RuntimeError, ValueError) as exc:
        return {"error": f"status_history_failed:{exc.__class__.__name__}"}


def dispatch_alerts(*, output_dir: Path) -> dict[str, Any]:
    """Best-effort alert digest and push; must never fail the daily run."""

    try:
        completed = run_subprocess(
            [
                sys.executable,
                str(SERVER_ROOT / "scripts" / "check_local_production_alerts.py"),
                "--status-path",
                str(output_dir / "latest-status.json"),
                "--output-dir",
                str(output_dir / "alerts"),
            ],
            timeout_seconds=120,
        )
        return {"exit_code": completed.returncode}
    except Exception as exc:  # noqa: BLE001 - alerting is auxiliary; the run result already exists on disk.
        return {"exit_code": None, "error": f"{exc.__class__.__name__}: {exc}"}


DAILY_CHAIN_BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


def run_daily_judgement_snapshot(args: argparse.Namespace, *, readiness: dict[str, Any]) -> dict[str, Any] | None:
    """Materialize the business-day judgement snapshot after the daily chain succeeds.

    Preconditions, in order: apply mode (dry runs never freeze snapshots), a
    non-blocked daily readiness (the "日度研判完成" stamp semantics), and the
    snapshot writer's own per-business-date uniqueness for idempotency.
    """

    if not args.apply:
        return {"status": "skipped", "reason": "dry_run_does_not_materialize_snapshots"}
    overall = str(readiness.get("overall_status") or "")
    if overall in {"blocked", "failed"}:
        return {"status": "skipped", "reason": "daily_chain_did_not_succeed", "overall_status": overall}
    business_date = _daily_chain_business_date(readiness)
    try:
        snapshot = materialize_daily_judgement(
            business_date=business_date,
            daily_chain_result={
                "business_date": business_date,
                "overall_status": overall,
                "started_at": str(readiness.get("started_at") or ""),
                "finished_at": str(readiness.get("finished_at") or ""),
            },
        )
    except DailyJudgementNotReadyError as exc:
        return {"status": "blocked", "reason": str(exc), "business_date": business_date}
    except Exception as exc:  # noqa: BLE001 - surfaced as a daily blocker, never crashes the bundle silently.
        return {"status": "failed", "reason": f"{exc.__class__.__name__}: {exc}"[:500], "business_date": business_date}
    return {
        "status": "idempotent_replay" if snapshot.get("idempotent_replay") else "materialized",
        "business_date": business_date,
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "payload_sha256": str(snapshot.get("payload_sha256") or ""),
        "source_run_id": str(snapshot.get("source_run_id") or ""),
    }


def _business_date_from_stamp(value: str) -> str:
    try:
        started = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(DAILY_CHAIN_BUSINESS_TIMEZONE).date().isoformat()
    return started.astimezone(DAILY_CHAIN_BUSINESS_TIMEZONE).date().isoformat()


def _daily_chain_business_date(readiness: dict[str, Any]) -> str:
    return _business_date_from_stamp(str(readiness.get("started_at") or ""))


# ---------------------------------------------------------------------------
# Post-snapshot LLM steps (DESIGN §2.6 方案A, 批次2/5 链内接线).
#
# Ordering is guaranteed by code: 判断快照物化 → 反证扫描 (≤90s) → 日报解读
# (≤90s) → write_reports. Failures never touch overall_status or the exit code
# because the customer deliverables were already frozen inside the snapshot;
# each step is idempotent (completed artifact exists → skip) and paid attempts
# are hard-capped by the agents' own O_EXCL reservations (≤2/business day).
# ---------------------------------------------------------------------------

POST_SNAPSHOT_STEP_TIMEOUT_MARGIN_SECONDS = 15


def _post_snapshot_step_enabled(env_name: str) -> bool:
    return os.getenv(env_name, "true").strip().lower() not in {"0", "false", "no", "off"}


def _run_post_snapshot_step(
    step: str,
    coroutine_factory,
    *,
    args: argparse.Namespace,
    readiness: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    """Run one post-snapshot agent step with a hard timeout; never raises."""
    if not args.apply:
        return {"step": step, "status": "skipped", "reason": "dry_run"}
    try:
        result = asyncio.run(
            asyncio.wait_for(
                coroutine_factory(),
                timeout=timeout_seconds + POST_SNAPSHOT_STEP_TIMEOUT_MARGIN_SECONDS,
            )
        )
    except TimeoutError:
        result = {"status": "degraded", "failure_reason": "step_timeout"}
    except Exception as exc:  # noqa: BLE001 - the snapshot is already frozen; this step must never fail the chain.
        result = {"status": "degraded", "failure_reason": f"{exc.__class__.__name__}: {exc}"[:300]}
    summary = {"step": step}
    summary_keys = (
        "status", "failure_reason", "reason", "scan_outcome", "artifact_path",
        "llm_cost_cny", "idempotent_replay",
    )
    for key in summary_keys:
        if key in result:
            summary[key] = result[key]
    timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
    if timings.get("total_ms") is not None:
        summary["total_ms"] = timings["total_ms"]
    return summary


def run_event_signal_assembly(
    args: argparse.Namespace, *, output_dir: Path, business_date: str
) -> dict[str, Any]:
    """Node A (docs/multi-agent-prediction-plan.md §6.4): freeze the day's event
    candidates into ``event-signal-latest.json`` before forecast issuance.

    Read-only over the intelligence snapshot plus one report write; any failure
    degrades to a warning and the chain proceeds (the forecast falls back to
    pure price behavior). Runs in dry-run too: it writes no database rows.
    """

    from app.event_signal import collect_event_signal_candidates

    try:
        report = collect_event_signal_candidates(
            as_of_time=utc_now(), business_date=business_date  # utc_now already ISO
        )
        path = write_json(output_dir / "event-signal-latest.json", report)
        return {
            "step": "event_signal",
            "status": report["status"],
            "selected_count": report["selected_count"],
            "covered_products": report["covered_products"],
            "input_sha256": report["input_sha256"],
            "artifact_path": str(path),
        }
    except Exception as exc:  # noqa: BLE001 - node A must never block the daily chain.
        return {
            "step": "event_signal",
            "status": "degraded",
            "failure_reason": f"{exc.__class__.__name__}: {exc}"[:300],
        }


def run_event_agent_chain_step(
    args: argparse.Namespace, *, output_dir: Path, business_date: str
) -> dict[str, Any]:
    """Node B (plan §6.4): the 40-call/day agent reasoning chain.

    Reads the frozen signal report from node A; skipped when there are no
    candidates. LLM failures degrade into template fallbacks inside the chain;
    any unexpected error degrades to a warning. Persists blackboard artifacts
    only in apply mode.
    """

    import os

    if os.getenv("AGENT_CHAIN_ENABLED", "true").strip().lower() in {"0", "false", "no", "off"}:
        return {"step": "agent_chain", "status": "skipped", "reason": "disabled_by_env"}
    signal_path = output_dir / "event-signal-latest.json"
    if not signal_path.is_file():
        return {"step": "agent_chain", "status": "skipped", "reason": "no_signal_report"}
    signal_report = read_json(signal_path)
    if signal_report.get("status") != "ok" or not signal_report.get("candidates"):
        return {"step": "agent_chain", "status": "skipped", "reason": "no_candidates"}
    from app.agent_chain import DeepSeekJsonPort, compute_baseline_for_prompts, run_event_agent_chain
    from app.storage import list_active_agent_lessons

    # Audit fix A: the synthesis/skeptic prompts need the price baseline they
    # argue against (computed read-only from the frozen label series).
    try:
        baseline_context = compute_baseline_for_prompts(str(signal_report["as_of_time"]))
    except Exception:  # noqa: BLE001 - context is best-effort.
        baseline_context = {}

    # 校准记忆（plan §4.2）：当日活跃教训注入全部 Agent 的 prompt（point-in-time，
    # valid_from <= as_of；注册表为唯一事实源）。
    try:
        lessons = list_active_agent_lessons(as_of_time=str(signal_report["as_of_time"]), limit=30)
    except Exception:  # noqa: BLE001 - lessons are an accelerant, never a dependency.
        lessons = []

    try:
        report = run_event_agent_chain(
            port=DeepSeekJsonPort(),
            signal_report=signal_report,
            business_date=business_date,
            as_of_time=str(signal_report["as_of_time"]),
            lessons=lessons,
            baseline_by_product=baseline_context,
            persist=bool(args.apply),
        )
        path = write_json(output_dir / "event-agent-chain-latest.json", report)
        return {
            "step": "agent_chain",
            "status": report["status"],
            "run_id": report["run_id"],
            "budget": report["budget"],
            "product_factor_count": len(report["product_factors"]),
            "artifact_path": str(path),
        }
    except Exception as exc:  # noqa: BLE001 - node B must never block the daily chain.
        return {
            "step": "agent_chain",
            "status": "degraded",
            "failure_reason": f"{exc.__class__.__name__}: {exc}"[:300],
        }


def run_post_snapshot_counter_scan(
    args: argparse.Namespace, *, readiness: dict[str, Any]
) -> dict[str, Any]:
    from app.counter_scan import counter_scan_timeout_seconds, run_counter_scan

    if not _post_snapshot_step_enabled("AI_COUNTER_SCAN_ENABLED"):
        return {"step": "counter_scan", "status": "skipped", "reason": "disabled_by_env"}
    business_date = _daily_chain_business_date(readiness)
    chain_status = str(readiness.get("overall_status") or "")

    def factory():
        return run_counter_scan(
            business_date=business_date,
            dry_run=not args.apply,
            chain_status=chain_status,
        )

    return _run_post_snapshot_step(
        "counter_scan",
        factory,
        args=args,
        readiness=readiness,
        timeout_seconds=counter_scan_timeout_seconds(),
    )


def run_post_snapshot_daily_interpretation(
    args: argparse.Namespace, *, readiness: dict[str, Any]
) -> dict[str, Any]:
    from app.daily_interpretation import daily_interpretation_timeout_seconds, run_daily_interpretation

    if not _post_snapshot_step_enabled("AI_DAILY_INTERPRETATION_ENABLED"):
        return {"step": "daily_interpretation", "status": "skipped", "reason": "disabled_by_env"}
    business_date = _daily_chain_business_date(readiness)
    chain_status = str(readiness.get("overall_status") or "")

    def factory():
        return run_daily_interpretation(
            business_date=business_date,
            dry_run=not args.apply,
            chain_status=chain_status,
        )

    return _run_post_snapshot_step(
        "daily_interpretation",
        factory,
        args=args,
        readiness=readiness,
        timeout_seconds=daily_interpretation_timeout_seconds(),
    )


def run_source_automation(args: argparse.Namespace, *, output_dir: Path) -> dict[str, Any]:
    source_output_dir = output_dir / "source-automation"
    latest_path = source_output_dir / "source-automation-latest.json"
    previous_mtime_ns = latest_path.stat().st_mtime_ns if latest_path.exists() else None
    command = [
        sys.executable,
        str(SERVER_ROOT / "scripts" / "run_source_automation.py"),
        "--db",
        str(args.db),
        "--codex-run",
        str(args.codex_run),
        "--output-dir",
        str(source_output_dir),
        "--record-plan",
        "--fetch-public",
        "--public-deadline-seconds",
        str(max(1, SOURCE_AUTOMATION_TIMEOUT_SECONDS - 240)),
        "--import-trade-futures-proxy",
        "--import-futures-daily",
        "--import-user-files",
        "--fetch-akshare-futures-daily",
        "--fetch-news",
        "--news-limit-per-source",
        "3",
        "--news-source-timeout-seconds",
        "20",
        "--run-quality",
    ]
    if args.as_of:
        command.extend(["--as-of", args.as_of])
    if args.apply:
        # Final backup state (DISK-MODEL §7): no pre-write backup for the source
        # step; recovery is covered by the unified daily anchor created later in
        # this same chain run plus re-fetchable news/price data.
        command.extend(["--apply"])
    completed = run_subprocess(command, timeout_seconds=SOURCE_AUTOMATION_TIMEOUT_SECONDS)
    current_mtime_ns = latest_path.stat().st_mtime_ns if latest_path.exists() else None
    fresh_output = current_mtime_ns is not None and current_mtime_ns != previous_mtime_ns
    return {
        "command": redact_command(command),
        "exit_code": completed.returncode,
        "stdout_tail": completed.stdout[-1500:],
        "stderr_tail": completed.stderr[-1500:],
        "output_dir": str(source_output_dir),
        "fresh_output": fresh_output,
        "lock_contention": _source_result_is_lock_contention(
            completed.returncode, completed.stdout, latest_path if fresh_output else None
        ),
    }


def _source_result_is_lock_contention(returncode: int, stdout: str, latest_path: Path | None) -> bool:
    """Identify the source runner's explicit lock outcome without guessing.

    Exit 75 is the runner's stable lock contract.  The JSON stdout fallback is
    retained for wrappers that normalize the process exit code, while the
    latest report is checked only when it was actually written by this attempt.
    """

    if returncode == 75:
        return True
    for line in reversed(stdout.splitlines()):
        try:
            payload = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload.get("status") == "locked":
            return True
    if latest_path is None:
        return False
    try:
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "locked"


def run_source_automation_with_lock_retry(args: argparse.Namespace, *, output_dir: Path) -> dict[str, Any]:
    """Run source automation, compensating only for a transient lock race.

    The retry window is deliberately bounded.  A timeout returns the last
    locked result with explicit metadata, so downstream gates still skip
    foundation rather than consuming stale data or pretending success.
    """

    first = run_source_automation(args, output_dir=output_dir)
    if not first.get("lock_contention"):
        return first

    retry_started = time.monotonic()
    attempts = 1
    max_wait = max(0, SOURCE_LOCK_RETRY_MAX_WAIT_SECONDS)
    poll_seconds = max(1, min(60, SOURCE_LOCK_RETRY_POLL_SECONDS))
    last = first
    while time.monotonic() - retry_started < max_wait:
        remaining = max_wait - (time.monotonic() - retry_started)
        time.sleep(min(poll_seconds, remaining))
        if time.monotonic() - retry_started >= max_wait:
            break
        attempts += 1
        candidate = run_source_automation(args, output_dir=output_dir)
        last = candidate
        if not candidate.get("lock_contention"):
            candidate["lock_retry"] = {
                "attempts": attempts,
                "waited_seconds": round(time.monotonic() - retry_started, 3),
                "max_wait_seconds": max_wait,
                "status": "recovered",
            }
            return candidate

    last["lock_retry"] = {
        "attempts": attempts,
        "waited_seconds": round(time.monotonic() - retry_started, 3),
        "max_wait_seconds": max_wait,
        "status": "timed_out",
        "reason": "source automation lock remained held",
    }
    return last


def run_ccf_industry_import(args: argparse.Namespace, *, output_dir: Path) -> dict[str, Any]:
    candidates = [
        args.codex_run / "ccf-authorized-capture" / "ccf_industry_observations.csv",
        args.codex_run / "ccf-auth" / "ccf_industry_observations.csv",
    ]
    input_path = next((path for path in candidates if path.exists()), None)
    summary_path = output_dir / "ccf-industry-import-summary.json"
    if input_path is None:
        return {
            "status": "skipped",
            "reason": "ccf_industry_observations.csv not found in authorized capture directories",
            "searched": [str(path) for path in candidates],
            "summary_path": str(summary_path),
        }
    command = [
        sys.executable,
        str(SERVER_ROOT / "scripts" / "import_ccf_industry_observations.py"),
        "--input",
        str(input_path),
        "--db",
        str(args.db),
        "--summary-output",
        str(summary_path),
    ]
    if args.apply:
        command.extend(["--apply", "--backup-db", "--backup-dir", str(output_dir / "db-backups")])
    else:
        command.append("--dry-run")
    completed = run_subprocess(command, timeout_seconds=SUBPROCESS_TIMEOUT_SECONDS)
    payload = read_json(summary_path)
    return {
        "status": "success" if completed.returncode == 0 else "failed",
        "command": redact_command(command),
        "exit_code": completed.returncode,
        "input": str(input_path),
        "summary_path": str(summary_path),
        "accepted_rows": payload.get("accepted_rows", 0),
        "rejected_rows": payload.get("rejected_rows", 0),
        "stored_rows": payload.get("stored_rows", 0),
        "would_store_rows": payload.get("would_store_rows", 0),
        "backup_path": payload.get("backup_path", ""),
        "errors": payload.get("errors", [])[:10],
        "stderr_tail": completed.stderr[-1000:],
    }


def run_quality_gate(args: argparse.Namespace, *, output_dir: Path) -> dict[str, Any]:
    output = output_dir / "source-automation" / "delivery-data-quality.json"
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
    completed = run_subprocess(command, timeout_seconds=SUBPROCESS_TIMEOUT_SECONDS)
    payload = read_json(output)
    return {
        "exit_code": completed.returncode,
        "output": str(output),
        "overall_status": payload.get("overall_status", "missing"),
        "alerts": len(payload.get("alerts", [])) if isinstance(payload.get("alerts"), list) else 0,
        "stderr_tail": completed.stderr[-1000:],
    }


def run_seven_product_lifecycle(
    args: argparse.Namespace, *, output_dir: Path, business_date: str = ""
) -> dict[str, Any]:
    lifecycle_output_dir = output_dir / "seven-product-lifecycle"
    latest_path = lifecycle_output_dir / "seven-product-lifecycle-latest.json"
    command = [
        sys.executable,
        str(SERVER_ROOT / "scripts" / "run_seven_product_forecast_lifecycle.py"),
        "--db",
        str(args.db),
        "--output-dir",
        str(lifecycle_output_dir),
        "--apply" if args.apply else "--dry-run",
    ]
    if args.as_of:
        command.extend(["--as-of", args.as_of])
    event_chain_report = output_dir / "event-agent-chain-latest.json"
    if event_chain_report.is_file():
        # Audit P1-2: never fuse a stale report — if node B failed today, the
        # file on disk is yesterday's and must not enter today's lifecycle.
        chain_report_check = read_json(event_chain_report)
        chain_report_date = str(chain_report_check.get("business_date") or "")
        if business_date and chain_report_date != business_date:
            event_chain_report = None
    if event_chain_report is not None and event_chain_report.is_file():
        command.extend(["--event-chain-report", str(event_chain_report)])
    for attempt in range(1, 4):
        completed = run_subprocess(command, timeout_seconds=SUBPROCESS_TIMEOUT_SECONDS)
        if completed.returncode != 75 or attempt == 3:
            break
        time.sleep(2 ** (attempt - 1))
    report = read_json(latest_path)
    return {
        "command": redact_command(command),
        "exit_code": completed.returncode,
        "status": report.get("status", "missing"),
        "latest_path": str(latest_path),
        "report_sha256": report.get("report_sha256", ""),
        "forecast": report.get("forecast", {}),
        "settlement": report.get("settlement", {}),
        "event_factor_settlement": report.get("event_factor_settlement", {}),
        "event_fusion": report.get("event_fusion", {}),
        "blockers": report.get("blockers", []),
        "warnings": report.get("warnings", []),
        "stderr_tail": completed.stderr[-1000:],
        "attempts": attempt,
    }


def run_seven_product_evaluation(
    args: argparse.Namespace,
    *,
    output_dir: Path,
    issued_business_date: str | None = None,
    anchor_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate fresh daily OOS evidence without treating an honest 0/21 as a crash."""

    evaluation_output_dir = output_dir / "seven-product-evaluation"
    latest_path = evaluation_output_dir / "seven-product-evaluation-latest.json"
    previous_identity = _file_identity(latest_path)
    if args.apply and not issued_business_date:
        return _evaluation_step_failure(
            "issued_forecast_business_date_missing",
            latest_path=latest_path,
        )
    evaluation_input: dict[str, Any] = {"mode": "live_database_preview"}
    database_argument = args.db
    if args.apply and issued_business_date:
        anchor_path: Path | None = None
        if isinstance(anchor_report, dict) and str(anchor_report.get("integrity_check") or "") == "ok":
            anchor_candidate = str(anchor_report.get("backup_path") or "")
            if anchor_candidate:
                anchor_path = Path(anchor_candidate)
        try:
            evaluation_input = create_evaluation_input_snapshot(
                args.db.expanduser().resolve(),
                snapshot_dir=evaluation_output_dir / "evaluation-input",
                business_date=issued_business_date,
                anchor_path=anchor_path,
            )
        except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
            return _evaluation_step_failure(
                f"evaluation_input_snapshot_failed:{exc.__class__.__name__}",
                latest_path=latest_path,
            )
        database_argument = Path(str(evaluation_input["path"]))
    command = [
        sys.executable,
        str(SERVER_ROOT / "scripts" / "run_seven_product_evaluation.py"),
        "--db",
        str(database_argument),
        "--output-dir",
        str(evaluation_output_dir),
    ]
    if issued_business_date:
        command.extend(["--issued-business-date", issued_business_date])
    if args.as_of:
        command.extend(["--as-of", args.as_of])
    completed = run_subprocess(command, timeout_seconds=SUBPROCESS_TIMEOUT_SECONDS)
    fresh_output = _file_identity(latest_path) not in {None, previous_identity}
    payload = read_json(latest_path) if fresh_output else {}
    evidence_valid, validation_error = _validate_daily_evaluation_evidence(payload)
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    database = payload.get("database") if isinstance(payload.get("database"), dict) else {}
    return {
        "command": redact_command(command),
        "exit_code": completed.returncode,
        "status": payload.get("status", "missing"),
        "fresh_output": fresh_output,
        "evidence_valid": evidence_valid,
        "validation_error": validation_error,
        "latest_path": str(latest_path),
        "evidence_body_sha256": payload.get("evidence_body_sha256", ""),
        "contract_complete": summary.get("contract_complete", False),
        "evaluation_cells": summary.get("evaluation_cells", 0),
        "passed_count": summary.get("passed_count", 0),
        "failed_count": summary.get("failed_count", 0),
        "evaluation_report_sha256": summary.get("evaluation_report_sha256", ""),
        "forecast_source": payload.get("forecast_source", ""),
        "forecast_batch_id": payload.get("forecast_batch_id", ""),
        "evaluation_cutoff_source": payload.get("evaluation_cutoff_source", ""),
        "forecast_as_of_time": payload.get("forecast_as_of_time", ""),
        "evaluation_as_of_time": payload.get("evaluation_as_of_time", ""),
        "cutoff_matches_forecast": payload.get("cutoff_matches_forecast", False),
        "database_sha256": database.get("sha256", ""),
        "database_main_sha256": database.get("main_sha256", ""),
        "database_fingerprint_schema_version": database.get("fingerprint_schema_version", ""),
        "database_unchanged_during_run": database.get("unchanged_during_run", False),
        "evaluation_input_mode": str(evaluation_input.get("mode", "")),
        "evaluation_input_source": str(evaluation_input.get("source", "")),
        "evaluation_input_degraded_reason": str(evaluation_input.get("degraded_reason", "")),
        "evaluation_input_path": str(evaluation_input.get("path", "")),
        "evaluation_input_sha256": str(evaluation_input.get("sha256", "")),
        "stderr_tail": completed.stderr[-1000:],
    }


def _evaluation_step_failure(validation_error: str, *, latest_path: Path) -> dict[str, Any]:
    return {
        "command": [],
        "exit_code": None,
        "status": "missing",
        "fresh_output": False,
        "evidence_valid": False,
        "validation_error": validation_error,
        "latest_path": str(latest_path),
        "evidence_body_sha256": "",
        "contract_complete": False,
        "evaluation_cells": 0,
        "passed_count": 0,
        "failed_count": 21,
        "evaluation_report_sha256": "",
        "forecast_source": "missing",
        "forecast_batch_id": "",
        "evaluation_cutoff_source": "missing",
        "forecast_as_of_time": "",
        "evaluation_as_of_time": "",
        "cutoff_matches_forecast": False,
        "database_sha256": "",
        "database_main_sha256": "",
        "database_fingerprint_schema_version": "",
        "database_unchanged_during_run": False,
        "evaluation_input_mode": "missing",
        "evaluation_input_source": "",
        "evaluation_input_degraded_reason": "",
        "evaluation_input_path": "",
        "evaluation_input_sha256": "",
        "stderr_tail": "",
    }


EVALUATION_SNAPSHOT_ATTEMPTS = 3
EVALUATION_SNAPSHOT_RETRY_SECONDS = 10


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_temporary_sqlite(path: Path) -> None:
    path.unlink(missing_ok=True)
    for suffix in ("-shm", "-wal", "-journal"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def create_evaluation_input_snapshot(
    db_path: Path,
    *,
    snapshot_dir: Path,
    business_date: str,
    anchor_path: Path | None = None,
) -> dict[str, Any]:
    """Freeze the just-issued database into a private consistent snapshot for OOS evaluation.

    Final backup state (DISK-MODEL §7): when the unified daily anchor from this
    same chain run is available (created after issue, integrity-gated), the
    snapshot is a hard link to the anchor inode -- zero extra bytes on disk,
    opened read-only by the evaluator. Anchor rotation unlinks only the anchor
    directory entry, so the linked data survives (POSIX link semantics). When
    the same-run anchor is missing or the link is impossible (cross-volume),
    fall back to the legacy full online-backup snapshot and record the degraded
    reason; an older anchor is never linked silently.
    """

    secure_private_directory(snapshot_dir)
    final_path = snapshot_dir / f"agent-evaluation-input-{business_date}.sqlite"
    degraded_reason = ""
    snapshot: dict[str, Any] | None = None
    if anchor_path is not None:
        snapshot = _hardlink_evaluation_input_to_anchor(
            anchor_path, final_path, business_date=business_date
        )
        if snapshot is None:
            degraded_reason = (
                "evaluation_input_anchor_missing"
                if not anchor_path.is_file()
                else "evaluation_input_anchor_link_failed"
            )
    if snapshot is None:
        for attempt in range(1, EVALUATION_SNAPSHOT_ATTEMPTS + 1):
            descriptor, temporary_name = tempfile.mkstemp(
                dir=snapshot_dir, prefix=f".{final_path.name}.", suffix=".tmp"
            )
            os.close(descriptor)
            temporary_path: Path | None = Path(temporary_name)
            try:
                with closing(
                    sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=30)
                ) as source:
                    source.execute("PRAGMA query_only = ON")
                    with closing(sqlite3.connect(temporary_path)) as destination:
                        source.backup(destination)
                with closing(
                    sqlite3.connect(f"{temporary_path.as_uri()}?mode=ro", uri=True)
                ) as snapshot_connection:
                    quick_check = str(snapshot_connection.execute("PRAGMA quick_check").fetchone()[0])
                if quick_check != "ok":
                    raise RuntimeError(f"evaluation_input_snapshot_integrity_failed:{quick_check}")
                os.chmod(temporary_path, 0o600)
                handle = os.open(temporary_path, os.O_RDONLY)
                try:
                    os.fsync(handle)
                finally:
                    os.close(handle)
                os.replace(temporary_path, final_path)
                temporary_path = None
                _fsync_directory(snapshot_dir)
                break
            except (OSError, sqlite3.Error, RuntimeError, ValueError):
                if attempt >= EVALUATION_SNAPSHOT_ATTEMPTS:
                    raise
                time.sleep(EVALUATION_SNAPSHOT_RETRY_SECONDS)
            finally:
                if temporary_path is not None:
                    _remove_temporary_sqlite(temporary_path)
        snapshot = {
            "mode": "consistent_snapshot_after_issue",
            "source": "full_online_backup_fallback" if anchor_path is not None else "full_online_backup",
            "path": str(final_path),
            "business_date": business_date,
            "sha256": _sha256_file(final_path),
            "size": final_path.stat().st_size,
        }
    if degraded_reason:
        snapshot["degraded_reason"] = degraded_reason
    for stale in snapshot_dir.glob("agent-evaluation-input-*.sqlite"):
        if stale != final_path:
            remove_sqlite_artifacts(stale)
    return snapshot


def _hardlink_evaluation_input_to_anchor(
    anchor_path: Path,
    final_path: Path,
    *,
    business_date: str,
) -> dict[str, Any] | None:
    """Zero-copy evaluation input: hard-link the same-run daily anchor.

    Returns None (caller falls back to the full copy) when the anchor is
    missing, not a regular file, or the link cannot be created -- e.g. EXDEV on
    a cross-volume snapshot directory.
    """

    try:
        anchor_stat = anchor_path.stat()
    except OSError:
        return None
    if not anchor_path.is_file():
        return None
    try:
        try:
            existing_stat = final_path.stat()
        except OSError:
            existing_stat = None
        if existing_stat is None or (existing_stat.st_dev, existing_stat.st_ino) != (
            anchor_stat.st_dev,
            anchor_stat.st_ino,
        ):
            if existing_stat is not None:
                final_path.unlink()
                for suffix in ("-shm", "-wal", "-journal"):
                    Path(f"{final_path}{suffix}").unlink(missing_ok=True)
            os.link(anchor_path, final_path)
    except OSError:
        return None
    return {
        "mode": "consistent_snapshot_after_issue",
        "source": "daily_anchor_hardlink",
        "path": str(final_path),
        "business_date": business_date,
        "anchor_path": str(anchor_path),
        "sha256": _sha256_file(final_path),
        "size": final_path.stat().st_size,
    }


def _validate_daily_evaluation_evidence(payload: dict[str, Any]) -> tuple[bool, str]:
    if not payload:
        return False, "evaluation_evidence_missing_or_invalid_json"
    if payload.get("schema_version") != "seven-product-evaluation-evidence.v2":
        return False, "evaluation_evidence_schema_invalid"
    body_sha256 = payload.get("evidence_body_sha256")
    body = dict(payload)
    body.pop("evidence_body_sha256", None)
    if not isinstance(body_sha256, str) or len(body_sha256) != 64 or _canonical_sha256(body) != body_sha256:
        return False, "evaluation_evidence_hash_invalid"
    status = payload.get("status")
    summary = payload.get("summary")
    database = payload.get("database")
    if status not in {"passed", "blocked"} or not isinstance(summary, dict) or not isinstance(database, dict):
        return False, "evaluation_evidence_contract_invalid"
    if not _valid_sqlite_fingerprint(database):
        return False, "evaluation_database_fingerprint_invalid"
    if payload.get("forecast_source") not in {"issued_ledger", "point_in_time_preview"}:
        return False, "evaluation_forecast_source_invalid"
    if not isinstance(payload.get("forecast_batch_id"), str) or not payload["forecast_batch_id"]:
        return False, "evaluation_forecast_batch_id_missing"
    forecast_as_of = payload.get("forecast_as_of_time")
    evaluation_as_of = payload.get("evaluation_as_of_time")
    expected_cutoff_source = (
        "issued_batch_as_of"
        if payload.get("forecast_source") == "issued_ledger"
        else "requested_or_run_start_as_of"
    )
    if (
        payload.get("evaluation_cutoff_source") != expected_cutoff_source
        or not isinstance(forecast_as_of, str)
        or not forecast_as_of
        or evaluation_as_of != forecast_as_of
        or payload.get("cutoff_matches_forecast") is not True
    ):
        return False, "evaluation_forecast_cutoff_mismatch"
    passed_count = summary.get("passed_count")
    failed_count = summary.get("failed_count")
    if (
        summary.get("contract_complete") is not True
        or summary.get("forecast_cells") != 21
        or summary.get("evaluation_cells") != 21
        or not isinstance(passed_count, int)
        or isinstance(passed_count, bool)
        or not 0 <= passed_count <= 21
        or failed_count != 21 - passed_count
        or database.get("unchanged_during_run") is not True
    ):
        return False, "evaluation_evidence_contract_invalid"
    expected_status = "passed" if passed_count == 21 else "blocked"
    if status != expected_status:
        return False, "evaluation_evidence_status_inconsistent"
    return True, ""


def _valid_sqlite_fingerprint(database: dict[str, Any]) -> bool:
    if (
        database.get("fingerprint_schema_version") != "sqlite-state-fingerprint.v1"
        or not isinstance(database.get("sha256"), str)
        or len(database["sha256"]) != 64
        or not isinstance(database.get("main_sha256"), str)
        or len(database["main_sha256"]) != 64
        or not isinstance(database.get("artifacts"), dict)
    ):
        return False
    artifacts = database["artifacts"]
    if set(artifacts) != {"main", "wal", "journal"}:
        return False
    for role in ("main", "wal", "journal"):
        item = artifacts.get(role)
        if not isinstance(item, dict) or not isinstance(item.get("exists"), bool):
            return False
        if role == "main" and item["exists"] is not True:
            return False
        if item["exists"] and (
            not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool)
            or item["size"] < 0
            or not isinstance(item.get("mtime_ns"), int)
            or isinstance(item.get("mtime_ns"), bool)
            or not isinstance(item.get("sha256"), str)
            or len(item["sha256"]) != 64
        ):
            return False
    content_artifacts = {
        role: {key: value for key, value in artifacts[role].items() if key != "mtime_ns"}
        for role in ("main", "wal", "journal")
    }
    expected_state_sha256 = _canonical_sha256(
        {
            "schema_version": "sqlite-state-fingerprint.v1",
            "artifacts": content_artifacts,
        }
    )
    return (
        database["main_sha256"] == artifacts["main"].get("sha256")
        and database["sha256"] == expected_state_sha256
    )


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_identity(path: Path) -> tuple[int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def run_foundation_materialization(
    args: argparse.Namespace, *, output_dir: Path, anchor_report: dict[str, Any]
) -> dict[str, Any]:
    """Run foundation materialization with the unified daily anchor as recovery point.

    Disk consolidation 2026-09-17 (DISK-MODEL §3.2 项2): this step no longer
    takes its own full ``copy2`` backup. Apply mode is gated on the anchor that
    ``verify_backup`` produced earlier in the same chain run; each materializer
    write stays transactional, and the observation-only fallback plus
    provider-review guards keep partial failures non-destructive.
    """

    foundation_output_dir = output_dir / "foundation"
    anchor_path = str(anchor_report.get("backup_path") or "")
    summary_path = foundation_output_dir / "foundation-materialization-summary.json"
    if args.apply and (
        anchor_report.get("integrity_check") != "ok" or not anchor_path or not Path(anchor_path).is_file()
    ):
        return {
            "status": "blocked",
            "exit_code": None,
            "reason": "daily_anchor_unavailable",
            "output_dir": str(foundation_output_dir),
            "summary_path": str(summary_path),
            "recovery_anchor": anchor_path,
            "recovery_anchor_integrity": str(anchor_report.get("integrity_check") or "not_run"),
            "memory": {},
            "rag_index": {},
            "graph_snapshot": {},
            "context_pack": {},
            "agent_trace": {},
            "before": {},
            "after": {},
            "guards": {},
        }
    command = [
        sys.executable,
        str(SERVER_ROOT / "scripts" / "materialize_agent_foundation.py"),
        "--db",
        str(args.db),
        "--output-dir",
        str(foundation_output_dir),
    ]
    if args.apply:
        command.extend(["--apply", "--recovery-anchor", anchor_path])
    else:
        command.append("--dry-run")
    # Relaxed vs other steps: the direction-review upstream gate may wait up
    # to DIRECTION_UPSTREAM_GATE_MAX_WAIT_SECONDS inside this materializer.
    completed = run_subprocess(command, timeout_seconds=FOUNDATION_STEP_TIMEOUT_SECONDS)
    payload = read_json(summary_path)
    return {
        "status": payload.get("status", "missing"),
        "command": redact_command(command),
        "exit_code": completed.returncode,
        "output_dir": str(foundation_output_dir),
        "summary_path": str(summary_path),
        "memory": payload.get("memory", {}),
        "rag_index": payload.get("rag_index", {}),
        "graph_snapshot": payload.get("graph_snapshot", {}),
        "context_pack": payload.get("context_pack", {}),
        "agent_trace": payload.get("agent_trace", {}),
        "before": payload.get("before", {}),
        "after": payload.get("after", {}),
        "guards": payload.get("guards", {}),
        "recovery_anchor": anchor_path,
        "stdout_tail": completed.stdout[-1000:],
        "stderr_tail": completed.stderr[-1000:],
    }


def run_subprocess(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            args=command,
            returncode=124,
            stdout=(exc.stdout or "")[-1500:] if isinstance(exc.stdout, str) else "",
            stderr=f"timeout_after_{timeout_seconds}s: {exc}"[-1500:],
        )


def inspect_database(db_path: Path) -> dict[str, Any]:
    tables = [
        "forecast_price_points",
        "industry_observations",
        "market_observations",
        "news_articles",
        "event_observations",
        "source_acquisition_runs",
        "agent_runs",
        "agent_tasks",
        "agent_turns",
        "agent_io_records",
        "agent_tool_calls",
        "evidence_bundles",
        "memory_items",
        "rag_documents",
        "rag_chunks",
        "graph_nodes",
        "graph_edges",
    ]
    snapshot: dict[str, Any] = {"db_path": str(db_path), "exists": db_path.exists(), "tables": {}}
    if not db_path.exists():
        return snapshot
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection:
            for table in tables:
                if not table_exists(connection, table):
                    snapshot["tables"][table] = {"exists": False, "rows": 0}
                    continue
                count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                snapshot["tables"][table] = {"exists": True, "rows": int(count)}
            snapshot["ccf_industry_latest"] = query_rows(
                connection,
                """
                SELECT product, metric, frequency, MAX(observed_at) AS latest_observed_at, COUNT(*) AS rows
                FROM industry_observations
                WHERE source_id LIKE 'ccf%'
                GROUP BY product, metric, frequency
                ORDER BY product, metric
                """,
            )
            snapshot["market_latest"] = query_rows(
                connection,
                """
                SELECT source_id, MAX(observed_at) AS latest_observed_at, COUNT(*) AS rows
                FROM market_observations
                GROUP BY source_id
                ORDER BY source_id
                """,
            )
    except sqlite3.Error as exc:
        snapshot["error"] = exc.__class__.__name__
    return snapshot


def verify_backup(db_path: Path, *, business_date: str = "") -> dict[str, Any]:
    """Create (or reuse) the unified daily backup anchor and drill a restore.

    Disk consolidation 2026-09-17 (DISK-MODEL §3.2): the chain no longer keeps
    separate probe/restore-drill full copies. Exactly one online-backup anchor
    is taken per business day, gated on ``PRAGMA integrity_check``, rotated to
    the newest ``DAILY_ANCHOR_RETENTION`` snapshots. The restore drill copies
    the anchor once, re-checks integrity plus core table counts, then deletes
    the copy (net anchor-directory footprint stays at the rotated anchors).
    """

    if not business_date:
        business_date = datetime.now(DAILY_CHAIN_BUSINESS_TIMEZONE).date().isoformat()
    anchor_dir = db_path.parent / "backups" / DAILY_ANCHOR_DIR_NAME
    report: dict[str, Any] = {
        "db_path": str(db_path),
        "business_date": business_date,
        "anchor_dir": str(anchor_dir),
        "backup_created": False,
        "integrity_check": "not_run",
        "restore_drill": "not_run",
    }
    if not db_path.exists():
        report["error"] = "database_not_found"
        return report
    secure_private_directory(anchor_dir)
    day_pattern = f"{db_path.name}.daily_anchor_{business_date.replace('-', '')}_*.sqlite"
    same_day = sorted(
        (path for path in anchor_dir.glob(day_pattern) if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    anchor_path: Path | None = None
    if same_day and _sqlite_integrity_result(same_day[0]) == "ok":
        anchor_path = same_day[0]
        report["anchor_reused"] = True
    else:
        # Prune before allocating another full SQLite copy; waiting until after
        # the copy can deadlock recovery when anchors have already filled the disk.
        report["pruned_before_anchor"] = prune_file_retention(
            anchor_dir,
            f"{db_path.name}.daily_anchor_*.sqlite",
            keep=max(0, DAILY_ANCHOR_RETENTION - 1),
        )
        candidate = anchor_dir / (
            # Date part comes from the business date (Asia/Shanghai), matching the
            # same-day reuse pattern above; the UTC clock only supplies the time.
            # Using the UTC date here broke reuse between 00:00-08:00 CST.
            f"{db_path.name}.daily_anchor_{business_date.replace('-', '')}_"
            f"{datetime.now(UTC).strftime('%H%M%S')}.sqlite"
        )
        counter = 1
        while candidate.exists():
            candidate = anchor_dir / f"{candidate.stem}-{counter}.sqlite"
            counter += 1
        try:
            # Online backup includes committed WAL pages; copying only the main
            # file can produce an integrity-valid but stale snapshot while
            # writers run.
            with (
                closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=30)) as source,
                closing(sqlite3.connect(candidate, timeout=30)) as destination,
            ):
                source.backup(destination, pages=256)
            secure_sqlite_artifacts(candidate)
            anchor_path = candidate
            report["anchor_reused"] = False
        except (OSError, sqlite3.Error) as exc:
            remove_sqlite_artifacts(candidate)
            report["error"] = f"anchor_copy_failed:{exc.__class__.__name__}"
            report["detail"] = str(exc)[:500]
            report["integrity_check"] = "anchor_copy_failed"
            return report
    report["backup_created"] = True
    report["backup_method"] = "sqlite_online_backup"
    report["backup_path"] = str(anchor_path)
    try:
        with closing(sqlite3.connect(f"file:{anchor_path}?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA cache_size = -65536")
            report["integrity_check"] = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        secure_sqlite_artifacts(anchor_path)
    except sqlite3.Error as exc:
        report["integrity_check"] = exc.__class__.__name__
    report["anchor_retention"] = {
        "keep": DAILY_ANCHOR_RETENTION,
        "pruned": prune_file_retention(
            anchor_dir,
            f"{db_path.name}.daily_anchor_*.sqlite",
            keep=DAILY_ANCHOR_RETENTION,
        ),
        "kept": len(list(anchor_dir.glob(f"{db_path.name}.daily_anchor_*.sqlite"))),
    }
    if report["integrity_check"] != "ok":
        return report
    # Restore drill against the anchor: copy once, verify, delete (net ~0).
    drill_path = anchor_path.parent / f"{anchor_path.stem}.restore_probe.sqlite"
    try:
        shutil.copy2(anchor_path, drill_path)
        secure_sqlite_artifacts(drill_path)
        with closing(sqlite3.connect(f"file:{drill_path}?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA cache_size = -65536")
            connection.row_factory = sqlite3.Row
            restore_integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            core_counts = {}
            for table in ("forecast_price_points", "industry_observations", "market_observations"):
                if table_exists(connection, table):
                    core_counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        secure_sqlite_artifacts(drill_path)
        report["restore_drill"] = "temp_copy_integrity_and_core_counts"
        report["restore_path"] = str(drill_path)
        report["restore_integrity_check"] = restore_integrity
        report["restore_core_counts"] = core_counts
    except (OSError, sqlite3.Error) as exc:
        report["restore_drill"] = exc.__class__.__name__
    finally:
        remove_sqlite_artifacts(drill_path)
    return report


def _sqlite_integrity_result(path: Path) -> str:
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as connection:
            # Bound page-cache memory to 64 MiB; retain the full integrity check
            # without thrashing the default 2 MiB cache on multi-GB day anchors.
            connection.execute("PRAGMA cache_size = -65536")
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.Error as exc:
        return exc.__class__.__name__


def prune_file_retention(directory: Path, pattern: str, *, keep: int) -> int:
    if keep < 0:
        raise ValueError("retention keep must be non-negative")
    if not directory.is_dir():
        return 0
    candidates = sorted(
        (path for path in directory.glob(pattern) if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    stale = candidates[keep:]
    for path in stale:
        remove_sqlite_artifacts(path)
    return len(stale)


def check_health(base_url: str) -> dict[str, Any]:
    endpoints = ["/api/v1/health/live", "/api/v1/health/ready", "/api/v1/health/deep", "/metrics"]
    results: list[dict[str, Any]] = []
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for endpoint in endpoints:
        url = base_url.rstrip("/") + endpoint
        last_error = ""
        for attempt in range(1, 3):
            try:
                with opener.open(url, timeout=35) as response:  # noqa: S310 - local operator URL.
                    body = response.read(200).decode("utf-8", errors="replace")
                    results.append(
                        {
                            "endpoint": endpoint,
                            "ok": 200 <= response.status < 400,
                            "status": response.status,
                            "attempts": attempt,
                            "preview": body,
                        }
                    )
                    break
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc.__class__.__name__
                if attempt < 2:
                    time.sleep(1)
        else:
            results.append({"endpoint": endpoint, "ok": False, "status": 0, "attempts": 2, "error": last_error})
    return {"base_url": base_url, "results": results, "all_ok": all(item["ok"] for item in results)}


def build_readiness(
    *,
    mode: str,
    started_at: str,
    finished_at: str,
    source_process: dict[str, Any],
    source_latest: dict[str, Any],
    industry_import: dict[str, Any],
    quality_refresh: dict[str, Any],
    foundation_materialization: dict[str, Any],
    seven_product_lifecycle: dict[str, Any],
    seven_product_evaluation: dict[str, Any],
    quality: dict[str, Any],
    db_snapshot: dict[str, Any],
    backup_report: dict[str, Any],
    health_report: dict[str, Any],
) -> dict[str, Any]:
    quality = customer_quality_report(quality)
    blockers: list[str] = []
    warnings: list[str] = []
    source_run_status = str(source_latest.get("status") or "")
    critical_source_failures = source_latest.get("critical_failures", [])
    observation_grade_source = (
        source_run_status == "blocked"
        and not critical_source_failures
        and quality.get("overall_status") == "needs_human_review"
    )
    if source_process.get("exit_code") != 0 and not source_process.get("fresh_output"):
        if observation_grade_source:
            warnings.append("source automation completed at observation grade")
        else:
            blockers.append("source automation command failed")
    if critical_source_failures:
        names = ", ".join(str(item) for item in critical_source_failures)
        blockers.append(f"critical public source fetch failed: {names}")
    elif source_run_status == "blocked" and not observation_grade_source and quality.get("overall_status") != "blocked":
        blockers.append("source automation status is blocked")
    elif source_run_status == "degraded" or observation_grade_source:
        warnings.append("source automation completed with isolated source failures")
    if quality_refresh.get("exit_code") not in {0, None}:
        blockers.append("quality gate refresh command failed")
    if foundation_materialization.get("exit_code") not in {0, None}:
        blockers.append("agent foundation materialization command failed")
    lifecycle_status = str(seven_product_lifecycle.get("status") or "missing")
    if seven_product_lifecycle.get("exit_code") != 0 or lifecycle_status in {"blocked", "failed", "missing"}:
        blockers.append("seven-product forecast lifecycle did not complete")
    elif lifecycle_status == "ready_with_warnings":
        warnings.extend(str(item) for item in seven_product_lifecycle.get("warnings", []) if item)
    evaluation_exit_code = seven_product_evaluation.get("exit_code")
    evaluation_status = str(seven_product_evaluation.get("status") or "missing")
    evaluation_execution_ok = (
        evaluation_exit_code == (0 if evaluation_status == "passed" else 2)
        and seven_product_evaluation.get("fresh_output") is True
        and seven_product_evaluation.get("evidence_valid") is True
        and seven_product_evaluation.get("database_unchanged_during_run") is True
        and seven_product_evaluation.get("cutoff_matches_forecast") is True
        and (
            (mode == "apply" and seven_product_evaluation.get("forecast_source") == "issued_ledger")
            or (mode != "apply" and seven_product_evaluation.get("forecast_source") == "point_in_time_preview")
        )
        and (
            mode != "apply"
            or seven_product_evaluation.get("evaluation_input_mode") == "consistent_snapshot_after_issue"
        )
    )
    if not evaluation_execution_ok:
        blockers.append("seven-product OOS evaluation did not publish fresh valid read-only evidence")
    elif evaluation_status == "blocked":
        blocked_cells = 21 - int(seven_product_evaluation.get("passed_count", 0) or 0)
        warnings.append(
            f"formal OOS promotion remains blocked for {blocked_cells} of 21 cells"
        )
    evaluation_input_degraded = str(seven_product_evaluation.get("evaluation_input_degraded_reason") or "")
    if evaluation_input_degraded:
        # Final backup state (DISK-MODEL §7): non-blocking, but never silent --
        # the evaluation input fell back to a full copy because the same-run
        # daily anchor was unavailable or not linkable.
        warnings.append(f"evaluation input used a full copy fallback: {evaluation_input_degraded}")
    foundation_status = foundation_materialization.get("status", "missing")
    if mode == "apply" and foundation_status != "success":
        blockers.append(f"agent foundation materialization status is {foundation_status}")
    if mode == "apply" and foundation_status == "success":
        after_counts = foundation_materialization.get("after", {})
        if int(after_counts.get("rag_chunks", 0) or 0) <= 0:
            blockers.append("persistent RAG index has no chunks")
        if int(after_counts.get("graph_nodes", 0) or 0) <= 0:
            blockers.append("persistent evidence graph has no nodes")
        trace = foundation_materialization.get("agent_trace", {})
        upstream_gate = trace.get("upstream_gate", {})
        if upstream_gate.get("status") == "timeout":
            warnings.append(
                "direction review skipped: upstream gate timeout ("
                + str(upstream_gate.get("reason_code") or "unknown") + ")"
            )
        execution_semantics = str(trace.get("execution_semantics") or "")
        if execution_semantics == "materialized_only":
            if int(trace.get("job_count", 0) or 0) != 12:
                blockers.append("daily Agent taxonomy did not materialize all 12 roles")
            if (
                int(trace.get("turn_count", 0) or 0) != 0
                or int(trace.get("handoff_count", 0) or 0) != 0
                or int(trace.get("accepted_handoffs", 0) or 0) != 0
                or bool(trace.get("report_agent_completed"))
                or bool(trace.get("report_artifact_id"))
            ):
                blockers.append("materialized-only Agent taxonomy contains false execution claims")
            warnings.append("daily Agent taxonomy is materialized-only; no execution or report is claimed")
        elif execution_semantics == "runtime_execution":
            if int(trace.get("turn_count", 0) or 0) != 12:
                blockers.append("daily Agent pipeline did not complete all 12 turns")
            if int(trace.get("accepted_handoffs", 0) or 0) != 11:
                blockers.append("daily Agent pipeline did not accept all 11 handoffs")
            if not trace.get("report_agent_completed"):
                blockers.append("report Agent did not complete; daily report is not eligible")
            if not trace.get("report_artifact_id"):
                blockers.append("report Agent did not persist a daily report artifact")
        else:
            blockers.append("agent foundation execution semantics are missing or unsupported")
    quality_status = quality.get("overall_status", "missing")
    if quality_status == "blocked":
        blockers.append("quality gate is blocked")
    elif quality_status in {"missing", "failed"}:
        blockers.append(f"quality gate status is {quality_status}")
    elif quality_status != "success":
        warnings.append(f"quality gate status is {quality_status}")
    if backup_report.get("integrity_check") != "ok":
        blockers.append("daily anchor integrity check did not pass")
    if health_report and not health_report.get("all_ok", False):
        warnings.append("local backend health endpoints are not all reachable")

    source_after = source_latest.get("after", {})
    manual_or_blocked = (
        int(source_after.get("non_ccf_manual_or_blocked", source_after.get("manual_or_blocked", 0)) or 0)
        if isinstance(source_after, dict)
        else 0
    )
    if manual_or_blocked:
        warnings.append(f"{manual_or_blocked} source acquisition tasks remain manual or blocked")

    return {
        "schema_version": "local_production_daily.v1",
        "mode": mode,
        "started_at": started_at,
        "finished_at": finished_at,
        "overall_status": "blocked" if blockers else "ready_with_warnings" if warnings else "ready",
        "blockers": blockers,
        "warnings": warnings,
        "source_automation": source_process,
        "source_summary": source_latest,
        "legacy_ccf": industry_import,
        "quality_refresh": quality_refresh,
        "foundation_materialization": foundation_materialization,
        "seven_product_forecast_lifecycle": seven_product_lifecycle,
        "seven_product_oos_evaluation": seven_product_evaluation,
        "quality_gate": quality,
        "db_snapshot": db_snapshot,
        "backup_restore": backup_report,
        "health": health_report,
        "guards": {
            "credentials_logged": False,
            # Data steps stay LLM-free; in apply mode the post-snapshot
            # observation-only agents (counter scan, daily interpretation)
            # each make at most one paid provider call, budget-capped and
            # failure-isolated from the publishables (DESIGN §2.6).
            "calls_external_llm_provider": mode == "apply",
            "writes_database": mode == "apply",
            "apply_requires_backup": True,
            "ccf_operational_status": "soft_removed",
        },
    }


def write_reports(output_dir: Path, readiness: dict[str, Any]) -> None:
    write_markdown(output_dir / "01-local-readiness-audit.md", render_readiness_audit(readiness))
    write_markdown(output_dir / "02-source-automation-report.md", render_source_report(readiness))
    write_markdown(output_dir / "03-local-daily-automation-report.md", render_daily_report(readiness))
    write_markdown(output_dir / "04-quality-gate-report.md", render_quality_report(readiness))
    write_markdown(output_dir / "05-local-env-report.md", render_env_report(readiness))
    write_markdown(output_dir / "06-backup-restore-report.md", render_backup_report(readiness))
    write_markdown(output_dir / "07-local-monitoring-report.md", render_monitoring_report(readiness))
    write_markdown(output_dir / "07-agent-foundation-report.md", render_foundation_report(readiness))
    if not (output_dir / "08-local-test-verification-report.md").exists():
        write_markdown(output_dir / "08-local-test-verification-report.md", render_test_placeholder(readiness))
    write_markdown(output_dir / "09-local-acceptance-report.md", render_acceptance_report(readiness))
    write_markdown(output_dir / "10-final-local-production-gate.md", render_final_gate(readiness))
    write_markdown(output_dir / "01-local-production-hardening-audit.md", render_hardening_audit(readiness))
    write_markdown(output_dir / "02-local-service-supervisor-report.md", render_supervisor_report())
    write_markdown(output_dir / "03-local-daily-scheduler-report.md", render_scheduler_report())
    write_markdown(output_dir / "05-warning-resolution-report.md", render_warning_resolution_report(readiness))
    write_markdown(output_dir / "06-backup-restore-drill-report.md", render_backup_restore_drill_report(readiness))
    write_markdown(output_dir / "07-local-monitoring-alert-report.md", render_monitoring_alert_report(readiness))
    if not (output_dir / "08-local-production-test-report.md").exists():
        write_markdown(
            output_dir / "08-local-production-test-report.md", render_local_production_test_placeholder(readiness)
        )
    write_markdown(
        output_dir / "09-final-local-unattended-production-report.md", render_final_unattended_report(readiness)
    )
    write_markdown(output_dir / "LOCAL_PRODUCTION_README.md", render_local_production_readme())


def customer_quality_report(quality: dict[str, Any]) -> dict[str, Any]:
    """Remove internal experiment gates from customer production readiness."""
    if not isinstance(quality, dict):
        return {}
    report = json.loads(json.dumps(quality))
    ignored_gate_ids = {"forecast_v2_action_gate"}
    gates = [
        gate for gate in report.get("gates", []) if isinstance(gate, dict) and gate.get("id") not in ignored_gate_ids
    ]
    alerts = [
        alert
        for alert in report.get("alerts", [])
        if isinstance(alert, dict) and alert.get("id") not in ignored_gate_ids
    ]
    report["gates"] = gates
    report["alerts"] = alerts
    has_blocked_gate = any(isinstance(gate, dict) and gate.get("status") == "blocked" for gate in gates)
    if report.get("overall_status") not in {"failed", "missing"} and not has_blocked_gate:
        report["overall_status"] = "needs_human_review" if alerts else "success"
    return report


def render_readiness_audit(readiness: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Local Readiness Audit",
            "",
            f"- Generated at: {readiness['finished_at']}",
            f"- Mode: {readiness['mode']}",
            f"- Overall status: {readiness['overall_status']}",
            f"- Blockers: {', '.join(readiness['blockers']) if readiness['blockers'] else 'none'}",
            f"- Warnings: {', '.join(readiness['warnings']) if readiness['warnings'] else 'none'}",
            "",
            "This report is local-only and does not deploy, publish, or write credentials.",
            "",
        ]
    )


def render_source_report(readiness: dict[str, Any]) -> str:  # noqa: F811
    source = readiness.get("source_summary", {})
    before = source.get("before", {})
    after = source.get("after", {})
    public_fetch = source.get("public_fetch", {})
    ccf = source.get("ccf_import", {})
    industry = readiness.get("ccf_industry_import", {})
    return "\n".join(
        [
            "# Source Automation Report",
            "",
            f"- Writes database: {source.get('writes_database', False)}",
            f"- Backup path: {source.get('backup_path') or 'not applicable'}",
            f"- Before task count: {before.get('task_count', 'unknown')}",
            f"- After task count: {after.get('task_count', 'unknown')}",
            f"- Public fetch stored observations: {public_fetch.get('stored_observations', 0)}",
            f"- CCF accepted rows: {ccf.get('accepted_rows', 0)}",
            f"- CCF stored rows: {ccf.get('stored_rows', 0)}",
            f"- CCF import errors: {len(ccf.get('errors', []) or [])}",
            f"- CCF industry import status: {industry.get('status', 'unknown')}",
            f"- CCF industry accepted rows: {industry.get('accepted_rows', 0)}",
            f"- CCF industry stored rows: {industry.get('stored_rows', 0)}",
            f"- CCF industry rejected rows: {industry.get('rejected_rows', 0)}",
            "",
            (
                "Commercial-source automation is authorization-first. CAPTCHA, 2FA, permission denials, export limits,"
                " and license boundaries remain human-handled blockers."
            ),
            "",
        ]
    )


def render_daily_report(readiness: dict[str, Any]) -> str:  # noqa: F811
    return "\n".join(
        [
            "# Local Daily Automation Report",
            "",
            "Run command:",
            "",
            "```bash",
            "npm run local:daily",
            "npm run local:daily -- --apply",
            "```",
            "",
            f"- Last mode: {readiness['mode']}",
            f"- Status: {readiness['overall_status']}",
            f"- Source output: {readiness['source_automation'].get('output_dir', '')}",
            "",
        ]
    )


def render_quality_report(readiness: dict[str, Any]) -> str:
    quality = readiness.get("quality_gate", {})
    lines = [
        "# Quality Gate Report",
        "",
        f"- Overall status: {quality.get('overall_status', 'missing')}",
        f"- As of: {quality.get('as_of', 'unknown')}",
        "",
        "| Gate | Status | Observed |",
        "| --- | --- | --- |",
    ]
    for gate in quality.get("gates", []) if isinstance(quality.get("gates"), list) else []:
        gate_name = gate.get("title") or gate.get("name") or gate.get("id") or "unnamed gate"
        lines.append(f"| {gate_name} | {gate.get('status', '')} | {gate.get('observed', '')} |")
    if quality.get("alerts"):
        lines.extend(["", "Alerts:"])
        for alert in quality.get("alerts", []):
            lines.append(f"- {alert}")
    lines.append("")
    return "\n".join(lines)


def render_env_report(readiness: dict[str, Any]) -> str:
    snapshot = readiness.get("db_snapshot", {})
    lines = [
        "# Local Environment Report",
        "",
        f"- Database exists: {snapshot.get('exists', False)}",
        f"- Database path: {snapshot.get('db_path', '')}",
        "",
        "| Table | Rows |",
        "| --- | ---: |",
    ]
    for table, item in sorted((snapshot.get("tables") or {}).items()):
        lines.append(f"| {table} | {item.get('rows', 0)} |")
    lines.append("")
    return "\n".join(lines)


def render_backup_report(readiness: dict[str, Any]) -> str:
    backup = readiness.get("backup_restore", {})
    return "\n".join(
        [
            "# Backup And Restore Report",
            "",
            f"- Daily anchor created: {backup.get('backup_created', False)}"
            f" (reused: {backup.get('anchor_reused', False)})",
            f"- Daily anchor path: {backup.get('backup_path', '')}",
            f"- Anchor business date: {backup.get('business_date', '')}",
            f"- Integrity check: {backup.get('integrity_check', 'not_run')}",
            f"- Restore drill type: {backup.get('restore_drill', '')}",
            "",
            (
                "Main database writes remain blocked unless a valid daily anchor exists; per-step full"
                " backups were consolidated into this single daily anchor (DISK-MODEL §3.2)."
            ),
            "",
        ]
    )


def render_monitoring_report(readiness: dict[str, Any]) -> str:
    health = readiness.get("health", {})
    lines = ["# Local Monitoring Report", "", f"- Base URL: {health.get('base_url', 'not checked')}", ""]
    lines.extend(["| Endpoint | OK | Status |", "| --- | --- | ---: |"])
    for item in health.get("results", []) if isinstance(health.get("results"), list) else []:
        lines.append(f"| {item.get('endpoint', '')} | {item.get('ok', False)} | {item.get('status', 0)} |")
    lines.extend(
        ["", "For unattended local operation, pair this report with an OS scheduler and alert channel owner.", ""]
    )
    return "\n".join(lines)


def _render_upstream_gate(gate: object) -> str:
    """One-line direction-review upstream gate state for the daily report."""
    if not isinstance(gate, dict) or not gate:
        return "not recorded"
    return (
        f"{gate.get('status', 'unknown')}, waited {gate.get('waited_seconds', 0)}s, "
        f"pending {gate.get('pending_summaries', 0)}, "
        f"in-flight {gate.get('in_flight_leases', 0)}, "
        f"evidence {gate.get('evidence_count', 0)}, reason {gate.get('reason_code', 'unknown')}"
    )


def render_foundation_report(readiness: dict[str, Any]) -> str:
    foundation = readiness.get("foundation_materialization", {})
    after = foundation.get("after", {})
    rag = foundation.get("rag_index", {})
    graph = foundation.get("graph_snapshot", {})
    context_pack = foundation.get("context_pack", {})
    trace = foundation.get("agent_trace", {})
    return "\n".join(
        [
            "# Agent Foundation Materialization Report",
            "",
            f"- Status: {foundation.get('status', 'missing')}",
            f"- Output: {foundation.get('summary_path', '')}",
            f"- Memory synced: {foundation.get('memory', {}).get('synced', 0)}",
            f"- RAG documents: {rag.get('document_count', after.get('rag_documents', 0))}",
            f"- RAG chunks: {rag.get('chunk_count', after.get('rag_chunks', 0))}",
            f"- Graph snapshot: {graph.get('snapshot_id', 'not created')}",
            f"- Graph nodes: {graph.get('node_count', after.get('graph_nodes', 0))}",
            f"- Graph edges: {graph.get('edge_count', after.get('graph_edges', 0))}",
            f"- Context pack: {context_pack.get('pack_id', 'not created')}",
            f"- Agent trace run: {trace.get('run_id', 'not created')}",
            f"- Execution semantics: {trace.get('execution_semantics', 'missing')}",
            f"- Materialized roles: {trace.get('job_count', 0)}",
            f"- Agent turns: {trace.get('turn_count', after.get('agent_turns', 0))}",
            (
                "- Direction upstream gate: "
                f"{_render_upstream_gate(trace.get('upstream_gate'))}"
            ),
            "",
            (
                "This step materializes local-only memory, persistent RAG chunks, evidence graph snapshots, context"
                " packs, and the 12-role Agent job taxonomy. Materialized jobs are not executed turns and do not"
                " claim an Agent-generated report. It does not record credentials."
            ),
            "",
        ]
    )


def render_hardening_audit(readiness: dict[str, Any]) -> str:  # noqa: F811
    return "\n".join(
        [
            "# Local Production Hardening Audit",
            "",
            f"- local:daily status: {readiness['overall_status']}",
            "- Backend residency: supported through launchd template or docker compose; current health is shown below.",
            (
                "- Frontend residency: supported through launchd template running the existing npm dev server for local"
                " access."
            ),
            (
                "- CCF automation: price CSV import and industry CSV import are wired into the local daily gate;"
                " browser capture remains authorization-first."
            ),
            "- Public-source automation: EIA, FRED, and CFTC fetchers run through source automation when configured.",
            (
                "- Agent foundation: local daily materializes memory, persistent RAG chunks, GraphRAG snapshots,"
                " context packs, and 12-step Agent IO trace records."
            ),
            (
                "- Quality gate: CCF freshness, industry freshness, unit checks, non-positive checks, leaks, and"
                " primary h1 gate are checked."
            ),
            (
                "- Backup/restore: the unified daily anchor is an online SQLite backup gated on PRAGMA"
                " integrity_check; the restore drill copies the anchor, re-checks integrity and core"
                " counts, then deletes the copy. It never overwrites the main DB."
            ),
            (
                "- Health checks: /api/v1/health/live, /api/v1/health/ready, /api/v1/health/deep, and /metrics are"
                " checked when backend is running."
            ),
            "- Logs/reports: local production artifacts are under .codex-run/local-production/.",
            "",
            f"- Blockers: {', '.join(readiness['blockers']) if readiness['blockers'] else 'none'}",
            f"- Warnings: {', '.join(readiness['warnings']) if readiness['warnings'] else 'none'}",
            "",
        ]
    )


def render_supervisor_report() -> str:
    return "\n".join(
        [
            "# Local Service Supervisor Report",
            "",
            "Default local supervisor: macOS launchd templates generated under `.codex-run/local-production/launchd/`.",
            "",
            "| Service | Address | Template | Notes |",
            "| --- | --- | --- | --- |",
            (
                "| Backend | http://127.0.0.1:8000 | com.poydty.agent.backend.plist | Runs uvicorn locally; logs under"
                " `.codex-run/local-production/logs/`. |"
            ),
            (
                "| Frontend | http://127.0.0.1:5173 | com.poydty.agent.frontend.plist | Runs the existing Vite dev"
                " server for local-only production access. |"
            ),
            (
                "| Daily gate | local scheduler | com.poydty.agent.local-daily.plist | Runs `npm run local:daily --"
                " --apply` at 18:00. |"
            ),
            "",
            (
                "Templates are not loaded automatically. Loading launchd jobs may require user confirmation and macOS"
                " automation permissions."
            ),
            "",
            (
                "Fallback: docker compose can keep the backend running with `restart: unless-stopped`; frontend still"
                " needs launchd/shell supervisor or a static preview service."
            ),
            "",
        ]
    )


def render_scheduler_report() -> str:  # noqa: F811
    return "\n".join(
        [
            "# Local Daily Scheduler Report",
            "",
            "Recommended local schedule:",
            "",
            "| Time | Cadence | Job | Automation boundary |",
            "| --- | --- | --- | --- |",
            (
                "| 08:30 | Weekdays | Public-source refresh and health check | `npm run local:daily -- --apply`"
                " includes public fetches. |"
            ),
            (
                "| 09:30 | Weekdays | CCF price/average check | Authorized browser capture must stay within the"
                " logged-in licensed page; local daily imports captured CSVs. |"
            ),
            (
                "| 16:30 | Fridays | CCF industry indicators | Import uses `ccf_industry_observations.csv`;"
                " CAPTCHA/2FA/permission issues require user action. |"
            ),
            (
                "| 18:00 | Daily | Final local production gate | launchd daily template runs `npm run local:daily --"
                " --apply`. |"
            ),
            "",
            (
                "The generated launchd template implements the 18:00 final gate. Additional CCF browser automations"
                " should call the existing authorized capture flow and write canonical CSVs into"
                " `.codex-run/ccf-authorized-capture/`."
            ),
            "",
        ]
    )


def render_ccf_production_report(readiness: dict[str, Any]) -> str:
    source = readiness.get("source_summary", {})
    ccf = source.get("ccf_import", {})
    industry = readiness.get("ccf_industry_import", {})
    quality = readiness.get("quality_gate", {})
    gates = {gate.get("id"): gate for gate in quality.get("gates", []) if isinstance(gate, dict)}
    return "\n".join(
        [
            "# CCF Production Automation Report",
            "",
            f"- CCF price import accepted rows: {ccf.get('accepted_rows', 0)}",
            f"- CCF price import stored rows: {ccf.get('stored_rows', 0)}",
            f"- CCF price import errors: {len(ccf.get('errors', []) or [])}",
            f"- CCF industry import status: {industry.get('status', 'unknown')}",
            f"- CCF industry accepted rows: {industry.get('accepted_rows', 0)}",
            f"- CCF industry stored rows: {industry.get('stored_rows', 0)}",
            f"- CCF industry rejected rows: {industry.get('rejected_rows', 0)}",
            f"- CCF industry backup path: {industry.get('backup_path') or 'not applicable'}",
            (
                f"- CCF coverage gate: {gates.get('coverage_gap_audit', {}).get('status', 'missing')}"
                f" ({gates.get('coverage_gap_audit', {}).get('observed', 'unknown')})"
            ),
            (
                f"- CCF price freshness: {gates.get('ccf_price_freshness', {}).get('status', 'missing')}"
                f" ({gates.get('ccf_price_freshness', {}).get('observed', 'unknown')})"
            ),
            (
                f"- CCF industry freshness: {gates.get('ccf_industry_freshness', {}).get('status', 'missing')}"
                f" ({gates.get('ccf_industry_freshness', {}).get('observed', 'unknown')})"
            ),
            "",
            (
                "No credentials are recorded by this gate. CAPTCHA, QR login, SMS, second-factor verification,"
                " permission denial, and export limits remain human-handled boundaries."
            ),
            "",
        ]
    )


def render_warning_resolution_report(readiness: dict[str, Any]) -> str:  # noqa: F811
    source_after = readiness.get("source_summary", {}).get("after", {})
    return "\n".join(
        [
            "# Warning Resolution Report",
            "",
            "| Warning | Classification | Owner | Revisit | Customer impact |",
            "| --- | --- | --- | --- | --- |",
            (
                "| Local backend health endpoints | Must pass when backend launchd service is running; warning is"
                " acceptable only before service is loaded. | Local operator | Each local daily run | Backend must be"
                " running before customer demo. |"
            ),
            (
                "| UN Comtrade API key missing | Accepted future enhancement for customs data; not a current POY/DTY h1"
                " blocker. | Data owner | When customs data is in scope | No current main-strategy impact. |"
            ),
            (
                f"| User files waiting channel: manual_or_blocked={source_after.get('manual_or_blocked', 0)} | Accepted"
                " semi-automatic input channel. | User / data operator | When customer sends files | Do not imply"
                " missing user files are already ingested. |"
            ),
            "",
        ]
    )


def render_backup_restore_drill_report(readiness: dict[str, Any]) -> str:
    backup = readiness.get("backup_restore", {})
    return "\n".join(
        [
            "# Backup Restore Drill Report",
            "",
            f"- Daily anchor created: {backup.get('backup_created', False)}"
            f" (reused: {backup.get('anchor_reused', False)})",
            f"- Daily anchor path: {backup.get('backup_path', '')}",
            f"- Integrity check: {backup.get('integrity_check', 'not_run')}",
            f"- Restore drill: {backup.get('restore_drill', '')}",
            f"- Drill integrity check: {backup.get('restore_integrity_check', 'not_run')}",
            "",
            (
                "The drill copies the daily anchor to a temporary probe, verifies integrity and core table"
                " counts, then deletes the probe; it never overwrites the main SQLite database. Restore"
                " remains a deliberate operator action."
            ),
            "",
        ]
    )


def render_monitoring_alert_report(readiness: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Local Monitoring Alert Report",
            "",
            "Alert generation command:",
            "",
            "```bash",
            "python3 server/scripts/check_local_production_alerts.py",
            "```",
            "",
            "Expected output:",
            "",
            "- `.codex-run/local-production/alerts/latest-alerts.json`",
            "- `.codex-run/local-production/alerts/latest-alerts.md`",
            "",
            f"- Current blockers: {len(readiness.get('blockers', []))}",
            f"- Current warnings: {len(readiness.get('warnings', []))}",
            "",
        ]
    )


def render_local_production_test_placeholder(readiness: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Local Production Test Report",
            "",
            "This file is overwritten or supplemented after running the full validation commands:",
            "",
            "```bash",
            "python3 -m py_compile server/scripts/run_local_daily.py",
            "python3 -m py_compile server/scripts/import_ccf_industry_observations.py",
            "python3 -m py_compile server/scripts/check_local_production_alerts.py",
            "npm run lint",
            "npm run assets:check",
            "npm run build",
            "python3 -m pytest server/tests",
            "npm run test:e2e",
            "npm audit --omit=dev",
            "npm run visual:diff",
            "```",
            "",
            f"- Daily automation status at report generation: {readiness['overall_status']}",
            "",
        ]
    )


def render_final_unattended_report(readiness: dict[str, Any]) -> str:  # noqa: F811
    decision = "NO-GO" if readiness["blockers"] else "GO WITH WARNINGS" if readiness["warnings"] else "GO"
    quality = readiness.get("quality_gate", {})
    gates = {gate.get("id"): gate for gate in quality.get("gates", []) if isinstance(gate, dict)}
    primary = gates.get("primary_strategy_effectiveness_gate", {})
    health = readiness.get("health", {})
    source = readiness.get("source_summary", {})
    ccf = source.get("ccf_import", {})
    industry = readiness.get("ccf_industry_import", {})
    backup = readiness.get("backup_restore", {})
    return "\n".join(
        [
            "# Final Local Unattended Production Report",
            "",
            f"## Decision: {decision}",
            "",
            f"- Status: {readiness['overall_status']}",
            f"- Blockers: {', '.join(readiness['blockers']) if readiness['blockers'] else 'none'}",
            f"- Warnings: {', '.join(readiness['warnings']) if readiness['warnings'] else 'none'}",
            "",
            "## CCF Automation",
            "",
            (
                f"- Price freshness: {gates.get('ccf_price_freshness', {}).get('status', 'missing')}"
                f" ({gates.get('ccf_price_freshness', {}).get('observed', 'unknown')})"
            ),
            (
                f"- Industry freshness: {gates.get('ccf_industry_freshness', {}).get('status', 'missing')}"
                f" ({gates.get('ccf_industry_freshness', {}).get('observed', 'unknown')})"
            ),
            f"- Gap tasks: {gates.get('coverage_gap_audit', {}).get('observed', 'unknown')}",
            f"- Latest CCF price rows stored: {ccf.get('stored_rows', 0)}",
            f"- Latest CCF industry rows stored: {industry.get('stored_rows', 0)}",
            f"- Latest source backup: {source.get('backup_path') or industry.get('backup_path') or 'not applicable'}",
            (
                "- Human handling needed: only if authorized page requests CAPTCHA/QR/SMS/2FA, permission, or export"
                " changes."
            ),
            "",
            "## Main Strategy",
            "",
            f"- Gate: {primary.get('status', 'missing')}",
            f"- Observed: {primary.get('observed', 'unknown')}",
            (
                "- Customer wording: can be used as short-cycle business research support only; it is not guaranteed"
                " model accuracy or automatic trading instruction."
            ),
            "",
            "## Local Service",
            "",
            f"- Backend health all ok: {health.get('all_ok', False) if health else 'not checked'}",
            "- Backend URL: http://127.0.0.1:8000",
            "- Frontend URL: http://127.0.0.1:5173",
            "- Supervisor templates: `.codex-run/local-production/launchd/`",
            "- Logs: `.codex-run/local-production/logs/`",
            "",
            "## Backup Restore",
            "",
            f"- Backup path: {backup.get('backup_path', '')}",
            f"- Integrity check: {backup.get('integrity_check', 'not_run')}",
            f"- Restore drill: {backup.get('restore_drill', '')}",
            "",
            "## Test Evidence",
            "",
            "- Latest test report path: `.codex-run/local-production/08-local-production-test-report.md`",
            "- Run the full validation suite after any code or automation change before accepting the local gate.",
            "",
            "## Cannot Fully Automate",
            "",
            "- CAPTCHA, QR login, SMS, and second-factor verification.",
            "- Vendor permission/export limit changes.",
            "- CCF page structure changes that require selector or parsing updates.",
            "- User files that have not been provided.",
            "- External API keys that are not configured.",
            "",
        ]
    )


def render_local_production_readme() -> str:  # noqa: F811
    return "\n".join(
        [
            "# Local Production README",
            "",
            (
                "This local production mode is for this machine only. It does not deploy a public domain, TLS, cloud"
                " server, or external hosting."
            ),
            "",
            "## Start Services",
            "",
            "Manual backend:",
            "",
            "```bash",
            "cd /path/to/project/server",
            "uvicorn app.main:app --host 127.0.0.1 --port 8000",
            "```",
            "",
            "Manual frontend:",
            "",
            "```bash",
            "cd /path/to/project",
            "npm run dev -- --host 127.0.0.1",
            "```",
            "",
            "## launchd Templates",
            "",
            (
                "Templates are generated under `.codex-run/local-production/launchd/`. Review them first, then install"
                " manually if desired:"
            ),
            "",
            "```bash",
            "mkdir -p ~/Library/LaunchAgents",
            "cp .codex-run/local-production/launchd/com.poydty.agent.backend.plist ~/Library/LaunchAgents/",
            "cp .codex-run/local-production/launchd/com.poydty.agent.frontend.plist ~/Library/LaunchAgents/",
            "cp .codex-run/local-production/launchd/com.poydty.agent.local-daily.plist ~/Library/LaunchAgents/",
            "launchctl load ~/Library/LaunchAgents/com.poydty.agent.backend.plist",
            "launchctl load ~/Library/LaunchAgents/com.poydty.agent.frontend.plist",
            "launchctl load ~/Library/LaunchAgents/com.poydty.agent.local-daily.plist",
            "```",
            "",
            "Unload:",
            "",
            "```bash",
            "launchctl unload ~/Library/LaunchAgents/com.poydty.agent.backend.plist",
            "launchctl unload ~/Library/LaunchAgents/com.poydty.agent.frontend.plist",
            "launchctl unload ~/Library/LaunchAgents/com.poydty.agent.local-daily.plist",
            "```",
            "",
            "## Daily Task",
            "",
            "```bash",
            "npm run local:daily -- --apply",
            "python3 server/scripts/check_local_production_alerts.py",
            "```",
            "",
            "## Check Today's Gate",
            "",
            "- Final gate: `.codex-run/local-production/10-final-local-production-gate.md`",
            "- Full report: `.codex-run/local-production/09-final-local-unattended-production-report.md`",
            "- Alerts: `.codex-run/local-production/alerts/latest-alerts.json`",
            "- CCF status: `.codex-run/local-production/04-ccf-production-automation-report.md`",
            "",
            (
                "If the decision is GO or GO WITH WARNINGS with accepted warnings only, the local system can be used"
                " for customer-facing research review. If it is NO-GO, handle the listed blocker first."
            ),
            "",
            "## CCF Human Intervention",
            "",
            (
                "If the authorized CCF page asks for CAPTCHA, QR login, SMS, 2FA, new permission, or export rights,"
                " pause automation and handle it manually. Do not bypass authorization controls."
            ),
            "",
            "## Restore Backup",
            "",
            "Never overwrite the main DB blindly. Copy a known-good backup to a temporary path first and run:",
            "",
            "```bash",
            "sqlite3 /path/to/backup.sqlite 'PRAGMA integrity_check;'",
            "```",
            "",
            "Then stop services, replace `server/data/agent.db` deliberately, and rerun `npm run local:daily`.",
            "",
        ]
    )


def render_test_placeholder(readiness: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Local Test Verification Report",
            "",
            (
                "This file is initialized by `npm run local:daily`. Manual test command outcomes should be appended"
                " after running:"
            ),
            "",
            "```bash",
            "npm run lint",
            "npm run assets:check",
            "npm run build",
            "npm run test:e2e",
            "python3 -m pytest server/tests",
            "npm audit --omit=dev",
            "```",
            "",
            f"- Daily automation status at initialization: {readiness['overall_status']}",
            "",
        ]
    )


def render_acceptance_report(readiness: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Local Acceptance Report",
            "",
            "Acceptance scope: local production-like unattended operation on this machine.",
            "",
            f"- Current gate: {readiness['overall_status']}",
            f"- Blockers: {', '.join(readiness['blockers']) if readiness['blockers'] else 'none'}",
            f"- Accepted warnings needed: {', '.join(readiness['warnings']) if readiness['warnings'] else 'none'}",
            "",
            (
                "Domain, TLS, remote hosting, and customer infrastructure deployment are intentionally out of scope for"
                " this local gate."
            ),
            "",
        ]
    )


def render_final_gate(readiness: dict[str, Any]) -> str:
    decision = "NO-GO" if readiness["blockers"] else "GO WITH WARNINGS" if readiness["warnings"] else "GO"
    return "\n".join(
        [
            "# Final Local Production Gate",
            "",
            f"- Decision: {decision}",
            f"- Status: {readiness['overall_status']}",
            "",
            "Blockers:",
            *(f"- {item}" for item in readiness["blockers"]),
            *(["- none"] if not readiness["blockers"] else []),
            "",
            "Warnings:",
            *(f"- {item}" for item in readiness["warnings"]),
            *(["- none"] if not readiness["warnings"] else []),
            "",
        ]
    )


def write_launchd_templates(output_dir: Path) -> None:
    launchd_dir = output_dir / "launchd"
    logs_dir = output_dir / "logs"
    launchd_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    uvicorn_bin = shutil.which("uvicorn") or "uvicorn"
    npm_bin = shutil.which("npm") or "npm"
    python_bin = sys.executable
    env_file = REPO_ROOT / ".env.local-production"
    local_env = (
        "set -eu; "
        f"if [ -f {env_file} ]; then set -a; . {env_file}; set +a; fi; "
        "export APP_ENV=${APP_ENV:-production}; "
        "export ENFORCE_INTERNAL_TOKEN=${ENFORCE_INTERNAL_TOKEN:-1}; "
        f"export SQLITE_PATH=${{SQLITE_PATH:-{DEFAULT_DB}}}; "
        "export NO_PROXY=127.0.0.1,localhost; export no_proxy=127.0.0.1,localhost"
    )
    templates = {
        "com.poydty.agent.backend.plist": launchd_plist(
            label="com.poydty.agent.backend",
            command=(
                f"{local_env}; cd {REPO_ROOT / 'server'} && {uvicorn_bin} app.main:app --host 127.0.0.1 --port 8000"
            ),
            stdout=logs_dir / "backend.out.log",
            stderr=logs_dir / "backend.err.log",
            keep_alive=True,
        ),
        "com.poydty.agent.frontend.plist": launchd_plist(
            label="com.poydty.agent.frontend",
            command=f"{local_env}; cd {REPO_ROOT} && {npm_bin} run build && {npm_bin} run preview -- --host 127.0.0.1",
            stdout=logs_dir / "frontend.out.log",
            stderr=logs_dir / "frontend.err.log",
            keep_alive=True,
        ),
        "com.poydty.agent.local-daily.plist": launchd_plist(
            label="com.poydty.agent.local-daily",
            command=(
                f"{local_env}; cd {REPO_ROOT} && {python_bin} server/scripts/run_local_daily.py --apply; {python_bin}"
                " server/scripts/check_local_production_alerts.py"
            ),
            stdout=logs_dir / "local-daily.out.log",
            stderr=logs_dir / "local-daily.err.log",
            keep_alive=False,
            calendar={"Hour": 18, "Minute": 0},
        ),
        "com.poydty.agent.news-scheduler.plist": launchd_plist(
            label="com.poydty.agent.news-scheduler",
            command=(
                "export NO_PROXY=127.0.0.1,localhost; export no_proxy=127.0.0.1,localhost; "
                f"export SQLITE_PATH={DEFAULT_DB}; cd {REPO_ROOT} && {python_bin} server/scripts/run_news_scheduler.py "
                f"--db {DEFAULT_DB} --codex-run {DEFAULT_CODEX_RUN} "
                f"--output-dir {output_dir / 'news-automation'}"
            ),
            stdout=logs_dir / "news-scheduler.out.log",
            stderr=logs_dir / "news-scheduler.err.log",
            keep_alive=True,
        ),
    }
    for name, content in templates.items():
        (launchd_dir / name).write_text(content, encoding="utf-8")


def launchd_plist(
    *,
    label: str,
    command: str,
    stdout: Path,
    stderr: Path,
    keep_alive: bool,
    calendar: dict[str, int] | None = None,
) -> str:
    calendar_xml = ""
    if calendar:
        calendar_xml = textwrap.dedent(f"""
            <key>StartCalendarInterval</key>
            <dict>
              <key>Hour</key><integer>{calendar["Hour"]}</integer>
              <key>Minute</key><integer>{calendar["Minute"]}</integer>
            </dict>
            """).strip()
    else:
        calendar_xml = "<key>RunAtLoad</key><true/>"
    keep_alive_xml = "<true/>" if keep_alive else "<false/>"
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
        <plist version="1.0">
        <dict>
          <key>Label</key><string>{label}</string>
          <key>ProgramArguments</key>
          <array>
            <string>/bin/zsh</string>
            <string>-lc</string>
            <string>{escape_plist(command)}</string>
          </array>
          {calendar_xml}
          <key>KeepAlive</key>{keep_alive_xml}
          <key>WorkingDirectory</key><string>{REPO_ROOT}</string>
          <key>StandardOutPath</key><string>{stdout}</string>
          <key>StandardErrorPath</key><string>{stderr}</string>
        </dict>
        </plist>
        """)


def escape_plist(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)).fetchone()
    return row is not None


def query_rows(connection: sqlite3.Connection, sql: str) -> list[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(sql).fetchall()]


def redact_command(command: list[str]) -> list[str]:
    redacted: list[str] = []
    skip_next = False
    sensitive_flags = {"--token", "--api-key", "--password"}
    for item in command:
        if skip_next:
            redacted.append("[REDACTED]")
            skip_next = False
            continue
        redacted.append(item)
        if item in sensitive_flags:
            skip_next = True
    return redacted


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


# Current report projections intentionally exclude the soft-removed CCF
# channel. Older rendering helpers remain above to read historical artifacts,
# but these final definitions are the active output contract.
def render_source_report(readiness: dict[str, Any]) -> str:
    source = readiness.get("source_summary", {})
    quality = readiness.get("quality_gate", {})
    return "\n".join(
        [
            "# Current Public Source Automation",
            "",
            f"- Status: {source.get('status', 'missing')}",
            f"- public-benchmark.v2 quality: {quality.get('overall_status', 'missing')}",
            f"- Public fetch status: {source.get('public_fetch', {}).get('status', 'not_run')}",
            "- CCF: soft-removed; historical rows remain read-only and no task is scheduled.",
        ]
    )


def render_daily_report(readiness: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Local Daily Automation Report",
            "",
            f"- Mode: {readiness.get('mode', 'unknown')}",
            f"- Overall status: {readiness.get('overall_status', 'unknown')}",
            f"- Blockers: {len(readiness.get('blockers', []))}",
            f"- Warnings: {len(readiness.get('warnings', []))}",
            "- Active data contract: public-benchmark.v2 (9 public inputs, 2 derived targets).",
            "- Same-day reruns use idempotent storage paths and never enqueue CCF work.",
        ]
    )


def render_hardening_audit(readiness: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Local Production Hardening Audit",
            "",
            f"- Overall status: {readiness.get('overall_status', 'unknown')}",
            "- Public-source qualification is based on provenance, per-series cadence, units, and time correctness.",
            "- Permission, license, authorization, and manifest approval fields do not gate the current contract.",
            "- CCF is soft-removed from capture, imports, schedules, alerts, freshness, and readiness.",
        ]
    )


def render_scheduler_report() -> str:
    return "\n".join(
        [
            "# Production Scheduler Matrix",
            "",
            "| Component | State | Reason |",
            "| --- | --- | --- |",
            "| Backend | enabled | API and current schedulers |",
            "| Frontend | enabled | public workbench |",
            "| Intraday public prices | enabled | 300-second collection |",
            "| Public source automation | enabled | due-source collection and quality |",
            "| Daily orchestration | enabled | catch-up capable |",
            "| Morning brief | enabled | catch-up capable |",
            "| Experience settlement | disabled | not accepted for unattended production |",
            "| Event summary worker | disabled | not accepted for unattended production |",
            "| CCF | soft-removed | no future operational entrypoint |",
        ]
    )


def render_warning_resolution_report(readiness: dict[str, Any]) -> str:
    warnings = readiness.get("warnings", []) if isinstance(readiness.get("warnings"), list) else []
    blockers = readiness.get("blockers", []) if isinstance(readiness.get("blockers"), list) else []
    return "\n".join(
        [
            "# Warning Resolution Report",
            "",
            f"- Blockers: {blockers or 'none'}",
            f"- Warnings: {warnings or 'none'}",
            "- Remote push remains disabled until operator credentials exist; local/provider-neutral alerting is used.",
        ]
    )


def render_final_unattended_report(readiness: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Final Local Unattended Production Report",
            "",
            f"- Status: {readiness.get('overall_status', 'unknown')}",
            f"- Finished at: {readiness.get('finished_at', 'unknown')}",
            "- Contract: public-benchmark.v2",
            "- CCF: soft-removed; historical data unchanged.",
            "- Delivery requires the first successful production daily run and its same-day artifacts.",
        ]
    )


def render_local_production_readme() -> str:
    return "\n".join(
        [
            "# Local Production Operations",
            "",
            "The current unattended path collects public sources, evaluates public-benchmark.v2,",
            "runs the daily Agent pipeline,",
            "writes a snapshot and morning brief, and emits provider-neutral local alerts.",
            "",
            "CCF is soft-removed: historical rows are read-only and no capture, import, queue, schedule,",
            "alert, freshness, or readiness path remains active.",
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())

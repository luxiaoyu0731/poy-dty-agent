from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = Path(os.getenv("SQLITE_PATH", "/data/agent.db"))
DEFAULT_OUTPUT_DIR = Path(os.getenv("LOCAL_PRODUCTION_OUTPUT_DIR", "/data/local-production"))
DEFAULT_CODEX_RUN = Path(os.getenv("CODEX_RUN_DIR", "/data/codex-run"))
DAILY_TIMEOUT_SECONDS = int(os.getenv("PRODUCTION_SCHEDULER_DAILY_TIMEOUT_SECONDS", "1800"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the production daily automation loop for Docker/cloud hosts.")
    parser.add_argument(
        "--interval-seconds", type=int, default=int(os.getenv("PRODUCTION_SCHEDULER_INTERVAL_SECONDS", "86400"))
    )
    parser.add_argument(
        "--initial-delay-seconds", type=int, default=int(os.getenv("PRODUCTION_SCHEDULER_INITIAL_DELAY_SECONDS", "30"))
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--skip-health", action="store_true", default=os.getenv("PRODUCTION_SCHEDULER_SKIP_HEALTH", "0") == "1"
    )
    parser.add_argument("--health-base-url", default=os.getenv("PRODUCTION_HEALTH_BASE_URL", "http://backend:8000"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--codex-run", type=Path, default=DEFAULT_CODEX_RUN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.initial_delay_seconds > 0 and not args.once:
        time.sleep(args.initial_delay_seconds)
    while True:
        summary = run_once(args)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        if args.once:
            return 0 if summary["status"] in {"ready", "ready_with_warnings"} else 1
        time.sleep(max(args.interval_seconds, 60))


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    daily = [
        sys.executable,
        str(REPO_ROOT / "server" / "scripts" / "run_local_daily.py"),
        "--apply",
        "--db",
        str(args.db),
        "--codex-run",
        str(args.codex_run),
        "--output-dir",
        str(args.output_dir),
        "--health-base-url",
        args.health_base_url,
    ]
    if args.skip_health:
        daily.append("--skip-health")
    alert_env = os.environ.copy()
    alert_env["LOCAL_PRODUCTION_STATUS_PATH"] = str(args.output_dir / "latest-status.json")
    alert_env["LOCAL_PRODUCTION_ALERT_OUTPUT_DIR"] = str(args.output_dir / "alerts")
    daily_completed = run_command(daily, timeout_seconds=DAILY_TIMEOUT_SECONDS)
    alerts_completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "server" / "scripts" / "check_local_production_alerts.py")],
        cwd=REPO_ROOT,
        env=alert_env,
        text=True,
        capture_output=True,
        check=False,
    )
    status = read_status(args.output_dir / "latest-status.json")
    reported_status = str(status.get("overall_status", "missing"))
    report_finished_at = parse_timestamp(status.get("finished_at"))
    current_started_at = parse_timestamp(started_at)
    report_is_current = bool(report_finished_at and current_started_at and report_finished_at >= current_started_at)
    if daily_completed.returncode != 0 and reported_status in {"ready", "ready_with_warnings"}:
        reported_status = "failed"
    if not report_is_current:
        reported_status = "failed"
    evaluation_summary = (
        status.get("seven_product_oos_evaluation", {})
        if isinstance(status.get("seven_product_oos_evaluation"), dict)
        else {}
    )
    formal_prediction_status = (
        "passed"
        if evaluation_summary.get("exit_code") == 0
        and evaluation_summary.get("status") == "passed"
        and evaluation_summary.get("passed_count") == 21
        and evaluation_summary.get("fresh_output") is True
        and evaluation_summary.get("evidence_valid") is True
        and evaluation_summary.get("forecast_source") == "issued_ledger"
        and evaluation_summary.get("database_unchanged_during_run") is True
        and evaluation_summary.get("cutoff_matches_forecast") is True
        else "blocked"
    )
    return {
        "schema_version": "production_scheduler_run.v1",
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "status": reported_status,
        "report_is_current": report_is_current,
        "daily_exit_code": daily_completed.returncode,
        "formal_prediction_status": formal_prediction_status,
        "formal_prediction_passed_count": int(evaluation_summary.get("passed_count") or 0),
        "formal_prediction_evaluation_exit_code": evaluation_summary.get("exit_code"),
        "formal_prediction_evidence_path": str(evaluation_summary.get("latest_path") or ""),
        "alerts_exit_code": alerts_completed.returncode,
        "daily_stdout_tail": daily_completed.stdout[-1000:],
        "daily_stderr_tail": daily_completed.stderr[-1000:],
        "formal_prediction_evaluation_stdout_tail": "",
        "formal_prediction_evaluation_stderr_tail": str(evaluation_summary.get("stderr_tail") or "")[-1000:],
        "alerts_stdout_tail": alerts_completed.stdout[-1000:],
        "alerts_stderr_tail": alerts_completed.stderr[-1000:],
    }


def run_command(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
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


def read_status(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def parse_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


if __name__ == "__main__":
    raise SystemExit(main())

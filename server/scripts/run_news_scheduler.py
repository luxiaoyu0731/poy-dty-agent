from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
DEFAULT_DB = Path(os.getenv("SQLITE_PATH", "/data/agent.db"))
DEFAULT_CODEX_RUN = Path(os.getenv("CODEX_RUN_DIR", "/data/codex-run"))
DEFAULT_OUTPUT_DIR = Path(os.getenv("NEWS_AUTOMATION_OUTPUT_DIR", "/data/news-automation"))

if str(REPO_ROOT / "server" / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "server" / "scripts"))
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from check_local_production_alerts import send_notification  # noqa: E402

from app.scheduler_observability_state import (  # noqa: E402
    build_scheduler_observability,
    news_scheduler_backlog,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run isolated production news acquisition on a short cadence.")
    parser.add_argument(
        "--interval-seconds", type=int, default=int(os.getenv("NEWS_SCHEDULER_INTERVAL_SECONDS", "1800"))
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--codex-run", type=Path, default=DEFAULT_CODEX_RUN)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--source-timeout-seconds",
        type=float,
        default=float(os.getenv("NEWS_SCHEDULER_SOURCE_TIMEOUT_SECONDS", "40")),
    )
    parser.add_argument("--limit-per-source", type=int, default=3)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    while True:
        cycle_started = time.monotonic()
        result = run_once(args)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if args.once:
            return 1 if result["status"] == "blocked" else 0
        time.sleep(seconds_until_next_cycle(cycle_started, time.monotonic(), args.interval_seconds))


def seconds_until_next_cycle(started: float, finished: float, interval_seconds: int) -> float:
    """Keep the scheduler on a fixed start-to-start cadence."""

    elapsed = max(0.0, finished - started)
    return max(0.0, max(60, interval_seconds) - elapsed)


def refresh_live_intelligence(args: argparse.Namespace) -> dict[str, Any]:
    output = args.output_dir / "live-intelligence.json"
    # Keep the outer subprocess budget ahead of the projection deadline so a
    # legitimately longer drain cycle is not killed as projection_timeout.
    projection_deadline = max(20, int(os.getenv("LIVE_INTELLIGENCE_PROJECTION_DEADLINE_SECONDS", "20")))
    try:
        result = subprocess.run(
            [sys.executable, str(SERVER_ROOT / "scripts" / "refresh_live_intelligence.py"),
             "--db", str(args.db), "--output", str(output)],
            cwd=REPO_ROOT, text=True, capture_output=True, check=False,
            timeout=projection_deadline + 60,
        )
        if result.returncode:
            return {"status": "failed", "exit_code": result.returncode,
                    "error": str(result.stderr)[-500:]}
        return read_json(output) or {"status": "failed", "error": "missing_projection_report"}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "projection_timeout"}


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    started_at = datetime.now(UTC).isoformat()
    latest_path = args.output_dir / "latest-scheduler.json"
    previous_scheduler = read_json(latest_path)
    command = [
        sys.executable,
        str(REPO_ROOT / "server" / "scripts" / "run_source_automation.py"),
        "--db",
        str(args.db),
        "--codex-run",
        str(args.codex_run),
        "--output-dir",
        str(args.output_dir),
        "--apply",
        "--fetch-public",
        "--public-deadline-seconds",
        "600",
        "--skip-public-benchmark-refresh",
        "--fetch-news",
        "--news-limit-per-source",
        str(max(1, args.limit_per_source)),
        "--news-cursor-pages",
        "1",
        "--news-source-timeout-seconds",
        str(max(1.0, args.source_timeout_seconds)),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=max(900, int(args.source_timeout_seconds * 50)),
        )
    except subprocess.TimeoutExpired as exc:
        completed = subprocess.CompletedProcess(command, 124, exc.stdout or "", f"scheduler_timeout: {exc}")
    summary = read_json(args.output_dir / "source-automation-latest.json")
    status = str(summary.get("status") or "blocked")
    if completed.returncode == 75:
        status = "locked"
    elif completed.returncode != 0 and status == "completed":
        status = "blocked"
    live_projection = (refresh_live_intelligence(args) if completed.returncode == 0
                       else {"status": "skipped", "reason": "collection_not_completed"})
    if live_projection.get("status") == "failed" and status == "completed":
        status = "degraded"
    result = {
        "schema_version": "news_scheduler_run.v1",
        "live_intelligence": live_projection,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "status": status,
        "exit_code": completed.returncode,
        "public_fetch": summary.get("public_fetch", {}),
        "news_fetch": summary.get("news_fetch", {}),
        "stdout_tail": str(completed.stdout)[-1000:],
        "stderr_tail": str(completed.stderr)[-1000:],
    }
    result["scheduler_observability"] = build_scheduler_observability(
        scheduler="news_scheduler",
        status=status,
        started_at=started_at,
        finished_at=result["finished_at"],
        backlog=news_scheduler_backlog(result),
        previous_payload=previous_scheduler,
    )
    result["notification"] = notify_if_needed(result, args.output_dir)
    write_json(latest_path, result)
    return result


def notify_if_needed(result: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    if result["status"] not in {"degraded", "blocked"}:
        return {"status": "not_needed", "configured": False}
    news = result.get("news_fetch", {}) if isinstance(result.get("news_fetch"), dict) else {}
    public = result.get("public_fetch", {}) if isinstance(result.get("public_fetch"), dict) else {}
    errors = [str(item) for item in [*public.get("errors", []), *news.get("errors", [])][:10]]
    fingerprint = hashlib.sha256("\n".join(sorted(errors)).encode("utf-8")).hexdigest()
    state_path = output_dir / "notification-state.json"
    previous = read_json(state_path)
    if previous.get("fingerprint") == fingerprint:
        return {"status": "deduplicated", "configured": bool(previous.get("configured"))}
    severity = "critical" if result["status"] == "blocked" else "warning"
    payload = {
        "generated_at": result["finished_at"],
        "alert_count": max(1, len(errors)),
        "highest_severity": severity,
        "alerts": (
            [
                {
                    "severity": severity,
                    "code": "SOURCE_AUTOMATION_FAILURE",
                    "title": "短周期公开来源自动更新异常",
                    "detail": error,
                    "next_step": "系统将按下一周期自动重试；持续失败时检查来源可用性。",
                }
                for error in errors
            ]
            or [
                {
                    "severity": severity,
                    "code": "SOURCE_AUTOMATION_RUN_FAILED",
                    "title": "短周期公开来源更新失败",
                    "detail": result["status"],
                    "next_step": "检查新闻调度日志。",
                }
            ]
        ),
    }
    notification = send_notification(payload)
    write_json(
        state_path,
        {
            "fingerprint": fingerprint,
            "configured": notification.get("configured", False),
            "sent_at": result["finished_at"],
        },
    )
    return notification


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "run_production_scheduler.py"
SPEC = importlib.util.spec_from_file_location("run_production_scheduler", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
run_production_scheduler = importlib.util.module_from_spec(SPEC)
sys.modules["run_production_scheduler"] = run_production_scheduler
SPEC.loader.exec_module(run_production_scheduler)


def test_scheduler_rejects_stale_ready_report_after_timeout(tmp_path: Path, monkeypatch) -> None:
    status_path = tmp_path / "latest-status.json"
    status_path.write_text(
        json.dumps(
            {
                "overall_status": "ready",
                "finished_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        run_production_scheduler,
        "run_command",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 124, "", "timeout"),
    )
    monkeypatch.setattr(
        run_production_scheduler.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 1, "", ""),
    )
    args = argparse.Namespace(
        db=tmp_path / "agent.db",
        codex_run=tmp_path / "codex-run",
        output_dir=tmp_path,
        health_base_url="http://backend:8000",
        skip_health=True,
    )

    result = run_production_scheduler.run_once(args)

    assert result["status"] == "failed"
    assert result["report_is_current"] is False
    assert result["formal_prediction_status"] == "blocked"


def test_scheduler_consumes_daily_oos_evidence_without_duplicate_evaluation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    status_path = tmp_path / "latest-status.json"
    status_path.write_text(
        json.dumps(
            {
                "overall_status": "ready",
                "finished_at": (datetime.now(UTC) + timedelta(seconds=1)).isoformat(),
                "seven_product_oos_evaluation": {
                    "status": "blocked",
                    "exit_code": 2,
                    "fresh_output": True,
                    "evidence_valid": True,
                    "forecast_source": "issued_ledger",
                    "database_unchanged_during_run": True,
                    "cutoff_matches_forecast": True,
                    "passed_count": 0,
                    "latest_path": str(tmp_path / "seven-product-evaluation-latest.json"),
                },
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def _run(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "ready", "")

    monkeypatch.setattr(run_production_scheduler, "run_command", _run)
    monkeypatch.setattr(
        run_production_scheduler.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )
    args = argparse.Namespace(
        db=tmp_path / "agent.db",
        codex_run=tmp_path / "codex-run",
        output_dir=tmp_path,
        health_base_url="http://backend:8000",
        skip_health=True,
    )

    result = run_production_scheduler.run_once(args)

    assert len(commands) == 1
    assert commands[0][1].endswith("run_local_daily.py")
    assert result["status"] == "ready"
    assert result["formal_prediction_status"] == "blocked"
    assert result["formal_prediction_passed_count"] == 0
    assert result["formal_prediction_evaluation_exit_code"] == 2
    assert result["formal_prediction_evidence_path"].endswith("seven-product-evaluation-latest.json")

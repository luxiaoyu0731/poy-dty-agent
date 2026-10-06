from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_news_scheduler.py"
SPEC = importlib.util.spec_from_file_location("run_news_scheduler", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
run_news_scheduler = importlib.util.module_from_spec(SPEC)
sys.modules["run_news_scheduler"] = run_news_scheduler
SPEC.loader.exec_module(run_news_scheduler)


def test_scheduler_hydrates_article_details_before_summary_generation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: list[str] = []

    def fake_run(command, **_kwargs):
        captured.extend(str(part) for part in command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(run_news_scheduler.subprocess, "run", fake_run)
    monkeypatch.setattr(
        run_news_scheduler,
        "read_json",
        lambda _: {"status": "completed", "news_fetch": {}},
    )
    args = argparse.Namespace(
        db=tmp_path / "agent.db",
        codex_run=tmp_path / "codex-run",
        output_dir=tmp_path,
        source_timeout_seconds=20.0,
        limit_per_source=3,
    )

    monkeypatch.setattr(run_news_scheduler, "refresh_live_intelligence", lambda _: {"status": "completed"})
    result = run_news_scheduler.run_once(args)

    assert result["status"] == "completed"
    assert "--fetch-public" in captured
    assert captured[captured.index("--public-deadline-seconds") + 1] == "600"
    assert "--skip-public-benchmark-refresh" in captured
    assert "--fetch-news" in captured
    assert "--news-skip-details" not in captured
    assert captured[captured.index("--news-limit-per-source") + 1] == "3"
    assert result["scheduler_observability"]["scheduler"] == "news_scheduler"
    assert result["scheduler_observability"]["status"] == "completed"
    assert result["scheduler_observability"]["backlog"] == 0
    assert result["scheduler_observability"]["last_success_at"] == result["finished_at"]


def test_degraded_news_attempt_preserves_previous_success_and_counts_backlog(tmp_path: Path, monkeypatch) -> None:
    previous = {
        "scheduler_observability": {
            "schema_version": "scheduler_observability.v1",
            "scheduler": "news_scheduler",
            "last_attempt_at": "2026-09-01T00:30:00+00:00",
            "last_success_at": "2026-09-01T00:30:00+00:00",
            "status": "completed",
            "duration_seconds": 30,
            "backlog": 0,
            "failure_counts": {"blocked": 1},
        }
    }
    latest = tmp_path / "latest-scheduler.json"
    latest.write_text(json.dumps(previous), encoding="utf-8")

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, "", "")

    summary = {
        "status": "degraded",
        "public_fetch": {"errors": ["public"]},
        "news_fetch": {
            "errors": ["news-1", "news-2"],
            "run_status_counts": {"error": 1, "timeout": 1},
            "summaries_failed": 1,
        },
    }
    original_read_json = run_news_scheduler.read_json
    monkeypatch.setattr(run_news_scheduler.subprocess, "run", fake_run)
    monkeypatch.setattr(
        run_news_scheduler,
        "read_json",
        lambda path: summary if path.name == "source-automation-latest.json" else original_read_json(path),
    )
    monkeypatch.setattr(run_news_scheduler, "notify_if_needed", lambda *_args, **_kwargs: {})
    args = argparse.Namespace(
        db=tmp_path / "agent.db",
        codex_run=tmp_path / "codex-run",
        output_dir=tmp_path,
        source_timeout_seconds=20.0,
        limit_per_source=3,
    )

    monkeypatch.setattr(run_news_scheduler, "refresh_live_intelligence", lambda _: {"status": "completed"})
    result = run_news_scheduler.run_once(args)

    state = result["scheduler_observability"]
    assert state["status"] == "degraded"
    assert state["last_success_at"] == "2026-09-01T00:30:00+00:00"
    assert state["backlog"] == 4
    assert state["failure_counts"] == {"blocked": 1}


def test_live_projection_timeout_is_recorded_not_reported_as_success(tmp_path, monkeypatch):
    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs['timeout'])
    monkeypatch.setattr(run_news_scheduler.subprocess, 'run', timeout)
    args = argparse.Namespace(output_dir=tmp_path, db=tmp_path/'test.db')
    result = run_news_scheduler.refresh_live_intelligence(args)
    assert result == {'status': 'failed', 'error': 'projection_timeout'}


def test_scheduler_runs_zero_copy_without_pre_write_backup(tmp_path: Path, monkeypatch) -> None:
    """Final backup state (DISK-MODEL §7, 2026-09-17): the 30-minute scheduler no
    longer asks run_source_automation for a pre-apply full copy at all -- db-backups/
    stays empty on news rounds; recovery is covered by the unified daily anchor,
    SQLite transactions, and re-fetchable news/price data."""
    captured: list[str] = []

    def fake_run(command, **_kwargs):
        captured.extend(str(part) for part in command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(run_news_scheduler.subprocess, "run", fake_run)
    monkeypatch.setattr(
        run_news_scheduler,
        "read_json",
        lambda _: {"status": "completed", "news_fetch": {}},
    )
    args = argparse.Namespace(
        db=tmp_path / "agent.db",
        codex_run=tmp_path / "codex-run",
        output_dir=tmp_path,
        source_timeout_seconds=20.0,
        limit_per_source=3,
    )
    monkeypatch.setattr(run_news_scheduler, "refresh_live_intelligence", lambda _: {"status": "completed"})
    monkeypatch.setattr(run_news_scheduler, "notify_if_needed", lambda result, output_dir: {"status": "not_needed"})

    run_news_scheduler.run_once(args)

    assert "--apply" in captured
    assert "--backup-db" not in captured
    assert "--backup-reuse-seconds" not in captured
    assert "--backup-timeout-seconds" not in captured

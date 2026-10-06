from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import agent_governance_scheduler, storage
from app.agent_governance_scheduler import (
    _next_daily_run,
    latest_daily_governance_report,
    run_daily_governance_window,
    run_late_start_governance_catchup,
)
from app.agent_production_policy import (
    LOW_CONFIDENCE_CAP,
    PRODUCTION_READINESS_POLICY,
    daily_governance_report,
    daily_governance_window,
)
from app.settings import settings


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Path:
    original = settings.sqlite_path
    path = tmp_path / "agent-governance.db"
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.discard(path)
    try:
        yield path
    finally:
        storage._MIGRATED_PATHS.discard(path)
        object.__setattr__(settings, "sqlite_path", original)


def _aggregate(*, ready: bool, window: dict[str, str]) -> dict:
    return {
        "window": window,
        "policy_applied": True,
        "production_ready": ready,
        "failed_policy_checks": [] if ready else ["sample_count"],
    }


def test_daily_governance_loop_survives_round_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """One failing round must not kill the scheduler; it retries soon and keeps going."""

    events: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds:.0f}")
        if len(events) >= 4:
            raise asyncio.CancelledError()

    def failing_window(*, now: datetime) -> dict:
        events.append("window")
        raise RuntimeError("injected round failure")

    monkeypatch.setattr(agent_governance_scheduler.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(agent_governance_scheduler, "run_daily_governance_window", failing_window)
    monkeypatch.setattr(
        agent_governance_scheduler, "run_late_start_governance_catchup", lambda *, now: None
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(agent_governance_scheduler._daily_governance_loop())

    assert "window" in events
    assert "sleep:300" in events


def test_daily_window_is_a_completed_beijing_day() -> None:
    before_cutoff = datetime(2026, 8, 6, 0, 9, tzinfo=UTC)
    exact_cutoff = datetime(2026, 8, 6, 0, 10, tzinfo=UTC)
    after_cutoff = datetime(2026, 8, 6, 0, 10, 1, tzinfo=UTC)

    assert daily_governance_window(before_cutoff) == {
        "start": "2026-08-04T08:10:00+08:00",
        "end": "2026-08-05T08:10:00+08:00",
    }
    assert daily_governance_window(after_cutoff) == {
        "start": "2026-08-05T08:10:00+08:00",
        "end": "2026-08-06T08:10:00+08:00",
    }
    assert _next_daily_run(before_cutoff).isoformat() == "2026-08-06T08:10:00+08:00"
    assert _next_daily_run(exact_cutoff).isoformat() == "2026-08-06T08:10:00+08:00"
    assert _next_daily_run(after_cutoff).isoformat() == "2026-08-07T08:10:00+08:00"


def test_non_ready_window_is_low_confidence_without_manual_review() -> None:
    evaluated_at = datetime(2026, 8, 6, 8, 10, tzinfo=UTC)
    report = daily_governance_report(
        _aggregate(ready=False, window=daily_governance_window(evaluated_at)),
        evaluated_at=evaluated_at,
    )

    assert report["delivery_mode"] == "low_confidence"
    assert report["confidence_cap"] == LOW_CONFIDENCE_CAP
    assert report["human_review_required"] is False
    assert report["failed_policy_checks"] == ["sample_count"]


def test_ready_window_is_standard_without_manual_review() -> None:
    evaluated_at = datetime(2026, 8, 6, 8, 10, tzinfo=UTC)
    report = daily_governance_report(
        _aggregate(ready=True, window=daily_governance_window(evaluated_at)),
        evaluated_at=evaluated_at,
    )

    assert report["delivery_mode"] == "standard"
    assert report["confidence_cap"] is None
    assert report["human_review_required"] is False


def test_scheduler_uses_only_the_frozen_policy_and_fails_to_low_confidence(isolated_db: Path) -> None:
    evaluated_at = datetime(2026, 8, 6, 8, 10, tzinfo=UTC)
    captured: dict[str, object] = {}

    def collector(**kwargs: object) -> dict:
        captured.update(kwargs)
        return _aggregate(ready=True, window=daily_governance_window(evaluated_at))

    report = run_daily_governance_window(now=evaluated_at, collector=collector)

    assert captured["policy"] == PRODUCTION_READINESS_POLICY
    assert report["production_ready"] is True

    report = run_daily_governance_window(
        now=evaluated_at,
        collector=lambda **_: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )
    assert report["production_ready"] is False
    assert report["delivery_mode"] == "low_confidence"
    assert report["human_review_required"] is False


def test_daily_report_is_persisted_idempotently_and_survives_cache_loss(isolated_db: Path) -> None:
    evaluated_at = datetime(2026, 8, 6, 8, 10, tzinfo=UTC)
    report = run_daily_governance_window(
        now=evaluated_at,
        collector=lambda **_: _aggregate(ready=True, window=daily_governance_window(evaluated_at)),
    )
    replay = run_daily_governance_window(
        now=evaluated_at,
        collector=lambda **_: _aggregate(ready=True, window=daily_governance_window(evaluated_at)),
    )
    assert replay == report

    agent_governance_scheduler._latest_report = None
    assert latest_daily_governance_report() == report
    with (
        closing(storage.connect()) as connection,
        connection,
        pytest.raises(Exception, match="agent_governance_report_immutable"),
    ):
        connection.execute("UPDATE agent_governance_reports SET created_at='forged'")


def test_changed_report_for_one_window_fails_closed(isolated_db: Path) -> None:
    evaluated_at = datetime(2026, 8, 6, 8, 10, tzinfo=UTC)
    window = daily_governance_window(evaluated_at)
    ready_report = daily_governance_report(_aggregate(ready=True, window=window), evaluated_at=evaluated_at)
    storage.save_agent_governance_report(ready_report)
    with pytest.raises(ValueError, match="agent_governance_window_conflict"):
        blocked_report = daily_governance_report(_aggregate(ready=False, window=window), evaluated_at=evaluated_at)
        storage.save_agent_governance_report(blocked_report)


def test_late_start_persists_one_low_confidence_report_without_running_collector(isolated_db: Path) -> None:
    late_start = datetime(2026, 8, 6, 0, 15, tzinfo=UTC)

    report = run_late_start_governance_catchup(now=late_start)
    replay = run_late_start_governance_catchup(now=late_start)

    assert report is not None
    assert replay == report
    assert report["window"] == {
        "start": "2026-08-05T08:10:00+08:00",
        "end": "2026-08-06T08:10:00+08:00",
    }
    assert report["production_ready"] is False
    assert report["delivery_mode"] == "low_confidence"
    assert report["human_review_required"] is False
    assert report["failed_policy_checks"] == ["window_collection_unavailable"]


def test_late_start_never_overwrites_a_normal_report_for_the_same_window(isolated_db: Path) -> None:
    normal_at_cutoff = datetime(2026, 8, 6, 0, 10, tzinfo=UTC)
    normal = run_daily_governance_window(
        now=normal_at_cutoff,
        collector=lambda **_: _aggregate(ready=True, window=daily_governance_window(normal_at_cutoff)),
    )

    late = run_late_start_governance_catchup(now=datetime(2026, 8, 6, 0, 15, tzinfo=UTC))

    assert late == normal


def test_two_processes_persist_one_identical_daily_governance_report(isolated_db: Path, tmp_path: Path) -> None:
    """Separate scheduler processes must converge on one immutable daily report."""

    evaluated_at = "2026-08-06T08:10:00+08:00"
    worker = """
import json
from datetime import datetime

from app.agent_governance_scheduler import run_daily_governance_window

now = datetime.fromisoformat('2026-08-06T00:10:00+00:00')
report = run_daily_governance_window(
    now=now,
    collector=lambda **kwargs: {
        'window': {'start': kwargs['window_start'], 'end': kwargs['window_end']},
        'policy_applied': True,
        'production_ready': False,
        'failed_policy_checks': ['sample_count'],
    },
)
print(json.dumps(report, ensure_ascii=False, sort_keys=True))
"""
    with closing(storage.connect()) as _created_connection, _created_connection:
        pass
    environment = {
        **os.environ,
        "DG01_TEST_DB_ROOT": str(tmp_path),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "SQLITE_PATH": str(isolated_db),
        "TMPDIR": str(tmp_path),
    }
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    outputs = [process.communicate(timeout=20) for process in processes]
    assert [process.returncode for process in processes] == [0, 0]
    assert all(stderr == "" for _, stderr in outputs)
    reports = [json.loads(stdout) for stdout, _ in outputs]
    assert reports[0] == reports[1]
    assert reports[0]["evaluated_at"] == evaluated_at

    with closing(storage.connect()) as connection, connection:
        rows = connection.execute(
            "SELECT report FROM agent_governance_reports "
            "WHERE window_start=? AND window_end=?",
            ("2026-08-05T08:10:00+08:00", "2026-08-06T08:10:00+08:00"),
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0]["report"]) == reports[0]


def test_policy_rejects_naive_time_and_inconsistent_aggregate() -> None:
    with pytest.raises(ValueError, match="requires_timezone"):
        daily_governance_window(datetime(2026, 8, 6, 8, 10))

    evaluated_at = datetime(2026, 8, 6, 8, 10, tzinfo=UTC)
    invalid = _aggregate(ready=True, window=daily_governance_window(evaluated_at))
    invalid["failed_policy_checks"] = ["sample_count"]
    with pytest.raises(ValueError, match="readiness_inconsistent"):
        daily_governance_report(invalid, evaluated_at=evaluated_at)

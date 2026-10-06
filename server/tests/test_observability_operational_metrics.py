from __future__ import annotations

import json
from pathlib import Path

from app import observability


def test_source_freshness_and_scheduler_metrics_are_exported(monkeypatch) -> None:
    monkeypatch.setattr(observability, "SOURCE_FRESHNESS_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_SUCCESS_UNIX_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_DURATION_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_BACKLOG", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_STATUS", {})
    monkeypatch.setattr(observability, "SCHEDULER_FAILURE_COUNTER", observability.Counter())

    observability.observe_source_freshness(source_id="official_feed", age_seconds=42.5)
    observability.observe_scheduler(
        scheduler="news",
        status="completed",
        last_attempt_unix_seconds=1_700_000_001,
        last_success_unix_seconds=1_700_000_000,
        duration_seconds=12.25,
        backlog=3,
    )

    output = observability.metrics_text()

    assert 'poy_dty_source_freshness_seconds{source_id="official_feed"} 42.5' in output
    assert 'poy_dty_scheduler_last_success_unixtime{scheduler="news"} 1700000000' in output
    assert 'poy_dty_scheduler_last_attempt_unixtime{scheduler="news"} 1700000001' in output
    assert 'poy_dty_scheduler_last_attempt_status{scheduler="news",status="completed"} 1' in output
    assert 'poy_dty_scheduler_last_duration_seconds{scheduler="news"} 12.25' in output
    assert 'poy_dty_scheduler_backlog{scheduler="news"} 3' in output


def test_operational_gauges_clamp_impossible_negative_values(monkeypatch) -> None:
    monkeypatch.setattr(observability, "SOURCE_FRESHNESS_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_SUCCESS_UNIX_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_DURATION_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_BACKLOG", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_STATUS", {})
    monkeypatch.setattr(observability, "SCHEDULER_FAILURE_COUNTER", observability.Counter())

    observability.observe_source_freshness(source_id="feed", age_seconds=-1)
    observability.observe_scheduler(
        scheduler="daily",
        status="failed",
        last_attempt_unix_seconds=-1,
        last_success_unix_seconds=-1,
        duration_seconds=-2,
        backlog=-3,
    )

    assert observability.SOURCE_FRESHNESS_SECONDS["feed"] == 0
    assert observability.SCHEDULER_LAST_SUCCESS_UNIX_SECONDS["daily"] == 0
    assert observability.SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS["daily"] == 0
    assert observability.SCHEDULER_LAST_DURATION_SECONDS["daily"] == 0
    assert observability.SCHEDULER_BACKLOG["daily"] == 0
    assert observability.SCHEDULER_FAILURE_COUNTER["daily failed"] == 1


def test_failure_does_not_advance_last_success(monkeypatch) -> None:
    monkeypatch.setattr(observability, "SCHEDULER_LAST_SUCCESS_UNIX_SECONDS", {"daily": 100.0})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_DURATION_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_BACKLOG", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_STATUS", {})
    monkeypatch.setattr(observability, "SCHEDULER_FAILURE_COUNTER", observability.Counter())

    observability.observe_scheduler(
        scheduler="daily",
        status="timeout",
        last_attempt_unix_seconds=200,
        duration_seconds=30,
        backlog=1,
    )

    assert observability.SCHEDULER_LAST_SUCCESS_UNIX_SECONDS["daily"] == 100
    assert observability.SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS["daily"] == 200


def test_scheduler_status_label_has_bounded_cardinality(monkeypatch) -> None:
    monkeypatch.setattr(observability, "SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_DURATION_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_BACKLOG", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_STATUS", {})
    monkeypatch.setattr(observability, "SCHEDULER_FAILURE_COUNTER", observability.Counter())

    observability.observe_scheduler(
        scheduler="daily",
        status="provider-specific-free-text",
        duration_seconds=1,
        backlog=0,
    )

    assert observability.SCHEDULER_LAST_STATUS["daily"] == "unknown"


def test_external_scheduler_state_is_refreshed_without_recounting_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    status_path = tmp_path / "latest-scheduler.json"
    status_path.write_text(
        json.dumps(
            {
                "scheduler_observability": {
                    "schema_version": "scheduler_observability.v1",
                    "scheduler": "news_scheduler",
                    "last_attempt_at": "2026-09-01T17:59:38+00:00",
                    "last_success_at": "2026-09-01T17:29:38+00:00",
                    "status": "degraded",
                    "duration_seconds": 210,
                    "backlog": 5,
                    "failure_counts": {"blocked": 2},
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NEWS_SCHEDULER_STATUS_PATH", str(status_path))
    monkeypatch.delenv("LOCAL_DAILY_SCHEDULER_STATUS_PATH", raising=False)
    monkeypatch.setattr(observability, "SCHEDULER_LAST_SUCCESS_UNIX_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_DURATION_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_BACKLOG", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_STATUS", {})
    monkeypatch.setattr(observability, "SCHEDULER_FAILURE_COUNTER", observability.Counter())

    first = observability.metrics_text()
    second = observability.metrics_text()

    assert 'poy_dty_scheduler_last_attempt_status{scheduler="news_scheduler",status="degraded"} 1' in first
    assert 'poy_dty_scheduler_last_duration_seconds{scheduler="news_scheduler"} 210.0' in first
    assert 'poy_dty_scheduler_backlog{scheduler="news_scheduler"} 5' in first
    assert 'poy_dty_scheduler_failures_total{scheduler="news_scheduler",status="blocked"} 2' in first
    assert second.count('poy_dty_scheduler_failures_total{scheduler="news_scheduler",status="blocked"} 2') == 1
    assert observability.SCHEDULER_FAILURE_COUNTER["news_scheduler blocked"] == 2


def test_invalid_external_scheduler_state_does_not_fabricate_success(tmp_path: Path, monkeypatch) -> None:
    status_path = tmp_path / "latest-status.json"
    status_path.write_text("not-json\n", encoding="utf-8")
    monkeypatch.setenv("LOCAL_DAILY_SCHEDULER_STATUS_PATH", str(status_path))
    monkeypatch.delenv("NEWS_SCHEDULER_STATUS_PATH", raising=False)
    monkeypatch.setattr(observability, "SCHEDULER_LAST_SUCCESS_UNIX_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_DURATION_SECONDS", {})
    monkeypatch.setattr(observability, "SCHEDULER_BACKLOG", {})
    monkeypatch.setattr(observability, "SCHEDULER_LAST_STATUS", {})
    monkeypatch.setattr(observability, "SCHEDULER_FAILURE_COUNTER", observability.Counter())

    output = observability.metrics_text()

    assert 'scheduler="local_daily"' not in output

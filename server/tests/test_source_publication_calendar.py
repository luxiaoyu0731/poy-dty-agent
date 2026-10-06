from datetime import UTC, datetime

import pytest

from app.public_benchmark_v2 import PUBLIC_BENCHMARK_INPUTS, evaluate_observation
from app.source_automation_policy import assess_readiness
from app.source_publication_calendar import cfets_latest_due_date


def clock(value):
    return datetime.fromisoformat(value).astimezone(UTC)


@pytest.mark.parametrize("now,expected", [
    ("2026-10-05T08:08:31+08:00", "2026-09-30"),
    ("2026-10-07T23:59:00+08:00", "2026-09-30"),
    ("2026-10-08T08:00:00+08:00", "2026-09-30"),
    ("2026-10-08T09:15:00+08:00", "2026-10-08"),
    ("2026-02-22T08:00:00+08:00", "2026-02-14"),
    ("2026-05-05T12:00:00+08:00", "2026-04-30"),
    ("2026-09-20T09:15:00+08:00", "2026-09-20"),
    ("2026-10-10T09:15:00+08:00", "2026-10-10"),
    ("2027-01-01T08:00:00+08:00", None),
])
def test_source_specific_latest_due_publication(now, expected):
    result = cfets_latest_due_date(clock(now))
    assert (result.isoformat() if result else None) == expected


@pytest.mark.parametrize("now,observed,expected", [
    ("2026-10-05T08:08:31+08:00", "2026-09-30", "ready"),
    ("2026-10-05T08:08:31+08:00", "2026-09-29", "stale"),
    ("2026-10-08T08:00:00+08:00", "2026-09-30", "ready"),
    ("2026-10-08T09:15:00+08:00", "2026-09-30", "stale"),
    ("2026-02-22T08:00:00+08:00", "2026-02-14", "ready"),
    ("2026-02-22T08:00:00+08:00", "2026-02-13", "stale"),
    ("2027-01-05T08:00:00+08:00", "2026-12-30", "stale"),
    ("2026-10-05T08:00:00+08:00", "2026-10-06", "invalid"),
])
def test_both_freshness_consumers_agree(now, observed, expected):
    now = clock(now)
    row = {"observed_at": observed, "value": 7.2, "unit": "cny_per_usd", "source_id": "cfets_cny_parity"}
    result = evaluate_observation(PUBLIC_BENCHMARK_INPUTS[-1], row, now=now)
    assert result["status"] == expected
    state = {"last_run_status": "success", "last_run_started_at": now.isoformat(),
             "last_success_at": now.isoformat(), "latest_observed_at": observed}
    readiness = assess_readiness("cfets_cny_parity", state, now=now)
    assert readiness.ready == (expected == "ready")
    if expected == "ready":
        assert "latest_due=" in result["reason"]
        assert result["age_seconds"] > 4 * 86400


def test_calendar_cannot_launder_other_source_or_invalid_unit():
    now = clock("2026-10-05T08:00:00+08:00")
    row = {"observed_at": "2026-09-30", "value": 7.2, "unit": "cny_per_usd", "source_id": "other"}
    assert evaluate_observation(PUBLIC_BENCHMARK_INPUTS[-1], row, now=now)["status"] == "stale"
    row.update(source_id="cfets_cny_parity", unit="USD")
    assert evaluate_observation(PUBLIC_BENCHMARK_INPUTS[-1], row, now=now)["status"] == "invalid"
    row.update(last=80, unit="USD/bbl")
    assert evaluate_observation(PUBLIC_BENCHMARK_INPUTS[0], row, now=now)["status"] == "stale"


def test_holiday_does_not_hide_failed_capture_or_transport_staleness():
    now = clock("2026-10-05T08:00:00+08:00")
    state = {"last_run_status": "failed", "last_success_at": "2026-09-30", "latest_observed_at": "2026-09-30"}
    result = assess_readiness("cfets_cny_parity", state, now=now)
    assert not result.ready
    assert "last_run_not_successful" in result.reasons
    assert "last_success_outside_sla" in result.reasons
    assert "latest_observation_outside_sla" not in result.reasons

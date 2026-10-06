from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest

from app import experience_settlement_scheduler as scheduler


def test_next_run_is_exactly_0930_beijing_on_weekdays_and_skips_weekends() -> None:
    before = datetime(2026, 8, 7, 1, 29, tzinfo=UTC)
    exact = datetime(2026, 8, 7, 1, 30, tzinfo=UTC)
    after = datetime(2026, 8, 7, 1, 30, 1, tzinfo=UTC)
    sunday = datetime(2026, 8, 9, 1, 0, tzinfo=UTC)

    assert scheduler._next_experience_settlement_run(before).isoformat() == "2026-08-07T09:30:00+08:00"
    assert scheduler._next_experience_settlement_run(exact).isoformat() == "2026-08-07T09:30:00+08:00"
    assert scheduler._next_experience_settlement_run(after).isoformat() == "2026-08-10T09:30:00+08:00"
    assert scheduler._next_experience_settlement_run(sunday).isoformat() == "2026-08-10T09:30:00+08:00"


def test_cutoff_runner_uses_only_the_frozen_0930_beijing_time(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def settle(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"status": "blocked", "execution": None}

    monkeypatch.setattr(scheduler, "settle_selected_terminal_experience_candidates", settle)

    result = scheduler.run_experience_settlement_cutoff(now=datetime(2026, 8, 7, 1, 30, tzinfo=UTC))

    assert captured == {"evaluation_as_of_time": "2026-08-07T09:30:00+08:00"}
    assert result == {"status": "blocked", "execution": None}
    assert scheduler.latest_experience_settlement_result() == result


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 8, 7, 1, 31, tzinfo=UTC),
        datetime(2026, 8, 8, 1, 30, tzinfo=UTC),
        datetime(2026, 8, 7, 1, 30),
    ],
)
def test_runner_rejects_late_weekend_or_unzoned_execution(now: datetime) -> None:
    with pytest.raises(ValueError, match="experience_settlement_scheduler"):
        scheduler.run_experience_settlement_cutoff(now=now)


def test_scheduled_failure_is_recorded_and_the_next_cutoff_can_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def to_thread(function: object, **kwargs: object) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("fixture settlement failure")
        return {"status": "completed", "execution": {"saved": 2}, **kwargs}

    monkeypatch.setattr(scheduler.asyncio, "to_thread", to_thread)
    first_cutoff = datetime(2026, 8, 7, 1, 30, tzinfo=UTC)
    second_cutoff = datetime(2026, 8, 10, 1, 30, tzinfo=UTC)

    failed = asyncio.run(scheduler._run_scheduled_cutoff(first_cutoff))
    completed = asyncio.run(scheduler._run_scheduled_cutoff(second_cutoff))

    assert failed == {
        "status": "failed",
        "execution": None,
        "evaluation_as_of_time": "2026-08-07T01:30:00+00:00",
        "error": {
            "code": "EXPERIENCE_SETTLEMENT_SCHEDULED_RUN_FAILED",
            "type": "RuntimeError",
            "message": "fixture settlement failure",
        },
    }
    assert completed["status"] == "completed"
    assert calls == 2


def test_scheduled_cutoff_does_not_swallow_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_cutoff(*, now: datetime) -> dict[str, object]:
        started.set()
        assert release.wait(timeout=5)
        return {"status": "completed", "execution": {"saved": 1}}

    monkeypatch.setattr(scheduler, "run_experience_settlement_cutoff", blocking_cutoff)

    async def scenario() -> None:
        task = asyncio.create_task(
            scheduler._run_scheduled_cutoff(datetime(2026, 8, 7, 1, 30, tzinfo=UTC))
        )
        for _ in range(100):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert scheduler._inflight_settlement is None

    asyncio.run(scenario())

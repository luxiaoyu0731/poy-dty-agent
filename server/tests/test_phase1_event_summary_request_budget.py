from __future__ import annotations

import argparse
import asyncio

import pytest

from app.deepseek_client import DeepSeekClient
from scripts import run_event_summary_worker as worker


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        batch_size=5,
        concurrency=1,
        max_attempts=3,
        lease_minutes=30,
        queue_batch_size=100,
        daily_request_limit=50,
        daily_budget_rmb=10.0,
        max_cost_per_request_rmb=0.2,
        request_interval_seconds=0.0,
    )


def test_client_http_attempt_budget_counts_retries_and_stops_before_network() -> None:
    client = DeepSeekClient()
    persisted: list[int] = []
    client.set_http_attempt_budget(2, on_attempt=persisted.append)

    client._reserve_http_attempt()
    client._reserve_http_attempt()

    assert client.http_attempts_used == 2
    assert persisted == [1, 2]
    with pytest.raises(RuntimeError, match="deepseek_http_attempt_budget_exhausted"):
        client._reserve_http_attempt()
    assert client.http_attempts_used == 2


def test_provider_attempt_is_not_allowed_when_durable_reservation_fails() -> None:
    client = DeepSeekClient()

    def fail_to_persist(_: int) -> None:
        raise OSError("disk unavailable")

    client.set_http_attempt_budget(2, on_attempt=fail_to_persist)
    with pytest.raises(OSError, match="disk unavailable"):
        client._reserve_http_attempt()
    assert client.http_attempts_used == 0


def test_worker_persists_actual_provider_attempts_not_selected_articles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DeepSeekClient()
    client.api_key = "test-only"
    monkeypatch.setattr(worker, "recover_stale_event_ai_summary_leases", lambda _: 0)
    monkeypatch.setattr(worker, "queue_eligible_summaries", lambda *_: 0)
    monkeypatch.setattr(worker, "event_ai_summary_queue_counts", lambda _: {"pending": 2})

    async def process(**_: object) -> dict[str, int]:
        client._reserve_http_attempt()
        client._reserve_http_attempt()
        client._reserve_http_attempt()
        return {"selected": 2, "completed": 1, "failed": 1}

    monkeypatch.setattr(worker, "process_event_summary_queue", process)
    result = asyncio.run(worker.run_cycle(_args(), client=client, allowance=4))

    assert result["processed"]["selected"] == 2
    assert result["provider_http_attempts"] == 3
    assert result["reserved_requests"] == 3

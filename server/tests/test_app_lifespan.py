from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import main as main_module


class _Connection:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def execute(self, statement: str) -> None:
        assert statement == "SELECT 1"
        self.events.append("storage_ready")

    def close(self) -> None:
        self.events.append("storage_closed")


def test_lifespan_starts_and_stops_schedulers_in_strict_reverse_order(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        main_module,
        "settings",
        SimpleNamespace(environment="production", enforce_internal_token=True, personal_mode=False),
    )
    monkeypatch.setattr(main_module, "connect", lambda: _Connection(events))
    monkeypatch.setattr(
        main_module,
        "warm_semantic_retrieval",
        lambda _query: events.append("warm_semantic") or {"status": "ready"},
    )
    monkeypatch.setattr(main_module, "start_intraday_price_scheduler", lambda: events.append("start_intraday"))
    monkeypatch.setattr(main_module, "start_agent_governance_scheduler", lambda: events.append("start_governance"))
    monkeypatch.setattr(main_module, "start_experience_settlement_scheduler", lambda: events.append("start_experience"))

    async def stop(name: str) -> None:
        events.append(f"stop_{name}")

    monkeypatch.setattr(main_module, "stop_intraday_price_scheduler", lambda: stop("intraday"))
    monkeypatch.setattr(main_module, "stop_agent_governance_scheduler", lambda: stop("governance"))
    monkeypatch.setattr(main_module, "stop_experience_settlement_scheduler", lambda: stop("experience"))

    async def run() -> None:
        async with main_module.application_lifespan(main_module.app):
            events.append("serving")

    asyncio.run(run())

    assert events == [
        "storage_ready",
        "storage_closed",
        "warm_semantic",
        "start_intraday",
        "start_governance",
        "start_experience",
        "serving",
        "stop_experience",
        "stop_governance",
        "stop_intraday",
    ]


def test_lifespan_rolls_back_only_schedulers_started_before_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(main_module, "connect", lambda: _Connection(events))
    monkeypatch.setattr(main_module, "start_intraday_price_scheduler", lambda: events.append("start_intraday"))

    def fail_governance() -> None:
        events.append("start_governance")
        raise RuntimeError("governance_start_failed")

    monkeypatch.setattr(main_module, "start_agent_governance_scheduler", fail_governance)
    monkeypatch.setattr(main_module, "start_experience_settlement_scheduler", lambda: events.append("start_experience"))

    async def stop(name: str) -> None:
        events.append(f"stop_{name}")

    monkeypatch.setattr(main_module, "stop_intraday_price_scheduler", lambda: stop("intraday"))
    monkeypatch.setattr(main_module, "stop_agent_governance_scheduler", lambda: stop("governance"))
    monkeypatch.setattr(main_module, "stop_experience_settlement_scheduler", lambda: stop("experience"))

    async def run() -> None:
        async with main_module.application_lifespan(main_module.app):
            raise AssertionError("lifespan must not yield after startup failure")

    with pytest.raises(RuntimeError, match="governance_start_failed"):
        asyncio.run(run())

    assert events == [
        "storage_ready",
        "storage_closed",
        "start_intraday",
        "start_governance",
        "stop_intraday",
    ]

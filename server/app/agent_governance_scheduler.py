from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

from .agent_evaluation_collection import collect_governed_assistant_window_observability
from .agent_production_policy import (
    DAILY_TIME_ZONE,
    PRODUCTION_READINESS_POLICY,
    daily_governance_report,
    daily_governance_window,
    unavailable_daily_governance_report,
)
from .observability import observe_scheduler
from .settings import settings
from .storage import (
    get_agent_governance_report_for_window_end,
    get_latest_agent_governance_report,
    save_agent_governance_report,
)

_scheduler_task: asyncio.Task[None] | None = None
_latest_report: dict[str, Any] | None = None
_logger = logging.getLogger(__name__)
_ROUND_FAILURE_RETRY_SECONDS = 300.0


def latest_daily_governance_report() -> dict[str, Any] | None:
    """Return the persisted latest report, with cache used only as a fallback."""

    try:
        persisted = get_latest_agent_governance_report()
    except Exception:
        persisted = None
    if persisted is not None:
        return persisted
    return None if _latest_report is None else dict(_latest_report)


def run_daily_governance_window(
    *,
    now: datetime,
    collector: Callable[..., dict[str, Any]] = collect_governed_assistant_window_observability,
) -> dict[str, Any]:
    """Collect one fixed-policy window; collection failures become low confidence."""

    global _latest_report
    window = daily_governance_window(now)
    try:
        aggregate = collector(
            window_start=window["start"],
            window_end=window["end"],
            policy=PRODUCTION_READINESS_POLICY,
        )
        report = daily_governance_report(aggregate, evaluated_at=now)
    except Exception:
        report = unavailable_daily_governance_report(evaluated_at=now)
    try:
        persisted = save_agent_governance_report(report)
    except Exception:
        persisted = unavailable_daily_governance_report(evaluated_at=now)
    _latest_report = persisted
    return dict(persisted)


def run_late_start_governance_catchup(*, now: datetime) -> dict[str, Any] | None:
    """Persist one low-confidence report for a completed missed daily window.

    A process which starts after the 08:10 Beijing cutoff must not silently
    leave that completed window without a governance posture.  It deliberately
    does not run a late collector: the report records low confidence instead.
    Existing immutable reports win, so a catch-up can never overwrite a normal
    report created by another process.
    """

    global _latest_report
    window_end = _late_start_window_end(now)
    if window_end is None:
        return None
    existing = get_agent_governance_report_for_window_end(window_end.isoformat())
    if existing is not None:
        _latest_report = existing
        return dict(existing)
    report = unavailable_daily_governance_report(evaluated_at=now)
    try:
        persisted = save_agent_governance_report(report)
    except ValueError as exc:
        if str(exc) != "agent_governance_window_conflict":
            raise
        persisted = get_agent_governance_report_for_window_end(window_end.isoformat())
        if persisted is None:  # pragma: no cover - protected by the writer transaction
            raise
    _latest_report = persisted
    return dict(persisted)


async def _daily_governance_loop() -> None:
    while True:
        attempt_started: float | None = None
        try:
            now = datetime.now(DAILY_TIME_ZONE)
            if _late_start_window_end(now) is not None:
                attempt_started = time.monotonic()
                await asyncio.to_thread(run_late_start_governance_catchup, now=now)
            now = datetime.now(DAILY_TIME_ZONE)
            next_run = _next_daily_run(now)
            await asyncio.sleep(max(0.0, (next_run - now).total_seconds()))
            attempt_started = time.monotonic()
            await asyncio.to_thread(run_daily_governance_window, now=next_run)
            completed_at = time.time()
            observe_scheduler(
                scheduler="agent_governance",
                status="completed",
                last_attempt_unix_seconds=completed_at,
                last_success_unix_seconds=completed_at,
                duration_seconds=time.monotonic() - attempt_started,
                backlog=0,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            observe_scheduler(
                scheduler="agent_governance",
                status="failed",
                last_attempt_unix_seconds=time.time(),
                duration_seconds=time.monotonic() - attempt_started if attempt_started is not None else 0,
                backlog=0,
            )
            # One failed round (DB briefly unavailable, clock jump, ...) must not
            # silently kill the daily scheduler; retry soon and keep the loop alive.
            _logger.exception("daily governance round failed; retrying in %.0fs", _ROUND_FAILURE_RETRY_SECONDS)
            await asyncio.sleep(_ROUND_FAILURE_RETRY_SECONDS)


def _scheduler_finished(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    _logger.error("daily governance scheduler stopped unexpectedly: %s: %s", type(exc).__name__, exc)
    global _scheduler_task
    _scheduler_task = None
    if settings.agent_governance_scheduler_enabled:
        with suppress(RuntimeError):
            start_agent_governance_scheduler()


def _next_daily_run(now: datetime) -> datetime:
    window = daily_governance_window(now)
    end = datetime.fromisoformat(window["end"])
    return end + timedelta(days=1) if now > end else end


def _late_start_window_end(now: datetime) -> datetime | None:
    """Return the elapsed 08:10 cutoff that warrants one catch-up report."""

    end = datetime.fromisoformat(daily_governance_window(now)["end"])
    return end if now > end else None


def start_agent_governance_scheduler() -> asyncio.Task[None] | None:
    global _scheduler_task
    if not settings.agent_governance_scheduler_enabled:
        return None
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(_daily_governance_loop(), name="agent-governance-daily-scheduler")
        _scheduler_task.add_done_callback(_scheduler_finished)
    return _scheduler_task


async def stop_agent_governance_scheduler() -> None:
    global _scheduler_task
    task = _scheduler_task
    _scheduler_task = None
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

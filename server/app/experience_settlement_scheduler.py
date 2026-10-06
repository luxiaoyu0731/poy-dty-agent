"""Disabled-by-default weekday scheduler for cutoff-bound Experience settlement."""

from __future__ import annotations

import asyncio
import time as time_module
from contextlib import suppress
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .experience_settlement_service import settle_selected_terminal_experience_candidates
from .observability import observe_scheduler
from .settings import settings

EXPERIENCE_TIME_ZONE = ZoneInfo("Asia/Shanghai")
EXPERIENCE_SETTLEMENT_CUTOFF = time(hour=9, minute=30)

_scheduler_task: asyncio.Task[None] | None = None
_latest_result: dict[str, Any] | None = None
_inflight_settlement: asyncio.Task[dict[str, Any]] | None = None


def latest_experience_settlement_result() -> dict[str, Any] | None:
    return None if _latest_result is None else dict(_latest_result)


def run_experience_settlement_cutoff(*, now: datetime) -> dict[str, Any]:
    """Run one explicit settlement cutoff through the selected-input boundary."""

    global _latest_result
    cutoff = _as_cutoff(now)
    result = settle_selected_terminal_experience_candidates(evaluation_as_of_time=cutoff.isoformat())
    _latest_result = result
    return dict(result)


async def _weekday_settlement_loop() -> None:
    while True:
        now = datetime.now(EXPERIENCE_TIME_ZONE)
        next_run = _next_experience_settlement_run(now)
        await asyncio.sleep((next_run - now).total_seconds())
        started = time_module.monotonic()
        result = await _run_scheduled_cutoff(next_run)
        status = str(result.get("status") or "unknown")
        attempted_at = time_module.time()
        observe_scheduler(
            scheduler="experience_settlement",
            status=status,
            last_attempt_unix_seconds=attempted_at,
            last_success_unix_seconds=attempted_at if status != "failed" else None,
            duration_seconds=time_module.monotonic() - started,
            backlog=0,
        )


async def _run_scheduled_cutoff(next_run: datetime) -> dict[str, Any]:
    """Isolate one scheduled failure so the weekday loop remains alive."""

    global _inflight_settlement, _latest_result
    if _inflight_settlement is not None and not _inflight_settlement.done():
        raise RuntimeError("experience_settlement_already_running")
    worker = asyncio.create_task(
        asyncio.to_thread(run_experience_settlement_cutoff, now=next_run),
        name="experience-settlement-cutoff-worker",
    )
    _inflight_settlement = worker
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # Cancelling ``to_thread`` only cancels its awaiter.  The underlying
        # SQLite work must reach a terminal state before shutdown may claim the
        # scheduler has stopped, otherwise a replacement scheduler could
        # overlap a still-writing worker thread.
        with suppress(Exception):
            await worker
        raise
    except Exception as exc:
        failure = {
            "status": "failed",
            "execution": None,
            "evaluation_as_of_time": next_run.isoformat(),
            "error": {
                "code": "EXPERIENCE_SETTLEMENT_SCHEDULED_RUN_FAILED",
                "type": type(exc).__name__,
                "message": str(exc),
            },
        }
        _latest_result = failure
        return dict(failure)
    finally:
        if _inflight_settlement is worker:
            _inflight_settlement = None


def _next_experience_settlement_run(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("experience_settlement_scheduler_requires_timezone")
    local = now.astimezone(EXPERIENCE_TIME_ZONE)
    candidate = local.replace(
        hour=EXPERIENCE_SETTLEMENT_CUTOFF.hour,
        minute=EXPERIENCE_SETTLEMENT_CUTOFF.minute,
        second=0,
        microsecond=0,
    )
    if local > candidate:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _as_cutoff(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("experience_settlement_scheduler_requires_timezone")
    local = now.astimezone(EXPERIENCE_TIME_ZONE)
    if local.weekday() >= 5 or local.time().replace(tzinfo=None) != EXPERIENCE_SETTLEMENT_CUTOFF:
        raise ValueError("experience_settlement_scheduler_cutoff_invalid")
    return local.replace(second=0, microsecond=0)


def start_experience_settlement_scheduler() -> asyncio.Task[None] | None:
    global _scheduler_task
    if not settings.experience_settlement_scheduler_enabled:
        return None
    if _scheduler_task is None or _scheduler_task.done():
        _scheduler_task = asyncio.create_task(
            _weekday_settlement_loop(), name="experience-settlement-weekday-scheduler"
        )
    return _scheduler_task


async def stop_experience_settlement_scheduler() -> None:
    global _scheduler_task
    task = _scheduler_task
    _scheduler_task = None
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

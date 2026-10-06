from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Any

SCHEMA_VERSION = "scheduler_observability.v1"
BOUNDED_STATUSES = frozenset(
    {
        "success",
        "completed",
        "ready",
        "ready_with_warnings",
        "degraded",
        "failed",
        "failure",
        "error",
        "timeout",
        "skipped",
        "blocked",
        "locked",
        "unknown",
    }
)
SUCCESS_STATUSES = frozenset({"success", "completed", "ready", "ready_with_warnings"})
FAILURE_STATUSES = frozenset({"failed", "failure", "error", "timeout", "blocked"})


def normalize_scheduler_status(value: object) -> str:
    candidate = str(value or "unknown").strip().lower()
    return candidate if candidate in BOUNDED_STATUSES else "unknown"


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def duration_seconds(started_at: object, finished_at: object) -> float:
    started = parse_timestamp(started_at)
    finished = parse_timestamp(finished_at)
    if started is None or finished is None:
        return 0.0
    return max(0.0, (finished - started).total_seconds())


def build_scheduler_observability(
    *,
    scheduler: str,
    status: object,
    started_at: object,
    finished_at: object,
    backlog: int,
    previous_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_scheduler_status(status)
    previous = _embedded_state(previous_payload or {})
    failure_counts = _bounded_failure_counts(previous.get("failure_counts"))
    if normalized in FAILURE_STATUSES:
        failure_counts[normalized] = failure_counts.get(normalized, 0) + 1
    last_success_at = previous.get("last_success_at")
    if normalized in SUCCESS_STATUSES and parse_timestamp(finished_at) is not None:
        last_success_at = str(finished_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "scheduler": scheduler,
        "last_attempt_at": str(finished_at or started_at or ""),
        "last_success_at": str(last_success_at or ""),
        "status": normalized,
        "duration_seconds": duration_seconds(started_at, finished_at),
        "backlog": max(0, int(backlog)),
        "failure_counts": failure_counts,
    }


def scheduler_snapshot(payload: Mapping[str, Any], *, scheduler: str) -> dict[str, Any] | None:
    embedded = _embedded_state(payload)
    if embedded:
        status = normalize_scheduler_status(embedded.get("status"))
        attempt = parse_timestamp(embedded.get("last_attempt_at"))
        success = parse_timestamp(embedded.get("last_success_at"))
        if attempt is None:
            return None
        return {
            "scheduler": scheduler,
            "status": status,
            "last_attempt_unix_seconds": attempt.timestamp(),
            "last_success_unix_seconds": success.timestamp() if success else None,
            "duration_seconds": _nonnegative_number(embedded.get("duration_seconds")),
            "backlog": _nonnegative_int(embedded.get("backlog")),
            "failure_counts": _bounded_failure_counts(embedded.get("failure_counts")),
        }
    return _legacy_scheduler_snapshot(payload, scheduler=scheduler)


def news_scheduler_backlog(payload: Mapping[str, Any]) -> int:
    public = payload.get("public_fetch") if isinstance(payload.get("public_fetch"), Mapping) else {}
    news = payload.get("news_fetch") if isinstance(payload.get("news_fetch"), Mapping) else {}
    public_errors = len(public.get("errors", [])) if isinstance(public.get("errors"), list) else 0
    news_errors = len(news.get("errors", [])) if isinstance(news.get("errors"), list) else 0
    counts = news.get("run_status_counts") if isinstance(news.get("run_status_counts"), Mapping) else {}
    failed_runs = _nonnegative_int(counts.get("error")) + _nonnegative_int(counts.get("timeout"))
    failed_summaries = _nonnegative_int(news.get("summaries_failed"))
    return public_errors + max(news_errors, failed_runs + failed_summaries)


def _legacy_scheduler_snapshot(payload: Mapping[str, Any], *, scheduler: str) -> dict[str, Any] | None:
    started_at = payload.get("started_at")
    finished_at = payload.get("finished_at")
    attempt = parse_timestamp(finished_at) or parse_timestamp(started_at)
    if attempt is None:
        return None
    raw_status = payload.get("overall_status") if scheduler == "local_daily" else payload.get("status")
    status = normalize_scheduler_status(raw_status)
    blockers = payload.get("blockers") if isinstance(payload.get("blockers"), list) else []
    backlog = len(blockers) if scheduler == "local_daily" else news_scheduler_backlog(payload)
    return {
        "scheduler": scheduler,
        "status": status,
        "last_attempt_unix_seconds": attempt.timestamp(),
        "last_success_unix_seconds": attempt.timestamp() if status in SUCCESS_STATUSES else None,
        "duration_seconds": duration_seconds(started_at, finished_at),
        "backlog": backlog,
        "failure_counts": {},
    }


def _embedded_state(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    candidate = payload.get("scheduler_observability")
    if not isinstance(candidate, Mapping) or candidate.get("schema_version") != SCHEMA_VERSION:
        return {}
    return candidate


def _bounded_failure_counts(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {status: _nonnegative_int(count) for status, count in value.items() if status in FAILURE_STATUSES}


def _nonnegative_int(value: object) -> int:
    return int(_nonnegative_number(value))


def _nonnegative_number(value: object) -> float:
    candidate = _number(value)
    return candidate if isfinite(candidate) and candidate >= 0 else 0.0


def _number(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0

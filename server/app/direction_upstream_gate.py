"""Upstream readiness gate for the daily direction review.

Root cause (2026-09-18 09:31 chain run): the foundation step's direction
review raced the resident event-summary worker. Overnight summaries were
still queued when the review retrieved its evidence, ``evidence_ids`` came
back empty, and ``review_daily_direction`` honestly abstained with zero
provider calls; a 09:5x re-run of the same retrieval already saw 12
documents. This gate bounds that race instead of assuming scheduling: the
caller waits, up to a configurable deadline, until

1. the current intake cohort's grounded summaries are finished (not the
   entire historical backlog), and
2. the ``event_direction`` evidence list for the review question is
   non-empty under the same business/visibility filters the review itself
   applies.

On timeout or unreadable upstream state the caller skips the provider entirely.
The exact checked evidence and cutoff are returned to avoid a second retrieval
racing the successful probe. The daily chain already awaits source acquisition
before entering foundation materialization.
"""

from __future__ import annotations

import math
import os
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .hybrid_direction_review import _business_evidence, _visible_at_cutoff

MAX_WAIT_SECONDS_ENV = "DIRECTION_UPSTREAM_GATE_MAX_WAIT_SECONDS"
POLL_SECONDS_ENV = "DIRECTION_UPSTREAM_GATE_POLL_SECONDS"
DEFAULT_MAX_WAIT_SECONDS = 1800.0
DEFAULT_POLL_SECONDS = 30.0
# Expired leases remain unfinished work; expose them separately for diagnosis.
STALE_LEASE_WINDOW_MINUTES = 30

EvidenceRetriever = Callable[[str], object]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


def _bounded_number(value: object, default: float, low: float, high: float) -> float:
    try:
        parsed = float(value)
        return min(high, max(low, parsed)) if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def gate_limits(*, max_wait_seconds: float | None = None, poll_seconds: float | None = None) -> dict[str, float]:
    return {
        "max_wait_seconds": _bounded_number(
            max_wait_seconds if max_wait_seconds is not None else os.getenv(MAX_WAIT_SECONDS_ENV),
            DEFAULT_MAX_WAIT_SECONDS,
            0,
            1800,
        ),
        "poll_seconds": _bounded_number(
            poll_seconds if poll_seconds is not None else os.getenv(POLL_SECONDS_ENV), DEFAULT_POLL_SECONDS, 1, 60
        ),
    }


def probe_event_summary_queue(
    db_path: Path | str, *, cohort_start: str | None = None, cohort_end: str | None = None,
) -> dict[str, Any]:
    """Read-only snapshot of the grounded summary queue the worker drains.

    Opens the database ``mode=ro`` (the materializer's own ``table_counts``
    precedent), so probing never takes a write lock and is safe in dry-run.
    A missing database or table is unknown, never ready.
    """
    path = Path(db_path)
    probed_at = datetime.now(UTC).isoformat()
    if not path.is_file():
        return {
            "database_present": False,
            "queue_table_present": False,
            "pending": 0,
            "processing": 0,
            "stale_processing": 0,
            "in_flight_leases": 0,
            "probed_at": probed_at,
        }
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)) as connection:
            connection.execute("PRAGMA busy_timeout = 10000")
            table_present = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='event_ai_summaries' LIMIT 1"
                ).fetchone()
                is not None
            )
            if not table_present:
                statuses: dict[str, int] = {}
                stale = 0
                retryable = 0
            else:
                # Freeze the intake cohort when the gate starts. Historical
                # retries and newly arriving articles cannot move its goalpost.
                cohort_sql = "SELECT * FROM event_ai_summaries"
                cohort_params: tuple[str, ...] = ()
                if cohort_start and cohort_end:
                    cohort_sql = (
                        "SELECT s.* FROM event_ai_summaries s JOIN news_articles a ON a.article_id=s.article_id "
                        "WHERE julianday(COALESCE(NULLIF(a.first_seen_at,''),a.created_at)) "
                        "BETWEEN julianday(?) AND julianday(?)"
                    )
                    cohort_params = (cohort_start, cohort_end)
                cte = f"WITH cohort AS ({cohort_sql}) "
                statuses = {
                    str(row[0]): int(row[1])
                    for row in connection.execute(
                        cte + "SELECT summary_status, COUNT(*) FROM cohort GROUP BY summary_status", cohort_params,
                    ).fetchall()
                }
                max_attempts = int(_bounded_number(os.getenv("EVENT_SUMMARY_MAX_ATTEMPTS"), 3, 1, 1000))
                retryable = int(
                    connection.execute(
                        cte + "SELECT COUNT(*) FROM cohort "
                        "WHERE summary_status IN ('failed','rejected') AND attempts < ?",
                        (*cohort_params, max_attempts),
                    ).fetchone()[0]
                )
                stale = int(
                    connection.execute(
                        cte + """SELECT COUNT(*) FROM cohort WHERE summary_status='processing'
                           AND datetime(updated_at)<datetime('now', ?)""",
                        (*cohort_params, f"-{STALE_LEASE_WINDOW_MINUTES} minutes"),
                    ).fetchone()[0]
                )
    except sqlite3.Error:
        # Unreadable evidence about the queue must not fabricate readiness.
        return {
            "database_present": path.is_file(),
            "queue_table_present": False,
            "pending": 0,
            "processing": 0,
            "stale_processing": 0,
            "in_flight_leases": 0,
            "probed_at": probed_at,
            "probe_error": "sqlite_error",
        }
    processing = statuses.get("processing", 0)
    return {
        "database_present": True,
        "queue_table_present": table_present,
        "pending": statuses.get("pending", 0),
        "retryable": retryable,
        "processing": processing,
        "stale_processing": stale,
        # Mirrors the worker's lease recovery: only leases inside the lease
        # window count as in-flight.
        "in_flight_leases": max(0, processing - stale),
        "probed_at": probed_at,
    }


def direction_review_evidence_ids(retrieval: object, as_of_time: str) -> list[str]:
    """Apply the review's own document filters so the gate predicts its outcome."""
    documents = [
        item
        for item in list(getattr(retrieval, "documents", []) or [])
        if _business_evidence(item) and _visible_at_cutoff(item, as_of_time)
    ]
    return [str(getattr(item, "doc_id", "")) for item in documents if getattr(item, "doc_id", "")]


def probe_upstream_readiness(
    *,
    db_path: Path | str,
    evidence_retriever: EvidenceRetriever,
    as_of_time: str,
    cohort_start: str | None = None,
    cohort_end: str | None = None,
) -> dict[str, Any]:
    queue = probe_event_summary_queue(db_path, cohort_start=cohort_start, cohort_end=cohort_end)
    known = bool(queue.get("database_present") and queue.get("queue_table_present") and not queue.get("probe_error"))
    pending = int(queue["pending"]) + int(queue.get("retryable", 0))
    # Expired processing leases still represent unfinished work, not completion.
    processing = int(queue["processing"])
    retrieval = None
    evidence_ids: list[str] = []
    error = ""
    if known and pending == 0 and processing == 0:
        try:
            retrieval = evidence_retriever(as_of_time)
            evidence_ids = direction_review_evidence_ids(retrieval, as_of_time)
        except Exception as exc:
            error = type(exc).__name__
    reason = (
        "upstream_state_unknown"
        if not known
        else "summaries_pending"
        if pending or processing
        else "retrieval_error"
        if error
        else "evidence_not_visible"
        if not evidence_ids
        else "ready"
    )
    return {
        "ready": reason == "ready",
        "reason_code": reason,
        "pending_summaries": pending,
        "in_flight_leases": int(queue["in_flight_leases"]),
        "processing": processing,
        "stale_processing": int(queue["stale_processing"]),
        "queue_table_present": bool(queue["queue_table_present"]),
        "evidence_count": len(evidence_ids),
        "probed_at": queue["probed_at"],
        "as_of_time": as_of_time,
        "cohort_start": cohort_start,
        "cohort_end": cohort_end,
        "error_type": error or queue.get("probe_error", ""),
        "_retrieval": retrieval,
    }


def wait_for_upstream_readiness(
    *,
    db_path: Path | str,
    evidence_retriever: EvidenceRetriever,
    wait: bool = True,
    max_wait_seconds: float | None = None,
    poll_seconds: float | None = None,
    sleep: Sleeper = time.sleep,
    monotonic: Clock = time.monotonic,
) -> dict[str, Any]:
    """Bounded wait for summary-queue drain plus non-empty review evidence.

    ``wait=False`` (dry-run / provider-disabled paths) performs exactly one
    probe and returns without sleeping. On timeout the caller keeps its
    provider is skipped; the last probe distinguishes unfinished summaries,
    unknown state and unavailable evidence. Probe I/O is additionally bounded
    by the daily chain foundation subprocess deadline.
    """
    limits = gate_limits(max_wait_seconds=max_wait_seconds, poll_seconds=poll_seconds)
    max_wait = limits["max_wait_seconds"]
    poll = limits["poll_seconds"]
    started = monotonic()
    cohort_end_time = datetime.now(UTC)
    cohort_end = cohort_end_time.isoformat()
    cohort_start = (cohort_end_time - timedelta(hours=48)).isoformat()
    deadline = started + max_wait
    poll_count = 0
    probe: dict[str, Any] = {}
    status = "probe_only"
    while True:
        poll_count += 1
        probe = probe_upstream_readiness(
            db_path=db_path,
            evidence_retriever=evidence_retriever,
            as_of_time=datetime.now(UTC).isoformat(),
            cohort_start=cohort_start,
            cohort_end=cohort_end,
        )
        if probe["ready"] and (not wait or max_wait == 0 or monotonic() <= deadline):
            status = "ready"
            break
        if not wait:
            status = "probe_only"
            break
        remaining = deadline - monotonic()
        if remaining <= 0:
            status = "timeout"
            break
        sleep(min(poll, remaining))
    retrieval = probe.pop("_retrieval", None)
    waited_seconds = round(monotonic() - started, 3)
    return {
        "status": status,
        "ready": status == "ready",
        "_retrieval": retrieval if status == "ready" else None,
        "as_of_time": probe.get("as_of_time", ""),
        "waited": bool(wait),
        "waited_seconds": waited_seconds,
        "poll_count": poll_count,
        "max_wait_seconds": max_wait,
        "poll_seconds": poll,
        "last_probe": probe,
    }


def gate_audit_summary(gate: dict[str, Any]) -> dict[str, Any]:
    """Compact, JSON-safe projection for audits and agent traces."""
    probe = gate.get("last_probe") if isinstance(gate.get("last_probe"), dict) else {}
    return {
        "status": str(gate.get("status") or "unknown"),
        "ready": bool(gate.get("ready")),
        "waited_seconds": round(float(gate.get("waited_seconds") or 0.0), 3),
        "poll_count": int(gate.get("poll_count") or 0),
        "max_wait_seconds": float(gate.get("max_wait_seconds") or 0.0),
        "pending_summaries": int(probe.get("pending_summaries") or 0),
        "in_flight_leases": int(probe.get("in_flight_leases") or 0),
        "evidence_count": int(probe.get("evidence_count") or 0),
        "reason_code": str(probe.get("reason_code") or "unknown"),
        "stale_processing": int(probe.get("stale_processing") or 0),
        "cohort_start": probe.get("cohort_start"),
        "cohort_end": probe.get("cohort_end"),
        "recovery_action": (
            "下一次日度补跑重新检查本轮证据；历史积压由摘要消费者独立处理" if not gate.get("ready") else ""
        ),
    }

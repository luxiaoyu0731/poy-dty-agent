#!/usr/bin/env python3
"""Consume the grounded summary queue with optional daily caps and bounded retries."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import ssl
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import certifi

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from scripts.runtime_guards import is_sqlite_contention  # noqa: E402

DeepSeekClient = importlib.import_module("app.deepseek_client").DeepSeekClient
news = importlib.import_module("app.news")
EVENT_SUMMARY_PROMPT_VERSION = news.EVENT_SUMMARY_PROMPT_VERSION
process_event_summary_queue = news.process_event_summary_queue
storage = importlib.import_module("app.storage")
enqueue_event_ai_summary = storage.enqueue_event_ai_summary
event_ai_summary_queue_counts = storage.event_ai_summary_queue_counts
recover_stale_event_ai_summary_leases = storage.recover_stale_event_ai_summary_leases
candidate_rows = importlib.import_module("scripts.backfill_event_deepseek_summaries").candidate_rows

DEFAULT_OUTPUT_DIR = Path(os.getenv("EVENT_SUMMARY_WORKER_OUTPUT_DIR", "/data/event-summary-worker"))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--poll-seconds", type=float, default=float(os.getenv("EVENT_SUMMARY_WORKER_POLL_SECONDS", "30"))
    )
    parser.add_argument("--batch-size", type=int, default=int(os.getenv("EVENT_SUMMARY_WORKER_BATCH_SIZE", "5")))
    parser.add_argument("--concurrency", type=int, default=int(os.getenv("EVENT_SUMMARY_WORKER_CONCURRENCY", "3")))
    parser.add_argument("--max-attempts", type=int, default=int(os.getenv("EVENT_SUMMARY_MAX_ATTEMPTS", "3")))
    parser.add_argument("--lease-minutes", type=int, default=int(os.getenv("EVENT_SUMMARY_LEASE_MINUTES", "30")))
    parser.add_argument("--queue-batch-size", type=int, default=int(os.getenv("EVENT_SUMMARY_QUEUE_BATCH_SIZE", "500")))
    parser.add_argument(
        "--daily-request-limit", type=int, default=int(os.getenv("EVENT_SUMMARY_DAILY_REQUEST_LIMIT", "50"))
    )
    parser.add_argument(
        "--daily-budget-rmb", type=float, default=float(os.getenv("EVENT_SUMMARY_DAILY_BUDGET_RMB", "10"))
    )
    parser.add_argument(
        "--max-cost-per-request-rmb",
        type=float,
        default=float(os.getenv("EVENT_SUMMARY_MAX_COST_PER_REQUEST_RMB", "0.20")),
    )
    parser.add_argument(
        "--request-interval-seconds",
        type=float,
        default=float(os.getenv("EVENT_SUMMARY_REQUEST_INTERVAL_SECONDS", "0.5")),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--index-refresh-seconds", type=float,
                        default=float(os.getenv("EVENT_SUMMARY_INDEX_REFRESH_SECONDS", "300")))
    args = parser.parse_args(argv)
    if min(args.batch_size, args.concurrency, args.max_attempts, args.queue_batch_size) < 1:
        parser.error("batch, concurrency, attempts and queue size must be positive")
    if args.poll_seconds < 0 or args.request_interval_seconds < 0 or args.index_refresh_seconds < 0:
        parser.error("intervals must be non-negative")
    if args.daily_request_limit < 0 or args.daily_budget_rmb < 0 or args.max_cost_per_request_rmb <= 0:
        parser.error("daily caps must be non-negative (0 disables a cap); maximum request cost must be positive")
    return args


def queue_eligible_summaries(model: str, limit: int) -> int:
    rows = candidate_rows(
        now=datetime.now(UTC),
        days=365,
        model=model,
        prompt_version=EVENT_SUMMARY_PROMPT_VERSION,
        limit=max(1, min(limit, 10_000)),
    )
    # Pending/failed items already belong to the consumer; do not rewrite their
    # timestamps (or contend with their processing leases) on every poll.
    rows = [row for row in rows if not (
        row.get("summary_status")
        and row.get("source_hash") == row.get("content_hash")
        and row.get("model") == model
        and row.get("prompt_version") == EVENT_SUMMARY_PROMPT_VERSION
    )]
    # The batched upsert can hit SQLITE_BUSY while the API/scheduler writes; a
    # bounded backoff retry keeps the worker alive instead of crash-looping.
    import sqlite3

    for attempt in range(3):
        try:
            for row in rows:
                enqueue_event_ai_summary(
                    str(row["article_id"]),
                    str(row["content_hash"]),
                    model,
                    EVENT_SUMMARY_PROMPT_VERSION,
                )
            return len(rows)
        except sqlite3.OperationalError as exc:
            if attempt == 2 or "locked" not in str(exc) and "busy" not in str(exc):
                raise
            time.sleep(2.0 * (attempt + 1))
    return 0


async def run_cycle(args: argparse.Namespace, *, client: DeepSeekClient, allowance: int) -> dict[str, Any]:
    # A deleted release can leave Python alive but remove certifi's bundle.
    # Fail before claiming any article so infrastructure failures don't consume
    # every article's retry allowance. launchd then restarts from current.
    if not Path(sys.prefix).is_dir() or not Path(__file__).is_file():
        raise RuntimeError("summary_worker_release_missing")
    ssl.create_default_context(cafile=certifi.where())
    recovered = recover_stale_event_ai_summary_leases(args.lease_minutes)
    queued = queue_eligible_summaries(client.model, args.queue_batch_size)
    before = event_ai_summary_queue_counts(args.max_attempts)
    limit = min(args.batch_size, max(0, allowance))
    if limit == 0:
        return {
            "status": "paused",
            "stopped": "daily_budget_or_request_limit",
            "recovered": recovered,
            "queued": queued,
            "before": before,
            "reserved_requests": 0,
        }
    set_attempt_budget = getattr(client, "set_http_attempt_budget", None)
    if callable(set_attempt_budget):
        set_attempt_budget(allowance)
    recovery_ids = storage.list_recoverable_event_summary_ids(limit=limit, include_missing_runtime=True)
    processed = await process_event_summary_queue(
        limit=limit,
        max_attempts=6 if recovery_ids else args.max_attempts,
        **({"article_ids": recovery_ids} if recovery_ids else {}),
        concurrency=args.concurrency,
        request_interval_seconds=args.request_interval_seconds,
        client=client,
    )
    provider_http_attempts = int(getattr(client, "http_attempts_used", processed.get("selected", 0)))
    return {
        "status": "completed",
        "recovered": recovered,
        "queued": queued,
        "before": before,
        "processed": processed,
        "recovery_article_ids": recovery_ids,
        "reserved_requests": provider_http_attempts,
        "provider_http_attempts": provider_http_attempts,
        "after": event_ai_summary_queue_counts(args.max_attempts),
    }


def _read_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def daily_allowance(args: argparse.Namespace, state: dict[str, Any]) -> int:
    today = datetime.now(UTC).date().isoformat()
    used = int(state.get("reserved_requests", 0)) if state.get("date") == today else 0
    # Explicit zero disables only that daily cap. Per-batch concurrency, retry
    # limits and provider-attempt accounting remain active in uncapped mode.
    by_requests = max(0, args.daily_request_limit - used) if args.daily_request_limit else sys.maxsize
    by_budget = (
        max(0, int(args.daily_budget_rmb / args.max_cost_per_request_rmb) - used)
        if args.daily_budget_rmb else sys.maxsize
    )
    return min(by_requests, by_budget)


def maintain_semantic_index(output_dir: Path, interval_seconds: float) -> dict[str, Any] | None:
    """Coalesce summary changes outside request handling; prune old generations.

    Rebuilds activate a new generation and then apply the retention policy
    (keep the newest 3 non-active generations, anchored pruning), recorded in
    the ``retention`` field of ``result`` and in the policy's audit log.
    """
    if interval_seconds <= 0:
        return None
    path = output_dir / "index-maintenance.json"
    previous = _read_state(path)
    now = datetime.now(UTC)
    try:
        elapsed = (now - datetime.fromisoformat(str(previous["checked_at"]))).total_seconds()
        if 0 <= elapsed < interval_seconds:
            return previous
    except (KeyError, ValueError, TypeError):
        pass
    report: dict[str, Any] = {"checked_at": now.isoformat(), "status": "checking"}
    # Persist the attempt before starting: a failed build must not spin on restart.
    _write_json(path, report)
    try:
        index = importlib.import_module("app.semantic_index")
        status = index.semantic_index_status()
        report["reason"] = status.get("stale_reason", "")
        if status.get("active_index") and status.get("stale_reason") in {
            "grounded_summary_changed", "snapshot_expired", "source_time_policy_changed",
        }:
            report["result"] = index.rebuild_semantic_index()
            report["status"] = str(report["result"].get("status", "failed"))
        else:
            report["status"] = "not_needed" if status.get("status") == "ready" else "requires_review"
    except Exception as exc:  # noqa: BLE001 -- index recovery must not stop summary consumption
        report.update(status="failed", error_type=type(exc).__name__)
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["elapsed_seconds"] = round((datetime.now(UTC) - now).total_seconds(), 3)
    report["refresh_interval_seconds"] = interval_seconds
    report["retention"] = "non_active_keep_3_anchored"
    _write_json(path, report)
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    state_path = args.output_dir / "daily-budget.json"
    client = DeepSeekClient()
    consecutive_contentions = 0
    while True:
        state = _read_state(state_path)
        allowance = daily_allowance(args, state)
        today = datetime.now(UTC).date().isoformat()
        prior = int(state.get("reserved_requests", 0)) if state.get("date") == today else 0

        def persist_http_attempt(
            cycle_attempts: int,
            *,
            budget_date: str = today,
            prior_reserved: int = prior,
        ) -> None:
            _write_json(
                state_path,
                {
                    "date": budget_date,
                    "reserved_requests": prior_reserved + cycle_attempts,
                },
            )

        client.http_attempt_callback = persist_http_attempt
        try:
            result = asyncio.run(run_cycle(args, client=client, allowance=allowance))
        except Exception as exc:
            if not is_sqlite_contention(exc):
                raise
            consecutive_contentions += 1
            # Provider reservations are durable before each HTTP attempt. A
            # failed database write after a model call must not refund usage,
            # nor reuse the client's previous cycle attempt counter.
            persisted = _read_state(state_path)
            used = int(persisted.get("reserved_requests", prior)) if persisted.get("date") == today else prior
            result = {
                "status": "locked", "error_code": "sqlite_contention", "retryable": True,
                "consecutive_contentions": consecutive_contentions,
                "provider_http_attempts": max(0, used - prior),
                "retry_after_seconds": max(30.0, args.poll_seconds),
            }
        else:
            consecutive_contentions = 0
        reserved = int(result.get("provider_http_attempts", result.get("processed", {}).get("selected", 0)))
        _write_json(state_path, {"date": today, "reserved_requests": prior + reserved})
        result["generated_at"] = datetime.now(UTC).isoformat()
        result["daily_reserved_requests"] = prior + reserved
        result["daily_limits"] = {
            "requests": args.daily_request_limit or None, "budget_rmb": args.daily_budget_rmb or None,
        }
        _write_json(args.output_dir / "latest.json", result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result.get("status") == "locked":
            if args.once or consecutive_contentions >= 3:
                return 75
            time.sleep(result["retry_after_seconds"])
            continue
        maintain_semantic_index(args.output_dir, args.index_refresh_seconds)
        if args.once:
            return 0
        time.sleep(max(1.0, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())

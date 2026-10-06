from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

DeepSeekClient = importlib.import_module("app.deepseek_client").DeepSeekClient
news = importlib.import_module("app.news")
EVENT_SUMMARY_PROMPT_VERSION = news.EVENT_SUMMARY_PROMPT_VERSION
RawNewsItem = news.RawNewsItem
classify_summary_input = news.classify_summary_input
get_news_source = news.get_news_source
process_event_summary_queue = news.process_event_summary_queue
settings = importlib.import_module("app.settings").settings
storage = importlib.import_module("app.storage")
connect = storage.connect
enqueue_event_ai_summary = storage.enqueue_event_ai_summary
event_ai_summary_queue_counts = storage.event_ai_summary_queue_counts
recover_stale_event_ai_summary_leases = storage.recover_stale_event_ai_summary_leases

DEFAULT_BACKUP_DIR = SERVER_ROOT.parent / ".codex-run" / "db-backups"
LOW_INFORMATION_PHRASES = (
    "原文未提供",
    "原文未说明",
    "仅提供标题",
    "无法摘要",
    "原文未给出",
    "原文内容缺失",
)


def _source_metadata(raw: object) -> dict:
    try:
        metadata = raw if isinstance(raw, dict) else json.loads(str(raw or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return metadata if isinstance(metadata, dict) else {}


def _source_grade(raw: object) -> str:
    metadata = _source_metadata(raw)
    source_content = metadata.get("source_content", {}) if isinstance(metadata, dict) else {}
    summary_input = metadata.get("summary_input_quality", {}) if isinstance(metadata, dict) else {}
    return str(
        (source_content.get("status") if isinstance(source_content, dict) else "")
        or (summary_input.get("level") if isinstance(summary_input, dict) else "")
        or "unknown"
    )


def select_candidates(
    rows: list[dict[str, object]],
    *,
    now: datetime,
    days: int,
    model: str,
    prompt_version: str,
    retry_rejected: bool = False,
) -> list[dict[str, object]]:
    cutoff = now.astimezone(UTC) - timedelta(days=max(1, min(days, 365)))
    selected: list[dict[str, object]] = []
    for row in rows:
        published = str(row.get("published_at") or row.get("first_seen_at") or row.get("created_at") or "")
        try:
            timestamp = datetime.fromisoformat(published.replace("Z", "+00:00")).astimezone(UTC)
        except ValueError:
            try:
                timestamp = parsedate_to_datetime(published).astimezone(UTC)
            except (TypeError, ValueError):
                continue
        if timestamp < cutoff or timestamp > now.astimezone(UTC):
            continue
        summary_input = _source_metadata(row.get("raw")).get("summary_input_quality", {})
        if isinstance(summary_input, dict) and summary_input.get("summary_blocked_reason") not in {
            None, "", "body_exceeds_single_summary_window",
        }:
            # Ignore the retired local length gate. Other explicit blockers
            # remain effective; normal revision/retry rules still apply below.
            continue
        source_grade = _source_grade(row.get("raw"))
        effective_grade = str(row.get("input_quality") or "") if source_grade == "unknown" else source_grade
        if effective_grade != "full_text":  # noqa: SIM102 -- narrow, short-circuited layout reclassification
            # Re-evaluate only the newly supported official short-article layout;
            # old stored length-only classifications must not strand these rows.
            if row.get("source_id") != "ndrc_news" or not news._verified_short_official(RawNewsItem(
                source_id="ndrc_news", tier=str(row.get("tier") or "A"),
                url=str(row.get("url") or ""), title=str(row.get("title") or ""),
                raw_text=str(row.get("raw_text") or ""),
            )):
                continue
        # Prompt upgrades must not erase already usable same-source results.
        # Rechecking irrelevant/impact-rejected history uses an explicit manifest.
        if (row.get("summary_status") == "completed"
                and row.get("source_hash") == row.get("content_hash")):
            continue
        if row.get("summary_status") == "processing":
            continue
        same_revision = (
            str(row.get("source_hash") or "") == str(row.get("content_hash") or "")
            and str(row.get("model") or "") == model
            and str(row.get("prompt_version") or "") == prompt_version
        )
        status = str(row.get("summary_status") or "")
        current = same_revision and (status == "completed" or (status == "rejected" and not retry_rejected))
        if not current:
            selected.append(row)
    return sorted(selected, key=lambda row: str(row.get("published_at") or ""), reverse=True)


def candidate_rows(
    *, now: datetime, days: int, model: str, prompt_version: str, limit: int, retry_rejected: bool = False
) -> list[dict[str, object]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """SELECT a.article_id,a.content_hash,a.published_at,a.first_seen_at,a.created_at,a.raw,
                      a.source_id,a.tier,a.url,a.title,
                      CASE WHEN a.source_id='ndrc_news' THEN a.raw_text ELSE '' END AS raw_text,
                      s.summary_status,s.source_hash,s.model,s.prompt_version,s.input_quality
               FROM news_articles a LEFT JOIN event_ai_summaries s USING(article_id)
               ORDER BY COALESCE(NULLIF(a.published_at,''),a.first_seen_at,a.created_at) DESC"""
        ).fetchall()
    return select_candidates(
        [dict(row) for row in rows],
        now=now,
        days=days,
        model=model,
        prompt_version=prompt_version,
        retry_rejected=retry_rejected,
    )[:limit]


def requeue_rejected_candidate(article_id: str, *, source_hash: str, model: str, prompt_version: str) -> bool:
    """Explicitly reopen one rejected result without weakening normal idempotency."""
    with closing(connect()) as connection, connection:
        cursor = connection.execute(
            """UPDATE event_ai_summaries
               SET factual_summary='',summary_status='pending',quality_status='pending',
                   quality_reasons='[]',fact_payload='{}',business_impact_payload='{}',
                   attempts=0,error='',updated_at=?
               WHERE article_id=? AND summary_status='rejected'
                 AND source_hash=? AND model=? AND prompt_version=?""",
            (
                datetime.now(UTC).isoformat(),
                article_id,
                source_hash,
                model,
                prompt_version,
            ),
        )
    return cursor.rowcount == 1


def run_usage(article_ids: list[str], *, prompt_version: str, started_at: str) -> tuple[int, int]:
    """Return only usage produced by this run's bounded candidate set."""
    if not article_ids:
        return 0, 0
    placeholders = ",".join("?" for _ in article_ids)
    with closing(connect()) as connection, connection:
        row = connection.execute(
            f"""SELECT COALESCE(SUM(input_chars),0),COALESCE(SUM(output_chars),0)
                FROM event_ai_summaries
                WHERE article_id IN ({placeholders}) AND summary_status='completed'
                  AND prompt_version=? AND updated_at>=?""",
            (*article_ids, prompt_version, started_at),
        ).fetchone()
    return int(row[0]), int(row[1])


def create_backup(database: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = (
        backup_dir / f"{database.name}.pre_event_summary_backfill_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.sqlite"
    )
    with (
        closing(sqlite3.connect(database)) as source,
        source,
        closing(sqlite3.connect(target)) as destination,
        destination,
    ):
        source.backup(destination)
    with closing(sqlite3.connect(f"file:{target}?mode=ro", uri=True)) as connection, connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"backup integrity check failed: {integrity}")
    return target


def degradation_decision(row: dict[str, object]) -> tuple[str, list[str]]:
    """Return the canonical input grade and reasons for hiding a legacy summary."""
    source_id = str(row.get("source_id") or "")
    source = get_news_source(source_id)
    quality = classify_summary_input(
        RawNewsItem(
            source_id=source_id,
            tier=source.tier if source else str(row.get("tier") or "C"),
            url=str(row.get("url") or ""),
            title=str(row.get("title") or ""),
            published_at=str(row.get("published_at") or ""),
            raw_text=str(row.get("raw_text") or ""),
            language=str(row.get("language") or "unknown"),
        ),
        source=source,
    )
    grade = str(quality["level"])
    summary = str(row.get("factual_summary") or "")
    reasons: list[str] = []
    if grade != "full_text":
        reasons.append("input_not_full_text")
    if any(phrase in summary for phrase in LOW_INFORMATION_PHRASES):
        reasons.append("legacy_low_information_summary")
    if str(row.get("quality_status") or "pending") != "usable":
        reasons.append("legacy_unverified_quality_gate")
    return grade, reasons


def degrade_existing_summaries() -> int:
    """Hide legacy low-information summaries after the caller has created a backup."""
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """SELECT a.article_id,a.source_id,a.tier,a.url,a.title,a.published_at,a.raw_text,a.language,
                      s.factual_summary,s.summary_status,s.quality_status
               FROM news_articles a JOIN event_ai_summaries s USING(article_id)
               WHERE s.summary_status='completed'"""
        ).fetchall()
        updates: list[tuple[str, str, str, str]] = []
        for raw_row in rows:
            row = dict(raw_row)
            grade, reasons = degradation_decision(row)
            if reasons:
                updates.append(
                    (
                        grade,
                        json.dumps(reasons, ensure_ascii=False),
                        datetime.now(UTC).isoformat(),
                        str(row["article_id"]),
                    )
                )
        connection.executemany(
            """UPDATE event_ai_summaries
               SET factual_summary='',summary_status='rejected',quality_status='rejected',
                   quality_reasons=?,input_quality=?,updated_at=? WHERE article_id=?""",
            [(reasons, grade, updated_at, article_id) for grade, reasons, updated_at, article_id in updates],
        )
    return len(updates)


def queue_existing(limit: int) -> int:
    client = DeepSeekClient()
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """SELECT a.article_id,a.content_hash
               FROM news_articles a
               LEFT JOIN event_ai_summaries s USING(article_id)
               WHERE s.article_id IS NULL
               ORDER BY COALESCE(NULLIF(a.published_at,''),a.first_seen_at,a.created_at) DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    for row in rows:
        enqueue_event_ai_summary(
            str(row["article_id"]),
            str(row["content_hash"]),
            client.model,
            EVENT_SUMMARY_PROMPT_VERSION,
        )
    return len(rows)


def counts(max_attempts: int = 3) -> dict[str, int]:
    return event_ai_summary_queue_counts(max_attempts)


async def run(args: argparse.Namespace) -> dict[str, object]:
    before = counts(args.max_attempts)
    client = DeepSeekClient()
    now = datetime.fromisoformat(args.as_of.replace("Z", "+00:00")) if args.as_of else datetime.now(UTC)
    candidates = candidate_rows(
        now=now,
        days=args.days,
        model=client.model,
        prompt_version=EVENT_SUMMARY_PROMPT_VERSION,
        limit=args.limit,
        retry_rejected=args.retry_rejected,
    )
    if not args.apply:
        return {
            "mode": "dry_run",
            "before": before,
            "requested_limit": args.limit,
            "days": args.days,
            "candidate_count": len(candidates),
            "candidate_ids": [str(row["article_id"]) for row in candidates],
            "writes_database": False,
            "provider_calls": False,
        }
    database = Path(settings.sqlite_path).expanduser().resolve()
    backup_path = create_backup(database, args.backup_dir.expanduser().resolve())
    remediated_total = degrade_existing_summaries() if args.degrade_legacy else 0
    recovered_total = recover_stale_event_ai_summary_leases(args.lease_minutes)
    for row in candidates:
        enqueue_event_ai_summary(
            str(row["article_id"]), str(row["content_hash"]), client.model, EVENT_SUMMARY_PROMPT_VERSION
        )
        if args.retry_rejected:
            requeue_rejected_candidate(
                str(row["article_id"]),
                source_hash=str(row["content_hash"]),
                model=client.model,
                prompt_version=EVENT_SUMMARY_PROMPT_VERSION,
            )
    queued_total = len(candidates)
    if args.queue_only:
        return {
            "mode": "queue_only",
            "before": before,
            "queued": queued_total,
            "remediated_existing": remediated_total,
            "recovered": recovered_total,
            "backup_path": str(backup_path),
            "after": counts(args.max_attempts),
        }
    completed_total = failed_total = 0
    estimated_input_tokens = estimated_output_tokens = 0
    batches = 0
    started = time.monotonic()
    started_at = datetime.now(UTC).isoformat()
    candidate_ids = [str(row["article_id"]) for row in candidates]
    while True:
        current = counts(args.max_attempts)
        if current.get("not_queued", 0) <= 0 and current.get("pending", 0) <= 0 and current.get("failed", 0) <= 0:
            break
        # Reserve the configured worst-case completion cost before making calls so the hard cap cannot be crossed.
        projected_batch_cost = (
            args.batch_size
            * (
                args.max_input_tokens * args.input_price_per_million
                + args.max_output_tokens * args.output_price_per_million
            )
            / 1_000_000
        )
        spent = (
            estimated_input_tokens * args.input_price_per_million
            + estimated_output_tokens * args.output_price_per_million
        ) / 1_000_000
        if spent + projected_batch_cost > args.budget_rmb:
            return {
                "mode": "apply",
                "stopped": "budget_guard",
                "budget_rmb": args.budget_rmb,
                "estimated_spend_rmb": round(spent, 4),
                "batches": batches,
                "after": current,
            }
        recovered_total += recover_stale_event_ai_summary_leases(args.lease_minutes)
        result = await process_event_summary_queue(
            limit=args.batch_size,
            max_attempts=args.max_attempts,
            concurrency=args.concurrency,
            request_interval_seconds=args.interval_seconds,
            article_ids=candidate_ids,
        )
        completed_total += result["completed"]
        failed_total += result["failed"]
        # Conservative token estimate for Chinese/mixed text; pricing remains CLI-configurable.
        estimated_input_tokens, estimated_output_tokens = run_usage(
            candidate_ids,
            prompt_version=EVENT_SUMMARY_PROMPT_VERSION,
            started_at=started_at,
        )
        batches += 1
        progress = {
            "batch": batches,
            "queued": queued_total,
            "completed": completed_total,
            "failed": failed_total,
            "counts": counts(args.max_attempts),
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "estimated_spend_rmb": round(
                (
                    estimated_input_tokens * args.input_price_per_million
                    + estimated_output_tokens * args.output_price_per_million
                )
                / 1_000_000,
                4,
            ),
            "elapsed_seconds": round(time.monotonic() - started, 1),
        }
        print(json.dumps(progress, ensure_ascii=False), flush=True)
        if not args.continuous:
            break
        if result["selected"] == 0:
            break
    return {
        "mode": "apply",
        "before": before,
        "queued": queued_total,
        "backup_path": str(backup_path),
        "remediated_existing": remediated_total,
        "recovered": recovered_total,
        "processed": {"completed": completed_total, "failed": failed_total},
        "after": counts(args.max_attempts),
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Queue and backfill DeepSeek factual summaries for source events.")
    parser.add_argument("--apply", action="store_true", help="Actually queue and call DeepSeek; default is dry-run.")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--days", type=int, default=14, choices=range(1, 366), metavar="1..365")
    parser.add_argument("--as-of", default="", help="UTC ISO timestamp used for deterministic dry-run audits.")
    parser.add_argument("--limit", type=int, default=3, choices=range(1, 101), metavar="1..100")
    parser.add_argument("--max-attempts", type=int, default=3, choices=range(1, 6), metavar="1..5")
    parser.add_argument(
        "--continuous", action="store_true", help="Continue batches until drained or budget guard stops the worker."
    )
    parser.add_argument("--queue-only", action="store_true", help="Queue all missing rows without calling DeepSeek.")
    parser.add_argument(
        "--retry-rejected",
        action="store_true",
        help=(
            "Explicitly reset current-version rejected candidates to pending; disabled by default to prevent repeated"
            " spend."
        ),
    )
    parser.add_argument(
        "--degrade-legacy",
        action="store_true",
        help="Explicitly re-audit and hide legacy completed summaries before backfill; never enabled by default.",
    )
    parser.add_argument("--queue-batch-size", type=int, default=500)
    parser.add_argument("--lease-minutes", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=20, choices=range(1, 101), metavar="1..100")
    parser.add_argument("--concurrency", type=int, default=2, choices=range(1, 9), metavar="1..8")
    parser.add_argument("--interval-seconds", type=float, default=0.5)
    parser.add_argument("--budget-rmb", type=float, default=320.0)
    parser.add_argument(
        "--input-price-per-million",
        type=float,
        default=-1.0,
        help="DeepSeek input price in RMB per million tokens; required with --apply.",
    )
    parser.add_argument(
        "--output-price-per-million",
        type=float,
        default=-1.0,
        help="DeepSeek output price in RMB per million tokens; required with --apply.",
    )
    parser.add_argument("--max-input-tokens", type=int, default=12000)
    parser.add_argument("--max-output-tokens", type=int, default=1000)
    args = parser.parse_args()
    if args.apply and (args.input_price_per_million < 0 or args.output_price_per_million < 0):
        parser.error("--apply requires non-negative --input-price-per-million and --output-price-per-million")
    if args.budget_rmb <= 0 or args.interval_seconds < 0:
        parser.error("budget must be positive and interval must be non-negative")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

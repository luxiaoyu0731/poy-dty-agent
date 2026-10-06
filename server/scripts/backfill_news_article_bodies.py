"""Bounded recovery of recent article bodies using the existing guarded fetcher.

Dry-run is read-only. Apply keeps discovery times and existing attempt budgets;
state/journal are sidecars, with no schema migration or historical bulk replay.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app import news  # noqa: E402
from app.publication_time import publication_instant  # noqa: E402
from app.settings import settings  # noqa: E402


def candidates(db: Path, state: dict, now: datetime, limit: int) -> list[dict]:
    selected = []
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        # Stream the bounded seven-day window. A prefix of 500 terminal or
        # complete records must not permanently starve older eligible articles.
        rows = con.execute(
            "SELECT * FROM news_articles WHERE julianday(first_seen_at)>=julianday(?) "
            "ORDER BY first_seen_at DESC, article_id DESC",
            ((now - timedelta(days=7)).isoformat(),),
        )
        for row in rows:
            row = dict(row)
            # Selection and recovery share historical source identity handling;
            # resolving an identity does not grant outbound or access permission.
            source = source_for(row)
            if not source:
                continue
            item = item_for(row)
            if news.classify_summary_input(item, source=source)["level"] == "full_text":
                continue
            previous = state.get(row["article_id"], {})
            if previous.get("content_hash") == row["content_hash"]:
                permission_restored = (
                    previous.get("reason") == "source_not_enabled"
                    and news._should_fetch_detail(item, source=source)
                )
                if (previous.get("terminal") and not permission_restored) or previous.get("attempts", 0) >= 3:
                    continue
                last = publication_instant(previous.get("attempted_at"))
                if last and now - datetime.fromisoformat(last) < timedelta(hours=1):
                    continue
            if news._id("art", item.discovery_url or item.url or item.title) != row["article_id"]:
                continue  # never generate a second identity for legacy imports
            selected.append(row)
            if len(selected) >= max(1, min(limit, 20)):
                break
    return selected


def source_for(row: dict):
    source = news.get_news_source(row["source_id"])
    if source is not None:
        return source
    # Historical public-news imports used these exact discovery identities.
    # Reuse the existing discovery/auth/host guards, without scheduling them.
    from app.models import NewsSource
    from scripts.public_news_backfill_v2 import QUERY_GROUPS

    for group in QUERY_GROUPS:
        if row["source_id"] in {group.source_id, group.source_id.replace("gdelt_v2_", "google_news_v2_")}:
            return NewsSource(source_id=row["source_id"], source_name="历史公开新闻索引",
                              tier="C", url="https://news.google.com/rss/search",
                              category=group.category, fetcher="rss", cadence="archive")
    return None


def item_for(row: dict) -> news.RawNewsItem:
    raw = json.loads(row.get("raw") or "{}")
    return news.RawNewsItem(
        source_id=row["source_id"],
        tier=row["tier"],
        url=row.get("canonical_url") or row["url"],
        title=row["title"],
        published_at=row["published_at"],
        first_seen_at=row["first_seen_at"],
        raw_text=row["raw_text"],
        language=row.get("language") or "unknown",
        discovery_url=raw.get("discovery_url") or (
            row["url"] if row.get("canonical_url") and row["canonical_url"] != row["url"] else ""
        ),
        discovery_timestamp=raw.get("discovery_timestamp") or "",
        **news.stored_body_fields(row),
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


async def persist_recovered_body(item, *, source, row: dict) -> dict:
    """Retry only transient SQLite contention, reusing the acquired body.

    A preceding attempt may have committed the article before a derived write
    failed. Accept only that exact recovered hash; never overwrite a third-party
    revision or refetch the publisher as part of a database retry.
    """
    prepared = news.prepare_article_body(item)
    target_hash = news._hash(f"{prepared.title}\n{prepared.raw_text}")
    for attempt in range(3):
        try:
            return news.ingest_news_items(
                [replace(item, first_seen_at=row["first_seen_at"])], source=source,
                expected_content_hashes={row["article_id"]: row["content_hash"]},
            )
        except sqlite3.OperationalError as exc:
            if not any(word in str(exc).lower() for word in ("locked", "busy")) or attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))
            with closing(sqlite3.connect(f"file:{settings.sqlite_path}?mode=ro", uri=True)) as con:
                current = con.execute(
                    "SELECT content_hash FROM news_articles WHERE article_id=?", (row["article_id"],)
                ).fetchone()
            if current and current[0] == target_hash:
                return {"articles_found": 1}
            if not current or current[0] != row["content_hash"]:
                raise ValueError("source_changed_during_recovery") from exc
    raise AssertionError("unreachable")


async def recover(rows: list[dict], state: dict, output: Path, concurrency: int) -> list[dict]:
    sem = asyncio.Semaphore(max(1, min(concurrency, 2)))

    async def one(row):
        async with sem:
            source = source_for(row)
            old = state.get(row["article_id"], {})
            attempts = old.get("attempts", 0) if old.get("content_hash") == row["content_hash"] else 0
            status = {
                "content_hash": row["content_hash"],
                "attempts": attempts + 1,
                "attempted_at": datetime.now(UTC).isoformat(),
                "terminal": False,
            }
            state[row["article_id"]] = status
            write_json(output.with_name("body-acquisition-state.json"), state)
            try:
                if source is None:
                    raise ValueError("unknown_historical_source")
                prepared = news.prepare_article_body(item_for(row))
                _errors = []
                if news.classify_summary_input(prepared, source=source)["eligible_for_summary"]:
                    items = [prepared]  # complete stored publisher page; no refetch needed
                else:
                    items, _errors = await asyncio.wait_for(
                        news._enrich_items_with_details([item_for(row)], source=source), 40
                    )
                item = items[0]
                quality = news.classify_summary_input(item, source=source)
                status["reason"] = item.detail_reason or quality["reason"]
                status["resolved_url"] = item.url
                status["discovery_url"] = item.discovery_url
                status["fetch_errors"] = _errors
                status["terminal"] = item.detail_reason in {
                    "access_restricted",
                    "source_not_enabled",
                    "unsupported_document",
                }
                status["status"] = "external_wait" if status["terminal"] else "pending"
                if item.detail_reason == "source_not_enabled":
                    status["status"] = "configuration_blocked"
                if quality["level"] == "full_text" and not item.detail_reason:
                    # Preserve a rollback journal before any existing-body change.
                    journal = (
                        output.parent / "body-recovery-journal" / f"{row['article_id']}-{row['content_hash'][:16]}.json"
                    )
                    with closing(sqlite3.connect(f"file:{settings.sqlite_path}?mode=ro", uri=True)) as con:
                        con.row_factory = sqlite3.Row
                        con.execute("PRAGMA query_only=ON")
                        current = con.execute(
                            "SELECT * FROM news_articles WHERE article_id=?", (row["article_id"],)
                        ).fetchone()
                        summary = con.execute(
                            "SELECT * FROM event_ai_summaries WHERE article_id=?", (row["article_id"],)
                        ).fetchone()
                    if not current or current["content_hash"] != row["content_hash"]:
                        status.update(status="pending", reason="source_changed_during_recovery")
                    elif summary and summary["summary_status"] == "processing":
                        status.update(status="pending", reason="summary_in_progress")
                    else:
                        if not journal.exists():
                            write_json(journal, {
                                "article": dict(current), "summary": dict(summary) if summary else None,
                            })
                        result = await persist_recovered_body(item, source=source, row=row)
                        with closing(sqlite3.connect(f"file:{settings.sqlite_path}?mode=ro", uri=True)) as con:
                            persisted = con.execute(
                                "SELECT content_hash FROM news_articles WHERE article_id=?", (row["article_id"],)
                            ).fetchone()
                        prepared = news.prepare_article_body(item)
                        expected_hash = news._hash(f"{prepared.title}\n{prepared.raw_text}")
                        if result["articles_found"] and persisted and persisted[0] == expected_hash:
                            status.update(status="completed", terminal=True, recovered_content_hash=expected_hash)
                        else:
                            status.update(status="failed", reason="body_not_persisted")
            except Exception as exc:
                status.update(status="failed", reason=type(exc).__name__)
                if isinstance(exc, ValueError) and str(exc) in {
                    "recovery_article_not_in_manifest", "article_changed_during_body_recovery",
                    "source_changed_during_recovery",
                }:
                    status["reason"] = str(exc)
                if isinstance(exc, sqlite3.OperationalError):
                    status["sqlite_errorname"] = getattr(exc, "sqlite_errorname", "unknown")
            state[row["article_id"]] = status
            write_json(output.with_name("body-acquisition-state.json"), state)
            return {"article_id": row["article_id"], "source_id": row["source_id"], **status}

    return await asyncio.gather(*(one(row) for row in rows))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    with args.lock.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        state_path = args.output.with_name("body-acquisition-state.json")
        # Corrupt retry state must fail closed, not silently reset all budgets.
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        now = datetime.now(UTC)
        rows = candidates(args.db, state, now, args.limit)
        results = []
        if args.apply:
            object.__setattr__(settings, "sqlite_path", str(args.db))
            results = asyncio.run(recover(rows, state, args.output, args.concurrency))
            write_json(state_path, state)
        write_json(
            args.output,
            {
                "generated_at": now.isoformat(),
                "mode": "apply" if args.apply else "dry_run",
                "candidate_ids": [r["article_id"] for r in rows],
                "results": results,
                "status": "completed" if args.apply else "planned",
            },
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Manifest-scoped historical recovery. No model calls: use the existing worker.

Plan is read-only. Apply performs one guarded body attempt per article, archives
old records and queues a new summary revision. Status tracks real worker results.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import news, storage  # noqa: E402
from app.settings import settings  # noqa: E402
from app.summary_revision_archive import archive_summary_revision  # noqa: E402
from scripts.backfill_news_article_bodies import item_for, recover, source_for  # noqa: E402


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_private(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp")
    fd = os.open(temp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def read_rows(db: Path, article_id: str | None = None) -> list[dict]:
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        con.execute("BEGIN")
        clause, params = (" WHERE article_id=?", (article_id,)) if article_id else ("", ())
        articles = con.execute(
            "SELECT * FROM news_articles" + clause + " ORDER BY article_id LIMIT 20001", params
        ).fetchall()
        if len(articles) > 20000:
            raise ValueError("recovery_inventory_cap_exceeded")
        summaries = {r["article_id"]: dict(r) for r in con.execute("SELECT * FROM event_ai_summaries" + clause, params)}
        return [{"article": dict(a), "summary": summaries.get(a["article_id"])} for a in articles]


def decision(record: dict) -> tuple[str, str]:
    article, summary = record["article"], record.get("summary") or {}
    if (
        summary.get("summary_status") == "completed"
        and summary.get("impact_analysis_status") == "completed"
        and summary.get("source_hash") == article["content_hash"]
    ):
        return "preserve_related", ""
    source = source_for(article)
    if not source:
        return "source_review", "unknown_historical_source"
    item = item_for(article)
    quality = news.classify_summary_input(item, source=source)
    if quality["eligible_for_summary"]:
        return "summary_ready", str(quality["reason"])
    if news._id("art", item.discovery_url or item.url or item.title) != article["article_id"]:
        return "identity_review", "article_identity_mismatch"
    return "body_recovery", str(quality["reason"])


def make_plan(records: list[dict]) -> dict:
    items = []
    for record in records:
        article = record["article"]
        action, reason = decision(record)
        items.append(
            dict(
                article_id=article["article_id"],
                source_id=article["source_id"],
                source_hash=article["content_hash"],
                summary_hash=digest(record.get("summary")),
                action=action,
                reason=reason,
            )
        )
    plan = {
        "created_at": datetime.now(UTC).isoformat(),
        "prompt_version": news.EVENT_SUMMARY_PROMPT_VERSION,
        "items": items,
        "counts": dict(Counter(i["action"] for i in items)),
    }
    plan["sha256"] = digest(plan)
    return plan


def validate_plan(plan: dict) -> None:
    payload = {k: v for k, v in plan.items() if k != "sha256"}
    if digest(payload) != plan["sha256"] or plan["prompt_version"] != news.EVENT_SUMMARY_PROMPT_VERSION:
        raise ValueError("recovery_plan_mismatch")
    if len({i["article_id"] for i in plan["items"]}) != len(plan["items"]):
        raise ValueError("duplicate_recovery_article")


def worker_result(record: dict, target_hash: str) -> str:
    summary = record.get("summary") or {}
    if summary.get("source_hash") != target_hash or summary.get("prompt_version") != news.EVENT_SUMMARY_PROMPT_VERSION:
        return "revision_changed"
    status = summary.get("summary_status")
    if status == "completed":
        return "completed_" + str(summary.get("impact_analysis_status"))
    if status in {"rejected", "failed"} and summary.get("attempts", 0) >= 3:
        return str(status)
    return "queued"


async def apply_batch(db: Path, plan: dict, directory: Path, *, limit: int, model: str) -> dict:
    validate_plan(plan)
    state_path = directory / "recovery-state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"plan_sha256": plan["sha256"], "items": {}}
    if state["plan_sha256"] != plan["sha256"]:
        raise ValueError("recovery_state_plan_mismatch")
    body_state_path = directory / "body-acquisition-state.json"
    body_state = json.loads(body_state_path.read_text()) if body_state_path.exists() else {}
    used = 0
    # Eligible facts first: slow external pages cannot starve ready summaries.
    ordered = sorted(plan["items"], key=lambda i: i["action"] != "summary_ready")
    for item in ordered:
        article_id = item["article_id"]
        previous = state["items"].get(article_id)
        if previous and previous["status"] != "queued":
            continue
        records = read_rows(db, article_id)
        if not records:
            state["items"][article_id] = {"status": "record_missing"}
            continue
        record = records[0]
        if previous:
            previous["status"] = worker_result(record, previous["target_hash"])
            previous["checked_at"] = datetime.now(UTC).isoformat()
            continue
        if item["action"] not in {"summary_ready", "body_recovery"}:
            state["items"][article_id] = {"status": item["action"], "reason": item["reason"]}
            continue
        if used >= max(1, min(limit, 100)):
            continue
        used += 1
        article, summary = record["article"], record.get("summary")
        current_version = bool(summary and summary.get("prompt_version") == news.EVENT_SUMMARY_PROMPT_VERSION)
        # The normal worker can upgrade/reject a legacy truncated body before
        # this manifest reaches it. A changed summary alone does not satisfy the
        # approved body-recovery action. Keep source CAS and archive the latest
        # summary; never take an active processing lease away from the worker.
        needs_original_body = (
            item["action"] == "body_recovery"
            and article["content_hash"] == item["source_hash"]
            and current_version
        )
        if needs_original_body and summary["summary_status"] == "processing":
            continue
        if article["content_hash"] != item["source_hash"] or (
            digest(summary) != item["summary_hash"] and not needs_original_body
        ):
            # Normal worker may have completed the new version between plan/apply.
            observed = worker_result(record, article["content_hash"])
            current_summary = record.get("summary") or {}
            tracked = current_summary.get("prompt_version") == news.EVENT_SUMMARY_PROMPT_VERSION
            state["items"][article_id] = {
                "status": observed if tracked else "concurrent_change",
                "target_hash": article["content_hash"],
            }
            continue
        if summary and summary["summary_status"] == "processing":
            continue
        journal = directory / "before" / f"{digest(record)}.json"
        if not journal.exists():
            write_private(journal, record)
        if item["action"] == "body_recovery":
            # Reuse persisted attempt state after interruption, never reset it.
            body_attempt = body_state.get(article_id, {})
            persistence_retry = (
                body_attempt.get("reason") in {
                    "OperationalError", "InterruptedError", "discovery_identity_repaired", "publisher_layout_repaired",
                }
                and body_attempt.get("attempts", 0) < 3
            )
            if not body_attempt.get("attempts") or persistence_retry:
                await recover([article], body_state, directory / "body-recovery.json", concurrency=1)
            record = read_rows(db, article_id)[0]
            article = record["article"]
            if decision(record)[0] == "preserve_related":
                state["items"][article_id] = {"status": "preserve_related"}
                write_private(state_path, state)
                continue
            quality = news.classify_summary_input(item_for(article), source=source_for(article))
            if not quality["eligible_for_summary"]:
                persistence_failed = body_state.get(article_id, {}).get("reason") == "OperationalError"
                state["items"][article_id] = {
                    "status": "recovery_failed" if persistence_failed else "body_unavailable",
                    "reason": body_state.get(article_id, {}).get("reason", quality["reason"]),
                }
                write_private(state_path, state)
                continue
        # Snapshot every displaced result. The storage transaction also archives
        # atomically before mutation and refuses an active processing lease.
        if record.get("summary"):
            archive_summary_revision(str(db), record["summary"])
        storage.enqueue_event_ai_summary(
            article_id,
            article["content_hash"],
            model,
            news.EVENT_SUMMARY_PROMPT_VERSION,
            recheck_completed=True,
            expected_revision_hash=digest(record.get("summary")),
        )
        after = read_rows(db, article_id)[0]
        state["items"][article_id] = {
            "status": worker_result(after, article["content_hash"]),
            "target_hash": article["content_hash"],
            "journal": str(journal),
        }
        write_private(state_path, state)
    state["updated_at"] = datetime.now(UTC).isoformat()
    state["counts"] = dict(Counter(x["status"] for x in state["items"].values()))
    state["not_attempted"] = len(plan["items"]) - len(state["items"])
    write_private(state_path, state)
    return state


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if not args.apply:
        if args.plan.exists():
            raise ValueError("refusing_to_replace_frozen_plan")
        plan = make_plan(read_rows(args.db))
        write_private(args.plan, plan)
        print(json.dumps(plan["counts"]))
        return 0
    object.__setattr__(settings, "sqlite_path", str(args.db))
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.output_dir / "recovery.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        from app.deepseek_client import DeepSeekClient

        state = asyncio.run(
            apply_batch(
                args.db,
                json.loads(args.plan.read_text()),
                args.output_dir,
                limit=args.limit,
                model=DeepSeekClient().model,
            )
        )
    print(json.dumps({"counts": state["counts"], "not_attempted": state["not_attempted"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

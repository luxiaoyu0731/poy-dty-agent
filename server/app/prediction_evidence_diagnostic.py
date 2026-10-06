"""Explicit-ID read-only diagnostic of stored article bodies and summary states.

Requires caller authorization for production. This is a mutable-table snapshot,
not proof that its present body or summary existed at an old publication time.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime

from .prediction_evidence_inputs import _check_urls, digest

NEWS_COLUMNS = (
    "article_id",
    "created_at",
    "source_id",
    "tier",
    "canonical_url",
    "title",
    "published_at",
    "first_seen_at",
    "content_hash",
    "language",
    "raw_text",
)
SUMMARY_COLUMNS = (
    "factual_summary",
    "summary_status",
    "fact_payload",
    "quality_status",
    "quality_reasons",
    "input_quality",
    "prompt_version",
    "generated_at",
    "attempts",
    "source_hash",
    "updated_at",
    "fact_summary_status",
)


def export_article_diagnostic(
    connection: sqlite3.Connection,
    article_ids: list[str],
    *,
    max_rows: int = 5000,
    max_bytes: int = 128 * 1024 * 1024,
    timeout_seconds: float = 45,
) -> dict:
    if (
        not article_ids
        or any(not isinstance(x, str) or not x for x in article_ids)
        or len(article_ids) != len(set(article_ids))
        or len(article_ids) > max_rows
        or not 1 <= max_rows <= 5000
        or not 0 < max_bytes <= 128 * 1024 * 1024
        or not 0 < timeout_seconds <= 45
    ):
        raise ValueError("invalid_diagnostic_scope_or_bounds")
    if connection.in_transaction:
        raise ValueError("diagnostic_requires_own_read_transaction")
    start = time.monotonic()
    connection.execute("PRAGMA query_only=ON")
    connection.set_progress_handler(lambda: int(time.monotonic() - start >= timeout_seconds), 1000)
    connection.execute("BEGIN")
    rows = []
    total = 0
    found = set()
    ids = sorted(article_ids)
    fields = [f"n.{c}" for c in NEWS_COLUMNS] + [f"s.{c}" for c in SUMMARY_COLUMNS]
    fields.append("json_extract(n.raw,'$.content_visible_at')")
    try:
        for offset in range(0, len(ids), 100):
            batch = ids[offset : offset + 100]
            placeholders = ",".join("?" for _ in batch)
            cursor = connection.execute(
                f"SELECT {','.join(fields)} FROM news_articles n "
                "LEFT JOIN event_ai_summaries s ON s.article_id=n.article_id "
                f"WHERE n.article_id IN ({placeholders}) ORDER BY n.article_id",
                batch,
            )
            for record in cursor:
                if time.monotonic() - start >= timeout_seconds:
                    raise TimeoutError("article_diagnostic_deadline")
                news = dict(zip(NEWS_COLUMNS, record[: len(NEWS_COLUMNS)], strict=True))
                summary = dict(zip(SUMMARY_COLUMNS, record[len(NEWS_COLUMNS) : -1], strict=True))
                _check_urls(news["canonical_url"])
                body = {"article": news, "summary": summary, "content_visible_at": record[-1]}
                encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
                total += len(encoded)
                if total > max_bytes:
                    raise ValueError("article_diagnostic_payload_limit")
                rows.append({"payload": body, "payload_sha256": digest(body)})
                found.add(news["article_id"])
        if time.monotonic() - start >= timeout_seconds:
            raise TimeoutError("article_diagnostic_deadline")
    finally:
        connection.rollback()
        connection.set_progress_handler(None, 0)
    body = {
        "schema_version": "prediction-article-diagnostic.v1",
        "captured_at": datetime.now(UTC).isoformat(),
        "requested_ids_sha256": digest(ids),
        "requested_count": len(ids),
        "returned_count": len(rows),
        "missing_article_ids": sorted(set(ids) - found),
        "payload_bytes": total,
        "complete_requested_scope": True,
        "historical_body_availability_verified": False,
        "rows": rows,
    }
    return {**body, "content_sha256": digest(body)}

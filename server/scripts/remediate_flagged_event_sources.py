from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
DeepSeekClient = importlib.import_module("app.deepseek_client").DeepSeekClient
news = importlib.import_module("app.news")
EVENT_SUMMARY_PROMPT_VERSION = news.EVENT_SUMMARY_PROMPT_VERSION
_extract_article_detail = news._extract_article_detail
_fetch_text = news._fetch_text
settings = importlib.import_module("app.settings").settings
storage = importlib.import_module("app.storage")
connect = storage.connect
enqueue_event_ai_summary = storage.enqueue_event_ai_summary
audit_row = importlib.import_module("scripts.audit_event_summary_quality").audit_row


def _json_object(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _flagged_rows() -> list[dict[str, object]]:
    with closing(connect()) as connection, connection:
        rows = connection.execute(
            """SELECT s.*, a.title, a.raw_text, a.summary AS source_summary,
                      a.language, a.source_id, a.url, a.published_at, a.raw, a.content_hash
               FROM event_ai_summaries s
               JOIN news_articles a USING(article_id)
               ORDER BY s.article_id"""
        ).fetchall()
    return [dict(row) for row in rows if audit_row(dict(row))["issues"]]


def _can_recapture(row: dict[str, object]) -> tuple[bool, str]:
    host = (urlparse(str(row.get("url") or "")).hostname or "").lower()
    if str(row.get("source_id") or "").startswith("google_news") or host == "news.google.com":
        return False, "discovery_feed_title_only"
    if host not in settings.outbound_hosts:
        return False, "host_not_authorized"
    return True, "authorized_public_source"


def _usable_body(text: str, title: str) -> bool:
    body, heading = " ".join(text.split()), " ".join(title.split())
    return len(body) >= max(160, len(heading) + 80) and body.casefold() != heading.casefold()


def _persist_marker(article_id: str, raw: object, *, status: str, reason: str) -> None:
    metadata = _json_object(raw)
    metadata["source_content"] = {"status": status, "reason": reason, "verified_at": datetime.now(UTC).isoformat()}
    with closing(connect()) as connection, connection:
        connection.execute(
            "UPDATE news_articles SET raw=? WHERE article_id=?", (json.dumps(metadata, ensure_ascii=False), article_id)
        )


async def remediate(*, apply: bool) -> dict[str, object]:
    rows = _flagged_rows()
    counts = Counter(str(row.get("source_id") or "unknown") for row in rows)
    result: dict[str, object] = {
        "mode": "apply" if apply else "dry_run",
        "flagged": len(rows),
        "sources": dict(counts),
        "recaptured": 0,
        "title_only": 0,
        "fetch_failed": 0,
        "requeued": 0,
    }
    for row in rows:
        article_id, title = str(row["article_id"]), str(row.get("title") or "")
        allowed, reason = _can_recapture(row)
        if not allowed:
            result["title_only"] = int(result["title_only"]) + 1
            if apply:
                _persist_marker(article_id, row.get("raw"), status="title_only", reason=reason)
            continue
        try:
            page, content_type = await _fetch_text(str(row["url"]))
            if "pdf" in content_type.lower():
                raise ValueError("pdf_requires_source_specific_parser")
            body = _extract_article_detail(page)["text"]
            if not _usable_body(body, title):
                raise ValueError("no_verifiable_article_body")
        except Exception as exc:
            result["title_only"] = int(result["title_only"]) + 1
            result["fetch_failed"] = int(result["fetch_failed"]) + 1
            if apply:
                _persist_marker(
                    article_id,
                    row.get("raw"),
                    status="title_only",
                    reason=exc.__class__.__name__ + ":" + str(exc)[:120],
                )
            continue
        result["recaptured"] = int(result["recaptured"]) + 1
        if not apply:
            continue
        content_hash = hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()
        metadata = _json_object(row.get("raw"))
        metadata["source_content"] = {"status": "full_text", "reason": "authorized_public_recapture"}
        with closing(connect()) as connection, connection:
            connection.execute(
                "UPDATE news_articles SET raw_text=?,content_hash=?,raw=? WHERE article_id=?",
                (body[:5000], content_hash, json.dumps(metadata, ensure_ascii=False), article_id),
            )
        enqueue_event_ai_summary(article_id, content_hash, DeepSeekClient().model, EVENT_SUMMARY_PROMPT_VERSION)
        result["requeued"] = int(result["requeued"]) + 1
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(remediate(apply=args.apply))
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()

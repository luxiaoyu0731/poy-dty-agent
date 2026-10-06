from __future__ import annotations

import importlib.util
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.storage import SCHEMA  # noqa: E402

SCRIPT_PATH = SERVER_ROOT / "scripts" / "public_news_backfill_v2.py"
SPEC = importlib.util.spec_from_file_location("public_news_backfill_v2", SCRIPT_PATH)
assert SPEC is not None
public_news_backfill_v2 = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["public_news_backfill_v2"] = public_news_backfill_v2
SPEC.loader.exec_module(public_news_backfill_v2)


def test_gdelt_articles_to_items_marks_summary_only_and_dedupes_title() -> None:
    payload = {
        "articles": [
            {
                "url": "https://example.test/opec-output",
                "title": "OPEC output policy shifts crude oil expectations",
                "seendate": "20250616091500",
                "domain": "example.test",
                "language": "English",
            },
            {
                "url": "https://example.test/opec-output-copy",
                "title": "OPEC output policy shifts crude oil expectations",
                "seendate": "20250616093000",
                "domain": "example.test",
            },
        ]
    }

    [item] = public_news_backfill_v2.gdelt_articles_to_items(
        payload,
        group=public_news_backfill_v2.QUERY_GROUPS[0],
        seen_urls=set(),
        seen_titles=set(),
    )

    assert item.source_id == "gdelt_v2_oil_policy"
    assert item.published_at == "2025-06-16T09:15:00+00:00"
    assert "full article body" in item.raw_text
    assert item.url == "https://example.test/opec-output"


def test_run_backfill_dry_run_dedupes_without_writing(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        insert_article(connection, title="Existing OPEC crude oil article", url="https://example.test/existing")

    response = {
        "articles": [
            {
                "url": "https://example.test/existing",
                "title": "Existing OPEC crude oil article",
                "seendate": "20250616091500",
                "domain": "example.test",
            },
            {
                "url": "https://example.test/new",
                "title": "EIA crude oil inventories draw supports refinery margins",
                "seendate": "20250616101500",
                "domain": "example.test",
            },
        ]
    }
    monkeypatch.setattr(public_news_backfill_v2, "fetch_gdelt_json", lambda *args, **kwargs: response)

    report = public_news_backfill_v2.run_backfill(
        db_path=db_path,
        start=public_news_backfill_v2.DEFAULT_START,
        end=public_news_backfill_v2.DEFAULT_END,
        target_raw_news=2_000,
        max_records=250,
        sleep_seconds=0.0,
        window_days=14,
        limit_requests=1,
        timeout_seconds=1.0,
        attempts=1,
        max_backoff_seconds=0.0,
        providers={"gdelt"},
        dry_run=True,
    )

    assert report["summary"]["new_articles"] == 1
    assert report["summary"]["updated_articles"] == 0
    assert report["after"]["raw_news"] == report["before"]["raw_news"] + 1
    with closing(sqlite3.connect(db_path)) as connection, connection:
        count = connection.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
    assert count == 1


def insert_article(connection: sqlite3.Connection, *, title: str, url: str) -> None:
    connection.execute(
        """
        INSERT INTO news_articles (
          article_id, created_at, source_id, tier, url, canonical_url, title, published_at,
          first_seen_at, content_hash, language, raw_text, summary, score, category, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "art_existing",
            "2025-06-16T00:00:00+00:00",
            "gdelt_oil_geopolitics_rss",
            "C",
            url,
            url,
            title,
            "2025-06-16T00:00:00+00:00",
            "2025-06-16T00:00:00+00:00",
            "hash",
            "en",
            title,
            title,
            25.0,
            "oil_policy",
            "{}",
        ),
    )

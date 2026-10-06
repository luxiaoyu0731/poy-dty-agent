from __future__ import annotations

import importlib.util
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "repair_opec_generic_titles.py"
SPEC = importlib.util.spec_from_file_location("repair_opec_titles_under_test", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
repair = importlib.util.module_from_spec(SPEC)
sys.modules["repair_opec_titles_under_test"] = repair
SPEC.loader.exec_module(repair)


def test_apply_repairs_updates_article_cluster_and_resets_summary_atomically(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    old_title = "Organization of the Petroleum Exporting Countries - Organization of the Petroleum Exporting Countries"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE news_articles (
              article_id TEXT PRIMARY KEY, source_id TEXT, canonical_url TEXT,
              published_at TEXT, title TEXT, content_hash TEXT
            );
            CREATE TABLE news_event_clusters (
              cluster_id TEXT PRIMARY KEY, article_ids TEXT, title TEXT,
              updated_at TEXT, status TEXT
            );
            CREATE TABLE event_ai_summaries (
              article_id TEXT PRIMARY KEY, factual_summary TEXT, summary_status TEXT,
              generated_at TEXT, attempts INTEGER, error TEXT, source_hash TEXT,
              output_chars INTEGER, updated_at TEXT, fact_payload TEXT,
              business_impact_payload TEXT, quality_status TEXT, quality_reasons TEXT,
              fact_summary_status TEXT, impact_analysis_status TEXT,
              impact_quality_reasons TEXT
            );
            """
        )
        connection.execute(
            "INSERT INTO news_articles VALUES (?, 'opec_press', ?, '2026-08-02', ?, 'old-hash')",
            ("article-1", "https://www.opec.org/pr-detail/example.html", old_title),
        )
        connection.execute(
            "INSERT INTO news_event_clusters VALUES ('cluster-1', '[\"article-1\"]', ?, '', 'candidate')",
            (old_title,),
        )
        connection.execute(
            """
            INSERT INTO event_ai_summaries VALUES (
              'article-1', 'old summary', 'completed', '2026-08-02', 1, '', 'old-hash',
              10, '', '{}', '{}', 'passed', '[]', 'completed', 'completed', '[]'
            )
            """
        )

    integrity, foreign_keys = repair._apply_repairs(
        database,
        [
            {
                "article_id": "article-1",
                "old_title": old_title,
                "new_title": "Specific OPEC production decision",
                "content_hash": "new-hash",
            }
        ],
    )

    assert integrity == "ok"
    assert foreign_keys == []
    with closing(sqlite3.connect(database)) as connection, connection:
        article = connection.execute("SELECT title, content_hash FROM news_articles").fetchone()
        cluster = connection.execute("SELECT title, status FROM news_event_clusters").fetchone()
        summary = connection.execute(
            "SELECT factual_summary, summary_status, quality_status, source_hash FROM event_ai_summaries"
        ).fetchone()
    assert article == ("Specific OPEC production decision", "new-hash")
    assert cluster == ("Specific OPEC production decision", "candidate")
    assert summary == ("", "pending", "pending", "new-hash")

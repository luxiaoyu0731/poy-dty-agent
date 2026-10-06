from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "audit_event_pipeline_funnel.py"
SPEC = importlib.util.spec_from_file_location("audit_event_pipeline_funnel", SCRIPT_PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
sys.modules["audit_event_pipeline_funnel"] = module
SPEC.loader.exec_module(module)


def _database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE news_articles (
              article_id TEXT PRIMARY KEY, source_id TEXT, url TEXT, canonical_url TEXT, title TEXT,
              published_at TEXT, content_hash TEXT, raw TEXT
            );
            CREATE TABLE event_ai_summaries (
              article_id TEXT PRIMARY KEY, input_quality TEXT, summary_status TEXT, quality_status TEXT,
              fact_summary_status TEXT, impact_analysis_status TEXT, business_impact_payload TEXT,
              factual_summary TEXT, prompt_version TEXT, model TEXT, provider TEXT, quality_reasons TEXT
            );
            CREATE TABLE news_event_clusters (
              cluster_id TEXT PRIMARY KEY, article_ids TEXT, status TEXT, event_record_id TEXT,
              affected_products TEXT
            );
            CREATE TABLE event_observations (event_record_id TEXT PRIMARY KEY, affected_products TEXT);
            """
        )
        articles = [
            ("a1", "s1", "https://x/1", "https://x/1", "T1", "2026-01-01", "h1", "{}"),
            ("a2", "s1", "https://x/2", "https://x/2", "T2", "2026-01-02", "h2", "{}"),
            ("a3", "s1", "", "", "T3", "2026-01-03", "h3", "{}"),
        ]
        connection.executemany("INSERT INTO news_articles VALUES (?,?,?,?,?,?,?,?)", articles)
        connection.execute(
            "INSERT INTO event_ai_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "a1",
                "full_text",
                "completed",
                "completed",
                "completed",
                "completed",
                json.dumps({"relevant": True}),
                "某公司宣布暂停相关装置运行。",
                "v1",
                "m1",
                "p1",
                "[]",
            ),
        )
        connection.execute(
            "INSERT INTO event_ai_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "a2",
                "partial_text",
                "rejected",
                "rejected",
                "rejected",
                "not_requested",
                "{}",
                "",
                "v1",
                "m1",
                "p1",
                '["insufficient_source_text"]',
            ),
        )
        connection.execute("INSERT INTO news_event_clusters VALUES ('c1','[\"a1\"]','featured','e1','[\"PTA\"]')")
        connection.execute("INSERT INTO event_observations VALUES ('e1','[\"PTA\"]')")


def test_report_has_mutually_exclusive_population_and_strict_complete_event(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _database(db)
    report = module.build_report(db)
    assert report["article_count"] == 3
    assert sum(report["content_classes"].values()) == 3
    assert report["content_classes"] == {
        "full_text": 1,
        "partial_text": 1,
        "title_only": 0,
        "metadata_missing": 1,
    }
    assert report["summary_queue"]["not_queued"] == 1
    assert report["summary_queue"]["completed"] == 1
    assert report["summary_queue"]["rejected"] == 1
    assert report["summary_dimensions"]["fact_summary_status"] == {
        "completed": 1,
        "rejected": 1,
    }
    assert report["summary_dimensions"]["impact_analysis_status"] == {
        "completed": 1,
        "not_requested": 1,
    }
    assert report["summary_dimensions"]["source_funnel"]["s1"]["articles"] == 3
    assert report["duplicates"]["url"] == {
        "duplicate_groups": 0,
        "duplicate_excess_rows": 0,
    }
    assert report["complete_event"]["count"] == 1
    assert report["public_event_population"] == {
        "definition": "news_event_clusters plus event_observations not linked by cluster.event_record_id",
        "cluster_events": 1,
        "linked_observations_excluded": 1,
        "standalone_observations": 0,
        "total_before_canonical_alias_dedup": 1,
    }


def test_english_summary_cannot_be_counted_as_complete(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _database(db)
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute(
            "UPDATE event_ai_summaries "
            "SET factual_summary='The company suspended plant operations.' "
            "WHERE article_id='a1'"
        )
    report = module.build_report(db)
    assert report["complete_event"]["count"] == 0


def test_read_only_connection_rejects_writes(tmp_path: Path) -> None:
    db = tmp_path / "events.db"
    _database(db)
    connection = module._readonly_connection(db)
    try:
        try:
            connection.execute("DELETE FROM news_articles")
        except sqlite3.OperationalError as exc:
            assert "readonly" in str(exc).lower()
        else:
            raise AssertionError("read-only audit connection accepted a write")
    finally:
        connection.close()

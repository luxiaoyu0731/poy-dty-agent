from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from datetime import date
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.storage import SCHEMA  # noqa: E402

SCRIPT_PATH = SERVER_ROOT / "scripts" / "asof_event_clusterer.py"
SPEC = importlib.util.spec_from_file_location("asof_event_clusterer", SCRIPT_PATH)
assert SPEC is not None
asof_event_clusterer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["asof_event_clusterer"] = asof_event_clusterer
SPEC.loader.exec_module(asof_event_clusterer)


def test_cluster_report_filters_future_candidates_per_as_of_window() -> None:
    candidates = [
        candidate("raw-before", "2026-06-10T08:00:00+00:00", source_kind="news_article"),
        candidate("raw-future", "2026-06-20T08:00:00+00:00", source_kind="news_article"),
        candidate("precomputed", "2026-06-10T09:00:00+00:00", source_kind="news_event_cluster"),
    ]

    report = asof_event_clusterer.build_cluster_report_from_candidates(
        candidates,
        start=date(2026, 6, 1),
        end=date(2026, 6, 30),
        window="half_month",
    )

    first_window = report["windows"][0]
    assert first_window["end"] == "2026-06-15"
    assert first_window["future_leak_count"] == 0
    assert first_window["future_candidates_after_as_of"] == 1
    first_cluster_ids = {
        candidate_id for cluster in first_window["clusters"] for candidate_id in cluster["candidate_ids"]
    }
    assert "raw-before" in first_cluster_ids
    assert "raw-future" not in first_cluster_ids
    assert "precomputed" not in first_cluster_ids
    assert report["guardrails"]["excluded_precomputed_news_cluster_candidates"] == 1
    assert report["guardrails"]["uses_precomputed_year_clusters"] is False


def test_cluster_report_accepts_rfc_candidate_times() -> None:
    candidates = [
        candidate("raw-rfc", "Wed, 10 Jun 2026 08:00:00 GMT", source_kind="news_article"),
    ]

    report = asof_event_clusterer.build_cluster_report_from_candidates(
        candidates,
        start=date(2026, 6, 10),
        end=date(2026, 6, 10),
        window="day",
    )

    clusters = report["windows"][0]["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["candidate_ids"] == ["raw-rfc"]


def test_db_clusterer_uses_raw_news_articles_not_year_cluster_replay(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        insert_news_article(connection, article_id="article-1", published_at="2026-06-10T08:00:00+00:00")
        insert_news_article(connection, article_id="article-2", published_at="2026-06-10T09:00:00+00:00")
        insert_news_cluster(connection)
        connection.commit()

    report = asof_event_clusterer.build_asof_event_clusters(
        db_path,
        start=date(2026, 6, 10),
        end=date(2026, 6, 10),
        window="day",
    )

    clusters = report["windows"][0]["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["source_kinds"] == ["news_article"]
    assert sorted(clusters[0]["candidate_ids"]) == [
        "cand_news_article_article_1",
        "cand_news_article_article_2",
    ]
    assert report["guardrails"]["uses_precomputed_year_clusters"] is False


def test_cli_writes_asof_event_clusters(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    output_dir = tmp_path / "out"
    candidates_path = tmp_path / "candidates.json"
    candidates_path.write_text(
        json.dumps({"candidates": [candidate("raw-before", "2026-06-10T08:00:00+00:00")]}),
        encoding="utf-8",
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        connection.commit()

    exit_code = asof_event_clusterer.main(
        [
            "--db",
            str(db_path),
            "--start",
            "2026-06-10",
            "--end",
            "2026-06-10",
            "--window",
            "day",
            "--limit",
            "10",
            "--output-dir",
            str(output_dir),
            "--candidates",
            str(candidates_path),
        ]
    )

    assert exit_code == 0
    output_path = output_dir / "asof-event-clusters-2026-06-10-to-2026-06-10.json"
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["scope"]["window"] == "day"
    assert payload["summary"]["cluster_count"] == 1


def candidate(candidate_id: str, as_of_time: str, *, source_kind: str = "news_article") -> dict[str, object]:
    return {
        "candidate_id": candidate_id,
        "candidate_type": "news_announcement",
        "source_kind": source_kind,
        "source_record_id": candidate_id,
        "as_of_time": as_of_time,
        "title": "OFAC sanctions tanker shipping network",
        "category": "sanctions_geopolitics",
        "summary": "Official sanctions mention crude oil tanker shipping.",
        "source_ids": ["ofac_recent_actions"],
        "cited_doc_ids": [f"news_article:{candidate_id}"],
        "evidence_level": "A",
        "affected_products": ["crude_oil", "PX", "PTA"],
        "direction_hint": "bullish",
        "heat_score": 90,
        "signals": {},
        "requires_human_review": False,
    }


def insert_news_article(connection: sqlite3.Connection, *, article_id: str, published_at: str) -> None:
    connection.execute(
        """
        INSERT INTO news_articles (
          article_id, created_at, source_id, tier, url, canonical_url, title, published_at,
          first_seen_at, content_hash, language, raw_text, summary, score, category, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article_id,
            published_at,
            "ofac_recent_actions",
            "A",
            f"https://example.test/{article_id}",
            f"https://example.test/{article_id}",
            "OFAC sanctions tanker network",
            published_at,
            published_at,
            f"hash-{article_id}",
            "en",
            "OFAC sanctions crude oil tanker shipping network.",
            "OFAC sanctions energy shipping.",
            88,
            "sanctions_geopolitics",
            "{}",
        ),
    )


def insert_news_cluster(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO news_event_clusters (
          cluster_id, created_at, updated_at, title, category, source_ids, article_ids, heat_score,
          evidence_level, affected_products, direction, impact_strength, summary, status, event_record_id, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "year-cluster-ofac",
            "2026-06-10T09:15:00+00:00",
            "2026-12-31T09:15:00+00:00",
            "Full-year OFAC tanker sanctions cluster",
            "sanctions_geopolitics",
            json.dumps(["ofac_recent_actions"]),
            json.dumps(["article-1", "article-2"]),
            99,
            "A",
            json.dumps(["crude_oil", "PX", "PTA"]),
            "bullish",
            "high",
            "Full-year cluster should not be replayed by as-of clusterer.",
            "featured",
            None,
            "{}",
        ),
    )

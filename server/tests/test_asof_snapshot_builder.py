from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.storage import SCHEMA  # noqa: E402

SCRIPT_PATH = SERVER_ROOT / "scripts" / "asof_snapshot_builder.py"
SPEC = importlib.util.spec_from_file_location("asof_snapshot_builder", SCRIPT_PATH)
assert SPEC is not None
asof_snapshot_builder = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["asof_snapshot_builder"] = asof_snapshot_builder
SPEC.loader.exec_module(asof_snapshot_builder)


def test_snapshot_contains_only_rows_at_or_before_as_of_time(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        insert_market(connection, observation_id="market-before", observed_at="2026-06-10")
        insert_market(connection, observation_id="market-after", observed_at="2026-06-11")
        insert_industry(connection, observation_id="industry-before", observed_at="2026-06-10")
        insert_industry(connection, observation_id="industry-after", observed_at="2026-06-11")
        insert_news_article(connection, article_id="article-before", published_at="2026-06-10T08:00:00+00:00")
        insert_news_article(connection, article_id="article-after", published_at="2026-06-11T08:00:00+00:00")
        insert_news_cluster(connection)
        connection.commit()

    snapshot = asof_snapshot_builder.build_asof_snapshot(
        db_path,
        as_of_time=datetime(2026, 6, 10, 23, 59, tzinfo=UTC),
    )

    market_ids = {row["observation_id"] for row in snapshot["payload"]["market_observations"]}
    industry_ids = {row["observation_id"] for row in snapshot["payload"]["industry_observations"]}
    article_ids = {row["article_id"] for row in snapshot["payload"]["news_articles"]}
    assert market_ids == {"market-before"}
    assert industry_ids == {"industry-before"}
    assert article_ids == {"article-before"}
    assert snapshot["future_leak_count"] == 0
    assert snapshot["excluded_future_rows_by_table"]["market_observations"] == 1
    assert snapshot["excluded_future_rows_by_table"]["industry_observations"] == 1
    assert snapshot["excluded_future_rows_by_table"]["news_articles"] == 1

    cluster = snapshot["payload"]["news_event_clusters"][0]
    assert cluster["article_ids"] == ["article-before"]
    assert cluster["_asof_trimmed_future_article_ids"] == ["article-after"]
    assert snapshot["trimmed_future_cluster_article_refs"] == 1


def test_cli_writes_asof_snapshot_series(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    output_dir = tmp_path / "out"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        insert_market(connection, observation_id="market-before", observed_at="2026-06-10")
        connection.commit()

    exit_code = asof_snapshot_builder.main(
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
            "20",
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    output_path = output_dir / "asof-snapshots-2026-06-10-to-2026-06-10.json"
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["scope"]["window"] == "day"
    assert payload["summary"]["snapshot_count"] == 1
    assert payload["snapshots"][0]["future_leak_count"] == 0


def insert_market(connection: sqlite3.Connection, *, observation_id: str, observed_at: str) -> None:
    connection.execute(
        """
        INSERT INTO market_observations (
          observation_id, created_at, source_id, observed_at, indicator, product, value, unit,
          frequency, region, evidence_url, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            "2026-06-10T12:00:00+00:00",
            "eia_petroleum_api",
            observed_at,
            "Brent crude spot",
            "crude_oil",
            80,
            "USD/bbl",
            "daily",
            "global",
            f"https://example.test/{observation_id}",
            "",
            "{}",
        ),
    )


def insert_industry(connection: sqlite3.Connection, *, observation_id: str, observed_at: str) -> None:
    connection.execute(
        """
        INSERT INTO industry_observations (
          observation_id, created_at, source_id, observed_at, product, metric, market, region,
          value, unit, frequency, evidence_level, evidence_url, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            observation_id,
            "2026-06-10T16:00:00+00:00",
            "manual_public_check",
            observed_at,
            "POY",
            "spot_quote",
            "全国",
            "CN",
            7500,
            "CNY/ton",
            "daily",
            "C",
            f"https://example.test/{observation_id}",
            "",
            "{}",
        ),
    )


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
            "cluster-ofac",
            "2026-06-10T09:15:00+00:00",
            "2026-06-11T09:15:00+00:00",
            "OFAC sanctions tanker network",
            "sanctions_geopolitics",
            json.dumps(["ofac_recent_actions"]),
            json.dumps(["article-before", "article-after"]),
            91,
            "A",
            json.dumps(["crude_oil", "PX", "PTA"]),
            "bullish",
            "high",
            "Official sanctions mention energy shipping.",
            "featured",
            None,
            "{}",
        ),
    )

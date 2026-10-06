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

SCRIPT_PATH = SERVER_ROOT / "scripts" / "event_candidate_generator.py"
SPEC = importlib.util.spec_from_file_location("event_candidate_generator", SCRIPT_PATH)
assert SPEC is not None
event_candidate_generator = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["event_candidate_generator"] = event_candidate_generator
SPEC.loader.exec_module(event_candidate_generator)


def test_generate_candidates_from_news_price_industry_and_macro(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        insert_news_article(connection, article_id="art-ofac", published_at="2026-06-10T09:00:00+00:00")
        insert_news_cluster(connection)
        insert_market(
            connection,
            observation_id="brent-prev",
            observed_at="2026-06-09",
            indicator="Brent crude spot",
            product="crude_oil",
            value=80,
        )
        insert_market(
            connection,
            observation_id="brent-jump",
            observed_at="2026-06-10",
            indicator="Brent crude spot",
            product="crude_oil",
            value=84,
        )
        insert_market(
            connection,
            observation_id="dgs10",
            observed_at="2026-06-10",
            indicator="10-Year Treasury yield",
            product="macro",
            value=4.5,
            raw={"series_id": "DGS10"},
            source_id="fred_macro_api",
        )
        insert_industry(
            connection,
            observation_id="poy-run-rate",
            observed_at="2026-06-10",
            product="POY",
            metric="operating_rate",
            value=76,
            notes="POY operating rate softened after maintenance.",
        )
        insert_intraday(connection)
        connection.commit()

    report = event_candidate_generator.generate_event_candidates(
        db_path,
        start=date(2026, 6, 9),
        end=date(2026, 6, 10),
        price_threshold_pct=2.0,
    )

    candidate_types = {item["candidate_type"] for item in report["candidates"]}
    assert {"news_announcement", "price_move", "industry_observation", "macro_finance"} <= candidate_types
    assert report["guardrails"]["provider_calls"] == 0
    assert report["guardrails"]["remote_fetches"] == 0
    assert any("news_event:cluster-ofac" in item["cited_doc_ids"] for item in report["candidates"])
    assert any("intraday:brent-intraday" in item["cited_doc_ids"] for item in report["candidates"])
    price_move = next(item for item in report["candidates"] if item["candidate_type"] == "price_move")
    assert price_move["signals"]["change_pct"] == 5.0
    macro = next(item for item in report["candidates"] if item["candidate_type"] == "macro_finance")
    assert macro["category"] == "macro_finance"
    assert report["input_counts"]["intraday_rows_loaded"] == 1


def test_generate_candidates_accepts_rfc_news_dates(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        insert_news_article(
            connection,
            article_id="art-rfc",
            published_at="Wed, 10 Jun 2026 09:00:00 GMT",
        )
        connection.commit()

    report = event_candidate_generator.generate_event_candidates(
        db_path,
        start=date(2026, 6, 10),
        end=date(2026, 6, 10),
        include_preclustered_news=False,
    )

    assert [item["source_record_id"] for item in report["candidates"]] == ["art-rfc"]


def test_cli_supports_required_flags_and_dry_run(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    output_dir = tmp_path / "out"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        insert_news_article(connection, article_id="art-eia", published_at="2026-06-10")
        connection.commit()

    exit_code = event_candidate_generator.main(
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
            "5",
            "--output-dir",
            str(output_dir),
            "--dry-run",
        ]
    )

    assert exit_code == 0
    assert not output_dir.exists()

    exit_code = event_candidate_generator.main(
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
            "5",
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    output_path = output_dir / "event-candidates-2026-06-10-to-2026-06-10.json"
    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["scope"]["window"] == "day"
    assert len(payload["candidates"]) == 1


def insert_news_article(
    connection: sqlite3.Connection,
    *,
    article_id: str,
    published_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO news_articles (
          article_id, created_at, source_id, tier, url, canonical_url, title, published_at,
          first_seen_at, content_hash, language, raw_text, summary, score, category, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            article_id,
            "2026-06-10T09:10:00+00:00",
            "ofac_recent_actions",
            "A",
            f"https://example.test/{article_id}",
            f"https://example.test/{article_id}",
            "OFAC sanctions tanker network",
            published_at,
            "2026-06-10T09:10:00+00:00",
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
            "2026-06-10T09:15:00+00:00",
            "OFAC sanctions tanker network",
            "sanctions_geopolitics",
            json.dumps(["ofac_recent_actions"]),
            json.dumps(["art-ofac"]),
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


def insert_market(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    observed_at: str,
    indicator: str,
    product: str,
    value: float,
    raw: dict[str, object] | None = None,
    source_id: str = "eia_petroleum_api",
) -> None:
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
            source_id,
            observed_at,
            indicator,
            product,
            value,
            "unit",
            "daily",
            "global",
            f"https://example.test/{observation_id}",
            "",
            json.dumps(raw or {}),
        ),
    )


def insert_industry(
    connection: sqlite3.Connection,
    *,
    observation_id: str,
    observed_at: str,
    product: str,
    metric: str,
    value: float,
    notes: str,
) -> None:
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
            product,
            metric,
            "全国",
            "CN",
            value,
            "percent",
            "daily",
            "C",
            f"https://example.test/{observation_id}",
            notes,
            "{}",
        ),
    )


def insert_intraday(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO intraday_price_observations (
          observation_id, created_at, instrument, symbol, observed_at, interval_seconds,
          price_type, last, open_value, high_value, low_value, volume, change_pct, unit,
          source_id, source_url, source_latency_seconds, quality, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "brent-intraday",
            "2026-06-10T16:00:00+00:00",
            "Brent",
            "BZ=F",
            "2026-06-10T15:30:00+00:00",
            60,
            "exchange_proxy",
            83.2,
            80.0,
            83.2,
            79.8,
            1000,
            4.0,
            "USD/bbl",
            "yahoo_finance_proxy",
            "https://example.test/brent-intraday",
            0.2,
            "proxy_market_quote",
            "Brent intraday proxy move.",
            "{}",
        ),
    )

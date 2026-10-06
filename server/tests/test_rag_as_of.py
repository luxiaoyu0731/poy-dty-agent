from __future__ import annotations

import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))

from app.rag import retrieve_evidence  # noqa: E402
from app.settings import settings  # noqa: E402
from app.storage import (  # noqa: E402
    connect,
    create_event_observation,
    create_industry_observation,
    create_market_observation,
    upsert_news_article,
    upsert_political_case_memory,
)


@pytest.fixture(autouse=True)
def isolated_sqlite(tmp_path: Path):
    original_sqlite_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "agent-test.db"))
    yield
    object.__setattr__(settings, "sqlite_path", original_sqlite_path)


def test_retrieve_evidence_as_of_filters_temporal_observation_types() -> None:
    _insert_temporal_fixture("before", "2026-06-12T08:00:00+00:00")
    _insert_temporal_fixture("after", "2026-06-14T08:00:00+00:00")

    search = retrieve_evidence(
        "checkpoint-rag crude POY Brent sanctions shipping",
        as_of_time="2026-06-12T23:59:59+00:00",
        limit=20,
    )

    doc_ids = {item.doc_id for item in search.documents}
    assert {
        "news_article:news-before",
        "event:event-before",
        "market:market-before",
        "industry:industry-before",
    } <= doc_ids
    assert "news_article:news-after" not in doc_ids
    assert "event:event-after" not in doc_ids
    assert "market:market-after" not in doc_ids
    assert "industry:industry-after" not in doc_ids

    as_of = _parse("2026-06-12T23:59:59+00:00")
    static_types = {"source_config", "news_source", "knowledge_node", "knowledge_edge"}
    for item in search.documents:
        if item.doc_type in static_types:
            continue
        assert item.observed_at
        assert _parse(item.observed_at) <= as_of


def test_retrieve_evidence_as_of_excludes_backfilled_historical_rows() -> None:
    _insert_temporal_fixture("backfilled", "2026-06-10T08:00:00+00:00")
    with closing(connect()) as connection, connection:
        connection.execute(
            "UPDATE news_articles SET created_at = ?, first_seen_at = ? WHERE article_id = ?",
            ("2026-06-14T08:00:00+00:00", "2026-06-14T08:00:00+00:00", "news-backfilled"),
        )
        connection.execute(
            "UPDATE event_observations SET created_at = ? WHERE event_record_id = ?",
            ("2026-06-14T08:00:00+00:00", "event-backfilled"),
        )
        connection.execute(
            "UPDATE market_observations SET created_at = ? WHERE observation_id = ?",
            ("2026-06-14T08:00:00+00:00", "market-backfilled"),
        )
        connection.execute(
            "UPDATE industry_observations SET created_at = ? WHERE observation_id = ?",
            ("2026-06-14T08:00:00+00:00", "industry-backfilled"),
        )

    search = retrieve_evidence(
        "checkpoint-rag backfilled sanctions shipping Brent POY",
        as_of_time="2026-06-12T23:59:59+00:00",
        limit=20,
    )

    doc_ids = {item.doc_id for item in search.documents}
    assert "news_article:news-backfilled" not in doc_ids
    assert "event:event-backfilled" not in doc_ids
    assert "market:market-backfilled" not in doc_ids
    assert "industry:industry-backfilled" not in doc_ids


def test_as_of_retrieval_excludes_prediction_records_unless_explicitly_allowed() -> None:
    _insert_temporal_fixture("before", "2026-06-12T08:00:00+00:00")
    _insert_historical_legacy_prediction()

    default_search = retrieve_evidence(
        "checkpoint-rag prediction Brent",
        as_of_time="2026-06-12T23:59:59+00:00",
        limit=20,
    )
    assert "prediction:prediction-before" not in {item.doc_id for item in default_search.documents}

    opt_in_search = retrieve_evidence(
        "checkpoint-rag prediction Brent",
        as_of_time="2026-06-12T23:59:59+00:00",
        include_prediction_records=True,
        limit=20,
    )
    assert "prediction:prediction-before" in {item.doc_id for item in opt_in_search.documents}


def _insert_historical_legacy_prediction() -> None:
    trigger_name = "trg_prediction_ledger_formal_scalar_insert_blocked"
    expected_columns = (
        "prediction_id",
        "created_at",
        "target",
        "horizon",
        "direction",
        "confidence",
        "rationale",
        "counter_evidence",
        "source_status",
        "tags",
        "data_snapshot_id",
        "review_status",
        "evidence_mapping",
        "direction_derivation",
        "review_audit",
        "confidence_derivation",
        "record_kind",
        "governance_status",
    )
    with closing(connect()) as connection, connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
            (trigger_name,),
        ).fetchone()
        assert trigger is not None
        original_trigger_sql = trigger["sql"]
        assert original_trigger_sql
        columns = tuple(row["name"] for row in connection.execute("PRAGMA table_info(prediction_ledger)"))
        assert columns == expected_columns

        connection.execute(f"DROP TRIGGER {trigger_name}")
        try:
            connection.execute(
                """
                INSERT INTO prediction_ledger (
                  prediction_id, created_at, target, horizon, direction, confidence,
                  rationale, counter_evidence, source_status, tags, data_snapshot_id,
                  review_status, evidence_mapping, direction_derivation, review_audit,
                  confidence_derivation, record_kind, governance_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "prediction-before",
                    "2026-06-12T09:00:00+00:00",
                    "Brent",
                    "14d",
                    "利多",
                    0.8,
                    "checkpoint-rag prediction ledger should stay out of event-direction evidence",
                    "none",
                    "connected",
                    '["checkpoint-rag"]',
                    None,
                    "pending",
                    "{}",
                    "{}",
                    "[]",
                    "{}",
                    "legacy_scalar",
                    "legacy_unverified",
                ),
            )
        finally:
            connection.execute(original_trigger_sql)
            restored_trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                (trigger_name,),
            ).fetchone()
            assert restored_trigger is not None
            assert restored_trigger["sql"] == original_trigger_sql


def test_political_case_memory_is_visible_only_after_case_time() -> None:
    upsert_political_case_memory(
        case_id="case-before",
        payload={
            "event_date": "2025-06-01T08:00:00+00:00",
            "event_type": "sanctions_geopolitics",
            "title": "checkpoint-rag historical sanctions case",
            "visible_at": "2025-06-02T08:00:00+00:00",
            "summary": "checkpoint-rag historical political case memory for sanctions and oil.",
            "stakeholders": ["buyer", "supplier"],
            "interest_map": {"buyer": "成本承压", "supplier": "议价增强"},
            "power_structure": {"dominant_actor": "供应端"},
            "stated_position": "公开表态偏强",
            "real_action": "实际执行需观察",
            "action_boundary": "只作为历史复盘，不替代当日证据。",
            "timing_window": "短周期",
            "compromise_space": "价格传导需要供需指标确认。",
            "market_reaction": "制裁消息可能出现二次发酵。",
            "transmission_path": ["原油", "PX", "PTA", "POY"],
            "affected_products": ["POY"],
            "price_direction": "利多",
            "confidence": 0.64,
            "outcome_window": "h1",
            "posterior_result": "观察",
            "lessons": ["单条政治事件不能单独行动"],
            "reusable_rules": ["必须结合价格和供需确认"],
            "evidence_refs": ["event-before"],
            "train_period": "2025",
            "metadata": {"test": True},
        },
    )
    upsert_political_case_memory(
        case_id="case-after",
        payload={
            "event_date": "2026-06-14T08:00:00+00:00",
            "event_type": "sanctions_geopolitics",
            "title": "checkpoint-rag future political case",
            "visible_at": "2026-06-14T08:00:00+00:00",
            "summary": "checkpoint-rag future political case memory.",
            "stakeholders": [],
            "interest_map": {},
            "power_structure": {},
            "transmission_path": [],
            "affected_products": [],
            "lessons": [],
            "reusable_rules": [],
            "evidence_refs": [],
            "train_period": "2026",
            "metadata": {"test": True},
        },
    )

    search = retrieve_evidence(
        "checkpoint-rag historical political sanctions case memory",
        as_of_time="2026-06-12T23:59:59+00:00",
        limit=20,
    )
    doc_ids = {item.doc_id for item in search.documents}
    assert "political_case:case-before" in doc_ids
    assert "political_case:case-after" not in doc_ids


def test_event_direction_purpose_prioritizes_business_evidence_and_case_memory() -> None:
    _insert_temporal_fixture("before", "2026-06-12T08:00:00+00:00")
    upsert_political_case_memory(
        case_id="case-sanctions-before",
        payload={
            "event_date": "2025-05-01T08:00:00+00:00",
            "event_type": "sanctions_geopolitics",
            "title": "checkpoint-rag sanctions transmission precedent",
            "visible_at": "2025-05-02T08:00:00+00:00",
            "summary": "checkpoint-rag political case: OFAC sanctions and tanker shipping can lift crude risk premium.",
            "stakeholders": ["upstream supplier", "polyester buyer"],
            "interest_map": {"upstream supplier": "risk premium", "buyer": "cost pressure"},
            "power_structure": {"dominant_actor": "sanctioning authority"},
            "stated_position": "strict enforcement",
            "real_action": "observe shipment disruption and inventory confirmation",
            "action_boundary": "must confirm with price chain.",
            "timing_window": "short",
            "compromise_space": "policy wording may not translate into physical disruption.",
            "market_reaction": "priced-in risk may fade.",
            "transmission_path": ["crude_oil", "PX", "PTA", "POY"],
            "affected_products": ["POY", "DTY"],
            "price_direction": "利多",
            "confidence": 0.72,
            "outcome_window": "h1",
            "posterior_result": "direct price pressure appeared after confirmation.",
            "lessons": ["political signal needs price and inventory confirmation"],
            "reusable_rules": ["separate public position from actual action"],
            "evidence_refs": ["event-before"],
            "train_period": "2025",
            "metadata": {"test": True},
        },
    )

    search = retrieve_evidence(
        "checkpoint-rag OFAC 制裁 shipping tanker crude oil POY cost pressure",
        as_of_time="2026-06-12T23:59:59+00:00",
        limit=8,
        purpose="event_direction",
    )

    doc_ids = {item.doc_id for item in search.documents}
    doc_types = {item.doc_type for item in search.documents}
    assert "political_case:case-sanctions-before" in doc_ids
    assert doc_types & {"news_article", "event_observation", "news_event_cluster"}
    assert doc_types & {"market_observation", "industry_observation"}
    assert doc_types & {"knowledge_node", "knowledge_edge"}
    assert not (doc_types & {"project_document", "source_config", "news_source", "prediction_record"})
    assert search.retrieval_metadata["purpose"] == "event_direction"


def _insert_temporal_fixture(suffix: str, observed_at: str) -> None:
    upsert_news_article(
        article_id=f"news-{suffix}",
        payload={
            "source_id": "ofac_recent_actions",
            "tier": "A",
            "url": f"https://example.test/news-{suffix}",
            "canonical_url": f"https://example.test/news-{suffix}",
            "title": f"checkpoint-rag {suffix} sanctions shipping signal",
            "published_at": observed_at,
            "content_hash": f"hash-{suffix}",
            "language": "en",
            "raw_text": (f"checkpoint-rag {suffix} crude oil tanker sanctions shipping. " * 15),
            "summary": f"checkpoint-rag {suffix} sanctions shipping summary",
            "score": 80,
            "category": "sanctions_geopolitics",
            "raw": {},
        },
    )
    create_event_observation(
        event_record_id=f"event-{suffix}",
        payload={
            "source_id": "ofac_recent_actions",
            "occurred_at": observed_at,
            "title": f"checkpoint-rag {suffix} event sanctions",
            "event_type": "sanctions_geopolitics",
            "evidence_level": "A",
            "summary": f"checkpoint-rag {suffix} event summary for crude and POY",
            "affected_products": ["Brent", "WTI", "POY"],
            "direction": "利多",
            "impact_strength": "high",
            "evidence_url": f"https://example.test/event-{suffix}",
            "requires_human_review": False,
            "notes": "test fixture",
            "raw": {},
        },
    )
    create_market_observation(
        observation_id=f"market-{suffix}",
        payload={
            "source_id": "eia_petroleum_api",
            "observed_at": observed_at,
            "indicator": "settlement",
            "product": "Brent",
            "value": 78.5,
            "unit": "USD/bbl",
            "frequency": "daily",
            "region": "global",
            "evidence_url": f"https://example.test/market-{suffix}",
            "notes": f"checkpoint-rag {suffix} market observation",
            "raw": {},
        },
    )
    create_industry_observation(
        observation_id=f"industry-{suffix}",
        payload={
            "source_id": "public_page_manual",
            "observed_at": observed_at,
            "product": "POY",
            "metric": "spot_quote",
            "market": "全国",
            "region": "全国",
            "value": 7500,
            "unit": "元/吨",
            "frequency": "daily",
            "evidence_level": "D",
            "evidence_url": f"https://example.test/industry-{suffix}",
            "notes": f"checkpoint-rag {suffix} industry observation",
            "raw": {},
        },
    )
    with closing(connect()) as connection, connection:
        connection.execute(
            "UPDATE news_articles SET created_at = ?, first_seen_at = ? WHERE article_id = ?",
            (observed_at, observed_at, f"news-{suffix}"),
        )
        connection.execute(
            "UPDATE event_observations SET created_at = ? WHERE event_record_id = ?",
            (observed_at, f"event-{suffix}"),
        )
        connection.execute(
            "UPDATE market_observations SET created_at = ? WHERE observation_id = ?",
            (observed_at, f"market-{suffix}"),
        )
        connection.execute(
            "UPDATE industry_observations SET created_at = ? WHERE observation_id = ?",
            (observed_at, f"industry-{suffix}"),
        )


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

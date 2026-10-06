from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from app.storage import SCHEMA

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "audit_rag_citation_coverage.py"
SPEC = importlib.util.spec_from_file_location("audit_rag_citation_coverage", SCRIPT_PATH)
assert SPEC is not None
audit_rag_citation_coverage = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["audit_rag_citation_coverage"] = audit_rag_citation_coverage
SPEC.loader.exec_module(audit_rag_citation_coverage)


def test_audit_citation_coverage_counts_missing_future_and_title_only_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        _insert_news_article(
            connection,
            article_id="visible_short",
            published_at="2026-06-01",
            raw_text="Visible short",
            title="Visible short",
        )
        _insert_news_article(
            connection,
            article_id="future",
            published_at="2026-06-20",
            raw_text="Detailed future body " * 20,
        )
        _insert_judgment(
            connection,
            judgment_id="j1",
            event_id="e1",
            as_of_time="2026-06-10T00:00:00+00:00",
            cited_doc_ids=["news_article:visible_short"],
        )
        _insert_judgment(
            connection,
            judgment_id="j2",
            event_id="e2",
            as_of_time="2026-06-10T00:00:00+00:00",
            cited_doc_ids=[],
        )
        _insert_judgment(
            connection,
            judgment_id="j3",
            event_id="e3",
            as_of_time="2026-06-10T00:00:00+00:00",
            cited_doc_ids=["news_article:future"],
        )

    report = audit_rag_citation_coverage.audit_citation_coverage(db_path, min_body_chars=80)

    assert report["metrics"]["total_llm_judgments"] == 3
    assert report["metrics"]["judgments_with_citations"] == 2
    assert report["metrics"]["citation_coverage_rate"] == 0.667
    assert report["metrics"]["avg_docs_per_judgment"] == 0.667
    assert report["metrics"]["future_leak_count"] == 1
    assert report["metrics"]["title_only_evidence_count"] == 1
    assert report["samples"]["future_leaks"][0]["doc_id"] == "news_article:future"
    assert report["samples"]["title_only_evidence"][0]["doc_id"] == "news_article:visible_short"


def test_as_of_visible_documents_do_not_count_as_future_leaks(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        _insert_news_article(
            connection,
            article_id="visible",
            published_at="2026-06-01",
            raw_text="Detailed visible body " * 20,
        )
        _insert_event_observation(connection, event_id="event_1", occurred_at="2026-06-02")
        _insert_judgment(
            connection,
            judgment_id="j1",
            event_id="e1",
            as_of_time="2026-06-10T00:00:00+00:00",
            cited_doc_ids=["news_article:visible", "event:event_1", "kg:node:crude_oil"],
        )

    report = audit_rag_citation_coverage.audit_citation_coverage(db_path, min_body_chars=80)

    assert report["metrics"]["future_leak_count"] == 0
    assert report["metrics"]["title_only_evidence_count"] == 0
    assert report["metrics"]["avg_docs_per_judgment"] == 3.0


def test_parse_doc_ids_accepts_json_and_csv_fallbacks() -> None:
    assert audit_rag_citation_coverage.parse_doc_ids('["a", "b"]') == ["a", "b"]
    assert audit_rag_citation_coverage.parse_doc_ids("a,b") == ["a", "b"]
    assert audit_rag_citation_coverage.parse_doc_ids("") == []


def test_fact_sentence_citation_coverage_is_audited(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        _insert_news_article(
            connection,
            article_id="visible",
            published_at="2026-06-01",
            raw_text="Detailed visible body " * 20,
        )
        _insert_judgment(
            connection,
            judgment_id="j1",
            event_id="e1",
            as_of_time="2026-06-10T00:00:00+00:00",
            cited_doc_ids=["news_article:visible"],
            raw={
                "fact_sentence_citations": [
                    {
                        "sentence": "EIA 库存下降支持原油价格。",
                        "cited_doc_ids": ["news_article:visible"],
                    },
                    {"sentence": "需求走弱可能抵消。", "cited_doc_ids": []},
                ]
            },
        )

    report = audit_rag_citation_coverage.audit_citation_coverage(db_path, min_body_chars=80)

    assert report["metrics"]["fact_sentence_citations"] == 2
    assert report["metrics"]["fact_sentence_citations_covered"] == 1
    assert report["metrics"]["fact_sentence_citation_coverage_rate"] == 0.5
    assert report["metrics"]["missing_fact_sentence_citation_count"] == 1
    assert report["guardrails"]["fact_sentence_citations_required"] is False
    assert report["samples"]["missing_fact_sentence_citations"][0]["sentence"] == "需求走弱可能抵消。"


def test_same_day_event_candidate_after_as_of_is_counted_as_future_leak() -> None:
    as_of = audit_rag_citation_coverage.parse_datetime("2026-06-15T00:00:00+00:00")
    observed_at = audit_rag_citation_coverage.parse_datetime("2026-06-15T06:46:07+00:00")

    assert audit_rag_citation_coverage.is_future_leak(
        doc_id="event_candidate:evt_1",
        as_of=as_of,
        observed_at=observed_at,
    )
    assert audit_rag_citation_coverage.is_future_leak(
        doc_id="news_article:future",
        as_of=as_of,
        observed_at=observed_at,
    )


def _insert_news_article(
    connection: sqlite3.Connection,
    *,
    article_id: str,
    published_at: str,
    raw_text: str,
    title: str = "Oil market update",
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
            "2026-06-01T00:00:00+00:00",
            "test_source",
            "A",
            f"https://example.test/{article_id}",
            f"https://example.test/{article_id}",
            title,
            published_at,
            "2026-06-01T00:00:00+00:00",
            f"hash-{article_id}",
            "en",
            raw_text,
            "Summary",
            80,
            "oil_policy",
            json.dumps({}),
        ),
    )


def _insert_event_observation(connection: sqlite3.Connection, *, event_id: str, occurred_at: str) -> None:
    connection.execute(
        """
        INSERT INTO event_observations (
          event_record_id, created_at, source_id, occurred_at, title, event_type, evidence_level, summary,
          affected_products, direction, impact_strength, evidence_url, requires_human_review, notes, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            "2026-06-02T00:00:00+00:00",
            "test_source",
            occurred_at,
            "Shipping alert",
            "shipping_security",
            "A",
            "Visible event",
            json.dumps(["crude_oil"]),
            "中性",
            "medium",
            "https://example.test/event",
            0,
            "",
            json.dumps({}),
        ),
    )


def _insert_judgment(
    connection: sqlite3.Connection,
    *,
    judgment_id: str,
    event_id: str,
    as_of_time: str,
    cited_doc_ids: list[str],
    reasoning: str = "Reasoning",
    raw: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO llm_event_directions (
          judgment_id, created_at, event_id, as_of_time, record_type, source_id, category, title, rule_direction,
          llm_direction, confidence, evidence_level, reasoning, counter_evidence, cited_doc_ids,
          risk_premium_decay, demand_weakness_offset, supply_recovery_offset, should_enter_backtest,
          provider, model, latency_ms, prompt_tokens_est, completion_tokens_est, fallback, error, raw
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            judgment_id,
            "2026-06-10T00:00:00+00:00",
            event_id,
            as_of_time,
            "event_observation",
            "test_source",
            "oil_policy",
            f"Judgment {event_id}",
            "中性",
            "中性",
            0.5,
            "A",
            reasoning,
            "Counter",
            json.dumps(cited_doc_ids),
            0,
            0,
            0,
            1,
            "deepseek",
            "test-model",
            10,
            100,
            20,
            0,
            "",
            json.dumps(raw or {}),
        ),
    )

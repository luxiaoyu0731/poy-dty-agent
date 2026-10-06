from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from scripts.audit_event_summary_backlog import (
    CURRENT_MODEL,
    CURRENT_PROMPT_VERSION,
    build_report,
    connect_read_only,
    request_attempt_bounds,
)


def _database(tmp_path):
    path = tmp_path / "audit.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE news_articles (
              article_id TEXT PRIMARY KEY, content_hash TEXT, raw TEXT, raw_text TEXT,
              published_at TEXT, first_seen_at TEXT, created_at TEXT
            );
            CREATE TABLE event_ai_summaries (
              article_id TEXT PRIMARY KEY, summary_status TEXT, fact_summary_status TEXT,
              impact_analysis_status TEXT, input_quality TEXT, source_hash TEXT,
              model TEXT, prompt_version TEXT, attempts INTEGER, quality_status TEXT,
              business_impact_payload TEXT, factual_summary TEXT
            );
            CREATE TABLE news_event_clusters (
              cluster_id TEXT, article_ids TEXT, event_record_id TEXT, status TEXT, affected_products TEXT
            );
            CREATE TABLE event_observations (event_record_id TEXT, affected_products TEXT);
            """
        )
        full_text_raw = json.dumps({"source_content": {"status": "full_text"}})
        articles = [
            ("complete", "h1", full_text_raw, "正文" * 500, "2026-07-01T00:00:00Z", "", ""),
            ("pending", "h2", full_text_raw, "正文" * 600, "2026-07-01T00:00:00Z", "", ""),
            ("retry", "h3", full_text_raw, "正文" * 700, "2026-07-01T00:00:00Z", "", ""),
            ("dead", "h4", full_text_raw, "正文" * 800, "2026-07-01T00:00:00Z", "", ""),
            ("rejected", "h5", full_text_raw, "正文" * 900, "2026-07-01T00:00:00Z", "", ""),
            (
                "partial",
                "h6",
                json.dumps({"source_content": {"status": "partial_text"}}),
                "片段",
                "2026-07-01T00:00:00Z",
                "",
                "",
            ),
        ]
        connection.executemany("INSERT INTO news_articles VALUES (?,?,?,?,?,?,?)", articles)
        base = (CURRENT_MODEL, CURRENT_PROMPT_VERSION)
        summaries = [
            (
                "complete",
                "completed",
                "completed",
                "completed",
                "full_text",
                "h1",
                *base,
                1,
                "completed",
                '{"relevant":true}',
                "某机构宣布实施一项有明确证据支持的措施。",
            ),
            ("pending", "pending", "pending", "not_requested", "full_text", "h2", *base, 0, "pending", "{}", ""),
            ("retry", "failed", "pending", "not_requested", "full_text", "h3", *base, 2, "pending", "{}", ""),
            ("dead", "failed", "pending", "not_requested", "full_text", "h4", *base, 3, "pending", "{}", ""),
            ("rejected", "rejected", "rejected", "not_requested", "full_text", "h5", *base, 1, "rejected", "{}", ""),
            ("partial", "pending", "pending", "not_requested", "partial_text", "h6", *base, 0, "pending", "{}", ""),
        ]
        connection.executemany("INSERT INTO event_ai_summaries VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", summaries)
        connection.execute(
            "INSERT INTO news_event_clusters VALUES (?,?,?,?,?)",
            ("cluster-1", '["complete"]', "event-1", "featured", '["PTA"]'),
        )
        connection.execute("INSERT INTO event_observations VALUES (?,?)", ("event-1", '["PTA"]'))
    return path


def test_report_is_mutually_exclusive_and_read_only(tmp_path):
    path = _database(tmp_path)
    report = build_report(path, target_ratio=0.5, daily_http_request_limit=50)

    assert report["population"] == {"articles": 6, "full_text": 5, "complete_events": 1}
    assert report["backlog"]["pending"] == 1
    assert report["backlog"]["retryable_failed"] == 1
    assert report["backlog"]["dead_letter"] == 1
    assert report["backlog"]["rejected"] == 1
    assert report["backlog"]["default_dry_run_candidates"] == 2
    assert report["backlog"]["explicit_reopen_candidates"] == 3
    assert report["target"]["additional_complete_events_needed"] == 2
    assert report["target"]["structurally_eligible_default_candidate_clusters"] == 0
    assert report["target"]["complete_event_upper_bound_without_new_promotions"] == 1
    assert report["target"]["structurally_possible_without_new_promotions"] is False
    assert report["writes_database"] is False
    assert report["provider_calls"] is False

    with (
        closing(connect_read_only(path)) as connection,
        connection,
        pytest.raises(sqlite3.OperationalError),
    ):
        connection.execute("DELETE FROM news_articles")


def test_article_limit_does_not_claim_to_be_http_request_limit():
    bounds = request_attempt_bounds(50, max_logical_calls_per_article=3, provider_retries=2)

    assert bounds == {
        "articles": 50,
        "logical_calls_min": 50,
        "logical_calls_max": 150,
        "http_attempts_min": 50,
        "http_attempts_max": 450,
    }
    assert bounds["http_attempts_max"] > 50


def test_cost_range_and_worst_case_daily_capacity_are_explicit(tmp_path):
    report = build_report(_database(tmp_path), daily_http_request_limit=50, provider_retries=2)

    assert report["request_budget"]["whole_articles_per_day_worst_case_planning_floor"] == 5
    assert report["request_budget"]["selected_article_count_is_http_request_measure"] is False
    assert report["request_budget"]["hard_http_attempt_guard_present"] is True
    assert report["request_budget"]["daily_http_attempts_max_with_guard"] == 50
    assert report["cost"]["currency"] == "USD"
    assert report["cost"]["cost_usd_high"] >= report["cost"]["cost_usd_low"] > 0

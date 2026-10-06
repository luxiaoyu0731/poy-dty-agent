from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path

from app import storage
from app.models import EventSummaryQualityResult
from app.settings import settings


def _article(connection) -> None:
    connection.execute("""INSERT INTO news_articles VALUES(
        'a1','2026-01-01','source','B','https://x/a','https://x/a','标题','2026-01-01',
        '2026-01-01','h1','zh','原始正文','采集摘要',1,'oil_policy','{}')""")


def test_event_summary_queue_is_idempotent_and_source_change_requeues(tmp_path: Path, monkeypatch) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "summary.db"))
    try:
        with closing(storage.connect()) as connection:
            _article(connection)
            connection.commit()
        storage.enqueue_event_ai_summary("a1", "h1", "deepseek-chat", "event-summary.v1")
        with closing(storage.connect()) as connection:
            queued_at = connection.execute(
                "SELECT updated_at FROM event_ai_summaries WHERE article_id='a1'"
            ).fetchone()[0]
        monkeypatch.setattr(storage, "_now", lambda: "2030-01-01T00:00:00Z")
        storage.enqueue_event_ai_summary("a1", "h1", "deepseek-chat", "event-summary.v1")
        with closing(storage.connect()) as connection:
            assert connection.execute(
                "SELECT updated_at FROM event_ai_summaries WHERE article_id='a1'"
            ).fetchone()[0] == queued_at
        assert len(storage.list_retryable_event_ai_summaries(10, 3)) == 1
        assert storage.mark_event_ai_summary_processing("a1") is True
        storage.mark_event_ai_summary_success(
            "a1", "可核验摘要", "deepseek", "deepseek-chat", "event-summary.v1", "h1", 20, 6
        )
        assert storage.list_retryable_event_ai_summaries(10, 3) == []
        with closing(storage.connect()) as connection, connection:
            connection.execute("UPDATE news_articles SET content_hash='h2',raw_text='新版本正文' WHERE article_id='a1'")
        storage.enqueue_event_ai_summary("a1", "h2", "deepseek-chat", "event-summary.v1")
        retry = storage.list_retryable_event_ai_summaries(10, 3)
        assert retry[0]["summary_status"] == "pending"
        assert retry[0]["attempts"] == 0
        assert retry[0]["factual_summary"] == ""
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def test_failed_summary_respects_attempt_cap(tmp_path: Path) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "failed.db"))
    try:
        with closing(storage.connect()) as connection:
            _article(connection)
            connection.commit()
        storage.enqueue_event_ai_summary("a1", "h1", "deepseek-chat", "event-summary.v1")
        storage.mark_event_ai_summary_failed(
            "a1", "timeout with secret omitted", "deepseek", "deepseek-chat", "event-summary.v1", "h1"
        )
        assert len(storage.list_retryable_event_ai_summaries(10, 2)) == 1
        storage.mark_event_ai_summary_failed("a1", "timeout", "deepseek", "deepseek-chat", "event-summary.v1", "h1")
        assert storage.list_retryable_event_ai_summaries(10, 2) == []
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def test_recent_article_is_not_starved_by_historical_priority(tmp_path: Path, monkeypatch) -> None:
    from dataclasses import replace
    monkeypatch.setattr(storage, "settings", replace(settings, sqlite_path=str(tmp_path / "priority.db")))
    with closing(storage.connect()) as connection, connection:
        _article(connection)
        connection.execute("UPDATE news_articles SET raw=? WHERE article_id='a1'",
                           ('{"analysis":{"affected_products":["dty"]}}',))
        connection.execute("""INSERT INTO news_articles VALUES(
            'a2','2026-01-02','source','B','https://x/b','https://x/b','原油新闻','2026-01-02',
            datetime('now'),'h2','zh','原始正文2','',1,'oil_policy','{}')""")
    storage.enqueue_event_ai_summary("a1", "h1", "deepseek-chat", "event-summary.v1")
    storage.enqueue_event_ai_summary("a2", "h2", "deepseek-chat", "event-summary.v1")
    assert [r['article_id'] for r in storage.list_retryable_event_ai_summaries(2, 3)] == ['a2', 'a1']


def test_retryable_summary_query_can_be_scoped_to_requested_articles(tmp_path: Path) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "targeted.db"))
    try:
        with closing(storage.connect()) as connection:
            _article(connection)
            connection.execute("""INSERT INTO news_articles VALUES(
                'a2','2026-01-02','source','B','https://x/b','https://x/b','标题2','2026-01-02',
                '2026-01-02','h2','zh','原始正文2','',1,'oil_policy','{}')""")
            connection.commit()
        storage.enqueue_event_ai_summary("a1", "h1", "deepseek-chat", "event-summary.v1")
        storage.enqueue_event_ai_summary("a2", "h2", "deepseek-chat", "event-summary.v1")

        targeted = storage.list_retryable_event_ai_summaries(10, 3, article_ids=["a2"])

        assert [row["article_id"] for row in targeted] == ["a2"]
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def test_queue_monitor_recovers_stale_and_counts_exhaustion(tmp_path: Path) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "monitor.db"))
    try:
        with closing(storage.connect()) as connection:
            _article(connection)
            connection.execute("""INSERT INTO news_articles VALUES(
                'a2','2026-01-02','source','B','https://x/b','https://x/b','标题2','2026-01-02',
                '2026-01-02','h2','zh','原始正文2','',1,'oil_policy','{}')""")
            connection.commit()
        assert storage.enqueue_all_missing_event_ai_summaries("deepseek-chat", "event-summary.v1", 1) == 2
        assert storage.enqueue_all_missing_event_ai_summaries("deepseek-chat", "event-summary.v1", 1) == 0
        assert storage.mark_event_ai_summary_processing("a1")
        with closing(storage.connect()) as connection:
            connection.execute("UPDATE event_ai_summaries SET updated_at='2020-01-01' WHERE article_id='a1'")
            connection.execute("UPDATE event_ai_summaries SET summary_status='failed',attempts=3 WHERE article_id='a2'")
            connection.commit()
        before = storage.event_ai_summary_queue_counts(3)
        assert before["not_queued"] == 0
        assert before["stale_processing"] == 1
        assert before["attempt_exhausted"] == 1
        assert storage.recover_stale_event_ai_summary_leases() == 1
        after = storage.event_ai_summary_queue_counts(3)
        assert after["processing"] == 0
        assert after["retryable_failed"] == 1
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def test_news_upsert_does_not_replace_full_text_with_partial_excerpt(tmp_path: Path) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "preserve-full.db"))
    full_text = "完整正文" * 300
    try:
        full_payload = {
            "source_id": "source",
            "tier": "B",
            "url": "https://x/a",
            "canonical_url": "https://x/a",
            "title": "标题",
            "published_at": "2026-01-01",
            "first_seen_at": "2026-01-01",
            "content_hash": "full-hash",
            "language": "zh",
            "raw_text": full_text,
            "summary": "",
            "score": 80,
            "category": "oil_policy",
            "raw": {"summary_input_quality": {"level": "full_text"}},
        }
        storage.upsert_news_article(article_id="a1", payload=full_payload)
        storage.upsert_news_article(
            article_id="a1",
            payload={
                **full_payload,
                "content_hash": "partial-hash",
                "raw_text": "短摘录",
                "raw": {"summary_input_quality": {"level": "partial_text"}},
            },
        )

        with closing(storage.connect()) as connection:
            row = connection.execute(
                "SELECT content_hash,raw_text,raw FROM news_articles WHERE article_id='a1'"
            ).fetchone()
        assert row["content_hash"] == "full-hash"
        assert row["raw_text"] == full_text
        assert '"level": "full_text"' in row["raw"]
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def test_rejected_summary_requeues_when_hydrated_source_changes(tmp_path: Path) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "requeue-rejected.db"))
    try:
        with closing(storage.connect()) as connection:
            _article(connection)
            connection.execute("UPDATE news_articles SET content_hash='partial-hash' WHERE article_id='a1'")
            connection.commit()
        storage.enqueue_event_ai_summary("a1", "partial-hash", "deepseek-chat", "event-summary.v1")
        with closing(storage.connect()) as connection:
            connection.execute("""UPDATE event_ai_summaries
                   SET summary_status='rejected',quality_status='rejected',
                       quality_reasons='["insufficient_source_text"]',attempts=1
                   WHERE article_id='a1'""")
            connection.commit()

        with closing(storage.connect()) as connection, connection:
            connection.execute(
                "UPDATE news_articles SET content_hash='full-hash',raw_text='已取得正文' WHERE article_id='a1'"
            )
        storage.enqueue_event_ai_summary("a1", "full-hash", "deepseek-chat", "event-summary.v1")

        retry = storage.list_retryable_event_ai_summaries(10, 3)
        assert len(retry) == 1
        assert retry[0]["summary_status"] == "pending"
        assert retry[0]["attempts"] == 0
        assert retry[0]["source_hash"] == "full-hash"
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def test_grounded_result_persists_independent_fact_and_impact_statuses(tmp_path: Path) -> None:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "stage-statuses.db"))
    try:
        with closing(storage.connect()) as connection:
            _article(connection)
            connection.commit()
        storage.enqueue_event_ai_summary("a1", "h1", "deepseek-chat", "event-summary.v2")
        result = EventSummaryQualityResult(
            status="completed",
            usable=True,
            input_quality="full_text",
            fact_summary_status="completed",
            impact_analysis_status="irrelevant",
            factual_summary="可核验事实摘要",
            impact_quality_reasons=["project_irrelevant"],
        )

        storage.mark_event_ai_grounded_summary_result(
            "a1",
            result,
            provider="deepseek",
            model="deepseek-chat",
            prompt_version="event-summary.v2",
            source_hash="h1",
            input_chars=20,
        )

        with closing(storage.connect()) as connection:
            row = connection.execute("""SELECT schema_version,fact_summary_status,impact_analysis_status,
                          impact_quality_reasons
                   FROM event_ai_summaries WHERE article_id='a1'""").fetchone()
        assert dict(row) == {
            "schema_version": "event-summary.v2",
            "fact_summary_status": "completed",
            "impact_analysis_status": "irrelevant",
            "impact_quality_reasons": '["project_irrelevant"]',
        }
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def test_retryable_summary_selection_prioritizes_business_evidence(tmp_path: Path) -> None:
    """Product-linked A/B sources must not queue behind C-tier aggregator reposts."""
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "priority.db"))
    try:
        with closing(storage.connect()) as connection:
            # Oldest queue entry: aggregator repost without affected products.
            connection.execute("""INSERT INTO news_articles VALUES(
                'agg','2026-01-01','google_news_oil_rss','C','https://x/a','https://x/a','t1','2026-01-01',
                '2026-01-01','h1','en','body','s',1,'sanctions_geopolitics','{}')""")
            # Later queue entry: direct B-tier price quote linked to a product.
            connection.execute("""INSERT INTO news_articles VALUES(
                'quote','2026-01-02','texnet_polyester_news','B','https://x/b','https://x/b','t2','2026-01-02',
                '2026-01-02','h2','zh','正文','s',78,'company_capacity',
                '{"analysis":{"affected_products":["dty"]}}')""")
            # Later C-tier entry with a product link (aggregated repost).
            connection.execute("""INSERT INTO news_articles VALUES(
                'agg2','2026-01-03','google_news_oil_rss','C','https://x/c','https://x/c','t3','2026-01-03',
                '2026-01-03','h3','en','body','s',1,'oil_policy',
                '{"analysis":{"affected_products":["crude"]}}')""")
            connection.commit()
        for article, source_hash in (("agg", "h1"), ("quote", "h2"), ("agg2", "h3")):
            storage.enqueue_event_ai_summary(article, source_hash, "deepseek-chat", "event-summary.v1")
        rows = storage.list_retryable_event_ai_summaries(10, 3)
        # Product-linked direct B-tier evidence first; product links outrank FIFO,
        # so the crude-linked aggregator also precedes the no-product repost.
        assert [row["article_id"] for row in rows] == ["quote", "agg2", "agg"]
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def test_access_restricted_article_upgrades_and_requeues_on_recovery(tmp_path: Path) -> None:
    """A1 recovery chain: title_only ingest -> full_text re-fetch -> worker re-enqueue.

    PPI-style access-restricted pages are stored title_only and never queued;
    when the source later serves the full body the upsert must upgrade the
    stored grade/content hash so the summary worker re-enqueues the article.
    """
    from datetime import UTC, datetime

    from scripts.backfill_event_deepseek_summaries import select_candidates

    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "recovery.db"))
    storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
    try:
        base_payload = {
            "source_id": "ppi_commodity_news",
            "tier": "B",
            "url": "https://www.100ppi.com/news/detail-20260920-6247554.html",
            "canonical_url": "https://www.100ppi.com/news/detail-20260920-6247554.html",
            "title": "生意社石脑油9月20日均差为224.50元/吨",
            "published_at": "2026-09-20T10:35:44+00:00",
            "first_seen_at": "2026-09-20T10:35:44+00:00",
            "language": "zh",
            "summary": "价格公告",
            "score": 60,
            "category": "energy",
        }
        restricted_payload = {
            **base_payload,
            "content_hash": "hash-title-only",
            "raw_text": "生意社石脑油9月20日均差为224.50元/吨",
            "raw": {
                "source_content": {
                    "status": "title_only",
                    "reason": "access_restricted",
                    "text_chars": 20,
                }
            },
        }
        storage.upsert_news_article(article_id="art-recovery-1", payload=restricted_payload)
        recovered_payload = {
            **base_payload,
            "content_hash": "hash-full-text",
            "raw_text": "生意社石脑油9月20日均差为224.50元/吨。据生意社监测，9月20日石脑油基准价"
            "9350.00元/吨，较前一交易日上调，均差为224.50元/吨，由正向扩大转为缩小。",
            "raw": {
                "source_content": {
                    "status": "full_text",
                    "reason": "resolved",
                    "text_chars": 74,
                }
            },
        }
        storage.upsert_news_article(article_id="art-recovery-1", payload=recovered_payload)

        with closing(storage.connect()) as connection:
            row = connection.execute(
                """SELECT content_hash, json_extract(raw, '$.source_content.status') AS grade
                   FROM news_articles WHERE article_id='art-recovery-1'"""
            ).fetchone()
        assert row["content_hash"] == "hash-full-text"
        assert row["grade"] == "full_text"

        rows = [
            {
                "article_id": "art-recovery-1",
                "content_hash": row["content_hash"],
                "published_at": base_payload["published_at"],
                "first_seen_at": base_payload["first_seen_at"],
                "created_at": base_payload["first_seen_at"],
                "raw": json.dumps(recovered_payload["raw"], ensure_ascii=False),
                "source_id": base_payload["source_id"],
                "tier": "B",
                "url": base_payload["url"],
                "title": base_payload["title"],
            }
        ]
        selected = select_candidates(rows, now=datetime.now(UTC), days=30, model="m", prompt_version="v")
        assert [row["article_id"] for row in selected] == ["art-recovery-1"]
        restricted_selected = select_candidates(
            [
                {**rows[0], "content_hash": "hash-title-only", "raw": json.dumps(restricted_payload["raw"])},
            ],
            now=datetime.now(UTC),
            days=30,
            model="m",
            prompt_version="v",
        )
        assert restricted_selected == []

        storage.enqueue_event_ai_summary("art-recovery-1", "hash-full-text", "m", "v")
        retry = storage.list_retryable_event_ai_summaries(10, 3)
        assert retry and retry[0]["article_id"] == "art-recovery-1"
        assert retry[0]["summary_status"] == "pending"
    finally:
        storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
        object.__setattr__(settings, "sqlite_path", original)

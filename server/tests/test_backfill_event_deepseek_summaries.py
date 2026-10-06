import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from app import storage
from app.settings import settings
from scripts.backfill_event_deepseek_summaries import (
    create_backup,
    degradation_decision,
    requeue_rejected_candidate,
    run_usage,
    select_candidates,
)


def test_select_candidates_enforces_fourteen_day_full_text_boundary_and_is_idempotent():
    now = datetime(2026, 7, 22, 0, 0, tzinfo=UTC)
    rows = [
        {
            "article_id": "new-full",
            "published_at": "2026-07-09T00:00:00Z",
            "raw": '{"source_content":{"status":"full_text"}}',
            "summary_status": "failed",
            "source_hash": "h1",
            "content_hash": "h1",
            "model": "m",
            "prompt_version": "v2",
        },
        {
            "article_id": "too-old",
            "published_at": "2026-07-07T23:59:59Z",
            "raw": '{"source_content":{"status":"full_text"}}',
            "summary_status": "failed",
            "source_hash": "h2",
            "content_hash": "h2",
            "model": "m",
            "prompt_version": "v2",
        },
        {
            "article_id": "partial",
            "published_at": "2026-07-20T00:00:00Z",
            "raw": '{"source_content":{"status":"partial_text"}}',
            "summary_status": "failed",
            "source_hash": "h3",
            "content_hash": "h3",
            "model": "m",
            "prompt_version": "v2",
        },
        {
            "article_id": "done",
            "published_at": "2026-07-20T00:00:00Z",
            "raw": '{"source_content":{"status":"full_text"}}',
            "summary_status": "completed",
            "source_hash": "h4",
            "content_hash": "h4",
            "model": "m",
            "prompt_version": "v2",
        },
    ]
    selected = select_candidates(rows, now=now, days=14, model="m", prompt_version="v2")
    assert [row["article_id"] for row in selected] == ["new-full"]


def test_select_candidates_accepts_rfc2822_publication_dates():
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    rows = [
        {
            "article_id": "rfc-full",
            "published_at": "Thu, 23 Jul 2026 08:00:00 GMT",
            "raw": '{"source_content":{"status":"full_text"}}',
            "summary_status": "rejected",
            "source_hash": "h1",
            "content_hash": "h1",
            "model": "old-model",
            "prompt_version": "event-facts-v1",
        }
    ]

    selected = select_candidates(rows, now=now, days=14, model="m", prompt_version="event-grounded-v2")

    assert [row["article_id"] for row in selected] == ["rfc-full"]


def test_select_candidates_prefers_current_source_grade_over_stale_summary_grade():
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    rows = [
        {
            "article_id": "stale-full-grade",
            "published_at": "2026-07-24T08:00:00Z",
            "raw": '{"source_content":{"status":"partial_text"}}',
            "input_quality": "full_text",
            "summary_status": "completed",
            "source_hash": "old",
            "content_hash": "new",
            "model": "old-model",
            "prompt_version": "old-prompt",
        }
    ]

    selected = select_candidates(
        rows,
        now=now,
        days=14,
        model="deepseek-chat",
        prompt_version="event-grounded-v7",
    )

    assert selected == []


def test_current_rejected_summary_requires_explicit_retry_opt_in():
    now = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
    rows = [
        {
            "article_id": "rejected-v6",
            "published_at": "2026-07-24T08:00:00Z",
            "raw": '{"source_content":{"status":"full_text"}}',
            "input_quality": "full_text",
            "summary_status": "rejected",
            "source_hash": "h1",
            "content_hash": "h1",
            "model": "deepseek-chat",
            "prompt_version": "event-grounded-v6-zh-summary",
        }
    ]

    default = select_candidates(
        rows,
        now=now,
        days=14,
        model="deepseek-chat",
        prompt_version="event-grounded-v6-zh-summary",
    )
    opted_in = select_candidates(
        rows,
        now=now,
        days=14,
        model="deepseek-chat",
        prompt_version="event-grounded-v6-zh-summary",
        retry_rejected=True,
    )

    assert default == []
    assert [row["article_id"] for row in opted_in] == ["rejected-v6"]


def test_explicit_rejected_requeue_and_usage_are_scoped_to_current_run(tmp_path: Path):
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "summary-retry.db"))
    try:
        with closing(storage.connect()) as connection, connection:
            for article_id in ("candidate", "historical"):
                connection.execute(
                    """INSERT INTO news_articles VALUES(
                    ?, '2026-07-24', 'source', 'B', ?, ?, ?, '2026-07-24',
                    '2026-07-24', ?, 'zh', ?, '', 1, 'oil_policy', '{}')""",
                    (
                        article_id,
                        f"https://x/{article_id}",
                        f"https://x/{article_id}",
                        article_id,
                        f"hash-{article_id}",
                        "可核验正文" * 30,
                    ),
                )
            connection.execute("""INSERT INTO event_ai_summaries(
                    article_id,factual_summary,summary_status,quality_status,quality_reasons,
                    model,prompt_version,source_hash,attempts,input_chars,output_chars,updated_at
                ) VALUES(
                    'candidate','旧摘要','rejected','rejected','["gate"]',
                    'deepseek-chat','event-grounded-v6-zh-summary','hash-candidate',2,900,90,
                    '2026-07-24T08:00:00+00:00'
                )""")
            connection.execute("""INSERT INTO event_ai_summaries(
                    article_id,factual_summary,summary_status,quality_status,
                    model,prompt_version,source_hash,attempts,input_chars,output_chars,updated_at
                ) VALUES(
                    'historical','历史摘要','completed','completed',
                    'deepseek-chat','event-grounded-v6-zh-summary','hash-historical',1,500000,50000,
                    '2026-07-20T08:00:00+00:00'
                )""")

        assert requeue_rejected_candidate(
            "candidate",
            source_hash="hash-candidate",
            model="deepseek-chat",
            prompt_version="event-grounded-v6-zh-summary",
        )
        with closing(storage.connect()) as connection, connection:
            row = connection.execute("SELECT * FROM event_ai_summaries WHERE article_id='candidate'").fetchone()
            assert row["summary_status"] == "pending"
            assert row["attempts"] == 0
            assert row["factual_summary"] == ""
            connection.execute("""UPDATE event_ai_summaries
                   SET summary_status='completed',input_chars=1200,output_chars=180,
                       updated_at='2026-07-24T10:00:00+00:00'
                   WHERE article_id='candidate'""")

        assert run_usage(
            ["candidate"],
            prompt_version="event-grounded-v6-zh-summary",
            started_at="2026-07-24T09:00:00+00:00",
        ) == (1200, 180)
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def test_create_backup_precedes_writes_and_passes_integrity_check(tmp_path: Path):
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES('before')")
    backup = create_backup(database, tmp_path / "backups")
    assert backup.exists()
    with closing(sqlite3.connect(backup)) as connection, connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM marker").fetchone()[0] == "before"


def test_legacy_snippet_or_disclaimer_is_degraded_but_grounded_full_text_is_kept():
    snippet = {
        "source_id": "google_news_oil_rss",
        "url": "https://news.google.com/rss/articles/x",
        "title": "Oil rises",
        "raw_text": "Oil rises on supply concerns " * 10,
        "factual_summary": "油价上涨，但原文未说明具体幅度。",
        "quality_status": "pending",
    }
    grade, reasons = degradation_decision(snippet)
    assert grade == "partial_text"
    assert reasons == ["input_not_full_text", "legacy_low_information_summary", "legacy_unverified_quality_gate"]

    full = {
        "source_id": "us_centcom_press",
        "url": "https://www.centcom.mil/MEDIA/PRESS-RELEASES/x",
        "title": "Port update",
        "raw_text": "The port authority announced operations resumed. " * 20,
        "factual_summary": "港口管理局宣布恢复作业。",
        "quality_status": "usable",
    }
    grade, reasons = degradation_decision(full)
    assert grade == "full_text"
    assert reasons == []

    legacy_unverified = {**full, "quality_status": "pending"}
    grade, reasons = degradation_decision(legacy_unverified)
    assert grade == "full_text"
    assert reasons == ["legacy_unverified_quality_gate"]

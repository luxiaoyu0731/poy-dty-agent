from __future__ import annotations

import asyncio
import json
from contextlib import closing
from dataclasses import replace

import pytest

from app import news, storage, summary_revision_archive
from app.settings import settings
from scripts import recover_historical_summaries as recovery
from scripts.backfill_news_article_bodies import source_for


@pytest.fixture
def db(tmp_path, monkeypatch):
    config = replace(settings, sqlite_path=str(tmp_path / "recovery.sqlite"))
    monkeypatch.setattr(storage, "settings", config)
    return tmp_path / "recovery.sqlite"


def seed(db, *, status="completed", impact="irrelevant", attempts=3):
    from pathlib import Path

    assert Path(storage.settings.sqlite_path) == db
    url = "https://www.sunsirs.com/uk/detail_news-54321.html"
    body = "原油与聚酯市场供需稳定。乙二醇库存增加，PTA供应正常。" * 30
    article_id = news._id("art", url)
    storage.upsert_news_article(
        article_id=article_id,
        payload={
            "source_id": "ppi_commodity_news",
            "tier": "B",
            "url": url,
            "canonical_url": url,
            "title": "聚酯原料市场分析",
            "content_hash": "h1",
            "raw_text": body,
            "published_at": "2024-01-01",
            "first_seen_at": "2024-01-02T00:00:00+00:00",
            "raw": {"source_content": {"body_method": "semantic_body_hint"}},
        },
    )
    storage.enqueue_event_ai_summary(article_id, "h1", "test", "v10")
    with closing(storage.connect()) as con, con:
        con.execute(
            "UPDATE event_ai_summaries SET summary_status=?, impact_analysis_status=?, "
            "attempts=?, factual_summary=? WHERE article_id=?",
            (status, impact, attempts, "原来的事实摘要", article_id),
        )
    return article_id


def row(db, article):
    return recovery.read_rows(db, article)[0]["summary"]


def test_all_history_includes_irrelevant_but_preserves_related(db):
    article = seed(db)
    plan = recovery.make_plan(recovery.read_rows(db))
    assert plan["counts"] == {"summary_ready": 1}  # Older than 365 days.
    with closing(storage.connect()) as con, con:
        con.execute("UPDATE event_ai_summaries SET impact_analysis_status='completed'")
    assert recovery.make_plan(recovery.read_rows(db))["counts"] == {"preserve_related": 1}
    assert row(db, article)["attempts"] == 3


def test_manifest_requeue_archives_attempts_and_resumes_without_reset(db, tmp_path):
    article = seed(db)
    original = row(db, article)
    plan = recovery.make_plan(recovery.read_rows(db))
    out = tmp_path / "batch"
    result = asyncio.run(recovery.apply_batch(db, plan, out, limit=20, model="test"))
    assert result["counts"] == {"queued": 1}
    current = row(db, article)
    assert current["prompt_version"] == news.EVENT_SUMMARY_PROMPT_VERSION
    assert current["attempts"] == 0
    assert current["summary_status"] == "pending"
    archived = list((tmp_path / "summary-revision-archive").glob("*.json"))
    assert any(json.loads(p.read_text()) == original for p in archived)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in archived)
    with closing(storage.connect()) as con, con:
        con.execute("UPDATE event_ai_summaries SET summary_status='rejected', attempts=3")
    again = asyncio.run(recovery.apply_batch(db, plan, out, limit=20, model="test"))
    assert again["counts"] == {"rejected": 1}
    assert row(db, article)["attempts"] == 3
    assert recovery.read_rows(db, article)[0]["article"]["first_seen_at"] == "2024-01-02T00:00:00+00:00"


def test_plan_tampering_cannot_write(db, tmp_path):
    article = seed(db)
    plan = recovery.make_plan(recovery.read_rows(db))
    plan["items"][0]["source_hash"] = "forged"
    with pytest.raises(ValueError, match="plan_mismatch"):
        asyncio.run(recovery.apply_batch(db, plan, tmp_path / "batch", limit=20, model="test"))
    assert row(db, article)["attempts"] == 3


def test_rejected_but_retryable_result_is_not_reported_as_terminal():
    record = {"summary": {"prompt_version": news.EVENT_SUMMARY_PROMPT_VERSION,
                          "source_hash": "h1", "summary_status": "rejected", "attempts": 1}}
    assert recovery.worker_result(record, "h1") == "queued"
    record["summary"]["attempts"] = 3
    assert recovery.worker_result(record, "h1") == "rejected"


@pytest.mark.parametrize("prior_contention", [
    None, "OperationalError", "InterruptedError", "discovery_identity_repaired", "publisher_layout_repaired",
])
def test_worker_prompt_upgrade_does_not_skip_approved_body_recovery(db, tmp_path, monkeypatch, prior_contention):
    article_id = seed(db, status="rejected", impact="not_requested")
    with closing(storage.connect()) as con, con:
        con.execute("UPDATE news_articles SET raw_text=?,raw='{}'", ("截断的正文" * 1000,))
    plan = recovery.make_plan(recovery.read_rows(db))
    assert plan["counts"] == {"body_recovery": 1}
    storage.enqueue_event_ai_summary(article_id, "h1", "test", news.EVENT_SUMMARY_PROMPT_VERSION)
    with closing(storage.connect()) as con, con:
        con.execute("UPDATE event_ai_summaries SET summary_status='rejected',attempts=1")
    called = []

    async def restore(rows, state, output, concurrency):
        called.append(rows[0]["article_id"])
        with closing(storage.connect()) as con, con:
            con.execute(
                "UPDATE news_articles SET content_hash='restored',raw_text=?", ("原油与聚酯原料供应正常。" * 90,)
            )
        return []

    monkeypatch.setattr(recovery, "recover", restore)
    if prior_contention:
        recovery.write_private(tmp_path / "batch/body-acquisition-state.json", {
            article_id: {"content_hash": "h1", "attempts": 1, "reason": prior_contention, "status": "failed"}
        })
    result = asyncio.run(recovery.apply_batch(db, plan, tmp_path / "batch", limit=20, model="test"))
    assert called == [article_id]
    assert result["counts"] == {"queued": 1}
    assert row(db, article_id)["source_hash"] == "restored"
    assert any(json.loads(p.read_text())["prompt_version"] == news.EVENT_SUMMARY_PROMPT_VERSION
               for p in (tmp_path / "summary-revision-archive").glob("*.json"))


def test_prompt_upgrade_keeps_completed_results_until_explicit_recheck(db):
    article = seed(db)
    old = row(db, article)
    storage.enqueue_event_ai_summary(article, "h1", "test", "v11")
    assert row(db, article) == old


def test_archive_failure_aborts_reset(db, monkeypatch):
    article = seed(db, status="failed")
    original = row(db, article)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(summary_revision_archive, "archive_summary_revision", fail)
    with pytest.raises(OSError, match="disk full"):
        storage.enqueue_event_ai_summary(article, "h1", "test", "v11")
    assert row(db, article) == original


@pytest.mark.parametrize("change", ["processing", "source", "concurrent_summary"])
def test_cannot_overwrite_lease_changed_source_or_concurrent_result(db, change):
    article = seed(db)
    original = row(db, article)
    with closing(storage.connect()) as con, con:
        if change == "source":
            con.execute("UPDATE news_articles SET content_hash='h2'")
        elif change == "processing":
            con.execute("UPDATE event_ai_summaries SET summary_status='processing'")
        else:
            con.execute("UPDATE event_ai_summaries SET factual_summary='新结果'")
    before = row(db, article)
    storage.enqueue_event_ai_summary(
        article, "h1", "test", "v11", recheck_completed=True, expected_revision_hash=recovery.digest(original)
    )
    assert row(db, article) == before


def test_historical_discovery_source_uses_existing_host_controls():
    source = source_for({"source_id": "google_news_v2_oil_policy"})
    assert source and source.cadence == "archive"
    item = news.RawNewsItem(
        source_id=source.source_id, tier="C", url="https://unapproved.example/story", title="原油新闻"
    )
    assert not news._should_fetch_detail(item, source=source)
    assert source_for({"source_id": "ccf_dom_daily"}) is None


def test_body_failure_stays_boundary_and_is_not_retried_on_resume(db, tmp_path, monkeypatch):
    article = seed(db, status="rejected")
    with closing(storage.connect()) as con, con:
        con.execute("UPDATE news_articles SET raw_text='仅有新闻标题'")
    plan = recovery.make_plan(recovery.read_rows(db))
    assert plan["counts"] == {"body_recovery": 1}
    calls = []

    async def unavailable(rows, state, output, concurrency):
        calls.append(rows[0]["article_id"])
        state[article] = {"attempts": 1, "reason": "access_restricted"}
        recovery.write_private(output.with_name("body-acquisition-state.json"), state)

    monkeypatch.setattr(recovery, "recover", unavailable)
    out = tmp_path / "batch"
    result = asyncio.run(recovery.apply_batch(db, plan, out, limit=20, model="test"))
    assert result["counts"] == {"body_unavailable": 1}
    asyncio.run(recovery.apply_batch(db, plan, out, limit=20, model="test"))
    assert calls == [article]
    assert row(db, article)["attempts"] == 3


def test_body_compare_and_swap_does_not_overwrite_a_newer_capture(db):
    article = seed(db)
    original = recovery.read_rows(db, article)[0]["article"]
    with closing(storage.connect()) as con, con:
        con.execute("UPDATE news_articles SET content_hash='newer'")
    with pytest.raises(ValueError, match="article_changed_during_body_recovery"):
        storage.upsert_news_article(article_id=article, payload=original, expected_content_hash="h1")
    assert recovery.read_rows(db, article)[0]["article"]["content_hash"] == "newer"

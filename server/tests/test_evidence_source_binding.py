"""Source/body identity must survive re-polls and async summary consumption."""
from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace

import pytest

from app import news, storage
from app.industrial_intelligence.analysis import detect_products
from app.models import EventSummaryQualityResult
from app.settings import settings


@pytest.fixture
def isolated_source(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "settings", replace(settings, sqlite_path=str(tmp_path / "source-binding.sqlite")))
    body = "The refinery confirmed that crude oil production resumed after repairs. " * 20
    payload = dict(source_id="google_news_oil_rss", tier="C", url="https://publisher.example/story",
                   canonical_url="https://publisher.example/story", title="Crude oil production resumes",
                   published_at="2026-09-24", first_seen_at="2026-09-24T00:00:00Z", language="en",
                   raw_text=body, content_hash="current-body", summary="", score=60, category="oil_supply",
                   raw={"summary_input_quality": {"level": "full_text"}})
    storage.upsert_news_article(article_id="source-1", payload=payload)
    return payload


def test_lower_quality_repoll_preserves_whole_source_identity(isolated_source):
    original = isolated_source
    returned, _ = storage.upsert_news_article(article_id="source-1", payload={
        **original, "source_id": "google_news_chemical_rss", "tier": "B", "language": "zh",
        "url": "https://news.google.com/rss/articles/discovery",
        "canonical_url": "https://news.google.com/rss/articles/discovery",
        "title": "New wrapper headline", "published_at": "2026-09-26",
        "raw_text": "Short wrapper", "content_hash": "wrapper-hash",
        "raw": {"summary_input_quality": {"level": "partial_text"}},
    })
    with closing(storage.connect()) as con:
        row = dict(con.execute("SELECT * FROM news_articles WHERE article_id='source-1'").fetchone())
    for key in ("raw_text", "content_hash", "title", "url", "canonical_url",
                "source_id", "tier", "language", "published_at"):
        assert row[key] == original[key], key
        assert returned[key] == row[key], key


def test_full_revision_still_updates_content_and_visibility(isolated_source):
    returned, _ = storage.upsert_news_article(article_id="source-1", payload={
        **isolated_source, "raw_text": "Updated verified article", "content_hash": "new-version",
        "title": "Updated production statement", "published_at": "2026-09-25",
    })
    assert returned["content_hash"] == "new-version"
    assert returned["title"] == "Updated production statement"
    assert returned["raw"]["content_visible_at"] != returned["first_seen_at"]


def test_consumer_uses_resolved_source_and_only_matching_content_version(isolated_source):
    storage.enqueue_event_ai_summary("source-1", "current-body", "test", news.EVENT_SUMMARY_PROMPT_VERSION)
    with closing(storage.connect()) as con, con:
        con.execute("UPDATE news_articles SET url='https://news.google.com/rss/articles/old-wrapper'")

    class Client:
        model = "test"
        called = False

        async def summarize_event_grounded(self, **kwargs):
            self.called = True
            assert kwargs["raw_text"] == isolated_source["raw_text"].strip()
            return EventSummaryQualityResult(status="rejected", usable=False,
                                             input_quality="full_text", rejection_reasons=["test_only"])

    client = Client()
    asyncio.run(news.process_event_summary_queue(limit=1, client=client, article_ids=["source-1"]))
    assert client.called, "Resolved publisher body was misclassified as a Google discovery snippet"
    with closing(storage.connect()) as con, con:
        con.execute("UPDATE news_articles SET content_hash='newer-body-version'")
    assert storage.list_retryable_event_ai_summaries(1, 3, article_ids=["source-1"]) == []


def test_queue_never_pairs_stale_hash_with_new_body(isolated_source):
    storage.enqueue_event_ai_summary("source-1", "stale-version", "test", news.EVENT_SUMMARY_PROMPT_VERSION)
    assert storage.list_retryable_event_ai_summaries(1, 3, article_ids=["source-1"]) == []
    storage.enqueue_event_ai_summary("source-1", "current-body", "test", news.EVENT_SUMMARY_PROMPT_VERSION)
    rows = storage.list_retryable_event_ai_summaries(1, 3, article_ids=["source-1"])
    assert len(rows) == 1 and rows[0]["source_hash"] == rows[0]["content_hash"] == "current-body"


@pytest.mark.parametrize(("text", "absent"), [
    ("Questerre tested oil shale plant technology at PX Energy. The PX project reduces fuel oil consumption.", "px"),
    ("PX Ltd workers at the nuclear plant went on strike. A PX spokesperson described the plant supply plan.", "px"),
    ("MEG Technical Analysis & Stock Price Forecast. Stock MEG trades at the closing price.", "meg"),
    ("Terminal end groups of poly(ethylene glycol) reduce antigenicity.", "meg"),
    ("聚乙二醇供应与价格变动。", "meg"),
])
def test_company_ticker_and_polymer_are_not_forecast_commodities(text, absent):
    assert absent not in detect_products(text)


@pytest.mark.parametrize(("text", "present"), [
    ("PX and PTA polyester feedstock prices increased.", "px"),
    ("MEG prices fell as polyester feedstock inventories increased.", "meg"),
    ("MEG stock levels declined at the chemical plant.", "meg"),
    ("Poly(ethylene glycol) is produced using ethylene glycol as feedstock.", "meg"),
    ("聚乙二醇生产使用乙二醇原料。", "meg"),
    ("PX Energy announced an oil shale test; paraxylene supply was discussed separately.", "px"),
])
def test_real_chemical_mentions_remain_available(text, present):
    assert present in detect_products(text)

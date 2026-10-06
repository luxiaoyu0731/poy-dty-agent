from __future__ import annotations

import asyncio
from contextlib import closing
from dataclasses import replace

import httpx
import pytest

from app import news, storage
from app.settings import settings


@pytest.fixture(autouse=True)
def cooldown(monkeypatch):
    monkeypatch.setattr(news, "_google_news_resolution_retry_at", 0.0)


@pytest.mark.parametrize("host", ["www.csis.org", "www.cfr.org", "maritime-executive.com",
                                      "www.aljazeera.com", "www.theguardian.com", "www.arabnews.com"])
def test_curated_public_publisher_requires_both_permissions(monkeypatch, host):
    item = news.RawNewsItem("google_news_oil_rss", "C", f"https://{host}/article", "Oil")
    monkeypatch.setattr(news, "settings", replace(settings, outbound_hosts=(host,)))
    monkeypatch.setattr(news, "_source_registry_auth_by_host", lambda: {})
    assert news._should_fetch_detail(item)
    monkeypatch.setattr(news, "settings", replace(settings, outbound_hosts=()))
    assert not news._should_fetch_detail(item)
    monkeypatch.setattr(news, "settings", replace(settings, outbound_hosts=(host,)))
    monkeypatch.setattr(news, "_source_registry_auth_by_host", lambda: {host: "subscription"})
    assert not news._should_fetch_detail(item)
    assert not news._should_fetch_detail(replace(item, url=f"https://user:pass@{host}/article"))


def test_unpermitted_decoded_url_is_retained_without_fetch(monkeypatch):
    source = news.get_news_source("google_news_oil_rss")
    wrapper = "https://news.google.com/rss/articles/example?oc=5"
    original = "https://unreviewed.example/oil"
    item = news.RawNewsItem(source.source_id, "C", wrapper, "Oil supply")
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        return "<html>public wrapper</html>", "text/html"

    async def decode(*args, **kwargs):
        return original

    monkeypatch.setattr(news, "_fetch_text", fetch)
    monkeypatch.setattr(news, "_decode_google_news_url", decode)
    monkeypatch.setattr(news, "_public_original_url", lambda *args, **kwargs: "")
    result, _ = asyncio.run(news._enrich_items_with_details([item], source=source))
    assert calls == [wrapper]
    assert result[0].url == original
    assert result[0].discovery_url == wrapper
    assert result[0].detail_reason == "source_not_enabled"


@pytest.mark.parametrize("status", [302, 403, 429])
def test_provider_challenge_stops_remaining_batch_requests(monkeypatch, status):
    source = news.get_news_source("google_news_oil_rss")
    calls = []
    html = '<div data-n-a-id="id" data-n-a-ts="123" data-n-a-sg="sig"></div>'

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            calls.append(url)
            return httpx.Response(status, headers={"location": "https://www.google.com/sorry/index"},
                                  request=httpx.Request("POST", url))

    async def fetch(url, **kwargs):
        calls.append(url)
        return html, "text/html"

    monkeypatch.setattr(news.httpx, "AsyncClient", Client)
    monkeypatch.setattr(news, "_fetch_text", fetch)
    monkeypatch.setattr(news, "_public_original_url", lambda *args, **kwargs: "")
    monkeypatch.setattr(news, "monotonic", lambda: 100.0)
    items = [news.RawNewsItem(source.source_id, "C", f"https://news.google.com/rss/articles/{i}", "Oil")
             for i in range(3)]
    result, _ = asyncio.run(news._enrich_items_with_details(items, source=source))
    assert len(calls) == 2  # one wrapper and one POST; no CAPTCHA or other articles
    assert {x.detail_reason for x in result} == {"discovery_rate_limited"}
    assert news._google_news_resolution_retry_at == 1900.0


def test_manifest_bound_repair_preserves_low_relevance_body(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "settings", replace(settings, sqlite_path=str(tmp_path / "db.sqlite")))
    item = news.RawNewsItem("uk_fcdo_news", "A", "https://www.gov.uk/example", "Public statement",
                           raw_text="A public statement with no oil keyword. " * 20, body_method="semantic_body_hint")
    aid = news._id("art", item.url)
    storage.upsert_news_article(article_id=aid, payload={"source_id": item.source_id, "tier": "A", "url": item.url,
                              "title": item.title, "content_hash": "old", "raw_text": "Old prefix",
                              "first_seen_at": "2026-01-01T00:00:00+00:00"})
    original = news.analyze_news_item

    def low_score(*args, **kwargs):
        return {**original(*args, **kwargs), "score": 0}

    monkeypatch.setattr(news, "analyze_news_item", low_score)
    monkeypatch.setattr(news, "enqueue_event_ai_summary", lambda *args, **kwargs: None)
    assert news.ingest_news_items([item])["articles_found"] == 0
    assert news.ingest_news_items([item], expected_content_hashes={aid: "old"})["articles_found"] == 1
    with closing(storage.connect()) as con:
        row = con.execute("SELECT raw_text,first_seen_at FROM news_articles WHERE article_id=?", (aid,)).fetchone()
        assert row["raw_text"] == item.raw_text.strip()
        assert row["first_seen_at"] == "2026-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="not_in_manifest"):
        news.ingest_news_items([replace(item, url=item.url + "-other")], expected_content_hashes={aid: "old"})


def test_discovery_cooldown_expires_without_clearing_attempts(monkeypatch):
    monkeypatch.setattr(news, "_google_news_resolution_retry_at", 100.0)
    monkeypatch.setattr(news, "monotonic", lambda: 99.0)
    calls = []

    async def decode(*args, **kwargs):
        calls.append(1)
        return "https://www.csis.org/analysis/example"

    monkeypatch.setattr(news, "_decode_google_news_url_once", decode)
    with pytest.raises(news.GoogleNewsResolutionDeferred):
        asyncio.run(news._decode_google_news_url("https://news.google.com/rss/articles/id", wrapper_html="metadata"))
    assert not calls
    monkeypatch.setattr(news, "monotonic", lambda: 101.0)
    assert asyncio.run(news._decode_google_news_url("https://news.google.com/rss/articles/id", wrapper_html="metadata"))
    assert calls == [1]

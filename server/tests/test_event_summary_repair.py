from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app import news
from app.workbench_events import _article_event_view


def test_article_container_survives_nested_divs_and_void_elements() -> None:
    html = """<main><div class="article-content"><div>First supported fact.</div>
    <div><span>Second supported fact.</span><img src="cover.png"><br/> Still in article.</div>
    <p>Last supported fact.</p><aside>RELATED STORY MUST NOT LEAK</aside></div>
    <div>OUTSIDE ARTICLE MUST NOT LEAK</div></main>"""
    result = news._extract_article_detail(html)["text"]
    assert result == "First supported fact. Second supported fact. Still in article. Last supported fact."


def test_semantic_article_survives_nested_article_close() -> None:
    result = news._extract_article_detail(
        "<article>Opening.<article>Embedded announcement.</article>Closing.</article><footer>Footer.</footer>"
    )["text"]
    assert result == "Opening. Embedded announcement. Closing."


def test_google_rpc_resolution_updates_original_pending_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    source = news.get_news_source("google_news_oil_rss")
    assert source is not None
    wrapper = "https://news.google.com/rss/articles/test-id"
    original = "https://www.opec.org/pr-detail/announcement.html"
    body = "The participating countries confirmed the crude oil production policy and monthly review. " * 12
    fetched: list[str] = []

    async def fetch(url: str, *, referer: str | None = None) -> tuple[str, str]:
        fetched.append(url)
        if url == wrapper:
            return '<div data-n-a-id="test-id" data-n-a-ts="123" data-n-a-sg="public-metadata"></div>', "text/html"
        assert url == original
        return f"<article><div>{body[:100]}</div><p>{body[100:]}</p></article>", "text/html"

    async def decode(url: str, *, wrapper_html: str | None = None) -> str:
        assert url == wrapper and wrapper_html and "data-n-a-id" in wrapper_html
        return original

    stored: list[dict] = []
    queued: list[str] = []
    monkeypatch.setattr(news, "_fetch_text", fetch)
    monkeypatch.setattr(news, "_decode_google_news_url", decode)
    monkeypatch.setattr(news, "upsert_news_article", lambda **row: stored.append(row))
    monkeypatch.setattr(news, "upsert_news_event_cluster", lambda **_: None)
    monkeypatch.setattr(news, "enqueue_event_ai_summary", lambda article_id, *_: queued.append(article_id))
    monkeypatch.setattr(news, "_deepseek_client", lambda: type("Client", (), {"model": "isolated"})())
    item = news.RawNewsItem(
        source.source_id, "C", wrapper, "OPEC crude production policy", "2026-09-06", raw_text="RSS excerpt"
    )
    enriched, errors = asyncio.run(news._enrich_items_with_details([item], source=source))
    news.ingest_news_items(enriched, source=source)
    assert errors == [] and fetched == [wrapper, original]
    assert stored[0]["article_id"] == news._id("art", wrapper)
    assert stored[0]["payload"]["url"] == original
    assert stored[0]["payload"]["raw"]["discovery_url"] == wrapper
    assert stored[0]["payload"]["raw"]["source_content"]["status"] == "full_text"
    assert queued == [stored[0]["article_id"]]


def test_decoded_unregistered_destination_is_not_fetched(monkeypatch: pytest.MonkeyPatch) -> None:
    source = news.get_news_source("google_news_oil_rss")
    assert source is not None
    wrapper = "https://news.google.com/rss/articles/id"
    calls: list[str] = []

    async def fetch(url: str, *, referer: str | None = None) -> tuple[str, str]:
        calls.append(url)
        assert url == wrapper
        return "<html>public wrapper</html>", "text/html"

    async def decode(*_: object, **__: object) -> str:
        return "http://127.0.0.1/private"

    monkeypatch.setattr(news, "_fetch_text", fetch)
    monkeypatch.setattr(news, "_decode_google_news_url", decode)
    item = news.RawNewsItem(source.source_id, "C", wrapper, "Oil policy", raw_text="Short public snippet.")
    enriched, _ = asyncio.run(news._enrich_items_with_details([item], source=source))
    assert calls == [wrapper]
    assert enriched[0].url == wrapper
    assert enriched[0].detail_reason == "source_not_enabled"
    assert not news.classify_summary_input(enriched[0], source=source)["eligible_for_summary"]


@pytest.mark.parametrize(
    "destination,valid",
    [
        ("https://www.opec.org/pr-detail/announcement.html", True),
        ("javascript:alert(1)", False),
        ("https://user:password@example.com/story", False),
    ],
)
def test_google_wrapper_rpc_resolves_without_fetching_target(
    monkeypatch: pytest.MonkeyPatch, destination: str, valid: bool
) -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST" and str(request.url) == news.GOOGLE_NEWS_DECODE_URL
        assert b"f.req=" in request.content
        encoded = json.dumps(["garturlres", destination, 1, 2])
        return httpx.Response(200, text=json.dumps([["wrb.fr", "Fbv4je", encoded]]))

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        news.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs)
    )
    call = news._decode_google_news_url(
        "https://news.google.com/rss/articles/test-id",
        wrapper_html='<div data-n-a-id="test-id" data-n-a-ts="123" data-n-a-sg="public-metadata"></div>',
    )
    if valid:
        assert asyncio.run(call) == destination
    else:
        with pytest.raises(ValueError, match="invalid public URL"):
            asyncio.run(call)
    assert len(requests) == 1


def test_opec_discovery_keeps_official_source_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    async def decode(_: str) -> str:
        return "https://example.com/unofficial-opec-analysis"

    monkeypatch.setattr(news, "_decode_google_news_url", decode)
    with pytest.raises(ValueError, match="not an official OPEC"):
        asyncio.run(news._decode_opec_google_news_url("https://news.google.com/rss/articles/test-id"))


def test_access_denial_stays_explicit_and_never_becomes_model_input(monkeypatch: pytest.MonkeyPatch) -> None:
    source = news.get_news_source("opec_press")
    assert source is not None
    url = "https://www.opec.org/pr-detail/protected.html"

    async def fetch(_: str, *, referer: str | None = None) -> tuple[str, str]:
        response = httpx.Response(403, request=httpx.Request("GET", url))
        response.raise_for_status()
        raise AssertionError("unreachable")

    monkeypatch.setattr(news, "_fetch_text", fetch)
    item = news.RawNewsItem(source.source_id, "A", url, "Oil policy", raw_text="A short RSS description of the policy.")
    enriched, errors = asyncio.run(news._enrich_items_with_details([item], source=source))
    quality = news.classify_summary_input(enriched[0], source=source)
    assert errors and quality["reason"] == "access_restricted"
    assert quality["eligible_for_summary"] is False
    event = _article_event_view(
        {
            "title": item.title,
            "raw": json.dumps({"source_content": {"status": quality["level"], "reason": quality["reason"]}}),
        }
    )
    assert event["source_content_status_label"] == "原站访问受限"
    assert event["analysis_available"] is False
    assert event["summary_generation_status"] == "awaiting_source"


@pytest.mark.parametrize(
    "reason,label",
    [
        ("discovery_snippet", "原文链接待解析"),
        ("original_url_unresolved", "原文链接待解析"),
        ("source_not_enabled", "原站未在自动采集范围内"),
        ("discovery_rate_limited", "新闻索引暂时限流，原文待恢复"),
        ("source_timeout", "原站读取超时"),
        ("source_fetch_failed", "原站正文读取失败"),
    ],
)
def test_customer_sees_specific_source_failure_without_false_ready(reason: str, label: str) -> None:
    event = _article_event_view(
        {"title": "Oil policy", "raw": json.dumps({"source_content": {"status": "partial_text", "reason": reason}})}
    )
    assert event["source_content_status_label"] == label
    assert event["summary_generation_status"] == "awaiting_source"
    assert not event["analysis_available"]


def test_detail_failure_metadata_does_not_lower_valid_full_text() -> None:
    item = news.RawNewsItem(
        "opec_press",
        "A",
        "https://www.opec.org/pr-detail/test.html",
        "Oil policy",
        raw_text="Already collected verifiable full article body. " * 25,
    )
    assert news.classify_summary_input(replace(item, detail_reason="source_timeout"))["level"] == "full_text"

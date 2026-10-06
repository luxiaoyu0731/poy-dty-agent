from __future__ import annotations

import asyncio
from datetime import datetime
from urllib.parse import urlsplit

import pytest

from app import news as news_module
from app.models import NewsSource
from app.news import RawNewsItem


def _source(
    source_id: str = "opec_press",
    *,
    tier: str = "A",
    url: str = "https://www.opec.org/press-releases.html",
) -> NewsSource:
    return NewsSource(
        source_id=source_id,
        source_name=source_id,
        tier=tier,
        url=url,
        category="oil_policy",
        fetcher="html",
        cadence="test",
    )


def _body(sentences: int = 10) -> str:
    sentence = (
        "OPEC ministers confirmed the production policy after reviewing crude supply, "
        "inventories, demand forecasts, and implementation data for member countries."
    )
    return " ".join(sentence for _ in range(sentences))


def test_classifies_full_partial_and_title_only_with_auditable_provenance() -> None:
    full = news_module.classify_summary_input(
        RawNewsItem("opec_press", "A", "https://www.opec.org/news/statement", "OPEC confirms policy", raw_text=_body()),
        source=_source(),
    )
    partial = news_module.classify_summary_input(
        RawNewsItem(
            "opec_press",
            "A",
            "https://www.opec.org/news/statement",
            "OPEC confirms policy",
            raw_text="OPEC confirmed the policy in a short RSS description and said details would follow.",
        ),
        source=_source(),
    )
    title_only = news_module.classify_summary_input(
        RawNewsItem(
            "opec_press",
            "A",
            "https://www.opec.org/news/statement",
            "OPEC confirms policy",
            raw_text="OPEC confirms policy",
        ),
        source=_source(),
    )

    assert full == {
        "level": "full_text",
        "reason": "sufficient_article_body",
        "eligible_for_summary": True,
        "summary_blocked_reason": "",
        "source_id": "opec_press",
        "source_url": "https://www.opec.org/news/statement",
        "discovery_source": False,
        "text_chars": len(_body()),
        "body_policy": news_module.BODY_POLICY,
        "body_method": "",
    }
    assert partial["level"] == "partial_text"
    assert partial["reason"] == "insufficient_article_body"
    assert partial["eligible_for_summary"] is False
    assert title_only["level"] == "title_only"
    assert title_only["reason"] == "title_only"
    assert title_only["eligible_for_summary"] is False


@pytest.mark.parametrize(
    "barrier",
    [
        "Subscribe to continue reading this article.",
        "Sign in to read the full story.",
        "Please complete the CAPTCHA to continue.",
        "登录后阅读全文。",
    ],
)
def test_access_barriers_fail_closed_even_when_the_page_is_long(barrier: str) -> None:
    quality = news_module.classify_summary_input(
        RawNewsItem(
            "publisher",
            "B",
            "https://publisher.example/story",
            "Oil market update",
            raw_text=f"{barrier} {_body()}",
        ),
        source=_source("publisher", tier="B", url="https://publisher.example/"),
    )

    assert quality["level"] == "title_only"
    assert quality["reason"] == "access_restricted"
    assert quality["eligible_for_summary"] is False


def test_long_rss_wrapper_url_does_not_masquerade_as_full_article_text() -> None:
    wrapped = (
        '<a href="https://news.example/articles/'
        + ("opaque-token-" * 80)
        + '">Oil inventories increased</a><font>Example Publisher</font>'
    )

    quality = news_module.classify_summary_input(
        RawNewsItem(
            "publisher_feed",
            "B",
            "https://publisher.example/story",
            "Oil inventories increased",
            raw_text=wrapped,
        ),
        source=_source("publisher_feed", tier="B", url="https://publisher.example/feed"),
    )

    assert quality["level"] != "full_text"
    assert quality["eligible_for_summary"] is False
    assert quality["text_chars"] < 100


def test_article_detail_prefers_semantic_body_over_long_navigation_chrome() -> None:
    navigation = " ".join(f"Navigation item {index}" for index in range(200))
    body = " ".join("CENTCOM reported strikes on coastal defence systems and maritime capabilities." for _ in range(12))

    detail = news_module._extract_article_detail(
        f"<html><head><title>CENTCOM statement</title></head><body>"
        f"<nav>{navigation}</nav><div class='news-body'>{body}</div>"
        f"<footer>{navigation}</footer></body></html>"
    )

    assert "Navigation item" not in detail["text"]
    assert detail["text"] == body
    assert len(detail["text"]) >= news_module.FULL_TEXT_MIN_CHARS


def test_short_semantic_body_remains_below_full_text_gate() -> None:
    body = "CENTCOM reported a maritime incident near the Strait of Hormuz."
    detail = news_module._extract_article_detail(
        f"<html><title>CENTCOM notice</title><div class='news-body'>{body}</div></html>"
    )
    quality = news_module.classify_summary_input(
        RawNewsItem(
            "us_centcom_press",
            "B",
            "https://www.dvidshub.net/news/notice",
            "CENTCOM notice",
            raw_text=detail["text"],
        ),
        source=_source(
            "us_centcom_press",
            tier="B",
            url="https://www.centcom.mil/MEDIA/PRESS-RELEASES/",
        ),
    )

    assert detail["text"] == body
    assert quality["level"] == "partial_text"
    assert quality["eligible_for_summary"] is False


def test_detail_enrichment_compares_clean_text_not_rss_markup_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(
        "us_centcom_press",
        tier="B",
        url="https://www.centcom.mil/MEDIA/PRESS-RELEASES/",
    )
    rss_markup = (
        '<a href="https://www.dvidshub.net/news/' + ("opaque-token-" * 80) + '">CENTCOM reports maritime strike</a>'
    )
    article_body = " ".join(
        "CENTCOM said the operation targeted maritime capabilities near the Strait of Hormuz." for _ in range(12)
    )
    item = RawNewsItem(
        "us_centcom_press",
        "B",
        "https://www.dvidshub.net/news/statement",
        "CENTCOM reports maritime strike",
        raw_text=rss_markup,
    )

    async def fetch_detail(_: str, *, referer: str | None = None) -> tuple[str, str]:
        return f"<html><div class='news-body'>{article_body}</div></html>", "text/html"

    monkeypatch.setattr(news_module, "_fetch_text", fetch_detail)
    enriched, errors = asyncio.run(news_module._enrich_items_with_details([item], source=source))

    assert errors == []
    assert enriched[0].raw_text == article_body
    assert news_module.classify_summary_input(enriched[0], source=source)["level"] == "full_text"


def test_google_news_is_discovery_only_and_only_public_original_urls_can_be_fetched() -> None:
    discovery = _source(
        "google_news_oil_rss",
        tier="C",
        url="https://news.google.com/rss/search?q=oil",
    )

    assert (
        news_module._should_fetch_detail(
            RawNewsItem("google_news_oil_rss", "C", "https://news.google.com/rss/articles/abc", "Headline"),
            source=discovery,
        )
        is False
    )
    assert (
        news_module._should_fetch_detail(
            RawNewsItem("google_news_oil_rss", "C", "https://www.opec.org/news/statement", "Headline"),
            source=discovery,
        )
        is True
    )
    assert (
        news_module._should_fetch_detail(
            RawNewsItem("google_news_oil_rss", "C", "https://www.ccf.com.cn/news/paywalled", "Headline"),
            source=discovery,
        )
        is False
    )
    wrapped_quality = news_module.classify_summary_input(
        RawNewsItem(
            "google_news_oil_rss",
            "C",
            "https://news.google.com/rss/articles/abc",
            "Headline",
            raw_text=_body(),
        ),
        source=discovery,
    )
    assert wrapped_quality["level"] == "partial_text"
    assert wrapped_quality["reason"] == "discovery_snippet"
    assert wrapped_quality["eligible_for_summary"] is False


def test_discovery_wrapper_resolves_public_original_then_full_text_is_enqueued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    discovery = _source(
        "google_news_oil_rss",
        tier="C",
        url="https://news.google.com/rss/search?q=oil",
    )
    item = RawNewsItem(
        discovery.source_id,
        discovery.tier,
        "https://news.google.com/rss/articles/abc",
        "OPEC confirms crude oil production policy",
        "2026-07-24",
        raw_text="Short discovery snippet.",
    )
    body = _body()

    async def fetch(url: str, *, referer: str | None = None) -> tuple[str, str]:
        if urlsplit(url).hostname == "news.google.com":
            return '<html><a href="https://www.opec.org/news/statement">Read full story</a></html>', "text/html"
        return f"<html><article>{body}</article></html>", "text/html"

    queued: list[str] = []
    monkeypatch.setattr(news_module, "_fetch_text", fetch)
    monkeypatch.setattr(news_module, "upsert_news_article", lambda **_: None)
    monkeypatch.setattr(news_module, "upsert_news_event_cluster", lambda **_: None)
    monkeypatch.setattr(news_module, "upsert_event_observation", lambda **_: ({}, False))
    monkeypatch.setattr(news_module, "enqueue_event_ai_summary", lambda article_id, *_: queued.append(article_id))
    monkeypatch.setattr(news_module, "_deepseek_client", lambda: type("Client", (), {"model": "test"})())

    enriched, errors = asyncio.run(news_module._enrich_items_with_details([item], source=discovery))
    result = news_module.ingest_news_items(enriched, source=discovery)

    assert errors == []
    assert enriched[0].url == "https://www.opec.org/news/statement"
    assert news_module.classify_summary_input(enriched[0], source=discovery)["level"] == "full_text"
    assert result["articles_found"] == 1
    assert queued


def test_detail_extraction_failure_keeps_partial_input_ineligible(monkeypatch: pytest.MonkeyPatch) -> None:
    source = _source()
    item = RawNewsItem(
        "opec_press",
        "A",
        "https://www.opec.org/news/statement",
        "OPEC confirms policy",
        raw_text="OPEC confirmed its policy in a short RSS description.",
    )

    async def fail_fetch(_: str, *, referer: str | None = None) -> tuple[str, str]:
        raise OSError("article unavailable")

    monkeypatch.setattr(news_module, "_fetch_text", fail_fetch)
    enriched, errors = asyncio.run(news_module._enrich_items_with_details([item], source=source))

    assert enriched[0].raw_text == item.raw_text
    assert enriched[0].detail_reason == "source_fetch_failed"
    assert errors and errors[0].startswith("detail:OSError")
    assert news_module.classify_summary_input(enriched[0], source=source)["level"] == "partial_text"


def test_only_full_text_is_enqueued_for_formal_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    stored: list[dict[str, object]] = []
    queued: list[str] = []
    monkeypatch.setattr(news_module, "upsert_news_article", lambda **kwargs: stored.append(kwargs["payload"]))
    monkeypatch.setattr(news_module, "upsert_news_event_cluster", lambda **_: None)
    monkeypatch.setattr(news_module, "upsert_event_observation", lambda **_: ({}, False))
    monkeypatch.setattr(news_module, "enqueue_event_ai_summary", lambda article_id, *_: queued.append(article_id))
    monkeypatch.setattr(news_module, "_deepseek_client", lambda: type("Client", (), {"model": "test"})())
    source = _source()
    items = [
        RawNewsItem(
            "opec_press",
            "A",
            "https://www.opec.org/news/full",
            "OPEC confirms oil production policy",
            "2026-07-22",
            raw_text=_body(),
        ),
        RawNewsItem(
            "opec_press",
            "A",
            "https://www.opec.org/news/partial",
            "OPEC confirms crude oil policy",
            "2026-07-22",
            raw_text="OPEC confirmed a crude oil policy update in a short RSS description.",
        ),
    ]

    result = news_module.ingest_news_items(items, source=source)

    assert result["articles_found"] == 2
    assert len(stored) == 2
    assert stored[0]["raw"]["summary_input_quality"]["level"] == "full_text"
    content = stored[0]['raw']['source_content']
    assert content['stored_text_sha256'] == news_module._hash(stored[0]['raw_text'])
    assert content['stored_text_content_hash'] == stored[0]['content_hash']
    assert content['stored_text_truncated'] is False
    assert datetime.fromisoformat(content['stored_text_verified_at']).tzinfo is not None
    assert stored[1]["raw"]["summary_input_quality"]["level"] == "partial_text"
    assert queued == [news_module._id("art", "https://www.opec.org/news/full")]


def test_a_tier_article_is_not_promoted_before_grounded_impact_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clusters: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    monkeypatch.setattr(news_module, "upsert_news_article", lambda **_: None)
    monkeypatch.setattr(
        news_module,
        "upsert_news_event_cluster",
        lambda **kwargs: clusters.append(kwargs["payload"]),
    )
    monkeypatch.setattr(
        news_module,
        "upsert_event_observation",
        lambda **kwargs: observations.append(kwargs) or ({}, True),
    )
    monkeypatch.setattr(news_module, "enqueue_event_ai_summary", lambda *_: None)
    monkeypatch.setattr(news_module, "_deepseek_client", lambda: type("Client", (), {"model": "test"})())
    item = RawNewsItem(
        "opec_press",
        "A",
        "https://www.opec.org/news/full",
        "OPEC announces crude oil production cut",
        "2026-07-22",
        raw_text=_body(),
    )

    result = news_module.ingest_news_items([item], source=_source())

    assert result["events_created"] == 0
    assert observations == []
    assert clusters[0]["status"] == "candidate"
    assert clusters[0]["direction"] == "中性"
    assert clusters[0]["affected_products"] == ["crude_oil"]
    assert clusters[0]["raw"]["promotion_blocked_reason"] == "awaiting_grounded_summary_and_impact_gate"


def test_discovery_refresh_cannot_overwrite_verified_publication_date() -> None:
    from app.storage import upsert_news_article

    payload = {
        "source_id": "gdelt_oil_geopolitics_rss",
        "tier": "B",
        "url": "https://example.com/verified-time",
        "title": "Oil supply update",
        "published_at": "2026-09-10",
        "content_hash": "verified-time",
        "raw": {
            "timestamp_correction": {"verified_published_at": "2026-09-10"},
            "discovery_timestamp": "12 Sep 2026 14:30:00 +0000",
        },
    }
    upsert_news_article(article_id="test_verified_publication_guard", payload=payload)
    for replacement in ["", "13 Sep 2026 14:30:00 +0000"]:
        result, _ = upsert_news_article(
            article_id="test_verified_publication_guard", payload={**payload, "published_at": replacement, "raw": {}}
        )
        assert result["published_at"] == "2026-09-10"
        assert result["raw"]["timestamp_correction"]["verified_published_at"] == "2026-09-10"

import asyncio
import contextlib
import json
import sqlite3

import pytest

from app import news, storage


def test_rss_relative_links_and_atom_summary_are_not_capture_timestamps():
    source = news.get_news_source("eia_press")
    rows = news._parse_feed('''<rss><channel><item><title>Crude oil production</title>
      <link>/pressroom/releases/press590.php</link><pubDate>Tue, 7 Jul 2026 12:00:00 EST</pubDate>
      <description>Crude oil supply increases.</description></item></channel></rss>''', source)
    assert rows[0].url == "https://www.eia.gov/pressroom/releases/press590.php"
    assert rows[0].first_seen_at == ""
    atom = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Crude oil production</title>
      <link href="/pressroom/releases/press590.php"/><updated>2026-07-07</updated>
      <summary>Crude oil supply increases.</summary></entry></feed>'''
    row = news._parse_feed(atom, source)[0]
    assert row.raw_text == "Crude oil supply increases."
    assert row.first_seen_at == ""


@pytest.mark.parametrize("feed_fails", [False, True])
def test_eia_uses_official_feed_and_falls_back_to_dated_html(monkeypatch, feed_fails):
    source = news.get_news_source("eia_today_in_energy")
    calls = []

    async def fetch(url, *, referer: str | None = None):
        calls.append(url)
        if url.endswith(".xml"):
            if feed_fails:
                raise TimeoutError("feed unavailable")
            return ('''<rss><channel><item><title>Crude oil production</title>
              <link>https://www.eia.gov/todayinenergy/detail.php?id=12</link>
              <pubDate>2026-09-09</pubDate></item></channel></rss>''', "application/xml")
        return ('''<div class="tie-article"><span class="date">Sep 9, 2026</span>
          <h1><a href="detail.php?id=12">Crude oil production</a></h1></div>''', "text/html")

    monkeypatch.setattr(news, "_fetch_text", fetch)
    monkeypatch.setattr(news, "ingest_news_items", lambda rows, **kwargs: {"articles_found": len(rows)})
    result, errors = asyncio.run(news._fetch_news_source_result(
        source, limit=20, mode="live", start_date=None, end_date=None,
        cursor_pages=1, include_details=False,
    ))
    assert result["articles_found"] == 1
    assert calls[0] == news.EIA_NEWS_FEEDS[source.source_id]
    assert len(calls) == 2
    assert bool(errors) is feed_fails


def test_eia_feed_rejects_missing_article_ids():
    source = news.get_news_source("eia_today_in_energy")
    feed = '''<rss><channel>
      <item><title>Crude oil imports</title><link>https://www.eia.gov/todayinenergy/detail.php?id=</link></item>
      <item><title>Crude oil exports</title><link>https://www.eia.gov/todayinenergy/detail.php?id=12</link></item>
      </channel></rss>'''
    rows = news._parse_feed(feed, source)
    assert len(rows) == 1
    assert rows[0].url.endswith("id=12")


def test_industry_source_only_admits_articles_and_keeps_dates_honest():
    source = news.get_news_source("ccfa_industry_news")
    rows = news._parse_html('''<a href="/19/index.html">化纤行业运行</a>
      <a href="/19/202605/4935.html">化纤行业运行简析</a>
      <a href="/14/202609/5121.html">聚酯产业链交流</a>
      <a href="https://example.com/14/202609/5122.html">聚酯产量</a>''', source)
    assert len(rows) == 2
    assert rows[0].url.endswith("/5121.html")
    assert rows[0].published_at == ""
    detail = news._extract_article_detail('''<title>聚酯产业链交流</title>
      <p>发布时间：2026/09/04 16:15</p><article>9月1日举行会议。</article>''')
    assert detail["published_at"] == "2026-09-04"


def test_svg_accessibility_labels_do_not_pollute_article_title():
    detail = news._extract_article_detail('''<title>Crude oil outlook</title>
      <nav><svg><title>Search</title></svg><svg><title>Chevron down</title></svg></nav>
      <article>Crude oil production increases.</article>''')
    assert detail["title"] == "Crude oil outlook"


@pytest.fixture
def article_db(tmp_path, monkeypatch):
    path = tmp_path / "news.db"

    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        return connection

    with contextlib.closing(connect()) as connection:
        connection.execute('''CREATE TABLE news_articles (
          article_id TEXT PRIMARY KEY, created_at TEXT, source_id TEXT, tier TEXT, url TEXT,
          canonical_url TEXT, title TEXT, published_at TEXT, first_seen_at TEXT, content_hash TEXT,
          language TEXT, raw_text TEXT, summary TEXT, score REAL, category TEXT, raw TEXT)''')
        connection.execute('''CREATE TABLE event_ai_summaries (
          article_id TEXT PRIMARY KEY, summary_status TEXT, attempts INTEGER, updated_at TEXT, source_hash TEXT)''')
    monkeypatch.setattr(storage, "connect", connect)
    return connect


def payload(source="eia_today_in_energy", first_seen=""):
    return dict(source_id=source, tier="A", url="https://www.eia.gov/todayinenergy/detail.php?id=1",
                title="Crude oil", published_at="2026-09-09", content_hash="hash", raw={},
                first_seen_at=first_seen)


def test_capture_time_repair_uses_existing_creation_time_and_is_idempotent(article_db):
    row, _ = storage.upsert_news_article(article_id="one", payload=payload(first_seen="article title"))
    assert row["first_seen_at"] == row["created_at"]
    with article_db() as connection:
        connection.execute("UPDATE news_articles SET first_seen_at='legacy title' WHERE article_id='one'")
    repaired, inserted = storage.upsert_news_article(article_id="one", payload=payload())
    assert not inserted
    assert repaired["first_seen_at"] == row["created_at"]
    assert repaired["raw"]["capture_time_repair"]["original_value"] == "legacy title"
    again, _ = storage.upsert_news_article(article_id="one", payload=payload())
    assert again["raw"]["capture_time_repair"] == repaired["raw"]["capture_time_repair"]
    with article_db() as connection:
        stored = connection.execute("SELECT * FROM news_articles WHERE article_id='one'").fetchone()
    assert stored["first_seen_at"] == row["created_at"]
    assert stored["published_at"] == "2026-09-09"
    assert json.loads(stored["raw"])["capture_time_repair"]["basis"] == "existing_record_created_at"


def test_source_queue_isolation_and_valid_capture_preservation(article_db):
    for article_id, source in [("one", "eia_today_in_energy"), ("two", "other")]:
        storage.upsert_news_article(article_id=article_id, payload=payload(source, "2026-09-01T12:00:00Z"))
        with article_db() as connection:
            connection.execute("INSERT INTO event_ai_summaries VALUES (?, 'pending', 0, '2026-09-10', 'hash')",
                               (article_id,))
    row, _ = storage.upsert_news_article(article_id="one", payload=payload())
    assert row["first_seen_at"] == "2026-09-01T12:00:00Z"
    rows = storage.list_retryable_event_ai_summaries(10, 3, source_ids=["eia_today_in_energy"])
    assert [row["article_id"] for row in rows] == ["one"]
    assert storage.list_retryable_event_ai_summaries(10, 3, source_ids=[]) == []

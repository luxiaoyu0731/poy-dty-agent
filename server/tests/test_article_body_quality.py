from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from app import news
from app.article_body_quality import body_defect, sunsirs_body, titles_conflict

TITLE = "SunSirs: Polyester filament market review"
URL = "https://www.sunsirs.com/uk/detail_news-12345.html"
CN_TITLE = "生意社WTI原油9月25日均线下穿 均差为-0.09美元/桶"
CN_URL = "https://www.100ppi.com/news/detail-20260925-6272956.html"
CN_BODY = ("生意社09月25日讯 据生意社监测，WTI原油9月25日均差为-0.09美元/桶。"
           "当日均线下穿，均差由正转负，表明行情出现了下跌或调整信号。"
           "该指标是历史价格计算结果，不能单独说明未来走势，也不能据此确定采购价格。")


def cn_page():
    return (f"<html><title>{CN_TITLE} - 商品动态 - 生意社</title><body>"
            "<h1>大宗商品涨跌榜-生意社</h1><div>本社首页 &gt; 商品动态 &gt; 正文 "
            f"{CN_TITLE} https://www.100ppi.com 2026年09月25日 18:11 生意社 "
            f"{CN_BODY} (文章来源：生意社) 相关商品 另一篇报价为999元。"
            "</div></body></html>")


def test_chinese_publisher_binds_real_article_not_navigation_heading():
    detail = news._extract_article_detail(cn_page(), source_url=CN_URL)
    assert detail["title"] == detail["headline"] == CN_TITLE
    assert detail["text"] == CN_BODY
    assert detail["published_at"] == "2026-09-25"
    assert detail["body_method"] == "publisher_100ppi"
    assert detail["body_reason"] == ""
    candidate = news.RawNewsItem("ppi_commodity_news", "B", CN_URL, CN_TITLE,
                                 raw_text=detail["text"], body_method=detail["body_method"])
    assert news.classify_summary_input(candidate)["eligible_for_summary"]


def test_chinese_stored_page_can_recover_verified_boundaries_without_refetch():
    from app.article_body_quality import ppi_chinese_article
    parser = news._LinkParser()
    parser.feed(cn_page())
    text = news.clean_event_source_text(parser.visible_text)
    extracted = ppi_chinese_article(text, url=CN_URL, title="大宗商品涨跌榜-生意社")
    assert extracted and extracted["body"] == CN_BODY
    old = news.RawNewsItem("ppi_commodity_news", "B", CN_URL, "大宗商品涨跌榜-生意社",
                           raw_text=text, body_method="document_fallback",
                           body_reason="article_body_not_located")
    prepared = news.prepare_article_body(old)
    assert prepared.title == CN_TITLE and prepared.raw_text == CN_BODY
    assert prepared.body_reason == ""
    assert news.classify_summary_input(prepared)["eligible_for_summary"]
    assert news.prepare_article_body(replace(old, body_truncated=True)).raw_text == text
    assert news.prepare_article_body(replace(old, body_reason="stored_body_hash_mismatch")).raw_text == text


def test_chinese_reprint_binds_matching_source_and_removes_known_pricing_advert():
    html = cn_page().replace('18:11 生意社', '18:11 外媒网').replace('(文章来源：生意社)',
        '【大宗商品公式定价原理】 生意社基准价是基于价格大数据与模型产生的指导价。'
        '定价公式：结算价 = 生意社基准价×K＋C；物流成本、品牌价差、区域价差等因素。'
        '(文章来源：外媒网)')
    detail = news._extract_article_detail(html, source_url=CN_URL)
    assert detail['body_method'] == 'publisher_100ppi'
    assert detail['text'] == CN_BODY


def test_chinese_reprint_source_mismatch_does_not_establish_verified_body():
    detail = news._extract_article_detail(cn_page().replace('18:11 生意社', '18:11 外媒网'), source_url=CN_URL)
    assert detail['body_method'] != 'publisher_100ppi'
    assert detail['body_reason'] == 'article_body_not_located'


@pytest.mark.parametrize("change", ["wrong_title", "wrong_date", "missing_end", "different_host"])
def test_chinese_publisher_rejects_unbound_or_incomplete_content(change):
    from app.article_body_quality import ppi_chinese_article
    parser = news._LinkParser()
    parser.feed(cn_page())
    text = news.clean_event_source_text(parser.visible_text)
    title, url = CN_TITLE, CN_URL
    if change == "wrong_title":
        title = "另一篇PTA文章"
    elif change == "wrong_date":
        text = text.replace("2026年09月25日", "2026年09月24日")
    elif change == "missing_end":
        text = text.split("(文章来源：")[0]
    elif change == "different_host":
        url = url.replace("www.100ppi.com", "example.com")
    assert ppi_chinese_article(text, url=url, title=title) is None


BODY = " ".join(
    "Polyester producers reported lower operating rates; MEG stocks increased while PTA supply was stable."
    for _ in range(10)
)


def page(body=BODY, title=TITLE):
    return (f"<html><title>{title}</title><body><nav>Commodity News Sign In Privacy Policy</nav>"
            f"<div class='news_detail'><h1>{title}</h1><span>September 20 2026 10:00:00 SunSirs (Editor)</span>"
            f"<p>{body}</p><p>If you have any inquiries or purchasing needs, contact us.</p></div>"
            "<div>Related Information Commodity Price Terms of Use All Rights Reserved</div></body></html>")


def item(**kwargs):
    return news.RawNewsItem("ppi_commodity_news", "B", URL, TITLE, raw_text=BODY, **kwargs)


def test_live_layout_extracts_body_and_excludes_related_stories_and_marketing():
    result = news._extract_article_detail(page(), source_url=URL)
    assert result["text"] == BODY
    assert result["body_method"] == "publisher_sunsirs"
    assert result["body_reason"] == ""
    assert not result["body_truncated"]
    assert result["published_at"] == "2026-09-20"


def test_flattened_body_needs_matching_headline_date_and_end_boundary():
    text = news._extract_article_detail(page())["text"]
    assert sunsirs_body(text, url=URL, title=TITLE)[0] == BODY
    assert sunsirs_body(text, url=URL, title="Polyester filament market review - Sunsirs")[0] == BODY
    assert sunsirs_body(text, url=URL, title="Other article") is None
    assert sunsirs_body(text, url=URL, title=TITLE.replace("Polyester", "Oil")) is None
    assert sunsirs_body(text[:text.index("If you have")], url=URL, title=TITLE) is None
    assert sunsirs_body(text, url="https://news.google.com/rss/articles/test", title=TITLE) is None
    assert sunsirs_body(text.replace("2026 10:00:00", "yesterday"), url=URL, title=TITLE) is None


def test_semantic_body_excludes_hidden_and_related_containers_and_keeps_inline_links():
    html = ("<article><h1>Oil production update</h1><div itemprop='articleBody'>"
            "<p>Crude oil <a href='/source'>production</a> increased.</p>"
            "<div class='related-articles'><div class='article-body'>" + BODY * 3 + "</div></div>"
            "<div hidden>Hidden oil decline</div><div style='display: none'>Invisible</div>"
            "<p>Demand was stable.</p></div></article>")
    detail = news._extract_article_detail(html)
    assert detail["text"] == "Crude oil production increased. Demand was stable."
    assert detail["headline"] == "Oil production update"


def test_unlocated_page_cannot_pass_just_because_it_is_long():
    detail = news._extract_article_detail("<html><body>" + BODY + "</body></html>")
    quality = news.classify_summary_input(replace(
        item(), raw_text=detail["text"], body_method=detail["body_method"], body_reason=detail["body_reason"],
    ))
    assert not quality["eligible_for_summary"]
    assert quality["reason"] == "article_body_not_located"


EIA_URL = "https://www.eia.gov/todayinenergy/detail.php?id=68024"
EIA_TITLE = "Eight petroleum liquids pipeline projects have been completed"


def eia_page():
    return ("<html><title>Energy Information Administration</title>"
            "<h1>Today in Energy</h1><div>Search News Navigation</div>"
            "<div class='tie-article' data-type='inbrief'><span class='date'>August 26, 2026</span>"
            f"<h1><a href='#'>{EIA_TITLE}</a></h1><p>{BODY}</p>"
            "<div class='source'>Data source: EIA</div><p>Final paragraph with original caveats.</p>"
            "<div class='do-not-print'>Sign In My Portfolio Stock Screener Market Screener</div></div>"
            "<div class='tie-archive-section'>Unrelated archive stories.</div></html>")


def test_eia_article_binds_own_headline_and_complete_container():
    detail = news._extract_article_detail(eia_page(), source_url=EIA_URL)
    assert detail['headline'] == EIA_TITLE
    assert detail['body_reason'] == ''
    assert detail['published_at'] == '2026-08-26'
    assert BODY in detail['text'] and 'Final paragraph with original caveats.' in detail['text']
    assert 'Today in Energy' not in detail['text']
    assert 'Stock Screener' not in detail['text'] and 'Unrelated archive' not in detail['text']


@pytest.mark.parametrize('url', [
    'https://example.com/todayinenergy/detail.php?id=68024',
    'https://www.eia.gov/todayinenergy/archive.php?id=68024',
    'https://www.eia.gov/todayinenergy/detail.php',
])
def test_eia_publisher_selector_is_scoped_to_actual_article_url(url):
    detail = news._extract_article_detail(eia_page(), source_url=url)
    assert detail['body_method'] == 'document_fallback'
    assert detail['body_reason']


def test_eia_wrong_article_still_rejected(monkeypatch):
    async def fetch(*args, **kwargs):
        return eia_page(), 'text/html'
    monkeypatch.setattr(news, '_fetch_text', fetch)
    incoming = news.RawNewsItem('eia_today_in_energy', 'A', EIA_URL, 'Natural gas prices decreased')
    result, _ = asyncio.run(news._enrich_items_with_details(
        [incoming], source=news.get_news_source('eia_today_in_energy')))
    assert result[0].body_reason == 'article_title_mismatch'


@pytest.mark.parametrize("text,reason", [
    (BODY + " kAm abc kAm def kAm xyz", "unreadable_article_body"),
    (BODY + " Sign In My Portfolio Stock Screener Market Screener", "navigation_contaminated_body"),
    ("x" * 5000, "legacy_body_boundary_unverified"),
    ("x" * 8000, "legacy_body_boundary_unverified"),
])
def test_known_defects_never_reach_model_by_length(text, reason):
    quality = news.classify_summary_input(replace(item(), raw_text=text))
    assert quality["reason"] == reason
    assert not quality["eligible_for_summary"]


def test_verified_exact_legacy_length_is_not_assumed_truncated():
    assert body_defect("x" * 5000, method="semantic_article") == ""


@pytest.mark.parametrize("auth,allowed", [
    ("public", True), ("public_personal_reuse", True), ("public_or_license", False),
    ("api_key", False), ("login", False), ("unknown", False),
])
def test_registered_public_reuse_is_not_misreported_as_access_restricted(monkeypatch, auth, allowed):
    from types import SimpleNamespace
    monkeypatch.setattr(news, "settings", SimpleNamespace(outbound_hosts={"www.sunsirs.com"}))
    monkeypatch.setattr(news, "_source_registry_auth_by_host", lambda: {"www.sunsirs.com": auth})
    assert news._should_fetch_detail(item()) is allowed
    assert not news._should_fetch_detail(replace(item(), url="https://unapproved.example/article"))


def test_long_body_survives_model_input_ceiling_without_truncation():
    body = BODY * 8
    assert 5000 < len(body) < 12_000
    assert news._extract_article_detail(f"<article>{body}</article>")["text"] == body
    detail = news._extract_article_detail(f"<article>{body * 3}</article>")
    assert detail["text"] == body * 3
    assert detail["body_reason"] == "" and not detail["body_truncated"]


def test_short_real_body_replaces_long_feed_chrome_and_clears_old_failure(monkeypatch):
    async def fetch(*args, **kwargs):
        return page(), "text/html"
    monkeypatch.setattr(news, "_fetch_text", fetch)
    monkeypatch.setattr(news, "_should_fetch_detail", lambda *a, **k: True)
    source = news.get_news_source("ppi_commodity_news")
    incoming = replace(item(), raw_text=BODY * 3, detail_reason="source_fetch_failed",
                       body_reason="navigation_contaminated_body")
    enriched, errors = asyncio.run(news._enrich_items_with_details([incoming], source=source))
    assert not errors
    assert enriched[0].raw_text == BODY
    assert not enriched[0].detail_reason and not enriched[0].body_reason
    assert news.classify_summary_input(enriched[0])["eligible_for_summary"]


def test_redirected_unrelated_article_is_not_attached_to_existing_identity(monkeypatch):
    async def fetch(*args, **kwargs):
        return page(title="Copper mining strike disrupts Chile exports"), "text/html"
    monkeypatch.setattr(news, "_fetch_text", fetch)
    monkeypatch.setattr(news, "_should_fetch_detail", lambda *a, **k: True)
    enriched, _ = asyncio.run(news._enrich_items_with_details([item()], source=news.NEWS_SOURCES[0]))
    assert enriched[0].title == TITLE and enriched[0].raw_text == BODY
    assert enriched[0].body_reason == "article_title_mismatch"
    assert not news.classify_summary_input(enriched[0])["eligible_for_summary"]
    assert not titles_conflict("Polyester market prices rise", "Polyester market prices rise - SunSirs")


def test_ingestion_stores_and_enqueues_long_complete_body_without_prefix_truncation(monkeypatch):
    rows, queue = [], []
    monkeypatch.setattr(news, "upsert_news_article", lambda **kw: rows.append(kw["payload"]))
    monkeypatch.setattr(news, "upsert_news_event_cluster", lambda **kw: None)
    monkeypatch.setattr(news, "enqueue_event_ai_summary", lambda *args: queue.append(args))
    monkeypatch.setattr(news, "_deepseek_client", lambda: type("Client", (), {"model": "test"})())
    long = BODY * 8
    news.ingest_news_items([replace(item(), raw_text=long, body_method="semantic_article")])
    assert rows[0]["raw_text"] == long
    assert len(queue) == 1
    news.ingest_news_items([replace(item(), raw_text=long * 3, body_method="semantic_article")])
    assert len(queue) == 2
    assert rows[1]["raw_text"] == long * 3
    assert not rows[1]["raw"]["source_content"]["stored_text_truncated"]
    assert rows[1]["raw"]["summary_input_quality"]["level"] == "full_text"
    assert rows[1]["raw"]["summary_input_quality"]["eligible_for_summary"]
    assert rows[1]["raw"]["summary_input_quality"]["summary_blocked_reason"] == ""


def test_persisted_truncation_and_tamper_metadata_propagate_to_consumer():
    row = {"raw_text": BODY, "raw": json.dumps({"source_content": {
        "stored_text_truncated": True, "stored_text_sha256": news._hash(BODY),
    }})}
    fields = news.stored_body_fields(row)
    quality = news.classify_summary_input(replace(item(), **fields))
    assert quality["reason"] == "article_body_truncated"
    row["raw_text"] += "changed"
    assert news.stored_body_fields(row)["body_reason"] == "stored_body_hash_mismatch"


def test_recovery_keeps_wrapper_identity_but_fetches_canonical_url():
    from scripts.backfill_news_article_bodies import item_for
    row = dict(source_id="google_news_oil_rss", tier="C", url="https://news.google.com/rss/articles/a",
               canonical_url=URL, title=TITLE, published_at="2026-09-20", first_seen_at="2026-09-20T00:00:00Z",
               raw_text=BODY, raw=json.dumps({"source_content": {"stored_text_truncated": True}}))
    recovered = item_for(row)
    assert recovered.url == URL and recovered.discovery_url == row["url"] and recovered.body_truncated


def test_consumer_rejects_persisted_bad_body_without_model_call(tmp_path, monkeypatch):
    from app import storage
    from app.settings import settings

    monkeypatch.setattr(storage, "settings", replace(settings, sqlite_path=str(tmp_path / "body.sqlite")))
    storage.upsert_news_article(article_id="bad-body", payload=dict(
        source_id="ppi_commodity_news", tier="B", url=URL, title=TITLE, content_hash="h",
        raw_text=BODY, raw={"source_content": {"status": "full_text", "stored_text_truncated": True}},
    ))
    storage.enqueue_event_ai_summary("bad-body", "h", "test", news.EVENT_SUMMARY_PROMPT_VERSION)

    class Client:
        model = "test"

        async def summarize_event_grounded(self, **kwargs):
            pytest.fail("Truncated input must not consume a model request")

    result = asyncio.run(news.process_event_summary_queue(limit=1, client=Client(), article_ids=["bad-body"]))
    assert result == {"selected": 1, "completed": 0, "failed": 1}


def test_expanded_stored_prefix_gets_new_visibility_even_if_original_hash_unchanged(tmp_path, monkeypatch):
    from app import storage
    from app.settings import settings

    monkeypatch.setattr(storage, "settings", replace(settings, sqlite_path=str(tmp_path / "time.sqlite")))
    old = dict(source_id="ppi_commodity_news", tier="B", url=URL, title=TITLE, content_hash="full-source-hash",
               raw_text=BODY[:500], first_seen_at="2026-09-20T00:00:00+00:00",
               raw={"source_content": {"status": "full_text"},
                    "content_visible_at": "2026-09-20T00:00:00+00:00"})
    monkeypatch.setattr(storage, "_now", lambda: "2026-09-20T00:00:00+00:00")
    storage.upsert_news_article(article_id="expanded", payload=old)
    monkeypatch.setattr(storage, "_now", lambda: "2026-09-27T01:00:00+00:00")
    restored, _ = storage.upsert_news_article(article_id="expanded", payload={**old, "raw_text": BODY})
    assert restored["raw"]["content_visible_at"] == "2026-09-27T01:00:00+00:00"
    assert restored["first_seen_at"] == old["first_seen_at"]


@pytest.mark.parametrize("ingest_ok", [True, False])
def test_recovery_journals_old_summary_and_verifies_write(tmp_path, monkeypatch, ingest_ok):
    from contextlib import closing

    from app import storage
    from app.settings import settings
    from scripts import backfill_news_article_bodies as worker

    isolated = replace(settings, sqlite_path=str(tmp_path / "recover.sqlite"))
    monkeypatch.setattr(storage, "settings", isolated)
    monkeypatch.setattr(worker, "settings", isolated)
    article_id = news._id("art", URL)
    old = dict(source_id="ppi_commodity_news", tier="B", url=URL, canonical_url=URL,
               title=TITLE, content_hash="old", raw_text="Short polyester market snippet.",
               first_seen_at="2026-09-20T00:00:00+00:00", published_at="2026-09-20",
               raw={"summary_input_quality": {"level": "partial_text"}})
    storage.upsert_news_article(article_id=article_id, payload=old)
    storage.enqueue_event_ai_summary(article_id, "old", "test", news.EVENT_SUMMARY_PROMPT_VERSION)
    with closing(storage.connect()) as con, con:
        con.execute("UPDATE event_ai_summaries SET attempts=3,summary_status='rejected'")
        row = dict(con.execute("SELECT * FROM news_articles WHERE article_id=?", (article_id,)).fetchone())

    async def enrich(items, source):
        return [replace(items[0], raw_text=BODY, body_method="publisher_sunsirs")], []

    monkeypatch.setattr(news, "_enrich_items_with_details", enrich)
    monkeypatch.setattr(news, "_deepseek_client", lambda: type("Client", (), {"model": "test"})())
    if not ingest_ok:
        monkeypatch.setattr(news, "ingest_news_items", lambda *a, **k: {"articles_found": 0})
    state = {article_id: {"content_hash": "old", "attempts": 2}}
    result = asyncio.run(worker.recover([row], state, tmp_path / "result.json", 1))[0]
    assert result["attempts"] == 3
    journal = json.loads(next((tmp_path / "body-recovery-journal").glob("*.json")).read_text())
    assert journal["summary"]["attempts"] == 3 and journal["summary"]["source_hash"] == "old"
    assert journal["article"]["raw_text"] == old["raw_text"]
    if ingest_ok:
        assert result["status"] == "completed"
        with closing(storage.connect()) as con:
            persisted = dict(con.execute("SELECT * FROM news_articles WHERE article_id=?", (article_id,)).fetchone())
        assert persisted["raw_text"] == BODY
        assert persisted["first_seen_at"] == old["first_seen_at"]
        assert json.loads(persisted["raw"])["content_visible_at"] > persisted["first_seen_at"]
    else:
        assert result["status"] == "failed" and result["reason"] == "body_not_persisted"


def test_texnet_brief_excludes_other_articles_and_requires_both_boundaries():
    from app.article_body_quality import texnet_body
    title = "9月11日涤纶POY为9242.50"
    body = "9月11日，涤纶POY参考价为9242.50，与9月1日(8767.50)相比，上涨了5.42%"
    page_text = (f"纺织网导航 {title} http://www.texnet.com.cn/ 2026-09-11 16:38:13 来源：生意社 "
                 f"{body} 文章关键词: 涤纶POY 打印文章 相关报道 别的价格10000")
    url = "https://info.texnet.com.cn/detail-1083817.html"
    assert texnet_body(page_text, url=url, title=title) == (body, "publisher_texnet")
    assert texnet_body(page_text.split("文章关键词:")[0], url=url, title=title) is None
    assert texnet_body(page_text, url=url, title="别的报道") is None
    item = news.RawNewsItem("texnet_polyester_news", "B", url, title, raw_text=page_text)
    assert not news.classify_summary_input(item)["eligible_for_summary"]
    clean = news.prepare_article_body(item)
    assert clean.raw_text == body
    assert news.classify_summary_input(clean)["eligible_for_summary"]
    assert "10000" not in clean.raw_text


@pytest.mark.parametrize("failure", ["busy", "partial_commit", "other_revision", "corrupt", "persistent_busy"])
def test_body_persistence_reuses_fetch_and_refuses_conflicting_revision(tmp_path, monkeypatch, failure):
    import sqlite3
    from contextlib import closing
    from types import SimpleNamespace

    from scripts import backfill_news_article_bodies as worker

    db = tmp_path / "body-retry.sqlite"
    with closing(sqlite3.connect(db)) as con, con:
        con.execute("CREATE TABLE news_articles(article_id TEXT, content_hash TEXT)")
        con.execute("INSERT INTO news_articles VALUES('article','old')")
    monkeypatch.setattr(worker, "settings", SimpleNamespace(sqlite_path=str(db)))
    target = news._hash(f"{TITLE}\n{BODY}")
    calls = []

    async def no_delay(seconds):
        pass

    def ingest(*args, **kwargs):
        calls.append(kwargs)
        if failure == "busy" and len(calls) == 2:
            return {"articles_found": 1}
        if failure in {"partial_commit", "other_revision"}:
            with closing(sqlite3.connect(db)) as con, con:
                con.execute("UPDATE news_articles SET content_hash=?", (
                    target if failure == "partial_commit" else "somebody_else",))
        raise sqlite3.OperationalError("database disk image is malformed" if failure == "corrupt"
                                       else "database is locked")

    monkeypatch.setattr(worker.asyncio, "sleep", no_delay)
    monkeypatch.setattr(news, "ingest_news_items", ingest)
    coroutine = worker.persist_recovered_body(
        item(), source=None,
        row={"article_id": "article", "content_hash": "old", "first_seen_at": "2024-01-01"},
    )
    if failure in {"corrupt", "persistent_busy"}:
        with pytest.raises(sqlite3.OperationalError):
            asyncio.run(coroutine)
        assert len(calls) == (3 if failure == "persistent_busy" else 1)
    elif failure == "other_revision":
        with pytest.raises(ValueError, match="source_changed"):
            asyncio.run(coroutine)
        assert len(calls) == 1
    else:
        assert asyncio.run(coroutine) == {"articles_found": 1}
        assert len(calls) == (2 if failure == "busy" else 1)


@pytest.mark.parametrize("existing_discovery", [False, True])
def test_resolving_canonical_wrapper_preserves_original_article_identity(monkeypatch, existing_discovery):
    from types import SimpleNamespace

    wrapper = "https://news.google.com/rss/articles/example"
    original = wrapper + "?oc=5" if existing_discovery else wrapper
    source = SimpleNamespace(source_id="google_news_oil_rss")
    article = news.RawNewsItem(
        source.source_id, "B", wrapper, TITLE,
        discovery_url=original if existing_discovery else "", raw_text="Short feed text",
    )

    async def fetch(url, **kwargs):
        return ("<html>wrapper</html>" if url == wrapper else page(), "text/html")

    monkeypatch.setattr(news, "_fetch_text", fetch)
    monkeypatch.setattr(news, "_public_original_url", lambda *args, **kwargs: URL)
    monkeypatch.setattr(news, "_should_fetch_detail", lambda *args, **kwargs: True)
    enriched, errors = asyncio.run(news._enrich_items_with_details([article], source=source))
    assert not errors
    assert enriched[0].url == URL
    assert enriched[0].discovery_url == original
    assert news._id("art", enriched[0].discovery_url) == news._id("art", original)
    assert enriched[0].raw_text == BODY

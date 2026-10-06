from app.industrial_intelligence.projection import intelligence_category, project_news_row
from app.workbench_events import _customer_category


def test_industry_title_overrides_incidental_oil_context():
    assert _customer_category("oil_policy", title="聚酯工业长丝产业链会议", body="原油 PTA 油价") == "供需"
    assert _customer_category("oil_policy", title="聚酯装置检修", body="原油") == "装置"
    assert _customer_category("oil_policy", title="原油产量上升", body="") == "原油"


def test_projection_maps_current_connector_categories():
    assert intelligence_category("polyester_chain") == "plant_supply"
    assert intelligence_category("shipping_security") == "shipping_ports"
    assert intelligence_category("macro_finance") == "macro_policy"


def test_invalid_eia_record_is_not_projected():
    assert project_news_row({
        "canonical_url": "https://www.eia.gov/todayinenergy/detail.php?id=",
        "article_id": "bad", "source_id": "eia_today_in_energy",
    }) is None


def test_market_cache_survives_clock_only_snapshot_changes(monkeypatch):
    import importlib
    main = importlib.import_module("app.main")
    state = {"snapshot": "clock-one", "summary": []}
    calls = []
    monkeypatch.setattr(main, "build_full_chain_summary", lambda **kw: {
        "data_snapshot_id": state["snapshot"], "summary": state["summary"], "status": "ready"})
    monkeypatch.setattr(main, "latest_intraday_price_observations", lambda **kw: [])
    def build(**kw):
        calls.append(kw)
        return {"products": [], "coverage": {}}
    monkeypatch.setattr(main, "build_market_chain_workbench", build)
    main._MARKET_CHAIN_CACHE.clear()
    main.workbench_market_chain()
    state["snapshot"] = "clock-two"
    assert main.workbench_market_chain()["cached"] is True
    assert len(calls) == 1
    state["summary"] = [{"product": "POY", "value": 9000}]
    assert main.workbench_market_chain().get("cached") is not True
    assert len(calls) == 2
    main._MARKET_CHAIN_CACHE.clear()


def test_error_page_cannot_be_full_text_but_error_reporting_is_allowed():
    from app.news import RawNewsItem, classify_summary_input
    from app.news_relevance import error_page_title, unusable_title
    for title in ("EIA - Sorry! Unexpected Error", "404 Page Not Found", "Service Unavailable"):
        assert error_page_title(title)
        assert unusable_title(title)
        quality = classify_summary_input(RawNewsItem(
            "eia_today_in_energy", "A", "https://www.eia.gov/todayinenergy/detail.php?id=67925",
            title, raw_text="Energy data navigation and links. " * 100,
        ))
        assert quality["eligible_for_summary"] is False
        assert quality["reason"] == "source_error_page"
    assert not error_page_title("EIA reports an unexpected error in oil production estimates")


def test_error_page_excluded_before_customer_pagination(monkeypatch):
    from app import workbench_events
    monkeypatch.setattr(workbench_events, "_load_event_rows", lambda **kw: [{
        "title": "EIA - Sorry! Unexpected Error", "source_id": "eia_today_in_energy",
        "url": "https://www.eia.gov/todayinenergy/detail.php?id=67925",
    }])
    result = workbench_events._build_event_library_workbench_uncached(limit=30, offset=0, q="", category="")
    assert result["total_events"] == 0
    assert result["events"] == []


def test_ec_uses_published_feed_without_admitting_unrelated_announcements():
    from app.news import _parse_feed, get_news_source
    source = get_news_source("european_commission_press")
    assert source.fetcher == "rss"
    feed = """<rss><channel>
      <item><title>Commission adopts new oil sanctions</title>
<link>https://ec.europa.eu/commission/presscorner/detail/en/ip_26_1</link>
<pubDate>Thu, 10 Sep 2026 12:00:00 GMT</pubDate>
<description>Oil imports and energy supply measures.</description></item>
      <item><title>European Capitals of Culture</title>
<link>https://ec.europa.eu/commission/presscorner/detail/en/ip_26_2</link>
<description>Cultural celebration.</description></item>
    </channel></rss>"""
    items = _parse_feed(feed, source)
    assert len(items) == 1
    assert items[0].title == "Commission adopts new oil sanctions"
    assert items[0].published_at == "2026-09-10T12:00:00+00:00"

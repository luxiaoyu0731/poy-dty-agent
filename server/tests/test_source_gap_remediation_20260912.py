"""2026-09-12 source-gap remediation: discovery funnel, NAPHTHA fallback, new CN sources."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path

import httpx

from app import news as news_module
from app import price_intraday
from app.industrial_intelligence import source_catalog
from app.news import RawNewsItem
from app.settings import settings


@contextmanager
def _outbound_hosts(hosts: tuple[str, ...]):
    """Temporarily pin the frozen settings singleton; ambient .env must not decide behavior."""

    saved = object.__getattribute__(settings, "outbound_hosts")
    object.__setattr__(settings, "outbound_hosts", hosts)
    try:
        yield
    finally:
        object.__setattr__(settings, "outbound_hosts", saved)


def _remediation_hosts() -> tuple[str, ...]:
    base = tuple(settings.outbound_hosts)
    additions = (
        "zh.tradingeconomics.com",
        "www.tradingeconomics.com",
        "www.100ppi.com",
        "www.cnbc.com",
        "oilprice.com",
        "finance.yahoo.com",
        "www.csis.org",
        "www.cfr.org",
        "maritime-executive.com",
    )
    return base + tuple(host for host in additions if host not in base)


def _discovery_source() -> object:
    source = news_module.get_news_source("google_news_oil_rss")
    assert source is not None
    return source


def test_google_news_decode_request_uses_answered_template() -> None:
    request = news_module._google_news_batchexecute_request("article-id", "1789177753", "sig")

    assert request.startswith('[[["Fbv4je"')
    assert "garturlreq" in request
    assert "US:en" in request
    assert "article-id" in request
    assert "1789177753" in request
    assert "sig" in request
    # The retired finance-index template is answered with a null payload.
    assert "FINANCE_TOP_INDICES" not in request
    assert "655000234" not in request


def test_discovery_publisher_detail_fetch_is_curated_and_allowlisted() -> None:
    source = _discovery_source()

    def allows(url: str) -> bool:
        return news_module._should_fetch_detail(
            RawNewsItem("google_news_oil_rss", "C", url, "title"),
            source=source,  # type: ignore[arg-type]
        )

    with _outbound_hosts(_remediation_hosts()):
        assert allows("https://www.cnbc.com/2026/09/11/saudi-arabia-shut-down-east-west-crude-oil-pipeline.html")
        assert allows("https://finance.yahoo.com/news/oil-price-120000000.html")
        assert allows("https://oilprice.com/Latest-Energy-News/World-News/example.html")
        # Allowlisted but neither curated nor registry-public: still refused.
        assert not allows("https://hq.sinajs.cn/list=nf_TA0")
    # Not in OUTBOUND_FETCH_HOSTS at all: refused regardless of curation.
    assert not allows("https://www.example.com/news/story.html")
    # Discovery wrappers themselves are never treated as publisher articles.
    assert not allows("https://news.google.com/rss/articles/CBMi?oc=5")


def test_chemical_rss_query_replaces_ambiguous_bare_product_terms() -> None:
    source = news_module.get_news_source("google_news_chemical_rss")
    assert source is not None
    assert "%22polyester%20POY%22" in source.url
    assert "%22polyester%20DTY%22" in source.url
    assert "%22polyester%20yarn%22" in source.url
    assert "OR%20POY%20" not in source.url
    assert "OR%20DTY%20" not in source.url

    archive_url = news_module._google_news_archive_url(
        source, start_date="2026-09-01", end_date="2026-09-02"
    )
    # urlencode renders spaces as '+', unlike the hand-encoded static feed URL.
    assert "%22polyester+POY%22" in archive_url
    assert "POY+OR+DTY" not in archive_url


def test_chemical_chain_filter_still_rejects_sports_poy_noise() -> None:
    assert not news_module._is_relevant_feed_item(
        news_module.get_news_source("google_news_chemical_rss"),  # type: ignore[arg-type]
        "2026 NFL awards tracker: Odds for MVP, Rookie of the Year and more",
        "Player of the year race headlines.",
    )
    assert news_module._is_relevant_feed_item(
        news_module.get_news_source("google_news_chemical_rss"),  # type: ignore[arg-type]
        "Monoethylene glycol market prices rise on feedstock pressure",
        "MEG values followed stronger feedstock costs.",
    )


def test_new_china_industry_sources_accept_only_verified_article_paths() -> None:
    texnet = news_module.get_news_source("texnet_polyester_news")
    ppi = news_module.get_news_source("ppi_commodity_news")
    assert texnet is not None and ppi is not None

    assert news_module._is_allowed_article_link(texnet, "https://info.texnet.com.cn/detail-1083817.html")
    assert not news_module._is_allowed_article_link(texnet, "https://info.texnet.com.cn/list--20-.html")
    assert not news_module._is_allowed_article_link(texnet, "https://www.texnet.com.cn/detail-1083817.html")

    assert news_module._is_allowed_article_link(ppi, "https://www.100ppi.com/news/detail-20260911-6201878.html")
    assert not news_module._is_allowed_article_link(ppi, "https://www.100ppi.com/")
    assert not news_module._is_allowed_article_link(ppi, "https://www.100ppi.com/news/detail-260911-1.html")

    # Chinese chain-product titles are relevant; nylon and unrelated commodities are not.
    assert news_module._is_relevant("9月11日涤纶POY为9242.50", "")
    assert news_module._is_relevant("涤纶POY商品报价动态（2026-09-11）", "")
    assert news_module._is_relevant("石脑油商品报价动态（2026-09-11）", "")
    assert not news_module._is_relevant("9月11日锦纶DTY为17520.00", "")
    assert not news_module._is_relevant("豆粕商品报价动态（2026-09-11）", "")


def test_new_china_industry_items_pass_ingest_relevance_gate() -> None:
    texnet = news_module.get_news_source("texnet_polyester_news")
    assert texnet is not None
    item = RawNewsItem(
        source_id="texnet_polyester_news",
        tier="B",
        url="https://info.texnet.com.cn/detail-1083817.html",
        title="9月11日涤纶POY为9242.50",
        published_at="2026-09-11",
        raw_text="9月11日涤纶POY为9242.50，较前一交易日上涨。",
    )
    analysis = news_module.analyze_news_item(item, source=texnet)
    assert analysis["score"] >= 25
    assert "POY" in analysis["affected_products"]


def test_naphtha_english_fallback_page_parses_with_series_compatible_unit() -> None:
    row = price_intraday._public_spot_page_to_row(
        "<html>Naphtha Chemical 855.28 2026-09-11 Naphtha Chemical 871.17 2026-09-10</html>",
        instrument="NAPHTHA",
        symbol="NAPHTHA_PUBLIC_SPOT",
        labels=price_intraday.NAPHTHA_FALLBACK_PUBLIC_SPOT_PAGE[1],
        unit=price_intraday.NAPHTHA_FALLBACK_PUBLIC_SPOT_PAGE[2],
        source_url=price_intraday.NAPHTHA_FALLBACK_PUBLIC_SPOT_PAGE[0],
        latency=0.1,
    )
    assert row["last"] == 855.28
    assert row["observed_at"] == "2026-09-11"
    assert row["unit"] == "USD/mt"
    assert price_intraday.is_plausible_intraday_price("NAPHTHA", row["last"])


class _FakePageResponse:
    def __init__(self, text: str, url: str) -> None:
        self.text = text
        self.url = url

    def raise_for_status(self) -> None:
        return None


def test_naphtha_collection_falls_back_to_english_host_when_primary_fails(monkeypatch) -> None:
    primary_url = price_intraday.PUBLIC_SPOT_PAGES["NAPHTHA"][0]
    fallback_url = price_intraday.NAPHTHA_FALLBACK_PUBLIC_SPOT_PAGE[0]

    async def fake_get_allowed(_client: object, url: str, **_kwargs: object) -> _FakePageResponse:
        if url == primary_url:
            raise httpx.ConnectError("primary host blocked")
        assert url == fallback_url
        return _FakePageResponse(
            "<html>Naphtha Chemical 855.28 2026-09-11</html>",
            fallback_url,
        )

    monkeypatch.setattr(price_intraday, "_get_allowed", fake_get_allowed)
    errors: list[price_intraday.ProviderError] = []

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient() as client:
            return await price_intraday._collect_public_spot_pages(client, {"NAPHTHA"}, errors)

    rows = asyncio.run(run())

    assert errors == []
    assert len(rows) == 1
    assert rows[0]["source_url"] == fallback_url
    assert rows[0]["last"] == 855.28
    assert rows[0]["observed_at"] == "2026-09-11"


def test_naphtha_collection_reports_error_only_after_all_candidates_fail(monkeypatch) -> None:
    async def fake_get_allowed(_client: object, url: str, **_kwargs: object) -> _FakePageResponse:
        raise httpx.ConnectError(f"unreachable: {url}")

    monkeypatch.setattr(price_intraday, "_get_allowed", fake_get_allowed)
    errors: list[price_intraday.ProviderError] = []

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient() as client:
            return await price_intraday._collect_public_spot_pages(client, {"NAPHTHA"}, errors)

    rows = asyncio.run(run())

    assert rows == []
    assert len(errors) == 1
    assert errors[0].instrument == "NAPHTHA"
    assert price_intraday.PUBLIC_SPOT_PAGES["NAPHTHA"][0] in errors[0].error


def test_default_outbound_allowlist_covers_every_new_source_and_publisher_host() -> None:
    # Behavior gate: the publisher set only works when every host is allowlisted.
    with _outbound_hosts(_remediation_hosts()):
        assert all(host in settings.outbound_hosts for host in news_module.DISCOVERY_PUBLISHER_HOSTS)
    # Literal default: settings.py's fallback list must carry the new hosts even
    # when an ambient .env pins OUTBOUND_FETCH_HOSTS over it.
    import app.settings as settings_module

    literal = Path(settings_module.__file__).read_text(encoding="utf-8")
    for host in (
        "zh.tradingeconomics.com",
        "www.tradingeconomics.com",
        "www.100ppi.com",
        "www.cnbc.com",
        "oilprice.com",
        "finance.yahoo.com",
    ):
        assert host in literal, host


def test_catalog_baseline_constants_track_added_sources() -> None:
    assert len(news_module.NEWS_SOURCES) == source_catalog.EXPECTED_NEWS_COUNT
    derivation = source_catalog.derive_catalog()
    assert derivation.active_baseline_count == (
        source_catalog.EXPECTED_ACTIVE_CORE_COUNT
        + source_catalog.EXPECTED_NEWS_COUNT
        - len(source_catalog.EXPECTED_OVERLAP_IDS)
    )
    entry_ids = {entry["source_id"] for entry in derivation.entries}
    assert {"texnet_polyester_news", "ppi_commodity_news"} <= entry_ids


def test_ppi_reference_quote_parses_dated_aggregate_not_spread_line() -> None:
    text = (
        "涤纶POY9月4日均差为140.80元/吨 由正向扩大转为缩小；"
        "9月11日，涤纶POY参考价为9,242.50，与9月1日(8,767.50)相比，上涨了5.42%"
    )
    quote = price_intraday._ppi_reference_quote(text, "POY")

    assert quote is not None
    value, observed_at, evidence = quote
    assert value == 9242.5
    assert observed_at.endswith("-09-11")
    assert "涤纶POY为9242.5" in evidence


def test_ppi_reference_quote_resolves_ambiguous_year_backward() -> None:
    quote = price_intraday._ppi_reference_quote("12月30日，涤纶DTY参考价为10365.00", "DTY")

    assert quote is not None
    _, observed_at, _ = quote
    # A month/day ahead of today belongs to the previous assessment year.
    assert observed_at.endswith("-12-30")
    assert int(observed_at[:4]) < 2026


def test_ppi_reference_quote_returns_none_without_dated_value() -> None:
    assert price_intraday._ppi_reference_quote("涤纶POY参考价暂无", "POY") is None
    assert price_intraday._ppi_reference_quote("13月40日，涤纶POY参考价为9000", "POY") is None


def test_poy_falls_back_to_ppi_reference_when_texnet_fails(monkeypatch) -> None:
    texnet_url = price_intraday.PUBLIC_SPOT_PAGES["POY"][0]
    ppi_url = price_intraday.PUBLIC_SPOT_PAGE_FALLBACKS["POY"][0][0]
    ppi_html = (
        "<html>9月11日，涤纶POY参考价为9242.50，与9月1日(8767.50)相比，上涨了5.42%</html>"
    )

    async def fake_get_allowed(_client: object, url: str, **_kwargs: object):
        if url == texnet_url:
            raise httpx.ConnectError("texnet unreachable")
        assert url == ppi_url
        return _FakePageResponse(ppi_html, ppi_url)

    monkeypatch.setattr(price_intraday, "_get_allowed", fake_get_allowed)
    errors: list[price_intraday.ProviderError] = []

    async def run() -> list[dict[str, object]]:
        async with httpx.AsyncClient() as client:
            return await price_intraday._collect_public_spot_pages(client, {"POY"}, errors)

    rows = asyncio.run(run())

    assert errors == []
    assert len(rows) == 1
    assert rows[0]["last"] == 9242.5
    assert rows[0]["observed_at"] == "2026-09-11"
    assert rows[0]["source_id"] == "public_spot_page_refresh"
    # The synthesized evidence must satisfy the polyester identity gate.
    assert price_intraday.public_spot_quote_matches_instrument("POY", rows[0]) is True


def test_sina_hf_to_row_maps_documented_global_fields() -> None:
    body = (
        'var hq_str_hf_OIL="104.246,,104.320,104.470,109.970,103.500,05:59:55,'
        '107.630,109.940,0,1,1,2026-09-12,布伦特原油,449953";'
    )
    row = price_intraday._sina_hf_to_row(
        body,
        instrument="Brent",
        symbol="OIL",
        label="布伦特原油外盘公开行情",
        unit="USD/bbl",
        source_url="https://hq.sinajs.cn/list=hf_OIL",
        latency=0.1,
    )
    assert row["last"] == 104.246
    assert row["high"] == 109.97
    assert row["low"] == 103.5
    assert row["open"] == 109.94
    assert row["observed_at"] == "2026-09-12T05:59:55+08:00"
    assert row["change_pct"] == round((104.246 - 107.63) / 107.63 * 100, 4)
    assert row["volume"] is None
    assert row["source_id"] == "sina_global_futures"


def test_brent_falls_back_to_sina_global_when_yahoo_fails(monkeypatch) -> None:
    async def fake_get_allowed(_client: object, url: str, **_kwargs: object):
        if "query1.finance.yahoo.com" in url:
            raise httpx.ConnectError("yahoo unreachable")
        assert "hq.sinajs.cn" in url
        body = (
            'var hq_str_hf_OIL="104.246,,104.320,104.470,109.970,103.500,05:59:55,'
            '107.630,109.940,0,1,1,2026-09-12,布伦特原油,449953";'
        )
        return _FakePageResponse(body, url)

    monkeypatch.setattr(price_intraday, "_get_allowed", fake_get_allowed)

    async def run() -> dict[str, object]:
        return await price_intraday.collect_intraday_prices(instruments=["Brent"], apply=False)

    result = asyncio.run(run())
    rows = [r for r in result["items"] if r["instrument"] == "Brent"]  # type: ignore[index]
    assert len(rows) == 1
    assert rows[0]["source_id"] == "sina_global_futures"
    assert rows[0]["last"] == 104.246
    assert any(e["source_id"] == "yahoo_finance_proxy" for e in result["errors"])  # type: ignore[index]

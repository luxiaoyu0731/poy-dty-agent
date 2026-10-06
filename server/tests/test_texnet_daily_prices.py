import asyncio

import httpx

from app import price_intraday as prices

DAILY = """<h1>2026年9月9日纺织大宗商品价格涨跌榜</h1>
<table><tr><th>商品</th><th>行业</th><th>9月8日价格</th><th>9月9日价格</th></tr>
<tr><td>锦纶POY</td><td>纺织</td><td>15000</td><td>16000</td></tr>
<tr><td>涤纶POY</td><td>纺织</td><td>9000</td><td>9080.00</td></tr>
<tr><td>涤纶DTY</td><td>纺织</td><td>10122.14</td><td>10122.14</td></tr></table>"""


def test_exact_product_and_current_column_even_when_price_unchanged():
    for instrument, value in [("POY", 9080.0), ("DTY", 10122.14)]:
        row = prices._public_spot_page_to_row(
            DAILY, instrument=instrument, symbol=instrument,
            labels=prices.PUBLIC_SPOT_PAGES[instrument][1], unit="CNY/mt",
            source_url="https://info.texnet.com.cn/detail-1083521.html", latency=0,
        )
        assert (row["observed_at"], row["last"]) == ("2026-09-09", value)
        assert prices.public_spot_quote_matches_instrument(instrument, row)
        assert "原始行=" in row["raw"]["quote"]


def test_missing_current_column_and_challenge_are_not_quotes():
    assert prices._texnet_daily_quote(DAILY.replace("9月9日价格", "9月7日价格"), "POY") is None
    assert prices._texnet_daily_quote("<script>challenge()</script>", "POY") is None
    assert prices._texnet_daily_quote(DAILY.replace("涤纶POY", "锦纶POY"), "POY") is None


def test_only_dated_same_host_article_can_be_followed():
    html = '''<a href="https://other.test/detail-1.html">2026年9月9日纺织大宗商品价格涨跌榜</a>
    <a href="/detail-2.html">2026年9月8日纺织大宗商品价格涨跌榜</a>
    <a href="/detail-3.html">2026年9月9日纺织大宗商品价格涨跌榜</a>'''
    assert prices._texnet_latest_daily_url(html, "https://info.texnet.com.cn/list--20-.html") == "https://info.texnet.com.cn/detail-3.html"


def test_collector_uses_daily_and_shares_requests(monkeypatch):
    listing = (
        '<a href="/detail-3.html">2026年9月9日纺织大宗商品价格涨跌榜</a> '
        '2026-09-08 涤纶POY为9080.00 2026-09-08 涤纶DTY为10122.14'
    )
    requested = []

    async def get(client, url, **kwargs):
        requested.append(url)
        return httpx.Response(200, text=DAILY if "detail-" in url else listing, request=httpx.Request("GET", url))

    monkeypatch.setattr(prices, "_get_allowed", get)
    monkeypatch.setattr(prices, "PUBLIC_SPOT_PAGE_FALLBACKS", {})

    async def run():
        async with httpx.AsyncClient() as client:
            errors = []
            rows = await prices._collect_public_spot_pages(client, {"POY", "DTY"}, errors)
            assert not errors
            assert len(rows) == 2
            assert all(row["observed_at"] == "2026-09-09" for row in rows)
            assert len(requested) == 2
    asyncio.run(run())


def test_daily_challenge_keeps_older_dated_listing_and_records_failure(monkeypatch):
    listing = (
        '<a href="/detail-3.html">2026年9月9日纺织大宗商品价格涨跌榜</a>'
        '2026-09-08 涤纶POY为9080.00'
    )

    async def get(client, url, **kwargs):
        return httpx.Response(
            200, text="<script>challenge()</script>" if "detail-" in url else listing,
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(prices, "_get_allowed", get)
    monkeypatch.setattr(prices, "PUBLIC_SPOT_PAGE_FALLBACKS", {})

    async def run():
        async with httpx.AsyncClient() as client:
            errors = []
            rows = await prices._collect_public_spot_pages(client, {"POY"}, errors)
            assert len(errors) == 1
            assert rows[0]["observed_at"] == "2026-09-08"
            assert rows[0]["source_url"].endswith("list--20-.html")
    asyncio.run(run())



def test_stale_primary_tries_equivalent_fallback_and_selects_newest(monkeypatch):
    async def run(fallback_date, fallback_fails=False):
        called = []
        async def get(client, url, **kwargs):
            called.append(url)
            if "backup" in url and fallback_fails:
                raise ValueError("challenge")
            return httpx.Response(200, text=url, request=httpx.Request("GET", url))
        def parse(text, **kwargs):
            return {"observed_at": fallback_date if "backup" in text else "2020-01-01", "source_url": text}
        monkeypatch.setattr(prices, "_get_allowed", get)
        monkeypatch.setattr(prices, "_public_spot_page_to_row", parse)
        monkeypatch.setattr(prices, "PUBLIC_SPOT_PAGES", {"NAPHTHA": ("https://primary.test/", ("Naphtha",), "USD/mt")})
        monkeypatch.setattr(prices, "PUBLIC_SPOT_PAGE_FALLBACKS",
                            {"NAPHTHA": (("https://backup.test/", ("Naphtha",), "USD/mt"),)})
        async with httpx.AsyncClient() as client:
            errors = []
            rows = await prices._collect_public_spot_pages(client, {"NAPHTHA"}, errors)
        assert len(called) == 2 and not errors
        return rows[0]
    assert asyncio.run(run("2020-01-02"))["source_url"] == "https://backup.test/"
    assert asyncio.run(run("2020-01-01"))["source_url"] == "https://primary.test/"
    assert asyncio.run(run("2020-01-02", True))["source_url"] == "https://primary.test/"

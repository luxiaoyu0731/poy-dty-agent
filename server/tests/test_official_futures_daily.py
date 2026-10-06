from __future__ import annotations

import asyncio
from datetime import date

from app.models import SourceConfig
from app.official_futures_daily import fetch_czce_pta_px_daily, parse_czce_daily_text
from app.settings import settings


def _source() -> SourceConfig:
    return SourceConfig(
        source_id="czce_pta_px",
        source_name="CZCE",
        tier="A",
        category="china_futures",
        url="https://www.czce.com.cn/",
        crawl_type="html_download",
        auth_type="public_personal_reuse",
        frequency="daily_public",
        products=["pta", "px", "methanol"],
        freshness_sla_minutes=1440,
        license_note="public",
        reliability_score=0.95,
    )


def _daily_text() -> str:
    return """
郑州商品交易所期货每日行情表(2026-08-31)
合约代码|昨结算|今开盘|最高价|最低价|今收盘|今结算|涨跌1|涨跌2|成交量(手)|持仓量|增减量|成交额(万元)|交割结算价
TA609 |5,000|5,010|5,060|4,990|5,050|5,040|50|40|10,000|20,000|100|500,000|
TA701 |5,100|5,110|5,160|5,090|5,150|5,140|50|40|9,000|30,000|100|450,000|
PX609 |7,000|7,010|7,060|6,990|7,050|7,040|50|40|8,000|12,000|100|400,000|
PX701 |7,100|7,110|7,160|7,090|7,150|7,140|50|40|7,000|11,000|100|350,000|
MA609 |2,800|2,810|2,860|2,790|2,850|2,840|50|40|20,000|25,000|100|560,000|
MA701 |2,900|2,910|2,960|2,890|2,950|2,940|50|40|19,000|35,000|100|550,000|
品种小计|0|0|0|0|0|0|0|0|0|0|0|0|0
"""


def test_czce_parser_keeps_supported_products_and_freezes_main_rule_and_lineage() -> None:
    rows = parse_czce_daily_text(
        _daily_text(),
        trade_date="2026-08-31",
        source_url="https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20260831/FutureDataDaily.txt",
        captured_at="2026-08-31T08:00:00+00:00",
        raw_sha256="a" * 64,
    )

    assert len(rows) == 6
    assert {(row["product"], row["contract_code"]) for row in rows if row["is_main"]} == {
        ("PTA", "TA701"),
        ("PX", "PX609"),
        ("METHANOL", "MA701"),
    }
    assert all(row["source_id"] == "czce_pta_px" and row["visible_at"].endswith("+00:00") for row in rows)
    assert all(row["raw"]["raw_sha256"] == "a" * 64 for row in rows)


def test_czce_parser_skips_zero_volume_inactive_contract_but_rejects_active_missing_prices() -> None:
    inactive = _daily_text() + (
        "\nPX707 |7,608|0|0|0|0|7,660|52|52|0|12|0|0|\n"
    )
    rows = parse_czce_daily_text(
        inactive,
        trade_date="2026-08-31",
        source_url="https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20260831/FutureDataDaily.txt",
        captured_at="2026-08-31T08:00:00+00:00",
        raw_sha256="b" * 64,
    )
    assert all(row["contract_code"] != "PX707" for row in rows)

    active_missing_price = _daily_text() + (
        "\nPX707 |7,608|0|0|0|0|7,660|52|52|1|12|0|3.8|\n"
    )
    try:
        parse_czce_daily_text(
            active_missing_price,
            trade_date="2026-08-31",
            source_url="https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20260831/FutureDataDaily.txt",
            captured_at="2026-08-31T08:00:00+00:00",
            raw_sha256="c" * 64,
        )
    except ValueError as exc:
        assert str(exc) == "czce_daily_active_row_missing_price:PX707"
    else:
        raise AssertionError("active CZCE rows with missing prices must fail closed")


def test_czce_fetcher_is_https_allowlisted_bounded_and_emits_all_supported_revisions() -> None:
    class Response:
        def __init__(self, status_code: int, text: str = "") -> None:
            self.status_code = status_code
            self.text = text
            self.content = text.encode()
            self.headers = {"content-type": "text/plain; charset=utf-8"}

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise AssertionError(f"unexpected status {self.status_code}")

    class Client:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get(self, url: str, **_kwargs) -> Response:
            self.urls.append(url)
            return Response(200, _daily_text()) if "20260831" in url else Response(404)

    client = Client()
    original = settings.require_outbound_url_allowed
    object.__setattr__(settings, "require_outbound_url_allowed", lambda url: None)
    try:
        result = asyncio.run(
            fetch_czce_pta_px_daily(_source(), lookback_days=2, today=date(2026, 8, 31), client=client)
        )
    finally:
        object.__setattr__(settings, "require_outbound_url_allowed", original)

    assert result.status == "ok"
    assert len(client.urls) == 2 and all(url.startswith("https://www.czce.com.cn/") for url in client.urls)
    assert len(result.bars) == 6
    assert {item["semantic_series_id"] for item in result.capture_revisions} == {
        "pta.czce.main_continuous.settlement.cny_mt",
        "px.czce.main_continuous.settlement.cny_mt",
        "methanol.czce.main.settlement.cny_mt",
    }



def test_delivery_settlement_row_does_not_abort_other_products():
    text = _daily_text() + "\nMA608 |3891|0|0|0|0|3914|23|23|479|0|-1309|1874.81|3485|\n"
    skipped = []
    rows = parse_czce_daily_text(text, trade_date="2026-08-31", source_url="https://www.czce.com.cn/file",
                                captured_at="2026-08-31T08:00:00Z", raw_sha256="d" * 64, skipped_rows=skipped)
    assert len(rows) == 6
    assert skipped[0]["contract"] == "MA608"
    assert skipped[0]["reason"] == "delivery_settlement_without_trading_bar"
    assert {r["product"] for r in rows if r["is_main"]} == {"PTA", "PX", "METHANOL"}


def test_delivery_row_with_residual_positions_never_becomes_training_price():
    # Actual 2026-07-13 MA607 official row: open delivery interest is nonzero.
    text = _daily_text() + '\nMA607 |2470|0|0|0|0|2525|55|55|200|2000|-200|524|2420\n'
    skipped = []
    rows = parse_czce_daily_text(text,trade_date='2026-07-13',source_url='https://www.czce.com.cn/',
        captured_at='2026-09-20T08:00:00Z',raw_sha256='a'*64,skipped_rows=skipped)
    assert any(row['product']=='PTA' for row in rows)
    assert not any(row['contract_code']=='MA607' for row in rows)
    assert skipped[0]['reason']=='delivery_settlement_without_trading_bar'

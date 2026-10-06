from __future__ import annotations

import asyncio
import csv
import io
import zipfile

import pytest

import app.fetchers as fetchers_module
from app.fetchers import (
    Fetcher,
    cfets_cny_parity_to_observation,
    cfets_published_at,
    cftc_zip_to_observations,
    coalchina_cctd_bohai_rim_daily_detail_url,
    coalchina_cctd_bohai_rim_daily_to_observation,
    eia_brent_capture_revisions,
    eia_rows_to_observations,
    fred_row_to_observation,
    is_cftc_petroleum_market,
    sunsirs_mx_east_china_daily_detail_url,
    sunsirs_mx_east_china_daily_to_observation,
)
from app.models import SourceConfig
from app.public_source_adapters import StructuredSourceResult
from app.source_registry import load_sources


def source(source_id: str = "eia_petroleum_api") -> SourceConfig:
    return SourceConfig(
        source_id=source_id,
        source_name="Test Source",
        tier="A",
        category="test",
        url="https://example.test",
        crawl_type="api_json",
        auth_type="public",
        frequency="daily",
        products=["test"],
        freshness_sla_minutes=60,
        license_note="test",
        reliability_score=0.9,
    )


def test_structured_un_comtrade_and_tnc_results_return_from_dedicated_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_un(*_args, **_kwargs) -> StructuredSourceResult:
        return StructuredSourceResult(
            status="partial",
            content_type="application/json",
            content_preview="UN preview fallback",
            observations=[{"source_id": "un_comtrade_api"}],
            state_update={"schema_version": "un-comtrade-backfill.v1"},
        )

    async def fake_tnc(*_args, **_kwargs) -> StructuredSourceResult:
        return StructuredSourceResult(
            status="ok",
            content_type="text/html",
            content_preview="TNC history",
            observations=[{"source_id": "tnc_polyester_history"}],
            capture_revisions=[{"semantic_series_id": "poy.public.polyester_spot_assessment.cny_mt"}],
        )

    monkeypatch.setattr(fetchers_module, "fetch_un_comtrade", fake_un)
    monkeypatch.setattr(fetchers_module, "fetch_tnc_polyester_history", fake_tnc)

    un_result = asyncio.run(Fetcher().fetch(source("un_comtrade_api")))
    tnc_result = asyncio.run(Fetcher().fetch(source("tnc_polyester_history")))

    assert un_result.status == "partial"
    assert un_result.observations == [{"source_id": "un_comtrade_api"}]
    assert un_result.state_update == {"schema_version": "un-comtrade-backfill.v1"}
    assert tnc_result.status == "ok"
    assert tnc_result.observations == [{"source_id": "tnc_polyester_history"}]
    assert tnc_result.capture_revisions == [
        {"semantic_series_id": "poy.public.polyester_spot_assessment.cny_mt"}
    ]


def test_eia_brent_fetch_emits_conservative_append_only_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        headers = {"content-type": "application/json", "date": "Mon, 31 Aug 2026 12:00:00 GMT"}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {
                "response": {
                    "data": [
                        {
                            "period": "2026-08-28",
                            "series": "RBRTE",
                            "series-description": "Europe Brent Spot Price FOB (Dollars per Barrel)",
                            "value": "74.50",
                            "units": "$/BBL",
                            "area-name": "NA",
                        }
                    ]
                }
            }

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str, **_kwargs: object) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(
        fetchers_module,
        "EIA_SERIES_GROUPS",
        ({"frequency": "daily", "path": "petroleum/pri/spt", "series": {"RBRTE": "crude_oil"}},),
    )
    monkeypatch.setattr(fetchers_module.httpx, "AsyncClient", lambda **_kwargs: FakeAsyncClient())
    monkeypatch.setattr(type(fetchers_module.settings), "outbound_host_allowed", lambda _self, _host: True)
    monkeypatch.setattr(Fetcher, "_now", staticmethod(lambda: "2026-08-31T12:00:01+00:00"))

    result = asyncio.run(Fetcher()._fetch_eia(source("eia_petroleum_api"), "secret-not-logged"))

    assert len(result.observations) == 1
    assert len(result.capture_revisions) == 1
    capture = result.capture_revisions[0]
    assert capture["semantic_series_id"] == "crude.brent.eia.spot.usd_bbl"
    assert capture["observed_at"] == "2026-08-28"
    assert capture["published_at"] == "2026-08-31T12:00:01+00:00"
    assert capture["visible_at"] == "2026-08-31T12:00:01+00:00"
    assert capture["parser_version"] == "eia-brent-open-data.v2"
    assert len(str(capture["raw_sha256"])) == 64


def test_eia_rows_to_observations_maps_target_series_and_skips_missing_values() -> None:
    rows = [
        {
            "period": "2026-06-12",
            "series": "WCESTUS1",
            "series-description": "U.S. Ending Stocks excluding SPR of Crude Oil",
            "value": "418222",
            "units": "MBBL",
            "area-name": "U.S.",
        },
        {
            "period": "2026-06-12",
            "series": "WPULEUS3",
            "series-description": "U.S. Percent Utilization of Refinery Operable Capacity",
            "value": ".",
            "units": "%",
            "area-name": "U.S.",
        },
    ]

    observations = eia_rows_to_observations(
        rows,
        source=source(),
        product_by_series={"WCESTUS1": "crude_oil", "WPULEUS3": "refinery_run"},
        frequency="weekly",
        evidence_url="https://api.eia.gov/v2/petroleum/stoc/wstk/data/",
    )

    assert len(observations) == 1
    assert observations[0]["product"] == "crude_oil"
    assert observations[0]["value"] == 418222
    assert observations[0]["frequency"] == "weekly"
    assert observations[0]["raw"]["series"] == "WCESTUS1"


def test_eia_brent_capture_revisions_skip_non_brent_and_invalid_periods() -> None:
    rows = [
        {
            "source_id": "eia_petroleum_api",
            "observed_at": "2026-08-28",
            "value": 74.5,
            "raw": {"period": "2026-08-28", "series": "RBRTE", "value": "74.50"},
        },
        {
            "source_id": "eia_petroleum_api",
            "observed_at": "2026-08-28",
            "value": 64.0,
            "raw": {"period": "2026-08-28", "series": "RWTC", "value": "64.00"},
        },
        {
            "source_id": "eia_petroleum_api",
            "observed_at": "2026-08-99",
            "value": 75.0,
            "raw": {"period": "2026-08-99", "series": "RBRTE", "value": "75.00"},
        },
        {
            "source_id": "unexpected_source",
            "observed_at": "2026-08-28",
            "value": 75.0,
            "raw": {"period": "2026-08-28", "series": "RBRTE", "value": "75.00"},
        },
    ]

    revisions = eia_brent_capture_revisions(
        rows,
        captured_at="2026-08-31T12:00:01+00:00",
        source_url="https://api.eia.gov/v2/petroleum/pri/spt/data/",
    )

    assert len(revisions) == 1
    assert revisions[0]["observed_at"] == "2026-08-28"
    assert revisions[0]["canonical_payload"] == rows[0]


def test_fred_row_to_observation_preserves_series_id_and_product() -> None:
    observation = fred_row_to_observation(
        {"date": "2026-06-18", "value": "6.7686"},
        source=source("fred_macro_api"),
        series={
            "series_id": "DEXCHUS",
            "label": "China / U.S. Foreign Exchange Rate",
            "unit": "cny_per_usd",
            "product": "fx",
        },
    )

    assert observation is not None
    assert observation["product"] == "fx"
    assert observation["indicator"] == "China / U.S. Foreign Exchange Rate (DEXCHUS)"
    assert observation["raw"]["series_id"] == "DEXCHUS"


def test_fred_row_to_observation_skips_dot_missing_value() -> None:
    observation = fred_row_to_observation(
        {"date": "2026-06-18", "value": "."},
        source=source("fred_macro_api"),
        series={"series_id": "SOFR", "label": "SOFR", "unit": "percent", "product": "macro"},
    )

    assert observation is None


def test_cfets_usd_cny_parity_preserves_direct_quote_and_capture_facts() -> None:
    observation = cfets_cny_parity_to_observation(
        {
            "data": {"lastDate": "2026-08-05 9:15"},
            "records": [
                {"vrtEName": "EUR/CNY", "price": "7.8059"},
                {"vrtEName": "USD/CNY", "price": "6.7889", "vrtName": "美元/人民币"},
            ],
        },
        source=source("cfets_cny_parity"),
        evidence_url="https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json",
        captured_at="2026-08-05T01:20:00+00:00",
    )

    assert observation["observed_at"] == "2026-08-05"
    assert observation["value"] == 6.7889
    assert observation["unit"] == "cny_per_usd"
    assert observation["raw"]["pair"] == "USD/CNY"
    assert observation["raw"]["captured_at"] == "2026-08-05T01:20:00+00:00"
    assert cfets_published_at("2026-08-05 9:15") == "2026-08-05T09:15:00+08:00"


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"data": {"lastDate": "bad"}, "records": []}, "cfets_last_date_invalid"),
        ({"data": {"lastDate": "2026-08-05 9:15"}, "records": []}, "cfets_usd_cny_record_missing"),
        (
            {"data": {"lastDate": "2026-08-05 9:15"}, "records": [{"vrtEName": "USD/CNY", "price": "0"}]},
            "cfets_usd_cny_price_invalid",
        ),
    ],
)
def test_cfets_usd_cny_parity_rejects_unprovable_payloads(payload: dict[str, object], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        cfets_cny_parity_to_observation(
            payload,
            source=source("cfets_cny_parity"),
            evidence_url="https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json",
            captured_at="2026-08-05T01:20:00+00:00",
        )


def test_coalchina_cctd_daily_reference_requires_complete_price_and_time_evidence() -> None:
    detail_url = coalchina_cctd_bohai_rim_daily_detail_url(
        """
        <a href="/index.php?a=show&c=index&catid=33&id=1&m=content">CCTD环渤海动力煤现货参考价日评（2026年8月5日）</a>
        <a href="/index.php?a=show&c=index&catid=33&id=2&m=content">CCTD环渤海动力煤现货参考价日评（2026年8月6日）</a>
        """
    )
    assert detail_url == "https://www.coalchina.org.cn/index.php?a=show&c=index&catid=33&id=2&m=content"

    observation = coalchina_cctd_bohai_rim_daily_to_observation(
        """
        <h1>CCTD环渤海动力煤现货参考价日评（2026年8月6日）</h1>
        <p>发布时间：2026-08-06 15:00:00</p>
        <p>5500K、5000K、4500K三个规格品分别收于734、652、563元/吨。</p>
        """,
        source=source("coalchina_cctd_bohai_rim_5500_daily_reference"),
        evidence_url=detail_url,
        captured_at="2026-08-06T08:01:00+00:00",
    )

    assert observation["observed_at"] == "2026-08-06"
    assert observation["value"] == 734.0
    assert observation["unit"] == "CNY/mt"
    assert observation["raw"]["market"] == "Bohai_rim_ports"
    assert observation["raw"]["published_at"] == "2026-08-06T15:00:00+08:00"


@pytest.mark.parametrize(
    "content,error",
    [
        ("<h1>CCTD环渤海动力煤现货参考价日评（2026年8月6日）</h1>", "published_at_missing"),
        ("发布时间：2026-08-06 15:00:00", "date_missing"),
        (
            "<h1>CCTD环渤海动力煤现货参考价日评（2026年8月6日）</h1>发布时间：2026-08-06 15:00:00",
            "5500_price_missing",
        ),
    ],
)
def test_coalchina_cctd_daily_reference_rejects_incomplete_evidence(content: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        coalchina_cctd_bohai_rim_daily_to_observation(
            content,
            source=source("coalchina_cctd_bohai_rim_5500_daily_reference"),
            evidence_url="https://www.coalchina.org.cn/example",
            captured_at="2026-08-06T08:01:00+00:00",
        )


def test_coalchina_fetcher_creates_candidate_with_immutable_capture_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    detail_url = "https://www.coalchina.org.cn/index.php?a=show&c=index&catid=33&id=2&m=content"

    class FakeResponse:
        def __init__(self, text: str) -> None:
            self.text = text
            self.content = text.encode("utf-8")
            self.headers = {"content-type": "text/html"}

        def raise_for_status(self) -> None:
            return None

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, url: str, **_kwargs: object) -> FakeResponse:
            if url == fetchers_module.COALCHINA_CCTD_BOHAI_RIM_DAILY_INDEX_URL:
                return FakeResponse(
                    '<a href="/index.php?a=show&c=index&catid=33&id=2&m=content">'
                    "CCTD环渤海动力煤现货参考价日评（2026年8月6日）</a>"
                )
            assert url == detail_url
            return FakeResponse(
                "<h1>CCTD环渤海动力煤现货参考价日评（2026年8月6日）</h1>"
                "发布时间：2026-08-06 15:00:00"
                "5500K、5000K、4500K三个规格品分别收于734、652、563元/吨。"
            )

    monkeypatch.setattr(fetchers_module.httpx, "AsyncClient", lambda **_kwargs: FakeAsyncClient())
    monkeypatch.setattr(type(fetchers_module.settings), "outbound_url_allowed", lambda _self, _url: True)
    result = asyncio.run(Fetcher().fetch(source("coalchina_cctd_bohai_rim_5500_daily_reference")))

    assert result.status == "ok"
    assert result.observations[0]["value"] == 734.0
    assert result.capture_revisions[0]["semantic_series_id"] == "coal.benchmark.unresolved.assessment.cny_mt"
    assert result.capture_revisions[0]["source_url"] == detail_url
    assert len(str(result.capture_revisions[0]["raw_sha256"])) == 64


def test_coalchina_candidate_can_be_registered_before_its_runtime_host_is_allowed() -> None:
    source_ids = {item.source_id for item in load_sources()}

    assert "coalchina_cctd_bohai_rim_5500_daily_reference" in source_ids


def test_sunsirs_mx_east_china_candidate_requires_dated_market_and_range() -> None:
    observation = sunsirs_mx_east_china_daily_to_observation(
        """
        <h1>8月6日华东地区二甲苯市场价格下调</h1>
        <p>发布时间：2026-08-06 14:10</p>
        <p>华东地区二甲苯市场价格在5790-5810元/吨。</p>
        """,
        source=source("sunsirs_mx_east_china_daily_assessment"),
        evidence_url="https://www.100ppi.com/news/detail-20260806-6022854.html",
        captured_at="2026-08-06T08:15:00+00:00",
    )

    assert observation["observed_at"] == "2026-08-06"
    assert observation["value"] == 5800.0
    assert observation["unit"] == "CNY/mt"
    assert observation["raw"]["market"] == "East_China"
    assert observation["raw"]["published_at"] == "2026-08-06T14:10:00+08:00"


@pytest.mark.parametrize(
    ("content", "error"),
    [
        ("发布时间：2026-08-06 14:10 华东市场5790-5810元/吨", "title_missing"),
        ("8月6日华东地区二甲苯市场价格下调 华东市场5790-5810元/吨", "published_at_missing"),
        ("8月6日华东地区二甲苯市场价格下调 发布时间：2026-08-06 14:10", "range_missing"),
        (
            "8月5日华东地区二甲苯市场价格下调 发布时间：2026-08-06 14:10 华东市场5790-5810元/吨",
            "observed_published_date_mismatch",
        ),
    ],
)
def test_sunsirs_mx_east_china_candidate_rejects_incomplete_evidence(content: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        sunsirs_mx_east_china_daily_to_observation(
            content,
            source=source("sunsirs_mx_east_china_daily_assessment"),
            evidence_url="https://www.100ppi.com/news/detail-20260806-6022854.html",
            captured_at="2026-08-06T08:15:00+00:00",
        )


def test_sunsirs_mx_east_china_candidate_is_not_http_fetched_past_browser_verification() -> None:
    manual_source = source("sunsirs_mx_east_china_daily_assessment").model_copy(update={"crawl_type": "manual_form"})
    result = asyncio.run(Fetcher().fetch(manual_source))

    assert result.status == "unsupported_crawl_type"


def test_sunsirs_mx_east_china_candidate_can_be_registered_before_its_runtime_host_is_allowed() -> None:
    source_ids = {item.source_id for item in load_sources()}

    assert "sunsirs_mx_east_china_daily_assessment" in source_ids


def test_sunsirs_mx_east_china_detail_link_uses_latest_dated_matching_article() -> None:
    detail_url = sunsirs_mx_east_china_daily_detail_url(
        '<a href="/news/detail-20260804-6022852.html">8月4日华东地区二甲苯市场价格上调</a>'
        '<a href="/news/detail-20260806-6022854.html">8月6日华东地区二甲苯市场价格下调</a>'
    )

    assert detail_url == "https://www.100ppi.com/news/detail-20260806-6022854.html"


def test_cftc_zip_to_observations_extracts_petroleum_contract_metrics() -> None:
    content = cftc_zip(
        [
            {
                "Market_and_Exchange_Names": "WTI FINANCIAL CRUDE OIL - NEW YORK MERCANTILE EXCHANGE",
                "Report_Date_as_YYYY-MM-DD": "2026-06-16",
                "CFTC_Contract_Market_Code": "06765A",
                "CFTC_Commodity_Code": "067",
                "Open_Interest_All": "1000",
                "M_Money_Positions_Long_All": "400",
                "M_Money_Positions_Short_All": "150",
                "Prod_Merc_Positions_Long_All": "200",
                "Prod_Merc_Positions_Short_All": "450",
            },
            {
                "Market_and_Exchange_Names": "USD Malaysian Crude Palm Oil C - CHICAGO MERCANTILE EXCHANGE",
                "Report_Date_as_YYYY-MM-DD": "2026-06-16",
                "Open_Interest_All": "999",
            },
        ]
    )

    observations = cftc_zip_to_observations(
        content,
        source=source("cftc_cot_petroleum"),
        evidence_url="https://www.cftc.gov/files/dea/history/fut_disagg_txt_2026.zip",
    )

    indicators = {str(item["indicator"]).split(" - ", 1)[0]: item for item in observations}
    assert indicators["CFTC COT open interest"]["value"] == 1000
    assert indicators["CFTC COT managed money net"]["value"] == 250
    assert indicators["CFTC COT producer merchant net"]["value"] == -250
    assert all(item["product"] == "crude_oil" for item in observations)


def test_cftc_market_filter_excludes_palm_oil_false_positive() -> None:
    assert is_cftc_petroleum_market("BRENT LAST DAY - NEW YORK MERCANTILE EXCHANGE")
    assert not is_cftc_petroleum_market("USD Malaysian Crude Palm Oil C - CHICAGO MERCANTILE EXCHANGE")


def cftc_zip(rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    fieldnames = sorted({key for row in rows for key in row})
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("f_year.txt", buffer.getvalue())
    return output.getvalue()

from __future__ import annotations

import asyncio

from app import textile_price_history as textile_price_history_module
from app.models import SourceConfig
from app.textile_price_history import fetch_tnc_polyester_history, parse_tnc_polyester_history


def _html(label: str, date: str, value: str = "8,600") -> str:
    return (
        "<table><tr><th>名称</th><th>价格</th><th>单位</th><th>涨跌</th><th>报价日期</th></tr>"
        f"<tr><td>{label}</td><td>{value}</td><td>元/吨</td><td>2.31%</td><td>{date}</td></tr></table>"
    )


def _source() -> SourceConfig:
    return SourceConfig(
        source_id="tnc_polyester_history",
        source_name="TNC",
        tier="B",
        category="polyester_public_assessment",
        url="https://www.tnc.com.cn/market/average-price.html",
        crawl_type="static_html",
        auth_type="public_personal_reuse",
        frequency="published_day",
        products=["poy", "dty"],
        freshness_sla_minutes=2880,
        license_note="public",
        reliability_score=0.82,
    )


def test_tnc_parser_preserves_exact_label_date_unit_and_lineage() -> None:
    rows = parse_tnc_polyester_history(
        _html("涤纶POY", "2026-08-18"),
        product="poy",
        evidence_url="https://www.tnc.com.cn/market/average-price-d92.html",
        captured_at="2026-08-31T10:00:00Z",
        raw_sha256="a" * 64,
    )

    assert rows == [
        {
            "source_id": "tnc_polyester_history",
            "observed_at": "2026-08-18",
            "indicator": "涤纶POY public recent average",
            "product": "poy",
            "value": 8600.0,
            "unit": "CNY/mt",
            "frequency": "published_day",
            "region": "China polyester public assessment",
            "evidence_url": "https://www.tnc.com.cn/market/average-price-d92.html",
            "notes": "全球纺织网公开近期行情均价；非逐笔成交价；按页面原始日期和单位保存。",
            "raw": {
                "captured_at": "2026-08-31T10:00:00Z",
                "change_text": "2.31%",
                "label": "涤纶POY",
                "page_url": "https://www.tnc.com.cn/market/average-price-d92.html",
                "raw_sha256": "a" * 64,
                "visibility_rule": (
                    "first successful system capture; historical page is not treated as live trade data"
                ),
            },
        }
    ]


def test_tnc_parser_rejects_wrong_unit() -> None:
    html = _html("涤纶DTY", "2026-08-18").replace("元/吨", "美元/桶")
    try:
        parse_tnc_polyester_history(
            html,
            product="dty",
            evidence_url="https://www.tnc.com.cn/market/average-price-d94.html",
            captured_at="2026-08-31T10:00:00Z",
        )
    except ValueError as error:
        assert str(error) == "tnc_row_contract_invalid"
    else:
        raise AssertionError("wrong unit must fail closed")


def test_tnc_fetcher_backfills_both_products_and_stops_on_empty_page(monkeypatch) -> None:
    class Response:
        def __init__(self, text: str) -> None:
            self.text = text
            self.content = text.encode()

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self) -> None:
            self.urls: list[str] = []

        async def get(self, url: str, **_kwargs) -> Response:
            self.urls.append(url)
            if "d92" in url and "-p2" not in url:
                return Response(_html("涤纶POY", "2026-08-18"))
            if "d94" in url and "-p2" not in url:
                return Response(_html("涤纶DTY", "2026-08-18", "9,850"))
            return Response("<table></table>")

    client = Client()
    monkeypatch.setattr(
        type(textile_price_history_module.settings),
        "require_outbound_url_allowed",
        lambda _self, url: url,
    )
    result = asyncio.run(fetch_tnc_polyester_history(_source(), max_pages=3, client=client))

    assert len(result.observations) == 2
    assert {row["product"] for row in result.observations} == {"poy", "dty"}
    assert len(result.capture_revisions) == 2
    assert {row["semantic_series_id"] for row in result.capture_revisions} == {
        "poy.public.polyester_spot_assessment.cny_mt",
        "dty.public.polyester_spot_assessment.cny_mt",
    }
    assert all(row["visible_at"] == row["captured_at"] for row in result.capture_revisions)
    assert all(row["raw_sha256"] == row["canonical_payload"]["raw"]["raw_sha256"] for row in result.capture_revisions)
    assert len(client.urls) == 4


def test_incremental_poll_stops_on_overlap_but_keeps_revision_rows():
    import httpx
    calls = []
    def respond(request):
        calls.append(str(request.url))
        label = '涤纶POY' if 'd92' in str(request.url) else '涤纶DTY'
        html = ('<table><tr><td>' + label + '</td><td>9400</td><td>元/吨</td>'
                '<td>0</td><td>2026-09-14</td></tr><tr><td>' + label + '</td>'
                '<td>9300</td><td>元/吨</td><td>0</td><td>2026-09-11</td></tr></table>')
        return httpx.Response(200, text=html)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await fetch_tnc_polyester_history(_source(), client=client,
                known_latest_dates={'poy': '2026-09-11', 'dty': '2026-09-11'})
    result = asyncio.run(run())
    assert len(calls) == 2 and len(result.observations) == 4
    assert len(result.capture_revisions) == 4

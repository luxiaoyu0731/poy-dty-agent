from __future__ import annotations

from app.public_source_adapters import (
    build_ofac_delta_events,
    comtrade_rows_to_observations,
    latest_comtrade_period,
    ofac_download_host_allowed,
    parse_gacc_latest_major_import_url,
    parse_gacc_major_imports,
    parse_ofac_sdn_csv,
)


def test_ofac_redirect_allowlist_is_limited_to_expected_s3_hosts() -> None:
    assert ofac_download_host_allowed("wc2h-sls-prod-public-published.s3.us-gov-west-1.amazonaws.com")
    assert ofac_download_host_allowed("official-bucket.s3.amazonaws.com")
    assert not ofac_download_host_allowed("s3.us-gov-west-1.amazonaws.com.attacker.test")
    assert not ofac_download_host_allowed("example.com")


def test_gacc_latest_link_and_supported_rows_are_normalized() -> None:
    index_html = """
    <table><tr><td>（14）Major Import Commodities in Quantity and Value</td><td>
      <a href="/Statics/june.html">Jun.</a><a href="/Statics/july.html">Jul.</a>
    </td></tr></table>
    """
    detail_html = """
    <table>
      <tr><td>（14）Major Import Commodities in Quantity and Value,7.2026</td></tr>
      <tr><td>Coal and lignite</td><td>10000T</td><td>4,273</td><td>4,354,503</td>
          <td>26,811</td><td>22,473,996</td><td>20.3</td><td>83.9</td></tr>
      <tr><td>Crude petroleum oils</td><td>10000T</td><td>3,573</td><td>22,782,435</td>
          <td>28,333</td><td>173,938,630</td><td>-24.3</td><td>-5.2</td></tr>
      <tr><td>Xylenes</td><td>10000T</td><td>52</td><td>558,619</td>
          <td>504</td><td>5,331,280</td><td>-35.0</td><td>-16.9</td></tr>
    </table>
    """

    url = parse_gacc_latest_major_import_url(index_html)
    observations = parse_gacc_major_imports(detail_html, evidence_url=url)

    assert url == "http://english.customs.gov.cn/Statics/july.html"
    assert len(observations) == 6
    crude_quantity = next(
        item for item in observations if item["product"] == "crude_oil" and item["unit"] == "metric_tonnes"
    )
    assert crude_quantity["observed_at"] == "2026-07-31"
    assert crude_quantity["value"] == 35_730_000
    xylenes = next(item for item in observations if item["product"] == "xylenes_broad")
    assert "broad GACC category" in str(xylenes["indicator"])
    assert "excluded from the pure PX formal series" in str(xylenes["notes"])
    assert xylenes["raw"]["hs_code"] == ""
    assert not [item for item in observations if item["product"] == "px"], (
        "broad xylenes must never be labelled as pure px"
    )


def test_ofac_baseline_stores_only_fingerprints_and_delta_is_event_scoped() -> None:
    csv_bytes = (
        b'123,"ENERGY SHIPPING CO","Entity","RUSSIA-EO14024",,,,,,,,"Oil tanker operator"\n'
        b'456,"UNRELATED PERSON","Individual","SDGT",,,,,,,,"Unrelated"\n'
    )
    total, relevant = parse_ofac_sdn_csv(csv_bytes)

    assert total == 2
    assert set(relevant) == {"123"}
    assert build_ofac_delta_events(relevant, None, occurred_at="2026-08-31T00:00:00+00:00", snapshot_sha256="a") == []

    events = build_ofac_delta_events(
        relevant,
        {"relevant_records": {}},
        occurred_at="2026-08-31T00:00:00+00:00",
        snapshot_sha256="b" * 64,
    )
    assert len(events) == 1
    assert events[0]["event_type"] == "sanctions_update"
    assert events[0]["requires_human_review"] is True
    assert "address" not in events[0]["raw"]


def test_comtrade_world_and_partner_rows_preserve_hs_revision_and_broad_naphtha_scope() -> None:
    rows = [
        {
            "period": "202607",
            "cmdCode": "271012",
            "partnerCode": 0,
            "partnerDesc": "World",
            "reporterCode": 156,
            "flowCode": "M",
            "clCode": "H6",
            "netWgt": 1_500_000,
            "primaryValue": 900_000,
            "isReported": True,
        },
        {
            "period": "202607",
            "cmdCode": "290243",
            "partnerCode": 410,
            "partnerDesc": "Republic of Korea",
            "reporterCode": 156,
            "flowCode": "M",
            "clCode": "H6",
            "netWgt": 2_000_000,
            "primaryValue": 1_700_000,
        },
    ]

    observations = comtrade_rows_to_observations(rows, evidence_url="https://comtradeapi.un.org/data/v1/get")

    assert len(observations) == 4
    naphtha = next(item for item in observations if item["product"] == "naphtha")
    assert "broad naphtha proxy" in str(naphtha["indicator"])
    assert naphtha["raw"]["classification_code"] == "H6"
    assert naphtha["observed_at"] == "2026-07-31"
    px_quantity = next(item for item in observations if item["product"] == "px" and item["unit"] == "metric_tonnes")
    assert px_quantity["value"] == 2000


def test_comtrade_availability_selects_latest_complete_period() -> None:
    rows = [
        {"period": 202312, "classificationCode": "H6"},
        {"period": 202412, "classificationCode": "H6"},
        {"period": "invalid", "classificationCode": "H6"},
    ]

    assert latest_comtrade_period(rows) == "202412"

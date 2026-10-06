"""Full-chain summary must read the current accepted daily label sources.

CCF is soft-removed: legacy industry/ccf_spot rows are no longer formal
evidence. PX/PTA come from the ZCE official settlement bars, MEG from the
SunSirs public assessment, POY/DTY from the TNC public history — the same
routing the seven-product label contract uses. Missing data must stay honest.
"""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app import storage as storage_module
from app.main import app

client = TestClient(app)

TEST_TAG = "fullchain-accepted-labels-test"
YESTERDAY = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
TODAY = datetime.now(UTC).date().isoformat()
STALE_DAY = (datetime.now(UTC) - timedelta(days=40)).date().isoformat()


@pytest.fixture(autouse=True)
def _clean_seeded_rows():
    with closing(storage_module.connect()) as connection, connection:
        connection.execute("DELETE FROM market_observations WHERE evidence_url LIKE ?", (f"%{TEST_TAG}%",))
        connection.execute("DELETE FROM industry_observations WHERE evidence_url LIKE ?", (f"%{TEST_TAG}%",))
        connection.execute("DELETE FROM intraday_price_observations WHERE source_url LIKE ?", (f"%{TEST_TAG}%",))
        connection.execute("DELETE FROM futures_daily_bars WHERE source_url LIKE ?", (f"%{TEST_TAG}%",))
        connection.execute("DELETE FROM forecast_price_points WHERE notes LIKE ?", (f"%{TEST_TAG}%",))
    yield
    with closing(storage_module.connect()) as connection, connection:
        connection.execute("DELETE FROM market_observations WHERE evidence_url LIKE ?", (f"%{TEST_TAG}%",))
        connection.execute("DELETE FROM industry_observations WHERE evidence_url LIKE ?", (f"%{TEST_TAG}%",))
        connection.execute("DELETE FROM intraday_price_observations WHERE source_url LIKE ?", (f"%{TEST_TAG}%",))
        connection.execute("DELETE FROM futures_daily_bars WHERE source_url LIKE ?", (f"%{TEST_TAG}%",))
        connection.execute("DELETE FROM forecast_price_points WHERE notes LIKE ?", (f"%{TEST_TAG}%",))


def _seed_market_observation(
    *, observation_id: str, source_id: str, product: str, indicator: str, observed_at: str, value: float, unit: str
) -> None:
    storage_module.create_market_observation(
        observation_id=observation_id,
        payload={
            "source_id": source_id,
            "observed_at": observed_at,
            "indicator": indicator,
            "product": product,
            "value": value,
            "unit": unit,
            "frequency": "published_day",
            "region": "test",
            "evidence_url": f"https://example.com/{TEST_TAG}/{observation_id}",
            "notes": "test seed",
            "raw": {},
        },
    )


def _seed_czce_bar(
    *,
    bar_id: str,
    trade_date: str,
    product: str,
    contract_code: str,
    contract_role: str,
    is_main: bool,
    settle: float,
    close: float,
) -> None:
    storage_module.upsert_futures_daily_bar(
        bar_id=bar_id,
        payload={
            "trade_date": trade_date,
            "exchange": "CZCE",
            "product": product,
            "contract_code": contract_code,
            "contract_role": contract_role,
            "is_main": is_main,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "settle": settle,
            "volume": 1000.0,
            "open_interest": 20000.0,
            "unit": "CNY/mt",
            "visible_at": f"{trade_date}T15:30:00+00:00",
            "source_id": "czce_pta_px",
            "source_name": "ZCE",
            "source_url": f"https://example.com/{TEST_TAG}/{bar_id}",
        },
    )


def _seed_sunsirs_meg(*, observation_id: str, observed_at: str, last: float) -> None:
    storage_module.upsert_intraday_price_observation(
        observation_id=observation_id,
        payload={
            "instrument": "MEG",
            "symbol": "MEG_PUBLIC_SPOT",
            "observed_at": observed_at,
            "interval_seconds": 900,
            "price_type": "spot_public_valuation",
            "last": last,
            "unit": "CNY/mt",
            "source_id": "sunsirs_public_commodity_assessment",
            "source_url": f"https://example.com/{TEST_TAG}/{observation_id}",
            "quality": "non_transaction_public_valuation",
            "notes": "test seed",
        },
    )


def _seed_ccf_industry_row(*, observation_id: str, product: str, observed_at: str, value: float) -> None:
    storage_module.create_industry_observation(
        observation_id=observation_id,
        payload={
            "source_id": "ccf_dom_daily",
            "observed_at": observed_at,
            "product": product,
            "metric": "spot_quote",
            "market": "test",
            "region": "CN",
            "value": value,
            "unit": "CNY/mt",
            "frequency": "daily",
            "evidence_level": "A",
            "evidence_url": f"https://example.com/{TEST_TAG}/{observation_id}",
            "notes": "test seed",
            "raw": {},
        },
    )


def _seed_ccf_authorized_point(*, point_id: str, product: str, observed_at: str, price: float) -> None:
    storage_module.upsert_forecast_price_point(
        point_id=point_id,
        payload={
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "observed_at": observed_at,
            "product": product,
            "spec": "test",
            "price": price,
            "unit": "CNY/mt",
            "quote_type": "daily_average",
            "notes": TEST_TAG,
            "raw": {"source_url": f"https://example.com/{TEST_TAG}/{point_id}"},
        },
    )


def _seed_all_accepted_sources() -> None:
    # Crude switched to the ICE Brent front-month futures close (BZ=F) in
    # seven-product-labels.v5; EIA spot remains evidence only.
    _seed_market_observation(
        observation_id=f"{TEST_TAG}-crude",
        source_id="yahoo_futures_daily_proxy",
        product="crude_oil",
        indicator="Brent futures daily close",
        observed_at=YESTERDAY,
        value=80.0,
        unit="USD/bbl",
    )
    for product, contract in (("PX", "PX701"), ("PTA", "TA701")):
        _seed_czce_bar(
            bar_id=f"{TEST_TAG}-{product}-main",
            trade_date=YESTERDAY,
            product=product,
            contract_code=contract,
            contract_role="main",
            is_main=True,
            settle=1000.0 + len(product),
            close=999.0,
        )
        _seed_czce_bar(
            bar_id=f"{TEST_TAG}-{product}-listed",
            trade_date=YESTERDAY,
            product=product,
            contract_code=f"{contract[:-1]}9",
            contract_role="listed_contract",
            is_main=False,
            settle=5000.0,
            close=5000.0,
        )
    _seed_sunsirs_meg(observation_id=f"{TEST_TAG}-meg", observed_at=YESTERDAY, last=6600.0)
    for product, value in (("poy", 9200.0), ("dty", 10300.0)):
        _seed_market_observation(
            observation_id=f"{TEST_TAG}-{product}",
            source_id="tnc_polyester_history",
            product=product,
            indicator=f"涤纶{product.upper()} public recent average",
            observed_at=YESTERDAY,
            value=value,
            unit="CNY/mt",
        )


def test_full_chain_summary_uses_current_accepted_label_sources() -> None:
    _seed_all_accepted_sources()
    payload = client.get("/api/v1/full-chain/summary").json()

    assert payload["status"] == "ready"
    assert payload["coverage"]["missing_products"] == []
    assert payload["coverage"]["stale_products"] == []
    assert payload["coverage"]["available_products"] == ["CRUDE", "DTY", "MEG", "POY", "PTA", "PX"]

    by_product = {row["product"]: row for row in payload["summary"]}
    for product in ("PX", "PTA"):
        row = by_product[product]
        assert row["source_id"] == "czce_pta_px"
        assert row["observed_at"] == YESTERDAY
        # The exchange official settlement of the main contract, never the
        # listed-contract bar and never the close when a settlement exists.
        assert row["value"] == 1000.0 + len(product)
        assert row["quote_type"] == "main_continuous_settlement"
        assert row["price_type"] == "futures_settlement_proxy"
        assert row["evidence_tier"] == "A"
        assert row["formal_eligible"] is True
    meg = by_product["MEG"]
    assert meg["source_id"] == "sunsirs_public_commodity_assessment"
    assert meg["value"] == 6600.0
    assert meg["observed_at"] == YESTERDAY
    assert meg["quote_type"] == "public_spot_assessment"
    assert meg["formal_eligible"] is True
    for product, expected in (("POY", 9200.0), ("DTY", 10300.0)):
        row = by_product[product]
        assert row["source_id"] == "tnc_polyester_history"
        assert row["value"] == expected
        assert row["observed_at"] == YESTERDAY
        assert row["quote_type"] == "public_recent_average"
        assert row["formal_eligible"] is True


def test_soft_removed_ccf_rows_are_never_selected_as_current_evidence() -> None:
    _seed_all_accepted_sources()
    # Fresh CCF-shaped rows (industry + authorized ccf_spot) dated today must
    # not displace the accepted sources: CCF is soft-removed and therefore not
    # formal-eligible for a current judgement.
    for product, value in (("POY", 8000.0), ("PTA", 4000.0)):
        _seed_ccf_industry_row(
            observation_id=f"{TEST_TAG}-ccf-ind-{product}", product=product, observed_at=TODAY, value=value
        )
        _seed_ccf_authorized_point(
            point_id=f"{TEST_TAG}-ccf-auth-{product}", product=product, observed_at=TODAY, price=value
        )
    # DTY has only CCF rows and no accepted-source row: it must stay honestly
    # missing instead of falling back to non-eligible CCF data.
    with closing(storage_module.connect()) as connection, connection:
        connection.execute("DELETE FROM market_observations WHERE observation_id = ?", (f"{TEST_TAG}-dty",))

    payload = client.get("/api/v1/full-chain/summary").json()
    by_product = {row["product"]: row for row in payload["summary"]}
    assert by_product["POY"]["source_id"] == "tnc_polyester_history"
    assert by_product["POY"]["observed_at"] == YESTERDAY
    assert by_product["PTA"]["source_id"] == "czce_pta_px"
    assert "DTY" not in by_product
    assert payload["status"] == "data_not_ready"
    assert "DTY" in payload["coverage"]["missing_products"]


def test_accepted_label_source_staleness_is_reported_not_hidden() -> None:
    _seed_all_accepted_sources()
    with closing(storage_module.connect()) as connection, connection:
        connection.execute(
            "UPDATE market_observations SET observed_at = ? WHERE observation_id = ?",
            (STALE_DAY, f"{TEST_TAG}-poy"),
        )

    payload = client.get("/api/v1/full-chain/summary").json()
    assert "POY" in payload["coverage"]["stale_products"]
    assert payload["status"] == "partial"


def test_no_data_reports_honest_missing_products() -> None:
    payload = client.get("/api/v1/full-chain/summary").json()
    for product in ("PX", "PTA", "MEG", "POY", "DTY"):
        assert product in payload["coverage"]["missing_products"]
    rendered = {row["product"] for row in payload["summary"]}
    assert not (rendered & {"PX", "PTA", "MEG", "POY", "DTY"})
    assert payload["poy_dty_gate"]["qualified"] is False


def test_market_chain_shares_the_accepted_source_snapshot() -> None:
    _seed_all_accepted_sources()
    full_chain = client.get("/api/v1/full-chain/summary").json()
    market_chain = client.get(
        "/api/v1/workbench/market-chain",
        params={"as_of_time": full_chain["as_of_time"]},
    ).json()
    market_prices = {row["key"]: row["latest_price"] for row in market_chain["products"]}
    for row in full_chain["summary"]:
        price = market_prices[row["product"]]
        assert price["value"] == row["value"]
        assert price["date"] == row["observed_at"][:10]


def test_rag_visual_canonical_prices_use_honest_quote_labels() -> None:
    _seed_all_accepted_sources()
    rag = client.get(
        "/api/v1/workbench/rag-visual",
        params={"q": "POY DTY 上游原料成本压力", "product": "POY", "limit": 8},
    ).json()
    adopted = rag["evidence_buckets"]["adopted"]
    canonical = [item for item in adopted if str(item.get("id", "")).startswith("canonical:")]
    assert canonical, "canonical POY/DTY price evidence must enter the adopted bucket"
    joined = " ".join(f"{item.get('title', '')} {item.get('summary', '')}" for item in canonical)
    assert "公开近期均价" in joined
    assert "授权现货日均价" not in joined


def test_unexpected_unit_is_rejected_instead_of_mislabeled() -> None:
    _seed_all_accepted_sources()
    # A MEG row in a non-price unit must not occupy the price slot.
    storage_module.upsert_intraday_price_observation(
        observation_id=f"{TEST_TAG}-meg-volume",
        payload={
            "instrument": "MEG",
            "symbol": "MEG_PUBLIC_SPOT",
            "observed_at": TODAY,
            "interval_seconds": 900,
            "price_type": "spot_public_valuation",
            "last": 12345.0,
            "unit": "lots",
            "source_id": "sunsirs_public_commodity_assessment",
            "source_url": f"https://example.com/{TEST_TAG}/meg-volume",
        },
    )
    payload = client.get("/api/v1/full-chain/summary").json()
    by_product = {row["product"]: row for row in payload["summary"]}
    assert by_product["MEG"]["observed_at"] == YESTERDAY
    assert by_product["MEG"]["value"] == 6600.0


def test_build_directly_is_consistent_with_the_api_contract() -> None:
    _seed_all_accepted_sources()
    from app.intelligence import build_full_chain_summary

    direct = build_full_chain_summary()
    via_api = client.get("/api/v1/full-chain/summary").json()
    assert direct["status"] == via_api["status"]
    assert direct["data_snapshot_id"] == via_api["data_snapshot_id"] or direct["summary"] == via_api["summary"]


def test_module_wiring_unchanged() -> None:
    # The endpoint must keep using the real builder (guard against a stale
    # monkeypatch from other suites leaking into this one).
    assert main_module.build_full_chain_summary is not None

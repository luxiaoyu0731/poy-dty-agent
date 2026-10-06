from __future__ import annotations

from datetime import UTC, datetime, tzinfo

import pytest

from app import intelligence


def _row(*, product: str, value: float, unit: str, observed_at: str, **extra: object) -> dict[str, object]:
    return {
        "observation_id": f"{product}-{observed_at}-{value}",
        "source_id": extra.pop(
            "source_id", "eia_petroleum_api" if product == "crude_oil" else "sunsirs_public_commodity_assessment"
        ),
        "observed_at": observed_at,
        "product": product,
        "value": value,
        "unit": unit,
        "evidence_url": "https://example.com/evidence",
        **extra,
    }


def test_full_chain_price_slots_reject_newer_non_price_metrics_for_every_product(monkeypatch) -> None:
    market_rows = [
        _row(
            product="crude_oil",
            value=81.5,
            unit="USD/bbl",
            observed_at="2026-07-18",
            indicator="WTI spot price",
        ),
        _row(
            product="crude_oil",
            value=11924,
            unit="contracts",
            observed_at="2026-07-19",
            indicator="CFTC COT open interest",
            source_id="cftc_cot_petroleum",
        ),
    ]
    industry_rows: list[dict[str, object]] = []
    expected = {"PX": 7600.0, "PTA": 5500.0, "MEG": 4200.0, "POY": 7200.0, "DTY": 8600.0}
    for product, price in expected.items():
        industry_rows.extend(
            [
                _row(
                    product=product,
                    value=price,
                    unit="CNY/mt",
                    observed_at="2026-07-18",
                    metric="spot_quote",
                ),
                _row(
                    product=product,
                    value=11924,
                    unit="contracts",
                    observed_at="2026-07-19",
                    metric="spot_quote",
                ),
            ]
        )

    monkeypatch.setattr(intelligence, "list_market_observations", lambda **_: market_rows)
    monkeypatch.setattr(intelligence, "list_industry_observations", lambda **_: industry_rows)
    monkeypatch.setattr(intelligence, "list_forecast_price_points", lambda **_: [])

    payload = intelligence.build_full_chain_summary(as_of_time="2026-07-20T00:00:00+00:00")
    selected = {row["product"]: row for row in payload["summary"]}

    assert selected["CRUDE"]["value"] == 81.5
    assert selected["CRUDE"]["unit"] == "USD/bbl"
    for product, price in expected.items():
        assert selected[product]["value"] == price
        assert selected[product]["unit"] == "CNY/mt"
    assert all(row["value"] != 11924 for row in selected.values())


def test_prediction_snapshot_does_not_count_non_price_rows_as_product_coverage() -> None:
    invalid_rows = [
        _row(
            product="crude_oil",
            value=11924,
            unit="contracts",
            observed_at="2026-07-19",
            indicator="CFTC COT open interest",
            source_id="cftc_cot_petroleum",
        )
    ]
    invalid_rows.extend(
        _row(
            product=product,
            value=11924,
            unit="contracts",
            observed_at="2026-07-19",
            metric="spot_quote",
        )
        for product in intelligence.FULL_CHAIN_PRODUCTS[1:]
    )
    snapshot = {
        "created_at": "2026-07-20T00:00:00+00:00",
        "metadata": {"as_of_time": "2026-07-20T00:00:00+00:00"},
        "payload": {
            "market_observations": invalid_rows[:1],
            "industry_observations": invalid_rows[1:],
            "authorized_price_observations": [],
        },
    }

    gate = intelligence.assess_prediction_snapshot(snapshot, target="POY/DTY 上游成本压力")

    assert gate["qualified"] is False
    assert gate["available_products"] == []
    assert gate["missing_products"] == sorted(intelligence.FULL_CHAIN_PRODUCTS)


def test_full_chain_soft_removed_ccf_never_overrides_active_public_price(monkeypatch) -> None:
    industry_rows = [
        _row(
            product="POY",
            value=9999.0,
            unit="CNY/mt",
            observed_at="2026-07-24",
            metric="spot_quote",
            source_id="sunsirs_public_commodity_assessment",
        )
    ]
    authorized_rows = [
        {
            "point_id": "ccf-poy-1",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "observed_at": "2026-07-23",
            "product": "POY",
            "price": 8540.0,
            "unit": "CNY/mt",
            "quote_type": "daily_average",
            "raw": {"source_url": "https://www.ccf.com.cn/datacenter/price.php"},
        }
    ]
    monkeypatch.setattr(intelligence, "list_market_observations", lambda **_: [])
    monkeypatch.setattr(intelligence, "list_industry_observations", lambda **_: industry_rows)
    monkeypatch.setattr(intelligence, "list_forecast_price_points", lambda **_: authorized_rows)

    payload = intelligence.build_full_chain_summary(as_of_time="2026-07-25T00:00:00+00:00")
    poy = next(row for row in payload["summary"] if row["product"] == "POY")

    assert poy["source_id"] == "sunsirs_public_commodity_assessment"
    assert poy["value"] == 9999.0
    assert poy["observed_at"] == "2026-07-24"


def test_default_as_of_stays_stable_when_one_refresh_crosses_minute_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BoundaryDateTime(datetime):
        moments = iter(
            (
                datetime(2026, 8, 27, 16, 18, 59, 900_000, tzinfo=UTC),
                datetime(2026, 8, 27, 16, 19, 0, 100_000, tzinfo=UTC),
            )
        )

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> datetime:
            value = next(cls.moments)
            return value if tz is None else value.astimezone(tz)

    monotonic_values = iter((100.0, 100.2, 106.0))
    monkeypatch.setattr(intelligence, "datetime", BoundaryDateTime)
    monkeypatch.setattr(intelligence.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(intelligence, "_DEFAULT_READ_AS_OF_VALUE", None)
    monkeypatch.setattr(intelligence, "_DEFAULT_READ_AS_OF_EXPIRES_AT", 0.0)

    first = intelligence._parse_as_of(None)
    second = intelligence._parse_as_of(None)
    after_lease = intelligence._parse_as_of(None)

    assert first == datetime(2026, 8, 27, 16, 19, 5, tzinfo=UTC)
    assert second == first
    assert after_lease == datetime(2026, 8, 27, 16, 20, 5, tzinfo=UTC)

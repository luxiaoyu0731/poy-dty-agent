from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import intelligence, main, storage
from app.settings import settings


def _snapshot(*, extra_observed_at: object) -> dict[str, Any]:
    market_rows = [
        {
            "observation_id": "crude-current",
            "source_id": "eia_petroleum_api",
            "observed_at": "2026-07-20",
            "indicator": "WTI spot price",
            "product": "crude_oil",
            "value": 100.0,
            "unit": "USD/bbl",
        }
    ]
    industry_rows = [
        {
            "observation_id": f"{product.lower()}-current",
            "source_id": "sunsirs_public_commodity_assessment",
            "observed_at": "2026-07-20",
            "product": product,
            "metric": "spot_quote",
            "value": value,
            "unit": "CNY/mt",
        }
        for product, value in (("PX", 7600.0), ("PTA", 5500.0), ("MEG", 4200.0), ("POY", 7200.0), ("DTY", 8600.0))
    ]
    industry_rows.append(
        {
            "observation_id": "poy-formal-timestamp-probe",
            "source_id": "sunsirs_public_commodity_assessment",
            "observed_at": extra_observed_at,
            "product": "POY",
            "metric": "spot_quote",
            "value": 99999.0,
            "unit": "CNY/mt",
        }
    )
    return {
        "snapshot_id": "timestamp-gate-snapshot",
        "created_at": "2026-07-20T12:00:00+00:00",
        "metadata": {"as_of_time": "2026-07-20T12:00:00+00:00"},
        "payload": {
            "market_observations": market_rows,
            "industry_observations": industry_rows,
            "authorized_price_observations": [],
        },
    }


@pytest.mark.parametrize("valid_observed_at", ["2026-07-20", "2026-07-20T09:00:00+08:00"])
def test_prediction_snapshot_keeps_dg_valid_date_and_zoned_rfc3339(valid_observed_at: str) -> None:
    result = intelligence.assess_prediction_snapshot(
        _snapshot(extra_observed_at=valid_observed_at),
        target="POY/DTY 上游成本压力",
    )

    assert result["qualified"] is True
    assert result["reason"] == "qualified"


@pytest.mark.parametrize(
    ("invalid_observed_at", "failure"),
    [
        ("not-a-timestamp", "rfc3339_parse_failed"),
        ("2026-02-30", "date_parse_failed"),
        (178, "timestamp_type_invalid"),
        ("2026-07-20T09:00:00", "timezone_required"),
    ],
)
def test_prediction_snapshot_timestamp_ineligible_is_stable_and_non_throwing(
    invalid_observed_at: object,
    failure: str,
) -> None:
    result = intelligence.assess_prediction_snapshot(
        _snapshot(extra_observed_at=invalid_observed_at),
        target="POY/DTY 上游成本压力",
    )

    assert result["qualified"] is False
    assert result["reason"] == "snapshot_timestamp_ineligible"
    assert result["timestamp_ineligible"] == [
        {
            "field": "observed_at",
            "product": "POY",
            "source_id": "sunsirs_public_commodity_assessment",
            "failure": failure,
        }
    ]


def test_prediction_snapshot_invalid_as_of_time_fails_closed_without_exception() -> None:
    snapshot = _snapshot(extra_observed_at="2026-07-20")
    snapshot["metadata"]["as_of_time"] = "2026-07-20T12:00:00"

    result = intelligence.assess_prediction_snapshot(snapshot, target="POY/DTY 上游成本压力")

    assert result["qualified"] is False
    assert result["reason"] == "snapshot_timestamp_ineligible"
    assert result["timestamp_ineligible"] == [
        {
            "field": "as_of_time",
            "product": "",
            "source_id": "snapshot",
            "failure": "timezone_required",
        }
    ]


def test_prediction_api_rejects_timestamp_ineligible_snapshot_with_zero_ledger_write(monkeypatch) -> None:
    snapshot = _snapshot(extra_observed_at="2026-07-20T09:00:00")
    monkeypatch.setattr(main, "get_data_snapshot", lambda snapshot_id: snapshot)
    original_enforcement = settings.enforce_internal_token
    object.__setattr__(settings, "enforce_internal_token", False)
    client = TestClient(main.app)
    before = len(storage.list_prediction_ledger_records(limit=None))
    try:
        response = client.post(
            "/api/v1/predictions",
            json={
                "target": "POY/DTY 上游成本压力",
                "horizon": "7d",
                "direction": "中性",
                "confidence": 0.7,
                "rationale": "timestamp eligibility regression",
                "counter_evidence": "demand weakness",
                "source_status": "formal_timestamp_gate_test",
                "tags": ["timestamp-gate"],
                "data_snapshot_id": snapshot["snapshot_id"],
            },
        )
    finally:
        object.__setattr__(settings, "enforce_internal_token", original_enforcement)

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "formal_prediction_write_path_disabled"
    assert body["error"]["details"] == {}
    assert len(storage.list_prediction_ledger_records(limit=None)) == before

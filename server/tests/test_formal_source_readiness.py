from __future__ import annotations

import json

import pytest

from app.formal_source_readiness import (
    DOMESTIC_ASSESSMENT_SERIES_IDS,
    EXACT_MARKET_SERIES_IDS,
    FormalSourceReadinessError,
    build_unresolved_source_readiness,
    canonical_readiness_json,
    normalize_range_assessment,
)


def test_readiness_describes_exact_fourteen_series_without_approving_them() -> None:
    first = build_unresolved_source_readiness()
    second = build_unresolved_source_readiness()

    assert canonical_readiness_json(first) == canonical_readiness_json(second)
    assert first["production_manifest_changed"] is False
    assert first["summary"] == {"record_count": 14, "ready_count": 0, "blocked_count": 14}
    assert [record["series_id"] for record in first["records"]] == [
        *EXACT_MARKET_SERIES_IDS,
        *DOMESTIC_ASSESSMENT_SERIES_IDS,
    ]
    assert all(record["readiness_status"] == "blocked" for record in first["records"])
    assert all(record["observation_only"] is True for record in first["records"])
    assert all(record["evidence_bundle_emitted"] is False for record in first["records"])


def test_exact_market_candidates_do_not_substitute_eia_or_ine() -> None:
    records = {record["series_id"]: record for record in build_unresolved_source_readiness()["records"]}

    for series_id in EXACT_MARKET_SERIES_IDS:
        source_text = json.dumps(records[series_id]["source_contracts"], sort_keys=True).lower()
        assert "eia" not in source_text
        assert "ine.cn" not in source_text
        assert "akshare" not in source_text
        if series_id == "meg.dce.main.settlement.cny_mt":
            assert records[series_id]["blocked_reasons"] == ["source_removed"]
            assert records[series_id]["historical_only"] is True
            assert records[series_id]["source_contracts"] == []
        else:
            assert "roll_evidence_missing" in records[series_id]["blocked_reasons"]


@pytest.mark.parametrize(
    "series_id",
    [
        "naphtha.ccf.domestic.daily_assessment.cny_mt",
        "px.ccf.domestic.daily_assessment.cny_mt",
    ],
)
def test_ccf_international_quotes_remain_observation_only_market_mismatches(series_id: str) -> None:
    records = {record["series_id"]: record for record in build_unresolved_source_readiness()["records"]}

    record = records[series_id]
    assert record["blocked_reasons"] == ["source_removed"]
    assert record["historical_only"] is True
    assert record["historical_blocked_reasons"] == [
        "no_exact_source",
        "market_mismatch",
        "unit_currency_mismatch",
        "visibility_unproven",
    ]
    assert record["observation_only"] is True


def test_every_readiness_record_carries_governance_and_lineage_fields() -> None:
    for record in build_unresolved_source_readiness()["records"]:
        assert record["owner_role"] == "Data Ingestion Agent"
        assert record["contract_digest"]
        assert record["readiness_digest"]
        assert record["lineage_policy"] == {
            "capture_table": "source_capture_revisions",
            "point_binding": "capture_revision_id",
            "required_hashes": ["raw_sha256", "canonical_payload_hash"],
        }
        assert record["retention_and_revision_policy"]["retroactive_revision"] == (
            "conflict_until_separately_reviewed"
        )
        assert record["blocked_reasons"]


def test_range_assessment_preserves_bounds_and_marks_midpoint_as_derived() -> None:
    value = normalize_range_assessment(
        low=7_100,
        high=7_300,
        unit="CNY/mt",
        quote_type="public_spot_assessment",
    )

    assert value["price_low"] == 7_100
    assert value["price_high"] == 7_300
    assert value["derived_midpoint"] == 7_200
    assert value["midpoint_rule"] == "arithmetic_midpoint.v1"
    assert value["is_transaction_price"] is False
    assert value["range_digest"]


def test_range_midpoint_does_not_overflow_for_large_finite_bounds() -> None:
    value = normalize_range_assessment(
        low=1.7e308,
        high=1.7e308,
        unit="CNY/mt",
        quote_type="public_spot_assessment",
    )

    assert value["derived_midpoint"] == 1.7e308


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"low": 2, "high": 1, "unit": "CNY/mt", "quote_type": "public_spot_assessment"}, "range_bounds_reversed"),
        (
            {
                "low": float("nan"),
                "high": 1,
                "unit": "CNY/mt",
                "quote_type": "public_spot_assessment",
            },
            "range_bounds_must_be_finite",
        ),
        (
            {
                "low": 10**10_000,
                "high": 10**10_000,
                "unit": "CNY/mt",
                "quote_type": "public_spot_assessment",
            },
            "range_bounds_must_be_finite",
        ),
        ({"low": 1, "high": 2, "unit": " CNY/mt", "quote_type": "public_spot_assessment"}, "range_unit_missing"),
        ({"low": 1, "high": 2, "unit": "CNY/mt", "quote_type": "transaction"}, "range_quote_type_not_assessment"),
    ],
)
def test_range_assessment_rejects_unsafe_normalization(kwargs: dict[str, object], error: str) -> None:
    with pytest.raises(FormalSourceReadinessError, match=error):
        normalize_range_assessment(**kwargs)  # type: ignore[arg-type]

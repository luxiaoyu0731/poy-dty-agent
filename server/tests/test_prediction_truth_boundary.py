from __future__ import annotations

from datetime import UTC, datetime

from app.prediction_contract import enrich_prediction_record, is_legacy_directional_prediction_record


def test_legacy_directional_record_is_explicitly_non_formal() -> None:
    record = enrich_prediction_record(
        {
            "prediction_id": "legacy-1",
            "created_at": "2026-07-01T00:00:00+00:00",
            "target": "POY/DTY upstream cost pressure",
            "horizon": "7d",
            "direction": "中性",
            "confidence": 0.5,
            "rationale": "historical only",
            "counter_evidence": "",
            "source_status": "legacy",
            "tags": [],
            "data_snapshot_id": None,
            "review_status": "pending",
        },
        now=datetime(2026, 7, 2, tzinfo=UTC),
    )

    assert is_legacy_directional_prediction_record(record) is True
    assert record["record_kind"] == "legacy_scalar"
    assert record["governance_status"] == "legacy_unverified"
    assert record["formal_status"] == "historical_legacy_contract"
    assert record["formal_eligible"] is False

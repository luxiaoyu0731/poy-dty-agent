from pathlib import Path

import pytest
from server.scripts.rebuild_full_chain_row_audit import build_row_audit


def _row(prediction: str, actual: str, verdict: str, future: float | None = 1.0) -> dict:
    return {
        "date": "2026-01-02",
        "period": "2026_forward",
        "product": "PTA",
        "prediction_direction": prediction,
        "actual_direction": actual,
        "prior_change_pct": 1.0,
        "future_change_pct": future,
        "verdict": verdict,
        "source_id": "ccf_dom_daily",
    }


def test_rebuild_reconciles_rows_and_preserves_unknown_posterior_date() -> None:
    payload = {
        "scope": {"neutral_definition": "abs(change) < 0.35%"},
        "summary": {"total_rows": 3, "non_neutral_candidates": 2, "scored": 1},
        "rows": [
            _row("偏强", "偏强", "hit"),
            _row("偏弱", "震荡", "neutral_or_unscored", 0.2),
            _row("震荡", "偏强", "neutral_or_unscored"),
        ],
    }
    result = build_row_audit(payload, source_path=Path("legacy.json"), source_sha256="abc")

    assert result["invariants"] == {
        "denominator_unchanged": 3,
        "legacy_non_neutral_candidates": 2,
        "legacy_scored": 1,
        "reconciles": True,
    }
    assert result["reason_counts"]["unscored_reason"] == {
        "actual_next_observation_within_neutral_band": 1,
        "prediction_direction_neutral": 1,
    }
    assert result["rows"][1]["price_window"]["posterior_observation_date"] is None
    assert result["rows"][1]["source_row_refs"][0]["json_pointer"] == "/rows/1"


def test_rebuild_rejects_summary_that_does_not_match_rows() -> None:
    payload = {
        "scope": {},
        "summary": {"total_rows": 4, "non_neutral_candidates": 1, "scored": 1},
        "rows": [_row("偏强", "偏强", "hit")],
    }
    with pytest.raises(ValueError, match="does not reconcile"):
        build_row_audit(payload, source_path=Path("legacy.json"), source_sha256="abc")

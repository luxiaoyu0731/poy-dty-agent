from pathlib import Path

import pytest
from server.scripts.audit_full_chain_scoring_gap import build_audit


def test_build_audit_reconciles_4008_2538_gap_without_changing_denominator() -> None:
    payload = {
        "summary": {
            "total_rows": 4008,
            "non_neutral_candidates": 3488,
            "scored": 2538,
            "hit": 1407,
            "miss": 1131,
        }
    }
    audit = build_audit(payload, source_path=Path("backtest.json"), source_sha256="abc")

    assert audit["invariants"]["total_not_scored"] == 1470
    assert audit["invariants"]["gap_reconciles"] is True
    assert audit["proven_attribution"][0]["count"] == 520
    assert audit["proven_attribution"][1]["count"] == 950
    assert audit["safe_salvage"]["immediately_rescorable_from_aggregate"] == 0
    assert audit["formal_status"]["eligible"] is False


@pytest.mark.parametrize(
    "summary",
    [
        {"total_rows": 10, "non_neutral_candidates": 11, "scored": 5, "hit": 3, "miss": 2},
        {"total_rows": 10, "non_neutral_candidates": 8, "scored": 7, "hit": 3, "miss": 3},
    ],
)
def test_build_audit_rejects_inconsistent_aggregates(summary: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        build_audit({"summary": summary}, source_path=Path("backtest.json"), source_sha256="abc")

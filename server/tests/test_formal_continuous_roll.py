from __future__ import annotations

from copy import deepcopy

import pytest

from app.formal_continuous_roll import (
    MAX_ROLL_ROWS,
    MAX_TRADE_DAYS,
    FormalContinuousRollError,
    audit_frozen_continuous_roll,
    canonical_roll_json,
)

SERIES_ID = "pta.czce.main.settlement.cny_mt"


def _row(
    day: str,
    contract: str,
    delivery_month: str,
    *,
    oi: int,
    volume: int,
    settle: int = 5_800,
    visible_at: str | None = None,
    revision: str | None = None,
) -> dict[str, object]:
    token = f"{day}-{contract}-{revision or 'r1'}"
    return {
        "capture_revision_id": revision or token,
        "raw_sha256": "a" * 64,
        "canonical_payload_hash": "b" * 64,
        "trade_date": day,
        "exchange": "ZCE",
        "product": "TA",
        "contract_code": contract,
        "delivery_month": delivery_month,
        "settle": settle,
        "volume": volume,
        "open_interest": oi,
        "unit": "CNY/mt",
        "quote_type": "futures_settlement",
        "visible_at": visible_at or f"{day}T16:00:00+08:00",
        "source_id": "czce_pta_px",
    }


def _cutoffs(days: list[str]) -> dict[str, str]:
    return {day: f"{day}T18:00:00+08:00" for day in days}


def test_two_day_confirmation_switches_on_next_trade_day_across_holiday_gap() -> None:
    days = ["2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"]
    rows = [
        _row(days[0], "TA701", "2027-01", oi=120, volume=100),
        _row(days[0], "TA705", "2027-05", oi=100, volume=90),
        _row(days[1], "TA701", "2027-01", oi=120, volume=100),
        _row(days[1], "TA705", "2027-05", oi=130, volume=110),
        _row(days[2], "TA701", "2027-01", oi=120, volume=100),
        _row(days[2], "TA705", "2027-05", oi=140, volume=120),
        _row(days[3], "TA701", "2027-01", oi=115, volume=95),
        _row(days[3], "TA705", "2027-05", oi=145, volume=125),
    ]

    result = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=rows,
        trade_calendar=days,
        assessment_cutoffs=_cutoffs(days),
    )

    assert result["status"] == "blocked"
    assert result["blocked_reasons"] == [
        "append_only_ledger_proof_missing",
        "calendar_cutoff_proof_missing",
    ]
    assert [item["contract_code"] for item in result["selections"]] == [
        "TA701",
        "TA701",
        "TA701",
        "TA705",
    ]
    assert result["roll_events"] == [
        {
            "effective_trade_date": "2026-09-30",
            "from_contract": "TA701",
            "to_contract": "TA705",
            "reason": "two_day_rank_confirmation",
        }
    ]
    assert result["formal_eligibility_granted"] is False


def test_final_month_forces_next_trade_day_to_top_non_final_contract() -> None:
    days = ["2026-09-29", "2026-09-30"]
    rows = [
        _row(days[0], "TA609", "2026-09", oi=200, volume=200),
        _row(days[0], "TA701", "2027-01", oi=150, volume=150),
        _row(days[1], "TA609", "2026-09", oi=190, volume=180),
        _row(days[1], "TA701", "2027-01", oi=160, volume=155),
    ]

    result = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=rows,
        trade_calendar=days,
        assessment_cutoffs=_cutoffs(days),
    )

    assert [item["contract_code"] for item in result["selections"]] == ["TA609", "TA701"]
    assert result["roll_events"][0]["reason"] == "forced_final_month"


def test_late_publication_is_hidden_and_reported_as_missing_day() -> None:
    day = "2026-09-25"
    row = _row(day, "TA701", "2027-01", oi=100, volume=100, visible_at=f"{day}T19:00:00+08:00")

    result = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=[row],
        trade_calendar=[day],
        assessment_cutoffs=_cutoffs([day]),
    )

    assert result["selections"] == []
    assert set(result["blocked_reasons"]) == {
        "append_only_ledger_proof_missing",
        "calendar_cutoff_proof_missing",
        "late_publication",
        "missing_contract_day",
    }


def test_retroactive_revision_for_same_contract_day_is_conflict() -> None:
    day = "2026-09-25"
    first = _row(day, "TA701", "2027-01", oi=100, volume=100, revision="r1")
    corrected = _row(day, "TA701", "2027-01", oi=110, volume=100, revision="r2")

    result = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=[first, corrected],
        trade_calendar=[day],
        assessment_cutoffs=_cutoffs([day]),
    )

    assert "retroactive_revision_conflict" in result["blocked_reasons"]
    assert "duplicate_contract_day" in result["blocked_reasons"]


def test_source_instrument_and_hash_mismatches_are_stable_blockers() -> None:
    day = "2026-09-25"
    row = _row(day, "TA701", "2027-01", oi=100, volume=100)
    row["source_id"] = "wrong"
    row["raw_sha256"] = "not-a-hash"

    result = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=[row],
        trade_calendar=[day],
        assessment_cutoffs=_cutoffs([day]),
    )

    assert set(result["blocked_reasons"]) == {
        "append_only_ledger_proof_missing",
        "calendar_cutoff_proof_missing",
        "missing_contract_day",
        "raw_sha256_invalid",
    }


def test_valid_capture_with_wrong_source_and_instrument_is_rejected() -> None:
    day = "2026-09-25"
    row = _row(day, "TA701", "2027-01", oi=100, volume=100)
    row["source_id"] = "wrong"
    row["product"] = "PX"

    result = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=[row],
        trade_calendar=[day],
        assessment_cutoffs=_cutoffs([day]),
    )

    assert set(result["blocked_reasons"]) == {
        "append_only_ledger_proof_missing",
        "calendar_cutoff_proof_missing",
        "missing_contract_day",
        "source_instrument_mismatch",
    }


def test_ranked_leader_does_not_need_to_beat_both_oi_and_volume() -> None:
    days = ["2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"]
    rows = [
        _row(days[0], "TA701", "2027-01", oi=120, volume=200),
        _row(days[0], "TA705", "2027-05", oi=100, volume=100),
        _row(days[1], "TA701", "2027-01", oi=120, volume=200),
        _row(days[1], "TA705", "2027-05", oi=130, volume=50),
        _row(days[2], "TA701", "2027-01", oi=120, volume=200),
        _row(days[2], "TA705", "2027-05", oi=140, volume=40),
        _row(days[3], "TA701", "2027-01", oi=120, volume=200),
        _row(days[3], "TA705", "2027-05", oi=150, volume=30),
    ]

    result = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=rows,
        trade_calendar=days,
        assessment_cutoffs=_cutoffs(days),
    )

    # The frozen ranking is lexicographic: OI first, then volume. The switch
    # occurs after two confirmations even though challenger volume is lower.
    assert [item["contract_code"] for item in result["selections"]] == [
        "TA701",
        "TA701",
        "TA701",
        "TA705",
    ]
    assert result["roll_events"][0]["reason"] == "two_day_rank_confirmation"


def test_contract_code_must_match_product_and_delivery_month() -> None:
    day = "2026-09-25"
    row = _row(day, "TA705", "2027-01", oi=100, volume=100)

    result = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=[row],
        trade_calendar=[day],
        assessment_cutoffs=_cutoffs([day]),
    )

    assert "contract_delivery_mismatch" in result["blocked_reasons"]
    assert "missing_contract_day" in result["blocked_reasons"]


def test_caller_supplied_hashes_and_cutoffs_can_never_self_approve() -> None:
    day = "2026-09-25"
    result = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=[_row(day, "TA701", "2027-01", oi=100, volume=100)],
        trade_calendar=[day],
        assessment_cutoffs=_cutoffs([day]),
    )

    assert result["status"] == "blocked"
    assert result["evidence_origin"] == "caller_supplied_untrusted"
    assert result["formal_eligibility_granted"] is False
    assert "append_only_ledger_proof_missing" in result["blocked_reasons"]
    assert "calendar_cutoff_proof_missing" in result["blocked_reasons"]


def test_input_budgets_fail_before_unbounded_processing() -> None:
    day = "2026-09-25"
    with pytest.raises(FormalContinuousRollError, match="roll_rows_budget_exceeded"):
        audit_frozen_continuous_roll(
            formal_series_id=SERIES_ID,
            rows=[{}] * (MAX_ROLL_ROWS + 1),
            trade_calendar=[day],
            assessment_cutoffs=_cutoffs([day]),
        )
    oversized_calendar = [f"{year:04d}-01-01" for year in range(1, MAX_TRADE_DAYS + 2)]
    with pytest.raises(FormalContinuousRollError, match="trade_calendar_budget_exceeded"):
        audit_frozen_continuous_roll(
            formal_series_id=SERIES_ID,
            rows=[],
            trade_calendar=oversized_calendar,
            assessment_cutoffs={},
        )


def test_extreme_integer_has_stable_numeric_error() -> None:
    day = "2026-09-25"
    row = _row(day, "TA701", "2027-01", oi=100, volume=100)
    row["open_interest"] = 10**10_000

    result = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=[row],
        trade_calendar=[day],
        assessment_cutoffs=_cutoffs([day]),
    )

    assert "open_interest_invalid" in result["blocked_reasons"]
    assert result["status"] == "blocked"


def test_roll_audit_is_deterministic_and_does_not_mutate_inputs() -> None:
    days = ["2026-09-25"]
    rows = [_row(days[0], "TA701", "2027-01", oi=100, volume=100)]
    original = deepcopy(rows)

    first = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=rows,
        trade_calendar=days,
        assessment_cutoffs=_cutoffs(days),
    )
    second = audit_frozen_continuous_roll(
        formal_series_id=SERIES_ID,
        rows=list(reversed(rows)),
        trade_calendar=days,
        assessment_cutoffs=_cutoffs(days),
    )

    assert rows == original
    assert canonical_roll_json(first) == canonical_roll_json(second)

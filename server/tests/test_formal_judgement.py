import pytest

from app.formal_judgement import derive_formal_direction, is_policy_approved_review


def _rows():
    return [
        {
            "doc_id": "industry:poy-old",
            "product": "POY",
            "observed_at": "2026-07-09T09:00:00+08:00",
            "value": 7000,
        },
        {
            "doc_id": "industry:poy-new",
            "product": "POY",
            "observed_at": "2026-07-10T09:00:00+08:00",
            "value": 7140,
        },
        {
            "doc_id": "industry:dty-old",
            "product": "DTY",
            "observed_at": "2026-07-09T09:00:00+08:00",
            "value": 8500,
        },
        {
            "doc_id": "industry:dty-new",
            "product": "DTY",
            "observed_at": "2026-07-10T09:00:00+08:00",
            "value": 8670,
        },
    ]


def _review(reviewer: str = "codex"):
    return {
        "status": "reviewed",
        "reviewer": reviewer,
        "reviewer_type": "codex",
        "method": "price_basis_crosscheck",
        "version": "1.0.0",
        "criteria": ["source", "timestamp", "unit"],
        "result": "approved",
        "reason": "All declared checks passed.",
        "purpose": "formal_cost_pressure",
        "evidence_role": "downstream_transmission",
        "reviewed_at": "2026-07-10T10:00:00Z",
    }


def test_formal_direction_requires_real_review_records_for_latest_evidence() -> None:
    result = derive_formal_direction(
        rows=_rows(),
        required_products=("POY", "DTY"),
        review_map={},
        required_snapshot_id="snapshot-1",
    )

    assert result["qualified"] is False
    assert result["reviewed_evidence_ids"] == []
    assert "reviewed_evidence_required" in result["reasons"]
    assert result["direction_derivation"]["status"] == "insufficient_evidence"


def test_formal_direction_is_derived_from_reviewed_same_snapshot_price_pairs() -> None:
    result = derive_formal_direction(
        rows=_rows(),
        required_products=("POY", "DTY"),
        review_map={row["doc_id"]: _review() for row in _rows()},
        required_snapshot_id="snapshot-1",
    )

    assert result["qualified"] is True
    assert result["direction"] == "偏强"
    assert result["reviewed_evidence_ids"] == [
        "industry:dty-new",
        "industry:dty-old",
        "industry:poy-new",
        "industry:poy-old",
    ]
    assert result["evidence_mapping"]["supporting_evidence"] == result["reviewed_evidence_ids"]
    assert result["direction_derivation"]["status"] == "verified"
    assert all(item["reviewer"] for item in result["review_audit"])


def test_formal_direction_keeps_date_only_observed_at_eligible() -> None:
    rows = [{**row, "observed_at": str(row["observed_at"])[:10]} for row in _rows()]

    result = derive_formal_direction(
        rows=rows,
        required_products=("POY", "DTY"),
        review_map={row["doc_id"]: _review() for row in rows},
        required_snapshot_id="snapshot-date-only",
    )

    assert result["qualified"] is True
    assert result["direction"] == "偏强"
    assert len(result["reviewed_evidence_ids"]) == 4


def test_unreviewed_rows_have_zero_influence_on_multi_spec_aggregation() -> None:
    rows = _rows() + [
        {
            "doc_id": "industry:poy-unreviewed",
            "product": "POY",
            "observed_at": "2026-07-10T10:00:00+08:00",
            "value": 20000,
        },
        {
            "doc_id": "industry:dty-unreviewed",
            "product": "DTY",
            "observed_at": "2026-07-10T10:00:00+08:00",
            "value": 25000,
        },
    ]
    reviews = {row["doc_id"]: _review() for row in _rows()}
    result = derive_formal_direction(
        rows=rows, required_products=("POY", "DTY"), review_map=reviews, required_snapshot_id="snapshot-1"
    )

    assert result["qualified"] is True
    assert result["direction"] == "偏强"
    assert all("unreviewed" not in doc_id for doc_id in result["reviewed_evidence_ids"])
    assert {item["latest_value"] for item in result["direction_derivation"]["trace"]} == {7140.0, 8670.0}


def test_unreviewed_previous_price_blocks_direction_derivation() -> None:
    reviews = {"industry:poy-new": _review(), "industry:dty-new": _review()}
    result = derive_formal_direction(
        rows=_rows(), required_products=("POY", "DTY"), review_map=reviews, required_snapshot_id="snapshot-1"
    )

    assert result["qualified"] is False
    assert result["direction"] == ""
    assert "reviewed_baseline_evidence_required:DTY" in result["reasons"]
    assert "reviewed_baseline_evidence_required:POY" in result["reasons"]


def test_legacy_or_incomplete_review_cannot_satisfy_formal_gate() -> None:
    legacy = {
        row["doc_id"]: {"status": "reviewed", "reviewer": "legacy", "reviewed_at": "2026-07-10T10:00:00Z"}
        for row in _rows()
    }
    result = derive_formal_direction(
        rows=_rows(), required_products=("POY", "DTY"), review_map=legacy, required_snapshot_id="snapshot-1"
    )
    assert result["qualified"] is False
    assert result["reviewed_evidence_ids"] == []


def test_downstream_reviews_alone_cannot_unlock_cost_pressure_gate() -> None:
    reviews = {row["doc_id"]: _review() for row in _rows()}
    result = derive_formal_direction(
        rows=_rows(),
        required_products=("POY", "DTY"),
        review_map=reviews,
        required_snapshot_id="snapshot-1",
        required_evidence_roles=("upstream_cost_driver", "transmission_path", "downstream_transmission"),
    )
    assert result["qualified"] is False
    assert "reviewed_evidence_role_required:upstream_cost_driver" in result["reasons"]
    assert "reviewed_evidence_role_required:transmission_path" in result["reasons"]


def test_invalid_or_unzoned_legacy_timestamps_have_zero_influence() -> None:
    invalid_rows = [
        {
            "doc_id": "industry:poy-invalid",
            "product": "POY",
            "observed_at": "2026-02-30T00:00:00+08:00",
            "value": 1,
        },
        {"doc_id": "industry:dty-unzoned", "product": "DTY", "observed_at": "2026-07-11T09:00:00", "value": 99999},
        {"doc_id": "industry:poy-non-string", "product": "POY", "observed_at": 178, "value": 99999},
        {"doc_id": "industry:dty-malformed", "product": "DTY", "observed_at": "not-a-timestamp", "value": 99999},
    ]
    rows = _rows() + invalid_rows
    reviews = {row["doc_id"]: _review() for row in rows}

    result = derive_formal_direction(
        rows=rows, required_products=("POY", "DTY"), review_map=reviews, required_snapshot_id="snapshot-1"
    )

    assert result["qualified"] is True
    assert result["direction"] == "偏强"
    assert all(
        not any(token in doc_id for token in ("invalid", "unzoned", "non-string", "malformed"))
        for doc_id in result["reviewed_evidence_ids"]
    )
    assert {item["latest_value"] for item in result["direction_derivation"]["trace"]} == {7140.0, 8670.0}


def test_invalid_timestamp_rows_cannot_supply_required_evidence_roles() -> None:
    invalid = {
        "doc_id": "industry:invalid-upstream",
        "product": "POY",
        "observed_at": "not-a-timestamp",
        "value": 100000,
    }
    reviews = {row["doc_id"]: _review() for row in _rows()}
    reviews[invalid["doc_id"]] = {**_review(), "evidence_role": "upstream_cost_driver"}

    result = derive_formal_direction(
        rows=[*_rows(), invalid],
        required_products=("POY", "DTY"),
        review_map=reviews,
        required_snapshot_id="snapshot-1",
        required_evidence_roles=("upstream_cost_driver",),
    )

    assert result["qualified"] is False
    assert result["conclusion_confidence"] == 0.0
    assert "reviewed_evidence_role_required:upstream_cost_driver" in result["reasons"]


def test_valid_nonpaired_px_and_pta_rows_unlock_roles_without_entering_direction_audit() -> None:
    role_rows = [
        {
            "doc_id": "industry:px-current",
            "product": "PX",
            "observed_at": "2026-07-10T09:15:00+08:00",
            "value": 842.5,
        },
        {
            "doc_id": "industry:pta-current",
            "product": "PTA",
            "observed_at": "2026-07-10",
            "value": 5120,
        },
    ]
    reviews = {row["doc_id"]: _review() for row in _rows()}
    reviews["industry:px-current"] = {**_review(), "evidence_role": "upstream_cost_driver"}
    reviews["industry:pta-current"] = {**_review(), "evidence_role": "transmission_path"}

    result = derive_formal_direction(
        rows=[*_rows(), *role_rows],
        required_products=("POY", "DTY"),
        review_map=reviews,
        required_snapshot_id="snapshot-1",
        required_evidence_roles=("upstream_cost_driver", "transmission_path", "downstream_transmission"),
    )

    assert result["qualified"] is True
    assert result["direction"] == "偏强"
    assert {item["product"] for item in result["direction_derivation"]["trace"]} == {"POY", "DTY"}
    assert result["reviewed_evidence_ids"] == [
        "industry:dty-new",
        "industry:dty-old",
        "industry:poy-new",
        "industry:poy-old",
    ]
    assert result["evidence_mapping"]["supporting_evidence"] == result["reviewed_evidence_ids"]
    assert {item["doc_id"] for item in result["review_audit"]} == set(result["reviewed_evidence_ids"])
    assert {"industry:px-current", "industry:pta-current"}.isdisjoint(result["reviewed_evidence_ids"])


@pytest.mark.parametrize(
    "observed_at",
    [
        "not-a-timestamp",
        "2026-02-30T09:00:00+08:00",
        178,
        "2026-07-10T09:00:00",
    ],
    ids=["malformed", "impossible", "non-string", "naive"],
)
def test_invalid_nonpaired_role_rows_fail_closed(observed_at: object) -> None:
    invalid_role_row = {
        "doc_id": "industry:px-invalid-role",
        "product": "PX",
        "observed_at": observed_at,
        "value": 842.5,
    }
    reviews = {row["doc_id"]: _review() for row in _rows()}
    reviews[invalid_role_row["doc_id"]] = {**_review(), "evidence_role": "upstream_cost_driver"}

    result = derive_formal_direction(
        rows=[*_rows(), invalid_role_row],
        required_products=("POY", "DTY"),
        review_map=reviews,
        required_snapshot_id="snapshot-1",
        required_evidence_roles=("upstream_cost_driver",),
    )

    assert result["qualified"] is False
    assert "reviewed_evidence_role_required:upstream_cost_driver" in result["reasons"]
    assert invalid_role_row["doc_id"] not in result["reviewed_evidence_ids"]
    assert invalid_role_row["doc_id"] not in result.get("evidence_mapping", {}).get("supporting_evidence", [])
    assert invalid_role_row["doc_id"] not in {item["doc_id"] for item in result["review_audit"]}
    assert {item["product"] for item in result["direction_derivation"]["trace"]} == {"POY", "DTY"}


def test_review_map_only_record_cannot_supply_required_role() -> None:
    reviews = {row["doc_id"]: _review() for row in _rows()}
    reviews["industry:px-not-in-input"] = {**_review(), "evidence_role": "upstream_cost_driver"}

    result = derive_formal_direction(
        rows=_rows(),
        required_products=("POY", "DTY"),
        review_map=reviews,
        required_snapshot_id="snapshot-1",
        required_evidence_roles=("upstream_cost_driver",),
    )

    assert result["qualified"] is False
    assert "reviewed_evidence_role_required:upstream_cost_driver" in result["reasons"]
    assert "industry:px-not-in-input" not in result["reviewed_evidence_ids"]


def test_personal_mode_review_gate_accepts_minimal_review() -> None:
    from app.settings import settings

    original = settings.personal_mode
    try:
        object.__setattr__(settings, "personal_mode", True)
        assert is_policy_approved_review({"status": "reviewed"}) is True
        assert is_policy_approved_review({"status": "rejected"}) is False
        assert is_policy_approved_review({}) is False

        object.__setattr__(settings, "personal_mode", False)
        assert is_policy_approved_review({"status": "reviewed"}) is False
    finally:
        object.__setattr__(settings, "personal_mode", original)

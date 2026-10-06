from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta

import pytest

from app.experience_cards import ExperienceCardInputError, build_experience_card_revision
from app.phase_a_contracts import assert_payload_valid, load_contract


def _dates() -> list[str]:
    start = date(2026, 6, 2)
    return [(start + timedelta(days=index)).isoformat() for index in range(30)]


def _bundle(*, subtarget: str = "poy", horizons=(1, 7, 30), directions=None) -> dict:
    return {
        "prediction_batch_id": "batch-1",
        "prediction_ids_by_horizon": {1: "pred-1", 7: "pred-7", 30: "pred-30"},
        "prediction_revision_id": "prediction-rev-1",
        "data_snapshot_id": "snapshot-1",
        "node_id": "poy_dty_upstream_cost_pressure",
        "subtarget": subtarget,
        "target_series_id": f"{subtarget}.test.contractible",
        "benchmark_series_id": "benchmark.test.contractible",
        "as_of_time": "2026-06-01T08:20:00+08:00",
        "directions_by_horizon": directions or {1: "up", 7: "up", 30: "up"},
        "horizons": list(horizons),
        "calendar_id": "test_effective_days",
        "calendar_version": "test-calendar.v1",
        "overlapping_event_ids": [],
    }


def _point(
    observation_id: str,
    series_id: str,
    effective_date: str,
    value: float,
    *,
    visible_at: str | None = None,
    visibility_mode: str = "strict_as_of",
    revision_id: str | None = None,
    supersedes_revision_id: str | None = None,
    unit: str = "CNY/mt",
    quote_type: str = "test_assessment",
) -> dict:
    return {
        "observation_id": observation_id,
        "series_id": series_id,
        "effective_date": effective_date,
        "value": value,
        "unit": unit,
        "quote_type": quote_type,
        "first_visible_at": visible_at or f"{effective_date}T18:00:00+08:00",
        "quality_status": "eligible",
        "visibility_mode": visibility_mode,
        "revision_id": revision_id or f"rev-{observation_id}",
        "supersedes_revision_id": supersedes_revision_id,
    }


def _paths(*, target_values: list[float], benchmark_values: list[float] | None = None, subtarget="poy"):
    dates = _dates()
    target_id = f"{subtarget}.test.contractible"
    benchmark_id = "benchmark.test.contractible"
    target = [_point("target-anchor", target_id, "2026-06-01", 100, visible_at="2026-06-01T07:00:00+08:00")]
    benchmark = [_point("benchmark-anchor", benchmark_id, "2026-06-01", 200, visible_at="2026-06-01T07:00:00+08:00")]
    target.extend(
        _point(f"target-{index}", target_id, day, value)
        for index, (day, value) in enumerate(zip(dates[: len(target_values)], target_values, strict=True), start=1)
    )
    values = benchmark_values or [200 + index for index in range(1, len(target_values) + 1)]
    benchmark.extend(
        _point(f"benchmark-{index}", benchmark_id, day, value)
        for index, (day, value) in enumerate(zip(dates[: len(values)], values, strict=True), start=1)
    )
    return target, benchmark


def _eligible(_series_id: str) -> str:
    return "eligible"


def _build(
    *,
    target_values: list[float],
    benchmark_values: list[float] | None = None,
    evaluation_as_of: str,
    bundle: dict | None = None,
    previous: dict | None = None,
    resolver=_eligible,
):
    active_bundle = bundle or _bundle()
    target, benchmark = _paths(
        target_values=target_values,
        benchmark_values=benchmark_values,
        subtarget=active_bundle["subtarget"],
    )
    return build_experience_card_revision(
        prediction_bundle=active_bundle,
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of=evaluation_as_of,
        series_eligibility_resolver=resolver,
        previous_revision=previous,
    )


def test_d1_metrics_relative_mfe_mae_and_phase_a_schema() -> None:
    result = _build(target_values=[110], benchmark_values=[204], evaluation_as_of="2026-06-02T20:00:00+08:00")
    card = result["card"]
    assert result["status"] == "built"
    assert card["maturity_stage"] == "d1_preliminary"
    assert card["raw_change_abs"] == 10
    assert card["raw_change_pct"] == 10
    assert card["relative_change"] == 8
    assert card["mfe"] == 10
    assert card["mae"] == 0
    assert card["days_to_peak"] == 1
    assert card["subtarget"] == "poy"
    assert card["diagnostic_only"] is False
    assert_payload_valid("experience_card", card)


def test_down_prediction_uses_direction_adjusted_mfe_and_mae() -> None:
    bundle = _bundle(directions={1: "down", 7: "down", 30: "down"})
    result = _build(target_values=[90], evaluation_as_of="2026-06-02T20:00:00+08:00", bundle=bundle)
    card = result["card"]
    assert card["mfe"] == 10
    assert card["mae"] == 0
    assert card["mechanism_support_status"] == "supported"


def test_peak_uses_first_tie_and_reversal_requires_prior_favorable_path() -> None:
    values = [110, 105, 110, 100, 99, 101, 102]
    d1 = _build(target_values=values, evaluation_as_of="2026-06-02T20:00:00+08:00")["card"]
    d7 = _build(
        target_values=values,
        evaluation_as_of="2026-06-08T20:00:00+08:00",
        previous=d1,
    )["card"]
    assert d7["days_to_peak"] == 1
    assert d7["first_reversal_at"] == "2026-06-05"


def test_path_that_is_never_favorable_does_not_invent_reversal() -> None:
    values = [99, 98, 97, 96, 95, 94, 93]
    d1 = _build(target_values=values, evaluation_as_of="2026-06-02T20:00:00+08:00")["card"]
    d7 = _build(target_values=values, evaluation_as_of="2026-06-08T20:00:00+08:00", previous=d1)["card"]
    assert d7["first_reversal_at"] is None
    assert d7["mfe"] == 0
    assert d7["mae"] == 7


def test_effective_observation_dates_not_calendar_distance_drive_maturity() -> None:
    dates = _dates()
    result = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=[],
        benchmark_points=[],
        expected_observation_dates=dates,
        evaluation_as_of="2026-06-07T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
    )
    assert result["card"]["maturity_stage"] == "d1_preliminary"
    assert result["completed_effective_days"] == 6


def test_d1_to_d7_to_d30_revision_chain_and_idempotency() -> None:
    values = [100 + index for index in range(1, 31)]
    d1_result = _build(target_values=values, evaluation_as_of="2026-06-02T20:00:00+08:00")
    d1 = d1_result["card"]
    d7 = _build(
        target_values=values,
        evaluation_as_of="2026-06-08T20:00:00+08:00",
        previous=d1,
    )["card"]
    d30 = _build(
        target_values=values,
        evaluation_as_of="2026-07-01T20:00:00+08:00",
        previous=d7,
    )["card"]
    assert [d1["maturity_stage"], d7["maturity_stage"], d30["maturity_stage"]] == [
        "d1_preliminary",
        "d7_intermediate",
        "d30_mature",
    ]
    assert d7["previous_revision_id"] == d1["revision_id"]
    assert d30["previous_revision_id"] == d7["revision_id"]
    for card in (d1, d7, d30):
        assert_payload_valid("experience_card", card)
    unchanged = _build(
        target_values=values,
        evaluation_as_of="2026-07-02T20:00:00+08:00",
        previous=d30,
    )
    assert unchanged["status"] == "unchanged"
    assert unchanged["card"]["revision_id"] == d30["revision_id"]


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("prediction_revision_id", "prediction-rev-2"),
        ("data_snapshot_id", "snapshot-2"),
        ("calendar_id", "other-calendar"),
        ("calendar_version", "test-calendar.v2"),
        ("overlapping_event_ids", ["event-2"]),
    ],
)
def test_same_stage_frozen_input_change_creates_new_revision(field: str, replacement: object) -> None:
    target, benchmark = _paths(target_values=[110])
    base_bundle = _bundle()
    base = build_experience_card_revision(
        prediction_bundle=base_bundle,
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
    )["card"]
    changed_bundle = deepcopy(base_bundle)
    changed_bundle[field] = replacement
    changed = build_experience_card_revision(
        prediction_bundle=changed_bundle,
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
        previous_revision=base,
    )
    assert changed["status"] == "built"
    assert changed["card"]["revision_id"] != base["revision_id"]
    assert changed["card"]["calculation_fingerprint"] != base["calculation_fingerprint"]


def test_selected_observation_revision_change_creates_new_revision() -> None:
    target, benchmark = _paths(target_values=[110])
    base = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
    )["card"]
    target[1]["revision_id"] = "revised-posterior"
    changed = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
        previous_revision=base,
    )
    assert changed["status"] == "built"
    assert changed["card"]["revision_id"] != base["revision_id"]


def test_anchor_uses_prediction_asof_revision_and_posterior_uses_evaluation_asof_revision() -> None:
    bundle = _bundle()
    target_id = bundle["target_series_id"]
    target = [
        _point(
            "anchor-original",
            target_id,
            "2026-06-01",
            100,
            visible_at="2026-06-01T07:00:00+08:00",
            revision_id="anchor-r1",
        ),
        _point(
            "anchor-correction",
            target_id,
            "2026-06-01",
            80,
            visible_at="2026-06-02T10:00:00+08:00",
            revision_id="anchor-r2",
            supersedes_revision_id="anchor-r1",
        ),
        _point(
            "posterior-original",
            target_id,
            _dates()[0],
            105,
            visible_at="2026-06-02T18:00:00+08:00",
            revision_id="posterior-r1",
        ),
        _point(
            "posterior-correction",
            target_id,
            _dates()[0],
            110,
            visible_at="2026-06-02T19:00:00+08:00",
            revision_id="posterior-r2",
            supersedes_revision_id="posterior-r1",
        ),
    ]
    _, benchmark = _paths(target_values=[110])
    card = build_experience_card_revision(
        prediction_bundle=bundle,
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
    )["card"]
    assert card["start_price"] == 100
    assert card["end_price"] == 110
    assert card["target_anchor_revision_id"] == "anchor-r1"
    assert card["source_revision_ids"] == ["posterior-r2"]
    assert card["scoreability"] == "scorable"


def test_maturity_cannot_skip_or_move_backwards() -> None:
    values = [100 + index for index in range(1, 31)]
    d1 = _build(target_values=values, evaluation_as_of="2026-06-02T20:00:00+08:00")["card"]
    skipped = _build(
        target_values=values,
        evaluation_as_of="2026-07-01T20:00:00+08:00",
        previous=d1,
    )
    assert skipped["status"] == "blocked"
    assert skipped["blockers"] == ["maturity_checkpoint_skip_forbidden"]
    d7 = _build(target_values=values, evaluation_as_of="2026-06-08T20:00:00+08:00", previous=d1)["card"]
    regressed = _build(
        target_values=values,
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        previous=d7,
    )
    assert regressed["blockers"] == ["maturity_regression_forbidden"]


def test_explicit_checkpoint_enables_deterministic_catchup() -> None:
    values = [100 + index for index in range(1, 31)]
    d1 = _build(
        target_values=values,
        evaluation_as_of="2026-07-01T20:00:00+08:00",
    )
    assert d1["status"] == "blocked"
    target, benchmark = _paths(target_values=values)
    d1 = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-07-01T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
        checkpoint_horizon=1,
    )["card"]
    d7 = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-07-01T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
        previous_revision=d1,
        checkpoint_horizon=7,
    )["card"]
    d30 = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-07-01T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
        previous_revision=d7,
        checkpoint_horizon=30,
    )["card"]
    assert [d1["horizon_days"], d7["horizon_days"], d30["horizon_days"]] == [1, 7, 30]
    assert all(card["evaluation_as_of"] == "2026-07-01T20:00:00+08:00" for card in (d1, d7, d30))


def test_explicit_checkpoint_pending_and_d14_rejection() -> None:
    target, benchmark = _paths(target_values=[110] * 7)
    pending = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
        checkpoint_horizon=7,
    )
    assert pending["status"] == "pending"
    assert pending["card"] is None
    assert pending["next_checkpoint"] == 7
    with pytest.raises(ExperienceCardInputError, match=r"D\+14"):
        build_experience_card_revision(
            prediction_bundle=_bundle(),
            target_points=target,
            benchmark_points=benchmark,
            expected_observation_dates=_dates(),
            evaluation_as_of="2026-06-08T20:00:00+08:00",
            series_eligibility_resolver=_eligible,
            checkpoint_horizon=14,
        )


def test_poy_and_dty_have_distinct_card_identity_and_are_not_averaged() -> None:
    poy = _build(target_values=[110], evaluation_as_of="2026-06-02T20:00:00+08:00")["card"]
    dty_bundle = _bundle(subtarget="dty")
    dty = _build(
        target_values=[90],
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        bundle=dty_bundle,
    )["card"]
    assert poy["experience_card_id"] != dty["experience_card_id"]
    assert poy["raw_change_pct"] == 10
    assert dty["raw_change_pct"] == -10


def test_blocked_series_computes_diagnostic_metrics_but_never_scores() -> None:
    result = _build(
        target_values=[110],
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        resolver=lambda _series_id: "blocked_missing_formula",
    )
    card = result["card"]
    assert card["raw_change_pct"] == 10
    assert card["scoreability"] == "unscorable"
    assert card["diagnostic_only"] is True
    assert card["eligible_for_retrieval_at"] is None
    assert "target_series_status:blocked_missing_formula" in card["exclusion_reasons"]


def test_current_phase_contract_never_makes_formal_target_scorable() -> None:
    contract = load_contract()
    statuses = {item["series_id"]: item["eligibility_status"] for item in contract["series"]}
    bundle = _bundle()
    bundle["target_series_id"] = "poy.upstream_cost_pressure.index"
    bundle["benchmark_series_id"] = "fx.usd_cny.cfets.central_parity.cny_per_usd"
    target = [
        _point("target-anchor", bundle["target_series_id"], "2026-06-01", 100, visible_at="2026-06-01T07:00:00+08:00"),
        _point("target-1", bundle["target_series_id"], _dates()[0], 110),
    ]
    benchmark = [
        _point(
            "benchmark-anchor",
            bundle["benchmark_series_id"],
            "2026-06-01",
            7,
            visible_at="2026-06-01T07:00:00+08:00",
            unit="CNY/USD",
            quote_type="official_benchmark",
        ),
        _point(
            "benchmark-1",
            bundle["benchmark_series_id"],
            _dates()[0],
            7.1,
            unit="CNY/USD",
            quote_type="official_benchmark",
        ),
    ]
    result = build_experience_card_revision(
        prediction_bundle=bundle,
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        series_eligibility_resolver=lambda series_id: statuses.get(series_id, "unknown"),
    )
    card = result["card"]
    assert card["scoreability"] == "unscorable"
    assert "target_series_status:blocked_evidence_capture_pending" in card["exclusion_reasons"]
    assert "benchmark_series_status:contractible" in card["exclusion_reasons"]


def test_reconstructed_and_future_visible_points_fail_closed() -> None:
    target, benchmark = _paths(target_values=[110])
    target[1]["visibility_mode"] = "reconstructed"
    target.append(
        _point(
            "future-revision",
            "poy.test.contractible",
            _dates()[0],
            120,
            visible_at="2026-06-03T10:00:00+08:00",
        )
    )
    result = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
    )
    card = result["card"]
    assert card["visibility_mode"] == "reconstructed"
    assert card["scoreability"] == "unscorable"
    assert any(reason.startswith("future_visible_excluded") for reason in card["exclusion_reasons"])


def test_unknown_visibility_mode_and_invalid_price_fail_closed() -> None:
    target, benchmark = _paths(target_values=[0])
    target[1]["visibility_mode"] = "unknown"
    result = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
    )
    reasons = result["card"]["exclusion_reasons"]
    assert "visibility_not_strict_as_of" in reasons
    assert "target_posterior_price_invalid" in reasons
    assert result["card"]["scoreability"] == "unscorable"


def test_missing_effective_date_and_quote_basis_mismatch_are_unscorable() -> None:
    target, benchmark = _paths(target_values=[101, 102, 103, 104, 105, 106, 107])
    target.pop(3)
    target[-1]["unit"] = "USD/mt"
    d1 = _build(target_values=[101], evaluation_as_of="2026-06-02T20:00:00+08:00")["card"]
    result = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-08T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
        previous_revision=d1,
    )
    assert result["card"]["scoreability"] == "unscorable"
    assert "target_effective_dates_missing" in result["card"]["exclusion_reasons"]
    assert "target_quote_basis_mismatch" in result["card"]["exclusion_reasons"]


def test_revision_chain_ambiguity_is_unscorable() -> None:
    target, benchmark = _paths(target_values=[110])
    duplicate = deepcopy(target[1])
    duplicate.update(observation_id="duplicate", revision_id="independent-revision")
    target.append(duplicate)
    result = build_experience_card_revision(
        prediction_bundle=_bundle(),
        target_points=target,
        benchmark_points=benchmark,
        expected_observation_dates=_dates(),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        series_eligibility_resolver=_eligible,
    )
    assert result["card"]["scoreability"] == "unscorable"
    assert any(reason.startswith("revision_chain_ambiguous") for reason in result["card"]["exclusion_reasons"])


def test_d14_and_non_zoned_time_are_rejected() -> None:
    with pytest.raises(ExperienceCardInputError, match=r"D\+14"):
        _build(
            target_values=[110],
            evaluation_as_of="2026-06-02T20:00:00+08:00",
            bundle=_bundle(horizons=(1, 7, 14, 30)),
        )
    bundle = _bundle()
    bundle["as_of_time"] = "2026-06-01T08:20:00"
    with pytest.raises(ExperienceCardInputError, match="timezone"):
        _build(target_values=[110], evaluation_as_of="2026-06-02T20:00:00+08:00", bundle=bundle)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prediction_ids_by_horizon", {1: "pred-1", 30: "pred-30"}),
        ("prediction_ids_by_horizon", {1: "pred-1", 7: "pred-7", 14: "legacy", 30: "pred-30"}),
        ("directions_by_horizon", {1: "up", 30: "up"}),
        ("directions_by_horizon", {1: "up", 7: "up", 14: "up", 30: "up"}),
    ],
)
def test_horizon_mappings_require_exact_formal_keys(field: str, value: dict) -> None:
    bundle = _bundle()
    bundle[field] = value
    with pytest.raises(ExperienceCardInputError, match=r"keys must be exactly|D\+14"):
        _build(target_values=[110], evaluation_as_of="2026-06-02T20:00:00+08:00", bundle=bundle)


def test_horizon_mapping_rejects_duplicate_normalized_keys() -> None:
    bundle = _bundle()
    bundle["prediction_ids_by_horizon"] = {
        1: "pred-1",
        "1": "duplicate-pred-1",
        7: "pred-7",
        30: "pred-30",
    }
    with pytest.raises(ExperienceCardInputError, match="duplicate normalized keys"):
        _build(target_values=[110], evaluation_as_of="2026-06-02T20:00:00+08:00", bundle=bundle)


@pytest.mark.parametrize("invalid_token", [1.9, 30.9, True, " 1", "+1", "01", "1.0"])
@pytest.mark.parametrize(
    "container",
    ["horizons", "prediction_ids_by_horizon", "directions_by_horizon"],
)
def test_all_horizon_containers_reject_noncanonical_tokens(container: str, invalid_token: object) -> None:
    bundle = _bundle()
    if container == "horizons":
        bundle[container] = [invalid_token, 7, 30]
    elif container == "prediction_ids_by_horizon":
        bundle[container] = {invalid_token: "pred-1", 7: "pred-7", 30: "pred-30"}
    else:
        bundle[container] = {invalid_token: "up", 7: "up", 30: "up"}
    with pytest.raises(ExperienceCardInputError, match="horizon token must be exactly"):
        _build(target_values=[110], evaluation_as_of="2026-06-02T20:00:00+08:00", bundle=bundle)


def test_neutral_direction_keeps_directional_metrics_unset_and_unscorable() -> None:
    bundle = _bundle(directions={1: "neutral", 7: "neutral", 30: "neutral"})
    card = _build(
        target_values=[110],
        evaluation_as_of="2026-06-02T20:00:00+08:00",
        bundle=bundle,
    )["card"]
    assert card["mfe"] is None
    assert card["mae"] is None
    assert card["scoreability"] == "unscorable"
    assert "direction_not_scorable" in card["exclusion_reasons"]

from __future__ import annotations

from copy import deepcopy
from datetime import date, timedelta

import pytest

from app.experience_settlement import (
    ExperienceSettlementInputError,
    plan_due_experience_settlements,
)


def _dates() -> list[str]:
    start = date(2026, 6, 2)
    return [(start + timedelta(days=index)).isoformat() for index in range(30)]


def _bundle(subtarget: str = "poy", horizons=(1, 7, 30)) -> dict:
    return {
        "prediction_batch_id": "batch-1",
        "prediction_ids_by_horizon": {
            1: f"pred-{subtarget}-1",
            7: f"pred-{subtarget}-7",
            30: f"pred-{subtarget}-30",
        },
        "prediction_revision_id": "prediction-r1",
        "data_snapshot_id": "snapshot-1",
        "node_id": "poy_dty_upstream_cost_pressure",
        "subtarget": subtarget,
        "target_series_id": f"{subtarget}.test.target",
        "benchmark_series_id": "benchmark.test.target",
        "as_of_time": "2026-06-01T08:20:00+08:00",
        "directions_by_horizon": {1: "up", 7: "up", 30: "up"},
        "horizons": horizons,
        "calendar_id": "test-effective-days",
        "calendar_version": "2026.v1",
        "overlapping_event_ids": [],
    }


def _point(
    observation_id: str,
    series_id: str,
    effective_date: str,
    value: float,
    *,
    visible_at: str | None = None,
    revision_id: str | None = None,
    supersedes_revision_id: str | None = None,
) -> dict:
    return {
        "observation_id": observation_id,
        "series_id": series_id,
        "effective_date": effective_date,
        "value": value,
        "unit": "CNY/mt",
        "quote_type": "test_assessment",
        "first_visible_at": visible_at or f"{effective_date}T18:00:00+08:00",
        "quality_status": "eligible",
        "visibility_mode": "strict_as_of",
        "revision_id": revision_id or f"rev-{observation_id}",
        "supersedes_revision_id": supersedes_revision_id,
    }


def _candidate(subtarget: str = "poy") -> dict:
    bundle = _bundle(subtarget)
    target = [
        _point(
            f"{subtarget}-anchor",
            bundle["target_series_id"],
            "2026-06-01",
            100,
            visible_at="2026-06-01T07:00:00+08:00",
        )
    ]
    benchmark = [
        _point(
            "benchmark-anchor",
            bundle["benchmark_series_id"],
            "2026-06-01",
            200,
            visible_at="2026-06-01T07:00:00+08:00",
        )
    ]
    target.extend(
        _point(f"{subtarget}-{index}", bundle["target_series_id"], day, 100 + index)
        for index, day in enumerate(_dates(), start=1)
    )
    benchmark.extend(
        _point(f"benchmark-{index}", bundle["benchmark_series_id"], day, 200 + index)
        for index, day in enumerate(_dates(), start=1)
    )
    return {
        "prediction_bundle": bundle,
        "target_points": target,
        "benchmark_points": benchmark,
        "expected_observation_dates": _dates(),
        "prediction_status": "available",
        "target_observation_status": "available",
        "benchmark_observation_status": "available",
        "target_series_status": "eligible",
        "benchmark_series_status": "eligible",
        "previous_revision": None,
    }


def test_d1_due_plan_contains_storage_ready_action() -> None:
    plan = plan_due_experience_settlements(candidates=[_candidate()], evaluation_as_of="2026-06-02T20:00:00+08:00")
    item = plan["items"][0]
    assert item["status"] == "planned"
    assert plan["counts"]["actions"] == 1
    assert [action["checkpoint_horizon"] for action in item["actions"]] == [1]
    assert item["actions"][0]["card"]["evaluation_as_of"] == "2026-06-02T20:00:00+08:00"


def test_not_due_is_pending_without_actions() -> None:
    plan = plan_due_experience_settlements(candidates=[_candidate()], evaluation_as_of="2026-06-01T20:00:00+08:00")
    item = plan["items"][0]
    assert item["status"] == "pending"
    assert item["actions"] == []
    assert item["next_checkpoint"] == 1
    assert item["next_expected_date"] == "2026-06-02"


def test_late_first_run_catches_up_d1_d7_d30_in_order() -> None:
    plan = plan_due_experience_settlements(candidates=[_candidate()], evaluation_as_of="2026-07-01T20:00:00+08:00")
    actions = plan["items"][0]["actions"]
    assert [action["checkpoint_horizon"] for action in actions] == [1, 7, 30]
    assert actions[1]["expected_previous_revision_id"] == actions[0]["card"]["revision_id"]
    assert actions[2]["expected_previous_revision_id"] == actions[1]["card"]["revision_id"]


def test_existing_d1_catches_up_only_d7_and_d30() -> None:
    candidate = _candidate()
    first = plan_due_experience_settlements(candidates=[candidate], evaluation_as_of="2026-06-02T20:00:00+08:00")
    candidate["previous_revision"] = first["items"][0]["actions"][0]["card"]
    catchup = plan_due_experience_settlements(candidates=[candidate], evaluation_as_of="2026-07-01T20:00:00+08:00")
    assert [action["checkpoint_horizon"] for action in catchup["items"][0]["actions"]] == [
        7,
        30,
    ]


@pytest.mark.parametrize(
    ("field", "value", "status", "reason"),
    [
        ("prediction_status", "unavailable", "unavailable", "prediction_unavailable"),
        ("target_observation_status", "blocked_license", "blocked", "target_observations_blocked"),
        ("target_series_status", "blocked_missing_formula", "blocked", "target_series_blocked"),
        ("benchmark_series_status", "contractible", "blocked", "benchmark_series_blocked"),
    ],
)
def test_unavailable_and_blocked_inputs_fail_closed(field: str, value: str, status: str, reason: str) -> None:
    candidate = _candidate()
    candidate[field] = value
    item = plan_due_experience_settlements(candidates=[candidate], evaluation_as_of="2026-07-01T20:00:00+08:00")[
        "items"
    ][0]
    assert item["status"] == status
    assert item["actions"] == []
    assert reason in item["reason_codes"]


def test_poy_and_dty_are_sorted_and_planned_independently() -> None:
    plan = plan_due_experience_settlements(
        candidates=[_candidate("poy"), _candidate("dty")],
        evaluation_as_of="2026-06-02T20:00:00+08:00",
    )
    assert [item["subtarget"] for item in plan["items"]] == ["dty", "poy"]
    cards = [item["actions"][0]["card"] for item in plan["items"]]
    assert cards[0]["experience_card_id"] != cards[1]["experience_card_id"]


def test_dual_asof_preserves_prediction_anchor_and_uses_visible_posterior_revision() -> None:
    candidate = _candidate()
    target_id = candidate["prediction_bundle"]["target_series_id"]
    candidate["target_points"] = [
        _point(
            "anchor-r1",
            target_id,
            "2026-06-01",
            100,
            visible_at="2026-06-01T07:00:00+08:00",
            revision_id="anchor-r1",
        ),
        _point(
            "anchor-r2",
            target_id,
            "2026-06-01",
            80,
            visible_at="2026-06-02T10:00:00+08:00",
            revision_id="anchor-r2",
            supersedes_revision_id="anchor-r1",
        ),
        _point(
            "posterior-r1",
            target_id,
            "2026-06-02",
            105,
            visible_at="2026-06-02T18:00:00+08:00",
            revision_id="posterior-r1",
        ),
        _point(
            "posterior-r2",
            target_id,
            "2026-06-02",
            110,
            visible_at="2026-06-02T19:00:00+08:00",
            revision_id="posterior-r2",
            supersedes_revision_id="posterior-r1",
        ),
    ]
    card = plan_due_experience_settlements(candidates=[candidate], evaluation_as_of="2026-06-02T20:00:00+08:00")[
        "items"
    ][0]["actions"][0]["card"]
    assert card["start_price"] == 100
    assert card["end_price"] == 110
    assert card["target_anchor_revision_id"] == "anchor-r1"
    assert card["source_revision_ids"] == ["posterior-r2"]


def test_d14_naive_time_and_force_are_rejected() -> None:
    candidate = _candidate()
    candidate["prediction_bundle"] = _bundle(horizons=(1, 7, 14, 30))
    with pytest.raises(ExperienceSettlementInputError, match=r"D\+14"):
        plan_due_experience_settlements(candidates=[candidate], evaluation_as_of="2026-06-15T20:00:00+08:00")
    with pytest.raises(ExperienceSettlementInputError, match="canonical RFC3339"):
        plan_due_experience_settlements(candidates=[_candidate()], evaluation_as_of="2026-06-02T20:00:00")
    with pytest.raises(TypeError, match="force"):
        plan_due_experience_settlements(
            candidates=[_candidate()],
            evaluation_as_of="2026-06-02T20:00:00+08:00",
            force=True,
        )


def test_input_order_does_not_change_deterministic_plan() -> None:
    candidates = [_candidate("poy"), _candidate("dty")]
    forward = plan_due_experience_settlements(candidates=candidates, evaluation_as_of="2026-06-02T20:00:00+08:00")
    reverse = plan_due_experience_settlements(
        candidates=list(reversed(deepcopy(candidates))),
        evaluation_as_of="2026-06-02T20:00:00+08:00",
    )
    assert forward == reverse


@pytest.mark.parametrize(
    "value",
    [
        "2026-06-02 20:00:00+08:00",
        "2026-06-02T20:00:00+0800",
        "2026-06-02t20:00:00+08:00",
        "2026-06-02T20:00:00+08:99",
    ],
)
def test_evaluation_asof_requires_persistable_canonical_rfc3339(value: str) -> None:
    with pytest.raises(ExperienceSettlementInputError, match="canonical RFC3339"):
        plan_due_experience_settlements(candidates=[_candidate()], evaluation_as_of=value)


def test_prediction_asof_requires_persistable_canonical_rfc3339() -> None:
    candidate = _candidate()
    candidate["prediction_bundle"]["as_of_time"] = "2026-06-01 08:20:00+08:00"
    with pytest.raises(ExperienceSettlementInputError, match="prediction_bundle.as_of_time"):
        plan_due_experience_settlements(candidates=[candidate], evaluation_as_of="2026-06-02T20:00:00+08:00")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda _candidate: [("prediction_bundle", {})], r"candidates\[0\] must be a mapping"),
        (
            lambda candidate: [{**candidate, "prediction_bundle": [("prediction_batch_id", "batch-1")]}],
            "prediction_bundle must be a mapping",
        ),
        (
            lambda candidate: [{**candidate, "target_points": [1]}],
            "target_points items must be mappings",
        ),
        (
            lambda candidate: [{**candidate, "expected_observation_dates": "2026-06-02"}],
            "expected_observation_dates must be a non-string sequence",
        ),
        (
            lambda candidate: [{**candidate, "expected_observation_dates": [1, 2, 3]}],
            "expected_observation_dates items must be strings",
        ),
    ],
)
def test_malformed_adapter_inputs_use_stable_settlement_error(mutate, message: str) -> None:
    with pytest.raises(ExperienceSettlementInputError, match=message):
        plan_due_experience_settlements(
            candidates=mutate(_candidate()),
            evaluation_as_of="2026-06-02T20:00:00+08:00",
        )


def test_d30_head_is_unchanged_until_new_visible_revision_then_emits_one_correction() -> None:
    candidate = _candidate()
    initial = plan_due_experience_settlements(candidates=[candidate], evaluation_as_of="2026-07-01T20:00:00+08:00")
    d30 = initial["items"][0]["actions"][-1]["card"]
    candidate["previous_revision"] = d30

    unchanged = plan_due_experience_settlements(candidates=[candidate], evaluation_as_of="2026-07-02T20:00:00+08:00")
    assert unchanged["items"][0]["status"] == "unchanged"
    assert unchanged["items"][0]["actions"] == []

    bundle = candidate["prediction_bundle"]
    candidate["target_points"].append(
        _point(
            "poy-30-correction",
            bundle["target_series_id"],
            "2026-07-01",
            150,
            visible_at="2026-07-02T10:00:00+08:00",
            revision_id="rev-poy-30-correction",
            supersedes_revision_id="rev-poy-30",
        )
    )
    corrected = plan_due_experience_settlements(candidates=[candidate], evaluation_as_of="2026-07-02T20:00:00+08:00")
    actions = corrected["items"][0]["actions"]
    assert corrected["items"][0]["status"] == "planned"
    assert len(actions) == 1
    assert actions[0]["checkpoint_horizon"] == 30
    assert actions[0]["expected_previous_revision_id"] == d30["revision_id"]
    assert actions[0]["card"]["previous_revision_id"] == d30["revision_id"]


def test_benchmark_change_does_not_create_second_card_identity() -> None:
    first = _candidate()
    second = deepcopy(first)
    second["prediction_bundle"]["benchmark_series_id"] = "other.benchmark"
    with pytest.raises(ExperienceSettlementInputError, match="duplicate settlement candidate identity"):
        plan_due_experience_settlements(candidates=[first, second], evaluation_as_of="2026-06-02T20:00:00+08:00")

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from app.sequential_replay import (
    MAX_ABS_METRIC_VALUE,
    MAX_CHECKPOINTS,
    MAX_COMPLETED_HORIZONS,
    MAX_EVIDENCE_TIMES,
    MAX_EXCLUSION_REASONS,
    MAX_ITEM_ID_LENGTH,
    MAX_ITEMS_PER_CHECKPOINT,
    MAX_METRIC_KEY_LENGTH,
    MAX_METRIC_VALUES,
    MAX_REASON_CODE_LENGTH,
    MAX_TOTAL_ACTIONS,
    MAX_TOTAL_EVIDENCE_TIMES,
    MAX_TOTAL_ITEMS,
    MAX_TOTAL_METRIC_ENTRIES,
    MAX_VERSION_LENGTH,
    SequentialReplayInputError,
    plan_sequential_replay,
)


def _policy(*, run_mode: str = "dry_run", expected_dates: list[str] | None = None) -> dict[str, object]:
    return {
        "policy_version": "sequential-replay-policy.v1",
        "run_mode": run_mode,
        "reconstructed_evidence_handling": "exclude_unscorable",
        "expected_checkpoint_dates": expected_dates or ["2025-01-01"],
    }


def _common(
    item_id: str,
    kind: str,
    *,
    product: str = "poy",
    node_id: str = "pta",
    scoreability: str = "scorable",
    exclusion_reasons: list[str] | None = None,
) -> dict[str, object]:
    return {
        "item_id": item_id,
        "kind": kind,
        "product": product,
        "node_id": node_id,
        "observed_at": "2025-01-01T01:00:00Z",
        "available_at": "2025-01-01T02:00:00Z",
        "evidence_times": ["2025-01-01T02:00:00Z"],
        "evidence_status": "point_in_time",
        "scoreability": scoreability,
        "exclusion_reasons": exclusion_reasons or [],
        "metric_values": {"signal": 0.25},
    }


def _prediction(item_id: str, **overrides: object) -> dict[str, object]:
    item = {**_common(item_id, "prediction"), "horizon": 1}
    item.update(overrides)
    return item


def _settlement(item_id: str, **overrides: object) -> dict[str, object]:
    item = {
        **_common(item_id, "settlement"),
        "matured_horizon": 30,
        "matured_at": "2025-01-01T03:00:00Z",
        "completed_horizons": [],
    }
    item.update(overrides)
    return item


def _shadow(item_id: str, **overrides: object) -> dict[str, object]:
    item = {
        **_common(item_id, "shadow_sample"),
        "horizon": 7,
        "shadow_policy_version": "shadow-promotion.explicit-v1",
    }
    item.update(overrides)
    return item


def _checkpoint(items: list[dict[str, object]], *, as_of: str = "2025-01-01T08:20:00Z") -> dict[str, object]:
    return {"as_of": as_of, "items": items}


def test_dry_run_accepts_both_frozen_boundary_dates() -> None:
    result = plan_sequential_replay(
        checkpoints=[
            _checkpoint([], as_of="2025-01-01T08:20:00+08:00"),
            _checkpoint([], as_of="2026-07-01T08:20:00+08:00"),
        ],
        policy=_policy(expected_dates=["2025-01-01", "2026-07-01"]),
    )

    assert result["replay_range"] == {
        "start": "2025-01-01",
        "end": "2026-07-01",
        "boundaries_inclusive": True,
    }
    assert result["checkpoint_count"] == 2


def test_full_run_uses_explicit_governed_dates_and_includes_both_boundaries() -> None:
    checkpoints = [
        _checkpoint([], as_of="2025-01-01T08:20:00+08:00"),
        _checkpoint([], as_of="2025-01-03T08:20:00+08:00"),
        _checkpoint([], as_of="2026-07-01T08:20:00+08:00"),
    ]
    result = plan_sequential_replay(
        checkpoints=checkpoints,
        policy=_policy(
            run_mode="full",
            expected_dates=["2025-01-01", "2025-01-03", "2026-07-01"],
        ),
    )

    assert result["checkpoint_count"] == 3
    assert result["checkpoints"][0]["replay_date"] == "2025-01-01"
    assert result["checkpoints"][-1]["replay_date"] == "2026-07-01"


def test_full_run_rejects_a_policy_without_both_boundaries() -> None:
    with pytest.raises(SequentialReplayInputError, match="both frozen boundaries"):
        plan_sequential_replay(
            checkpoints=[_checkpoint([], as_of="2025-01-01T08:20:00Z")],
            policy=_policy(run_mode="full"),
        )


@pytest.mark.parametrize(
    "checkpoints, message",
    [
        (
            [
                _checkpoint([], as_of="2025-01-02T08:20:00Z"),
                _checkpoint([], as_of="2025-01-01T08:20:00Z"),
            ],
            "strictly ordered",
        ),
        (
            [
                _checkpoint([], as_of="2025-01-01T08:20:00Z"),
                _checkpoint([], as_of="2025-01-01T09:20:00Z"),
            ],
            "one checkpoint",
        ),
    ],
)
def test_checkpoint_order_and_uniqueness_are_fail_closed(checkpoints: list[dict[str, object]], message: str) -> None:
    with pytest.raises(SequentialReplayInputError, match=message):
        plan_sequential_replay(checkpoints=checkpoints, policy=_policy())


def test_checkpoint_dates_must_match_explicit_policy_without_guessing_a_calendar() -> None:
    with pytest.raises(SequentialReplayInputError, match="explicit policy checkpoint dates"):
        plan_sequential_replay(
            checkpoints=[_checkpoint([], as_of="2025-01-02T08:20:00Z")],
            policy=_policy(expected_dates=["2025-01-01"]),
        )


@pytest.mark.parametrize(
    "field, future_value",
    [
        ("observed_at", "2025-01-01T08:20:01Z"),
        ("available_at", "2025-01-01T08:20:01Z"),
        ("evidence_times", ["2025-01-01T08:20:01Z"]),
    ],
)
def test_any_future_visible_input_is_rejected(field: str, future_value: object) -> None:
    item = _prediction("future")
    item[field] = future_value
    if field == "observed_at":
        item["available_at"] = future_value

    with pytest.raises(SequentialReplayInputError, match="later than the checkpoint"):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


def test_future_maturity_is_rejected() -> None:
    item = _settlement("future-maturity", matured_at="2025-01-01T08:20:01Z")

    with pytest.raises(SequentialReplayInputError, match="later than the checkpoint"):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


@pytest.mark.parametrize(
    "field",
    ["as_of", "observed_at", "available_at", "evidence_times", "matured_at"],
)
def test_negative_zero_rfc3339_offset_is_rejected(field: str) -> None:
    item = _settlement("negative-zero")
    checkpoint = _checkpoint([item])
    if field == "as_of":
        checkpoint["as_of"] = "2025-01-01T08:20:00-00:00"
    elif field == "evidence_times":
        item[field] = ["2025-01-01T02:00:00-00:00"]
    else:
        item[field] = "2025-01-01T02:00:00-00:00"

    with pytest.raises(SequentialReplayInputError, match="canonical RFC3339"):
        plan_sequential_replay(checkpoints=[checkpoint], policy=_policy())


def test_point_in_time_item_requires_at_least_one_evidence_time() -> None:
    item = _prediction("missing-evidence", evidence_times=[])

    with pytest.raises(SequentialReplayInputError, match="must not be empty"):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


def test_reconstructed_item_may_have_no_evidence_time_but_remains_unscorable() -> None:
    item = _prediction("reconstructed-empty", evidence_times=[])
    item["evidence_status"] = "reconstructed"

    result = plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())

    assert result["action_count"] == 0
    assert result["checkpoints"][0]["diagnostics"][0]["status"] == "unscorable"


def test_d14_is_read_only_and_never_emits_an_action() -> None:
    historical = {
        **_common("legacy-d14", "historical_prediction"),
        "horizon": 14,
    }
    result = plan_sequential_replay(
        checkpoints=[_checkpoint([historical])],
        policy=_policy(),
    )

    assert result["action_count"] == 0
    assert result["checkpoints"][0]["diagnostics"] == [
        {
            "item_id": "legacy-d14",
            "kind": "historical_prediction",
            "product": "poy",
            "node_id": "pta",
            "status": "historical_read_only",
            "reason_codes": ["d14_historical_read_only"],
        }
    ]


def test_reconstructed_d14_is_unscorable_before_historical_read_only_classification() -> None:
    historical = {
        **_common("reconstructed-d14", "historical_prediction"),
        "horizon": 14,
        "evidence_status": "reconstructed",
        "evidence_times": [],
    }

    result = plan_sequential_replay(
        checkpoints=[_checkpoint([historical])],
        policy=_policy(),
    )

    assert result["action_count"] == 0
    assert result["checkpoints"][0]["diagnostics"] == [
        {
            "item_id": "reconstructed-d14",
            "kind": "historical_prediction",
            "product": "poy",
            "node_id": "pta",
            "status": "unscorable",
            "reason_codes": ["reconstructed_evidence"],
        }
    ]


def test_explicitly_unscorable_d14_is_not_reclassified_as_historical_read_only() -> None:
    historical = {
        **_common(
            "unscorable-d14",
            "historical_prediction",
            scoreability="unscorable",
            exclusion_reasons=["source_unavailable"],
        ),
        "horizon": 14,
    }

    result = plan_sequential_replay(
        checkpoints=[_checkpoint([historical])],
        policy=_policy(),
    )

    assert result["action_count"] == 0
    assert result["checkpoints"][0]["diagnostics"][0]["status"] == "unscorable"
    assert result["checkpoints"][0]["diagnostics"][0]["reason_codes"] == ["source_unavailable"]


@pytest.mark.parametrize("kind", ["prediction", "shadow_sample"])
def test_d14_cannot_create_new_prediction_or_shadow_actions(kind: str) -> None:
    item = _prediction("new-d14", horizon=14)
    if kind == "shadow_sample":
        item = _shadow("new-d14", horizon=14)

    with pytest.raises(SequentialReplayInputError, match="historical read-only"):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


def test_settlement_catch_up_is_strictly_d1_then_d7_then_d30() -> None:
    result = plan_sequential_replay(
        checkpoints=[_checkpoint([_settlement("late-card")])],
        policy=_policy(),
    )

    actions = result["checkpoints"][0]["actions"]
    assert [item["horizon"] for item in actions] == [1, 7, 30]
    assert {item["action"] for item in actions} == {"plan_experience_settlement"}


def test_settlement_resumes_after_a_valid_completed_prefix() -> None:
    result = plan_sequential_replay(
        checkpoints=[_checkpoint([_settlement("late-card", completed_horizons=[1], matured_horizon=30)])],
        policy=_policy(),
    )

    assert [item["horizon"] for item in result["checkpoints"][0]["actions"]] == [7, 30]


def test_mature_d30_is_rechecked_for_a_possible_same_stage_revision() -> None:
    result = plan_sequential_replay(
        checkpoints=[_checkpoint([_settlement("mature-card", completed_horizons=[1, 7, 30], matured_horizon=30)])],
        policy=_policy(),
    )

    assert result["checkpoints"][0]["actions"][0]["mode"] == "d30_recheck"
    assert result["checkpoints"][0]["actions"][0]["horizon"] == 30


@pytest.mark.parametrize("completed", [[7], [1, 30], [1, 14], [1, 7, 30, 30]])
def test_settlement_rejects_non_prefix_and_historical_completed_stages(
    completed: list[int],
) -> None:
    with pytest.raises(SequentialReplayInputError):
        plan_sequential_replay(
            checkpoints=[_checkpoint([_settlement("bad-prefix", completed_horizons=completed)])],
            policy=_policy(),
        )


def test_blocked_unscorable_and_reconstructed_items_have_stable_exclusions() -> None:
    blocked = _prediction(
        "blocked",
        scoreability="blocked",
        exclusion_reasons=["source_blocked", "source_blocked", "unit_unproven"],
    )
    reconstructed = _prediction("reconstructed")
    reconstructed["evidence_status"] = "reconstructed"
    result = plan_sequential_replay(
        checkpoints=[_checkpoint([reconstructed, blocked])],
        policy=_policy(),
    )

    assert result["action_count"] == 0
    diagnostics = {item["item_id"]: item for item in result["checkpoints"][0]["diagnostics"]}
    assert diagnostics["blocked"]["reason_codes"] == ["source_blocked", "unit_unproven"]
    assert diagnostics["reconstructed"]["status"] == "unscorable"
    assert diagnostics["reconstructed"]["reason_codes"] == ["reconstructed_evidence"]


def test_products_and_nodes_remain_independent_even_with_same_item_id() -> None:
    items = [
        _prediction("same-id", product="dty", node_id="meg"),
        _prediction("same-id", product="poy", node_id="meg"),
        _prediction("same-id", product="poy", node_id="pta"),
    ]
    result = plan_sequential_replay(checkpoints=[_checkpoint(items)], policy=_policy())

    assert [(item["product"], item["node_id"], item["action_count"]) for item in result["groups"]] == [
        ("dty", "meg", 1),
        ("poy", "meg", 1),
        ("poy", "pta", 1),
    ]


def test_duplicate_identity_within_an_isolated_group_is_rejected() -> None:
    with pytest.raises(SequentialReplayInputError, match="duplicate replay item identity"):
        plan_sequential_replay(
            checkpoints=[_checkpoint([_prediction("duplicate"), _prediction("duplicate")])],
            policy=_policy(),
        )


def test_duplicate_identity_across_checkpoints_is_rejected() -> None:
    with pytest.raises(SequentialReplayInputError, match="across checkpoints"):
        plan_sequential_replay(
            checkpoints=[
                _checkpoint([_prediction("duplicate")]),
                _checkpoint([_prediction("duplicate")], as_of="2025-01-02T08:20:00Z"),
            ],
            policy=_policy(expected_dates=["2025-01-01", "2025-01-02"]),
        )


def test_unknown_node_cannot_enter_a_replay_group() -> None:
    with pytest.raises(SequentialReplayInputError, match="formal Phase A node"):
        plan_sequential_replay(
            checkpoints=[_checkpoint([_prediction("unknown", node_id="invented-node")])],
            policy=_policy(),
        )


def test_input_item_order_does_not_change_output_order_or_content() -> None:
    items = [
        _shadow("z-shadow", product="dty", node_id="pta"),
        _settlement("a-settlement", product="poy", node_id="meg"),
        _prediction("m-prediction", product="poy", node_id="pta"),
    ]

    first = plan_sequential_replay(checkpoints=[_checkpoint(items)], policy=_policy())
    second = plan_sequential_replay(checkpoints=[_checkpoint(list(reversed(items)))], policy=_policy())

    assert first == second


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"), True, "1"])
def test_metric_values_reject_non_finite_and_non_numeric_values(value: object) -> None:
    item = _prediction("bad-metric", metric_values={"signal": value})

    with pytest.raises(SequentialReplayInputError, match="finite number"):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


@pytest.mark.parametrize("value", [MAX_ABS_METRIC_VALUE + 1, 1e16, -(MAX_ABS_METRIC_VALUE + 1)])
def test_oversized_numeric_metrics_are_rejected(value: int | float) -> None:
    item = _prediction("large-number", metric_values={"count": value})

    with pytest.raises(SequentialReplayInputError, match="JSON-safe numeric magnitude"):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


def test_metric_numeric_boundaries_are_json_serializable() -> None:
    item = _prediction(
        "numeric-boundaries",
        metric_values={"lower": -MAX_ABS_METRIC_VALUE, "upper": MAX_ABS_METRIC_VALUE},
    )

    result = plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())

    assert json.loads(json.dumps(result, allow_nan=False))["action_count"] == 1


def test_huge_integer_metric_is_stably_rejected() -> None:
    item = _prediction("huge-integer", metric_values={"count": 10**10_000})

    with pytest.raises(SequentialReplayInputError, match="JSON-safe numeric magnitude"):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


def test_checkpoint_limit_accepts_the_entire_frozen_calendar_range() -> None:
    assert MAX_CHECKPOINTS == 547
    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(MAX_CHECKPOINTS)]
    checkpoints = [_checkpoint([], as_of=f"{day.isoformat()}T08:20:00Z") for day in days]

    result = plan_sequential_replay(
        checkpoints=checkpoints,
        policy=_policy(expected_dates=[day.isoformat() for day in days]),
    )

    assert result["checkpoint_count"] == MAX_CHECKPOINTS


def test_checkpoint_limit_rejects_an_oversized_payload_before_date_processing() -> None:
    with pytest.raises(SequentialReplayInputError, match=f"maximum of {MAX_CHECKPOINTS}"):
        plan_sequential_replay(
            checkpoints=[_checkpoint([])] * (MAX_CHECKPOINTS + 1),
            policy=_policy(),
        )


def test_item_evidence_and_metric_collection_limits_accept_boundaries() -> None:
    items = [
        _prediction(
            "item-boundary",
            evidence_times=["2025-01-01T02:00:00Z"] * MAX_EVIDENCE_TIMES,
            metric_values={f"metric-{metric}": metric for metric in range(MAX_METRIC_VALUES)},
        )
    ]

    result = plan_sequential_replay(checkpoints=[_checkpoint(items)], policy=_policy())

    assert result["action_count"] == 1


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"evidence_times": ["2025-01-01T02:00:00Z"] * (MAX_EVIDENCE_TIMES + 1)}, "evidence_times"),
        ({"metric_values": {f"metric-{index}": index for index in range(MAX_METRIC_VALUES + 1)}}, "metric_values"),
    ],
)
def test_item_collection_limits_reject_oversized_payloads(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(SequentialReplayInputError, match=message):
        plan_sequential_replay(
            checkpoints=[_checkpoint([_prediction("oversized", **overrides)])],
            policy=_policy(),
        )


def test_items_per_checkpoint_limit_rejects_oversized_payload() -> None:
    items = [_prediction(f"item-{index}") for index in range(MAX_ITEMS_PER_CHECKPOINT + 1)]

    with pytest.raises(SequentialReplayInputError, match=f"maximum of {MAX_ITEMS_PER_CHECKPOINT}"):
        plan_sequential_replay(checkpoints=[_checkpoint(items)], policy=_policy())


def test_small_item_sequence_limits_accept_exact_boundaries() -> None:
    blocked = _prediction(
        "reason-boundary",
        scoreability="blocked",
        exclusion_reasons=[f"reason_{index}" for index in range(MAX_EXCLUSION_REASONS)],
    )
    settlement = _settlement(
        "completed-boundary",
        completed_horizons=list((1, 7, 30)[:MAX_COMPLETED_HORIZONS]),
    )

    result = plan_sequential_replay(
        checkpoints=[_checkpoint([blocked, settlement])],
        policy=_policy(),
    )

    diagnostics = {item["item_id"]: item for item in result["checkpoints"][0]["diagnostics"]}
    assert len(diagnostics["reason-boundary"]["reason_codes"]) == MAX_EXCLUSION_REASONS
    assert result["action_count"] == 1


def test_reconstructed_system_reason_fits_after_fifteen_ordinary_reasons() -> None:
    item = _prediction(
        "reconstructed-reason-boundary",
        scoreability="blocked",
        exclusion_reasons=[f"reason_{index}" for index in range(MAX_EXCLUSION_REASONS - 1)],
    )
    item["evidence_status"] = "reconstructed"

    result = plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())

    diagnostic = result["checkpoints"][0]["diagnostics"][0]
    assert len(diagnostic["reason_codes"]) == MAX_EXCLUSION_REASONS
    assert "reconstructed_evidence" in diagnostic["reason_codes"]


def test_reconstructed_system_reason_rejects_a_seventeenth_final_reason() -> None:
    item = _prediction(
        "reconstructed-reason-overflow",
        scoreability="blocked",
        exclusion_reasons=[f"reason_{index}" for index in range(MAX_EXCLUSION_REASONS)],
    )
    item["evidence_status"] = "reconstructed"

    with pytest.raises(
        SequentialReplayInputError,
        match=f"exclusion_reasons.*{MAX_EXCLUSION_REASONS} after system reasons",
    ):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


def test_existing_reconstructed_system_reason_is_deduplicated_before_final_limit() -> None:
    item = _prediction(
        "reconstructed-reason-deduplicated",
        scoreability="blocked",
        exclusion_reasons=[
            "reconstructed_evidence",
            *(f"reason_{index}" for index in range(MAX_EXCLUSION_REASONS - 1)),
        ],
    )
    item["evidence_status"] = "reconstructed"

    result = plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())

    reason_codes = result["checkpoints"][0]["diagnostics"][0]["reason_codes"]
    assert len(reason_codes) == MAX_EXCLUSION_REASONS
    assert reason_codes.count("reconstructed_evidence") == 1


@pytest.mark.parametrize(
    "item, message",
    [
        (
            _prediction(
                "too-many-reasons",
                scoreability="blocked",
                exclusion_reasons=[f"reason_{index}" for index in range(MAX_EXCLUSION_REASONS + 1)],
            ),
            "exclusion_reasons",
        ),
        (
            _settlement(
                "too-many-completed",
                completed_horizons=[1] * (MAX_COMPLETED_HORIZONS + 1),
            ),
            "completed_horizons",
        ),
    ],
)
def test_small_item_sequence_limits_reject_oversized_values(
    item: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(SequentialReplayInputError, match=message):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


def test_total_item_budget_accepts_boundary_and_rejects_one_more() -> None:
    days = [date(2025, 1, 1) + timedelta(days=index) for index in range(5)]

    def make_checkpoints(total: int) -> list[dict[str, object]]:
        checkpoints = []
        emitted = 0
        for checkpoint_index, day in enumerate(days):
            count = min(MAX_ITEMS_PER_CHECKPOINT, total - emitted)
            if count <= 0:
                break
            items = [
                _prediction(
                    f"blocked-{checkpoint_index}-{item_index}",
                    scoreability="blocked",
                    exclusion_reasons=["source_blocked"],
                )
                for item_index in range(count)
            ]
            checkpoints.append(_checkpoint(items, as_of=f"{day.isoformat()}T08:20:00Z"))
            emitted += count
        return checkpoints

    boundary_checkpoints = make_checkpoints(MAX_TOTAL_ITEMS)
    result = plan_sequential_replay(
        checkpoints=boundary_checkpoints,
        policy=_policy(expected_dates=[checkpoint["as_of"][:10] for checkpoint in boundary_checkpoints]),
    )
    assert sum(len(checkpoint["diagnostics"]) for checkpoint in result["checkpoints"]) == MAX_TOTAL_ITEMS

    oversized_checkpoints = make_checkpoints(MAX_TOTAL_ITEMS + 1)
    with pytest.raises(SequentialReplayInputError, match=f"total items.*{MAX_TOTAL_ITEMS}"):
        plan_sequential_replay(
            checkpoints=oversized_checkpoints,
            policy=_policy(expected_dates=[checkpoint["as_of"][:10] for checkpoint in oversized_checkpoints]),
        )


def test_total_evidence_timestamp_budget_accepts_boundary_and_rejects_one_more() -> None:
    item_count = MAX_TOTAL_EVIDENCE_TIMES // MAX_EVIDENCE_TIMES
    items = [
        _prediction(
            f"evidence-{index}",
            evidence_times=["2025-01-01T02:00:00Z"] * MAX_EVIDENCE_TIMES,
        )
        for index in range(item_count)
    ]

    result = plan_sequential_replay(checkpoints=[_checkpoint(items)], policy=_policy())
    assert result["action_count"] == item_count

    with pytest.raises(SequentialReplayInputError, match=f"total evidence_times.*{MAX_TOTAL_EVIDENCE_TIMES}"):
        plan_sequential_replay(
            checkpoints=[_checkpoint([*items, _prediction("one-more-evidence")])],
            policy=_policy(),
        )


def test_total_evidence_timestamp_budget_accumulates_across_checkpoints() -> None:
    items_per_half = MAX_TOTAL_EVIDENCE_TIMES // MAX_EVIDENCE_TIMES // 2
    first_items = [
        _prediction(
            f"first-evidence-{index}",
            evidence_times=["2025-01-01T02:00:00Z"] * MAX_EVIDENCE_TIMES,
        )
        for index in range(items_per_half)
    ]
    second_items = [
        _prediction(
            f"second-evidence-{index}",
            evidence_times=["2025-01-01T02:00:00Z"] * MAX_EVIDENCE_TIMES,
        )
        for index in range(items_per_half)
    ]
    second_items.append(_prediction("cross-checkpoint-evidence-overflow"))

    with pytest.raises(SequentialReplayInputError, match=f"total evidence_times.*{MAX_TOTAL_EVIDENCE_TIMES}"):
        plan_sequential_replay(
            checkpoints=[
                _checkpoint(first_items),
                _checkpoint(second_items, as_of="2025-01-02T08:20:00Z"),
            ],
            policy=_policy(expected_dates=["2025-01-01", "2025-01-02"]),
        )


def test_total_metric_entry_budget_accepts_boundary_and_rejects_one_more() -> None:
    item_count = MAX_TOTAL_METRIC_ENTRIES // MAX_METRIC_VALUES
    full_metrics = {f"metric-{index}": index for index in range(MAX_METRIC_VALUES)}
    items = [_prediction(f"metrics-{index}", metric_values=full_metrics) for index in range(item_count)]

    result = plan_sequential_replay(checkpoints=[_checkpoint(items)], policy=_policy())
    assert result["action_count"] == item_count

    with pytest.raises(SequentialReplayInputError, match=f"total metric_entries.*{MAX_TOTAL_METRIC_ENTRIES}"):
        plan_sequential_replay(
            checkpoints=[_checkpoint([*items, _prediction("one-more-metric")])],
            policy=_policy(),
        )


def test_total_metric_entry_budget_accumulates_across_checkpoints() -> None:
    items_per_half = MAX_TOTAL_METRIC_ENTRIES // MAX_METRIC_VALUES // 2
    full_metrics = {f"metric-{index}": index for index in range(MAX_METRIC_VALUES)}
    first_items = [_prediction(f"first-metrics-{index}", metric_values=full_metrics) for index in range(items_per_half)]
    second_items = [
        _prediction(f"second-metrics-{index}", metric_values=full_metrics) for index in range(items_per_half)
    ]
    second_items.append(_prediction("cross-checkpoint-metric-overflow"))

    with pytest.raises(SequentialReplayInputError, match=f"total metric_entries.*{MAX_TOTAL_METRIC_ENTRIES}"):
        plan_sequential_replay(
            checkpoints=[
                _checkpoint(first_items),
                _checkpoint(second_items, as_of="2025-01-02T08:20:00Z"),
            ],
            policy=_policy(expected_dates=["2025-01-01", "2025-01-02"]),
        )


def test_total_action_budget_accepts_boundary_and_rejects_one_more() -> None:
    settlement_count, remainder = divmod(MAX_TOTAL_ACTIONS, 3)
    items = [_settlement(f"settlement-{index}") for index in range(settlement_count)]
    items.extend(_prediction(f"remainder-{index}") for index in range(remainder))
    checkpoints = [
        _checkpoint(
            items[offset : offset + MAX_ITEMS_PER_CHECKPOINT],
            as_of=f"2025-01-{checkpoint_index + 1:02d}T08:20:00Z",
        )
        for checkpoint_index, offset in enumerate(range(0, len(items), MAX_ITEMS_PER_CHECKPOINT))
    ]
    expected_dates = [checkpoint["as_of"][:10] for checkpoint in checkpoints]

    result = plan_sequential_replay(
        checkpoints=checkpoints,
        policy=_policy(expected_dates=expected_dates),
    )
    assert result["action_count"] == MAX_TOTAL_ACTIONS

    last_checkpoint = checkpoints[-1]
    oversized = [
        *checkpoints[:-1],
        {
            **last_checkpoint,
            "items": [*last_checkpoint["items"], _prediction("extra-action")],
        },
    ]
    with pytest.raises(SequentialReplayInputError, match=f"total actions.*{MAX_TOTAL_ACTIONS}"):
        plan_sequential_replay(
            checkpoints=oversized,
            policy=_policy(expected_dates=expected_dates),
        )


@pytest.mark.parametrize(
    "item, policy, message",
    [
        (_prediction("i" * (MAX_ITEM_ID_LENGTH + 1)), _policy(), "item_id"),
        (_prediction("policy"), _policy() | {"policy_version": "p" * (MAX_VERSION_LENGTH + 1)}, "policy_version"),
        (_shadow("shadow", shadow_policy_version="s" * (MAX_VERSION_LENGTH + 1)), _policy(), "shadow_policy_version"),
        (
            _prediction(
                "reason",
                scoreability="blocked",
                exclusion_reasons=["r" * (MAX_REASON_CODE_LENGTH + 1)],
            ),
            _policy(),
            "exclusion_reasons",
        ),
        (
            _prediction("metric-key", metric_values={"m" * (MAX_METRIC_KEY_LENGTH + 1): 1}),
            _policy(),
            "metric_values",
        ),
    ],
)
def test_governed_text_limits_reject_oversized_values(
    item: dict[str, object],
    policy: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(SequentialReplayInputError, match=message):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=policy)


def test_governed_text_limits_accept_exact_boundaries() -> None:
    item = _shadow(
        "i" * MAX_ITEM_ID_LENGTH,
        shadow_policy_version="s" * MAX_VERSION_LENGTH,
        metric_values={"m" * MAX_METRIC_KEY_LENGTH: 1},
    )
    blocked = _prediction(
        "blocked",
        scoreability="blocked",
        exclusion_reasons=["r" * MAX_REASON_CODE_LENGTH],
    )

    result = plan_sequential_replay(
        checkpoints=[_checkpoint([item, blocked])],
        policy=_policy() | {"policy_version": "p" * MAX_VERSION_LENGTH},
    )

    assert result["action_count"] == 1


def test_exclusion_reasons_are_codes_not_free_text() -> None:
    item = _prediction(
        "bad-reason",
        scoreability="blocked",
        exclusion_reasons=["contains free text"],
    )

    with pytest.raises(SequentialReplayInputError, match="stable reason codes"):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


@pytest.mark.parametrize(
    "mutator",
    [
        lambda item: item.update(horizon=True),
        lambda item: item.update(evidence_times="2025-01-01T02:00:00Z"),
        lambda item: item.update(exclusion_reasons="blocked"),
        lambda item: item.update(product=1),
        lambda item: item.update(unknown_field="hidden"),
    ],
)
def test_malicious_or_ambiguous_adapter_types_fail_closed(mutator: object) -> None:
    item = _prediction("hostile")
    mutator(item)  # type: ignore[operator]

    with pytest.raises(SequentialReplayInputError):
        plan_sequential_replay(checkpoints=[_checkpoint([item])], policy=_policy())


def test_policy_must_be_explicit_versioned_and_cannot_guess_reconstruction_handling() -> None:
    bad_policies = [
        {},
        {"policy_version": "v1", "run_mode": "dry_run"},
        {
            "policy_version": "v1",
            "run_mode": "dry_run",
            "reconstructed_evidence_handling": "include",
        },
        {
            "policy_version": "",
            "run_mode": "dry_run",
            "reconstructed_evidence_handling": "exclude_unscorable",
        },
    ]

    for bad_policy in bad_policies:
        with pytest.raises(SequentialReplayInputError):
            plan_sequential_replay(checkpoints=[_checkpoint([])], policy=bad_policy)


def test_output_is_a_pure_adapter_plan_and_names_downstream_boundaries() -> None:
    result = plan_sequential_replay(
        checkpoints=[
            _checkpoint(
                [
                    _prediction("prediction"),
                    _settlement("settlement", completed_horizons=[1, 7]),
                    _shadow("shadow"),
                ]
            )
        ],
        policy=_policy(),
    )

    assert [item["action"] for item in result["checkpoints"][0]["actions"]] == [
        "freeze_prediction",
        "plan_experience_settlement",
        "assemble_shadow_sample",
    ]
    assert result["schema_version"] == "sequential-replay-plan.v1"

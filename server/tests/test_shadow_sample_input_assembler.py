from __future__ import annotations

import copy
import hashlib

import pytest

from app.shadow_factor_lifecycle import evaluate_shadow_factor_lifecycle
from app.shadow_sample_input_assembler import ShadowSampleAssemblyError, assemble_shadow_sample_input


def _action(*, horizon: int = 7, evidence_status: str = "point_in_time") -> dict[str, object]:
    as_of = "2026-01-20T08:20:00+08:00"
    source_item_id = "shadow-source-1"
    node_id = "pta"
    return {
        "as_of": as_of,
        "source_item_id": source_item_id,
        "product": "poy",
        "node_id": node_id,
        "observed_at": "2026-01-19T08:20:00+08:00",
        "available_at": "2026-01-19T09:30:00+08:00",
        "evidence_times": ["2026-01-19T08:20:00+08:00", "2026-01-19T09:30:00+08:00"],
        "evidence_status": evidence_status,
        "metric_values": {"confidence": 0.75, "magnitude": 1.5},
        "replay_policy_version": "sequential-replay.test-v1",
        "action": "assemble_shadow_sample",
        "horizon": horizon,
        "shadow_policy_version": "shadow-policy.proposed-v1",
        "idempotency_key": [as_of, "poy", node_id, "assemble_shadow_sample", source_item_id, horizon],
    }


def _window() -> dict[str, str]:
    return {
        "window_id": "w1",
        "train_end": "2026-01-01T00:00:00+00:00",
        "evaluation_start": "2026-01-02T00:00:00+00:00",
        "evaluation_end": "2026-02-28T00:00:00+00:00",
    }


def _projection(*, role: str, status: str = "unaudited", evidence_status: str = "point_in_time") -> dict[str, object]:
    if role == "outcome":
        observed_at = "2026-01-20T07:00:00+08:00"
        available_at = "2026-01-20T07:30:00+08:00"
    else:
        observed_at = "2026-01-18T08:20:00+08:00"
        available_at = "2026-01-19T07:30:00+08:00"
    return {
        "revision_id": f"{role}-revision-1",
        "product": "poy",
        "node_id": "pta",
        "horizon": 7,
        "observed_at": observed_at,
        "available_at": available_at,
        "evidence_status": evidence_status,
        "review_status": status,
        "payload_sha256": hashlib.sha256(role.encode()).hexdigest(),
    }


def _policy() -> dict[str, object]:
    return {
        "policy_version": "shadow-policy.proposed-v1",
        "minimum_window_gain": 0.1,
        "maximum_high_confidence_error_rate_delta": 0.0,
        "high_confidence_threshold": 0.8,
        "minimum_high_confidence_paired_samples": 12,
    }


def _assemble(**overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "action": _action(),
        "factor_id": "factor-pta-test",
        "baseline_id": "transparent-baseline-test",
        "baseline_is_transparent": True,
        "window": _window(),
        "factor_projection": _projection(role="factor"),
        "baseline_projection": _projection(role="baseline", status="unapproved"),
        "outcome_projection": _projection(role="outcome"),
        "metric_contract_version": "shadow-error.proposed-v1",
        "policy": _policy(),
    }
    arguments.update(overrides)
    return assemble_shadow_sample_input(**arguments)


def test_caller_supplied_projections_remain_blocked_and_handoff_directly() -> None:
    assembled = _assemble()

    assert assembled["assembly_status"] == "blocked"
    assert assembled["reason_codes"] == [
        "baseline_projection_unapproved",
        "factor_projection_unaudited",
        "metric_contract_unapproved",
        "outcome_binding_unverified",
        "outcome_projection_unaudited",
        "replay_action_provenance_unverified",
    ]
    lifecycle = evaluate_shadow_factor_lifecycle(**assembled["lifecycle_input"])
    assert lifecycle["promotion_eligible"] is False
    assert lifecycle["scorable_sample_count"] == 0
    assert lifecycle["next_status"] == "shadow"


def test_missing_inputs_report_stable_blockers_without_projection_values() -> None:
    assembled = _assemble(
        factor_projection=None,
        baseline_projection=None,
        outcome_projection=None,
        metric_contract_version=None,
        policy=None,
    )

    assert assembled["assembly_status"] == "blocked"
    assert assembled["reason_codes"] == [
        "baseline_projection_missing",
        "factor_projection_missing",
        "lifecycle_policy_missing",
        "metric_contract_missing",
        "outcome_projection_missing",
        "replay_action_provenance_unverified",
    ]
    assert set(assembled["lifecycle_input"]["samples"][0]) == {
        "sample_id",
        "window_id",
        "scoreability",
        "exclusion_reasons",
    }


@pytest.mark.parametrize("horizon", [1, 7, 30])
def test_formal_horizons_are_preserved(horizon: int) -> None:
    action = _action(horizon=horizon)
    projections = []
    for role in ("factor", "baseline", "outcome"):
        projection = _projection(role=role)
        projection["horizon"] = horizon
        projections.append(projection)
    assembled = _assemble(
        action=action,
        factor_projection=projections[0],
        baseline_projection=projections[1],
        outcome_projection=projections[2],
    )
    assert assembled["lineage"]["horizon"] == horizon


def test_d14_is_rejected() -> None:
    action = _action()
    action["horizon"] = 14
    action["idempotency_key"][-1] = 14
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_horizon_invalid"):
        _assemble(action=action)


def test_reconstructed_evidence_is_unscorable_even_with_other_blockers() -> None:
    assembled = _assemble(action=_action(evidence_status="reconstructed"))
    sample = assembled["lifecycle_input"]["samples"][0]
    assert assembled["assembly_status"] == "unscorable"
    assert sample["scoreability"] == "unscorable"
    assert "reconstructed_evidence" in sample["exclusion_reasons"]


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("product", "shadow_product_mismatch"),
        ("node_id", "shadow_node_mismatch"),
        ("horizon", "shadow_horizon_mismatch"),
    ],
)
def test_projection_identity_mismatch_fails_closed(field: str, code: str) -> None:
    projection = _projection(role="factor")
    projection[field] = {"product": "dty", "node_id": "meg", "horizon": 30}[field]
    with pytest.raises(ShadowSampleAssemblyError, match=code):
        _assemble(factor_projection=projection)


def test_future_action_or_projection_evidence_fails_closed() -> None:
    action = _action()
    action["evidence_times"] = ["2026-01-21T08:20:00+08:00"]
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_evidence_after_as_of"):
        _assemble(action=action)

    projection = _projection(role="factor")
    projection["available_at"] = "2026-01-21T08:20:00+08:00"
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_evidence_after_as_of"):
        _assemble(factor_projection=projection)


def test_window_and_projection_timeline_mismatch_fail_closed() -> None:
    window = _window()
    window["evaluation_start"] = "2026-01-20T00:00:00+00:00"
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_window_mismatch"):
        _assemble(window=window)

    factor = _projection(role="factor")
    factor["available_at"] = "2026-01-19T09:30:00+08:00"
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_prediction_time_mismatch"):
        _assemble(factor_projection=factor)

    outcome = _projection(role="outcome")
    outcome["observed_at"] = "2026-01-18T08:20:00+08:00"
    outcome["available_at"] = "2026-01-19T08:00:00+08:00"
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_outcome_time_mismatch"):
        _assemble(outcome_projection=outcome)


def test_action_and_projection_shapes_are_closed() -> None:
    action = _action()
    action["hidden"] = "not-audited"
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_action_invalid"):
        _assemble(action=action)

    projection = _projection(role="factor")
    projection["factor_error"] = 0.0
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_projection_invalid"):
        _assemble(factor_projection=projection)


def test_caller_cannot_self_attest_projection_as_audited() -> None:
    projection = _projection(role="factor")
    projection["review_status"] = "audited"
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_projection_review_status_invalid"):
        _assemble(factor_projection=projection)


def test_policy_version_must_match_replay_action() -> None:
    policy = _policy()
    policy["policy_version"] = "different"
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_policy_version_mismatch"):
        _assemble(policy=policy)


def test_lifecycle_state_and_retirement_cannot_be_caller_overridden() -> None:
    with pytest.raises(TypeError, match="current_status"):
        _assemble(current_status="promoted")
    with pytest.raises(TypeError, match="retirement_requested"):
        _assemble(retirement_requested=True)

    assembled = _assemble()
    assert assembled["lifecycle_input"]["current_status"] == "shadow"
    assert assembled["lifecycle_input"]["retirement_requested"] is False


def test_idempotency_key_must_match_action_identity() -> None:
    action = _action()
    action["idempotency_key"][1] = "dty"
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_idempotency_key_mismatch"):
        _assemble(action=action)


def test_nonfinite_and_oversized_action_inputs_fail_with_bounded_codes() -> None:
    action = _action()
    action["metric_values"] = {"confidence": float("nan")}
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_action_resource_limit"):
        _assemble(action=action)

    action = _action()
    action["source_item_id"] = "x" * 4_097
    with pytest.raises(ShadowSampleAssemblyError) as error:
        _assemble(action=action)
    assert error.value.code.startswith("shadow_")
    assert len(error.value.code) <= 120


def test_deep_cyclic_and_whitespace_inputs_fail_at_bounded_boundary() -> None:
    action = _action()
    nested: object = 1
    for _ in range(20):
        nested = [nested]
    action["metric_values"] = {"nested": nested}
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_action_resource_limit"):
        _assemble(action=action)

    action = _action()
    cycle: list[object] = []
    cycle.append(cycle)
    action["metric_values"] = {"cycle": cycle}
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_action_resource_limit"):
        _assemble(action=action)

    action = _action()
    action["source_item_id"] = " shadow-source-1 "
    action["idempotency_key"][4] = " shadow-source-1 "
    with pytest.raises(ShadowSampleAssemblyError, match="shadow_source_item_id_invalid"):
        _assemble(action=action)


def test_equivalent_inputs_are_deterministic_and_inputs_are_not_mutated() -> None:
    action = _action()
    factor = _projection(role="factor")
    before = copy.deepcopy((action, factor))

    first = _assemble(action=action, factor_projection=factor)
    second = _assemble(action=copy.deepcopy(action), factor_projection=copy.deepcopy(factor))

    assert first == second
    assert (action, factor) == before
    assert first["lineage"]["lifecycle_input_sha256"] == hashlib.sha256(
        __import__("json").dumps(
            first["lifecycle_input"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


@pytest.mark.parametrize("mutation", ["payload", "time", "status", "window"])
def test_sample_identity_binds_full_projection_and_window(mutation: str) -> None:
    original = _assemble()
    factor = _projection(role="factor")
    window = _window()
    if mutation == "payload":
        factor["payload_sha256"] = hashlib.sha256(b"changed").hexdigest()
    elif mutation == "time":
        factor["available_at"] = "2026-01-19T07:31:00+08:00"
    elif mutation == "status":
        factor["review_status"] = "unapproved"
    else:
        window["evaluation_end"] = "2026-03-01T00:00:00+00:00"

    changed = _assemble(factor_projection=factor, window=window)
    assert changed["lifecycle_input"]["samples"][0]["sample_id"] != original["lifecycle_input"]["samples"][0][
        "sample_id"
    ]
    if mutation == "window":
        assert changed["lineage"]["window_sha256"] != original["lineage"]["window_sha256"]
    else:
        assert changed["lineage"]["projection_sha256"]["factor"] != original["lineage"]["projection_sha256"][
            "factor"
        ]


def test_equivalent_z_and_offset_action_times_share_canonical_identity() -> None:
    action_z = _action()
    action_z["as_of"] = "2026-01-20T00:20:00Z"
    action_z["idempotency_key"][0] = "2026-01-20T00:20:00Z"
    action_offset = copy.deepcopy(action_z)
    action_offset["as_of"] = "2026-01-20T00:20:00+00:00"
    action_offset["idempotency_key"][0] = "2026-01-20T00:20:00+00:00"

    first = _assemble(action=action_z)
    second = _assemble(action=action_offset)
    assert first["lineage"]["action_sha256"] == second["lineage"]["action_sha256"]
    assert first["lifecycle_input"]["samples"][0]["sample_id"] == second["lifecycle_input"]["samples"][0][
        "sample_id"
    ]

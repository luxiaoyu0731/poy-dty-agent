from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from hashlib import sha256

import pytest

from app import formal_series_eligibility as eligibility
from app.formal_series_eligibility import (
    APPROVED_MANIFEST_VERSION,
    ELIGIBILITY_POLICY_VERSION,
    EVIDENCE_BUNDLE_VERSION,
    FORMAL_SERIES_IDS,
    GATE_APPROVAL_VERSION,
    GATE_ASSERTIONS,
    GATE_NAMES,
    RESULT_SCHEMA_VERSION,
    FormalSeriesEligibilityInputError,
    evaluate_formal_series_eligibility,
)

ASSESSMENT_AS_OF = "2026-06-30T08:20:00+08:00"


class _ExplodingMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise RuntimeError("private-mapping-secret")

    def __iter__(self):
        raise RuntimeError("private-mapping-secret")

    def __len__(self) -> int:
        raise RuntimeError("private-mapping-secret")

    def items(self):
        raise RuntimeError("private-mapping-secret")

    def keys(self):
        raise RuntimeError("private-mapping-secret")


class _ExplodingSequence(Sequence[object]):
    def __getitem__(self, index: int) -> object:
        raise RuntimeError("private-sequence-secret")

    def __iter__(self):
        raise RuntimeError("private-sequence-secret")

    def __len__(self) -> int:
        raise RuntimeError("private-sequence-secret")


def _numeric_checks(gate_name: str) -> dict[str, int]:
    if gate_name == "freshness":
        return {"age_seconds": 60, "max_age_seconds": 3600}
    if gate_name == "visibility":
        return {"visible_age_seconds": 300}
    return {"evidence_count": 1}


def _gate(gate_name: str, series_id: str) -> dict[str, object]:
    return {
        "status": "passed",
        "policy_version": ELIGIBILITY_POLICY_VERSION,
        "series_id": series_id,
        "evidence_id": f"immutable-proof:{gate_name}:v1",
        "valid_from": "2026-01-01T00:00:00+08:00",
        "valid_through": "2026-12-31T23:59:59+08:00",
        "applicable_horizons": [1, 7, 30],
        "assertions": {name: True for name in GATE_ASSERTIONS[gate_name]},
        "numeric_checks": _numeric_checks(gate_name),
    }


def _record(
    series_id: str = FORMAL_SERIES_IDS[0],
    *,
    horizon_days: int = 1,
) -> dict[str, object]:
    return {
        "series_id": series_id,
        "horizon_days": horizon_days,
        "evidence_bundle": {
            "bundle_version": EVIDENCE_BUNDLE_VERSION,
            "series_id": series_id,
            "gates": {gate_name: _gate(gate_name, series_id) for gate_name in GATE_NAMES},
        },
    }


def _gate_digest(gate: dict[str, object]) -> str:
    try:
        encoded = json.dumps(
            gate,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, OverflowError):
        return "0" * 64
    return sha256(encoded).hexdigest()


def _approved_manifest(
    records: tuple[dict[str, object], ...],
    *,
    series_ids: tuple[str, ...] = FORMAL_SERIES_IDS,
) -> dict[str, object]:
    approvals_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for record in records:
        series_id = record.get("series_id")
        if series_id not in series_ids:
            continue
        bundle = record.get("evidence_bundle")
        if not isinstance(bundle, dict) or not isinstance(bundle.get("gates"), dict):
            continue
        for gate_name, gate in bundle["gates"].items():
            if gate_name not in GATE_NAMES or not isinstance(gate, dict):
                continue
            approvals_by_key[(series_id, gate_name)] = {
                "series_id": series_id,
                "gate_name": gate_name,
                "evidence_id": gate.get("evidence_id"),
                "gate_facts_digest": _gate_digest(gate),
                "approval_version": GATE_APPROVAL_VERSION,
                "decision": "approved",
                "valid_from": gate.get("valid_from"),
                "valid_through": gate.get("valid_through"),
                "applicable_horizons": gate.get("applicable_horizons"),
            }
    return {
        "manifest_version": APPROVED_MANIFEST_VERSION,
        "policy_version": ELIGIBILITY_POLICY_VERSION,
        "approvals": [approvals_by_key[key] for key in sorted(approvals_by_key)],
    }


def _manifest_digest(manifest: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _evaluate_with_manifest(
    records: tuple[dict[str, object], ...],
    manifest: dict[str, object] | None,
    *,
    policy_version: str = ELIGIBILITY_POLICY_VERSION,
):
    return evaluate_formal_series_eligibility(
        list(records),
        assessment_as_of=ASSESSMENT_AS_OF,
        policy_version=policy_version,
        approved_evidence_manifest=manifest,
    )


def _evaluate(
    *records: dict[str, object],
    policy_version: str = ELIGIBILITY_POLICY_VERSION,
    include_approved_manifest: bool = True,
):
    manifest = _approved_manifest(records) if include_approved_manifest else None
    return _evaluate_with_manifest(
        records,
        manifest,
        policy_version=policy_version,
    )


def test_closed_result_schema_and_single_series_can_become_eligible() -> None:
    result = _evaluate(_record())

    assert set(result) == {
        "schema_version",
        "policy_version",
        "approved_manifest_digest",
        "assessment_as_of",
        "summary",
        "results",
    }
    assert result["schema_version"] == RESULT_SCHEMA_VERSION
    assert result["approved_manifest_digest"] is not None
    assert result["summary"] == {
        "record_count": 1,
        "eligible_count": 1,
        "blocked_count": 0,
    }
    item = result["results"][0]
    assert set(item) == {
        "series_id",
        "horizon_days",
        "eligibility_status",
        "blocked_reasons",
        "gate_statuses",
    }
    assert item["eligibility_status"] == "eligible"
    assert item["blocked_reasons"] == []
    assert item["gate_statuses"] == dict.fromkeys(GATE_NAMES, "passed")


def test_historical_v4_replay_keeps_sc_in_its_frozen_matrix() -> None:
    records = tuple(
        _record(series_id, horizon_days=horizon)
        for series_id in eligibility.V4_FORMAL_SERIES_IDS
        for horizon in eligibility.FORMAL_HORIZONS
    )
    manifest = _approved_manifest(records, series_ids=eligibility.V4_FORMAL_SERIES_IDS)

    result = eligibility._replay_formal_series_eligibility(
        list(records),
        assessment_as_of=ASSESSMENT_AS_OF,
        policy_version=ELIGIBILITY_POLICY_VERSION,
        approved_evidence_manifest=manifest,
        contract_version="phase-a.v4",
    )

    assert result["summary"] == {"record_count": 60, "eligible_count": 60, "blocked_count": 0}
    assert [item["series_id"] for item in result["results"][6:9]] == [
        "crude.sc.ine.main.settlement.cny_bbl"
    ] * 3


def test_complete_caller_claims_without_approved_manifest_remain_blocked() -> None:
    coal = _record("coal.benchmark.unresolved.assessment.cny_mt")

    item = _evaluate(coal, include_approved_manifest=False)["results"][0]

    assert item["eligibility_status"] == "blocked"
    assert item["blocked_reasons"][0] == "approved_evidence_manifest_missing"
    assert all(f"{gate_name}_approval_missing" in item["blocked_reasons"] for gate_name in GATE_NAMES)


def test_caller_manifest_is_accepted_without_any_trust_parameter() -> None:
    record = _record()
    manifest = _approved_manifest((record,))

    result = _evaluate_with_manifest((record,), manifest)

    assert result["approved_manifest_digest"] == _manifest_digest(manifest)
    assert result["results"][0]["eligibility_status"] == "eligible"


def test_deployment_frozen_manifest_digest_and_gate_facts_are_both_bound() -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    record["evidence_bundle"]["gates"]["freshness"]["numeric_checks"]["age_seconds"] = 61

    result = _evaluate_with_manifest((record,), manifest)

    assert result["results"][0]["blocked_reasons"] == ["freshness_approval_facts_mismatch"]
    assert result["approved_manifest_digest"] == _manifest_digest(manifest)


@pytest.mark.parametrize("mutation", ["missing_field", "extra_field", "manifest_version", "policy_version"])
def test_manifest_schema_and_versions_fail_closed_after_trust_lookup(mutation: str) -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    if mutation == "missing_field":
        del manifest["manifest_version"]
    elif mutation == "extra_field":
        manifest["unexpected"] = True
    elif mutation == "manifest_version":
        manifest["manifest_version"] = "formal-series-approved-evidence.v2"
    else:
        manifest["policy_version"] = "formal-series-eligibility.v2"

    item = _evaluate_with_manifest((record,), manifest)["results"][0]

    assert item["blocked_reasons"][0] == "approved_evidence_manifest_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [("decision", "rejected"), ("approval_version", "formal-series-gate-approval.v2")],
)
def test_approval_decision_and_version_are_frozen(field: str, value: str) -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    manifest["approvals"][0][field] = value

    item = _evaluate_with_manifest((record,), manifest)["results"][0]

    assert item["blocked_reasons"][0] == "approved_evidence_manifest_invalid"


def test_duplicate_approval_is_invalid_instead_of_last_write_wins() -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    manifest["approvals"].append(deepcopy(manifest["approvals"][0]))

    item = _evaluate_with_manifest((record,), manifest)["results"][0]

    assert item["blocked_reasons"][0] == "approved_evidence_manifest_invalid"


def test_uncanonicalizable_manifest_is_invalid_and_never_echoed() -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    manifest["unexpected"] = {"private-token"}

    result = evaluate_formal_series_eligibility(
        [record],
        assessment_as_of=ASSESSMENT_AS_OF,
        approved_evidence_manifest=manifest,
    )

    assert result["results"][0]["blocked_reasons"][0] == "approved_evidence_manifest_invalid"
    assert "private-token" not in repr(result)


@pytest.mark.parametrize("failure", [RecursionError, MemoryError])
def test_manifest_canonical_resource_failures_are_stably_invalid(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    record = _record()
    manifest = _approved_manifest((record,))

    def fail_canonicalization(*args: object, **kwargs: object) -> str:
        raise failure("private-resource-detail")

    monkeypatch.setattr(eligibility, "dumps", fail_canonicalization)
    result = evaluate_formal_series_eligibility(
        [record],
        assessment_as_of=ASSESSMENT_AS_OF,
        approved_evidence_manifest=manifest,
    )

    assert result["results"][0]["blocked_reasons"][0] == "approved_evidence_manifest_invalid"
    assert "private-resource-detail" not in repr(result)


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_manifest_canonical_process_control_exceptions_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    record = _record()
    manifest = _approved_manifest((record,))

    def interrupt_canonicalization(*args: object, **kwargs: object) -> str:
        raise failure()

    monkeypatch.setattr(eligibility, "dumps", interrupt_canonicalization)
    with pytest.raises(failure):
        evaluate_formal_series_eligibility(
            [record],
            assessment_as_of=ASSESSMENT_AS_OF,
            approved_evidence_manifest=manifest,
        )


@pytest.mark.parametrize("failure", [RecursionError, MemoryError])
def test_gate_canonical_resource_failures_become_stable_facts_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    failure: type[BaseException],
) -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    canonical_calls = 0

    def fail_after_manifest(value: object, **kwargs: object) -> str:
        nonlocal canonical_calls
        canonical_calls += 1
        if canonical_calls == 1:
            return json.dumps(value, **kwargs)
        raise failure("private-gate-resource-detail")

    monkeypatch.setattr(eligibility, "dumps", fail_after_manifest)
    result = evaluate_formal_series_eligibility(
        [record],
        assessment_as_of=ASSESSMENT_AS_OF,
        approved_evidence_manifest=manifest,
    )

    assert result["results"][0]["blocked_reasons"] == [
        f"{gate_name}_approval_facts_mismatch" for gate_name in GATE_NAMES
    ]
    assert "private-gate-resource-detail" not in repr(result)


@pytest.mark.parametrize("resource", ["depth", "nodes", "string", "total_strings", "collection"])
def test_manifest_resource_limits_reject_before_canonical_json(
    monkeypatch: pytest.MonkeyPatch,
    resource: str,
) -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    if resource == "depth":
        nested: dict[str, object] = {}
        manifest["unexpected"] = nested
        for _ in range(eligibility.MAX_CANONICAL_DEPTH + 1):
            child: dict[str, object] = {}
            nested["child"] = child
            nested = child
    elif resource == "nodes":
        manifest["unexpected"] = [
            [None] * eligibility.MAX_CANONICAL_COLLECTION_ITEMS
            for _ in range(eligibility.MAX_CANONICAL_NODES // eligibility.MAX_CANONICAL_COLLECTION_ITEMS + 2)
        ]
    elif resource == "string":
        manifest["unexpected"] = "x" * (eligibility.MAX_CANONICAL_STRING_LENGTH + 1)
    elif resource == "total_strings":
        manifest["unexpected"] = [["x" * eligibility.MAX_CANONICAL_STRING_LENGTH for _ in range(70)] for _ in range(2)]
    else:
        manifest["approvals"].extend(
            deepcopy(manifest["approvals"][0])
            for _ in range(eligibility.MAX_MANIFEST_APPROVALS - len(manifest["approvals"]) + 1)
        )

    def unexpected_canonicalization(*args: object, **kwargs: object) -> str:
        raise AssertionError("canonicalization_must_not_run")

    monkeypatch.setattr(eligibility, "_canonical_manifest_digest", unexpected_canonicalization)
    result = evaluate_formal_series_eligibility(
        [record],
        assessment_as_of=ASSESSMENT_AS_OF,
        approved_evidence_manifest=manifest,
    )

    assert result["results"][0]["blocked_reasons"][0] == "approved_evidence_manifest_invalid"


def test_deep_gate_extra_is_blocked_without_recursive_canonicalization() -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    gate = record["evidence_bundle"]["gates"]["source_authorization"]
    nested: dict[str, object] = {}
    gate["unexpected"] = nested
    for _ in range(eligibility.MAX_CANONICAL_DEPTH + 1):
        child: dict[str, object] = {}
        nested["child"] = child
        nested = child

    item = _evaluate_with_manifest((record,), manifest)["results"][0]

    assert "invalid_source_authorization_evidence" in item["blocked_reasons"]
    assert "source_authorization_approval_facts_mismatch" in item["blocked_reasons"]


def test_approval_expiry_is_checked_independently_of_gate_expiry() -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    approval = next(item for item in manifest["approvals"] if item["gate_name"] == "source_authorization")
    approval["valid_through"] = "2026-06-29T23:59:59+08:00"

    item = _evaluate_with_manifest((record,), manifest)["results"][0]

    assert item["blocked_reasons"] == ["source_authorization_approval_not_valid_at_assessment"]


def test_approval_and_gate_horizon_sets_must_match_even_when_both_cover_request() -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    approval = next(item for item in manifest["approvals"] if item["gate_name"] == "source_authorization")
    approval["applicable_horizons"] = [1, 7]

    item = _evaluate_with_manifest((record,), manifest)["results"][0]

    assert item["blocked_reasons"] == ["source_authorization_approval_horizons_mismatch"]


def test_complete_19_matrix_is_eligible_without_any_manifest_authorization() -> None:
    records = tuple(_record(series_id) for series_id in FORMAL_SERIES_IDS)
    manifest = _approved_manifest(records)

    result = evaluate_formal_series_eligibility(
        list(records),
        assessment_as_of=ASSESSMENT_AS_OF,
        approved_evidence_manifest=manifest,
    )

    assert len(FORMAL_SERIES_IDS) == 19
    assert result["summary"] == {
        "record_count": 19,
        "eligible_count": 19,
        "blocked_count": 0,
    }
    assert all(item["eligibility_status"] == "eligible" for item in result["results"])
    assert result["approved_manifest_digest"] == _manifest_digest(manifest)


def test_frozen_record_and_approval_budgets_accept_the_complete_19_by_3_matrix() -> None:
    records = tuple(
        _record(series_id, horizon_days=horizon)
        for series_id in FORMAL_SERIES_IDS
        for horizon in eligibility.FORMAL_HORIZONS
    )
    manifest = _approved_manifest(records)

    result = _evaluate_with_manifest(records, manifest)

    assert len(records) == eligibility.MAX_RECORDS == 57
    assert len(manifest["approvals"]) == eligibility.MAX_MANIFEST_APPROVALS == 114
    assert result["summary"] == {
        "record_count": 57,
        "eligible_count": 57,
        "blocked_count": 0,
    }


def test_record_budget_rejects_before_record_normalization() -> None:
    records = [_record() for _ in range(eligibility.MAX_RECORDS + 1)]

    with pytest.raises(FormalSeriesEligibilityInputError, match="records_limit_exceeded"):
        evaluate_formal_series_eligibility(records, assessment_as_of=ASSESSMENT_AS_OF)


@pytest.mark.parametrize("gate_name", GATE_NAMES)
def test_each_governance_gate_blocks_independently(gate_name: str) -> None:
    record = _record()
    record["evidence_bundle"]["gates"][gate_name]["status"] = "failed"

    item = _evaluate(record)["results"][0]

    assert item["eligibility_status"] == "blocked"
    assert item["blocked_reasons"] == [f"{gate_name}_failed"]
    assert item["gate_statuses"][gate_name] == "blocked"


@pytest.mark.parametrize("status", ["conflict", "unknown"])
def test_conflict_and_unknown_are_stable_fail_closed_states(status: str) -> None:
    record = _record()
    record["evidence_bundle"]["gates"]["source_authorization"]["status"] = status

    item = _evaluate(record)["results"][0]

    assert item["blocked_reasons"] == [f"source_authorization_{status}"]


def test_missing_gate_and_false_assertion_cannot_be_replaced_by_pass_status() -> None:
    missing = _record()
    del missing["evidence_bundle"]["gates"]["visibility"]
    false_assertion = _record(FORMAL_SERIES_IDS[1])
    false_assertion["evidence_bundle"]["gates"]["freshness"]["assertions"]["within_freshness_threshold"] = False

    results = _evaluate(missing, false_assertion)["results"]

    assert results[0]["blocked_reasons"] == ["missing_visibility_evidence"]
    assert results[1]["blocked_reasons"] == ["freshness_assertion_failed"]


def test_mixed_batch_is_sorted_by_frozen_series_order_not_input_order() -> None:
    eligible = _record(FORMAL_SERIES_IDS[0], horizon_days=30)
    blocked = _record(FORMAL_SERIES_IDS[-1], horizon_days=7)
    blocked["evidence_bundle"]["gates"]["trading_calendar"]["status"] = "conflict"

    result = _evaluate(blocked, eligible)

    assert [item["series_id"] for item in result["results"]] == [
        FORMAL_SERIES_IDS[0],
        FORMAL_SERIES_IDS[-1],
    ]
    assert result["summary"] == {
        "record_count": 2,
        "eligible_count": 1,
        "blocked_count": 1,
    }


def test_duplicate_known_series_horizon_blocks_every_duplicate() -> None:
    first = _record()
    second = deepcopy(first)

    result = _evaluate(first, second)

    assert result["summary"] == {
        "record_count": 2,
        "eligible_count": 0,
        "blocked_count": 2,
    }
    assert all(item["blocked_reasons"] == ["duplicate_series_horizon"] for item in result["results"])


def test_unknown_series_and_credentials_are_never_echoed() -> None:
    secret = "TOKEN-super-secret-value"
    unknown = _record(f"unknown-{secret}")
    unknown["evidence_bundle"]["gates"]["source_authorization"]["evidence_id"] = secret
    unknown["evidence_bundle"]["gates"]["source_authorization"]["access_token"] = secret

    result = _evaluate(unknown)
    rendered = repr(result)

    assert result["results"][0]["series_id"] == "__unknown__"
    assert "unknown_series" in result["results"][0]["blocked_reasons"]
    assert "invalid_source_authorization_evidence" in result["results"][0]["blocked_reasons"]
    assert secret not in rendered


def test_evidence_validity_and_horizon_are_bound_to_the_requested_period() -> None:
    stale = _record()
    stale["evidence_bundle"]["gates"]["freshness"]["valid_through"] = "2026-06-29T23:59:59+08:00"
    wrong_horizon = _record(FORMAL_SERIES_IDS[1], horizon_days=30)
    wrong_horizon["evidence_bundle"]["gates"]["visibility"]["applicable_horizons"] = [
        1,
        7,
    ]

    results = _evaluate(stale, wrong_horizon)["results"]

    assert results[0]["blocked_reasons"] == [
        "freshness_not_valid_at_assessment",
        "freshness_approval_not_valid_at_assessment",
    ]
    assert results[1]["blocked_reasons"] == [
        "visibility_horizon_not_covered",
        "visibility_approval_horizon_not_covered",
    ]


def test_bundle_and_every_gate_are_bound_to_the_exact_series() -> None:
    original = _record(FORMAL_SERIES_IDS[0])
    reused = deepcopy(original)
    reused["series_id"] = FORMAL_SERIES_IDS[1]

    item = _evaluate(reused)["results"][0]

    assert item["eligibility_status"] == "blocked"
    assert item["blocked_reasons"][0] == "evidence_bundle_series_mismatch"
    assert all(f"{gate_name}_series_mismatch" in item["blocked_reasons"] for gate_name in GATE_NAMES)


@pytest.mark.parametrize(
    ("horizon", "reason"),
    [(14, "legacy_horizon_read_only"), (3, "unsupported_horizon")],
)
def test_non_formal_horizons_never_become_eligible(horizon: int, reason: str) -> None:
    item = _evaluate(_record(horizon_days=horizon))["results"][0]

    assert item["eligibility_status"] == "blocked"
    assert reason in item["blocked_reasons"]


def test_version_mismatches_block_without_accepting_connectivity_as_evidence() -> None:
    record = _record()
    record["evidence_bundle"]["source_reachable"] = True
    record["evidence_bundle"]["gates"]["source_authorization"]["policy_version"] = "future-policy"

    item = _evaluate(record, policy_version="future-policy")["results"][0]

    assert item["eligibility_status"] == "blocked"
    assert item["blocked_reasons"][:2] == [
        "policy_version_mismatch",
        "invalid_evidence_bundle_schema",
    ]
    assert "source_authorization_version_mismatch" in item["blocked_reasons"]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), True])
def test_numeric_evidence_must_be_finite_and_not_boolean(value: object) -> None:
    record = _record()
    record["evidence_bundle"]["gates"]["freshness"]["numeric_checks"] = {
        "age_seconds": value,
        "max_age_seconds": 3600,
    }

    item = _evaluate(record)["results"][0]

    assert "freshness_numeric_checks_invalid" in item["blocked_reasons"]


def test_extreme_integer_is_stably_blocked_without_float_overflow() -> None:
    record = _record()
    record["evidence_bundle"]["gates"]["freshness"]["numeric_checks"]["age_seconds"] = 10**10000

    item = _evaluate(record)["results"][0]

    assert "freshness_numeric_checks_invalid" in item["blocked_reasons"]


def test_extreme_horizon_is_rejected_before_it_can_enter_json_output() -> None:
    with pytest.raises(FormalSeriesEligibilityInputError, match="horizon_days_out_of_range"):
        _evaluate(_record(horizon_days=10**10000))


def test_semantically_blocked_result_is_json_serializable() -> None:
    result = _evaluate(_record(horizon_days=3))

    assert json.loads(json.dumps(result, allow_nan=False)) == result


@pytest.mark.parametrize(
    "numeric_checks",
    [{}, {"age_seconds": 3601, "max_age_seconds": 3600}, {"age_seconds": -1, "max_age_seconds": 3600}],
)
def test_freshness_cannot_pass_without_complete_bounded_measurements(
    numeric_checks: dict[str, int],
) -> None:
    record = _record()
    record["evidence_bundle"]["gates"]["freshness"]["numeric_checks"] = numeric_checks

    assert _evaluate(record)["results"][0]["blocked_reasons"] == ["freshness_numeric_checks_invalid"]


@pytest.mark.parametrize(
    "records",
    [
        "not-a-sequence",
        ["not-an-object"],
        [{"series_id": 7, "horizon_days": 1, "evidence_bundle": {}}],
    ],
)
def test_outer_input_types_are_strict(records: object) -> None:
    with pytest.raises(FormalSeriesEligibilityInputError):
        evaluate_formal_series_eligibility(records, assessment_as_of=ASSESSMENT_AS_OF)


def test_custom_mapping_and_sequence_inputs_are_rejected_without_invoking_them() -> None:
    for records in (_ExplodingSequence(), [_ExplodingMapping()]):
        with pytest.raises(FormalSeriesEligibilityInputError) as caught:
            evaluate_formal_series_eligibility(records, assessment_as_of=ASSESSMENT_AS_OF)

        rendered = str(caught.value)
        assert rendered in {"records_must_be_a_sequence", "record_must_be_an_object"}
        assert "private-" not in rendered


@pytest.mark.parametrize(
    "nested_value",
    [_ExplodingMapping(), _ExplodingSequence()],
)
def test_nested_custom_containers_block_before_business_traversal(nested_value: object) -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    record["evidence_bundle"] = nested_value

    result = _evaluate_with_manifest((record,), manifest)

    assert result["results"][0]["blocked_reasons"] == ["invalid_record_data"]
    assert "private-" not in repr(result)


def test_custom_manifest_containers_are_invalid_without_invoking_them() -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    manifest["approvals"] = _ExplodingSequence()

    result = evaluate_formal_series_eligibility(
        [record],
        assessment_as_of=ASSESSMENT_AS_OF,
        approved_evidence_manifest=manifest,
    )

    assert result["results"][0]["blocked_reasons"][0] == "approved_evidence_manifest_invalid"
    assert "private-sequence-secret" not in repr(result)


def test_unexpected_container_traversal_failure_uses_a_fixed_error_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_without_echo(value: object) -> bool:
        raise RuntimeError("private-container-secret")

    monkeypatch.setattr(eligibility, "_plain_object_within_shallow_limits", fail_without_echo)
    with pytest.raises(FormalSeriesEligibilityInputError) as caught:
        evaluate_formal_series_eligibility([_record()], assessment_as_of=ASSESSMENT_AS_OF)

    assert str(caught.value) == "input_container_traversal_failed"
    assert "private-container-secret" not in str(caught.value)


def test_oversized_gate_is_blocked_before_schema_key_materialization() -> None:
    record = _record()
    manifest = _approved_manifest((record,))
    record["evidence_bundle"]["gates"]["source_authorization"] = {
        f"gate-key-{index}": True for index in range(eligibility.MAX_CANONICAL_COLLECTION_ITEMS + 1)
    }

    result = _evaluate_with_manifest((record,), manifest)

    reasons = result["results"][0]["blocked_reasons"]
    assert reasons[0] == "invalid_source_authorization_evidence"
    assert "source_authorization_approval_facts_mismatch" in reasons
    assert not any(reason.startswith("gate-key-") for reason in reasons)


def test_assessment_time_requires_timezone_and_results_are_deterministic() -> None:
    with pytest.raises(FormalSeriesEligibilityInputError, match="assessment_as_of_must_be_rfc3339"):
        evaluate_formal_series_eligibility([], assessment_as_of="2026-06-30T08:20:00")
    with pytest.raises(FormalSeriesEligibilityInputError, match="assessment_as_of_must_be_rfc3339"):
        evaluate_formal_series_eligibility([], assessment_as_of="2026-06-30 08:20:00+08:00")

    records = [
        _record(FORMAL_SERIES_IDS[1], horizon_days=7),
        _record(FORMAL_SERIES_IDS[0]),
    ]
    assert _evaluate(*records) == _evaluate(*reversed(records))


def test_unknown_rfc3339_offset_is_rejected_for_assessment_gate_and_approval() -> None:
    with pytest.raises(FormalSeriesEligibilityInputError, match="assessment_as_of_must_be_rfc3339"):
        evaluate_formal_series_eligibility([], assessment_as_of="2026-06-30T00:20:00-00:00")

    record = _record()
    manifest = _approved_manifest((record,))
    record["evidence_bundle"]["gates"]["freshness"]["valid_from"] = "2026-01-01T00:00:00-00:00"
    gate_item = _evaluate_with_manifest((record,), manifest)["results"][0]
    assert "freshness_validity_invalid" in gate_item["blocked_reasons"]

    record = _record()
    manifest = _approved_manifest((record,))
    manifest["approvals"][0]["valid_from"] = "2026-01-01T00:00:00-00:00"
    approval_item = _evaluate_with_manifest((record,), manifest)["results"][0]
    assert approval_item["blocked_reasons"][0] == "approved_evidence_manifest_invalid"


def test_frozen_19_series_match_the_v5_contract_and_keep_cfets_out_of_prediction_nodes() -> None:
    from app import phase_a_contracts

    contract = phase_a_contracts.load_contract()
    assert tuple(contract["formal_evidence_series_ids"]) == FORMAL_SERIES_IDS
    assert len(FORMAL_SERIES_IDS) == 19
    assert contract["non_prediction_series_ids"] == ["fx.usd_cny.cfets.central_parity.cny_per_usd"]
    assert len(contract["formal_nodes"]) == 14

"""Pure, fail-closed assembly of one replay Shadow lifecycle sample.

This boundary deliberately cannot approve caller-supplied factor, baseline, or
outcome projections.  Until those projections and their metric contract have
an owning verified reader, the assembled sample remains excluded from every
promotion denominator.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any

from .phase_a_contracts import EXPECTED_FORMAL_NODES

ASSEMBLY_SCHEMA_VERSION = "shadow-sample-input-assembly.v1"
FORMAL_HORIZONS = frozenset({1, 7, 30})
MAX_ACTION_BYTES = 64 * 1024
MAX_EVIDENCE_TIMES = 128
MAX_METRICS = 64
MAX_STRING_BYTES = 4_096
MAX_TOTAL_STRING_BYTES = 32 * 1024
MAX_ABS_METRIC_VALUE = 9_007_199_254_740_991
MAX_JSON_DEPTH = 16
MAX_JSON_NODES = 2_048

_ACTION_FIELDS = frozenset(
    {
        "as_of",
        "source_item_id",
        "product",
        "node_id",
        "observed_at",
        "available_at",
        "evidence_times",
        "evidence_status",
        "metric_values",
        "replay_policy_version",
        "action",
        "horizon",
        "shadow_policy_version",
        "idempotency_key",
    }
)
_WINDOW_FIELDS = frozenset({"window_id", "train_end", "evaluation_start", "evaluation_end"})
_PROJECTION_FIELDS = frozenset(
    {
        "revision_id",
        "product",
        "node_id",
        "horizon",
        "observed_at",
        "available_at",
        "evidence_status",
        "review_status",
        "payload_sha256",
    }
)
_POLICY_FIELDS = frozenset(
    {
        "policy_version",
        "minimum_window_gain",
        "maximum_high_confidence_error_rate_delta",
        "high_confidence_threshold",
        "minimum_high_confidence_paired_samples",
    }
)
_CANONICAL_RFC3339 = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_ERROR_CODE = re.compile(r"\Ashadow_[a-z0-9_]{1,112}\Z")


class ShadowSampleAssemblyError(ValueError):
    """Stable, bounded and non-secret assembly failure."""

    def __init__(self, code: str) -> None:
        bounded = code if _ERROR_CODE.fullmatch(code) else "shadow_sample_assembly_failed"
        super().__init__(bounded)
        self.code = bounded


def assemble_shadow_sample_input(
    *,
    action: dict[str, Any],
    factor_id: str,
    baseline_id: str,
    baseline_is_transparent: bool,
    window: dict[str, Any],
    factor_projection: dict[str, Any] | None = None,
    baseline_projection: dict[str, Any] | None = None,
    outcome_projection: dict[str, Any] | None = None,
    metric_contract_version: str | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic lifecycle input whose sample cannot self-approve.

    Projection dictionaries are provenance references only.  A caller cannot
    turn one into audited evidence by setting a flag, and no projection values
    are accepted or used to manufacture errors, confidences, or correctness.
    """

    normalized_action = _action(action)
    normalized_window = _window(window)
    prediction_time = _time(normalized_action["observed_at"], "shadow_observed_at_invalid")
    train_end = _time(normalized_window["train_end"], "shadow_window_time_invalid")
    evaluation_start = _time(normalized_window["evaluation_start"], "shadow_window_time_invalid")
    evaluation_end = _time(normalized_window["evaluation_end"], "shadow_window_time_invalid")
    if not train_end < prediction_time or not evaluation_start <= prediction_time <= evaluation_end:
        raise ShadowSampleAssemblyError("shadow_window_mismatch")
    normalized_factor_id = _text(factor_id, "shadow_factor_id_invalid")
    normalized_baseline_id = _text(baseline_id, "shadow_baseline_id_invalid")
    if type(baseline_is_transparent) is not bool:
        raise ShadowSampleAssemblyError("shadow_baseline_transparency_invalid")
    normalized_policy = _policy(policy, normalized_action["shadow_policy_version"])
    normalized_metric_contract = (
        _text(metric_contract_version, "shadow_metric_contract_invalid")
        if metric_contract_version is not None
        else None
    )
    projections = {
        "factor": _projection("factor", factor_projection, normalized_action),
        "baseline": _projection("baseline", baseline_projection, normalized_action),
        "outcome": _projection("outcome", outcome_projection, normalized_action),
    }
    _validate_projection_timeline(projections, normalized_action, normalized_window)

    reasons: set[str] = {"replay_action_provenance_unverified"}
    reconstructed = normalized_action["evidence_status"] == "reconstructed"
    if reconstructed:
        reasons.add("reconstructed_evidence")
    for role, projection in projections.items():
        if projection is None:
            reasons.add(f"{role}_projection_missing")
            continue
        reasons.add(f"{role}_projection_{projection['review_status']}")
        if projection["evidence_status"] == "reconstructed":
            reconstructed = True
            reasons.update({"reconstructed_evidence", f"{role}_projection_reconstructed"})
    if projections["outcome"] is not None:
        reasons.add("outcome_binding_unverified")
    if not baseline_is_transparent:
        reasons.add("baseline_not_transparent")
    if normalized_metric_contract is None:
        reasons.add("metric_contract_missing")
    else:
        reasons.add("metric_contract_unapproved")
    if normalized_policy is None:
        reasons.add("lifecycle_policy_missing")

    scoreability = "unscorable" if reconstructed else "blocked"
    sample = {
        "sample_id": _sample_id(
            normalized_action,
            normalized_window,
            normalized_factor_id,
            normalized_baseline_id,
            projections,
            normalized_metric_contract,
        ),
        "window_id": normalized_window["window_id"],
        "scoreability": scoreability,
        "exclusion_reasons": sorted(reasons),
    }
    lifecycle_input = {
        "factor_id": normalized_factor_id,
        "baseline_id": normalized_baseline_id,
        "baseline_is_transparent": baseline_is_transparent,
        "current_status": "shadow",
        "windows": [normalized_window],
        "samples": [sample],
        "policy": normalized_policy,
        "retirement_requested": False,
    }
    action_sha256 = _digest(normalized_action)
    lifecycle_input_sha256 = _digest(lifecycle_input)
    return {
        "schema_version": ASSEMBLY_SCHEMA_VERSION,
        "assembly_status": scoreability,
        "reason_codes": sorted(reasons),
        "lifecycle_input": lifecycle_input,
        "lineage": {
            "action_sha256": action_sha256,
            "lifecycle_input_sha256": lifecycle_input_sha256,
            "source_item_id": normalized_action["source_item_id"],
            "idempotency_key": list(normalized_action["idempotency_key"]),
            "product": normalized_action["product"],
            "node_id": normalized_action["node_id"],
            "horizon": normalized_action["horizon"],
            "as_of": normalized_action["as_of"],
            "replay_policy_version": normalized_action["replay_policy_version"],
            "shadow_policy_version": normalized_action["shadow_policy_version"],
            "metric_contract_version": normalized_metric_contract,
            "window_sha256": _digest(normalized_window),
            "projection_sha256": {
                role: _digest(projection) if projection is not None else None
                for role, projection in sorted(projections.items())
            },
            "projections": projections,
        },
    }


def _action(raw: Any) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) != _ACTION_FIELDS:
        raise ShadowSampleAssemblyError("shadow_action_invalid")
    _bounded_json(raw, "shadow_action_resource_limit")
    if raw["action"] != "assemble_shadow_sample":
        raise ShadowSampleAssemblyError("shadow_action_kind_mismatch")
    product = raw["product"]
    if product not in {"poy", "dty"}:
        raise ShadowSampleAssemblyError("shadow_product_invalid")
    node_id = raw["node_id"]
    if node_id not in EXPECTED_FORMAL_NODES:
        raise ShadowSampleAssemblyError("shadow_node_invalid")
    horizon = raw["horizon"]
    if type(horizon) is not int or horizon not in FORMAL_HORIZONS:
        raise ShadowSampleAssemblyError("shadow_horizon_invalid")
    source_item_id = _text(raw["source_item_id"], "shadow_source_item_id_invalid")
    replay_policy_version = _text(raw["replay_policy_version"], "shadow_replay_policy_invalid")
    shadow_policy_version = _text(raw["shadow_policy_version"], "shadow_policy_version_invalid")
    as_of = _time(raw["as_of"], "shadow_as_of_invalid")
    observed_at = _time(raw["observed_at"], "shadow_observed_at_invalid")
    available_at = _time(raw["available_at"], "shadow_available_at_invalid")
    if observed_at > available_at:
        raise ShadowSampleAssemblyError("shadow_available_precedes_observed")
    if available_at > as_of:
        raise ShadowSampleAssemblyError("shadow_evidence_after_as_of")
    evidence_status = raw["evidence_status"]
    if evidence_status not in {"point_in_time", "reconstructed"}:
        raise ShadowSampleAssemblyError("shadow_evidence_status_invalid")
    evidence_times = raw["evidence_times"]
    if type(evidence_times) is not list or not evidence_times or len(evidence_times) > MAX_EVIDENCE_TIMES:
        raise ShadowSampleAssemblyError("shadow_evidence_times_invalid")
    normalized_evidence_times = []
    for value in evidence_times:
        parsed = _time(value, "shadow_evidence_time_invalid")
        if parsed > as_of:
            raise ShadowSampleAssemblyError("shadow_evidence_after_as_of")
        normalized_evidence_times.append(parsed.isoformat())
    if normalized_evidence_times != sorted(set(normalized_evidence_times)):
        raise ShadowSampleAssemblyError("shadow_evidence_times_invalid")
    metrics = raw["metric_values"]
    if type(metrics) is not dict or len(metrics) > MAX_METRICS:
        raise ShadowSampleAssemblyError("shadow_metric_values_invalid")
    normalized_metrics: dict[str, int | float] = {}
    for key, value in metrics.items():
        name = _text(key, "shadow_metric_key_invalid")
        if type(value) not in {int, float} or (type(value) is float and not math.isfinite(value)):
            raise ShadowSampleAssemblyError("shadow_metric_value_invalid")
        if abs(value) > MAX_ABS_METRIC_VALUE:
            raise ShadowSampleAssemblyError("shadow_metric_value_invalid")
        normalized_metrics[name] = value
    raw_key = raw["idempotency_key"]
    if type(raw_key) is not list or len(raw_key) != 6:
        raise ShadowSampleAssemblyError("shadow_idempotency_key_mismatch")
    normalized_key = [
        _time(raw_key[0], "shadow_idempotency_key_mismatch").isoformat(),
        raw_key[1],
        raw_key[2],
        raw_key[3],
        raw_key[4],
        raw_key[5],
    ]
    expected_key = [as_of.isoformat(), product, node_id, raw["action"], source_item_id, horizon]
    if normalized_key != expected_key:
        raise ShadowSampleAssemblyError("shadow_idempotency_key_mismatch")
    return {
        "as_of": as_of.isoformat(),
        "source_item_id": source_item_id,
        "product": product,
        "node_id": node_id,
        "observed_at": observed_at.isoformat(),
        "available_at": available_at.isoformat(),
        "evidence_times": normalized_evidence_times,
        "evidence_status": evidence_status,
        "metric_values": dict(sorted(normalized_metrics.items())),
        "replay_policy_version": replay_policy_version,
        "action": "assemble_shadow_sample",
        "horizon": horizon,
        "shadow_policy_version": shadow_policy_version,
        "idempotency_key": expected_key,
    }


def _window(raw: Any) -> dict[str, str]:
    if type(raw) is not dict or set(raw) != _WINDOW_FIELDS:
        raise ShadowSampleAssemblyError("shadow_window_invalid")
    window_id = _text(raw["window_id"], "shadow_window_id_invalid")
    train_end = _time(raw["train_end"], "shadow_window_time_invalid")
    evaluation_start = _time(raw["evaluation_start"], "shadow_window_time_invalid")
    evaluation_end = _time(raw["evaluation_end"], "shadow_window_time_invalid")
    if not train_end < evaluation_start <= evaluation_end:
        raise ShadowSampleAssemblyError("shadow_window_order_invalid")
    return {
        "window_id": window_id,
        "train_end": train_end.isoformat(),
        "evaluation_start": evaluation_start.isoformat(),
        "evaluation_end": evaluation_end.isoformat(),
    }


def _projection(role: str, raw: Any, action: dict[str, Any]) -> dict[str, Any] | None:
    if raw is None:
        return None
    if type(raw) is not dict or set(raw) != _PROJECTION_FIELDS:
        raise ShadowSampleAssemblyError("shadow_projection_invalid")
    if raw["product"] != action["product"]:
        raise ShadowSampleAssemblyError("shadow_product_mismatch")
    if raw["node_id"] != action["node_id"]:
        raise ShadowSampleAssemblyError("shadow_node_mismatch")
    if raw["horizon"] != action["horizon"]:
        raise ShadowSampleAssemblyError("shadow_horizon_mismatch")
    observed_at = _time(raw["observed_at"], "shadow_projection_time_invalid")
    available_at = _time(raw["available_at"], "shadow_projection_time_invalid")
    if observed_at > available_at:
        raise ShadowSampleAssemblyError("shadow_available_precedes_observed")
    if available_at > _time(action["as_of"], "shadow_as_of_invalid"):
        raise ShadowSampleAssemblyError("shadow_evidence_after_as_of")
    evidence_status = raw["evidence_status"]
    if evidence_status not in {"point_in_time", "reconstructed"}:
        raise ShadowSampleAssemblyError("shadow_evidence_status_invalid")
    review_status = raw["review_status"]
    if review_status not in {"unaudited", "unapproved"}:
        raise ShadowSampleAssemblyError("shadow_projection_review_status_invalid")
    payload_sha256 = raw["payload_sha256"]
    if type(payload_sha256) is not str or not _SHA256.fullmatch(payload_sha256):
        raise ShadowSampleAssemblyError("shadow_projection_hash_invalid")
    return {
        "role": role,
        "revision_id": _text(raw["revision_id"], "shadow_projection_revision_invalid"),
        "product": raw["product"],
        "node_id": raw["node_id"],
        "horizon": raw["horizon"],
        "observed_at": observed_at.isoformat(),
        "available_at": available_at.isoformat(),
        "evidence_status": evidence_status,
        "review_status": review_status,
        "payload_sha256": payload_sha256,
    }


def _policy(raw: Any, expected_version: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if type(raw) is not dict or set(raw) != _POLICY_FIELDS:
        raise ShadowSampleAssemblyError("shadow_policy_invalid")
    if raw["policy_version"] != expected_version:
        raise ShadowSampleAssemblyError("shadow_policy_version_mismatch")
    _bounded_json(raw, "shadow_policy_resource_limit")
    return dict(raw)


def _validate_projection_timeline(
    projections: dict[str, dict[str, Any] | None],
    action: dict[str, Any],
    window: dict[str, str],
) -> None:
    prediction_time = _time(action["observed_at"], "shadow_observed_at_invalid")
    for role in ("factor", "baseline"):
        projection = projections[role]
        if projection is not None and _time(
            projection["available_at"], "shadow_projection_time_invalid"
        ) > prediction_time:
            raise ShadowSampleAssemblyError("shadow_prediction_time_mismatch")
    outcome = projections["outcome"]
    if outcome is None:
        return
    outcome_visible = _time(outcome["available_at"], "shadow_projection_time_invalid")
    evaluation_end = _time(window["evaluation_end"], "shadow_window_time_invalid")
    if outcome_visible <= prediction_time or outcome_visible > evaluation_end:
        raise ShadowSampleAssemblyError("shadow_outcome_time_mismatch")
    for role in ("factor", "baseline"):
        projection = projections[role]
        if projection is not None and _time(
            projection["available_at"], "shadow_projection_time_invalid"
        ) >= outcome_visible:
            raise ShadowSampleAssemblyError("shadow_outcome_time_mismatch")


def _sample_id(
    action: dict[str, Any],
    window: dict[str, str],
    factor_id: str,
    baseline_id: str,
    projections: dict[str, dict[str, Any] | None],
    metric_contract_version: str | None,
) -> str:
    identity = {
        "action_sha256": _digest(action),
        "window_sha256": _digest(window),
        "factor_id": factor_id,
        "baseline_id": baseline_id,
        "projection_sha256": {
            role: _digest(projection) if projection is not None else None
            for role, projection in sorted(projections.items())
        },
        "metric_contract_version": metric_contract_version,
    }
    return f"shadow-sample-{_digest(identity)}"


def _text(value: Any, code: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value.encode("utf-8")) > MAX_STRING_BYTES
    ):
        raise ShadowSampleAssemblyError(code)
    return value


def _time(value: Any, code: str) -> datetime:
    if type(value) is not str or not _CANONICAL_RFC3339.fullmatch(value) or "-00:00" in value:
        raise ShadowSampleAssemblyError(code)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ShadowSampleAssemblyError(code) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShadowSampleAssemblyError(code)
    return parsed


def _bounded_json(value: Any, code: str) -> bytes:
    _validate_json_shape(value, code)
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError):
        raise ShadowSampleAssemblyError(code) from None
    if len(encoded) > MAX_ACTION_BYTES:
        raise ShadowSampleAssemblyError(code)
    return encoded


def _validate_json_shape(value: Any, code: str) -> None:
    stack = [(value, 0)]
    seen_containers: set[int] = set()
    nodes = 0
    string_bytes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ShadowSampleAssemblyError(code)
        if type(current) is str:
            string_bytes += len(current.encode("utf-8"))
        elif current is None or type(current) in {bool, int}:
            pass
        elif type(current) is float:
            if not math.isfinite(current):
                raise ShadowSampleAssemblyError(code)
        elif type(current) is list:
            identity = id(current)
            if identity in seen_containers:
                raise ShadowSampleAssemblyError(code)
            seen_containers.add(identity)
            stack.extend((item, depth + 1) for item in current)
        elif type(current) is dict:
            identity = id(current)
            if identity in seen_containers:
                raise ShadowSampleAssemblyError(code)
            seen_containers.add(identity)
            for key, item in current.items():
                if type(key) is not str:
                    raise ShadowSampleAssemblyError(code)
                string_bytes += len(key.encode("utf-8"))
                stack.append((item, depth + 1))
        else:
            raise ShadowSampleAssemblyError(code)
        if string_bytes > MAX_TOTAL_STRING_BYTES:
            raise ShadowSampleAssemblyError(code)


def _digest(value: Any) -> str:
    return hashlib.sha256(_bounded_json(value, "shadow_canonicalization_failed")).hexdigest()

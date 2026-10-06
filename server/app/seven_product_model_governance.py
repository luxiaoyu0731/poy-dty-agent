from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import SevenProductEvaluationBatch
from .seven_product_contract import CURRENT_FORMAL_CELL_COUNT, CURRENT_FORMAL_HORIZONS, CURRENT_FORMAL_TARGETS

MODEL_REGISTRY_SCHEMA_VERSION = "seven-product-model-registry.v2"
DEFAULT_MODEL_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "model_registry" / "seven_product_registry.json"
REFERENCE_FALLBACK_MODEL_VERSION = "persistence.v1"
REFERENCE_CONSECUTIVE_LOSS_LIMIT = 3
FORMAL_FALLBACK_MODEL_VERSION = "persistence.v1"
FORMAL_CONSECUTIVE_LOSS_LIMIT = 3
FORMAL_EVALUATION_POLICY_VERSION = "seven-product-oos-gate.v4"
FORMAL_MINIMUM_ERROR_IMPROVEMENT = 0.05
FORMAL_MINIMUM_DIRECTION_ACCURACY = 0.55
FORMAL_MINIMUM_EFFECTIVE_SAMPLES = 20


class SevenProductModelGovernanceError(ValueError):
    """Stable fail-closed error for model registry operations."""


def load_model_registry(path: Path = DEFAULT_MODEL_REGISTRY_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SevenProductModelGovernanceError("model_registry_unavailable") from exc
    validate_model_registry(payload)
    return payload


def validate_model_registry(registry: Mapping[str, Any]) -> None:
    if registry.get("schema_version") != MODEL_REGISTRY_SCHEMA_VERSION:
        raise SevenProductModelGovernanceError("model_registry_schema_invalid")
    candidates = registry.get("candidate_models")
    champions = registry.get("champions")
    history = registry.get("promotion_history")
    rollback_targets = registry.get("rollback_targets")
    reference_champions = registry.get("reference_champions")
    reference_history = registry.get("reference_promotion_history")
    reference_rollback_targets = registry.get("reference_rollback_targets")
    if not isinstance(candidates, Mapping) or not candidates:
        raise SevenProductModelGovernanceError("candidate_models_missing")
    if not isinstance(champions, Mapping) or set(champions) != set(CURRENT_FORMAL_TARGETS):
        raise SevenProductModelGovernanceError("champion_grid_invalid")
    for target in CURRENT_FORMAL_TARGETS:
        horizon_map = champions.get(target)
        expected_horizons = {str(value) for value in CURRENT_FORMAL_HORIZONS}
        if not isinstance(horizon_map, Mapping) or set(horizon_map) != expected_horizons:
            raise SevenProductModelGovernanceError(f"champion_horizons_invalid:{target}")
        for model_version in horizon_map.values():
            if model_version is not None and model_version not in candidates:
                raise SevenProductModelGovernanceError(f"champion_model_unknown:{target}:{model_version}")
    if not isinstance(history, list) or not isinstance(rollback_targets, Mapping):
        raise SevenProductModelGovernanceError("model_registry_history_invalid")
    if not isinstance(reference_history, list) or not isinstance(reference_rollback_targets, Mapping):
        raise SevenProductModelGovernanceError("reference_registry_history_invalid")
    if not isinstance(reference_champions, Mapping) or set(reference_champions) != set(CURRENT_FORMAL_TARGETS):
        raise SevenProductModelGovernanceError("reference_champion_grid_invalid")
    for target in CURRENT_FORMAL_TARGETS:
        horizon_map = reference_champions.get(target)
        expected_horizons = {str(value) for value in CURRENT_FORMAL_HORIZONS}
        if not isinstance(horizon_map, Mapping) or set(horizon_map) != expected_horizons:
            raise SevenProductModelGovernanceError(f"reference_champion_horizons_invalid:{target}")
        for model_version in horizon_map.values():
            if model_version is not None and model_version not in candidates:
                raise SevenProductModelGovernanceError(f"reference_champion_model_unknown:{target}:{model_version}")
    policy = registry.get("reference_policy")
    if not isinstance(policy, Mapping):
        raise SevenProductModelGovernanceError("reference_policy_missing")
    if (
        policy.get("formal_status_ceiling") != "reference"
        or policy.get("fallback_model_version") != REFERENCE_FALLBACK_MODEL_VERSION
        or policy.get("consecutive_loss_limit") != REFERENCE_CONSECUTIVE_LOSS_LIMIT
    ):
        raise SevenProductModelGovernanceError("reference_policy_invalid")
    for model_version, candidate in candidates.items():
        if not isinstance(candidate, Mapping):
            raise SevenProductModelGovernanceError(f"candidate_model_invalid:{model_version}")
        if not isinstance(candidate.get("runtime_implemented"), bool):
            raise SevenProductModelGovernanceError(f"candidate_runtime_status_missing:{model_version}")
        requirements = candidate.get("required_feature_series")
        if not isinstance(requirements, list) or any(not isinstance(value, str) or not value for value in requirements):
            raise SevenProductModelGovernanceError(f"candidate_feature_contract_invalid:{model_version}")
    revision = registry.get("registry_revision")
    if not isinstance(revision, str) or not revision:
        raise SevenProductModelGovernanceError("model_registry_revision_missing")


def approve_model_promotion(
    *,
    registry: Mapping[str, Any],
    evaluation: SevenProductEvaluationBatch,
    candidate_model_version: str,
    approved_cells: Sequence[tuple[str, int]],
    actor: str,
    reason: str,
    approved: bool,
    approved_at: str | None = None,
) -> dict[str, Any]:
    """Return a new registry revision; never persist or auto-promote it."""

    validate_model_registry(registry)
    if approved is not True:
        raise SevenProductModelGovernanceError("explicit_promotion_approval_required")
    actor = _required_text(actor, "promotion_actor_required")
    reason = _required_text(reason, "promotion_reason_required")
    candidate = registry["candidate_models"].get(candidate_model_version)
    if not isinstance(candidate, Mapping):
        raise SevenProductModelGovernanceError("candidate_model_unknown")
    if candidate.get("runtime_implemented") is not True:
        raise SevenProductModelGovernanceError("candidate_model_runtime_not_implemented")
    _validate_formal_evaluation(evaluation)
    requested = list(approved_cells)
    if not requested or len(set(requested)) != len(requested):
        raise SevenProductModelGovernanceError("approved_cells_invalid")
    evaluation_cells = {(cell.target, cell.horizon_days): cell for cell in evaluation.cells}
    if len(evaluation_cells) != CURRENT_FORMAL_CELL_COUNT or not evaluation.contract_complete:
        raise SevenProductModelGovernanceError("evaluation_contract_incomplete")
    for target, horizon in requested:
        if target not in CURRENT_FORMAL_TARGETS or horizon not in CURRENT_FORMAL_HORIZONS:
            raise SevenProductModelGovernanceError(f"approved_cell_unknown:{target}:{horizon}")
        cell = evaluation_cells[(target, horizon)]
        if cell.model_version != candidate_model_version:
            raise SevenProductModelGovernanceError(f"evaluation_model_mismatch:{target}:{horizon}")
        if not cell.promotion_eligible:
            raise SevenProductModelGovernanceError(f"evaluation_gate_failed:{target}:{horizon}")

    timestamp = _timestamp(approved_at or datetime.now(UTC).isoformat())
    updated = deepcopy(dict(registry))
    previous_revision = str(registry["registry_revision"])
    transition = {
        "actor": actor,
        "approved_at": timestamp,
        "candidate_model_version": candidate_model_version,
        "evaluation_id": evaluation.evaluation_id,
        "evaluation_report_sha256": evaluation.report_sha256,
        "from_registry_revision": previous_revision,
        "reason": reason,
        "cells": [],
    }
    for target, horizon in sorted(requested):
        horizon_key = str(horizon)
        previous_model = updated["champions"][target][horizon_key]
        updated["champions"][target][horizon_key] = candidate_model_version
        cell_key = f"{target}:{horizon}"
        updated["rollback_targets"][cell_key] = previous_model
        transition["cells"].append(
            {
                "target": target,
                "horizon_days": horizon,
                "from_model_version": previous_model,
                "to_model_version": candidate_model_version,
                "evaluation_result_sha256": evaluation_cells[(target, horizon)].result_sha256,
            }
        )
    transition_digest = _digest(transition)
    transition["transition_sha256"] = transition_digest
    updated["promotion_history"].append(transition)
    updated["updated_at"] = timestamp
    updated["registry_revision"] = f"seven-registry-{transition_digest[:24]}"
    validate_model_registry(updated)
    return updated


def formal_runtime_decision(
    *,
    registry: Mapping[str, Any],
    target: str,
    horizon_days: int,
    feature_readiness: Mapping[str, bool],
    settled_history: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Resolve an active formal champion before any reference-only selector.

    A runtime fallback never inherits the displaced champion's approval.  The
    caller must therefore mark the cell non-formal unless the selected model is
    still the active, explicitly approved champion.
    """

    validate_model_registry(registry)
    if target not in CURRENT_FORMAL_TARGETS or horizon_days not in CURRENT_FORMAL_HORIZONS:
        raise SevenProductModelGovernanceError("formal_runtime_cell_unknown")
    configured = registry["champions"][target][str(horizon_days)]
    if configured is None:
        return None
    candidate = registry["candidate_models"].get(configured)
    if not isinstance(candidate, Mapping) or candidate.get("runtime_implemented") is not True:
        raise SevenProductModelGovernanceError("formal_champion_runtime_not_implemented")
    fallback = registry["rollback_targets"].get(f"{target}:{horizon_days}") or FORMAL_FALLBACK_MODEL_VERSION
    fallback_candidate = registry["candidate_models"].get(fallback)
    if not isinstance(fallback_candidate, Mapping) or fallback_candidate.get("runtime_implemented") is not True:
        raise SevenProductModelGovernanceError("formal_fallback_model_unknown")
    missing = [
        feature
        for feature in candidate["required_feature_series"]
        if feature_readiness.get(feature) is not True
    ]
    if missing:
        return {
            "governance_tier": "formal",
            "configured_model_version": configured,
            "selected_model_version": fallback,
            "fallback_triggered": True,
            "fallback_reason": f"formal_runtime_features_unavailable:{','.join(sorted(missing))}",
            "consecutive_losses_to_best_naive": 0,
        }
    loss_streak = _consecutive_losses_to_best_naive(settled_history, model_version=configured)
    triggered = loss_streak >= FORMAL_CONSECUTIVE_LOSS_LIMIT
    return {
        "governance_tier": "formal",
        "configured_model_version": configured,
        "selected_model_version": fallback if triggered else configured,
        "fallback_triggered": triggered,
        "fallback_reason": "formal_three_consecutive_losses_to_best_naive" if triggered else None,
        "consecutive_losses_to_best_naive": loss_streak,
    }


def rollback_model_cells(
    *,
    registry: Mapping[str, Any],
    cells: Sequence[tuple[str, int]],
    actor: str,
    reason: str,
    approved: bool,
    rolled_back_at: str | None = None,
) -> dict[str, Any]:
    """Return a new revision using only pre-recorded rollback targets."""

    validate_model_registry(registry)
    if approved is not True:
        raise SevenProductModelGovernanceError("explicit_rollback_approval_required")
    actor = _required_text(actor, "rollback_actor_required")
    reason = _required_text(reason, "rollback_reason_required")
    requested = list(cells)
    if not requested or len(set(requested)) != len(requested):
        raise SevenProductModelGovernanceError("rollback_cells_invalid")
    updated = deepcopy(dict(registry))
    timestamp = _timestamp(rolled_back_at or datetime.now(UTC).isoformat())
    transition = {
        "actor": actor,
        "approved_at": timestamp,
        "candidate_model_version": "rollback",
        "evaluation_id": None,
        "evaluation_report_sha256": None,
        "from_registry_revision": str(registry["registry_revision"]),
        "reason": reason,
        "cells": [],
    }
    for target, horizon in sorted(requested):
        if target not in CURRENT_FORMAL_TARGETS or horizon not in CURRENT_FORMAL_HORIZONS:
            raise SevenProductModelGovernanceError(f"rollback_cell_unknown:{target}:{horizon}")
        key = f"{target}:{horizon}"
        if key not in updated["rollback_targets"]:
            raise SevenProductModelGovernanceError(f"rollback_target_missing:{key}")
        current = updated["champions"][target][str(horizon)]
        rollback_target = updated["rollback_targets"][key]
        updated["champions"][target][str(horizon)] = rollback_target
        updated["rollback_targets"][key] = current
        transition["cells"].append(
            {
                "target": target,
                "horizon_days": horizon,
                "from_model_version": current,
                "to_model_version": rollback_target,
                "evaluation_result_sha256": None,
            }
        )
    transition_digest = _digest(transition)
    transition["transition_sha256"] = transition_digest
    updated["promotion_history"].append(transition)
    updated["updated_at"] = timestamp
    updated["registry_revision"] = f"seven-registry-{transition_digest[:24]}"
    validate_model_registry(updated)
    return updated


def approve_reference_promotion(
    *,
    registry: Mapping[str, Any],
    research_report: Mapping[str, Any],
    candidate_model_version: str,
    approved_cells: Sequence[tuple[str, int]],
    actor: str,
    reason: str,
    approved: bool,
    approved_at: str | None = None,
) -> dict[str, Any]:
    """Create an auditable reference-only revision from a hash-valid research report."""

    validate_model_registry(registry)
    if approved is not True:
        raise SevenProductModelGovernanceError("explicit_reference_promotion_approval_required")
    actor = _required_text(actor, "reference_promotion_actor_required")
    reason = _required_text(reason, "reference_promotion_reason_required")
    candidate = registry["candidate_models"].get(candidate_model_version)
    if not isinstance(candidate, Mapping):
        raise SevenProductModelGovernanceError("reference_candidate_model_unknown")
    if candidate.get("runtime_implemented") is not True:
        raise SevenProductModelGovernanceError("reference_candidate_runtime_not_implemented")
    report_sha256 = _validate_research_report(research_report)
    requested = list(approved_cells)
    if not requested or len(set(requested)) != len(requested):
        raise SevenProductModelGovernanceError("reference_approved_cells_invalid")
    report_candidates = {
        (str(item.get("target")), int(item.get("horizon_days")), str(item.get("model_version")))
        for item in research_report.get("promotion_candidates", [])
        if isinstance(item, Mapping) and str(item.get("horizon_days", "")).isdigit()
    }
    for target, horizon in requested:
        if target not in CURRENT_FORMAL_TARGETS or horizon not in CURRENT_FORMAL_HORIZONS:
            raise SevenProductModelGovernanceError(f"reference_approved_cell_unknown:{target}:{horizon}")
        if (target, horizon, candidate_model_version) not in report_candidates:
            raise SevenProductModelGovernanceError(f"reference_research_gate_failed:{target}:{horizon}")

    timestamp = _timestamp(approved_at or datetime.now(UTC).isoformat())
    updated = deepcopy(dict(registry))
    transition = {
        "actor": actor,
        "approved_at": timestamp,
        "candidate_model_version": candidate_model_version,
        "research_report_sha256": report_sha256,
        "from_registry_revision": str(registry["registry_revision"]),
        "formal_status_ceiling": "reference",
        "reason": reason,
        "cells": [],
    }
    for target, horizon in sorted(requested):
        horizon_key = str(horizon)
        previous_model = updated["reference_champions"][target][horizon_key]
        updated["reference_champions"][target][horizon_key] = candidate_model_version
        cell_key = f"{target}:{horizon}"
        updated["reference_rollback_targets"][cell_key] = previous_model or REFERENCE_FALLBACK_MODEL_VERSION
        transition["cells"].append(
            {
                "target": target,
                "horizon_days": horizon,
                "from_model_version": previous_model,
                "to_model_version": candidate_model_version,
            }
        )
    transition_digest = _digest(transition)
    transition["transition_sha256"] = transition_digest
    updated["reference_promotion_history"].append(transition)
    updated["updated_at"] = timestamp
    updated["registry_revision"] = f"seven-registry-{transition_digest[:24]}"
    validate_model_registry(updated)
    return updated


def reference_runtime_decision(
    *,
    registry: Mapping[str, Any],
    target: str,
    horizon_days: int,
    feature_readiness: Mapping[str, bool],
    settled_history: Sequence[Mapping[str, Any]],
    default_model_version: str,
) -> dict[str, Any]:
    """Resolve a per-cell reference model without mutating registry or history."""

    validate_model_registry(registry)
    if target not in CURRENT_FORMAL_TARGETS or horizon_days not in CURRENT_FORMAL_HORIZONS:
        raise SevenProductModelGovernanceError("reference_runtime_cell_unknown")
    default_candidate = registry["candidate_models"].get(default_model_version)
    if not isinstance(default_candidate, Mapping) or default_candidate.get("runtime_implemented") is not True:
        raise SevenProductModelGovernanceError("reference_default_model_not_implemented")
    configured = registry["reference_champions"][target][str(horizon_days)]
    if configured is None:
        return {
            "configured_model_version": None,
            "selected_model_version": default_model_version,
            "fallback_triggered": False,
            "fallback_reason": None,
            "consecutive_losses_to_best_naive": 0,
        }
    candidate = registry["candidate_models"].get(configured)
    if not isinstance(candidate, Mapping) or candidate.get("runtime_implemented") is not True:
        raise SevenProductModelGovernanceError("reference_candidate_runtime_not_implemented")
    fallback = registry["reference_rollback_targets"].get(
        f"{target}:{horizon_days}", REFERENCE_FALLBACK_MODEL_VERSION
    )
    fallback_candidate = registry["candidate_models"].get(fallback)
    if not isinstance(fallback_candidate, Mapping) or fallback_candidate.get("runtime_implemented") is not True:
        raise SevenProductModelGovernanceError("reference_fallback_model_unknown")
    missing = [
        feature
        for feature in candidate["required_feature_series"]
        if feature_readiness.get(feature) is not True
    ]
    if missing:
        return {
            "configured_model_version": configured,
            "selected_model_version": fallback,
            "fallback_triggered": True,
            "fallback_reason": f"runtime_features_unavailable:{','.join(sorted(missing))}",
            "consecutive_losses_to_best_naive": 0,
        }
    loss_streak = _consecutive_losses_to_best_naive(settled_history, model_version=configured)
    triggered = loss_streak >= REFERENCE_CONSECUTIVE_LOSS_LIMIT
    return {
        "configured_model_version": configured,
        "selected_model_version": fallback if triggered else configured,
        "fallback_triggered": triggered,
        "fallback_reason": "three_consecutive_losses_to_best_naive" if triggered else None,
        "consecutive_losses_to_best_naive": loss_streak,
    }


def registry_status(registry: Mapping[str, Any]) -> dict[str, object]:
    validate_model_registry(registry)
    cells = [
        {
            "target": target,
            "horizon_days": horizon,
            "champion_model_version": registry["champions"][target][str(horizon)],
            "rollback_model_version": registry["rollback_targets"].get(f"{target}:{horizon}"),
            "reference_champion_model_version": registry["reference_champions"][target][str(horizon)],
            "reference_rollback_model_version": registry["reference_rollback_targets"].get(
                f"{target}:{horizon}"
            ),
        }
        for target in CURRENT_FORMAL_TARGETS
        for horizon in CURRENT_FORMAL_HORIZONS
    ]
    return {
        "schema_version": MODEL_REGISTRY_SCHEMA_VERSION,
        "registry_revision": registry["registry_revision"],
        "champion_count": sum(cell["champion_model_version"] is not None for cell in cells),
        "reference_champion_count": sum(
            cell["reference_champion_model_version"] is not None for cell in cells
        ),
        "cells": cells,
        "automatic_promotion": False,
        "formal_consecutive_loss_limit": FORMAL_CONSECUTIVE_LOSS_LIMIT,
        "reference_formal_status_ceiling": "reference",
        "reference_consecutive_loss_limit": REFERENCE_CONSECUTIVE_LOSS_LIMIT,
    }


def approved_champion_evidence(
    registry: Mapping[str, Any],
    *,
    target: str,
    horizon_days: int,
    model_version: str,
) -> dict[str, str] | None:
    """Resolve the latest explicit approval for the active per-cell champion."""

    validate_model_registry(registry)
    if target not in CURRENT_FORMAL_TARGETS or horizon_days not in CURRENT_FORMAL_HORIZONS:
        raise SevenProductModelGovernanceError("champion_cell_unknown")
    if registry["champions"][target][str(horizon_days)] != model_version:
        return None
    for transition in reversed(registry["promotion_history"]):
        for cell in transition.get("cells", []):
            if (
                cell.get("target") == target
                and cell.get("horizon_days") == horizon_days
                and cell.get("to_model_version") == model_version
                and transition.get("evaluation_id")
                and cell.get("evaluation_result_sha256")
            ):
                return {
                    "evaluation_id": str(transition["evaluation_id"]),
                    "evaluation_report_sha256": str(transition["evaluation_report_sha256"]),
                    "evaluation_result_sha256": str(cell["evaluation_result_sha256"]),
                    "registry_revision": str(registry["registry_revision"]),
                }
    return None


def _required_text(value: object, code: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise SevenProductModelGovernanceError(code)
    return normalized


def _validate_research_report(report: Mapping[str, Any]) -> str:
    report_sha256 = str(report.get("report_body_sha256") or "")
    body = dict(report)
    body.pop("report_body_sha256", None)
    if len(report_sha256) != 64 or _digest(body) != report_sha256:
        raise SevenProductModelGovernanceError("reference_research_report_hash_invalid")
    if report.get("mode") not in {
        "SIMULATION_ONLY",
        "SOURCE_MISMATCHED_RESEARCH_CONTROL",
        "CAUSAL_CHAIN_RESEARCH_CONTROL",
    }:
        raise SevenProductModelGovernanceError("reference_research_mode_invalid")
    if report.get("formal_promotion_allowed") is True:
        raise SevenProductModelGovernanceError("reference_research_report_formal_boundary_invalid")
    gates = report.get("gates")
    if isinstance(gates, Mapping) and gates.get("formal_promotion_allowed") is True:
        raise SevenProductModelGovernanceError("reference_research_report_formal_boundary_invalid")
    if not isinstance(report.get("promotion_candidates"), list):
        raise SevenProductModelGovernanceError("reference_research_candidates_missing")
    if any(
        not isinstance(item, Mapping) or item.get("formal_status_ceiling") != "reference"
        for item in report["promotion_candidates"]
    ):
        raise SevenProductModelGovernanceError("reference_research_candidate_boundary_invalid")
    return report_sha256


def _validate_formal_evaluation(evaluation: SevenProductEvaluationBatch) -> str:
    """Recompute every derivable formal-evaluation identity before approval."""

    cells = list(evaluation.cells)
    cell_keys = {(cell.target, cell.horizon_days) for cell in cells}
    if len(cells) != CURRENT_FORMAL_CELL_COUNT or len(cell_keys) != CURRENT_FORMAL_CELL_COUNT:
        raise SevenProductModelGovernanceError("evaluation_contract_incomplete")
    if evaluation.contract_complete is not True:
        raise SevenProductModelGovernanceError("evaluation_contract_incomplete")
    if evaluation.evaluation_policy_version != FORMAL_EVALUATION_POLICY_VERSION:
        raise SevenProductModelGovernanceError("evaluation_policy_invalid")
    if (
        evaluation.minimum_error_improvement != FORMAL_MINIMUM_ERROR_IMPROVEMENT
        or evaluation.minimum_direction_accuracy != FORMAL_MINIMUM_DIRECTION_ACCURACY
        or evaluation.minimum_effective_samples != FORMAL_MINIMUM_EFFECTIVE_SAMPLES
    ):
        raise SevenProductModelGovernanceError("evaluation_thresholds_invalid")
    for cell in cells:
        if cell.evaluation_policy_version != FORMAL_EVALUATION_POLICY_VERSION:
            raise SevenProductModelGovernanceError(
                f"evaluation_cell_policy_invalid:{cell.target}:{cell.horizon_days}"
            )
        if cell.promotion_eligible != (not cell.gate_reasons):
            raise SevenProductModelGovernanceError(
                f"evaluation_cell_gate_inconsistent:{cell.target}:{cell.horizon_days}"
            )
    passed_count = sum(cell.promotion_eligible for cell in cells)
    if evaluation.passed_count != passed_count:
        raise SevenProductModelGovernanceError("evaluation_passed_count_invalid")
    expected_status = "passed" if passed_count == CURRENT_FORMAL_CELL_COUNT else "blocked"
    if evaluation.overall_status != expected_status:
        raise SevenProductModelGovernanceError("evaluation_status_invalid")
    report_body = {
        "as_of_time": evaluation.as_of_time,
        "cells": [cell.model_dump(mode="json") for cell in cells],
        "configuration_sha256": evaluation.evaluation_configuration_sha256,
        "data_snapshot_sha256": evaluation.data_snapshot_sha256,
        "schema_version": evaluation.schema_version,
    }
    report_sha256 = _digest(report_body)
    if evaluation.report_sha256 != report_sha256:
        raise SevenProductModelGovernanceError("evaluation_report_hash_invalid")
    if evaluation.evaluation_id != f"seven-eval-{report_sha256[:24]}":
        raise SevenProductModelGovernanceError("evaluation_id_invalid")
    return report_sha256


def _consecutive_losses_to_best_naive(
    settled_history: Sequence[Mapping[str, Any]], *, model_version: str
) -> int:
    comparable = [
        item
        for item in settled_history
        if item.get("model_version") == model_version
        and item.get("invalidated") is not True
        and _finite_nonnegative_number(item.get("model_absolute_error"))
        and _finite_nonnegative_number(item.get("persistence_absolute_error"))
        and _finite_nonnegative_number(item.get("seasonal_naive_absolute_error"))
    ]
    streak = 0
    for item in reversed(comparable):
        best_naive_error = min(
            float(item["persistence_absolute_error"]),
            float(item["seasonal_naive_absolute_error"]),
        )
        if float(item["model_absolute_error"]) <= best_naive_error:
            break
        streak += 1
    return streak


def _finite_nonnegative_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value) and value >= 0


def _timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SevenProductModelGovernanceError("governance_timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise SevenProductModelGovernanceError("governance_timestamp_timezone_required")
    return parsed.astimezone(UTC).isoformat()


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

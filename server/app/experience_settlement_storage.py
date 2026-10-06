from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from . import storage

PLAN_SCHEMA_VERSION = "experience-settlement-plan.v1"
RESULT_SCHEMA_VERSION = "experience-settlement-persistence-result.v1"
FORMAL_HORIZONS = (1, 7, 30)
STAGE_BY_HORIZON = {
    1: "d1_preliminary",
    7: "d7_intermediate",
    30: "d30_mature",
}
ITEM_STATUSES = ("planned", "pending", "unchanged", "blocked", "unavailable")
_RFC3339_CANONICAL = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)
_PLAN_FIELDS = {"schema_version", "evaluation_as_of", "items", "counts"}
_ITEM_FIELDS = {
    "settlement_key",
    "prediction_batch_id",
    "node_id",
    "subtarget",
    "target_series_id",
    "status",
    "reason_codes",
    "upstream_statuses",
    "actions",
    "next_checkpoint",
    "next_expected_date",
}
_ACTION_FIELDS = {"checkpoint_horizon", "expected_previous_revision_id", "card"}
_UPSTREAM_STATUS_FIELDS = {
    "prediction_status",
    "target_observation_status",
    "benchmark_observation_status",
    "target_series_status",
    "benchmark_series_status",
}
_PLANNED_UPSTREAM_STATUSES = {
    "prediction_status": "available",
    "target_observation_status": "available",
    "benchmark_observation_status": "available",
    "target_series_status": "eligible",
    "benchmark_series_status": "eligible",
}


class ExperienceSettlementPersistenceError(ValueError):
    """Raised before writes when a settlement plan violates its frozen v1 contract."""


def save_experience_settlement_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Persist each candidate independently and each candidate's actions atomically."""
    normalized = _validate_plan(plan)
    results: list[dict[str, Any]] = []
    for item in normalized["items"]:
        actions = item["actions"]
        if actions:
            batch = storage.save_experience_card_revision_batch([action["card"] for action in actions])
            status = batch["status"]
            action_results = batch["results"]
        else:
            status = item["status"]
            action_results = []
        results.append(
            {
                "settlement_key": item["settlement_key"],
                "status": status,
                "action_results": action_results,
            }
        )
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "evaluation_as_of": normalized["evaluation_as_of"],
        "items": results,
        "counts": {
            status: sum(item["status"] == status for item in results) for status in (*ITEM_STATUSES, "inserted")
        },
    }


def _validate_plan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, Mapping):
        raise ExperienceSettlementPersistenceError("experience_settlement_plan_required")
    if set(plan) != _PLAN_FIELDS or plan.get("schema_version") != PLAN_SCHEMA_VERSION:
        raise ExperienceSettlementPersistenceError("experience_settlement_plan_contract_invalid")
    evaluation_as_of = plan.get("evaluation_as_of")
    _validate_evaluation_as_of(evaluation_as_of)
    items = plan.get("items")
    if not _is_sequence(items):
        raise ExperienceSettlementPersistenceError("experience_settlement_items_invalid")
    normalized_items = [_validate_item(item, evaluation_as_of, index) for index, item in enumerate(items)]
    settlement_keys = [item["settlement_key"] for item in normalized_items]
    if len(settlement_keys) != len(set(settlement_keys)):
        raise ExperienceSettlementPersistenceError("experience_settlement_key_duplicate")
    revision_ids = [action["card"]["revision_id"] for item in normalized_items for action in item["actions"]]
    if len(revision_ids) != len(set(revision_ids)):
        raise ExperienceSettlementPersistenceError("experience_settlement_revision_id_duplicate")
    expected_counts = {status: sum(item["status"] == status for item in normalized_items) for status in ITEM_STATUSES}
    expected_counts["actions"] = sum(len(item["actions"]) for item in normalized_items)
    counts = plan.get("counts")
    if (
        not isinstance(counts, Mapping)
        or set(counts) != set(expected_counts)
        or any(isinstance(value, bool) or not isinstance(value, int) for value in counts.values())
        or dict(counts) != expected_counts
    ):
        raise ExperienceSettlementPersistenceError("experience_settlement_counts_invalid")
    return {
        "schema_version": PLAN_SCHEMA_VERSION,
        "evaluation_as_of": evaluation_as_of,
        "items": normalized_items,
        "counts": expected_counts,
    }


def _validate_item(item: Any, evaluation_as_of: str, index: int) -> dict[str, Any]:
    if not isinstance(item, Mapping) or set(item) != _ITEM_FIELDS:
        raise ExperienceSettlementPersistenceError(f"experience_settlement_item_contract_invalid:{index}")
    status = item.get("status")
    if status not in ITEM_STATUSES:
        raise ExperienceSettlementPersistenceError(f"experience_settlement_item_status_invalid:{index}")
    for field in ("settlement_key", "prediction_batch_id", "node_id", "target_series_id"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ExperienceSettlementPersistenceError(f"experience_settlement_item_identity_invalid:{index}")
    subtarget = item.get("subtarget")
    if subtarget not in (None, "poy", "dty"):
        raise ExperienceSettlementPersistenceError(f"experience_settlement_item_identity_invalid:{index}")
    expected_key = "/".join(
        (
            item["prediction_batch_id"],
            item["node_id"],
            subtarget or "",
            item["target_series_id"],
        )
    )
    if item["settlement_key"] != expected_key:
        raise ExperienceSettlementPersistenceError(f"experience_settlement_key_invalid:{index}")
    reason_codes = item.get("reason_codes")
    if not _is_sequence(reason_codes) or not all(isinstance(reason, str) and reason for reason in reason_codes):
        raise ExperienceSettlementPersistenceError(f"experience_settlement_reason_codes_invalid:{index}")
    upstream_statuses = item.get("upstream_statuses")
    if (
        not isinstance(upstream_statuses, Mapping)
        or set(upstream_statuses) != _UPSTREAM_STATUS_FIELDS
        or not all(isinstance(value, str) and value for value in upstream_statuses.values())
    ):
        raise ExperienceSettlementPersistenceError(f"experience_settlement_upstream_statuses_invalid:{index}")
    if status == "planned" and dict(upstream_statuses) != _PLANNED_UPSTREAM_STATUSES:
        raise ExperienceSettlementPersistenceError(f"experience_settlement_planned_upstream_status_invalid:{index}")
    actions = item.get("actions")
    if not _is_sequence(actions):
        raise ExperienceSettlementPersistenceError(f"experience_settlement_actions_invalid:{index}")
    if (status == "planned") != bool(actions):
        raise ExperienceSettlementPersistenceError(f"experience_settlement_status_actions_mismatch:{index}")
    if status in {"blocked", "unavailable"} and not reason_codes:
        raise ExperienceSettlementPersistenceError(f"experience_settlement_reason_codes_invalid:{index}")
    if status not in {"blocked", "unavailable"} and reason_codes:
        raise ExperienceSettlementPersistenceError(f"experience_settlement_reason_codes_invalid:{index}")
    normalized_actions = [
        _validate_action(action, item, evaluation_as_of, index, action_index)
        for action_index, action in enumerate(actions)
    ]
    _validate_action_chain(normalized_actions, index)
    next_checkpoint = item.get("next_checkpoint")
    if next_checkpoint is not None and (isinstance(next_checkpoint, bool) or next_checkpoint not in FORMAL_HORIZONS):
        raise ExperienceSettlementPersistenceError(f"experience_settlement_next_checkpoint_invalid:{index}")
    next_expected_date = item.get("next_expected_date")
    if next_expected_date is not None and not isinstance(next_expected_date, str):
        raise ExperienceSettlementPersistenceError(f"experience_settlement_next_expected_date_invalid:{index}")
    return {**dict(item), "reason_codes": list(reason_codes), "actions": normalized_actions}


def _validate_action(
    action: Any,
    item: Mapping[str, Any],
    evaluation_as_of: str,
    item_index: int,
    action_index: int,
) -> dict[str, Any]:
    location = f"{item_index}:{action_index}"
    if not isinstance(action, Mapping) or set(action) != _ACTION_FIELDS:
        raise ExperienceSettlementPersistenceError(f"experience_settlement_action_contract_invalid:{location}")
    card = action.get("card")
    if not isinstance(card, Mapping):
        raise ExperienceSettlementPersistenceError(f"experience_settlement_action_card_invalid:{location}")
    horizon = action.get("checkpoint_horizon")
    if isinstance(horizon, bool) or horizon not in FORMAL_HORIZONS:
        raise ExperienceSettlementPersistenceError(f"experience_settlement_action_horizon_invalid:{location}")
    if card.get("horizon_days") != horizon or card.get("maturity_stage") != STAGE_BY_HORIZON[horizon]:
        raise ExperienceSettlementPersistenceError(f"experience_settlement_action_horizon_invalid:{location}")
    if action.get("expected_previous_revision_id") != card.get("previous_revision_id"):
        raise ExperienceSettlementPersistenceError(f"experience_settlement_action_predecessor_invalid:{location}")
    if card.get("evaluation_as_of") != evaluation_as_of:
        raise ExperienceSettlementPersistenceError(f"experience_settlement_action_evaluation_asof_invalid:{location}")
    for item_field, card_field in (
        ("prediction_batch_id", "prediction_batch_id"),
        ("node_id", "node_id"),
        ("subtarget", "subtarget"),
        ("target_series_id", "target_series_id"),
    ):
        if item.get(item_field) != card.get(card_field):
            raise ExperienceSettlementPersistenceError(f"experience_settlement_action_identity_invalid:{location}")
    return {**dict(action), "card": dict(card)}


def _validate_action_chain(actions: Sequence[Mapping[str, Any]], item_index: int) -> None:
    revisions: set[str] = set()
    fingerprints: set[str] = set()
    previous: Mapping[str, Any] | None = None
    for action_index, action in enumerate(actions):
        card = action["card"]
        revision_id = card.get("revision_id")
        fingerprint = card.get("calculation_fingerprint")
        if not isinstance(revision_id, str) or not revision_id or revision_id in revisions:
            raise ExperienceSettlementPersistenceError(
                f"experience_settlement_action_revision_invalid:{item_index}:{action_index}"
            )
        if not isinstance(fingerprint, str) or not fingerprint or fingerprint in fingerprints:
            raise ExperienceSettlementPersistenceError(
                f"experience_settlement_action_fingerprint_invalid:{item_index}:{action_index}"
            )
        revisions.add(revision_id)
        fingerprints.add(fingerprint)
        if previous is not None and action["expected_previous_revision_id"] != previous["revision_id"]:
            raise ExperienceSettlementPersistenceError(
                f"experience_settlement_action_chain_invalid:{item_index}:{action_index}"
            )
        previous = card


def _is_sequence(value: Any) -> bool:
    return not isinstance(value, (str, bytes, bytearray)) and isinstance(value, Sequence)


def _validate_evaluation_as_of(value: Any) -> None:
    if not isinstance(value, str) or not _RFC3339_CANONICAL.fullmatch(value):
        raise ExperienceSettlementPersistenceError("experience_settlement_evaluation_asof_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExperienceSettlementPersistenceError("experience_settlement_evaluation_asof_invalid") from exc
    if parsed.utcoffset() is None:
        raise ExperienceSettlementPersistenceError("experience_settlement_evaluation_asof_invalid")

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, datetime
from typing import Any

from .phase_a_contracts import EXPECTED_FORMAL_NODES

FORMAL_HORIZONS = (1, 7, 30)
HISTORICAL_READ_ONLY_HORIZON = 14
REPLAY_START = date(2025, 1, 1)
REPLAY_END = date(2026, 7, 1)
PRODUCTS = frozenset({"poy", "dty"})
ITEM_KINDS = frozenset({"prediction", "settlement", "shadow_sample", "historical_prediction"})
SCOREABILITY = frozenset({"scorable", "blocked", "unscorable"})
MAX_CHECKPOINTS = (REPLAY_END - REPLAY_START).days + 1
MAX_ITEMS_PER_CHECKPOINT = 512
MAX_EVIDENCE_TIMES = 128
MAX_METRIC_VALUES = 64
MAX_EXCLUSION_REASONS = 16
MAX_COMPLETED_HORIZONS = len(FORMAL_HORIZONS)
MAX_TOTAL_ITEMS = 2_048
MAX_TOTAL_EVIDENCE_TIMES = 8_192
MAX_TOTAL_METRIC_ENTRIES = 4_096
MAX_TOTAL_ACTIONS = 4_096
MAX_ITEM_ID_LENGTH = 256
MAX_VERSION_LENGTH = 128
MAX_REASON_CODE_LENGTH = 128
MAX_METRIC_KEY_LENGTH = 128
MAX_ABS_METRIC_VALUE = 9_007_199_254_740_991
_RFC3339_CANONICAL = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)
_COMMON_ITEM_FIELDS = frozenset(
    {
        "item_id",
        "kind",
        "product",
        "node_id",
        "observed_at",
        "available_at",
        "evidence_times",
        "evidence_status",
        "scoreability",
        "exclusion_reasons",
        "metric_values",
    }
)
_KIND_FIELDS = {
    "prediction": frozenset({"horizon"}),
    "historical_prediction": frozenset({"horizon"}),
    "settlement": frozenset({"matured_horizon", "matured_at", "completed_horizons"}),
    "shadow_sample": frozenset({"horizon", "shadow_policy_version"}),
}


class SequentialReplayInputError(ValueError):
    """Raised when a replay cannot be proven point-in-time safe."""


def plan_sequential_replay(
    *,
    checkpoints: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Return deterministic replay actions without performing any I/O.

    The explicit policy supplies the governed checkpoint dates; this planner does
    not guess exchange calendars, weekends, holidays, or catch-up sessions.
    """
    normalized_policy = _policy(policy)
    checkpoint_items = _bounded_sequence(checkpoints, "checkpoints", maximum=MAX_CHECKPOINTS)
    if not checkpoint_items:
        raise SequentialReplayInputError("checkpoints must not be empty")

    budget = {
        "items": 0,
        "evidence_times": 0,
        "metric_entries": 0,
        "actions": 0,
    }
    normalized_checkpoints = []
    previous_as_of: datetime | None = None
    previous_day: date | None = None
    for index, raw_checkpoint in enumerate(checkpoint_items):
        checkpoint = _exact_dict(raw_checkpoint, f"checkpoints[{index}]")
        if set(checkpoint) != {"as_of", "items"}:
            raise SequentialReplayInputError(f"checkpoints[{index}] fields are invalid")
        as_of_text = _required_text(checkpoint["as_of"], f"checkpoints[{index}].as_of")
        as_of = _zoned_datetime(as_of_text, f"checkpoints[{index}].as_of")
        as_of_day = date.fromisoformat(as_of_text[:10])
        if not REPLAY_START <= as_of_day <= REPLAY_END:
            raise SequentialReplayInputError("checkpoint is outside the frozen replay range")
        if previous_as_of is not None and as_of <= previous_as_of:
            raise SequentialReplayInputError("checkpoints must be strictly ordered by as_of")
        if previous_day is not None and as_of_day <= previous_day:
            raise SequentialReplayInputError("only one checkpoint is allowed per calendar date")
        previous_as_of = as_of
        previous_day = as_of_day
        items = _bounded_sequence(
            checkpoint["items"],
            f"checkpoints[{index}].items",
            maximum=MAX_ITEMS_PER_CHECKPOINT,
        )
        _consume_budget(
            budget,
            "items",
            len(items),
            maximum=MAX_TOTAL_ITEMS,
        )
        normalized_checkpoints.append(
            {
                "as_of": as_of_text,
                "as_of_datetime": as_of,
                "replay_date": as_of_day,
                "items": items,
            }
        )

    _validate_run_coverage(normalized_checkpoints, normalized_policy)
    seen_identities: set[tuple[str, str, str, str]] = set()
    checkpoint_outputs = [
        _plan_checkpoint(checkpoint, normalized_policy, seen_identities, budget)
        for checkpoint in normalized_checkpoints
    ]
    all_actions = [action for checkpoint in checkpoint_outputs for action in checkpoint["actions"]]
    all_diagnostics = [diagnostic for checkpoint in checkpoint_outputs for diagnostic in checkpoint["diagnostics"]]
    groups = _group_summary(all_actions, all_diagnostics)
    status_counts = Counter(item["status"] for item in all_diagnostics)
    return {
        "schema_version": "sequential-replay-plan.v1",
        "policy_version": normalized_policy["policy_version"],
        "run_mode": normalized_policy["run_mode"],
        "expected_checkpoint_dates": [value.isoformat() for value in normalized_policy["expected_checkpoint_dates"]],
        "replay_range": {
            "start": REPLAY_START.isoformat(),
            "end": REPLAY_END.isoformat(),
            "boundaries_inclusive": True,
        },
        "checkpoint_count": len(checkpoint_outputs),
        "action_count": len(all_actions),
        "diagnostic_counts": dict(sorted(status_counts.items())),
        "checkpoints": checkpoint_outputs,
        "groups": groups,
    }


def _policy(raw: Any) -> dict[str, Any]:
    policy = _exact_dict(raw, "policy")
    required = {
        "policy_version",
        "run_mode",
        "reconstructed_evidence_handling",
        "expected_checkpoint_dates",
    }
    if set(policy) != required:
        raise SequentialReplayInputError("policy fields are invalid")
    policy_version = _required_text(
        policy["policy_version"],
        "policy.policy_version",
        maximum=MAX_VERSION_LENGTH,
    )
    run_mode = policy["run_mode"]
    if type(run_mode) is not str or run_mode not in {"dry_run", "full"}:
        raise SequentialReplayInputError("policy.run_mode must be dry_run or full")
    handling = policy["reconstructed_evidence_handling"]
    if type(handling) is not str or handling != "exclude_unscorable":
        raise SequentialReplayInputError("policy.reconstructed_evidence_handling must be exclude_unscorable")
    expected_dates = tuple(
        _replay_date(value, "policy.expected_checkpoint_dates")
        for value in _bounded_sequence(
            policy["expected_checkpoint_dates"],
            "policy.expected_checkpoint_dates",
            maximum=MAX_CHECKPOINTS,
        )
    )
    if not expected_dates:
        raise SequentialReplayInputError("policy.expected_checkpoint_dates must not be empty")
    if tuple(sorted(set(expected_dates))) != expected_dates:
        raise SequentialReplayInputError("policy.expected_checkpoint_dates must be strictly increasing and unique")
    if expected_dates[0] < REPLAY_START or expected_dates[-1] > REPLAY_END:
        raise SequentialReplayInputError("policy checkpoint date is outside the frozen replay range")
    if run_mode == "full" and (expected_dates[0], expected_dates[-1]) != (
        REPLAY_START,
        REPLAY_END,
    ):
        raise SequentialReplayInputError("full replay policy must include both frozen boundaries")
    return {
        "policy_version": policy_version,
        "run_mode": run_mode,
        "reconstructed_evidence_handling": handling,
        "expected_checkpoint_dates": expected_dates,
    }


def _validate_run_coverage(checkpoints: list[dict[str, Any]], policy: dict[str, Any]) -> None:
    actual_days = [checkpoint["replay_date"] for checkpoint in checkpoints]
    if actual_days != list(policy["expected_checkpoint_dates"]):
        raise SequentialReplayInputError("checkpoints must exactly match the explicit policy checkpoint dates")


def _plan_checkpoint(
    checkpoint: dict[str, Any],
    policy: dict[str, Any],
    seen_identities: set[tuple[str, str, str, str]],
    budget: dict[str, int],
) -> dict[str, Any]:
    as_of = checkpoint["as_of_datetime"]
    normalized_items = [
        _normalize_item(raw, index, as_of, policy, budget) for index, raw in enumerate(checkpoint["items"])
    ]
    identity_keys = [item["identity_key"] for item in normalized_items]
    if len(identity_keys) != len(set(identity_keys)):
        raise SequentialReplayInputError("duplicate replay item identity in checkpoint")
    repeated = set(identity_keys) & seen_identities
    if repeated:
        raise SequentialReplayInputError("duplicate replay item identity across checkpoints")
    seen_identities.update(identity_keys)

    actions: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for item in sorted(normalized_items, key=lambda value: value["sort_key"]):
        item_actions, diagnostic = _plan_item(item, checkpoint["as_of"])
        _consume_budget(
            budget,
            "actions",
            len(item_actions),
            maximum=MAX_TOTAL_ACTIONS,
        )
        actions.extend(item_actions)
        diagnostics.append(diagnostic)
    actions.sort(key=_action_sort_key)
    diagnostics.sort(key=_diagnostic_sort_key)
    return {
        "replay_date": checkpoint["replay_date"].isoformat(),
        "as_of": checkpoint["as_of"],
        "actions": actions,
        "diagnostics": diagnostics,
    }


def _normalize_item(
    raw: Any,
    index: int,
    as_of: datetime,
    policy: dict[str, Any],
    budget: dict[str, int],
) -> dict[str, Any]:
    item = _exact_dict(raw, f"items[{index}]")
    kind = item.get("kind")
    if type(kind) is not str or kind not in ITEM_KINDS:
        raise SequentialReplayInputError(f"items[{index}].kind is invalid")
    if set(item) != _COMMON_ITEM_FIELDS | _KIND_FIELDS[kind]:
        raise SequentialReplayInputError(f"items[{index}] fields are invalid for {kind}")

    item_id = _required_text(
        item["item_id"],
        f"items[{index}].item_id",
        maximum=MAX_ITEM_ID_LENGTH,
    )
    product = item["product"]
    if type(product) is not str or product not in PRODUCTS:
        raise SequentialReplayInputError(f"items[{index}].product must be poy or dty")
    node_id = _required_text(item["node_id"], f"items[{index}].node_id")
    if node_id not in EXPECTED_FORMAL_NODES:
        raise SequentialReplayInputError(f"items[{index}].node_id is not a formal Phase A node")
    observed_at = _visible_time(item["observed_at"], f"items[{index}].observed_at", as_of)
    available_at = _visible_time(item["available_at"], f"items[{index}].available_at", as_of)
    if available_at < observed_at:
        raise SequentialReplayInputError(f"items[{index}].available_at precedes observed_at")
    evidence_times = _bounded_sequence(
        item["evidence_times"],
        f"items[{index}].evidence_times",
        maximum=MAX_EVIDENCE_TIMES,
    )
    _consume_budget(
        budget,
        "evidence_times",
        len(evidence_times),
        maximum=MAX_TOTAL_EVIDENCE_TIMES,
    )
    normalized_evidence_times = [
        _visible_time(value, f"items[{index}].evidence_times", as_of).isoformat() for value in evidence_times
    ]
    evidence_status = item["evidence_status"]
    if type(evidence_status) is not str or evidence_status not in {
        "point_in_time",
        "reconstructed",
    }:
        raise SequentialReplayInputError(f"items[{index}].evidence_status is invalid")
    if evidence_status == "point_in_time" and not evidence_times:
        raise SequentialReplayInputError("point_in_time evidence_times must not be empty")
    scoreability = item["scoreability"]
    if type(scoreability) is not str or scoreability not in SCOREABILITY:
        raise SequentialReplayInputError(f"items[{index}].scoreability is invalid")
    exclusion_reasons = [
        _reason_code(reason, f"items[{index}].exclusion_reasons")
        for reason in _bounded_sequence(
            item["exclusion_reasons"],
            f"items[{index}].exclusion_reasons",
            maximum=MAX_EXCLUSION_REASONS,
        )
    ]
    if scoreability == "scorable" and exclusion_reasons:
        raise SequentialReplayInputError("scorable item cannot have exclusion reasons")
    if scoreability != "scorable" and not exclusion_reasons:
        raise SequentialReplayInputError("excluded item must have an exclusion reason")
    if evidence_status == "reconstructed":
        scoreability = "unscorable"
        exclusion_reasons.append("reconstructed_evidence")
    exclusion_reasons = sorted(set(exclusion_reasons))
    if len(exclusion_reasons) > MAX_EXCLUSION_REASONS:
        raise SequentialReplayInputError(
            f"items[{index}].exclusion_reasons exceeds the maximum of {MAX_EXCLUSION_REASONS} after system reasons"
        )
    metric_values = _metric_values(
        item["metric_values"],
        f"items[{index}].metric_values",
        budget,
    )

    normalized: dict[str, Any] = {
        "item_id": item_id,
        "kind": kind,
        "product": product,
        "node_id": node_id,
        "observed_at": observed_at.isoformat(),
        "available_at": available_at.isoformat(),
        "evidence_times": normalized_evidence_times,
        "evidence_status": evidence_status,
        "scoreability": scoreability,
        "exclusion_reasons": exclusion_reasons,
        "metric_values": metric_values,
        "identity_key": (product, node_id, kind, item_id),
        "sort_key": (product, node_id, kind, item_id),
        "policy_version": policy["policy_version"],
    }
    _normalize_kind_fields(item, normalized, index, as_of)
    return normalized


def _normalize_kind_fields(item: dict[str, Any], normalized: dict[str, Any], index: int, as_of: datetime) -> None:
    kind = normalized["kind"]
    if kind in {"prediction", "historical_prediction", "shadow_sample"}:
        horizon = _horizon(item["horizon"], f"items[{index}].horizon", allow_historical=True)
        if kind == "historical_prediction" and horizon != HISTORICAL_READ_ONLY_HORIZON:
            raise SequentialReplayInputError("historical_prediction requires D+14")
        if kind != "historical_prediction" and horizon == HISTORICAL_READ_ONLY_HORIZON:
            raise SequentialReplayInputError("D+14 is historical read-only and cannot create an action")
        normalized["horizon"] = horizon
    if kind == "shadow_sample":
        normalized["shadow_policy_version"] = _required_text(
            item["shadow_policy_version"],
            f"items[{index}].shadow_policy_version",
            maximum=MAX_VERSION_LENGTH,
        )
    if kind == "settlement":
        matured_horizon = _horizon(item["matured_horizon"], f"items[{index}].matured_horizon", allow_historical=False)
        matured_at = _visible_time(item["matured_at"], f"items[{index}].matured_at", as_of)
        completed = tuple(
            _horizon(value, f"items[{index}].completed_horizons", allow_historical=False)
            for value in _bounded_sequence(
                item["completed_horizons"],
                f"items[{index}].completed_horizons",
                maximum=MAX_COMPLETED_HORIZONS,
            )
        )
        prefixes = ((), (1,), (1, 7), (1, 7, 30))
        if completed not in prefixes:
            raise SequentialReplayInputError("completed_horizons must be an ordered formal prefix")
        if completed and completed[-1] > matured_horizon:
            raise SequentialReplayInputError("completed_horizons exceed matured_horizon")
        normalized.update(
            {
                "matured_horizon": matured_horizon,
                "matured_at": matured_at.isoformat(),
                "completed_horizons": completed,
            }
        )


def _plan_item(item: dict[str, Any], as_of: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    base = {
        "item_id": item["item_id"],
        "kind": item["kind"],
        "product": item["product"],
        "node_id": item["node_id"],
    }
    if item["scoreability"] != "scorable":
        return [], {
            **base,
            "status": item["scoreability"],
            "reason_codes": item["exclusion_reasons"],
        }
    if item["kind"] == "historical_prediction":
        return [], {
            **base,
            "status": "historical_read_only",
            "reason_codes": ["d14_historical_read_only"],
        }

    action_base = {
        "as_of": as_of,
        "source_item_id": item["item_id"],
        "product": item["product"],
        "node_id": item["node_id"],
        "observed_at": item["observed_at"],
        "available_at": item["available_at"],
        "evidence_times": item["evidence_times"],
        "evidence_status": item["evidence_status"],
        "metric_values": item["metric_values"],
        "replay_policy_version": item["policy_version"],
    }
    if item["kind"] == "prediction":
        actions = [{**action_base, "action": "freeze_prediction", "horizon": item["horizon"]}]
    elif item["kind"] == "shadow_sample":
        actions = [
            {
                **action_base,
                "action": "assemble_shadow_sample",
                "horizon": item["horizon"],
                "shadow_policy_version": item["shadow_policy_version"],
            }
        ]
    else:
        completed = set(item["completed_horizons"])
        actions = [
            {
                **action_base,
                "action": "plan_experience_settlement",
                "horizon": horizon,
                "matured_at": item["matured_at"],
                "mode": "catch_up",
            }
            for horizon in FORMAL_HORIZONS
            if horizon <= item["matured_horizon"] and horizon not in completed
        ]
        if item["matured_horizon"] == 30 and item["completed_horizons"] == FORMAL_HORIZONS:
            actions.append(
                {
                    **action_base,
                    "action": "plan_experience_settlement",
                    "horizon": 30,
                    "matured_at": item["matured_at"],
                    "mode": "d30_recheck",
                }
            )
    for action in actions:
        action["idempotency_key"] = [
            action["as_of"],
            action["product"],
            action["node_id"],
            action["action"],
            action["source_item_id"],
            action["horizon"],
        ]
    return actions, {**base, "status": "planned", "reason_codes": []}


def _group_summary(actions: list[dict[str, Any]], diagnostics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = sorted({(item["product"], item["node_id"]) for item in [*actions, *diagnostics]})
    return [
        {
            "product": product,
            "node_id": node_id,
            "action_count": sum(item["product"] == product and item["node_id"] == node_id for item in actions),
            "diagnostic_count": sum(item["product"] == product and item["node_id"] == node_id for item in diagnostics),
        }
        for product, node_id in keys
    ]


def _metric_values(
    value: Any,
    field: str,
    budget: dict[str, int],
) -> dict[str, float | int]:
    values = _exact_dict(value, field)
    if len(values) > MAX_METRIC_VALUES:
        raise SequentialReplayInputError(f"{field} exceeds the maximum of {MAX_METRIC_VALUES}")
    _consume_budget(
        budget,
        "metric_entries",
        len(values),
        maximum=MAX_TOTAL_METRIC_ENTRIES,
    )
    normalized: dict[str, float | int] = {}
    for key, raw in values.items():
        name = _required_text(key, f"{field} key", maximum=MAX_METRIC_KEY_LENGTH)
        if type(raw) not in {int, float} or (type(raw) is float and not math.isfinite(raw)):
            raise SequentialReplayInputError(f"{field}.{name} must be a finite number")
        if abs(raw) > MAX_ABS_METRIC_VALUE:
            raise SequentialReplayInputError(f"{field}.{name} exceeds the JSON-safe numeric magnitude")
        normalized[name] = raw
    return dict(sorted(normalized.items()))


def _horizon(value: Any, field: str, *, allow_historical: bool) -> int:
    if type(value) is not int:
        raise SequentialReplayInputError(f"{field} must be an integer horizon")
    allowed = {*FORMAL_HORIZONS}
    if allow_historical:
        allowed.add(HISTORICAL_READ_ONLY_HORIZON)
    if value not in allowed:
        raise SequentialReplayInputError(f"{field} is not an allowed horizon")
    return value


def _visible_time(value: Any, field: str, as_of: datetime) -> datetime:
    parsed = _zoned_datetime(value, field)
    if parsed > as_of:
        raise SequentialReplayInputError(f"{field} is later than the checkpoint as_of")
    return parsed


def _zoned_datetime(value: Any, field: str) -> datetime:
    text = _required_text(value, field)
    if not _RFC3339_CANONICAL.fullmatch(text) or text.endswith("-00:00"):
        raise SequentialReplayInputError(f"{field} must be canonical RFC3339")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SequentialReplayInputError(f"{field} must be canonical RFC3339") from exc
    if parsed.utcoffset() is None:
        raise SequentialReplayInputError(f"{field} must contain a UTC offset")
    return parsed


def _replay_date(value: Any, field: str) -> date:
    text = _required_text(value, field)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise SequentialReplayInputError(f"{field} must contain ISO calendar dates")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise SequentialReplayInputError(f"{field} must contain ISO calendar dates") from exc


def _required_text(value: Any, field: str, *, maximum: int | None = None) -> str:
    if type(value) is not str or not value.strip() or value != value.strip():
        raise SequentialReplayInputError(f"{field} must be a non-empty string")
    if maximum is not None and len(value) > maximum:
        raise SequentialReplayInputError(f"{field} exceeds the maximum length of {maximum}")
    return value


def _reason_code(value: Any, field: str) -> str:
    reason = _required_text(value, field, maximum=MAX_REASON_CODE_LENGTH)
    if not re.fullmatch(r"[a-z][a-z0-9_.:-]*", reason):
        raise SequentialReplayInputError(f"{field} must contain stable reason codes")
    return reason


def _exact_dict(value: Any, field: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise SequentialReplayInputError(f"{field} must be a plain dict")
    return value


def _sequence(value: Any, field: str) -> list[Any] | tuple[Any, ...]:
    if type(value) not in {list, tuple}:
        raise SequentialReplayInputError(f"{field} must be a list or tuple")
    return value


def _bounded_sequence(
    value: Any,
    field: str,
    *,
    maximum: int,
) -> list[Any] | tuple[Any, ...]:
    sequence = _sequence(value, field)
    if len(sequence) > maximum:
        raise SequentialReplayInputError(f"{field} exceeds the maximum of {maximum}")
    return sequence


def _consume_budget(
    budget: dict[str, int],
    name: str,
    amount: int,
    *,
    maximum: int,
) -> None:
    updated = budget[name] + amount
    if updated > maximum:
        raise SequentialReplayInputError(f"total {name} exceeds the maximum of {maximum}")
    budget[name] = updated


def _action_sort_key(action: dict[str, Any]) -> tuple[str, str, str, str, int]:
    action_rank = {
        "freeze_prediction": "0",
        "plan_experience_settlement": "1",
        "assemble_shadow_sample": "2",
    }
    return (
        action["product"],
        action["node_id"],
        action_rank[action["action"]],
        action["source_item_id"],
        action["horizon"],
    )


def _diagnostic_sort_key(diagnostic: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        diagnostic["product"],
        diagnostic["node_id"],
        diagnostic["kind"],
        diagnostic["item_id"],
    )

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from .experience_cards import build_experience_card_revision

FORMAL_HORIZONS = (1, 7, 30)
STAGE_TO_HORIZON = {
    "d1_preliminary": 1,
    "d7_intermediate": 7,
    "d30_mature": 30,
}
TERMINAL_NODE = "poy_dty_upstream_cost_pressure"
_RFC3339_CANONICAL = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)


class ExperienceSettlementInputError(ValueError):
    """Raised when a deterministic settlement plan cannot be formed from explicit inputs."""


def plan_due_experience_settlements(
    *,
    candidates: Sequence[Mapping[str, Any]],
    evaluation_as_of: str,
) -> dict[str, Any]:
    """Return a deterministic, side-effect-free settlement plan for explicit candidates."""
    evaluation_time = _parse_zoned_datetime(evaluation_as_of, "evaluation_as_of")
    _validate_sequence(candidates, "candidates")
    normalized = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, Mapping):
            raise ExperienceSettlementInputError(f"candidates[{index}] must be a mapping")
        normalized.append(_normalize_candidate(candidate, evaluation_time))
    keys = [item[0] for item in normalized]
    if len(keys) != len(set(keys)):
        raise ExperienceSettlementInputError("duplicate settlement candidate identity")

    items = [
        _plan_candidate(candidate, settlement_key, evaluation_as_of)
        for settlement_key, candidate in sorted(normalized, key=lambda item: item[0])
    ]
    counts = {
        status: sum(item["status"] == status for item in items)
        for status in ("planned", "pending", "unchanged", "blocked", "unavailable")
    }
    counts["actions"] = sum(len(item["actions"]) for item in items)
    return {
        "schema_version": "experience-settlement-plan.v1",
        "evaluation_as_of": evaluation_as_of,
        "items": items,
        "counts": counts,
    }


def _normalize_candidate(
    candidate: Mapping[str, Any], evaluation_time: datetime
) -> tuple[tuple[str, str, str, str], dict[str, Any]]:
    required = {
        "prediction_bundle",
        "target_points",
        "benchmark_points",
        "expected_observation_dates",
        "prediction_status",
        "target_observation_status",
        "benchmark_observation_status",
        "target_series_status",
        "benchmark_series_status",
    }
    missing = required - set(candidate)
    if missing:
        raise ExperienceSettlementInputError(f"settlement candidate missing fields: {sorted(missing)}")
    if not isinstance(candidate["prediction_bundle"], Mapping):
        raise ExperienceSettlementInputError("prediction_bundle must be a mapping")
    bundle = dict(candidate["prediction_bundle"])
    for field in ("target_points", "benchmark_points"):
        _validate_sequence(candidate[field], field)
        if not all(isinstance(point, Mapping) for point in candidate[field]):
            raise ExperienceSettlementInputError(f"{field} items must be mappings")
    _validate_sequence(candidate["expected_observation_dates"], "expected_observation_dates")
    if not all(isinstance(item, str) for item in candidate["expected_observation_dates"]):
        raise ExperienceSettlementInputError("expected_observation_dates items must be strings")
    _validate_bundle_identity_and_horizons(bundle)
    _parse_zoned_datetime(bundle["as_of_time"], "prediction_bundle.as_of_time")
    for field in (
        "prediction_status",
        "target_observation_status",
        "benchmark_observation_status",
        "target_series_status",
        "benchmark_series_status",
    ):
        _validate_upstream_status(candidate[field], field)
    previous = candidate.get("previous_revision")
    if previous is not None:
        if not isinstance(previous, Mapping):
            raise ExperienceSettlementInputError("previous_revision must be a mapping or null")
        stage = previous.get("maturity_stage")
        if stage not in STAGE_TO_HORIZON:
            raise ExperienceSettlementInputError("previous revision maturity is invalid")
        previous_evaluation = _parse_zoned_datetime(
            previous.get("evaluation_as_of"), "previous_revision.evaluation_as_of"
        )
        if evaluation_time < previous_evaluation:
            raise ExperienceSettlementInputError("evaluation_as_of cannot precede previous revision evaluation_as_of")
    normalized = dict(candidate)
    normalized["prediction_bundle"] = bundle
    normalized["previous_revision"] = dict(previous) if previous is not None else None
    key = (
        str(bundle["prediction_batch_id"]),
        str(bundle["node_id"]),
        str(bundle.get("subtarget") or ""),
        str(bundle["target_series_id"]),
    )
    return key, normalized


def _validate_bundle_identity_and_horizons(bundle: Mapping[str, Any]) -> None:
    required = {
        "prediction_batch_id",
        "node_id",
        "subtarget",
        "target_series_id",
        "benchmark_series_id",
        "as_of_time",
        "horizons",
    }
    missing = required - set(bundle)
    if missing:
        raise ExperienceSettlementInputError(f"prediction bundle missing fields: {sorted(missing)}")
    _validate_sequence(bundle["horizons"], "prediction horizons")
    horizons = tuple(sorted(_normalize_horizon(item) for item in bundle["horizons"]))
    if 14 in horizons:
        raise ExperienceSettlementInputError("D+14 is historical read-only and cannot enter a settlement plan")
    if horizons != FORMAL_HORIZONS:
        raise ExperienceSettlementInputError("settlement horizons must be exactly D+1, D+7 and D+30")
    for field in (
        "prediction_batch_id",
        "node_id",
        "target_series_id",
        "benchmark_series_id",
    ):
        if not isinstance(bundle[field], str) or not bundle[field].strip():
            raise ExperienceSettlementInputError(f"prediction bundle {field} must not be empty")
    subtarget = bundle.get("subtarget")
    if bundle["node_id"] == TERMINAL_NODE and subtarget not in {"poy", "dty"}:
        raise ExperienceSettlementInputError("terminal settlement candidate requires poy or dty")
    if bundle["node_id"] != TERMINAL_NODE and subtarget is not None:
        raise ExperienceSettlementInputError("non-terminal settlement candidate cannot use poy or dty")


def _normalize_horizon(value: Any) -> int:
    if isinstance(value, bool):
        raise ExperienceSettlementInputError("horizon token must be exactly 1, 7 or 30")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value in {"1", "7", "14", "30"}:
        return int(value)
    raise ExperienceSettlementInputError("horizon token must be exactly 1, 7 or 30")


def _validate_upstream_status(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ExperienceSettlementInputError(f"{field} must be an explicit non-empty status")


def _plan_candidate(
    candidate: Mapping[str, Any],
    settlement_key: tuple[str, str, str, str],
    evaluation_as_of: str,
) -> dict[str, Any]:
    bundle = candidate["prediction_bundle"]
    base = {
        "settlement_key": "/".join(settlement_key),
        "prediction_batch_id": bundle["prediction_batch_id"],
        "node_id": bundle["node_id"],
        "subtarget": bundle.get("subtarget"),
        "target_series_id": bundle["target_series_id"],
        "actions": [],
    }
    upstream_statuses = {
        field: str(candidate[field])
        for field in (
            "prediction_status",
            "target_observation_status",
            "benchmark_observation_status",
            "target_series_status",
            "benchmark_series_status",
        )
    }
    unavailable_reasons = [
        reason
        for field, reason in (
            ("prediction_status", "prediction_unavailable"),
            ("target_observation_status", "target_observations_unavailable"),
            ("benchmark_observation_status", "benchmark_observations_unavailable"),
            ("target_series_status", "target_series_unavailable"),
            ("benchmark_series_status", "benchmark_series_unavailable"),
        )
        if upstream_statuses[field] == "unavailable"
    ]
    if unavailable_reasons:
        return {
            **base,
            "status": "unavailable",
            "reason_codes": unavailable_reasons,
            "upstream_statuses": upstream_statuses,
            "next_checkpoint": _next_checkpoint(candidate.get("previous_revision")),
            "next_expected_date": None,
        }
    blocked_reasons = [
        reason
        for field, reason in (
            ("prediction_status", "prediction_blocked"),
            ("target_observation_status", "target_observations_blocked"),
            ("benchmark_observation_status", "benchmark_observations_blocked"),
        )
        if upstream_statuses[field] != "available"
    ]
    blocked_reasons.extend(
        reason
        for field, reason in (
            ("target_series_status", "target_series_blocked"),
            ("benchmark_series_status", "benchmark_series_blocked"),
        )
        if upstream_statuses[field] != "eligible"
    )
    if blocked_reasons:
        return {
            **base,
            "status": "blocked",
            "reason_codes": blocked_reasons,
            "upstream_statuses": upstream_statuses,
            "next_checkpoint": _next_checkpoint(candidate.get("previous_revision")),
            "next_expected_date": None,
        }

    previous = candidate.get("previous_revision")
    horizons = _remaining_horizons(previous)
    actions: list[dict[str, Any]] = []
    last_result: dict[str, Any] | None = None
    resolver = _series_resolver(bundle, candidate)
    for horizon in horizons:
        try:
            result = build_experience_card_revision(
                prediction_bundle=bundle,
                target_points=candidate["target_points"],
                benchmark_points=candidate["benchmark_points"],
                expected_observation_dates=candidate["expected_observation_dates"],
                evaluation_as_of=evaluation_as_of,
                series_eligibility_resolver=resolver,
                previous_revision=previous,
                checkpoint_horizon=horizon,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ExperienceSettlementInputError(str(exc)) from exc
        last_result = result
        if result["status"] == "pending":
            break
        if result["status"] == "blocked":
            return {
                **base,
                "status": "blocked",
                "reason_codes": list(result["blockers"]),
                "upstream_statuses": upstream_statuses,
                "next_checkpoint": result["next_checkpoint"],
                "next_expected_date": result["next_expected_date"],
            }
        if result["status"] == "unchanged":
            previous = result["card"]
            continue
        card = result["card"]
        actions.append(
            {
                "checkpoint_horizon": horizon,
                "expected_previous_revision_id": card["previous_revision_id"],
                "card": card,
            }
        )
        previous = card

    if actions:
        status = "planned"
    elif last_result and last_result["status"] == "pending":
        status = "pending"
    else:
        status = "unchanged"
    return {
        **base,
        "status": status,
        "reason_codes": [],
        "upstream_statuses": upstream_statuses,
        "actions": actions,
        "next_checkpoint": last_result["next_checkpoint"] if last_result else None,
        "next_expected_date": last_result["next_expected_date"] if last_result else None,
    }


def _remaining_horizons(previous: Mapping[str, Any] | None) -> tuple[int, ...]:
    if previous is None:
        return FORMAL_HORIZONS
    horizon = STAGE_TO_HORIZON[str(previous["maturity_stage"])]
    if horizon == 30:
        return (30,)
    return tuple(item for item in FORMAL_HORIZONS if item > horizon)


def _next_checkpoint(previous: Mapping[str, Any] | None) -> int | None:
    return _remaining_horizons(previous)[0]


def _series_resolver(bundle: Mapping[str, Any], candidate: Mapping[str, Any]):
    statuses = {
        str(bundle["target_series_id"]): str(candidate["target_series_status"]),
        str(bundle["benchmark_series_id"]): str(candidate["benchmark_series_status"]),
    }
    return lambda series_id: statuses.get(series_id, "unavailable")


def _validate_sequence(value: Any, field: str) -> None:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise ExperienceSettlementInputError(f"{field} must be a non-string sequence")


def _parse_zoned_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not _RFC3339_CANONICAL.fullmatch(value):
        raise ExperienceSettlementInputError(f"{field} must be canonical RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExperienceSettlementInputError(f"{field} must be canonical RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExperienceSettlementInputError(f"{field} must include a timezone")
    return parsed

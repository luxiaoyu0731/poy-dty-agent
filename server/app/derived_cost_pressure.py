"""Deterministic PTA/MEG cost-pressure observations for POY and DTY.

This module deliberately has no database or clock dependency.  Callers provide
only source observations that were already selected under an explicit as-of
cutoff.  The returned revision identity binds every derived point to the two
immutable capture revisions that produced it.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from . import phase_a_contracts


class CostPressureInputError(ValueError):
    """Raised for malformed or contradictory source-evidence input."""


_COMPONENT_SERIES = tuple(
    component["series_id"] for component in phase_a_contracts.DERIVED_COST_PRESSURE_FORMULA["components"]
)
_TARGET_SERIES = tuple(phase_a_contracts.DERIVED_COST_PRESSURE_FORMULA["shared_by_targets"])
_WINDOW_DAYS = 20


def derive_cost_pressure_observations(points: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive both target indices from exact common PTA/MEG observation dates.

    A caller must pass price evidence with a source observation identifier,
    capture revision, effective date, zoned first-visible timestamp, value,
    unit, quote type, quality and visibility mode.  The function never fills
    dates, chooses proxies or promotes source quality.  Fewer than 20 shared
    dates is a normal unavailable result rather than an inferred value.
    """

    if not _is_plain_sequence(points):
        raise CostPressureInputError("cost_pressure_points_must_be_a_sequence")
    formula = phase_a_contracts.load_contract().get("derived_cost_pressure_formula")
    if formula != phase_a_contracts.DERIVED_COST_PRESSURE_FORMULA:
        raise CostPressureInputError("cost_pressure_formula_contract_mismatch")

    normalized = _normalize_points(points)
    by_series = {
        series_id: {item["effective_date"]: item for item in normalized if item["series_id"] == series_id}
        for series_id in _COMPONENT_SERIES
    }
    common_dates = sorted(set(by_series[_COMPONENT_SERIES[0]]) & set(by_series[_COMPONENT_SERIES[1]]))
    if len(common_dates) < _WINDOW_DAYS:
        return {
            "status": "unavailable",
            "reason_codes": ["cost_pressure_common_history_insufficient"],
            "common_effective_dates": common_dates,
            "observations": [],
        }

    weights = [Decimal(str(component["weight"])) for component in formula["components"]]
    baskets = {
        effective_date: sum(
            (
                weights[index] * by_series[series_id][effective_date]["value"]
                for index, series_id in enumerate(_COMPONENT_SERIES)
            ),
            Decimal("0"),
        )
        for effective_date in common_dates
    }
    observations: list[dict[str, Any]] = []
    for position in range(_WINDOW_DAYS - 1, len(common_dates)):
        effective_date = common_dates[position]
        baseline_dates = common_dates[position - _WINDOW_DAYS + 1 : position + 1]
        baseline = sum((baskets[item] for item in baseline_dates), Decimal("0")) / Decimal(_WINDOW_DAYS)
        if baseline <= 0:
            raise CostPressureInputError("cost_pressure_nonpositive_baseline")
        components = [by_series[series_id][effective_date] for series_id in _COMPONENT_SERIES]
        baseline_components = [
            by_series[series_id][baseline_date]
            for baseline_date in baseline_dates
            for series_id in _COMPONENT_SERIES
        ]
        value = Decimal("100") * baskets[effective_date] / baseline
        for target_series_id in _TARGET_SERIES:
            observations.append(
                _derived_observation(
                    target_series_id=target_series_id,
                    effective_date=effective_date,
                    value=value,
                    baseline_dates=baseline_dates,
                    components=components,
                    baseline_components=baseline_components,
                )
            )
    return {
        "status": "ready",
        "reason_codes": [],
        "common_effective_dates": common_dates,
        "observations": observations,
    }


def _normalize_points(points: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in points:
        if not isinstance(item, Mapping):
            raise CostPressureInputError("cost_pressure_point_must_be_a_mapping")
        series_id = item.get("series_id")
        if series_id not in _COMPONENT_SERIES:
            continue
        effective_date = item.get("effective_date")
        if type(effective_date) is not str:
            raise CostPressureInputError("cost_pressure_effective_date_invalid")
        try:
            date.fromisoformat(effective_date)
        except ValueError as exc:
            raise CostPressureInputError("cost_pressure_effective_date_invalid") from exc
        key = (series_id, effective_date)
        if key in seen:
            raise CostPressureInputError("cost_pressure_duplicate_component_date")
        seen.add(key)
        value = _decimal(item.get("value"))
        if value <= 0:
            raise CostPressureInputError("cost_pressure_value_invalid")
        capture_revision_id = item.get("capture_revision_id")
        observation_id = item.get("observation_id")
        if not all(type(value) is str and value for value in (capture_revision_id, observation_id)):
            raise CostPressureInputError("cost_pressure_lineage_missing")
        first_visible_at = item.get("first_visible_at")
        if type(first_visible_at) is not str or _parse_zoned_datetime(first_visible_at) is None:
            raise CostPressureInputError("cost_pressure_visibility_invalid")
        if item.get("unit") != "CNY/mt" or type(item.get("quote_type")) is not str or not item["quote_type"]:
            raise CostPressureInputError("cost_pressure_unit_or_quote_invalid")
        if item.get("visibility_mode") != "strict_as_of":
            raise CostPressureInputError("cost_pressure_visibility_mode_invalid")
        quality_status = item.get("quality_status")
        if quality_status not in {"eligible", "blocked", "unavailable"}:
            raise CostPressureInputError("cost_pressure_quality_invalid")
        normalized.append(
            {
                "series_id": series_id,
                "effective_date": effective_date,
                "value": value,
                "observation_id": observation_id,
                "capture_revision_id": capture_revision_id,
                "first_visible_at": first_visible_at,
                "quote_type": item["quote_type"],
                "quality_status": quality_status,
            }
        )
    return normalized


def _derived_observation(
    *,
    target_series_id: str,
    effective_date: str,
    value: Decimal,
    baseline_dates: list[str],
    components: list[dict[str, Any]],
    baseline_components: list[dict[str, Any]],
) -> dict[str, Any]:
    first_visible_at = max(baseline_components, key=lambda item: _parse_zoned_datetime(item["first_visible_at"]))[
        "first_visible_at"
    ]
    quality_status = (
        "eligible" if all(item["quality_status"] == "eligible" for item in baseline_components) else "blocked"
    )
    lineage = {
        "formula_id": phase_a_contracts.DERIVED_COST_PRESSURE_FORMULA["formula_id"],
        "target_series_id": target_series_id,
        "effective_date": effective_date,
        "baseline_dates": baseline_dates,
        "current_components": [
            {
                "series_id": item["series_id"],
                "observation_id": item["observation_id"],
                "capture_revision_id": item["capture_revision_id"],
                "value": _decimal_string(item["value"]),
            }
            for item in components
        ],
        "baseline_components": [
            {
                "series_id": item["series_id"],
                "effective_date": item["effective_date"],
                "observation_id": item["observation_id"],
                "capture_revision_id": item["capture_revision_id"],
                "value": _decimal_string(item["value"]),
            }
            for item in baseline_components
        ],
    }
    lineage_json = json.dumps(lineage, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    lineage_hash = hashlib.sha256(lineage_json.encode("utf-8")).hexdigest()
    return {
        "observation_id": f"derived-cost-pressure:{target_series_id}:{effective_date}:{lineage_hash[:16]}",
        "series_id": target_series_id,
        "effective_date": effective_date,
        "value": float(value),
        "unit": "index",
        "quote_type": "derived_cost_pressure_index",
        "first_visible_at": first_visible_at,
        "quality_status": quality_status,
        "visibility_mode": "strict_as_of",
        "revision_id": f"derived-cost-pressure:{lineage_hash}",
        "supersedes_revision_id": None,
        "capture_revision_ids": [item["capture_revision_id"] for item in baseline_components],
        "lineage_sha256": lineage_hash,
    }


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise CostPressureInputError("cost_pressure_value_invalid")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CostPressureInputError("cost_pressure_value_invalid") from exc
    if not parsed.is_finite():
        raise CostPressureInputError("cost_pressure_value_invalid")
    return parsed


def _decimal_string(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _parse_zoned_datetime(value: str) -> datetime | None:
    if "T" not in value or value.endswith("-00:00"):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _is_plain_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))

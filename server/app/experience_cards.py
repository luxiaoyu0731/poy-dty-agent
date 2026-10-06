from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

FORMAL_HORIZONS = (1, 7, 30)
MATURITY_BY_HORIZON = {1: "d1_preliminary", 7: "d7_intermediate", 30: "d30_mature"}
MATURITY_ORDER = {stage: index for index, stage in enumerate(MATURITY_BY_HORIZON.values(), start=1)}
SCORABLE_SERIES_STATUSES = {"eligible"}
DIRECTION_MULTIPLIER = {"up": 1.0, "down": -1.0}
TERMINAL_NODE = "poy_dty_upstream_cost_pressure"

SeriesEligibilityResolver = Callable[[str], str]


class ExperienceCardInputError(ValueError):
    """Raised when an explicit computation input violates the runtime contract."""


def build_experience_card_revision(
    *,
    prediction_bundle: Mapping[str, Any],
    target_points: Sequence[Mapping[str, Any]],
    benchmark_points: Sequence[Mapping[str, Any]],
    expected_observation_dates: Sequence[str],
    evaluation_as_of: str,
    series_eligibility_resolver: SeriesEligibilityResolver,
    previous_revision: Mapping[str, Any] | None = None,
    checkpoint_horizon: int | str | None = None,
) -> dict[str, Any]:
    """Build one deterministic, side-effect-free Experience Card revision."""
    _validate_prediction_bundle(prediction_bundle)
    evaluation_time = _parse_zoned_datetime(evaluation_as_of, "evaluation_as_of")
    prediction_time = _parse_zoned_datetime(str(prediction_bundle["as_of_time"]), "prediction.as_of_time")
    if evaluation_time < prediction_time:
        raise ExperienceCardInputError("evaluation_as_of cannot precede prediction.as_of_time")

    expected_dates = _parse_expected_dates(expected_observation_dates, prediction_time.date())
    elapsed_dates = [item for item in expected_dates if item <= evaluation_time.date()]
    completed_horizon = _completed_checkpoint(len(elapsed_dates))
    requested_horizon = _normalize_checkpoint_horizon(checkpoint_horizon)
    if completed_horizon is None or (requested_horizon is not None and requested_horizon > completed_horizon):
        next_checkpoint = requested_horizon or 1
        return {
            "status": "pending",
            "card": None,
            "blockers": [],
            "completed_effective_days": len(elapsed_dates),
            "next_checkpoint": next_checkpoint,
            "next_expected_date": expected_dates[next_checkpoint - 1].isoformat(),
        }
    horizon = requested_horizon or completed_horizon

    maturity_stage = MATURITY_BY_HORIZON[horizon]
    identity = _identity(prediction_bundle)
    card_id = f"ec-{_digest(identity)[:20]}"
    transition_error = _transition_error(previous_revision, card_id, maturity_stage)
    if transition_error:
        return {
            "status": "blocked",
            "card": None,
            "blockers": [transition_error],
            "completed_effective_days": len(elapsed_dates),
            "next_checkpoint": _next_checkpoint(horizon),
            "next_expected_date": _next_expected_date(expected_dates, horizon),
        }

    window_dates = expected_dates[:horizon]
    window_date_set = set(window_dates)
    target_series_id = str(prediction_bundle["target_series_id"])
    benchmark_series_id = str(prediction_bundle["benchmark_series_id"])
    target_anchor_points, target_anchor_blockers = _resolve_points(
        target_points,
        target_series_id,
        prediction_time,
        effective_date_predicate=lambda item: item <= prediction_time.date(),
        report_future_visibility=False,
    )
    benchmark_anchor_points, benchmark_anchor_blockers = _resolve_points(
        benchmark_points,
        benchmark_series_id,
        prediction_time,
        effective_date_predicate=lambda item: item <= prediction_time.date(),
        report_future_visibility=False,
    )
    target_resolved, target_resolution_blockers = _resolve_points(
        target_points,
        target_series_id,
        evaluation_time,
        effective_date_predicate=window_date_set.__contains__,
    )
    benchmark_resolved, benchmark_resolution_blockers = _resolve_points(
        benchmark_points,
        benchmark_series_id,
        evaluation_time,
        effective_date_predicate=window_date_set.__contains__,
    )

    target_anchor = _anchor_point(target_anchor_points, prediction_time.date())
    benchmark_anchor = _anchor_point(benchmark_anchor_points, prediction_time.date())
    target_window = [target_resolved[item] for item in window_dates if item in target_resolved]
    benchmark_window = [benchmark_resolved[item] for item in window_dates if item in benchmark_resolved]
    target_by_date = {_point_date(item): item for item in target_window}
    benchmark_by_date = {_point_date(item): item for item in benchmark_window}

    blockers = [
        *target_anchor_blockers,
        *benchmark_anchor_blockers,
        *target_resolution_blockers,
        *benchmark_resolution_blockers,
    ]
    blockers.extend(_series_status_blockers(target_series_id, benchmark_series_id, series_eligibility_resolver))
    blockers.extend(_anchor_blockers("target", target_anchor, prediction_time))
    blockers.extend(_anchor_blockers("benchmark", benchmark_anchor, prediction_time))
    blockers.extend(_basis_blockers("target", target_anchor, target_window))
    blockers.extend(_basis_blockers("benchmark", benchmark_anchor, benchmark_window))
    blockers.extend(_price_blockers("target", target_anchor, target_window))
    blockers.extend(_price_blockers("benchmark", benchmark_anchor, benchmark_window))

    target_missing_dates = [item.isoformat() for item in window_dates if item not in target_by_date]
    benchmark_missing_dates = [item.isoformat() for item in window_dates if item not in benchmark_by_date]
    if target_missing_dates:
        blockers.append("target_effective_dates_missing")
    if benchmark_missing_dates:
        blockers.append("benchmark_effective_dates_missing")

    selected_points = [item for item in (target_anchor, benchmark_anchor) if item]
    selected_points.extend(target_window)
    selected_points.extend(benchmark_window)
    reconstructed = any(item.get("visibility_mode") == "reconstructed" for item in selected_points)
    if any(item.get("visibility_mode") != "strict_as_of" for item in selected_points):
        blockers.append("visibility_not_strict_as_of")
    if reconstructed:
        blockers.append("reconstructed_visibility")

    direction = _direction_for_horizon(prediction_bundle, horizon)
    metrics = _metrics(
        direction=direction,
        target_anchor=target_anchor,
        target_by_date=target_by_date,
        benchmark_anchor=benchmark_anchor,
        benchmark_by_date=benchmark_by_date,
        window_dates=window_dates,
    )
    if direction not in DIRECTION_MULTIPLIER:
        blockers.append("direction_not_scorable")

    target_completeness = len(target_by_date) / horizon
    benchmark_completeness = len(benchmark_by_date) / horizon
    data_completeness = {
        "target": round(target_completeness, 6),
        "benchmark": round(benchmark_completeness, 6),
        "overall": round(min(target_completeness, benchmark_completeness), 6),
        "target_missing_dates": target_missing_dates,
        "benchmark_missing_dates": benchmark_missing_dates,
    }
    blockers = sorted(set(blockers))
    scoreability = "scorable" if not blockers else "unscorable"
    visibility_mode = "reconstructed" if reconstructed else "strict_as_of"
    mechanism_support_status = _mechanism_support(direction, metrics.get("raw_change_pct"), scoreability)

    checkpoint_prediction_id = _checkpoint_prediction_id(prediction_bundle, horizon)
    calculation = {
        "identity": identity,
        "prediction_bundle": dict(prediction_bundle),
        "expected_observation_dates": [item.isoformat() for item in expected_dates],
        "checkpoint_prediction_id": checkpoint_prediction_id,
        "maturity_stage": maturity_stage,
        "horizon_days": horizon,
        "window_dates": [item.isoformat() for item in window_dates],
        "target_observations": [_point_fingerprint(item) for item in target_window],
        "benchmark_observations": [_point_fingerprint(item) for item in benchmark_window],
        "target_anchor": _point_fingerprint(target_anchor) if target_anchor else None,
        "benchmark_anchor": _point_fingerprint(benchmark_anchor) if benchmark_anchor else None,
        "metrics": metrics,
        "data_completeness": data_completeness,
        "blockers": blockers,
        "visibility_mode": visibility_mode,
    }
    calculation_fingerprint = _digest(calculation)
    if (
        previous_revision
        and previous_revision.get("maturity_stage") == maturity_stage
        and previous_revision.get("calculation_fingerprint") == calculation_fingerprint
    ):
        return {
            "status": "unchanged",
            "card": dict(previous_revision),
            "blockers": blockers,
            "completed_effective_days": len(elapsed_dates),
            "next_checkpoint": _next_checkpoint(horizon),
            "next_expected_date": _next_expected_date(expected_dates, horizon),
        }

    previous_revision_id = str(previous_revision["revision_id"]) if previous_revision else None
    revision_id = f"ecr-{_digest([card_id, maturity_stage, previous_revision_id, calculation_fingerprint])[:24]}"
    success_reasons = ["posterior_direction_matches_prediction"] if mechanism_support_status == "supported" else []
    failure_reasons = ["posterior_direction_opposes_prediction"] if mechanism_support_status == "unsupported" else []
    card = {
        "schema_version": "phase-a.experience-card.v1",
        "experience_card_id": card_id,
        "prediction_id": checkpoint_prediction_id,
        "node_id": prediction_bundle["node_id"],
        "revision_id": revision_id,
        "previous_revision_id": previous_revision_id,
        "as_of_time": prediction_bundle["as_of_time"],
        "evaluation_as_of": evaluation_as_of,
        "horizon_days": horizon,
        "maturity_stage": maturity_stage,
        "posterior_window_start": window_dates[0].isoformat(),
        "posterior_window_end": window_dates[-1].isoformat(),
        "start_price": metrics.get("start_price"),
        "end_price": metrics.get("end_price"),
        "raw_change_abs": metrics.get("raw_change_abs"),
        "raw_change_pct": metrics.get("raw_change_pct"),
        "benchmark_series_id": benchmark_series_id,
        "relative_change": metrics.get("relative_change"),
        "mfe": metrics.get("mfe"),
        "mae": metrics.get("mae"),
        "days_to_peak": metrics.get("days_to_peak"),
        "first_reversal_at": metrics.get("first_reversal_at"),
        "data_completeness": data_completeness,
        "scoreability": scoreability,
        "exclusion_reasons": blockers,
        "visibility_mode": visibility_mode,
        "mechanism_support_status": mechanism_support_status,
        "overlapping_event_ids": list(prediction_bundle.get("overlapping_event_ids", [])),
        "success_reasons": success_reasons,
        "failure_reasons": failure_reasons,
        "horizon_mismatch": False if scoreability == "scorable" else None,
        "reusable_experience": [],
        "counterexamples": [],
        "candidate_factor_ids": [],
        "applicability_conditions": [],
        "eligible_for_retrieval_at": evaluation_as_of if scoreability == "scorable" else None,
        "created_at": evaluation_as_of,
        # Runtime identity extensions. They are intentionally explicit until a later
        # public schema version freezes batch/subtarget persistence semantics.
        "prediction_batch_id": prediction_bundle["prediction_batch_id"],
        "checkpoint_prediction_id": checkpoint_prediction_id,
        "subtarget": prediction_bundle.get("subtarget"),
        "target_series_id": target_series_id,
        "prediction_revision_id": prediction_bundle["prediction_revision_id"],
        "data_snapshot_id": prediction_bundle["data_snapshot_id"],
        "calendar_id": prediction_bundle["calendar_id"],
        "calendar_version": prediction_bundle["calendar_version"],
        "identity_version": "experience-identity.v1",
        "metric_version": "posterior-metrics.v1",
        "effective_observation_dates": [item.isoformat() for item in window_dates],
        "source_observation_ids": [str(item["observation_id"]) for item in target_window],
        "source_revision_ids": [str(item["revision_id"]) for item in target_window],
        "benchmark_observation_ids": [str(item["observation_id"]) for item in benchmark_window],
        "benchmark_revision_ids": [str(item["revision_id"]) for item in benchmark_window],
        "target_anchor_observation_id": str(target_anchor["observation_id"]) if target_anchor else None,
        "target_anchor_revision_id": str(target_anchor["revision_id"]) if target_anchor else None,
        "benchmark_anchor_observation_id": str(benchmark_anchor["observation_id"]) if benchmark_anchor else None,
        "benchmark_anchor_revision_id": str(benchmark_anchor["revision_id"]) if benchmark_anchor else None,
        "diagnostic_only": scoreability != "scorable",
        "calculation_fingerprint": calculation_fingerprint,
    }
    return {
        "status": "built",
        "card": card,
        "blockers": blockers,
        "completed_effective_days": len(elapsed_dates),
        "next_checkpoint": _next_checkpoint(horizon),
        "next_expected_date": _next_expected_date(expected_dates, horizon),
    }


def experience_card_id_for_bundle(prediction_bundle: Mapping[str, Any]) -> str:
    """Return the stable card identity used by the immutable revision chain."""

    if not isinstance(prediction_bundle, Mapping):
        raise ExperienceCardInputError("prediction_bundle_required")
    _validate_prediction_bundle(prediction_bundle)
    return f"ec-{_digest(_identity(prediction_bundle))[:20]}"


def _validate_prediction_bundle(bundle: Mapping[str, Any]) -> None:
    required = {
        "prediction_batch_id",
        "prediction_ids_by_horizon",
        "prediction_revision_id",
        "data_snapshot_id",
        "node_id",
        "subtarget",
        "target_series_id",
        "benchmark_series_id",
        "as_of_time",
        "directions_by_horizon",
        "horizons",
        "calendar_id",
        "calendar_version",
    }
    missing = required - set(bundle)
    if missing:
        raise ExperienceCardInputError(f"prediction bundle missing fields: {sorted(missing)}")
    try:
        horizons = tuple(sorted(_normalize_horizon_token(item) for item in bundle["horizons"]))
    except TypeError as exc:
        raise ExperienceCardInputError("prediction horizons must be a sequence") from exc
    if horizons != FORMAL_HORIZONS:
        if 14 in horizons:
            raise ExperienceCardInputError("D+14 is historical read-only and cannot create a new Experience Card")
        raise ExperienceCardInputError("Experience Card horizons must be exactly D+1, D+7 and D+30")
    expected_horizons = set(FORMAL_HORIZONS)
    prediction_ids = _horizon_mapping(bundle["prediction_ids_by_horizon"])
    directions = _horizon_mapping(bundle["directions_by_horizon"])
    if set(prediction_ids) != expected_horizons:
        raise ExperienceCardInputError("prediction_ids_by_horizon keys must be exactly 1, 7 and 30")
    if set(directions) != expected_horizons:
        raise ExperienceCardInputError("directions_by_horizon keys must be exactly 1, 7 and 30")
    node_id = str(bundle["node_id"])
    subtarget = bundle.get("subtarget")
    if node_id == TERMINAL_NODE and subtarget not in {"poy", "dty"}:
        raise ExperienceCardInputError("terminal node requires subtarget poy or dty")
    if node_id != TERMINAL_NODE and subtarget is not None:
        raise ExperienceCardInputError("non-terminal nodes cannot define a POY/DTY subtarget")
    for field in (
        "prediction_batch_id",
        "prediction_revision_id",
        "data_snapshot_id",
        "target_series_id",
        "calendar_id",
        "calendar_version",
    ):
        if not str(bundle[field]).strip():
            raise ExperienceCardInputError(f"{field} must not be empty")
    for horizon in FORMAL_HORIZONS:
        _checkpoint_prediction_id(bundle, horizon)
        _direction_for_horizon(bundle, horizon)


def _parse_expected_dates(values: Sequence[str], prediction_date: date) -> list[date]:
    try:
        parsed = [date.fromisoformat(str(item)) for item in values]
    except ValueError as exc:
        raise ExperienceCardInputError("expected observation dates must be ISO dates") from exc
    if parsed != sorted(set(parsed)):
        raise ExperienceCardInputError("expected observation dates must be unique and increasing")
    if len(parsed) < 30:
        raise ExperienceCardInputError("at least 30 expected observation dates are required")
    if any(item <= prediction_date for item in parsed):
        raise ExperienceCardInputError("expected observation dates must follow prediction date")
    return parsed


def _parse_zoned_datetime(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExperienceCardInputError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ExperienceCardInputError(f"{field} must include a timezone")
    return parsed


def _completed_checkpoint(elapsed_days: int) -> int | None:
    return next((item for item in reversed(FORMAL_HORIZONS) if elapsed_days >= item), None)


def _normalize_checkpoint_horizon(value: int | str | None) -> int | None:
    if value is None:
        return None
    horizon = _normalize_horizon_token(value)
    if horizon == 14:
        raise ExperienceCardInputError("D+14 is historical read-only and cannot create a new Experience Card")
    if horizon not in FORMAL_HORIZONS:
        raise ExperienceCardInputError("Experience Card checkpoint must be exactly D+1, D+7 or D+30")
    return horizon


def _next_checkpoint(horizon: int) -> int | None:
    return next((item for item in FORMAL_HORIZONS if item > horizon), None)


def _next_expected_date(expected_dates: Sequence[date], horizon: int) -> str | None:
    next_checkpoint = _next_checkpoint(horizon)
    if next_checkpoint is None or len(expected_dates) < next_checkpoint:
        return None
    return expected_dates[next_checkpoint - 1].isoformat()


def _identity(bundle: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "prediction_batch_id": bundle["prediction_batch_id"],
        "node_id": bundle["node_id"],
        "subtarget": bundle.get("subtarget"),
        "target_series_id": bundle["target_series_id"],
        "identity_version": "experience-identity.v1",
    }


def _transition_error(previous: Mapping[str, Any] | None, card_id: str, next_stage: str) -> str | None:
    next_order = MATURITY_ORDER[next_stage]
    if previous is None:
        return None if next_order == 1 else "previous_maturity_revision_required"
    if previous.get("experience_card_id") != card_id:
        return "previous_revision_identity_mismatch"
    previous_stage = str(previous.get("maturity_stage", ""))
    if previous_stage not in MATURITY_ORDER:
        return "previous_revision_maturity_invalid"
    previous_order = MATURITY_ORDER[previous_stage]
    if next_order < previous_order:
        return "maturity_regression_forbidden"
    if next_order > previous_order + 1:
        return "maturity_checkpoint_skip_forbidden"
    return None


def _resolve_points(
    points: Sequence[Mapping[str, Any]],
    series_id: str,
    visibility_cutoff: datetime,
    *,
    effective_date_predicate: Callable[[date], bool],
    report_future_visibility: bool = True,
) -> tuple[dict[date, Mapping[str, Any]], list[str]]:
    grouped: dict[date, list[Mapping[str, Any]]] = {}
    blockers: list[str] = []
    for point in points:
        if str(point.get("series_id", "")) != series_id:
            continue
        observation_id = str(point.get("observation_id", "missing"))
        if point.get("quality_status") != "eligible":
            blockers.append(f"quality_ineligible:{observation_id}")
            continue
        try:
            visible_at = _parse_zoned_datetime(str(point.get("first_visible_at", "")), "first_visible_at")
            effective_date = date.fromisoformat(str(point.get("effective_date", "")))
        except ExperienceCardInputError:
            blockers.append(f"visibility_invalid:{observation_id}")
            continue
        except ValueError:
            blockers.append(f"effective_date_invalid:{observation_id}")
            continue
        if not effective_date_predicate(effective_date):
            continue
        if visible_at > visibility_cutoff:
            if report_future_visibility:
                blockers.append(f"future_visible_excluded:{observation_id}")
            continue
        if not point.get("revision_id"):
            blockers.append(f"revision_missing:{observation_id}")
            continue
        grouped.setdefault(effective_date, []).append(point)

    resolved: dict[date, Mapping[str, Any]] = {}
    for effective_date, revisions in grouped.items():
        if len(revisions) == 1:
            revision = revisions[0]
            if revision.get("supersedes_revision_id"):
                blockers.append(f"revision_chain_incomplete:{effective_date.isoformat()}")
            resolved[effective_date] = revision
            continue
        ids = [str(item["revision_id"]) for item in revisions]
        if len(ids) != len(set(ids)):
            blockers.append(f"revision_id_duplicate:{effective_date.isoformat()}")
            continue
        superseded = {str(item["supersedes_revision_id"]) for item in revisions if item.get("supersedes_revision_id")}
        if not superseded <= set(ids):
            blockers.append(f"revision_chain_incomplete:{effective_date.isoformat()}")
            continue
        heads = [item for item in revisions if str(item["revision_id"]) not in superseded]
        if len(heads) != 1:
            blockers.append(f"revision_chain_ambiguous:{effective_date.isoformat()}")
            continue
        resolved[effective_date] = heads[0]
    return resolved, blockers


def _point_fingerprint(point: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        "observation_id",
        "series_id",
        "effective_date",
        "value",
        "unit",
        "quote_type",
        "first_visible_at",
        "quality_status",
        "visibility_mode",
        "revision_id",
        "supersedes_revision_id",
    )
    return {field: point.get(field) for field in fields}


def _anchor_point(points: Mapping[date, Mapping[str, Any]], prediction_date: date) -> Mapping[str, Any] | None:
    candidates = [item for effective_date, item in points.items() if effective_date <= prediction_date]
    return max(candidates, key=_point_date) if candidates else None


def _anchor_blockers(label: str, anchor: Mapping[str, Any] | None, prediction_time: datetime) -> list[str]:
    if anchor is None:
        return [f"{label}_anchor_missing"]
    visible_at = _parse_zoned_datetime(str(anchor["first_visible_at"]), "first_visible_at")
    blockers = []
    if visible_at > prediction_time:
        blockers.append(f"{label}_anchor_not_visible_at_prediction")
    if anchor.get("visibility_mode") != "strict_as_of":
        blockers.append(f"{label}_anchor_not_strict_as_of")
    return blockers


def _basis_blockers(label: str, anchor: Mapping[str, Any] | None, points: Sequence[Mapping[str, Any]]) -> list[str]:
    if anchor is None:
        return []
    expected = (anchor.get("unit"), anchor.get("quote_type"))
    if not all(expected):
        return [f"{label}_quote_basis_missing"]
    if any((item.get("unit"), item.get("quote_type")) != expected for item in points):
        return [f"{label}_quote_basis_mismatch"]
    return []


def _price_blockers(label: str, anchor: Mapping[str, Any] | None, points: Sequence[Mapping[str, Any]]) -> list[str]:
    if anchor is None:
        return []
    if _positive_price(anchor) is None:
        return [f"{label}_anchor_price_invalid"]
    if any(_positive_price(item) is None for item in points):
        return [f"{label}_posterior_price_invalid"]
    return []


def _series_status_blockers(
    target_series_id: str,
    benchmark_series_id: str,
    resolver: SeriesEligibilityResolver,
) -> list[str]:
    blockers = []
    for label, series_id in (("target", target_series_id), ("benchmark", benchmark_series_id)):
        status = str(resolver(series_id))
        if status not in SCORABLE_SERIES_STATUSES:
            blockers.append(f"{label}_series_status:{status or 'unknown'}")
    return blockers


def _metrics(
    *,
    direction: str,
    target_anchor: Mapping[str, Any] | None,
    target_by_date: Mapping[date, Mapping[str, Any]],
    benchmark_anchor: Mapping[str, Any] | None,
    benchmark_by_date: Mapping[date, Mapping[str, Any]],
    window_dates: Sequence[date],
) -> dict[str, float | int | str | None]:
    empty = {
        "start_price": None,
        "end_price": None,
        "raw_change_abs": None,
        "raw_change_pct": None,
        "relative_change": None,
        "mfe": None,
        "mae": None,
        "days_to_peak": None,
        "first_reversal_at": None,
    }
    if target_anchor is None or window_dates[-1] not in target_by_date:
        return empty
    start_price = _positive_price(target_anchor)
    if start_price is None:
        return empty
    end_price = _positive_price(target_by_date[window_dates[-1]])
    if end_price is None:
        return empty
    raw_change_abs = end_price - start_price
    raw_change_pct = raw_change_abs / abs(start_price) * 100
    path = []
    for index, effective_date in enumerate(window_dates, start=1):
        point = target_by_date.get(effective_date)
        price = _positive_price(point) if point else None
        if price is not None:
            path.append((index, effective_date, (price - start_price) / abs(start_price) * 100))

    relative_change = None
    if benchmark_anchor is not None and window_dates[-1] in benchmark_by_date:
        benchmark_start = _positive_price(benchmark_anchor)
        benchmark_end = _positive_price(benchmark_by_date[window_dates[-1]])
        if benchmark_start is not None and benchmark_end is not None:
            benchmark_change = (benchmark_end - benchmark_start) / abs(benchmark_start) * 100
            relative_change = raw_change_pct - benchmark_change

    result = {
        **empty,
        "start_price": _rounded(start_price),
        "end_price": _rounded(end_price),
        "raw_change_abs": _rounded(raw_change_abs),
        "raw_change_pct": _rounded(raw_change_pct),
        "relative_change": _rounded(relative_change),
    }
    multiplier = DIRECTION_MULTIPLIER.get(direction)
    if multiplier is None or not path:
        return result
    signed_path = [(index, effective_date, multiplier * change) for index, effective_date, change in path]
    best = max(change for _, _, change in signed_path)
    worst = min(change for _, _, change in signed_path)
    mfe = max(0.0, best)
    mae = max(0.0, -worst)
    days_to_peak = next(index for index, _, change in signed_path if change == best) if mfe > 0 else None
    favorable_seen = False
    first_reversal_at = None
    for _, effective_date, change in signed_path:
        if change > 0:
            favorable_seen = True
        elif favorable_seen and change <= 0:
            first_reversal_at = effective_date.isoformat()
            break
    result.update(
        mfe=_rounded(mfe),
        mae=_rounded(mae),
        days_to_peak=days_to_peak,
        first_reversal_at=first_reversal_at,
    )
    return result


def _mechanism_support(direction: str, raw_change_pct: float | None, scoreability: str) -> str:
    if scoreability != "scorable" or raw_change_pct is None or direction not in DIRECTION_MULTIPLIER:
        return "inconclusive"
    signed_change = DIRECTION_MULTIPLIER[direction] * raw_change_pct
    if signed_change > 0:
        return "supported"
    if signed_change < 0:
        return "unsupported"
    return "inconclusive"


def _positive_price(point: Mapping[str, Any] | None) -> float | None:
    if point is None:
        return None
    try:
        value = float(point["value"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _point_date(point: Mapping[str, Any]) -> date:
    return date.fromisoformat(str(point["effective_date"]))


def _checkpoint_prediction_id(bundle: Mapping[str, Any], horizon: int) -> str:
    values = _horizon_mapping(bundle["prediction_ids_by_horizon"])
    value = str(values.get(horizon, "")).strip()
    if not value:
        raise ExperienceCardInputError(f"prediction id missing for D+{horizon}")
    return value


def _direction_for_horizon(bundle: Mapping[str, Any], horizon: int) -> str:
    values = _horizon_mapping(bundle["directions_by_horizon"])
    value = str(values.get(horizon, "")).strip()
    if value not in {"up", "down", "neutral", "uncertain"}:
        raise ExperienceCardInputError(f"invalid direction for D+{horizon}")
    return value


def _horizon_mapping(value: Any) -> dict[int, Any]:
    if not isinstance(value, Mapping):
        raise ExperienceCardInputError("horizon mapping must be an object")
    normalized = {_normalize_horizon_token(key): item for key, item in value.items()}
    if len(normalized) != len(value):
        raise ExperienceCardInputError("horizon mapping contains duplicate normalized keys")
    return normalized


def _normalize_horizon_token(value: Any) -> int:
    if type(value) is int and value in FORMAL_HORIZONS:
        return value
    if isinstance(value, str) and value in {"1", "7", "30"}:
        return int(value)
    if (type(value) is int and value == 14) or value == "14":
        raise ExperienceCardInputError("D+14 is historical read-only and cannot create a new Experience Card")
    raise ExperienceCardInputError("horizon token must be exactly 1, 7 or 30")


def _rounded(value: float | None) -> float | None:
    return round(value, 8) if value is not None else None


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

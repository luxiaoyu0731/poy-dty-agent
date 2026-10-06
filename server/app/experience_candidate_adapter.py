"""Read-only assembly of terminal Experience settlement candidates.

The adapter is intentionally separate from scheduling and persistence.  It
turns one verified Phase A prediction payload and one explicit evaluation
snapshot into POY/DTY candidate dictionaries for ``experience_settlement``.
Missing or unapproved evidence remains an explicit blocked/unavailable status.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .derived_cost_pressure import CostPressureInputError, derive_cost_pressure_observations
from .phase_a_contracts import EXPECTED_CONTRACT_VERSION, assert_payload_valid

TERMINAL_NODE = "poy_dty_upstream_cost_pressure"
TARGET_SERIES_BY_SUBTARGET = {
    "poy": "poy.upstream_cost_pressure.index",
    "dty": "dty.upstream_cost_pressure.index",
}
BENCHMARK_RULE_BY_SUBTARGET = {
    "poy": ("POY", "POY 150D/48F", "poy.ccf.domestic.150d_48f.assessment.cny_mt"),
    "dty": ("DTY", "DTY 150D/48F低弹", "dty.ccf.domestic.150d_48f_low_elastic.assessment.cny_mt"),
}
COMPONENT_SERIES_BY_PRODUCT_SPEC = {
    ("PTA", "内盘PTA"): "pta.ccf.domestic.daily_assessment.cny_mt",
    ("MEG", "内盘MEG现货"): "meg.ccf.domestic.daily_assessment.cny_mt",
}


class ExperienceCandidateAdapterError(ValueError):
    """Raised when a persisted prediction or snapshot cannot be adapted safely."""


def assemble_terminal_experience_candidates(
    *,
    prediction_payload: Mapping[str, Any],
    evaluation_snapshot: Mapping[str, Any],
    expected_observation_dates: Sequence[str],
    series_statuses: Mapping[str, str],
    previous_revisions: Mapping[str, Mapping[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Build explicit POY/DTY candidates without assigning or promoting status.

    ``series_statuses`` is deliberately caller-supplied and must contain a
    status for both derived targets and both benchmark series.  This avoids an
    adapter treating a row's existence as formal eligibility.
    """

    try:
        assert_payload_valid("prediction", prediction_payload)
    except ValueError as exc:
        raise ExperienceCandidateAdapterError("experience_prediction_contract_invalid") from exc
    if prediction_payload.get("schema_version") != "phase-a.prediction.v1":
        raise ExperienceCandidateAdapterError("experience_prediction_contract_invalid")
    if not _is_sequence(expected_observation_dates) or not all(
        type(item) is str for item in expected_observation_dates
    ):
        raise ExperienceCandidateAdapterError("experience_calendar_invalid")
    authorized_rows = _snapshot_rows(evaluation_snapshot)
    component_points, benchmark_points = _normalize_snapshot_rows(authorized_rows, series_statuses)
    try:
        derived = derive_cost_pressure_observations(component_points)
    except CostPressureInputError as exc:
        raise ExperienceCandidateAdapterError(str(exc)) from None
    derived_by_target: dict[str, list[dict[str, Any]]] = {target: [] for target in TARGET_SERIES_BY_SUBTARGET.values()}
    for point in derived["observations"]:
        derived_by_target[str(point["series_id"])].append(point)

    terminal_cells = {
        int(cell["horizon_days"]): cell
        for cell in prediction_payload["cells"]
        if cell["node_id"] == TERMINAL_NODE
    }
    if set(terminal_cells) != {1, 7, 30}:
        raise ExperienceCandidateAdapterError("experience_terminal_prediction_missing")
    result: list[dict[str, Any]] = []
    previous = previous_revisions or {}
    for subtarget, target_series_id in TARGET_SERIES_BY_SUBTARGET.items():
        benchmark_series_id = BENCHMARK_RULE_BY_SUBTARGET[subtarget][2]
        statuses = {
            target_series_id: _required_status(series_statuses, target_series_id),
            benchmark_series_id: _required_status(series_statuses, benchmark_series_id),
        }
        subtarget_predictions = {
            horizon: _subtarget_result(terminal_cells[horizon], subtarget) for horizon in (1, 7, 30)
        }
        prediction_status = "available" if all(
            item["scoreability"] == "scorable" for item in subtarget_predictions.values()
        ) else "blocked"
        target_points = derived_by_target[target_series_id]
        target_observation_status = "available" if derived["status"] == "ready" else "unavailable"
        subtarget_benchmarks = benchmark_points[subtarget]
        benchmark_observation_status = "available" if subtarget_benchmarks else "unavailable"
        result.append(
            {
                "prediction_bundle": {
                    "prediction_batch_id": prediction_payload["prediction_batch_id"],
                    "prediction_ids_by_horizon": {
                        horizon: _subtarget_output_id(prediction_payload["revision_id"], horizon, subtarget)
                        for horizon in (1, 7, 30)
                    },
                    "prediction_revision_id": prediction_payload["revision_id"],
                    "data_snapshot_id": prediction_payload["data_snapshot_id"],
                    "node_id": TERMINAL_NODE,
                    "subtarget": subtarget,
                    "target_series_id": target_series_id,
                    "benchmark_series_id": benchmark_series_id,
                    "as_of_time": prediction_payload["as_of_time"],
                    "directions_by_horizon": {
                        horizon: subtarget_predictions[horizon]["direction"] for horizon in (1, 7, 30)
                    },
                    "horizons": [1, 7, 30],
                    "calendar_id": "pta-meg-common-effective-days.v1",
                    "calendar_version": "pta-meg-common-effective-days.v1",
                    "overlapping_event_ids": [],
                },
                "target_points": target_points,
                "benchmark_points": subtarget_benchmarks,
                "expected_observation_dates": list(expected_observation_dates),
                "prediction_status": prediction_status,
                "target_observation_status": target_observation_status,
                "benchmark_observation_status": benchmark_observation_status,
                "target_series_status": statuses[target_series_id],
                "benchmark_series_status": statuses[benchmark_series_id],
                "previous_revision": previous.get(subtarget),
            }
        )
    return {
        "schema_version": "experience-candidate-adapter.v1",
        "contract_version": EXPECTED_CONTRACT_VERSION,
        "derivation_status": derived["status"],
        "derivation_reason_codes": derived["reason_codes"],
        "candidates": result,
    }


def _snapshot_rows(snapshot: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    payload = snapshot.get("payload")
    if not isinstance(payload, Mapping):
        raise ExperienceCandidateAdapterError("experience_snapshot_payload_invalid")
    rows = payload.get("authorized_price_observations")
    if not _is_sequence(rows) or not all(isinstance(item, Mapping) for item in rows):
        raise ExperienceCandidateAdapterError("experience_snapshot_price_rows_invalid")
    return rows


def _normalize_snapshot_rows(
    rows: Sequence[Mapping[str, Any]], series_statuses: Mapping[str, str]
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    components: list[dict[str, Any]] = []
    benchmarks = {"poy": [], "dty": []}
    for row in rows:
        product = row.get("product")
        spec = row.get("spec")
        capture_revision_id = row.get("capture_revision_id")
        if type(capture_revision_id) is not str or not capture_revision_id:
            continue
        base = _base_point(row)
        component_series_id = COMPONENT_SERIES_BY_PRODUCT_SPEC.get((product, spec))
        if component_series_id is not None:
            components.append(
                {
                    **base,
                    "series_id": component_series_id,
                    "quality_status": "eligible"
                    if _required_status(series_statuses, component_series_id) == "eligible"
                    else "blocked",
                }
            )
        for subtarget, (expected_product, expected_spec, benchmark_series_id) in BENCHMARK_RULE_BY_SUBTARGET.items():
            if (product, spec) != (expected_product, expected_spec):
                continue
            benchmarks[subtarget].append(
                {
                    **base,
                    "series_id": benchmark_series_id,
                    "quality_status": "eligible"
                    if _required_status(series_statuses, benchmark_series_id) == "eligible"
                    else "blocked",
                }
            )
    return components, benchmarks


def _base_point(row: Mapping[str, Any]) -> dict[str, Any]:
    required = ("point_id", "observed_at", "price", "unit", "quote_type", "capture_revision_id")
    if any(type(row.get(field)) is not str or not row[field] for field in required if field != "price"):
        raise ExperienceCandidateAdapterError("experience_snapshot_price_row_invalid")
    if isinstance(row.get("price"), bool) or not isinstance(row.get("price"), (int, float)):
        raise ExperienceCandidateAdapterError("experience_snapshot_price_row_invalid")
    raw = row.get("raw")
    if not isinstance(raw, Mapping) or type(raw.get("visible_at")) is not str:
        raise ExperienceCandidateAdapterError("experience_snapshot_price_row_invalid")
    return {
        "observation_id": row["point_id"],
        "effective_date": row["observed_at"],
        "value": row["price"],
        "unit": row["unit"],
        "quote_type": row["quote_type"],
        "first_visible_at": raw["visible_at"],
        "visibility_mode": "strict_as_of",
        "revision_id": row["capture_revision_id"],
        "supersedes_revision_id": None,
        "capture_revision_id": row["capture_revision_id"],
    }


def _subtarget_result(cell: Mapping[str, Any], subtarget: str) -> Mapping[str, Any]:
    for item in cell["subtarget_results"]:
        if item["target"] == subtarget:
            return item
    raise ExperienceCandidateAdapterError("experience_terminal_subtarget_missing")


def _subtarget_output_id(revision_id: str, horizon: int, subtarget: str) -> str:
    return "fpo-" + hashlib.sha256(
        f"{revision_id}:{TERMINAL_NODE}:{horizon}:{subtarget}".encode()
    ).hexdigest()


def _required_status(statuses: Mapping[str, str], series_id: str) -> str:
    value = statuses.get(series_id)
    if value not in {"eligible", "blocked", "unavailable"}:
        raise ExperienceCandidateAdapterError("experience_series_status_missing")
    return value


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))

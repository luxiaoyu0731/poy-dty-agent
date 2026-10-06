from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from .seven_product_contract import (
    CURRENT_FORMAL_CELL_COUNT,
    CURRENT_FORMAL_HORIZONS,
    CURRENT_FORMAL_TARGETS,
    CURRENT_LABEL_REGISTRY_VERSION,
    LABEL_REGISTRY,
)
from .seven_product_evaluation import (
    MINIMUM_DIRECTION_ACCURACY,
    MINIMUM_EFFECTIVE_SAMPLES,
    MINIMUM_ERROR_IMPROVEMENT,
    _evaluate_cell,
)
from .seven_product_forecast import LoadedLabelSeries, PricePoint

SIMULATION_SCHEMA_VERSION = "seven-product-isolated-simulation.v1"
SIMULATION_POLICY_VERSION = "seven-product-historical-replay.v1"
BOUNDARY_POINTS = {1: 41, 7: 49, 30: 93}
ROLLING_WINDOW_POINTS = 120
ROLLING_STRIDE_POINTS = 20
FIXED_SEEDS = tuple(range(10_001, 10_031))
PSEUDO_VISIBILITY_LAG_DAYS = {
    "crude": 1,
    "naphtha": 2,
    "px": 1,
    "pta": 1,
    "meg": 1,
    "poy": 1,
    "dty": 1,
}


def run_isolated_historical_simulation(
    *,
    exact_series: dict[str, LoadedLabelSeries],
    proxy_series: dict[str, LoadedLabelSeries] | None,
    as_of_time: str,
    seeds: tuple[int, ...] = FIXED_SEEDS,
    bootstrap_replicates: int = 100,
    pilot: bool = False,
) -> dict[str, Any]:
    """Run deterministic research replay without producing formal OOS evidence."""

    as_of = _timestamp(as_of_time)
    if tuple(exact_series) != CURRENT_FORMAL_TARGETS:
        raise ValueError("simulation_exact_series_contract_incomplete")
    if len(seeds) != len(set(seeds)) or not seeds:
        raise ValueError("simulation_seeds_invalid")
    normalized = {
        target: _pseudo_visible_exact_series(target, exact_series[target], as_of) for target in CURRENT_FORMAL_TARGETS
    }
    boundary: list[dict[str, Any]] = []
    rolling: list[dict[str, Any]] = []
    full: list[dict[str, Any]] = []
    for target in CURRENT_FORMAL_TARGETS:
        loaded = normalized[target]
        for horizon in CURRENT_FORMAL_HORIZONS:
            required = BOUNDARY_POINTS[horizon]
            boundary.append(
                _evaluate_window(
                    target=target,
                    horizon=horizon,
                    loaded=_slice_loaded(loaded, -required),
                    as_of=as_of,
                    window_kind="boundary",
                    window_id=f"boundary:{target}:d{horizon}:{required}",
                    required_points=required,
                    bootstrap_replicates=bootstrap_replicates,
                )
            )
            full.append(
                _evaluate_window(
                    target=target,
                    horizon=horizon,
                    loaded=loaded,
                    as_of=as_of,
                    window_kind="full_history",
                    window_id=f"full:{target}:d{horizon}",
                    required_points=required,
                    bootstrap_replicates=bootstrap_replicates,
                )
            )
            for end in _rolling_endpoints(len(loaded.points)):
                start = end - ROLLING_WINDOW_POINTS
                rolling.append(
                    _evaluate_window(
                        target=target,
                        horizon=horizon,
                        loaded=LoadedLabelSeries(points=loaded.points[start:end], source_matches_label=True),
                        as_of=as_of,
                        window_kind="rolling_120",
                        window_id=f"rolling:{target}:d{horizon}:{start}-{end}",
                        required_points=ROLLING_WINDOW_POINTS,
                        bootstrap_replicates=bootstrap_replicates,
                    )
                )

    seed_ledger: list[dict[str, Any]] = []
    selected_seeds = seeds[:3] if pilot else seeds
    for seed in selected_seeds:
        for target in CURRENT_FORMAL_TARGETS:
            perturbed = _perturb_visibility_only(normalized[target], seed=seed, target=target)
            for horizon in CURRENT_FORMAL_HORIZONS:
                seed_ledger.append(
                    _evaluate_window(
                        target=target,
                        horizon=horizon,
                        loaded=perturbed,
                        as_of=as_of,
                        window_kind="seed_stress",
                        window_id=f"seed:{seed}:{target}:d{horizon}",
                        required_points=BOUNDARY_POINTS[horizon],
                        bootstrap_replicates=bootstrap_replicates,
                        seed=seed,
                    )
                )

    proxy_controls: list[dict[str, Any]] = []
    normalized_proxy_series: dict[str, LoadedLabelSeries] = {}
    proxy_lineage: dict[str, dict[str, Any]] = {}
    for target, loaded in sorted((proxy_series or {}).items()):
        if target not in CURRENT_FORMAL_TARGETS:
            raise ValueError("simulation_proxy_target_unknown")
        control = _pseudo_visible_proxy_series(target, loaded, as_of)
        normalized_proxy_series[target] = control
        proxy_lineage[target] = _feature_lineage(loaded, historical_visibility="batch_backfilled_proxy")
        for horizon in CURRENT_FORMAL_HORIZONS:
            record = _evaluate_window(
                target=target,
                horizon=horizon,
                loaded=control,
                as_of=as_of,
                window_kind="source_mismatched_control",
                window_id=f"proxy:{target}:d{horizon}",
                required_points=BOUNDARY_POINTS[horizon],
                bootstrap_replicates=bootstrap_replicates,
            )
            record["effect_eligible"] = False
            record["status"] = "source_mismatched"
            proxy_controls.append(record)

    from .seven_product_experiment import run_nested_research_experiment

    research_optimization = run_nested_research_experiment(
        exact_series=normalized,
        as_of_time=as_of.isoformat(),
        bootstrap_replicates=bootstrap_replicates,
    )
    from .seven_product_multivariate_experiment import run_upstream_ridge_research

    upstream_features = {
        "crude": normalized["crude"],
        "naphtha": normalized_proxy_series.get("naphtha", normalized["naphtha"]),
        "px": normalized_proxy_series.get("px", normalized["px"]),
        "pta": normalized_proxy_series.get("pta", normalized["pta"]),
        "meg": normalized_proxy_series.get("meg", normalized["meg"]),
    }
    upstream_lineage = {
        feature_name: proxy_lineage.get(
            feature_name,
            _feature_lineage(upstream_features[feature_name], historical_visibility="exact_or_empty"),
        )
        for feature_name in upstream_features
    }
    upstream_ridge_control = run_upstream_ridge_research(
        target_series={"poy": normalized["poy"], "dty": normalized["dty"]},
        feature_series=upstream_features,
        feature_lineage=upstream_lineage,
        as_of_time=as_of.isoformat(),
        bootstrap_replicates=bootstrap_replicates,
    )
    from .seven_product_chain_experiment import run_chain_pass_through_research

    chain_driver_series = {
        "pta": upstream_features["pta"],
        "meg": upstream_features["meg"],
        "poy": normalized["poy"],
    }
    chain_driver_lineage = {
        "pta": upstream_lineage["pta"],
        "meg": upstream_lineage["meg"],
        "poy": _feature_lineage(normalized["poy"], historical_visibility="pseudo_visible_exact_label"),
    }
    chain_pass_through_control = run_chain_pass_through_research(
        target_series={"poy": normalized["poy"], "dty": normalized["dty"]},
        driver_series=chain_driver_series,
        driver_lineage=chain_driver_lineage,
        as_of_time=as_of.isoformat(),
        bootstrap_replicates=bootstrap_replicates,
    )

    full_by_cell = {(row["target"], row["horizon_days"]): row for row in full}
    if len(full_by_cell) != CURRENT_FORMAL_CELL_COUNT:
        raise RuntimeError("simulation_21_cell_contract_missing")
    testable = [row for row in full if row["effect_eligible"]]
    passed = [row for row in testable if row["performance_gate_passed"]]
    not_testable = [row for row in full if not row["effect_eligible"]]
    body = {
        "schema_version": SIMULATION_SCHEMA_VERSION,
        "policy_version": SIMULATION_POLICY_VERSION,
        "mode": "SIMULATION_ONLY",
        "as_of_time": as_of.isoformat(),
        "configuration": {
            "boundary_points": BOUNDARY_POINTS,
            "rolling_window_points": ROLLING_WINDOW_POINTS,
            "rolling_stride_points": ROLLING_STRIDE_POINTS,
            "seeds": list(selected_seeds),
            "bootstrap_replicates": bootstrap_replicates,
            "historical_values_only": True,
            "pseudo_visibility_lag_days": PSEUDO_VISIBILITY_LAG_DAYS,
            "promotion_allowed": False,
            "production_champion_candidate_allowed": True,
            "formal_promotion_allowed": False,
        },
        "denominators": {
            "contract_cells": CURRENT_FORMAL_CELL_COUNT,
            "testable_cells": len(testable),
            "passed_cells": len(passed),
            "not_testable_cells": len(not_testable),
            "proxy_control_cells": len(proxy_controls),
        },
        "mechanism_gates": {
            "contract_complete": len(full_by_cell) == CURRENT_FORMAL_CELL_COUNT,
            "source_mismatch_excluded_from_effect": all(not row["effect_eligible"] for row in proxy_controls),
            "historical_values_only": True,
            "formal_score_unchanged": True,
            "formal_oos_unchanged": True,
        },
        "full_history": full,
        "boundary_windows": boundary,
        "rolling_windows": rolling,
        "seed_ledger": seed_ledger,
        "proxy_controls": proxy_controls,
        "research_optimization": research_optimization,
        "upstream_ridge_control": upstream_ridge_control,
        "chain_pass_through_control": chain_pass_through_control,
    }
    body["report_body_sha256"] = _digest(body)
    return body


def _evaluate_window(
    *,
    target: str,
    horizon: int,
    loaded: LoadedLabelSeries,
    as_of: datetime,
    window_kind: str,
    window_id: str,
    required_points: int,
    bootstrap_replicates: int,
    seed: int | None = None,
) -> dict[str, Any]:
    points = loaded.points
    base = {
        "window_id": window_id,
        "window_kind": window_kind,
        "target": target,
        "horizon_days": horizon,
        "seed": seed,
        "point_count": len(points),
        "required_points": required_points,
        "window_start": points[0].observed_at if points else None,
        "window_end": points[-1].observed_at if points else None,
        "pseudo_visibility": True,
        "source_matches_label": loaded.source_matches_label,
    }
    if len(points) < required_points:
        return {
            **base,
            "status": "not_testable",
            "effect_eligible": False,
            "performance_gate_passed": False,
            "effective_sample_count": 0,
            "gate_reasons": [f"insufficient_points:{len(points)}<{required_points}"],
            "metrics": {},
        }
    evaluation = _evaluate_cell(
        target=target,
        horizon_days=horizon,
        as_of=as_of,
        loaded=loaded,
        bootstrap_replicates=bootstrap_replicates,
    )
    effect_eligible = bool(
        loaded.source_matches_label
        and evaluation.leakage_status == "passed"
        and evaluation.effective_sample_count >= MINIMUM_EFFECTIVE_SAMPLES
    )
    performance_passed = bool(
        effect_eligible
        and evaluation.error_improvement is not None
        and evaluation.error_improvement >= MINIMUM_ERROR_IMPROVEMENT
        and evaluation.direction_accuracy is not None
        and evaluation.direction_accuracy >= MINIMUM_DIRECTION_ACCURACY
    )
    return {
        **base,
        "status": "passed" if performance_passed else "evaluated" if effect_eligible else "not_testable",
        "effect_eligible": effect_eligible,
        "performance_gate_passed": performance_passed,
        "effective_sample_count": evaluation.effective_sample_count,
        "gate_reasons": evaluation.gate_reasons,
        "metrics": {
            "candidate_mae": evaluation.candidate_mae,
            "persistence_mae": evaluation.persistence_mae,
            "seasonal_mae": evaluation.seasonal_mae,
            "best_baseline_mae": evaluation.best_baseline_mae,
            "error_improvement": evaluation.error_improvement,
            "direction_accuracy": evaluation.direction_accuracy,
            "worst_regime": evaluation.worst_regime,
        },
        "result_sha256": evaluation.result_sha256,
    }


def _pseudo_visible_exact_series(target: str, loaded: LoadedLabelSeries, as_of: datetime) -> LoadedLabelSeries:
    definition = LABEL_REGISTRY[target]
    valid = tuple(
        point
        for point in loaded.points
        if point.source_id == definition.source_id
        and point.unit == definition.unit
        and (not point.semantic_series_id or point.semantic_series_id == definition.series_id)
    )
    return LoadedLabelSeries(
        points=_apply_pseudo_visibility(target, valid, as_of, definition.series_id),
        source_matches_label=loaded.source_matches_label and len(valid) == len(loaded.points),
        data_gaps=loaded.data_gaps,
    )


def _pseudo_visible_proxy_series(target: str, loaded: LoadedLabelSeries, as_of: datetime) -> LoadedLabelSeries:
    return LoadedLabelSeries(
        points=_apply_pseudo_visibility(target, loaded.points, as_of, f"proxy.{target}"),
        source_matches_label=False,
        data_gaps=(*loaded.data_gaps, "source_mismatched_negative_control"),
    )


def _apply_pseudo_visibility(
    target: str,
    points: tuple[PricePoint, ...],
    as_of: datetime,
    default_series_id: str,
) -> tuple[PricePoint, ...]:
    normalized: list[PricePoint] = []
    for point in points:
        observed = _timestamp(point.observed_at)
        visible = observed + timedelta(days=PSEUDO_VISIBILITY_LAG_DAYS[target], hours=12)
        if visible > as_of:
            continue
        normalized.append(
            replace(
                point,
                visible_at=visible.isoformat(),
                semantic_series_id=point.semantic_series_id or default_series_id,
                contract_version=point.contract_version or CURRENT_LABEL_REGISTRY_VERSION,
            )
        )
    return tuple(sorted(normalized, key=lambda point: _timestamp(point.observed_at)))


def _perturb_visibility_only(loaded: LoadedLabelSeries, *, seed: int, target: str) -> LoadedLabelSeries:
    """Stress missing/delay/revision timing without generating or altering prices."""

    if not loaded.points:
        return loaded
    target_seed = int(_digest([seed, target])[:16], 16)
    rng = np.random.default_rng(target_seed)
    points: list[PricePoint] = []
    for point in loaded.points:
        draw = float(rng.random())
        if draw < 0.05:  # missing publication
            continue
        extra_days = 0
        if draw < 0.15:  # delayed publication
            extra_days += int(rng.integers(1, 4))
        if draw > 0.95:  # revision held back until a later visibility boundary
            extra_days += 1
        points.append(
            replace(
                point,
                visible_at=(_timestamp(point.visible_at) + timedelta(days=extra_days)).isoformat(),
            )
        )
    return LoadedLabelSeries(
        points=tuple(points),
        source_matches_label=loaded.source_matches_label,
        data_gaps=loaded.data_gaps,
    )


def _rolling_endpoints(count: int) -> tuple[int, ...]:
    if count < ROLLING_WINDOW_POINTS:
        return ()
    endpoints = list(range(ROLLING_WINDOW_POINTS, count + 1, ROLLING_STRIDE_POINTS))
    if endpoints[-1] != count:
        endpoints.append(count)
    return tuple(endpoints)


def _slice_loaded(loaded: LoadedLabelSeries, start: int) -> LoadedLabelSeries:
    points = loaded.points[start:] if len(loaded.points) >= abs(start) else loaded.points
    return LoadedLabelSeries(
        points=points,
        source_matches_label=loaded.source_matches_label,
        data_gaps=loaded.data_gaps,
    )


def _feature_lineage(loaded: LoadedLabelSeries, *, historical_visibility: str) -> dict[str, Any]:
    return {
        "point_count": len(loaded.points),
        "source_ids": sorted({point.source_id for point in loaded.points}),
        "semantic_series_ids": sorted({point.semantic_series_id for point in loaded.points}),
        "distinct_original_visible_dates": len({point.visible_at[:10] for point in loaded.points}),
        "historical_visibility": historical_visibility,
        "source_matches_formal_label": loaded.source_matches_label,
    }


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _digest(value: Any) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

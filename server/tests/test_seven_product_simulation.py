from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import numpy as np

from app.seven_product_chain_experiment import (
    MATERIAL_WEIGHTS,
    fit_predict_non_negative_pass_through,
    material_basket_return,
    select_chain_penalty_from_past,
)
from app.seven_product_contract import (
    CURRENT_FORMAL_TARGETS,
    CURRENT_LABEL_REGISTRY_VERSION,
    LABEL_REGISTRY,
    frozen_label_identity,
)
from app.seven_product_experiment import (
    ANOMALY_POLICY_VERSION,
    CANDIDATE_IDS,
    candidate_forecasts,
    mad_neutral_band,
    quarantine_online_anomalies,
    select_candidate_from_past,
)
from app.seven_product_forecast import LoadedLabelSeries, PricePoint
from app.seven_product_multivariate_experiment import (
    FEATURE_GROUP_ABLATION_ORDER,
    UPSTREAM_FEATURE_SERIES,
    build_upstream_feature_vector,
    feature_indices_for_series,
    feature_names,
    fit_predict_ridge_return,
)
from app.seven_product_simulation import run_isolated_historical_simulation


def _series(target: str, count: int, *, source_matches: bool = True) -> LoadedLabelSeries:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    definition = LABEL_REGISTRY[target]
    source = definition.source_id if source_matches else "source_mismatched_fixture"
    series = definition.series_id if source_matches else f"proxy.{target}.fixture"
    points = tuple(
        PricePoint(
            observation_id=f"{target}-{index}",
            observed_at=(start + timedelta(days=index)).date().isoformat(),
            visible_at=(start + timedelta(days=index + 5)).isoformat(),
            value=100 + index * 0.2 + (index % 7) * 0.1,
            unit=definition.unit,
            source_id=source,
            source_url=f"https://example.test/{target}/{index}",
            raw_sha256=f"{index % 10}" * 64,
            semantic_series_id=series,
            contract_version=CURRENT_LABEL_REGISTRY_VERSION,
        )
        for index in range(count)
    )
    return LoadedLabelSeries(points=points, source_matches_label=source_matches)


def test_simulation_preserves_21_cell_denominator_and_is_deterministic() -> None:
    exact = {target: _series(target, 140 if target == "crude" else 10) for target in CURRENT_FORMAL_TARGETS}
    proxy = {"px": _series("px", 140, source_matches=False)}

    first = run_isolated_historical_simulation(
        exact_series=exact,
        proxy_series=proxy,
        as_of_time="2026-09-01T23:59:59+08:00",
        pilot=True,
    )
    second = run_isolated_historical_simulation(
        exact_series=exact,
        proxy_series=proxy,
        as_of_time="2026-09-01T23:59:59+08:00",
        pilot=True,
    )

    assert len(first["full_history"]) == 21
    assert first["denominators"]["contract_cells"] == 21
    assert first["denominators"]["not_testable_cells"] > 0
    assert first["mechanism_gates"]["source_mismatch_excluded_from_effect"] is True
    assert {record["status"] for record in first["proxy_controls"]} == {"source_mismatched"}
    assert first["configuration"]["historical_values_only"] is True
    assert first["configuration"]["promotion_allowed"] is False
    assert first["configuration"]["production_champion_candidate_allowed"] is True
    assert first["configuration"]["formal_promotion_allowed"] is False
    assert first["research_optimization"]["denominators"]["contract_cells"] == 21
    assert first["research_optimization"]["gates"]["formal_promotion_allowed"] is False
    assert len(first["research_optimization"]["raw_primary"][0]["fixed_candidate_ablation"]) == 12
    assert first["upstream_ridge_control"]["denominators"]["contract_cells"] == 6
    assert first["upstream_ridge_control"]["formal_promotion_allowed"] is False
    assert first["upstream_ridge_control"]["denominators"]["sensitivity_cells"] == 6
    assert len(first["upstream_ridge_control"]["cells"][0]["feature_group_ablation"]) == len(
        FEATURE_GROUP_ABLATION_ORDER
    )
    assert all(
        cell["effect_eligible"] is False
        and cell["performance_gate_passed"] is False
        and "anomaly_quarantine_sensitivity_not_promotion_evidence" in cell["gate_reasons"]
        for cell in first["upstream_ridge_control"]["anomaly_quarantine_sensitivity"]["cells"]
    )
    assert first["chain_pass_through_control"]["denominators"]["contract_cells"] == 6
    assert first["chain_pass_through_control"]["formal_promotion_allowed"] is False
    assert first["chain_pass_through_control"]["zero_intercept"] is True
    assert first["chain_pass_through_control"]["non_negative_driver_coefficient"] is True
    assert first["chain_pass_through_control"]["driver_contract"]["dty"] == {
        "kind": "direct_upstream_label",
        "series": "poy",
    }
    assert first["chain_pass_through_control"]["driver_contract"]["poy"]["formula"] == [
        {"series": "pta", "weight": 0.855},
        {"series": "meg", "weight": 0.335},
    ]
    assert first["report_body_sha256"] == second["report_body_sha256"]


def test_simulation_rejects_incomplete_target_contract() -> None:
    exact = {target: _series(target, 10) for target in CURRENT_FORMAL_TARGETS[:-1]}

    try:
        run_isolated_historical_simulation(
            exact_series=exact,
            proxy_series={},
            as_of_time="2026-09-01T23:59:59+08:00",
            pilot=True,
        )
    except ValueError as exc:
        assert str(exc) == "simulation_exact_series_contract_incomplete"
    else:
        raise AssertionError("incomplete 21-cell contract must fail closed")


def test_online_anomaly_quarantine_preserves_raw_observation_and_recovers() -> None:
    loaded = _series("poy", 24)
    points = list(loaded.points)
    points[12] = replace(points[12], value=1000.0)
    altered = LoadedLabelSeries(points=tuple(points), source_matches_label=True)

    filtered, anomalies = quarantine_online_anomalies(target="poy", loaded=altered)

    assert altered.points[12].value == 1000.0
    assert len(filtered.points) == len(altered.points) - 1
    assert [row["observation_id"] for row in anomalies] == ["poy-12"]
    assert anomalies[0]["policy_version"] == ANOMALY_POLICY_VERSION
    assert altered.points[13] in filtered.points


def test_mad_direction_band_is_not_dominated_by_single_extreme_point() -> None:
    loaded = _series("poy", 30)
    points = list(loaded.points)
    points[-1] = replace(points[-1], value=1000.0)

    band = mad_neutral_band(training=tuple(points), target="poy", horizon_days=30)

    assert 0.006 <= band < 0.05


def test_candidate_library_is_deterministic_and_complete() -> None:
    training = _series("crude", 140).points

    first = candidate_forecasts(training=training, horizon_days=7, target="crude")
    second = candidate_forecasts(training=training, horizon_days=7, target="crude")

    assert first == second
    assert set(first) == {
        "robust-drift-reference.v1",
        "calendar-median-drift.v1",
        "damped-theil-sen.w20.d025.v1",
        "damped-theil-sen.w20.d050.v1",
        "damped-theil-sen.w60.d025.v1",
        "damped-theil-sen.w60.d050.v1",
        "damped-theil-sen.w120.d025.v1",
        "damped-theil-sen.w120.d050.v1",
        "robust-level-reversion.w20.h7.s050.v1",
        "robust-level-reversion.w20.h30.s050.v1",
        "robust-level-reversion.w60.h7.s050.v1",
        "robust-level-reversion.w60.h30.s050.v1",
    }
    assert all(value > 0 for value in first.values())


def test_nested_selection_ignores_outcomes_not_visible_at_origin() -> None:
    records = []
    for index in range(10):
        forecasts = {candidate_id: 110.0 for candidate_id in CANDIDATE_IDS}
        forecasts[CANDIDATE_IDS[0]] = 100.0
        records.append(
            {
                "origin_index": index,
                "origin_visible_at": f"2025-01-{index + 1:02d}T12:00:00+00:00",
                "actual_visible_at": f"2025-01-{index + 2:02d}T10:00:00+00:00",
                "actual_observation_id": f"actual-{index}",
                "actual": 100.0,
                "candidate_forecasts": forecasts,
            }
        )
    future_forecasts = {candidate_id: 200.0 for candidate_id in CANDIDATE_IDS}
    future_forecasts[CANDIDATE_IDS[0]] = 100.0
    records.append(
        {
            "origin_index": 10,
            "origin_visible_at": "2025-01-11T12:00:00+00:00",
            "actual_visible_at": "2025-01-12T10:00:00+00:00",
            "actual_observation_id": "future-actual",
            "actual": 200.0,
            "candidate_forecasts": future_forecasts,
        }
    )
    origin = {"origin_index": 20, "origin_visible_at": "2025-01-11T12:00:00+00:00"}

    selected, fallback, effective = select_candidate_from_past(origin_record=origin, origin_records=records)

    assert selected == CANDIDATE_IDS[0]
    assert fallback is False
    assert effective == 10


def test_label_registry_v5_switches_crude_to_brent_futures_close() -> None:
    assert CURRENT_LABEL_REGISTRY_VERSION == "seven-product-labels.v5"
    assert LABEL_REGISTRY["crude"].series_id == "crude.brent.futures.yahoo.usd_bbl"
    assert LABEL_REGISTRY["crude"].source_id == "yahoo_futures_daily_proxy"
    assert LABEL_REGISTRY["crude"].frequency == "business_day"
    # v4 continuity: polyester frequency metadata stays corrected.
    assert LABEL_REGISTRY["poy"].frequency == "published_day"
    assert LABEL_REGISTRY["dty"].frequency == "published_day"
    assert all(
        LABEL_REGISTRY[target].frequency == "business_day" for target in ("crude", "naphtha", "px", "pta", "meg")
    )
    # v1-v4 forecasts keep settling against the EIA spot identity they were issued with.
    assert (
        frozen_label_identity(
            target="crude",
            registry_version="seven-product-labels.v4",
            series_id="crude.brent.eia.spot.usd_bbl",
        ).source_id
        == "eia_petroleum_api"
    )
    assert (
        frozen_label_identity(
            target="meg",
            registry_version="seven-product-labels.v3",
            series_id="meg.sunsirs.china.spot_assessment.cny_mt",
        ).source_id
        == "sunsirs_public_commodity_assessment"
    )


def test_upstream_feature_vector_ignores_feature_not_visible_at_origin() -> None:
    target = _series("poy", 40)
    features = {name: _series(name, 40) for name in UPSTREAM_FEATURE_SERIES}
    origin = target.points[30]
    first, _ = build_upstream_feature_vector(
        origin_observed_at=origin.observed_at,
        origin_visible_at=origin.visible_at,
        target_points=target.points[:31],
        feature_series=features,
    )
    hidden = replace(
        features["crude"].points[29],
        observation_id="hidden-future-revision",
        visible_at=(datetime(2025, 3, 1, tzinfo=UTC)).isoformat(),
        value=10_000.0,
    )
    changed = {
        **features,
        "crude": LoadedLabelSeries(
            points=(*features["crude"].points, hidden),
            source_matches_label=True,
        ),
    }
    second, _ = build_upstream_feature_vector(
        origin_observed_at=origin.observed_at,
        origin_visible_at=origin.visible_at,
        target_points=target.points[:31],
        feature_series=changed,
    )

    assert first.tolist() == second.tolist()


def test_feature_group_ablation_is_cumulative_and_full_group_is_complete() -> None:
    counts = [len(feature_indices_for_series(included_series)) for _, included_series in FEATURE_GROUP_ABLATION_ORDER]

    assert counts == sorted(counts)
    assert counts[0] == 7
    assert counts[-1] == len(feature_names())
    assert feature_indices_for_series(("target", *UPSTREAM_FEATURE_SERIES)) == tuple(range(len(feature_names())))


def test_closed_form_ridge_prediction_is_deterministic() -> None:
    records = [
        {
            "features": np.asarray([float(index), float(index % 3)]),
            "actual_log_return": 0.01 * index,
        }
        for index in range(1, 31)
    ]
    feature = np.asarray([31.0, 1.0])

    first = fit_predict_ridge_return(training_records=records, feature_vector=feature, penalty=0.1)
    second = fit_predict_ridge_return(training_records=records, feature_vector=feature, penalty=0.1)

    assert first == second
    assert abs(first - 0.31) < 0.01


def test_material_basket_return_uses_frozen_weights_and_origin_visible_points() -> None:
    pta = _series("pta", 40)
    meg = _series("meg", 40)
    origin_time = datetime(2025, 2, 5, tzinfo=UTC)
    cutoff = datetime(2025, 2, 11, tzinfo=UTC)

    result = material_basket_return(
        origin_time=origin_time,
        cutoff=cutoff,
        lag_days=7,
        pta_points=pta.points,
        meg_points=meg.points,
    )

    assert result is not None
    observed_return, lineage = result
    current_index = 35
    baseline_index = 28
    current = (
        MATERIAL_WEIGHTS["pta"] * pta.points[current_index].value
        + MATERIAL_WEIGHTS["meg"] * meg.points[current_index].value
    )
    baseline = (
        MATERIAL_WEIGHTS["pta"] * pta.points[baseline_index].value
        + MATERIAL_WEIGHTS["meg"] * meg.points[baseline_index].value
    )
    assert observed_return == np.log(current / baseline)
    assert lineage == {
        "pta_current": "pta-35",
        "meg_current": "meg-35",
        "pta_baseline": "pta-28",
        "meg_baseline": "meg-28",
    }


def test_non_negative_pass_through_collapses_inverse_relation_to_persistence() -> None:
    records = [
        {"driver_return": index / 100.0, "actual_log_return": -index / 200.0}
        for index in range(1, 31)
    ]

    prediction, coefficient = fit_predict_non_negative_pass_through(
        training_records=records,
        driver_return=0.4,
        penalty=0.1,
    )

    assert prediction == 0.0
    assert coefficient == 0.0


def test_chain_penalty_selection_ignores_outcomes_not_visible_at_validation_origin() -> None:
    records = [
        {
            "origin_visible_at": (datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
            "actual_visible_at": (datetime(2025, 1, 2, tzinfo=UTC) + timedelta(days=index)).isoformat(),
            "actual_observation_id": f"actual-{index}",
            "origin": 100.0,
            "actual": 100.0 + index,
            "horizon_days": 1,
            "driver_return": index / 100.0,
            "actual_log_return": index / 100.0,
        }
        for index in range(24)
    ]
    hidden = {
        **records[-1],
        "actual_visible_at": "2026-01-01T00:00:00+00:00",
        "actual_observation_id": "hidden-future",
        "actual_log_return": -10.0,
    }

    cutoff = "2025-02-01T00:00:00+00:00"
    first = select_chain_penalty_from_past(records, as_of_origin_visible_at=cutoff)
    second = select_chain_penalty_from_past([*records, hidden], as_of_origin_visible_at=cutoff)

    assert first == second

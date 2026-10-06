from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.seven_product_contract import CURRENT_FORMAL_HORIZONS, CURRENT_FORMAL_TARGETS
from app.seven_product_evaluation import evaluate_seven_product_forecast
from app.seven_product_forecast import LoadedLabelSeries, PricePoint, build_seven_product_forecast
from app.seven_product_model_governance import (
    SevenProductModelGovernanceError,
    approve_model_promotion,
    approve_reference_promotion,
    formal_runtime_decision,
    load_model_registry,
    reference_runtime_decision,
    registry_status,
    rollback_model_cells,
)


def _passing_series(target: str, _as_of: datetime) -> LoadedLabelSeries:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return LoadedLabelSeries(
        points=tuple(
            PricePoint(
                observation_id=f"{target}-{index}",
                observed_at=(start + timedelta(days=index)).isoformat(),
                visible_at=(start + timedelta(days=index, hours=1)).isoformat(),
                value=100 * (1.01**index),
                unit="USD/mt",
                source_id=f"source-{target}",
                source_url=f"https://example.com/{target}/{index}",
            )
            for index in range(200)
        ),
        source_matches_label=True,
    )


def _passing_evaluation():
    return evaluate_seven_product_forecast(
        as_of_time="2026-01-01T00:00:00Z",
        series_loader=_passing_series,
        bootstrap_replicates=100,
    )


def test_default_registry_has_no_champions_or_automatic_promotion() -> None:
    status = registry_status(load_model_registry())
    assert status["champion_count"] == 0
    assert status["reference_champion_count"] == 0
    assert status["automatic_promotion"] is False
    assert status["reference_formal_status_ceiling"] == "reference"
    assert len(status["cells"]) == 21


def test_explicit_21_cell_approval_binds_evaluation_and_preserves_rollback() -> None:
    evaluation = _passing_evaluation()
    approved_cells = [(target, horizon) for target in CURRENT_FORMAL_TARGETS for horizon in CURRENT_FORMAL_HORIZONS]
    promoted = approve_model_promotion(
        registry=load_model_registry(),
        evaluation=evaluation,
        candidate_model_version="robust-drift-reference.v1",
        approved_cells=approved_cells,
        actor="test-owner",
        reason="all frozen per-cell gates passed",
        approved=True,
        approved_at="2026-01-02T00:00:00Z",
    )

    assert registry_status(promoted)["champion_count"] == 21
    transition = promoted["promotion_history"][-1]
    assert transition["evaluation_report_sha256"] == evaluation.report_sha256
    assert len(transition["cells"]) == 21
    assert all(value is None for value in promoted["rollback_targets"].values())
    forecast = build_seven_product_forecast(
        as_of_time="2025-07-20T00:00:00Z",
        series_loader=_passing_series,
        model_registry=promoted,
    )
    assert forecast.formal_count == 21
    assert all(cell.evaluation_status == "passed" and cell.evaluation_id for cell in forecast.cells)

    rolled_back = rollback_model_cells(
        registry=promoted,
        cells=[("crude", 1)],
        actor="test-owner",
        reason="drift alarm",
        approved=True,
        rolled_back_at="2026-01-03T00:00:00Z",
    )
    assert rolled_back["champions"]["crude"]["1"] is None
    assert rolled_back["rollback_targets"]["crude:1"] == "robust-drift-reference.v1"


def test_failed_or_unapproved_promotion_is_rejected() -> None:
    evaluation = _passing_evaluation()
    registry = load_model_registry()
    with pytest.raises(SevenProductModelGovernanceError, match="explicit_promotion_approval_required"):
        approve_model_promotion(
            registry=registry,
            evaluation=evaluation,
            candidate_model_version="robust-drift-reference.v1",
            approved_cells=[("crude", 1)],
            actor="test-owner",
            reason="not actually approved",
            approved=False,
        )

    def insufficient_series(target: str, as_of: datetime) -> LoadedLabelSeries:
        loaded = _passing_series(target, as_of)
        return LoadedLabelSeries(points=loaded.points[:10], source_matches_label=True)

    failed_evaluation = evaluate_seven_product_forecast(
        as_of_time="2026-01-01T00:00:00Z",
        series_loader=insufficient_series,
        bootstrap_replicates=100,
    )
    with pytest.raises(SevenProductModelGovernanceError, match="evaluation_gate_failed:crude:1"):
        approve_model_promotion(
            registry=registry,
            evaluation=failed_evaluation,
            candidate_model_version="robust-drift-reference.v1",
            approved_cells=[("crude", 1)],
            actor="test-owner",
            reason="failed gate",
            approved=True,
        )


def test_formal_promotion_rejects_tampered_report_and_unimplemented_runtime() -> None:
    evaluation = _passing_evaluation()
    tampered_cell = evaluation.cells[0].model_copy(update={"candidate_mae": 123.456})
    tampered = evaluation.model_copy(update={"cells": [tampered_cell, *evaluation.cells[1:]]})
    with pytest.raises(SevenProductModelGovernanceError, match="evaluation_report_hash_invalid"):
        approve_model_promotion(
            registry=load_model_registry(),
            evaluation=tampered,
            candidate_model_version="robust-drift-reference.v1",
            approved_cells=[("crude", 1)],
            actor="test-owner",
            reason="tampered evidence",
            approved=True,
        )

    registry = load_model_registry()
    registry["candidate_models"]["robust-drift-reference.v1"]["runtime_implemented"] = False
    with pytest.raises(SevenProductModelGovernanceError, match="candidate_model_runtime_not_implemented"):
        approve_model_promotion(
            registry=registry,
            evaluation=evaluation,
            candidate_model_version="robust-drift-reference.v1",
            approved_cells=[("crude", 1)],
            actor="test-owner",
            reason="not deployable",
            approved=True,
        )


def test_formal_champion_precedes_reference_and_fails_closed_on_runtime_regression() -> None:
    evaluation = _passing_evaluation()
    registry = approve_model_promotion(
        registry=load_model_registry(),
        evaluation=evaluation,
        candidate_model_version="robust-drift-reference.v1",
        approved_cells=[("crude", 1)],
        actor="test-owner",
        reason="formal gate passed",
        approved=True,
    )
    registry["reference_champions"]["crude"]["1"] = "persistence.v1"
    registry["reference_rollback_targets"]["crude:1"] = "persistence.v1"

    selected = formal_runtime_decision(
        registry=registry,
        target="crude",
        horizon_days=1,
        feature_readiness={"target": True},
        settled_history=[],
    )
    assert selected is not None
    assert selected["governance_tier"] == "formal"
    assert selected["selected_model_version"] == "robust-drift-reference.v1"

    missing = formal_runtime_decision(
        registry=registry,
        target="crude",
        horizon_days=1,
        feature_readiness={"target": False},
        settled_history=[],
    )
    assert missing is not None
    assert missing["selected_model_version"] == "persistence.v1"
    assert missing["fallback_reason"] == "formal_runtime_features_unavailable:target"

    forecast = build_seven_product_forecast(
        as_of_time="2025-07-20T00:00:00Z",
        series_loader=_passing_series,
        model_registry=registry,
    )
    crude_d1 = next(cell for cell in forecast.cells if cell.target == "crude" and cell.horizon_days == 1)
    assert crude_d1.formal_status == "formal"
    assert crude_d1.model_version == "robust-drift-reference.v1"
    assert "runtime_model_tier=formal" in crude_d1.key_drivers
    assert "runtime_model_best_naive_loss_streak=0" in crude_d1.key_drivers

    losing_history = [
        {
            "model_version": "robust-drift-reference.v1",
            "model_absolute_error": error,
            "persistence_absolute_error": 1.0,
            "seasonal_naive_absolute_error": 0.5,
            "invalidated": False,
        }
        for error in (0.75, 0.8, 0.9)
    ]
    degraded = build_seven_product_forecast(
        as_of_time="2025-07-20T00:00:00Z",
        series_loader=_passing_series,
        model_registry=registry,
        runtime_model_history={("crude", 1): losing_history},
    )
    crude_d1 = next(cell for cell in degraded.cells if cell.target == "crude" and cell.horizon_days == 1)
    assert crude_d1.formal_eligible is False
    assert crude_d1.formal_status == "degraded"
    assert crude_d1.model_version == "persistence.v1"
    assert crude_d1.evaluation_id is None
    assert "runtime_model_best_naive_loss_streak=3" in crude_d1.key_drivers
    assert (
        "formal_champion_fallback:formal_three_consecutive_losses_to_best_naive"
        in crude_d1.data_gaps
    )


def _research_report(*, target: str = "crude", horizon_days: int = 1) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": "test-reference-research.v1",
        "mode": "SIMULATION_ONLY",
        "formal_promotion_allowed": False,
        "promotion_candidates": [
            {
                "target": target,
                "horizon_days": horizon_days,
                "model_version": "robust-drift-reference.v1",
                "formal_status_ceiling": "reference",
            }
        ],
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    body["report_body_sha256"] = hashlib.sha256(encoded.encode()).hexdigest()
    return body


def test_reference_promotion_is_separate_hash_bound_and_reference_only() -> None:
    promoted = approve_reference_promotion(
        registry=load_model_registry(),
        research_report=_research_report(),
        candidate_model_version="robust-drift-reference.v1",
        approved_cells=[("crude", 1)],
        actor="test-owner",
        reason="historical reference gate passed",
        approved=True,
        approved_at="2026-01-02T00:00:00Z",
    )

    status = registry_status(promoted)
    assert status["reference_champion_count"] == 1
    assert status["champion_count"] == 0
    assert promoted["reference_champions"]["crude"]["1"] == "robust-drift-reference.v1"
    assert promoted["reference_rollback_targets"]["crude:1"] == "persistence.v1"
    forecast = build_seven_product_forecast(
        as_of_time="2025-07-20T00:00:00Z",
        series_loader=_passing_series,
        model_registry=promoted,
    )
    crude_d1 = next(cell for cell in forecast.cells if cell.target == "crude" and cell.horizon_days == 1)
    assert crude_d1.model_version == "robust-drift-reference.v1"
    assert crude_d1.formal_status == "reference"
    assert crude_d1.formal_eligible is False


def test_reference_promotion_rejects_tampered_or_unlisted_research() -> None:
    report = _research_report()
    report["mode"] = "tampered"
    with pytest.raises(SevenProductModelGovernanceError, match="reference_research_report_hash_invalid"):
        approve_reference_promotion(
            registry=load_model_registry(),
            research_report=report,
            candidate_model_version="robust-drift-reference.v1",
            approved_cells=[("crude", 1)],
            actor="test-owner",
            reason="tampered",
            approved=True,
        )

    with pytest.raises(SevenProductModelGovernanceError, match="reference_research_gate_failed:crude:7"):
        approve_reference_promotion(
            registry=load_model_registry(),
            research_report=_research_report(),
            candidate_model_version="robust-drift-reference.v1",
            approved_cells=[("crude", 7)],
            actor="test-owner",
            reason="not listed",
            approved=True,
        )


def test_reference_runtime_falls_back_on_missing_feature_or_three_valid_losses() -> None:
    promoted = approve_reference_promotion(
        registry=load_model_registry(),
        research_report=_research_report(),
        candidate_model_version="robust-drift-reference.v1",
        approved_cells=[("crude", 1)],
        actor="test-owner",
        reason="runtime guard",
        approved=True,
    )
    missing = reference_runtime_decision(
        registry=promoted,
        target="crude",
        horizon_days=1,
        feature_readiness={"target": False},
        settled_history=[],
        default_model_version="robust-drift-reference.v1",
    )
    assert missing["selected_model_version"] == "persistence.v1"
    assert missing["fallback_reason"] == "runtime_features_unavailable:target"

    history = [
        {
            "model_version": "robust-drift-reference.v1",
            "model_absolute_error": 0.75,
            "persistence_absolute_error": 1.0,
            "seasonal_naive_absolute_error": 0.5,
            "invalidated": False,
        },
        {
            "model_version": "robust-drift-reference.v1",
            "model_absolute_error": 999.0,
            "persistence_absolute_error": 0.0,
            "seasonal_naive_absolute_error": 0.0,
            "invalidated": True,
        },
        {
            "model_version": "robust-drift-reference.v1",
            "model_absolute_error": 0.8,
            "persistence_absolute_error": 1.0,
            "seasonal_naive_absolute_error": 0.5,
            "invalidated": False,
        },
        {
            "model_version": "robust-drift-reference.v1",
            "model_absolute_error": 999.0,
            "persistence_absolute_error": 1.0,
            "invalidated": False,
        },
        {
            "model_version": "persistence.v1",
            "model_absolute_error": 999.0,
            "persistence_absolute_error": 0.0,
            "seasonal_naive_absolute_error": 0.0,
            "invalidated": False,
        },
        {
            "model_version": "robust-drift-reference.v1",
            "model_absolute_error": float("nan"),
            "persistence_absolute_error": 0.0,
            "seasonal_naive_absolute_error": 0.0,
            "invalidated": False,
        },
        {
            "model_version": "robust-drift-reference.v1",
            "model_absolute_error": 999.0,
            "persistence_absolute_error": 0.0,
            "seasonal_naive_absolute_error": -1.0,
            "invalidated": False,
        },
        {
            "model_version": "robust-drift-reference.v1",
            "model_absolute_error": 0.9,
            "persistence_absolute_error": 1.0,
            "seasonal_naive_absolute_error": 0.5,
            "invalidated": False,
        },
    ]
    losses = reference_runtime_decision(
        registry=promoted,
        target="crude",
        horizon_days=1,
        feature_readiness={"target": True},
        settled_history=history,
        default_model_version="robust-drift-reference.v1",
    )
    assert losses["consecutive_losses_to_best_naive"] == 3
    assert losses["selected_model_version"] == "persistence.v1"
    assert losses["fallback_reason"] == "three_consecutive_losses_to_best_naive"

    tie_breaks_streak = reference_runtime_decision(
        registry=promoted,
        target="crude",
        horizon_days=1,
        feature_readiness={"target": True},
        settled_history=[
            *history,
            {
                "model_version": "robust-drift-reference.v1",
                "model_absolute_error": 0.5,
                "persistence_absolute_error": 1.0,
                "seasonal_naive_absolute_error": 0.5,
                "invalidated": False,
            },
        ],
        default_model_version="robust-drift-reference.v1",
    )
    assert tie_breaks_streak["consecutive_losses_to_best_naive"] == 0
    assert tie_breaks_streak["fallback_triggered"] is False

    forecast = build_seven_product_forecast(
        as_of_time="2025-07-20T00:00:00Z",
        series_loader=_passing_series,
        model_registry=promoted,
        runtime_model_history={("crude", 1): history},
    )
    crude_d1 = next(cell for cell in forecast.cells if cell.target == "crude" and cell.horizon_days == 1)
    assert crude_d1.model_version == "persistence.v1"
    assert crude_d1.point_forecast == pytest.approx(crude_d1.latest_value, abs=1e-4)
    assert crude_d1.formal_eligible is False
    assert "reference_champion_fallback:three_consecutive_losses_to_best_naive" in crude_d1.data_gaps

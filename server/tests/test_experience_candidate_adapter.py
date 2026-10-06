from __future__ import annotations

from datetime import date, timedelta

from app.experience_candidate_adapter import (
    BENCHMARK_RULE_BY_SUBTARGET,
    TARGET_SERIES_BY_SUBTARGET,
    assemble_terminal_experience_candidates,
)
from app.experience_settlement import plan_due_experience_settlements
from app.phase_a_contracts import load_contract


def _prediction_payload() -> dict:
    cells = []
    for node_id in load_contract()["formal_nodes"]:
        for horizon in (1, 7, 30):
            cells.append(
                {
                    "node_id": node_id,
                    "horizon_days": horizon,
                    "direction": "neutral",
                    "direction_probability": 0.5,
                    "magnitude": 0.0,
                    "magnitude_unit": "index_point",
                    "confidence": 0.6,
                    "remaining_effective_probability": 0.5,
                    "driver_event_ids": [],
                    "counter_event_ids": [],
                    "data_completeness": 1.0,
                    "scoreability": "scorable",
                    "missing_series_ids": [],
                    "subtarget_results": (
                        [_subtarget("poy"), _subtarget("dty")]
                        if node_id == "poy_dty_upstream_cost_pressure"
                        else []
                    ),
                }
            )
    return {
        "schema_version": "phase-a.prediction.v1",
        "prediction_batch_id": "formal-batch-1",
        "revision_id": "formal-revision-1",
        "previous_revision_id": None,
        "business_date": "2025-12-31",
        "data_frozen_at": "2025-12-31T08:20:00+08:00",
        "published_at": "2025-12-31T09:30:00+08:00",
        "as_of_time": "2025-12-31T08:20:00+08:00",
        "data_snapshot_id": "prediction-snapshot-1",
        "composition_rule_version": "phase-a.composition.v1",
        "cells": cells,
        "created_at": "2025-12-31T08:25:00+08:00",
    }


def _subtarget(target: str) -> dict:
    return {
        "target": target,
        "direction": "up",
        "direction_probability": 0.7,
        "magnitude": 1.0,
        "magnitude_unit": "index_point",
        "confidence": 0.6,
        "remaining_effective_probability": 0.5,
        "data_completeness": 1.0,
        "scoreability": "scorable",
        "missing_series_ids": [],
    }


def _snapshot() -> dict:
    rows = []
    start = date(2026, 1, 1)
    for offset in range(21):
        observed_at = (start + timedelta(days=offset)).isoformat()
        rows.extend(
            [
                _price_row("PTA", "内盘PTA", observed_at, 5000 + offset * 10),
                _price_row("MEG", "内盘MEG现货", observed_at, 4000 + offset * 5),
                _price_row("POY", "POY 150D/48F", observed_at, 7000 + offset * 8),
                _price_row("DTY", "DTY 150D/48F低弹", observed_at, 8000 + offset * 7),
            ]
        )
    return {"payload": {"authorized_price_observations": rows}}


def _price_row(product: str, spec: str, observed_at: str, price: float) -> dict:
    point_id = f"{product}:{spec}:{observed_at}"
    return {
        "point_id": point_id,
        "product": product,
        "spec": spec,
        "observed_at": observed_at,
        "price": price,
        "unit": "CNY/mt",
        "quote_type": "authorized_daily_assessment",
        "capture_revision_id": f"capture:{point_id}",
        "raw": {"visible_at": f"{observed_at}T07:30:00+08:00"},
    }


def _statuses(*, target_status: str = "eligible") -> dict[str, str]:
    statuses = {
        "pta.ccf.domestic.daily_assessment.cny_mt": "eligible",
        "meg.ccf.domestic.daily_assessment.cny_mt": "eligible",
    }
    statuses.update({target: target_status for target in TARGET_SERIES_BY_SUBTARGET.values()})
    statuses.update({rule[2]: "eligible" for rule in BENCHMARK_RULE_BY_SUBTARGET.values()})
    return statuses


def test_adapter_builds_independent_poy_and_dty_candidates_with_immutable_lineage() -> None:
    result = assemble_terminal_experience_candidates(
        prediction_payload=_prediction_payload(),
        evaluation_snapshot=_snapshot(),
        expected_observation_dates=[(date(2026, 1, 1) + timedelta(days=index)).isoformat() for index in range(30)],
        series_statuses=_statuses(),
    )

    assert result["derivation_status"] == "ready"
    poy, dty = result["candidates"]
    assert poy["prediction_bundle"]["subtarget"] == "poy"
    assert dty["prediction_bundle"]["subtarget"] == "dty"
    assert poy["prediction_bundle"]["target_series_id"] != dty["prediction_bundle"]["target_series_id"]
    assert poy["prediction_bundle"]["benchmark_series_id"] != dty["prediction_bundle"]["benchmark_series_id"]
    assert len(poy["target_points"]) == len(dty["target_points"]) == 2
    assert len(poy["target_points"][-1]["capture_revision_ids"]) == 40
    assert poy["benchmark_observation_status"] == dty["benchmark_observation_status"] == "available"


def test_adapter_preserves_formal_block_and_cannot_produce_settlement_actions() -> None:
    result = assemble_terminal_experience_candidates(
        prediction_payload=_prediction_payload(),
        evaluation_snapshot=_snapshot(),
        expected_observation_dates=[(date(2026, 1, 1) + timedelta(days=index)).isoformat() for index in range(30)],
        series_statuses=_statuses(target_status="blocked"),
    )

    plan = plan_due_experience_settlements(
        candidates=result["candidates"], evaluation_as_of="2026-01-30T12:00:00+08:00"
    )
    assert [item["status"] for item in plan["items"]] == ["blocked", "blocked"]
    assert all(item["actions"] == [] for item in plan["items"])

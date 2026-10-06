from __future__ import annotations

from copy import deepcopy

import pytest

from app import experience_candidate_loader as loader
from app import phase_a_contracts
from app.experience_candidate_adapter import (
    BENCHMARK_RULE_BY_SUBTARGET,
    COMPONENT_SERIES_BY_PRODUCT_SPEC,
    TARGET_SERIES_BY_SUBTARGET,
)
from app.formal_prediction_batches import FormalPredictionBatchError


def _prediction_payload() -> dict[str, object]:
    cells = []
    for node_id in phase_a_contracts.load_contract()["formal_nodes"]:
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
                        [
                            {
                                "target": target,
                                "direction": "neutral",
                                "direction_probability": 0.5,
                                "magnitude": 0.0,
                                "magnitude_unit": "index_point",
                                "confidence": 0.6,
                                "remaining_effective_probability": 0.5,
                                "data_completeness": 1.0,
                                "scoreability": "scorable",
                                "missing_series_ids": [],
                            }
                            for target in ("poy", "dty")
                        ]
                        if node_id == "poy_dty_upstream_cost_pressure"
                        else []
                    ),
                }
            )
    return {
        "schema_version": "phase-a.prediction.v1",
        "prediction_batch_id": "batch-1",
        "revision_id": "revision-1",
        "previous_revision_id": None,
        "business_date": "2026-07-31",
        "data_frozen_at": "2026-08-01T08:20:00+08:00",
        "published_at": "2026-08-01T09:30:00+08:00",
        "data_snapshot_id": "prediction-snapshot-1",
        "as_of_time": "2026-08-01T08:20:00+08:00",
        "composition_rule_version": "phase-a.composition.v1",
        "cells": cells,
        "created_at": "2026-08-01T08:25:00+08:00",
    }


def _snapshot() -> dict[str, object]:
    rows = []
    for offset in range(21):
        day = f"2026-07-{offset + 1:02d}"
        for product, spec, price in (
            ("PTA", "内盘PTA", 5000 + offset),
            ("MEG", "内盘MEG现货", 4000 + offset),
            ("POY", "POY 150D/48F", 7000 + offset),
            ("DTY", "DTY 150D/48F低弹", 8000 + offset),
        ):
            rows.append(
                {
                    "point_id": f"{product}-{offset}",
                    "product": product,
                    "spec": spec,
                    "observed_at": day,
                    "price": price,
                    "unit": "CNY/mt",
                    "quote_type": "assessment",
                    "capture_revision_id": f"capture-{product}-{offset}",
                    "raw": {"visible_at": f"{day}T08:00:00+08:00"},
                }
            )
    return {
        "snapshot_id": "evaluation-snapshot-1",
        "metadata": {"as_of_time": "2026-08-01T08:20:00+08:00"},
        "payload": {"authorized_price_observations": rows},
    }


def _statuses() -> dict[str, str]:
    return {
        **{series_id: "eligible" for series_id in COMPONENT_SERIES_BY_PRODUCT_SPEC.values()},
        **{series_id: "blocked" for _, _, series_id in BENCHMARK_RULE_BY_SUBTARGET.values()},
        **{series_id: "blocked" for series_id in TARGET_SERIES_BY_SUBTARGET.values()},
    }


def test_loader_reaudits_explicit_inputs_and_preserves_blocked_statuses(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = _prediction_payload()
    snapshot = _snapshot()
    calls: list[tuple[str, str]] = []

    def read_revision(*, revision_id: str, as_of_time: str) -> dict[str, object]:
        calls.append((revision_id, as_of_time))
        return {"assessment_id": "assessment-1", "payload": deepcopy(payload)}

    monkeypatch.setattr(loader.formal_prediction_batches, "load_verified_formal_prediction_revision", read_revision)
    monkeypatch.setattr(
        loader.formal_eligibility_proofs, "load_verified_formal_series_statuses", lambda **_: _statuses()
    )
    monkeypatch.setattr(loader.storage, "get_data_snapshot", lambda snapshot_id: deepcopy(snapshot))
    heads: list[str] = []

    def get_head(card_id: str) -> None:
        heads.append(card_id)
        return None

    monkeypatch.setattr(loader.storage, "get_experience_card_head", get_head)

    result = loader.load_terminal_experience_candidates(
        prediction_revision_id="revision-1",
        evaluation_snapshot_id="evaluation-snapshot-1",
        evaluation_as_of_time="2026-08-01T08:20:00+08:00",
        expected_observation_dates=["2026-08-02", "2026-08-08", "2026-08-31"],
        series_statuses=_statuses(),
    )

    assert calls == [("revision-1", "2026-08-01T08:20:00+08:00")]
    assert result["schema_version"] == "experience-candidate-loader.v1"
    assert result["evaluation_snapshot_id"] == "evaluation-snapshot-1"
    assert {item["target_series_status"] for item in result["candidates"]} == {"blocked"}
    assert len(heads) == 2


def test_loader_rejects_snapshot_newer_than_explicit_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        loader.formal_prediction_batches,
        "load_verified_formal_prediction_revision",
        lambda **_: {"assessment_id": "assessment-1", "payload": _prediction_payload()},
    )
    monkeypatch.setattr(
        loader.formal_eligibility_proofs, "load_verified_formal_series_statuses", lambda **_: _statuses()
    )
    newer_snapshot = _snapshot()
    newer_snapshot["metadata"] = {"as_of_time": "2026-08-01T08:21:00+08:00"}
    monkeypatch.setattr(loader.storage, "get_data_snapshot", lambda _: newer_snapshot)

    with pytest.raises(loader.ExperienceCandidateLoaderError, match="experience_evaluation_snapshot_after_as_of"):
        loader.load_terminal_experience_candidates(
            prediction_revision_id="revision-1",
            evaluation_snapshot_id="evaluation-snapshot-1",
            evaluation_as_of_time="2026-08-01T08:20:00+08:00",
            expected_observation_dates=[],
            series_statuses=_statuses(),
        )


def test_loader_preserves_verified_reader_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def reject(**_: object) -> None:
        raise FormalPredictionBatchError("formal_prediction_projection_mismatch")

    monkeypatch.setattr(loader.formal_prediction_batches, "load_verified_formal_prediction_revision", reject)
    with pytest.raises(loader.ExperienceCandidateLoaderError, match="formal_prediction_projection_mismatch"):
        loader.load_terminal_experience_candidates(
            prediction_revision_id="revision-1",
            evaluation_snapshot_id="evaluation-snapshot-1",
            evaluation_as_of_time="2026-08-01T08:20:00+08:00",
            expected_observation_dates=[],
            series_statuses=None,
        )


def test_loader_rejects_caller_status_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        loader.formal_prediction_batches,
        "load_verified_formal_prediction_revision",
        lambda **_: {"assessment_id": "assessment-1", "payload": _prediction_payload()},
    )
    monkeypatch.setattr(
        loader.formal_eligibility_proofs, "load_verified_formal_series_statuses", lambda **_: _statuses()
    )
    monkeypatch.setattr(loader.storage, "get_data_snapshot", lambda _: _snapshot())
    monkeypatch.setattr(loader.storage, "get_experience_card_head", lambda _: None)

    with pytest.raises(loader.ExperienceCandidateLoaderError, match="experience_series_status_override_rejected"):
        loader.load_terminal_experience_candidates(
            prediction_revision_id="revision-1",
            evaluation_snapshot_id="evaluation-snapshot-1",
            evaluation_as_of_time="2026-08-01T08:20:00+08:00",
            expected_observation_dates=[],
            series_statuses={},
        )

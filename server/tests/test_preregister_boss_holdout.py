from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from server.scripts.preregister_boss_holdout import append_prediction, append_score, freeze, status


def source_artifact(path: Path) -> Path:
    payload = {
        "guardrails": {"prediction_features": "decision-day features", "rag_visible_at_required": True},
        "best_accepted_candidate": {
            "name": "upstream_crack_mid_down_profit_stress_gate_t1",
            "family": "证据门控",
            "params": {
                "weights": {"Brent": 1, "WTI": 1, "CRACK": 1},
                "prediction_threshold": 1,
                "actual_threshold": 2,
                "gate_policy": {"mid_down_conflict_min_mid_abs": 1, "bullish_polyester_profit_max": 0},
            },
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_freeze_is_idempotent_but_cannot_be_rewritten(tmp_path: Path) -> None:
    source = source_artifact(tmp_path / "source.json")
    registry = tmp_path / "registry"
    first = freeze(source, registry, start_date=date(2026, 7, 12), min_scored=60, max_days=180)
    second = freeze(source, registry, start_date=date(2026, 7, 12), min_scored=60, max_days=180)
    assert first == second
    assert first["strategy_hash"]
    with pytest.raises(ValueError, match="immutable"):
        freeze(source, registry, start_date=date(2026, 7, 13), min_scored=60, max_days=180)


def test_prediction_cannot_be_initialized_with_history_or_future_answer(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    freeze(source_artifact(tmp_path / "source.json"), registry, start_date=date(2026, 7, 12), min_scored=2, max_days=30)
    base = {"as_of_time": "2026-07-12T12:00:00Z", "direction": "偏强", "features": {"crack": 1.2}}
    with pytest.raises(ValueError, match="historical"):
        append_prediction(registry, {**base, "decision_date": "2026-07-11"})
    with pytest.raises(ValueError, match="posterior"):
        append_prediction(registry, {**base, "decision_date": "2026-07-12", "actual_direction": "偏强"})
    prediction = append_prediction(registry, {**base, "decision_date": "2026-07-12"})
    assert prediction["previous_hash"] == "GENESIS"
    with pytest.raises(ValueError, match="already exists"):
        append_prediction(registry, {**base, "decision_date": "2026-07-12"})


def test_score_requires_prior_prediction_and_post_decision_visibility(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    freeze(source_artifact(tmp_path / "source.json"), registry, start_date=date(2026, 7, 12), min_scored=2, max_days=30)
    with pytest.raises(ValueError, match="existing"):
        append_score(
            registry, {"prediction_id": "missing", "observed_at": "2026-07-13T12:00:00Z", "actual_direction": "偏强"}
        )
    append_prediction(
        registry,
        {"decision_date": "2026-07-12", "as_of_time": "2026-07-12T12:00:00Z", "direction": "偏强", "features": {}},
    )
    with pytest.raises(ValueError, match="after"):
        append_score(
            registry, {"prediction_id": "2026-07-12", "observed_at": "2026-07-12T23:00:00Z", "actual_direction": "偏强"}
        )
    score = append_score(
        registry, {"prediction_id": "2026-07-12", "observed_at": "2026-07-13T12:00:00Z", "actual_direction": "偏强"}
    )
    assert score["hit"] is True


def test_empty_new_holdout_remains_pending(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    freeze(
        source_artifact(tmp_path / "source.json"), registry, start_date=date(2026, 7, 12), min_scored=60, max_days=180
    )
    result = status(registry, today=date(2026, 7, 12))
    assert result["status"] == "pending"
    assert result["predictions"] == 0
    assert result["scored"] == 0
    assert result["remaining_to_evaluate"] == 60


def test_append_chain_detects_manual_rewrite(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    freeze(source_artifact(tmp_path / "source.json"), registry, start_date=date(2026, 7, 12), min_scored=2, max_days=30)
    append_prediction(
        registry,
        {"decision_date": "2026-07-12", "as_of_time": "2026-07-12T12:00:00Z", "direction": "偏强", "features": {}},
    )
    path = registry / "predictions.jsonl"
    row = json.loads(path.read_text(encoding="utf-8"))
    row["direction"] = "偏弱"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="chain verification"):
        status(registry, today=date(2026, 7, 12))

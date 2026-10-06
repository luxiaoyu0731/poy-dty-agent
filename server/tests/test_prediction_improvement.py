from __future__ import annotations

import copy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from app.prediction_benchmark import MODEL_IDS, run_benchmark
from app.prediction_features import (
    CLASSIFIER_CANDIDATES,
    FEATURE_CANDIDATES,
    TARGET_CONTROL,
    candidates_at,
    feature_vector,
    fit_direction_classifier,
    supervised_pairs,
)
from app.prediction_improvement import (
    ALL_MODELS,
    ENSEMBLE,
    EXPERTS,
    FAMILIES,
    PRIOR,
    SELECTIVE,
    ensemble_prediction,
    run_improvement,
    summarize_calls,
)
from app.prediction_replay import digest, load_export, timestamp
from app.seven_product_forecast import PricePoint


def prices(count=90):
    return tuple(
        PricePoint(
            observation_id=str(i),
            observed_at=(date(2026, 1, 1) + timedelta(days=i)).isoformat(),
            visible_at=(datetime(2026, 1, 1, 9, tzinfo=UTC) + timedelta(days=i)).isoformat(),
            value=100 + 0.1 * i + np.sin(i / 4),
            source_id="test",
            unit="CNY/mt",
            source_url="https://example.test",
            raw_sha256="a" * 64,
            semantic_series_id="poy.test",
            contract_version="v1",
        )
        for i in range(count)
    )


def export_body():
    records = [
        {
            "revision_id": p.observation_id,
            "observed_at": p.observed_at,
            "value": p.value,
            "visible_at": p.visible_at,
            "captured_at": p.visible_at,
            "created_at": p.visible_at,
            "source_id": p.source_id,
            "series_id": p.semantic_series_id,
            "unit": p.unit,
            "source_url": p.source_url,
            "evidence_sha256": p.raw_sha256,
            "hash_kind": "source_capture",
            "payload_hash_verified": True,
            "instrument_matches": True,
            "contract_version": p.contract_version,
        }
        for p in prices()
    ]
    body = {
        "schema_version": "prediction-vintages.v1",
        "as_of_time": "2026-04-01T12:00:00Z",
        "series": {
            "poy": {
                "source_id": "test",
                "series_id": "poy.test",
                "unit": "CNY/mt",
                "truncated": False,
                "records": records,
            }
        },
    }
    return {**body, "content_sha256": digest(body)}


def test_training_pairs_use_only_data_available_by_fitting_cutoff():
    points = prices()
    cutoff = timestamp("2026-03-01T12:00:00Z")
    rows = supervised_pairs(points, {}, horizon_days=7, fitting_cutoff=cutoff)
    visible = tuple(p for p in points if timestamp(p.visible_at) <= cutoff)
    reference = supervised_pairs(visible, {}, horizon_days=7, fitting_cutoff=cutoff)
    assert [r["actual_id"] for r in rows] == [r["actual_id"] for r in reference]
    assert all(timestamp(r["actual_visible_at"]) <= cutoff for r in rows)
    assert all(np.array_equal(a["features"], b["features"]) for a, b in zip(rows, reference, strict=True))


def test_training_features_do_not_read_economic_dates_after_the_anchor():
    points = prices()
    anchor = date(2026, 2, 5)
    prefix = tuple(p for p in points if date.fromisoformat(p.observed_at) <= anchor)
    a = feature_vector(points, {}, anchor)
    b = feature_vector(prefix, {}, anchor)
    assert np.array_equal(a[0], b[0]) and a[1] == b[1]


def test_publication_training_counts_quotes_after_issue_not_calendar_projection():
    points = prices()
    # Remove alternating late observations: the fifth quote is not five days away.
    points = tuple(p for p in points if int(p.observation_id) < 35 or int(p.observation_id) % 2 == 0)
    rows = supervised_pairs(
        points,
        {},
        horizon_days=5,
        publication_steps=5,
        issue_offset_days=2,
        fitting_cutoff=timestamp("2026-04-01T12:00:00Z"),
    )
    sample = next(r for r in rows if r["origin_id"] == "36")
    assert sample["actual_id"] == "48"  # After issue day38: 40,42,44,46,48.
    calendar = supervised_pairs(points, {}, horizon_days=5, fitting_cutoff=timestamp("2026-04-01T12:00:00Z"))
    assert next(r for r in calendar if r["origin_id"] == "36")["actual_id"] == "42"
    late = tuple(replace(p, visible_at="2026-05-01T00:00:00Z") if p.observation_id == "48" else p for p in points)
    changed = supervised_pairs(
        late,
        {},
        horizon_days=5,
        publication_steps=5,
        issue_offset_days=2,
        fitting_cutoff=timestamp("2026-04-01T12:00:00Z"),
    )
    assert all(r["actual_id"] != "48" for r in changed)


def test_missing_upstream_is_an_abstention_not_zero_and_new_ridge_uses_history():
    export = export_body()
    benchmark = run_benchmark(export, start=date(2026, 3, 12), end=date(2026, 3, 12))
    row = next(r for r in benchmark["records"] if r["contract_id"] == "issue-calendar-1.v1")
    candidates, _ = candidates_at(load_export(export), row)
    assert candidates["direct-target-ridge.v1"]["training_count"] >= 40
    assert candidates["direct-target-ridge.v1"]["raw_direction"] in {"down", "neutral", "up"}
    assert candidates["direct-upstream-ridge.v1"]["raw_direction"] is None
    assert candidates["upstream-momentum7.v1"]["reason"] == "upstream_unavailable"
    assert candidates["direct-target-ridge.v1"]["calibration_evidence"] is False


def learning_row(i, truth="up"):
    issue = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i * 2)
    return {
        "state": "issued",
        "regime": "up",
        "issue_at": issue.isoformat(),
        "outcome": {
            "state": "scored",
            "revision_id": str(i),
            "direction": truth,
            "settled_at": (issue + timedelta(days=1)).isoformat(),
        },
        "models": {m: {"raw_direction": "up", "reason": None} for m in (*EXPERTS, TARGET_CONTROL)},
    }


def test_ensemble_balances_correlated_families_and_does_not_call_votes_probability():
    row = learning_row(8)
    for m in MODEL_IDS:
        row["models"][m]["raw_direction"] = "neutral"
    result, selective = ensemble_prediction([learning_row(i) for i in range(6)], row)
    weights = result["weights"]
    totals = {f: sum(v for m, v in weights.items() if FAMILIES[m] == f) for f in set(FAMILIES.values())}
    assert all(v == pytest.approx(0.25) for v in totals.values())
    assert result["consensus_is_calibrated_probability"] is False
    assert selective["raw_direction"] in {None, "up", "neutral", "down"}


def test_selective_warmup_does_not_manufacture_confidence():
    _, result = ensemble_prediction([], learning_row(0))
    assert result["raw_direction"] is None
    assert result["reason"] == "consensus_or_history_insufficient"


def test_ensemble_rejects_pending_feedback_and_duplicate_outcome():
    past = [learning_row(i) for i in range(6)]
    current = learning_row(8)
    original = ensemble_prediction(past, current)
    future = learning_row(9, "down")
    delayed = learning_row(7, "down")
    delayed["outcome"]["settled_at"] = "2026-02-01T00:00:00Z"
    duplicate = copy.deepcopy(past[0])
    assert ensemble_prediction([*past, future, delayed, duplicate], current) == original


def test_same_call_baseline_not_whole_population_and_abstention_in_denominator():
    rows = [learning_row(0, "up"), learning_row(1, "down"), learning_row(2, "down")]
    for i, row in enumerate(rows):
        row["models"][SELECTIVE] = {"raw_direction": "up" if i == 0 else None, "reason": "gate"}
    result = summarize_calls(rows, SELECTIVE, cutoff=timestamp("2026-02-01T12:00:00Z"))
    assert result["metrics"]["accuracy"] == 1
    assert result["metrics"]["coverage"] == pytest.approx(1 / 3)
    assert result["paired_baselines"]["always_up"]["accuracy"] == 1
    assert result["paired_baselines"]["always_up"]["accuracy_delta"] == 0


def test_end_to_end_late_correction_cannot_change_earlier_feature_predictions_or_weights():
    exp = export_body()
    baseline = run_benchmark(exp, start=date(2026, 3, 12), end=date(2026, 3, 13))
    a = run_improvement(exp, baseline)
    assert any(r["models"][CLASSIFIER_CANDIDATES[0]]["raw_direction"] is not None for r in a["records"])
    changed = copy.deepcopy(exp)
    r = copy.deepcopy(changed["series"]["poy"]["records"][1])
    r.update(
        revision_id="correction",
        value=103,
        visible_at="2026-03-20T09:00:00Z",
        created_at="2026-03-20T09:00:00Z",
        captured_at="2026-03-20T09:00:00Z",
    )
    changed["series"]["poy"]["records"].append(r)
    changed["content_sha256"] = digest({k: v for k, v in changed.items() if k != "content_sha256"})
    second_baseline = run_benchmark(changed, start=date(2026, 3, 12), end=date(2026, 3, 13))
    b = run_improvement(changed, second_baseline)
    for x, y in zip(a["records"], b["records"], strict=True):
        for m in (*FEATURE_CANDIDATES, *CLASSIFIER_CANDIDATES, TARGET_CONTROL, ENSEMBLE, SELECTIVE, PRIOR):
            assert x["models"][m] == y["models"][m]
    assert all(m["production_promotion_allowed"] is False for m in a["summary"].values())


def test_tampered_baseline_rejected():
    exp = export_body()
    baseline = run_benchmark(exp, start=date(2026, 3, 1), end=date(2026, 3, 1))
    baseline["records"][0]["models"][MODEL_IDS[0]]["raw_direction"] = "bad"
    with pytest.raises(ValueError, match="integrity"):
        run_improvement(exp, baseline)


def test_all_reported_model_ids_are_fixed_and_distinct():
    assert len(ALL_MODELS) == 14
    assert len(set(ALL_MODELS)) == 14


def separable_pairs():
    return [
        {"features": np.asarray([float(c), 2.0]), "actual_log_return": c * 0.03} for c in (-1, 0, 1) for _ in range(20)
    ]


def test_direction_classifier_minimizes_fixed_objective_and_recovers_separable_classes():
    pairs = separable_pairs()
    for c, label in ((-1, "down"), (0, "neutral"), (1, "up")):
        result = fit_direction_classifier(pairs, np.asarray([float(c), 2.0]), "poy")
        assert result["raw_direction"] == label
        assert result["final_loss"] < result["initial_loss"]
        assert result["gradient_max"] <= 1e-5
        assert sum(result["class_scores"].values()) == pytest.approx(1)
        assert result["scores_calibrated"] is False


def test_classifier_scaling_uses_training_only_and_missing_classes_abstain():
    pairs = separable_pairs()
    vector = np.asarray([-1.0, 2.0])
    expected = fit_direction_classifier(pairs, vector, "poy")
    transformed = [{**p, "features": p["features"] * 3.5 + 200} for p in pairs]
    actual = fit_direction_classifier(transformed, vector * 3.5 + 200, "poy")
    assert actual["class_scores"] == pytest.approx(expected["class_scores"])
    assert fit_direction_classifier(pairs[:40], vector, "poy")["reason"] == "classifier_class_history_insufficient"


def test_unconverged_classifier_abstains_instead_of_reporting_a_confident_prediction(monkeypatch):
    monkeypatch.setattr("app.prediction_features.CLASSIFIER_MAX_STEPS", 1)
    result = fit_direction_classifier(separable_pairs(), np.asarray([1.0, 2.0]), "poy")
    assert result["raw_direction"] is None
    assert result["reason"] == "classifier_not_converged"

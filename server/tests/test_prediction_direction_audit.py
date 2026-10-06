from copy import deepcopy

import pytest

from scripts.audit_prediction_direction import audit_history


def _cell(*, observed="2026-09-27", confidence=0.45):
    return {
        "settlement_status": "scored", "invalidation": None,
        "forecast": {
            "target": "poy", "horizon_days": 1, "label_registry_version": "test.v1",
            "as_of_time": "2026-09-26T00:00:00Z", "latest_observation_at": "2026-09-25",
            "direction": "neutral", "confidence": confidence, "neutral_band_pct": 0.01,
        },
        "outcome": {"actual_observed_at": observed, "actual_visible_at": "2026-09-28T01:00:00Z",
                    "actual_direction": "neutral", "actual_observation_id": "same-label"},
    }


def _report(cells):
    return audit_history([{"as_of_time": "2026-09-26T00:00:00Z", "cells": cells}])


def test_delayed_past_observation_is_not_a_future_direction_trial():
    report = _report([_cell(observed="2026-09-25"), _cell(observed="2026-09-26"), _cell()])
    cell = report["cells"][0]
    assert cell["frozen_contract_scores"]["n"] == 3
    assert cell["strictly_later_observation_day"]["n"] == 1
    assert cell["visible_before_or_at_issue"] == 0
    assert cell["nominal_due_on_or_before_issue_day"] == 3


def test_invalidated_outcomes_and_pending_do_not_inflate_accuracy():
    invalidated = _cell()
    invalidated["settlement_status"] = "invalidated_contract_mismatch"
    invalidated["invalidation"] = {"reason": "source mismatch"}
    pending = _cell()
    pending.update(settlement_status="pending", outcome=None)
    report = _report([_cell(), invalidated, pending])
    assert report["cells"][0]["issued"] == 3
    assert report["cells"][0]["frozen_contract_scores"]["n"] == 1
    assert report["settlement_statuses"]["pending"] == 1


def test_all_neutral_hits_are_compared_with_neutral_baseline_and_not_abstention():
    report = _report([_cell(), _cell()])
    score = report["cells"][0]["frozen_contract_scores"]
    assert score["accuracy"] == score["neutral_baseline_accuracy"] == 1.0
    assert score["balanced_accuracy"] is None
    assert score["non_neutral_calls"] == 0
    assert score["unique_actual_observations"] == 1
    assert score["selective"][0]["coverage_of_scored_cohort"] == 1.0


def test_selective_accuracy_always_exposes_coverage_and_preserves_input():
    wrong = _cell(confidence=0.4)
    wrong["outcome"]["actual_direction"] = "up"
    cells = [_cell(confidence=0.6), wrong]
    original = deepcopy(cells)
    score = _report(cells)["cells"][0]["frozen_contract_scores"]
    selected = next(row for row in score["selective"] if row["threshold"] == 0.5)
    assert selected["accuracy"] == 1.0
    assert selected["coverage_of_scored_cohort"] == 0.5
    assert score["accuracy"] == 0.5
    assert cells == original


def test_ambiguous_issue_time_fails_instead_of_claiming_future():
    cell = _cell()
    cell["forecast"]["as_of_time"] = "2026-09-26T08:00:00"
    with pytest.raises(ValueError, match="timezone_aware"):
        _report([cell])

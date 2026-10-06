"""Explicit internal composition for terminal Experience settlement.

This module deliberately does not choose a prediction revision, evaluation
snapshot, cutoff, or source status.  A separately authorized caller must
provide those inputs before any candidate can be loaded or persisted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .experience_business_calendar import expected_china_workdays_after
from .experience_candidate_loader import load_terminal_experience_candidates
from .experience_settlement import plan_due_experience_settlements
from .experience_settlement_selection import select_terminal_experience_inputs
from .experience_settlement_storage import save_experience_settlement_plan

RESULT_SCHEMA_VERSION = "experience-terminal-settlement-execution.v1"
SELECTED_RESULT_SCHEMA_VERSION = "experience-terminal-settlement-selected-execution.v1"


def settle_terminal_experience_candidates(
    *,
    prediction_revision_id: str,
    evaluation_snapshot_id: str,
    evaluation_as_of_time: str,
    expected_observation_dates: Sequence[str],
    series_statuses: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Load explicit verified inputs, plan deterministically, then persist the plan.

    Errors deliberately propagate unchanged from the loader, planner, and
    persistence boundary so callers retain the existing fail-closed contracts.
    """

    loaded = load_terminal_experience_candidates(
        prediction_revision_id=prediction_revision_id,
        evaluation_snapshot_id=evaluation_snapshot_id,
        evaluation_as_of_time=evaluation_as_of_time,
        expected_observation_dates=expected_observation_dates,
        series_statuses=series_statuses,
    )
    evaluation_as_of = loaded["evaluation_as_of_time"]
    plan = plan_due_experience_settlements(
        candidates=loaded["candidates"],
        evaluation_as_of=evaluation_as_of,
    )
    persistence = save_experience_settlement_plan(plan)
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "prediction_revision_id": prediction_revision_id,
        "evaluation_snapshot_id": evaluation_snapshot_id,
        "evaluation_as_of_time": evaluation_as_of,
        "candidate_count": len(loaded["candidates"]),
        "plan": plan,
        "persistence": persistence,
    }


def settle_selected_terminal_experience_candidates(
    *,
    evaluation_as_of_time: str,
) -> dict[str, Any]:
    """Select point-in-time inputs, or return a zero-write blocked outcome.

    The calendar uses the frozen weekday-only v1 rule; it is derived from the
    immutable selected prediction timestamp rather than the scheduler's clock.
    """

    selection = select_terminal_experience_inputs(evaluation_as_of_time=evaluation_as_of_time)
    if selection["status"] == "blocked":
        return {
            "schema_version": SELECTED_RESULT_SCHEMA_VERSION,
            "status": "blocked",
            "reason_codes": selection["reason_codes"],
            "selection": selection,
            "execution": None,
        }
    execution = settle_terminal_experience_candidates(
        prediction_revision_id=selection["prediction_revision_id"],
        evaluation_snapshot_id=selection["evaluation_snapshot_id"],
        evaluation_as_of_time=selection["evaluation_as_of_time"],
        expected_observation_dates=expected_china_workdays_after(
            prediction_as_of_time=selection["prediction_as_of_time"]
        ),
    )
    return {
        "schema_version": SELECTED_RESULT_SCHEMA_VERSION,
        "status": "executed",
        "reason_codes": [],
        "selection": selection,
        "execution": execution,
    }

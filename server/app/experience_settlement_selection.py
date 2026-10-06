"""Deterministic read-only input selection for terminal Experience settlement."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import formal_prediction_batches, storage

SELECTION_SCHEMA_VERSION = "experience-terminal-settlement-selection.v1"


def select_terminal_experience_inputs(*, evaluation_as_of_time: str) -> dict[str, Any]:
    """Select the newest verified inputs that were available by one cutoff.

    This boundary never reads the system clock, upgrades formal eligibility, or
    writes. Missing candidate data is an explicit blocked result so callers can
    prove that no Experience persistence was attempted.
    """

    cutoff = _canonical_time(evaluation_as_of_time, "experience_evaluation_as_of_invalid")
    predictions = formal_prediction_batches.list_verified_formal_prediction_batches(
        as_of_time=cutoff,
        limit=100,
    )
    eligible_predictions = [
        row
        for row in predictions
        if _available_at_or_before(row, cutoff, "experience_prediction_selection_invalid")
    ]
    if not eligible_predictions:
        return _blocked(cutoff, "formal_prediction_before_cutoff_missing")
    prediction = max(
        eligible_predictions,
        key=lambda row: (
            _canonical_time(row["as_of_time"], "experience_prediction_selection_invalid"),
            _canonical_time(row["persisted_at"], "experience_prediction_selection_invalid"),
            str(row["revision_id"]),
        ),
    )

    snapshots = storage.list_data_snapshots(limit=5_000)
    eligible_snapshots = [snapshot for snapshot in snapshots if _snapshot_available_at_or_before(snapshot, cutoff)]
    if not eligible_snapshots:
        return _blocked(cutoff, "evaluation_snapshot_before_cutoff_missing")
    snapshot = max(
        eligible_snapshots,
        key=lambda item: (
            _canonical_time(item["metadata"]["as_of_time"], "experience_evaluation_snapshot_invalid"),
            _canonical_time(item["created_at"], "experience_evaluation_snapshot_invalid"),
            str(item["snapshot_id"]),
        ),
    )
    return {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "status": "selected",
        "reason_codes": [],
        "evaluation_as_of_time": cutoff,
        "prediction_revision_id": str(prediction["revision_id"]),
        "prediction_as_of_time": _canonical_time(
            prediction["as_of_time"], "experience_prediction_selection_invalid"
        ),
        "evaluation_snapshot_id": str(snapshot["snapshot_id"]),
    }


def _blocked(cutoff: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": SELECTION_SCHEMA_VERSION,
        "status": "blocked",
        "reason_codes": [reason],
        "evaluation_as_of_time": cutoff,
        "prediction_revision_id": None,
        "prediction_as_of_time": None,
        "evaluation_snapshot_id": None,
    }


def _available_at_or_before(row: Any, cutoff: str, error: str) -> bool:
    if not isinstance(row, dict):
        raise ValueError(error)
    revision_id = row.get("revision_id")
    if type(revision_id) is not str or not revision_id:
        raise ValueError(error)
    return (
        _canonical_time(row.get("as_of_time"), error) <= cutoff
        and _canonical_time(row.get("persisted_at"), error) <= cutoff
    )


def _snapshot_available_at_or_before(snapshot: Any, cutoff: str) -> bool:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("metadata"), dict):
        raise ValueError("experience_evaluation_snapshot_invalid")
    snapshot_id = snapshot.get("snapshot_id")
    if type(snapshot_id) is not str or not snapshot_id:
        raise ValueError("experience_evaluation_snapshot_invalid")
    return (
        _canonical_time(snapshot["metadata"].get("as_of_time"), "experience_evaluation_snapshot_invalid") <= cutoff
        and _canonical_time(snapshot.get("created_at"), "experience_evaluation_snapshot_invalid") <= cutoff
    )


def _canonical_time(value: Any, error: str) -> str:
    if type(value) is not str:
        raise ValueError(error)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(error) from None
    if parsed.tzinfo is None:
        raise ValueError(error)
    return parsed.isoformat()

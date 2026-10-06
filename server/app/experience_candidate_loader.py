"""Verified, read-only inputs for terminal Experience settlement candidates."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from . import experience_cards, formal_eligibility_proofs, formal_prediction_batches, storage
from .experience_candidate_adapter import assemble_terminal_experience_candidates


class ExperienceCandidateLoaderError(ValueError):
    """Raised when an explicitly selected persisted input is unsafe to use."""


def load_terminal_experience_candidates(
    *,
    prediction_revision_id: str,
    evaluation_snapshot_id: str,
    evaluation_as_of_time: str,
    expected_observation_dates: Sequence[str],
    series_statuses: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Re-audit explicit stored inputs, then delegate to the pure adapter.

    This function makes no choice of a revision or snapshot, and does no
    persistence.  Its inputs therefore remain suitable for a separately
    controlled scheduler or settlement writer.
    """

    _opaque_id(prediction_revision_id, "experience_prediction_revision_invalid")
    _opaque_id(evaluation_snapshot_id, "experience_evaluation_snapshot_invalid")
    cutoff = _canonical_time(evaluation_as_of_time, "experience_evaluation_as_of_invalid")
    try:
        prediction_revision = formal_prediction_batches.load_verified_formal_prediction_revision(
            revision_id=prediction_revision_id,
            as_of_time=cutoff,
        )
        prediction_payload = prediction_revision["payload"]
        persisted_statuses = formal_eligibility_proofs.load_verified_formal_series_statuses(
            assessment_id=prediction_revision["assessment_id"],
            as_of_time=cutoff,
        )
    except formal_prediction_batches.FormalPredictionBatchError as exc:
        raise ExperienceCandidateLoaderError(str(exc)) from None
    except formal_eligibility_proofs.FormalEligibilityProofError as exc:
        raise ExperienceCandidateLoaderError(str(exc)) from None
    if series_statuses is not None and dict(series_statuses) != persisted_statuses:
        raise ExperienceCandidateLoaderError("experience_series_status_override_rejected")
    snapshot = storage.get_data_snapshot(evaluation_snapshot_id)
    if snapshot is None:
        raise ExperienceCandidateLoaderError("experience_evaluation_snapshot_missing")
    snapshot_as_of = snapshot.get("metadata", {}).get("as_of_time")
    if _canonical_time(snapshot_as_of, "experience_evaluation_snapshot_invalid") > cutoff:
        raise ExperienceCandidateLoaderError("experience_evaluation_snapshot_after_as_of")
    try:
        assembled = assemble_terminal_experience_candidates(
            prediction_payload=prediction_payload,
            evaluation_snapshot=snapshot,
            expected_observation_dates=expected_observation_dates,
            series_statuses=persisted_statuses,
            previous_revisions=None,
        )
    except ValueError as exc:
        raise ExperienceCandidateLoaderError(str(exc)) from None
    for candidate in assembled["candidates"]:
        bundle = candidate["prediction_bundle"]
        candidate["previous_revision"] = storage.get_experience_card_head(
            experience_cards.experience_card_id_for_bundle(bundle)
        )
    return {
        **assembled,
        "schema_version": "experience-candidate-loader.v1",
        "prediction_revision_id": prediction_revision_id,
        "evaluation_snapshot_id": evaluation_snapshot_id,
        "evaluation_as_of_time": cutoff,
    }


def _opaque_id(value: Any, error: str) -> None:
    if type(value) is not str or not value or len(value) > 160:
        raise ExperienceCandidateLoaderError(error)


def _canonical_time(value: Any, error: str) -> str:
    if type(value) is not str:
        raise ExperienceCandidateLoaderError(error)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ExperienceCandidateLoaderError(error) from None
    if parsed.tzinfo is None:
        raise ExperienceCandidateLoaderError(error)
    return parsed.isoformat()

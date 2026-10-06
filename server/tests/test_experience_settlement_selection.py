from __future__ import annotations

import pytest

from app import experience_settlement_selection as selection


def _prediction(revision_id: str, *, as_of: str, persisted_at: str) -> dict[str, str]:
    return {"revision_id": revision_id, "as_of_time": as_of, "persisted_at": persisted_at}


def _snapshot(snapshot_id: str, *, as_of: str, created_at: str) -> dict[str, object]:
    return {
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "metadata": {"as_of_time": as_of},
    }


def test_selection_chooses_latest_verified_inputs_available_at_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        selection.formal_prediction_batches,
        "list_verified_formal_prediction_batches",
        lambda **_: [
            _prediction("revision-old", as_of="2026-08-06T08:10:00+08:00", persisted_at="2026-08-06T08:11:00+08:00"),
            _prediction("revision-new", as_of="2026-08-06T08:20:00+08:00", persisted_at="2026-08-06T08:21:00+08:00"),
            _prediction("revision-future", as_of="2026-08-06T08:30:00+08:00", persisted_at="2026-08-06T08:22:00+08:00"),
        ],
    )
    monkeypatch.setattr(
        selection.storage,
        "list_data_snapshots",
        lambda **_: [
            _snapshot("snapshot-old", as_of="2026-08-06T08:15:00+08:00", created_at="2026-08-06T08:16:00+08:00"),
            _snapshot("snapshot-new", as_of="2026-08-06T08:20:00+08:00", created_at="2026-08-06T08:21:00+08:00"),
            _snapshot("snapshot-future", as_of="2026-08-06T08:20:00+08:00", created_at="2026-08-06T08:30:00+08:00"),
        ],
    )

    result = selection.select_terminal_experience_inputs(evaluation_as_of_time="2026-08-06T08:25:00+08:00")

    assert result == {
        "schema_version": "experience-terminal-settlement-selection.v1",
        "status": "selected",
        "reason_codes": [],
        "evaluation_as_of_time": "2026-08-06T08:25:00+08:00",
        "prediction_revision_id": "revision-new",
        "prediction_as_of_time": "2026-08-06T08:20:00+08:00",
        "evaluation_snapshot_id": "snapshot-new",
    }


@pytest.mark.parametrize(
    ("predictions", "snapshots", "reason"),
    [
        (
            [],
            [_snapshot("snapshot-1", as_of="2026-08-06T08:20:00+08:00", created_at="2026-08-06T08:21:00+08:00")],
            "formal_prediction_before_cutoff_missing",
        ),
        (
            [_prediction("revision-1", as_of="2026-08-06T08:20:00+08:00", persisted_at="2026-08-06T08:21:00+08:00")],
            [],
            "evaluation_snapshot_before_cutoff_missing",
        ),
    ],
)
def test_selection_returns_blocked_zero_write_input_when_required_input_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    predictions: list[dict[str, str]],
    snapshots: list[dict[str, object]],
    reason: str,
) -> None:
    monkeypatch.setattr(
        selection.formal_prediction_batches,
        "list_verified_formal_prediction_batches",
        lambda **_: predictions,
    )
    monkeypatch.setattr(selection.storage, "list_data_snapshots", lambda **_: snapshots)

    result = selection.select_terminal_experience_inputs(evaluation_as_of_time="2026-08-06T08:25:00+08:00")

    assert result["status"] == "blocked"
    assert result["reason_codes"] == [reason]
    assert result["prediction_revision_id"] is None
    assert result["prediction_as_of_time"] is None
    assert result["evaluation_snapshot_id"] is None


def test_selection_rejects_malformed_persisted_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        selection.formal_prediction_batches,
        "list_verified_formal_prediction_batches",
        lambda **_: [_prediction("revision-1", as_of="not-a-time", persisted_at="2026-08-06T08:21:00+08:00")],
    )
    monkeypatch.setattr(selection.storage, "list_data_snapshots", lambda **_: [])

    with pytest.raises(ValueError, match="experience_prediction_selection_invalid"):
        selection.select_terminal_experience_inputs(evaluation_as_of_time="2026-08-06T08:25:00+08:00")

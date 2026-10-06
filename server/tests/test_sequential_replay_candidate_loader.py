from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from test_experience_card_persistence import _card
from test_formal_prediction_batches import _assessment, _payload

from app import formal_prediction_batches, storage
from app import sequential_replay_candidate_loader as loader
from app.sequential_replay import plan_sequential_replay
from app.settings import settings

CHECKPOINT = "2026-07-01T09:00:00+08:00"


@pytest.fixture()
def isolated_db(tmp_path: Path):
    original = settings.sqlite_path
    path = tmp_path / "replay-candidates.db"
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.discard(path)
    try:
        yield path
    finally:
        storage._MIGRATED_PATHS.discard(path)
        object.__setattr__(settings, "sqlite_path", original)


def _seed(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reconstructed: bool = False,
    formal_unscorable: bool = False,
) -> None:
    assessment, _ = _assessment(monkeypatch)
    payload = _payload()
    if formal_unscorable:
        terminal = next(
            cell
            for cell in payload["cells"]
            if cell["node_id"] == "poy_dty_upstream_cost_pressure" and cell["horizon_days"] == 1
        )
        poy = next(item for item in terminal["subtarget_results"] if item["target"] == "poy")
        poy["scoreability"] = "unscorable"
        poy["missing_series_ids"] = ["poy.formal.missing"]
    formal_prediction_batches.save_formal_prediction_batch(
        assessment_id=assessment["assessment_id"],
        payload=payload,
    )
    card = _card(scoreability="unscorable" if reconstructed else "scorable")
    card.update(
        {
            "prediction_batch_id": "formal-batch-1",
            "prediction_revision_id": "revision-1",
            "data_snapshot_id": "snapshot-formal-1",
            "visibility_mode": "reconstructed" if reconstructed else "strict_as_of",
        }
    )
    monkeypatch.setattr(storage, "_now", lambda: "2026-07-01T08:56:00+08:00")
    storage.save_experience_card_revision(card)


def _formal_selector(
    *,
    product: str = "poy",
    node_id: str = "poy_dty_upstream_cost_pressure",
    horizon: int = 1,
) -> dict[str, Any]:
    return {
        "kind": "formal_prediction_revision",
        "revision_id": "revision-1",
        "data_snapshot_id": "snapshot-formal-1",
        "product": product,
        "node_id": node_id,
        "horizon": horizon,
    }


def _experience_selector(*, product: str = "poy", horizon: int = 1) -> dict[str, Any]:
    return {
        "kind": "experience_revision",
        "revision_id": "ecr-d1",
        "prediction_revision_id": "revision-1",
        "data_snapshot_id": "snapshot-formal-1",
        "product": product,
        "node_id": "poy_dty_upstream_cost_pressure",
        "horizon": horizon,
        "calendar_id": "cn-business-days",
        "calendar_version": "2026.v1",
    }


def _digest(path: Path) -> str:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection, connection:
        payload = "\n".join(connection.iterdump()).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _zero_write(path: Path, operation: Callable[[], Any]) -> Any:
    before = _digest(path)
    try:
        return operation()
    finally:
        assert _digest(path) == before


@pytest.mark.parametrize("selector", [_formal_selector(), _experience_selector()])
def test_missing_database_is_not_created_by_any_verified_reader(
    isolated_db: Path,
    selector: dict[str, Any],
) -> None:
    assert not isolated_db.exists()

    with pytest.raises(loader.SequentialReplayCandidateLoaderError) as exc_info:
        loader.load_sequential_replay_checkpoint(as_of=CHECKPOINT, selectors=[selector])

    expected = (
        "formal_prediction_storage_unavailable"
        if selector["kind"] == "formal_prediction_revision"
        else "experience_revision_storage_unavailable"
    )
    assert exc_info.value.code == expected
    assert not isolated_db.exists()
    assert not Path(f"{isolated_db}-wal").exists()
    assert not Path(f"{isolated_db}-shm").exists()


def test_empty_database_is_not_initialized_or_migrated(
    isolated_db: Path,
) -> None:
    isolated_db.touch(mode=0o600)
    before = isolated_db.stat()

    with pytest.raises(loader.SequentialReplayCandidateLoaderError) as exc_info:
        loader.load_sequential_replay_checkpoint(as_of=CHECKPOINT, selectors=[_experience_selector()])

    after = isolated_db.stat()
    assert exc_info.value.code == "experience_revision_storage_unavailable"
    assert after.st_size == before.st_size == 0
    assert after.st_mtime_ns == before.st_mtime_ns
    assert not Path(f"{isolated_db}-wal").exists()
    assert not Path(f"{isolated_db}-shm").exists()


def test_old_schema_is_not_migrated_or_journal_switched(
    isolated_db: Path,
) -> None:
    with closing(sqlite3.connect(isolated_db)) as connection, connection:
        connection.execute("CREATE TABLE legacy_marker(value TEXT NOT NULL)")
        connection.execute("INSERT INTO legacy_marker VALUES ('unchanged')")
    before_bytes = isolated_db.read_bytes()
    before = isolated_db.stat()

    with pytest.raises(loader.SequentialReplayCandidateLoaderError) as exc_info:
        loader.load_sequential_replay_checkpoint(as_of=CHECKPOINT, selectors=[_formal_selector()])

    after = isolated_db.stat()
    assert exc_info.value.code == "formal_prediction_storage_unavailable"
    assert isolated_db.read_bytes() == before_bytes
    assert after.st_size == before.st_size
    assert after.st_mtime_ns == before.st_mtime_ns
    assert not Path(f"{isolated_db}-wal").exists()
    assert not Path(f"{isolated_db}-shm").exists()


def test_exact_formal_and_experience_revisions_hand_off_directly_to_the_pure_planner(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(monkeypatch)
    selectors = [
        _formal_selector(product="dty", node_id="pta", horizon=7),
        _experience_selector(),
        _formal_selector(),
    ]

    loaded = _zero_write(
        isolated_db,
        lambda: loader.load_sequential_replay_checkpoint(as_of=CHECKPOINT, selectors=selectors),
    )

    assert loaded["diagnostics"] == []
    assert [(item["product"], item["node_id"], item["kind"]) for item in loaded["checkpoint"]["items"]] == [
        ("dty", "pta", "prediction"),
        ("poy", "poy_dty_upstream_cost_pressure", "prediction"),
        ("poy", "poy_dty_upstream_cost_pressure", "settlement"),
    ]
    terminal = loaded["checkpoint"]["items"][1]
    assert terminal["horizon"] == 1
    assert terminal["metric_values"]["confidence"] == 0.6
    assert terminal["evidence_status"] == "point_in_time"
    plan = plan_sequential_replay(
        checkpoints=[loaded["checkpoint"]],
        policy={
            "policy_version": "sequential-replay-policy.test-v1",
            "run_mode": "dry_run",
            "reconstructed_evidence_handling": "exclude_unscorable",
            "expected_checkpoint_dates": ["2026-07-01"],
        },
    )
    assert plan["action_count"] == 3


def test_explicit_product_horizon_snapshot_and_calendar_mismatches_are_rejected_without_writes(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(monkeypatch)
    mismatches = [
        {**_experience_selector(), "product": "dty"},
        {**_experience_selector(), "horizon": 7},
        {**_experience_selector(), "data_snapshot_id": "other-snapshot"},
        {**_experience_selector(), "calendar_version": "other-calendar"},
    ]
    expected = [
        "replay_selector_product_mismatch",
        "replay_selector_horizon_mismatch",
        "replay_selector_data_snapshot_id_mismatch",
        "replay_selector_calendar_version_mismatch",
    ]
    for selector, error in zip(mismatches, expected, strict=True):
        with pytest.raises(loader.SequentialReplayCandidateLoaderError, match=error):
            _zero_write(
                isolated_db,
                lambda selector=selector: loader.load_sequential_replay_checkpoint(
                    as_of=CHECKPOINT,
                    selectors=[selector],
                ),
            )


@pytest.mark.parametrize(
    "checkpoint, error",
    [
        ("2026-07-01T08:54:59+08:00", "formal_prediction_revision_after_as_of"),
        ("2026-07-01T08:55:30+08:00", "experience_revision_after_as_of"),
    ],
)
def test_future_visible_records_are_rejected_before_any_items_are_returned(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    checkpoint: str,
    error: str,
) -> None:
    _seed(monkeypatch)
    selector = _formal_selector() if "formal" in error else _experience_selector()
    with pytest.raises(loader.SequentialReplayCandidateLoaderError, match=error):
        _zero_write(
            isolated_db,
            lambda: loader.load_sequential_replay_checkpoint(as_of=checkpoint, selectors=[selector]),
        )


@pytest.mark.parametrize(
    ("target", "error"),
    [
        ("parent", "formal_prediction_audit_failed"),
        ("cell", "formal_prediction_audit_failed"),
        ("proof", "formal_prediction_audit_failed"),
        ("payload", "formal_prediction_audit_failed"),
        ("experience", "experience_revision_audit_failed"),
    ],
)
def test_existing_audits_reject_tampered_rows_without_loader_side_reimplementation(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    error: str,
) -> None:
    _seed(monkeypatch)
    with closing(sqlite3.connect(isolated_db)) as connection, connection:
        if target == "parent":
            connection.execute("DROP TRIGGER trg_formal_prediction_batch_revisions_no_update")
            connection.execute(
                """
                UPDATE formal_prediction_batch_revisions
                SET prediction_batch_id='forged' WHERE revision_id='revision-1'
                """
            )
        elif target == "cell":
            connection.execute("DROP TRIGGER trg_formal_prediction_cells_no_update")
            connection.execute(
                """
                UPDATE formal_prediction_cells SET node_id='forged'
                WHERE rowid=(SELECT rowid FROM formal_prediction_cells WHERE revision_id='revision-1' LIMIT 1)
                """
            )
        elif target == "proof":
            connection.execute("DROP TRIGGER trg_formal_prediction_batch_proofs_no_update")
            connection.execute(
                """
                UPDATE formal_prediction_batch_proofs SET proof_sha256=?
                WHERE revision_id='revision-1' AND horizon_days=1
                """,
                ("0" * 64,),
            )
        elif target == "payload":
            connection.execute("DROP TRIGGER trg_formal_prediction_batch_revisions_no_update")
            connection.execute(
                """
                UPDATE formal_prediction_batch_revisions
                SET payload=replace(payload, 'formal-batch-1', 'formal-batch-x')
                WHERE revision_id='revision-1'
                """
            )
        else:
            connection.execute("DROP TRIGGER trg_experience_card_no_update")
            connection.execute(
                """
                UPDATE experience_card_revisions
                SET payload=replace(payload, 'benchmark.target', 'benchmark.forged')
                WHERE revision_id='ecr-d1'
                """
            )
    selector = _experience_selector() if target == "experience" else _formal_selector()
    with pytest.raises(loader.SequentialReplayCandidateLoaderError) as exc_info:
        _zero_write(
            isolated_db,
            lambda: loader.load_sequential_replay_checkpoint(as_of=CHECKPOINT, selectors=[selector]),
        )
    assert exc_info.value.code == error


def test_reconstructed_experience_is_preserved_as_unscorable(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(monkeypatch, reconstructed=True)

    loaded = _zero_write(
        isolated_db,
        lambda: loader.load_sequential_replay_checkpoint(
            as_of=CHECKPOINT,
            selectors=[_experience_selector()],
        ),
    )

    item = loaded["checkpoint"]["items"][0]
    assert item["evidence_status"] == "reconstructed"
    assert item["scoreability"] == "unscorable"
    assert item["exclusion_reasons"] == ["reconstructed_evidence", "target_series_status:blocked"]


def test_formal_unscorable_status_and_audited_missing_evidence_are_preserved(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(monkeypatch, formal_unscorable=True)

    loaded = _zero_write(
        isolated_db,
        lambda: loader.load_sequential_replay_checkpoint(as_of=CHECKPOINT, selectors=[_formal_selector()]),
    )

    item = loaded["checkpoint"]["items"][0]
    assert item["scoreability"] == "unscorable"
    assert item["exclusion_reasons"] == ["formal_prediction_unscorable", "missing_series_evidence"]


def test_missing_duplicate_and_unsupported_selectors_have_stable_bounded_outcomes(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(monkeypatch)
    missing = {**_formal_selector(), "revision_id": "missing-revision"}
    with pytest.raises(loader.SequentialReplayCandidateLoaderError, match="formal_prediction_revision_missing"):
        _zero_write(
            isolated_db,
            lambda: loader.load_sequential_replay_checkpoint(as_of=CHECKPOINT, selectors=[missing]),
        )
    with pytest.raises(loader.SequentialReplayCandidateLoaderError, match="replay_selector_duplicate"):
        _zero_write(
            isolated_db,
            lambda: loader.load_sequential_replay_checkpoint(
                as_of=CHECKPOINT,
                selectors=[_formal_selector(), _formal_selector()],
            ),
        )

    unsupported = _zero_write(
        isolated_db,
        lambda: loader.load_sequential_replay_checkpoint(
            as_of=CHECKPOINT,
            selectors=[{"kind": "shadow_revision", "revision_id": "shadow-1"}],
        ),
    )
    assert unsupported["checkpoint"]["items"] == []
    assert unsupported["diagnostics"] == [
        {
            "selector_sha256": unsupported["diagnostics"][0]["selector_sha256"],
            "kind": "shadow_revision",
            "status": "unsupported",
            "reason_code": "unsupported_persisted_kind",
        }
    ]
    assert len(unsupported["diagnostics"][0]["selector_sha256"]) == 64


def test_missing_experience_revision_and_bound_snapshot_have_stable_failures(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(monkeypatch)
    missing_card = {**_experience_selector(), "revision_id": "missing-card"}
    with pytest.raises(loader.SequentialReplayCandidateLoaderError, match="experience_revision_missing"):
        _zero_write(
            isolated_db,
            lambda: loader.load_sequential_replay_checkpoint(as_of=CHECKPOINT, selectors=[missing_card]),
        )

    with closing(sqlite3.connect(isolated_db)) as connection, connection:
        connection.execute("DELETE FROM data_snapshots WHERE snapshot_id='snapshot-formal-1'")
    with pytest.raises(loader.SequentialReplayCandidateLoaderError, match="formal_snapshot_missing"):
        _zero_write(
            isolated_db,
            lambda: loader.load_sequential_replay_checkpoint(
                as_of=CHECKPOINT,
                selectors=[_formal_selector()],
            ),
        )


def test_selector_order_does_not_change_checkpoint_or_diagnostics(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(monkeypatch)
    selectors = [
        _formal_selector(),
        _experience_selector(),
        {"kind": "shadow_revision", "revision_id": "shadow-1"},
    ]
    forward = _zero_write(
        isolated_db,
        lambda: loader.load_sequential_replay_checkpoint(as_of=CHECKPOINT, selectors=selectors),
    )
    reverse = _zero_write(
        isolated_db,
        lambda: loader.load_sequential_replay_checkpoint(as_of=CHECKPOINT, selectors=list(reversed(selectors))),
    )
    assert forward == reverse


def test_checkpoint_visibility_compares_instants_across_rfc3339_offsets(
    isolated_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(monkeypatch)

    loaded = _zero_write(
        isolated_db,
        lambda: loader.load_sequential_replay_checkpoint(
            as_of="2026-07-01T01:00:00+00:00",
            selectors=[_formal_selector(), _experience_selector()],
        ),
    )

    assert len(loaded["checkpoint"]["items"]) == 2

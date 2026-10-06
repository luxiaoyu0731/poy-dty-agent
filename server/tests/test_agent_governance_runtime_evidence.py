from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app import agent_governance_runtime_evidence as runtime_evidence
from app import storage
from app.agent_governance_scheduler import run_daily_governance_window
from app.agent_production_policy import daily_governance_window
from app.settings import settings


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Path:
    original = settings.sqlite_path
    path = tmp_path / "runtime-evidence.db"
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.discard(path)
    try:
        yield path
    finally:
        storage._MIGRATED_PATHS.discard(path)
        object.__setattr__(settings, "sqlite_path", original)


def _seed_report() -> dict:
    evaluated_at = datetime(2026, 8, 6, 0, 10, tzinfo=UTC)
    window = daily_governance_window(evaluated_at)
    return run_daily_governance_window(
        now=evaluated_at,
        collector=lambda **_: {
            "window": window,
            "policy_applied": True,
            "production_ready": False,
            "failed_policy_checks": ["sample_count"],
        },
    )


def _capture_in_subprocess(database: Path, root: Path) -> dict:
    worker = """
import json
from app.agent_governance_runtime_evidence import capture_agent_governance_runtime_checkpoint
result = capture_agent_governance_runtime_checkpoint(
    window_end='2026-08-06T08:10:00+08:00',
    expected_window_start='2026-08-05T08:10:00+08:00',
)
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
"""
    environment = {
        **os.environ,
        "DG01_TEST_DB_ROOT": str(root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
        "SQLITE_PATH": str(database),
        "TMPDIR": str(root),
    }
    completed = subprocess.run(
        [sys.executable, "-c", worker],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return json.loads(completed.stdout)


def _capture_after_process_start_boundary(database: Path, root: Path, before: dict) -> dict:
    """Wait out ps(1)'s whole-second process-start timestamp precision."""

    deadline = time.monotonic() + 5
    before_observed = datetime.fromisoformat(before["process_identity"]["observed_at"])
    while True:
        after = _capture_in_subprocess(database, root)
        after_started = datetime.fromisoformat(after["process_identity"]["started_at"])
        if after_started >= before_observed:
            return after
        if time.monotonic() >= deadline:
            pytest.fail("process start timestamp did not advance past the before checkpoint")
        time.sleep(0.25)


def test_restart_evidence_derives_persistence_and_queue_facts(
    isolated_db: Path, tmp_path: Path,
) -> None:
    _seed_report()
    before = _capture_in_subprocess(isolated_db, tmp_path)
    after = _capture_after_process_start_boundary(isolated_db, tmp_path, before)
    result = runtime_evidence.compare_agent_governance_restart_snapshots(
        before=before,
        after=after,
        restart_boundary={
            "restart_id": "restart-1",
            "requested_at": before["process_identity"]["observed_at"],
            "completed_at": after["process_identity"]["started_at"],
        },
    )
    assert result["result"] == "unsigned_snapshot_comparison"
    assert result["checkpoint_snapshot_equal"] is True
    assert result["review_queue_snapshots_equal"] is True
    assert result["persistence_facts"] == {
        "review_record_count": 0,
        "review_records_sha256": hashlib.sha256(b"[]").hexdigest(),
        "window_row_count": 1,
    }
    assert result["production_service_identity_verified"] is False
    assert result["production_restart_verified"] is False
    assert result["remaining_gate"] == "signed_service_event_and_append_only_review_ledger_required"
    assert len(result["evidence_sha256"]) == 64


def test_capture_collects_current_process_and_rejects_caller_identity(
    isolated_db: Path,
) -> None:
    report = _seed_report()
    checkpoint = runtime_evidence.capture_agent_governance_runtime_checkpoint(
        window_end=report["window"]["end"],
    )
    identity = checkpoint["process_identity"]
    assert identity["pid"] == os.getpid()
    assert identity["ppid"] == os.getppid()
    assert identity["executable"] == str(Path(sys.executable).resolve())
    assert len(identity["executable_sha256"]) == 64
    assert len(identity["process_observation_sha256"]) == 64
    with pytest.raises(TypeError):
        runtime_evidence.capture_agent_governance_runtime_checkpoint(
            window_end=report["window"]["end"],
            process_identity={"pid": 999},  # type: ignore[call-arg]
        )


def test_missing_window_and_reader_failures_have_stable_codes(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_report()
    with pytest.raises(runtime_evidence.AgentGovernanceRuntimeEvidenceError, match="window_unavailable"):
        runtime_evidence.capture_agent_governance_runtime_checkpoint(
            window_end="2026-08-07T08:10:00+08:00",
        )
    monkeypatch.setattr(
        runtime_evidence,
        "get_agent_governance_runtime_persistence_snapshot",
        lambda _: (_ for _ in ()).throw(sqlite3.IntegrityError("tamper")),
    )
    with pytest.raises(runtime_evidence.AgentGovernanceRuntimeEvidenceError, match="persistence_mismatch"):
        runtime_evidence.capture_agent_governance_runtime_checkpoint(
            window_end="2026-08-06T08:10:00+08:00",
        )


def test_restart_rejects_same_process_changed_persistence_and_queue_growth(
    isolated_db: Path, tmp_path: Path,
) -> None:
    _seed_report()
    before = _capture_in_subprocess(isolated_db, tmp_path)
    boundary = {
        "restart_id": "restart-1",
        "requested_at": before["process_identity"]["observed_at"],
        "completed_at": before["process_identity"]["observed_at"],
    }
    with pytest.raises(runtime_evidence.AgentGovernanceRuntimeEvidenceError, match="identity_mismatch"):
        runtime_evidence.compare_agent_governance_restart_snapshots(
            before=before, after=before, restart_boundary=boundary,
        )
    after = _capture_after_process_start_boundary(isolated_db, tmp_path, before)
    boundary["completed_at"] = after["process_identity"]["started_at"]
    changed = json.loads(json.dumps(after))
    changed["persistence_facts"]["review_record_count"] = 1
    changed["checkpoint_sha256"] = runtime_evidence._digest({
        key: value for key, value in changed.items() if key != "checkpoint_sha256"
    })
    with pytest.raises(runtime_evidence.AgentGovernanceRuntimeEvidenceError, match="persistence_mismatch"):
        runtime_evidence.compare_agent_governance_restart_snapshots(
            before=before, after=changed, restart_boundary=boundary,
        )


def test_unsigned_checkpoint_cannot_claim_nonexistent_executable(
    isolated_db: Path, tmp_path: Path,
) -> None:
    _seed_report()
    before = _capture_in_subprocess(isolated_db, tmp_path)
    forged = json.loads(json.dumps(before))
    forged["process_identity"]["executable"] = "/does/not/exist"
    forged["checkpoint_sha256"] = runtime_evidence._digest({
        key: value for key, value in forged.items() if key != "checkpoint_sha256"
    })

    with pytest.raises(runtime_evidence.AgentGovernanceRuntimeEvidenceError, match="identity_mismatch"):
        runtime_evidence.compare_agent_governance_restart_snapshots(
            before=before,
            after=forged,
            restart_boundary={
                "restart_id": "restart-1",
                "requested_at": before["process_identity"]["observed_at"],
                "completed_at": before["process_identity"]["observed_at"],
            },
        )


def test_two_interpreters_read_same_report_without_database_changes(
    isolated_db: Path, tmp_path: Path,
) -> None:
    _seed_report()
    database_hash_before = hashlib.sha256(isolated_db.read_bytes()).hexdigest()
    checkpoints = [
        _capture_in_subprocess(isolated_db, tmp_path),
        _capture_in_subprocess(isolated_db, tmp_path),
    ]
    assert checkpoints[0]["report_identity"] == checkpoints[1]["report_identity"]
    assert checkpoints[0]["persistence_facts"] == checkpoints[1]["persistence_facts"]
    assert checkpoints[0]["process_identity"]["pid"] != checkpoints[1]["process_identity"]["pid"]
    assert hashlib.sha256(isolated_db.read_bytes()).hexdigest() == database_hash_before


def test_readonly_connection_never_creates_or_migrates(tmp_path: Path) -> None:
    original = settings.sqlite_path
    missing = tmp_path / "missing" / "agent.db"
    object.__setattr__(settings, "sqlite_path", str(missing))
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly_database_unavailable"):
            storage.connect_readonly()
        assert not missing.exists()
        assert not missing.parent.exists()

        legacy = tmp_path / "legacy.db"
        object.__setattr__(settings, "sqlite_path", str(legacy))
        with closing(sqlite3.connect(legacy)) as connection, connection:
            connection.execute("PRAGMA user_version=1")
        before = legacy.read_bytes()
        with pytest.raises(sqlite3.OperationalError, match="readonly_schema_not_current"):
            storage.connect_readonly()
        assert legacy.read_bytes() == before
        assert not legacy.with_name(f"{legacy.name}-wal").exists()
        assert not legacy.with_name(f"{legacy.name}-shm").exists()
    finally:
        object.__setattr__(settings, "sqlite_path", original)


def test_review_ledger_snapshot_budget_fails_closed(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _seed_report()
    with closing(storage.connect()) as connection, connection:
        for index in range(2):
            connection.execute(
                """
                INSERT INTO agent_review_records(
                  review_id,turn_id,run_id,created_at,reviewer,status,result,notes,metadata
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    f"review-{index}",
                    "turn-1",
                    "run-1",
                    "2026-08-06T08:10:00+08:00",
                    "governance-test",
                    "complete",
                    "{}",
                    "",
                    "{}",
                ),
            )
        connection.commit()
    monkeypatch.setattr(storage, "AGENT_GOVERNANCE_REVIEW_HASH_MAX_ROWS", 1)

    with pytest.raises(runtime_evidence.AgentGovernanceRuntimeEvidenceError, match="persistence_mismatch"):
        runtime_evidence.capture_agent_governance_runtime_checkpoint(
            window_end=report["window"]["end"],
        )


def test_review_ledger_oversized_row_fails_before_python_materialization(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _seed_report()
    with closing(storage.connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO agent_review_records(
              review_id,turn_id,run_id,created_at,reviewer,status,result,notes,metadata
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                "review-large",
                "turn-1",
                "run-1",
                "2026-08-06T08:10:00+08:00",
                "governance-test",
                "complete",
                "{}",
                "x" * 1024,
                "{}",
            ),
        )
        connection.commit()
    monkeypatch.setattr(storage, "AGENT_GOVERNANCE_REVIEW_HASH_MAX_BYTES", 128)

    with pytest.raises(runtime_evidence.AgentGovernanceRuntimeEvidenceError, match="persistence_mismatch"):
        runtime_evidence.capture_agent_governance_runtime_checkpoint(
            window_end=report["window"]["end"],
        )

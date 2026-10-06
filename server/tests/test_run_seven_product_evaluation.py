from __future__ import annotations

import importlib.util
import json
import os
import sys
from contextlib import closing
from pathlib import Path

import pytest

from app import storage
from app.seven_product_forecast_ledger import record_daily_seven_product_forecast

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "run_seven_product_evaluation.py"
SPEC = importlib.util.spec_from_file_location("run_seven_product_evaluation", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
evaluation_runner = importlib.util.module_from_spec(SPEC)
sys.modules["run_seven_product_evaluation"] = evaluation_runner
SPEC.loader.exec_module(evaluation_runner)


def test_runner_emits_blocked_content_addressed_evidence_without_mutating_database(tmp_path: Path) -> None:
    db_path = Path(os.environ["SQLITE_PATH"]).resolve()
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
    before_sha256 = evaluation_runner.sha256_file(db_path)
    output_dir = tmp_path / "evidence"

    exit_code = evaluation_runner.main(
        [
            "--db",
            str(db_path),
            "--output-dir",
            str(output_dir),
            "--as-of",
            "2026-08-31T00:00:00+00:00",
        ]
    )

    assert exit_code == 2
    latest = output_dir / "seven-product-evaluation-latest.json"
    payload = json.loads(latest.read_text(encoding="utf-8"))
    addressed = output_dir / f"seven-product-evaluation-{payload['evidence_body_sha256'][:16]}.json"
    assert addressed.read_bytes() == latest.read_bytes()
    assert output_dir.stat().st_mode & 0o777 == 0o700
    assert latest.stat().st_mode & 0o777 == 0o600
    assert addressed.stat().st_mode & 0o777 == 0o600
    assert payload["status"] == "blocked"
    assert payload["schema_version"] == "seven-product-evaluation-evidence.v2"
    assert payload["evaluation_cutoff_source"] == "requested_or_run_start_as_of"
    assert payload["forecast_as_of_time"] == "2026-08-31T00:00:00+00:00"
    assert payload["evaluation_as_of_time"] == payload["forecast_as_of_time"]
    assert payload["cutoff_matches_forecast"] is True
    assert payload["summary"]["contract_complete"] is True
    assert payload["summary"]["forecast_cells"] == 21
    assert payload["summary"]["evaluation_cells"] == 21
    assert payload["summary"]["passed_count"] == 0
    assert payload["database"]["fingerprint_schema_version"] == "sqlite-state-fingerprint.v1"
    assert payload["database"]["main_sha256"] == before_sha256
    assert payload["database"]["artifacts"]["main"]["sha256"] == before_sha256
    assert set(payload["database"]["artifacts"]) == {"main", "wal", "journal"}
    assert payload["database"]["unchanged_during_run"] is True
    assert evaluation_runner.sha256_file(db_path) == before_sha256


def test_runner_rejects_database_that_would_require_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "stale.db"
    db_path.touch()

    try:
        evaluation_runner.assert_current_database_read_only(db_path)
    except RuntimeError as exc:
        assert str(exc).startswith("evaluation_database_schema_mismatch:0!=")
    else:
        raise AssertionError("stale database must fail closed")


def test_runner_can_bind_evidence_to_exact_issued_ledger_batch(tmp_path: Path) -> None:
    db_path = Path(os.environ["SQLITE_PATH"]).resolve()
    issued = record_daily_seven_product_forecast(as_of_time="2026-09-02T01:30:00+00:00")
    before_sha256 = evaluation_runner.sha256_file(db_path)
    output_dir = tmp_path / "issued-evidence"

    exit_code = evaluation_runner.main(
        [
            "--db",
            str(db_path),
            "--output-dir",
            str(output_dir),
            "--issued-business-date",
            issued.business_date,
        ]
    )

    assert exit_code == 2
    payload = json.loads((output_dir / "seven-product-evaluation-latest.json").read_text(encoding="utf-8"))
    assert payload["forecast_source"] == "issued_ledger"
    assert payload["forecast_batch_id"] == issued.batch_id
    assert payload["forecast"]["payload_sha256"] == issued.payload_sha256
    assert payload["evaluation_cutoff_source"] == "issued_batch_as_of"
    assert payload["forecast_as_of_time"] == issued.as_of_time
    assert payload["evaluation_as_of_time"] == issued.as_of_time
    assert payload["cutoff_matches_forecast"] is True
    assert evaluation_runner.sha256_file(db_path) == before_sha256


def test_runner_rejects_requested_cutoff_that_conflicts_with_issued_batch(tmp_path: Path) -> None:
    db_path = Path(os.environ["SQLITE_PATH"]).resolve()
    issued = record_daily_seven_product_forecast(as_of_time="2026-09-02T01:30:00+00:00")
    before_sha256 = evaluation_runner.sha256_file(db_path)
    output_dir = tmp_path / "mismatched-issued-evidence"

    with pytest.raises(RuntimeError, match="issued_forecast_cutoff_mismatch"):
        evaluation_runner.main(
            [
                "--db",
                str(db_path),
                "--output-dir",
                str(output_dir),
                "--as-of",
                "2026-09-02T01:31:00+00:00",
                "--issued-business-date",
                issued.business_date,
            ]
        )

    assert not (output_dir / "seven-product-evaluation-latest.json").exists()
    assert evaluation_runner.sha256_file(db_path) == before_sha256


def test_evidence_publish_keeps_old_latest_when_latest_swap_fails(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "atomic-evidence"
    output_dir.mkdir()
    latest = output_dir / "seven-product-evaluation-latest.json"
    latest.write_text("old-complete-evidence\n", encoding="utf-8")
    original_replace = evaluation_runner.os.replace

    def fail_latest_swap(source: Path, destination: Path) -> None:
        if Path(destination) == latest:
            raise OSError("simulated_latest_swap_failure")
        original_replace(source, destination)

    monkeypatch.setattr(evaluation_runner.os, "replace", fail_latest_swap)

    with pytest.raises(OSError, match="simulated_latest_swap_failure"):
        evaluation_runner.write_evidence({"schema_version": "test.v1"}, output_dir=output_dir)

    assert latest.read_text(encoding="utf-8") == "old-complete-evidence\n"
    addressed = [path for path in output_dir.glob("seven-product-evaluation-*.json") if path != latest]
    assert len(addressed) == 1
    assert addressed[0].stat().st_mode & 0o777 == 0o600
    assert not list(output_dir.glob("tmp*"))


def test_evidence_publish_rejects_truncated_hash_path_collision(tmp_path: Path) -> None:
    output_dir = tmp_path / "collision-evidence"
    output_dir.mkdir()
    payload = {"schema_version": "test.v1"}
    body_sha256 = evaluation_runner.canonical_sha256(payload)
    addressed = output_dir / f"seven-product-evaluation-{body_sha256[:16]}.json"
    addressed.write_text("different bytes\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="evaluation_evidence_address_collision"):
        evaluation_runner.write_evidence(payload, output_dir=output_dir)

    assert addressed.read_text(encoding="utf-8") == "different bytes\n"
    assert not (output_dir / "seven-product-evaluation-latest.json").exists()


def test_sqlite_state_fingerprint_detects_wal_only_change_and_ignores_shm(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    database.write_bytes(b"main-stays-the-same")
    wal = Path(f"{database}-wal")
    wal.write_bytes(b"committed-wal-state-1")

    before = evaluation_runner.sqlite_state_fingerprint(database)
    main_sha256 = evaluation_runner.sha256_file(database)
    Path(f"{database}-shm").write_bytes(b"reader-lock-cache")
    after_shm = evaluation_runner.sqlite_state_fingerprint(database)

    assert after_shm == before
    wal.write_bytes(b"committed-wal-state-2")
    after_wal = evaluation_runner.sqlite_state_fingerprint(database)
    assert evaluation_runner.sha256_file(database) == main_sha256
    assert after_wal["artifacts"]["main"] == before["artifacts"]["main"]
    assert after_wal["artifacts"]["wal"]["sha256"] != before["artifacts"]["wal"]["sha256"]
    assert after_wal["sha256"] != before["sha256"]

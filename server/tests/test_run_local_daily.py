from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "run_local_daily.py"
SPEC = importlib.util.spec_from_file_location("run_local_daily", SCRIPT_PATH)
assert SPEC is not None
run_local_daily = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["run_local_daily"] = run_local_daily
SPEC.loader.exec_module(run_local_daily)


def _valid_database_fingerprint() -> dict[str, object]:
    main = {"exists": True, "size": 1, "mtime_ns": 1, "sha256": "c" * 64}
    artifacts = {
        "main": main,
        "wal": {"exists": False},
        "journal": {"exists": False},
    }
    content_artifacts = {
        role: {key: value for key, value in identity.items() if key != "mtime_ns"}
        for role, identity in artifacts.items()
    }
    state_sha256 = run_local_daily._canonical_sha256(
        {"schema_version": "sqlite-state-fingerprint.v1", "artifacts": content_artifacts}
    )
    return {
        "sha256": state_sha256,
        "main_sha256": "c" * 64,
        "fingerprint_schema_version": "sqlite-state-fingerprint.v1",
        "artifacts": artifacts,
        "unchanged_during_run": True,
    }


def test_local_daily_source_automation_does_not_schedule_soft_removed_ccf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured_commands: list[list[str]] = []

    def fake_run_subprocess(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        captured_commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(run_local_daily, "run_subprocess", fake_run_subprocess)
    args = run_local_daily.parse_args(
        [
            "--dry-run",
            "--db",
            str(tmp_path / "agent.db"),
            "--codex-run",
            str(tmp_path / ".codex-run"),
            "--output-dir",
            str(tmp_path / "local-production"),
        ]
    )

    result = run_local_daily.run_source_automation(args, output_dir=tmp_path / "local-production")

    assert result["exit_code"] == 0
    assert captured_commands
    command = captured_commands[0]
    assert "--import-ccf" not in command
    assert "--news-skip-details" not in command
    assert command[command.index("--news-limit-per-source") + 1] == "3"


def test_source_automation_marks_unchanged_previous_report_as_stale(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "local-production"
    latest = output_dir / "source-automation" / "source-automation-latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text('{"status":"blocked","critical_failures":["ccf_authorized_import"]}\n')

    def fake_run_subprocess(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="crashed")

    monkeypatch.setattr(run_local_daily, "run_subprocess", fake_run_subprocess)
    args = run_local_daily.parse_args(
        ["--dry-run", "--db", str(tmp_path / "agent.db"), "--output-dir", str(output_dir)]
    )

    result = run_local_daily.run_source_automation(args, output_dir=output_dir)

    assert result["exit_code"] == 1
    assert result["fresh_output"] is False


def test_source_lock_contention_is_retried_and_recovers_before_foundation(
    tmp_path: Path, monkeypatch
) -> None:
    attempts = 0
    clock = {"value": 0.0}

    def fake_source(args, output_dir):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return {"exit_code": 75, "fresh_output": False, "lock_contention": True}
        return {"exit_code": 0, "fresh_output": True, "lock_contention": False}

    monkeypatch.setattr(run_local_daily, "run_source_automation", fake_source)
    monkeypatch.setattr(run_local_daily, "SOURCE_LOCK_RETRY_MAX_WAIT_SECONDS", 2)
    monkeypatch.setattr(run_local_daily, "SOURCE_LOCK_RETRY_POLL_SECONDS", 1)
    monkeypatch.setattr(run_local_daily.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(
        run_local_daily.time, "sleep", lambda seconds: clock.__setitem__("value", clock["value"] + seconds)
    )

    result = run_local_daily.run_source_automation_with_lock_retry(
        run_local_daily.parse_args(["--dry-run"]), output_dir=tmp_path / "local-production"
    )

    assert attempts == 2
    assert result["exit_code"] == 0
    assert result["lock_retry"]["status"] == "recovered"


def test_source_lock_contention_timeout_remains_explicitly_blocked(tmp_path: Path, monkeypatch) -> None:
    clock = {"value": 0.0}
    calls = {"count": 0}

    def fake_source(args, output_dir):
        calls["count"] += 1
        return {"exit_code": 75, "fresh_output": False, "lock_contention": True}

    monkeypatch.setattr(run_local_daily, "run_source_automation", fake_source)
    monkeypatch.setattr(run_local_daily, "SOURCE_LOCK_RETRY_MAX_WAIT_SECONDS", 1)
    monkeypatch.setattr(run_local_daily, "SOURCE_LOCK_RETRY_POLL_SECONDS", 1)
    monkeypatch.setattr(run_local_daily.time, "monotonic", lambda: clock["value"])
    monkeypatch.setattr(
        run_local_daily.time, "sleep", lambda seconds: clock.__setitem__("value", clock["value"] + seconds)
    )

    result = run_local_daily.run_source_automation_with_lock_retry(
        run_local_daily.parse_args(["--dry-run"]), output_dir=tmp_path / "local-production"
    )

    assert calls["count"] == 1
    assert result["exit_code"] == 75
    assert result["lock_retry"]["status"] == "timed_out"
    assert result["lock_retry"]["reason"] == "source automation lock remained held"


def test_source_non_lock_failure_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    calls = {"count": 0}

    def fake_source(args, output_dir):
        calls["count"] += 1
        return {"exit_code": 1, "fresh_output": False, "lock_contention": False}

    monkeypatch.setattr(run_local_daily, "run_source_automation", fake_source)
    result = run_local_daily.run_source_automation_with_lock_retry(
        run_local_daily.parse_args(["--dry-run"]), output_dir=tmp_path / "local-production"
    )

    assert calls["count"] == 1
    assert "lock_retry" not in result


def test_apply_source_automation_command_requests_no_pre_write_backup(
    tmp_path: Path, monkeypatch
) -> None:
    """Final backup state (DISK-MODEL §7): the daily chain's source step runs
    --apply without --backup-db; its writes are covered by the daily anchor
    created later in the same chain run."""
    captured_commands: list[list[str]] = []

    def fake_run_subprocess(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        captured_commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(run_local_daily, "run_subprocess", fake_run_subprocess)
    args = run_local_daily.parse_args(
        ["--apply", "--db", str(tmp_path / "agent.db"), "--output-dir", str(tmp_path / "local-production")]
    )

    run_local_daily.run_source_automation(args, output_dir=tmp_path / "local-production")

    command = captured_commands[0]
    assert "--apply" in command
    assert "--backup-db" not in command
    assert "--backup-reuse-seconds" not in command
    assert "--backup-timeout-seconds" not in command


def test_local_daily_invokes_apply_forecast_lifecycle_and_reads_its_report(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "local-production"

    def fake_run_subprocess(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        latest = output_dir / "seven-product-lifecycle" / "seven-product-lifecycle-latest.json"
        latest.parent.mkdir(parents=True)
        latest.write_text(
            '{"status":"ready_with_warnings","report_sha256":"abc","forecast":{"cell_count":21},'
            '"settlement":{"inserted":0},"blockers":[],"warnings":["promotion pending"]}\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(run_local_daily, "run_subprocess", fake_run_subprocess)
    args = run_local_daily.parse_args(
        ["--apply", "--db", str(tmp_path / "agent.db"), "--output-dir", str(output_dir)]
    )

    result = run_local_daily.run_seven_product_lifecycle(args, output_dir=output_dir)

    assert "--apply" in result["command"]
    assert result["status"] == "ready_with_warnings"
    assert result["forecast"]["cell_count"] == 21
    assert result["warnings"] == ["promotion pending"]


def test_local_daily_accepts_fresh_blocked_oos_evidence_as_healthy_execution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "local-production"
    source_db = tmp_path / "agent.db"
    with closing(sqlite3.connect(source_db)) as connection:
        connection.execute("CREATE TABLE market_observations (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO market_observations VALUES (1)")
        connection.commit()

    def fake_run_subprocess(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        latest = output_dir / "seven-product-evaluation" / "seven-product-evaluation-latest.json"
        latest.parent.mkdir(parents=True, exist_ok=True)
        body = {
            "schema_version": "seven-product-evaluation-evidence.v2",
            "status": "blocked",
            "forecast_source": "issued_ledger",
            "forecast_batch_id": "seven-test",
            "evaluation_cutoff_source": "issued_batch_as_of",
            "forecast_as_of_time": "2026-09-02T01:30:00+00:00",
            "evaluation_as_of_time": "2026-09-02T01:30:00+00:00",
            "cutoff_matches_forecast": True,
            "database": _valid_database_fingerprint(),
            "summary": {
                "contract_complete": True,
                "forecast_cells": 21,
                "evaluation_cells": 21,
                "passed_count": 0,
                "failed_count": 21,
                "evaluation_report_sha256": "b" * 64,
            },
        }
        body["evidence_body_sha256"] = run_local_daily._canonical_sha256(body)
        latest.write_text(json.dumps(body), encoding="utf-8")
        return subprocess.CompletedProcess(command, 2, stdout="blocked", stderr="")

    monkeypatch.setattr(run_local_daily, "run_subprocess", fake_run_subprocess)
    args = run_local_daily.parse_args(
        ["--apply", "--db", str(tmp_path / "agent.db"), "--output-dir", str(output_dir)]
    )

    result = run_local_daily.run_seven_product_evaluation(
        args,
        output_dir=output_dir,
        issued_business_date="2026-09-02",
    )

    assert result["exit_code"] == 2
    assert result["status"] == "blocked"
    assert result["fresh_output"] is True
    assert result["evidence_valid"] is True
    assert result["passed_count"] == 0
    assert result["database_unchanged_during_run"] is True
    assert result["forecast_source"] == "issued_ledger"
    assert result["evaluation_cutoff_source"] == "issued_batch_as_of"
    assert result["cutoff_matches_forecast"] is True
    assert "--issued-business-date" in result["command"]
    assert result["evaluation_input_mode"] == "consistent_snapshot_after_issue"
    assert len(result["evaluation_input_sha256"]) == 64
    snapshot_path = Path(result["evaluation_input_path"])
    assert snapshot_path.name == "agent-evaluation-input-2026-09-02.sqlite"
    assert snapshot_path.parent.name == "evaluation-input"
    assert snapshot_path.stat().st_mode & 0o777 == 0o600
    database_argument = result["command"][result["command"].index("--db") + 1]
    assert database_argument == str(snapshot_path)
    assert database_argument != str(source_db)


def test_evaluation_input_snapshot_copies_committed_rows_and_prunes_previous_dates(tmp_path: Path) -> None:
    source_db = tmp_path / "agent.db"
    with closing(sqlite3.connect(source_db)) as connection:
        connection.execute("CREATE TABLE market_observations (id INTEGER PRIMARY KEY, source_id TEXT)")
        connection.execute("INSERT INTO market_observations VALUES (1, 'eia_petroleum_api')")
        connection.commit()

    snapshot_dir = tmp_path / "evaluation-input"
    snapshot_dir.mkdir()
    stale = snapshot_dir / "agent-evaluation-input-2026-09-01.sqlite"
    stale.write_bytes(b"stale")
    Path(f"{stale}-wal").write_bytes(b"stale")

    snapshot = run_local_daily.create_evaluation_input_snapshot(
        source_db,
        snapshot_dir=snapshot_dir,
        business_date="2026-09-02",
    )

    snapshot_path = Path(str(snapshot["path"]))
    assert snapshot["mode"] == "consistent_snapshot_after_issue"
    assert snapshot["business_date"] == "2026-09-02"
    assert snapshot["size"] > 0
    assert len(snapshot["sha256"]) == 64
    assert not stale.exists()
    assert not Path(f"{stale}-wal").exists()
    assert snapshot_path.exists()
    assert snapshot_path.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(f"{snapshot_path.as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0]
    assert rows == 1


def test_apply_evaluation_snapshot_failure_blocks_without_running_evaluator(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = tmp_path / "local-production"
    calls: list[list[str]] = []
    monkeypatch.setattr(run_local_daily, "EVALUATION_SNAPSHOT_RETRY_SECONDS", 0)

    def fake_run_subprocess(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(run_local_daily, "run_subprocess", fake_run_subprocess)
    args = run_local_daily.parse_args(
        ["--apply", "--db", str(tmp_path / "missing.db"), "--output-dir", str(output_dir)]
    )

    result = run_local_daily.run_seven_product_evaluation(
        args,
        output_dir=output_dir,
        issued_business_date="2026-09-02",
    )

    assert calls == []
    assert result["exit_code"] is None
    assert result["status"] == "missing"
    assert result["fresh_output"] is False
    assert result["evidence_valid"] is False
    assert result["evaluation_input_mode"] == "missing"
    assert result["validation_error"] == "evaluation_input_snapshot_failed:OperationalError"
    snapshot_dir = output_dir / "seven-product-evaluation" / "evaluation-input"
    if snapshot_dir.exists():
        assert list(snapshot_dir.glob("*.tmp*")) == []


def test_snapshot_retries_after_transient_failure_without_leaving_temp_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_db = tmp_path / "agent.db"
    with closing(sqlite3.connect(source_db)) as connection:
        connection.execute("CREATE TABLE market_observations (id INTEGER PRIMARY KEY)")
        connection.commit()

    snapshot_dir = tmp_path / "evaluation-input"
    sleeps: list[float] = []
    monkeypatch.setattr(run_local_daily.time, "sleep", sleeps.append)
    real_fsync_directory = run_local_daily._fsync_directory
    fsync_calls = {"count": 0}

    def flaky_fsync(path: Path) -> None:
        fsync_calls["count"] += 1
        if fsync_calls["count"] == 1:
            raise OSError("transient")
        real_fsync_directory(path)

    monkeypatch.setattr(run_local_daily, "_fsync_directory", flaky_fsync)

    snapshot = run_local_daily.create_evaluation_input_snapshot(
        source_db,
        snapshot_dir=snapshot_dir,
        business_date="2026-09-04",
    )

    assert sleeps == [run_local_daily.EVALUATION_SNAPSHOT_RETRY_SECONDS]
    assert fsync_calls["count"] == 2
    assert snapshot["mode"] == "consistent_snapshot_after_issue"
    assert list(snapshot_dir.glob(".*.tmp*")) == []
    assert sorted(path.name for path in snapshot_dir.iterdir()) == [
        "agent-evaluation-input-2026-09-04.sqlite"
    ]


def _open_link_readonly_count(link: Path) -> int:
    """Open a hard-linked database read-only from a subprocess.

    The in-process DG01 safety gate intentionally refuses to open hard-linked
    database files (it guards against accidental writes through a second link),
    so the read-only-open proof runs in a clean child interpreter.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sqlite3, sys\n"
                "c = sqlite3.connect('file:' + sys.argv[1] + '?mode=ro', uri=True)\n"
                "c.execute('PRAGMA query_only = ON')\n"
                "print(c.execute('SELECT COUNT(*) FROM market_observations').fetchone()[0])\n"
            ),
            str(link),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-500:]
    return int(completed.stdout.strip())


def test_evaluation_input_hardlinks_the_daily_anchor_zero_copy(tmp_path: Path) -> None:
    """Final backup state (DISK-MODEL §7): with a valid same-run anchor, the
    evaluation input is an os.link hard link to the anchor inode (zero extra
    bytes), not a copy."""
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE market_observations (id INTEGER PRIMARY KEY, source_id TEXT)")
        connection.execute("INSERT INTO market_observations VALUES (1, 'eia_petroleum_api')")
    anchor_report = run_local_daily.verify_backup(database, business_date="2026-09-17")
    assert anchor_report["integrity_check"] == "ok"
    anchor = Path(anchor_report["backup_path"])

    snapshot = run_local_daily.create_evaluation_input_snapshot(
        database,
        snapshot_dir=tmp_path / "evaluation-input",
        business_date="2026-09-17",
        anchor_path=anchor,
    )

    link = Path(str(snapshot["path"]))
    assert snapshot["mode"] == "consistent_snapshot_after_issue"
    assert snapshot["source"] == "daily_anchor_hardlink"
    assert not snapshot.get("degraded_reason")
    assert link.name == "agent-evaluation-input-2026-09-17.sqlite"
    assert link.exists() and anchor.exists()
    link_stat = os.stat(link)
    assert (link_stat.st_dev, link_stat.st_ino) == (anchor.stat().st_dev, anchor.stat().st_ino)
    assert link_stat.st_nlink >= 2
    assert len(snapshot["sha256"]) == 64
    assert _open_link_readonly_count(link) == 1


def test_evaluation_input_link_survives_anchor_rotation_unlink(tmp_path: Path) -> None:
    """POSIX link semantics: when anchor rotation deletes the anchor directory
    entry, the hard-linked evaluation input keeps the data and stays readable."""
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE market_observations (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO market_observations VALUES (7)")
    anchor_report = run_local_daily.verify_backup(database, business_date="2026-09-17")
    anchor = Path(anchor_report["backup_path"])
    snapshot = run_local_daily.create_evaluation_input_snapshot(
        database,
        snapshot_dir=tmp_path / "evaluation-input",
        business_date="2026-09-17",
        anchor_path=anchor,
    )
    link = Path(str(snapshot["path"]))

    # Simulate keep-7 rotation removing the anchor entry (and its sidecars).
    anchor.unlink()
    assert not anchor.exists()

    assert link.exists()
    assert os.stat(link).st_nlink == 1
    assert _open_link_readonly_count(link) == 1


def test_evaluation_input_falls_back_to_full_copy_when_anchor_missing(tmp_path: Path) -> None:
    """A missing same-run anchor must fall back to the legacy full snapshot with
    an explicit degraded reason -- never silently link an older anchor."""
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE market_observations (id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO market_observations VALUES (1)")

    snapshot = run_local_daily.create_evaluation_input_snapshot(
        database,
        snapshot_dir=tmp_path / "evaluation-input",
        business_date="2026-09-18",
        anchor_path=tmp_path / "backups" / "daily-anchor" / "missing_anchor.sqlite",
    )

    fallback = Path(str(snapshot["path"]))
    assert snapshot["mode"] == "consistent_snapshot_after_issue"
    assert snapshot["source"] == "full_online_backup_fallback"
    assert snapshot["degraded_reason"] == "evaluation_input_anchor_missing"
    assert fallback.exists()
    assert os.stat(fallback).st_nlink == 1
    assert fallback.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(f"file:{fallback}?mode=ro", uri=True)) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM market_observations").fetchone()[0]
    assert rows == 1


def test_seven_product_evaluation_step_passes_anchor_report_and_links_input(
    tmp_path: Path, monkeypatch
) -> None:
    """run_seven_product_evaluation must hand the same-run anchor to the snapshot
    helper so the normal apply path is zero-copy."""
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE market_observations (id INTEGER PRIMARY KEY)")
    anchor_report = run_local_daily.verify_backup(database, business_date="2026-09-17")
    captured: dict[str, object] = {}

    def fake_snapshot(db_path, *, snapshot_dir, business_date, anchor_path=None):
        captured["anchor_path"] = anchor_path
        return {
            "mode": "consistent_snapshot_after_issue",
            "source": "daily_anchor_hardlink",
            "path": str(snapshot_dir / f"agent-evaluation-input-{business_date}.sqlite"),
            "business_date": business_date,
            "sha256": "a" * 64,
            "size": 1,
        }

    monkeypatch.setattr(run_local_daily, "create_evaluation_input_snapshot", fake_snapshot)
    monkeypatch.setattr(
        run_local_daily,
        "run_subprocess",
        lambda command, *, timeout_seconds: subprocess.CompletedProcess(command, 0, stdout="", stderr=""),
    )
    args = run_local_daily.parse_args(
        ["--apply", "--db", str(database), "--output-dir", str(tmp_path / "local-production")]
    )

    result = run_local_daily.run_seven_product_evaluation(
        args,
        output_dir=tmp_path / "local-production",
        issued_business_date="2026-09-17",
        anchor_report=anchor_report,
    )

    assert captured["anchor_path"] == Path(anchor_report["backup_path"])
    assert result["evaluation_input_source"] == "daily_anchor_hardlink"
    assert result["evaluation_input_degraded_reason"] == ""


def test_evaluation_input_degraded_reason_is_a_nonblocking_warning() -> None:
    """The fallback must be visible in readiness warnings without blocking."""
    readiness = build_readiness_for_foundation(
        {
            "execution_semantics": "runtime_execution",
            "job_count": 12,
            "turn_count": 12,
            "handoff_count": 11,
            "accepted_handoffs": 11,
            "report_agent_completed": True,
            "report_artifact_id": "daily-report",
        },
        evaluation={
            "status": "passed",
            "exit_code": 0,
            "fresh_output": True,
            "evidence_valid": True,
            "database_unchanged_during_run": True,
            "forecast_source": "issued_ledger",
            "cutoff_matches_forecast": True,
            "passed_count": 21,
            "evaluation_input_mode": "consistent_snapshot_after_issue",
            "evaluation_input_degraded_reason": "evaluation_input_anchor_missing",
        },
    )

    assert readiness["overall_status"] == "ready_with_warnings"
    assert readiness["blockers"] == []
    assert any(
        "evaluation input used a full copy fallback: evaluation_input_anchor_missing" in warning
        for warning in readiness["warnings"]
    )


def test_daily_status_history_is_content_addressed_and_pruned(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(run_local_daily, "DAILY_STATUS_HISTORY_RETENTION", 2)
    output_dir = tmp_path / "local-production"

    first = run_local_daily.persist_daily_status_history(output_dir, {"overall_status": "blocked", "blockers": ["a"]})
    repeat = run_local_daily.persist_daily_status_history(output_dir, {"overall_status": "blocked", "blockers": ["a"]})
    second = run_local_daily.persist_daily_status_history(
        output_dir, {"overall_status": "ready_with_warnings", "blockers": []}
    )
    third = run_local_daily.persist_daily_status_history(output_dir, {"overall_status": "ready", "blockers": []})

    history_dir = output_dir / "status-history"
    assert history_dir.stat().st_mode & 0o777 == 0o700
    files = sorted(history_dir.glob("daily-status-*.json"))
    assert len(files) == 2
    assert first["status_body_sha256"] == repeat["status_body_sha256"]
    assert len({first["status_body_sha256"], second["status_body_sha256"], third["status_body_sha256"]}) == 3
    assert repeat["pruned"] == 0 and second["pruned"] == 0 and third["pruned"] == 1
    matching = [
        path
        for path in files
        if json.loads(path.read_text(encoding="utf-8"))["status_body_sha256"] == third["status_body_sha256"]
    ]
    assert len(matching) == 1
    payload = json.loads(matching[0].read_text(encoding="utf-8"))
    assert payload["overall_status"] == "ready"
    assert payload["status_body_sha256"] == third["status_body_sha256"]
    for path in history_dir.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600


def test_local_daily_rejects_stale_oos_evidence_after_runner_failure(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "local-production"
    latest = output_dir / "seven-product-evaluation" / "seven-product-evaluation-latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text('{"status":"passed"}\n', encoding="utf-8")

    def fake_run_subprocess(command: list[str], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
        del timeout_seconds
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="crashed")

    monkeypatch.setattr(run_local_daily, "run_subprocess", fake_run_subprocess)
    args = run_local_daily.parse_args(
        ["--dry-run", "--db", str(tmp_path / "agent.db"), "--output-dir", str(output_dir)]
    )

    result = run_local_daily.run_seven_product_evaluation(args, output_dir=output_dir)

    assert result["fresh_output"] is False
    assert result["evidence_valid"] is False
    assert result["validation_error"] == "evaluation_evidence_missing_or_invalid_json"


def test_local_daily_rejects_hash_valid_oos_evidence_with_mixed_cutoffs() -> None:
    body = {
        "schema_version": "seven-product-evaluation-evidence.v2",
        "status": "blocked",
        "forecast_source": "issued_ledger",
        "forecast_batch_id": "seven-test",
        "evaluation_cutoff_source": "issued_batch_as_of",
        "forecast_as_of_time": "2026-09-02T01:30:00+00:00",
        "evaluation_as_of_time": "2026-09-02T01:31:00+00:00",
        "cutoff_matches_forecast": True,
        "database": _valid_database_fingerprint(),
        "summary": {
            "contract_complete": True,
            "forecast_cells": 21,
            "evaluation_cells": 21,
            "passed_count": 0,
            "failed_count": 21,
            "evaluation_report_sha256": "b" * 64,
        },
    }
    body["evidence_body_sha256"] = run_local_daily._canonical_sha256(body)

    valid, reason = run_local_daily._validate_daily_evaluation_evidence(body)

    assert valid is False
    assert reason == "evaluation_forecast_cutoff_mismatch"


def test_local_daily_rejects_hash_valid_evidence_with_spoofed_database_fingerprint() -> None:
    body = {
        "schema_version": "seven-product-evaluation-evidence.v2",
        "status": "blocked",
        "forecast_source": "issued_ledger",
        "forecast_batch_id": "seven-test",
        "evaluation_cutoff_source": "issued_batch_as_of",
        "forecast_as_of_time": "2026-09-02T01:30:00+00:00",
        "evaluation_as_of_time": "2026-09-02T01:30:00+00:00",
        "cutoff_matches_forecast": True,
        "database": _valid_database_fingerprint(),
        "summary": {
            "contract_complete": True,
            "forecast_cells": 21,
            "evaluation_cells": 21,
            "passed_count": 0,
            "failed_count": 21,
            "evaluation_report_sha256": "b" * 64,
        },
    }
    body["database"]["sha256"] = "d" * 64
    body["evidence_body_sha256"] = run_local_daily._canonical_sha256(body)

    valid, reason = run_local_daily._validate_daily_evaluation_evidence(body)

    assert valid is False
    assert reason == "evaluation_database_fingerprint_invalid"


def _seed_anchor_dir(database: Path, *, stamps: list[str]) -> Path:
    anchor_dir = database.parent / "backups" / run_local_daily.DAILY_ANCHOR_DIR_NAME
    anchor_dir.mkdir(parents=True, exist_ok=True)
    for index, stamp in enumerate(stamps):
        anchor = anchor_dir / f"{database.name}.daily_anchor_{stamp}.sqlite"
        with closing(sqlite3.connect(anchor)) as connection, connection:
            connection.execute("CREATE TABLE market_observations (value TEXT)")
        os.utime(anchor, (1_700_000_000 + index, 1_700_000_000 + index))
    return anchor_dir


def test_daily_anchor_is_created_integrity_checked_and_rotated_keep_seven(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE market_observations (value TEXT)")

    anchor_dir = _seed_anchor_dir(database, stamps=[f"202609{day:02d}_093000" for day in range(10, 17)])

    report = run_local_daily.verify_backup(database, business_date="2026-09-17")

    assert report["integrity_check"] == "ok"
    assert report["restore_integrity_check"] == "ok"
    assert report["backup_method"] == "sqlite_online_backup"
    assert report["anchor_reused"] is False
    assert run_local_daily.DAILY_ANCHOR_RETENTION == 7
    assert report["anchor_retention"]["keep"] == 7
    assert report["anchor_retention"]["kept"] == 7
    anchors = sorted(anchor_dir.glob("*.sqlite"))
    assert len(anchors) == 7
    assert Path(report["backup_path"]).name in {path.name for path in anchors}
    assert anchor_dir.stat().st_mode & 0o777 == 0o700
    assert Path(report["backup_path"]).stat().st_mode & 0o777 == 0o600
    # The restore drill must leave no probe copy behind (net anchor footprint).
    assert not list(anchor_dir.glob("*.restore_probe.sqlite"))


def test_daily_anchor_reuses_same_business_date_and_keeps_one_per_day(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE market_observations (value TEXT)")
    anchor_dir = database.parent / "backups" / run_local_daily.DAILY_ANCHOR_DIR_NAME

    first = run_local_daily.verify_backup(database, business_date="2026-09-17")
    second = run_local_daily.verify_backup(database, business_date="2026-09-17")

    assert first["anchor_reused"] is False
    assert second["anchor_reused"] is True
    assert first["backup_path"] == second["backup_path"]
    assert second["integrity_check"] == "ok"
    assert len(list(anchor_dir.glob("*.sqlite"))) == 1


def test_daily_anchor_reports_missing_database_without_writing(tmp_path: Path) -> None:
    report = run_local_daily.verify_backup(tmp_path / "missing.db", business_date="2026-09-17")

    assert report["backup_created"] is False
    assert report["error"] == "database_not_found"
    assert report["integrity_check"] == "not_run"


def test_foundation_step_blocks_safely_when_daily_anchor_is_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    """Explanatory failure-safety test (DISK-MODEL §3.2 项2): with the per-step
    full backups removed, an invalid anchor must fail the foundation step closed
    before any subprocess runs, so no business data is written without a
    recovery point."""

    def must_not_run(*_: object, **__: object) -> object:
        raise AssertionError("foundation subprocess must not run without a valid anchor")

    monkeypatch.setattr(run_local_daily, "run_subprocess", must_not_run)
    args = run_local_daily.parse_args(
        ["--apply", "--db", str(tmp_path / "agent.db"), "--output-dir", str(tmp_path / "out")]
    )

    for broken_report in (
        {"integrity_check": "anchor_copy_failed", "backup_path": ""},
        {"integrity_check": "ok", "backup_path": str(tmp_path / "nope.sqlite")},
    ):
        result = run_local_daily.run_foundation_materialization(
            args, output_dir=tmp_path / "out", anchor_report=broken_report
        )
        assert result["status"] == "blocked"
        assert result["reason"] == "daily_anchor_unavailable"
        assert result["exit_code"] is None


def test_materialized_only_agent_taxonomy_is_warning_not_false_execution_blocker() -> None:
    readiness = build_readiness_for_foundation(
        {
            "execution_semantics": "materialized_only",
            "job_count": 12,
            "turn_count": 0,
            "handoff_count": 0,
            "accepted_handoffs": 0,
            "report_agent_completed": False,
            "report_artifact_id": "",
        }
    )

    assert readiness["overall_status"] == "ready_with_warnings"
    assert readiness["blockers"] == []
    assert "daily Agent taxonomy is materialized-only; no execution or report is claimed" in readiness["warnings"]


def test_materialized_only_agent_taxonomy_blocks_false_execution_claims() -> None:
    readiness = build_readiness_for_foundation(
        {
            "execution_semantics": "materialized_only",
            "job_count": 12,
            "turn_count": 12,
            "handoff_count": 11,
            "accepted_handoffs": 11,
            "report_agent_completed": True,
            "report_artifact_id": "forged-report",
        }
    )

    assert readiness["overall_status"] == "blocked"
    assert "materialized-only Agent taxonomy contains false execution claims" in readiness["blockers"]


def test_runtime_execution_still_requires_complete_trace_and_report() -> None:
    readiness = build_readiness_for_foundation(
        {
            "execution_semantics": "runtime_execution",
            "job_count": 12,
            "turn_count": 12,
            "handoff_count": 11,
            "accepted_handoffs": 11,
            "report_agent_completed": True,
            "report_artifact_id": "daily-report",
        }
    )

    assert readiness["overall_status"] == "ready"
    assert readiness["blockers"] == []


def test_unknown_agent_execution_semantics_fail_closed() -> None:
    readiness = build_readiness_for_foundation({"job_count": 12})

    assert readiness["overall_status"] == "blocked"
    assert "agent foundation execution semantics are missing or unsupported" in readiness["blockers"]


def test_oos_gate_failure_warns_without_blocking_daily_execution() -> None:
    readiness = build_readiness_for_foundation(
        {
            "execution_semantics": "runtime_execution",
            "job_count": 12,
            "turn_count": 12,
            "handoff_count": 11,
            "accepted_handoffs": 11,
            "report_agent_completed": True,
            "report_artifact_id": "daily-report",
        },
        evaluation={
            "status": "blocked",
            "exit_code": 2,
            "fresh_output": True,
            "evidence_valid": True,
            "database_unchanged_during_run": True,
            "forecast_source": "issued_ledger",
            "cutoff_matches_forecast": True,
            "passed_count": 0,
            "evaluation_input_mode": "consistent_snapshot_after_issue",
        },
    )

    assert readiness["overall_status"] == "ready_with_warnings"
    assert readiness["blockers"] == []
    assert "formal OOS promotion remains blocked for 21 of 21 cells" in readiness["warnings"]


def test_oos_evaluator_failure_blocks_daily_readiness() -> None:
    readiness = build_readiness_for_foundation(
        {
            "execution_semantics": "runtime_execution",
            "job_count": 12,
            "turn_count": 12,
            "handoff_count": 11,
            "accepted_handoffs": 11,
            "report_agent_completed": True,
            "report_artifact_id": "daily-report",
        },
        evaluation={
            "status": "missing",
            "exit_code": 1,
            "fresh_output": False,
            "evidence_valid": False,
            "database_unchanged_during_run": False,
            "forecast_source": "missing",
            "cutoff_matches_forecast": False,
            "passed_count": 0,
            "evaluation_input_mode": "missing",
        },
    )

    assert readiness["overall_status"] == "blocked"
    assert "seven-product OOS evaluation did not publish fresh valid read-only evidence" in readiness["blockers"]


def test_oos_cutoff_mismatch_blocks_daily_readiness_even_if_hash_validator_was_bypassed() -> None:
    readiness = build_readiness_for_foundation(
        {
            "execution_semantics": "runtime_execution",
            "job_count": 12,
            "turn_count": 12,
            "handoff_count": 11,
            "accepted_handoffs": 11,
            "report_agent_completed": True,
            "report_artifact_id": "daily-report",
        },
        evaluation={
            "status": "blocked",
            "exit_code": 2,
            "fresh_output": True,
            "evidence_valid": True,
            "database_unchanged_during_run": True,
            "forecast_source": "issued_ledger",
            "cutoff_matches_forecast": False,
            "passed_count": 0,
            "evaluation_input_mode": "consistent_snapshot_after_issue",
        },
    )

    assert readiness["overall_status"] == "blocked"
    assert "seven-product OOS evaluation did not publish fresh valid read-only evidence" in readiness["blockers"]


def test_apply_mode_evaluation_without_consistent_snapshot_input_blocks_readiness() -> None:
    readiness = build_readiness_for_foundation(
        {
            "execution_semantics": "runtime_execution",
            "job_count": 12,
            "turn_count": 12,
            "handoff_count": 11,
            "accepted_handoffs": 11,
            "report_agent_completed": True,
            "report_artifact_id": "daily-report",
        },
        evaluation={
            "status": "blocked",
            "exit_code": 2,
            "fresh_output": True,
            "evidence_valid": True,
            "database_unchanged_during_run": True,
            "forecast_source": "issued_ledger",
            "cutoff_matches_forecast": True,
            "passed_count": 0,
            "evaluation_input_mode": "live_database_preview",
        },
    )

    assert readiness["overall_status"] == "blocked"
    assert "seven-product OOS evaluation did not publish fresh valid read-only evidence" in readiness["blockers"]


def test_local_daily_scheduler_state_preserves_success_on_blocked_attempt() -> None:
    previous = {
        "scheduler_observability": {
            "schema_version": "scheduler_observability.v1",
            "scheduler": "local_daily",
            "last_attempt_at": "2026-09-01T01:00:00+00:00",
            "last_success_at": "2026-09-01T01:00:00+00:00",
            "status": "ready",
            "duration_seconds": 60,
            "backlog": 0,
            "failure_counts": {},
        }
    }
    payload = {
        "overall_status": "blocked",
        "blockers": ["source unavailable"],
        "finished_at": "2026-09-02T01:05:00+00:00",
    }

    run_local_daily._attach_scheduler_observability(
        payload,
        previous_payload=previous,
        started_at="2026-09-02T01:00:00+00:00",
    )

    state = payload["scheduler_observability"]
    assert state["status"] == "blocked"
    assert state["last_success_at"] == "2026-09-01T01:00:00+00:00"
    assert state["duration_seconds"] == 300
    assert state["backlog"] == 1
    assert state["failure_counts"] == {"blocked": 1}


def build_readiness_for_foundation(
    trace: dict[str, object],
    *,
    evaluation: dict[str, object] | None = None,
) -> dict[str, object]:
    return run_local_daily.build_readiness(
        mode="apply",
        started_at="2026-08-31T00:00:00+00:00",
        finished_at="2026-08-31T00:01:00+00:00",
        source_process={"exit_code": 0, "fresh_output": True},
        source_latest={"status": "completed", "critical_failures": [], "after": {}},
        industry_import={"status": "soft_removed", "exit_code": 0},
        quality_refresh={"exit_code": 0},
        foundation_materialization={
            "status": "success",
            "exit_code": 0,
            "after": {"rag_chunks": 1, "graph_nodes": 1},
            "agent_trace": trace,
        },
        seven_product_lifecycle={"status": "ready", "exit_code": 0, "warnings": []},
        seven_product_evaluation=evaluation or {
            "status": "passed",
            "exit_code": 0,
            "fresh_output": True,
            "evidence_valid": True,
            "database_unchanged_during_run": True,
            "forecast_source": "issued_ledger",
            "cutoff_matches_forecast": True,
            "passed_count": 21,
            "evaluation_input_mode": "consistent_snapshot_after_issue",
        },
        quality={"overall_status": "success", "gates": [], "alerts": []},
        db_snapshot={},
        backup_report={"integrity_check": "ok"},
        health_report={"all_ok": True},
    )


def _judgement_readiness(overall: str = "ready_with_warnings") -> dict[str, object]:
    return {
        "overall_status": overall,
        "blockers": [],
        "started_at": "2026-09-17T01:30:00+00:00",
        "finished_at": "2026-09-17T01:38:12+00:00",
    }


def test_daily_success_materializes_judgement_snapshot(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_materialize(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {
            "snapshot_id": "daily-2026-09-17-abc123",
            "business_date": "2026-09-17",
            "payload_sha256": "a" * 64,
            "source_run_id": "local-daily:2026-09-17",
            "idempotent_replay": False,
        }

    monkeypatch.setattr(run_local_daily, "materialize_daily_judgement", fake_materialize)
    args = run_local_daily.parse_args(
        ["--apply", "--db", str(tmp_path / "agent.db"), "--output-dir", str(tmp_path / "o")]
    )

    result = run_local_daily.run_daily_judgement_snapshot(args, readiness=_judgement_readiness())

    assert result["status"] == "materialized"
    assert result["snapshot_id"] == "daily-2026-09-17-abc123"
    # 01:30Z is 09:30 Asia/Shanghai on the same calendar day.
    assert captured["business_date"] == "2026-09-17"
    assert captured["daily_chain_result"] == {
        "business_date": "2026-09-17",
        "overall_status": "ready_with_warnings",
        "started_at": "2026-09-17T01:30:00+00:00",
        "finished_at": "2026-09-17T01:38:12+00:00",
    }


def test_daily_judgement_snapshot_step_is_idempotent_on_same_day(tmp_path: Path, monkeypatch) -> None:
    def fake_materialize(**_: object) -> dict[str, object]:
        return {
            "snapshot_id": "daily-2026-09-17-abc123",
            "idempotent_replay": True,
            "payload_sha256": "a" * 64,
            "source_run_id": "local-daily:2026-09-17",
        }

    monkeypatch.setattr(run_local_daily, "materialize_daily_judgement", fake_materialize)
    args = run_local_daily.parse_args(
        ["--apply", "--db", str(tmp_path / "agent.db"), "--output-dir", str(tmp_path / "o")]
    )

    result = run_local_daily.run_daily_judgement_snapshot(args, readiness=_judgement_readiness())

    assert result["status"] == "idempotent_replay"


def test_daily_failure_or_dry_run_never_materializes_judgement_snapshot(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def fail_if_called(**_: object) -> dict[str, object]:
        calls.append("called")
        raise AssertionError("materialize must not run")

    monkeypatch.setattr(run_local_daily, "materialize_daily_judgement", fail_if_called)
    apply_args = run_local_daily.parse_args(
        ["--apply", "--db", str(tmp_path / "agent.db"), "--output-dir", str(tmp_path / "o")]
    )

    blocked = run_local_daily.run_daily_judgement_snapshot(
        apply_args, readiness=_judgement_readiness(overall="blocked")
    )
    assert blocked["status"] == "skipped"
    assert blocked["reason"] == "daily_chain_did_not_succeed"

    dry_args = run_local_daily.parse_args(
        ["--dry-run", "--db", str(tmp_path / "agent.db"), "--output-dir", str(tmp_path / "o")]
    )
    dry = run_local_daily.run_daily_judgement_snapshot(dry_args, readiness=_judgement_readiness())
    assert dry["status"] == "skipped"
    assert dry["reason"] == "dry_run_does_not_materialize_snapshots"
    assert calls == []


def test_daily_judgement_snapshot_failure_blocks_daily_readiness(tmp_path: Path, monkeypatch) -> None:
    def broken_materialize(**_: object) -> dict[str, object]:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(run_local_daily, "materialize_daily_judgement", broken_materialize)
    args = run_local_daily.parse_args(
        ["--apply", "--db", str(tmp_path / "agent.db"), "--output-dir", str(tmp_path / "o")]
    )

    result = run_local_daily.run_daily_judgement_snapshot(args, readiness=_judgement_readiness())

    assert result["status"] == "failed"
    assert "database is locked" in str(result["reason"])


def test_run_daily_with_lock_freezes_snapshot_and_blocks_when_it_fails(tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "local-production"
    output_dir.mkdir(parents=True)
    materialize_calls: list[dict[str, object]] = []

    def fake_materialize(**kwargs: object) -> dict[str, object]:
        materialize_calls.append(dict(kwargs))
        return {
            "snapshot_id": "daily-2026-09-17-fixed",
            "business_date": "2026-09-17",
            "payload_sha256": "b" * 64,
            "source_run_id": "local-daily:2026-09-17",
            "idempotent_replay": False,
        }

    def patch_bundle(*, foundation_exit: int = 0) -> None:
        monkeypatch.setattr(
            run_local_daily, "run_source_automation",
            lambda args, output_dir, business_date="": {"exit_code": 0, "fresh_output": True},
        )
        monkeypatch.setattr(
            run_local_daily, "run_quality_gate",
            lambda args, output_dir, business_date="": {"exit_code": 0, "output": "q", "overall_status": "success"},
        )
        (output_dir / "source-automation").mkdir(parents=True, exist_ok=True)
        (output_dir / "source-automation" / "source-automation-latest.json").write_text(
            json.dumps({"status": "completed", "critical_failures": [], "after": {}}), encoding="utf-8"
        )
        (output_dir / "source-automation" / "delivery-data-quality.json").write_text(
            json.dumps({"overall_status": "success", "gates": [], "alerts": []}), encoding="utf-8"
        )

        def fake_foundation(args, output_dir, anchor_report=None):
            payload = {
                "status": "success",
                "exit_code": foundation_exit,
                "after": {"rag_chunks": 10, "graph_nodes": 10},
                "agent_trace": {
                    "execution_semantics": "materialized_only",
                    "job_count": 12,
                    "turn_count": 0,
                    "handoff_count": 0,
                    "accepted_handoffs": 0,
                    "report_agent_completed": False,
                    "report_artifact_id": "",
                },
            }
            foundation_dir = output_dir / "foundation"
            foundation_dir.mkdir(parents=True, exist_ok=True)
            (foundation_dir / "foundation-materialization-summary.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            return payload

        monkeypatch.setattr(run_local_daily, "run_foundation_materialization", fake_foundation)
        monkeypatch.setattr(
            run_local_daily, "run_seven_product_lifecycle",
            lambda args, output_dir, business_date="": {"status": "ready", "exit_code": 0, "warnings": []},
        )
        monkeypatch.setattr(
            run_local_daily, "run_seven_product_evaluation",
            # anchor_report kwarg added by the final backup-state reorder
            # (DISK-MODEL §7: issue -> anchor -> foundation -> evaluation).
            lambda args, output_dir, issued_business_date, anchor_report=None: {
                "status": "passed", "exit_code": 0, "fresh_output": True, "evidence_valid": True,
                "database_unchanged_during_run": True, "forecast_source": "issued_ledger",
                "cutoff_matches_forecast": True, "passed_count": 21,
                "evaluation_input_mode": "consistent_snapshot_after_issue",
                "forecast": {"business_date": "2026-09-17"},
            },
        )
        monkeypatch.setattr(run_local_daily, "inspect_database", lambda db_path: {})
        monkeypatch.setattr(
        run_local_daily, "verify_backup", lambda db_path, business_date="": {"integrity_check": "ok"}
    )
        monkeypatch.setattr(run_local_daily, "write_reports", lambda output_dir, readiness: None)
        monkeypatch.setattr(run_local_daily, "dispatch_alerts", lambda output_dir: {"exit_code": 0})
        monkeypatch.setattr(run_local_daily, "write_launchd_templates", lambda output_dir: None)
        monkeypatch.setattr(run_local_daily, "materialize_daily_judgement", fake_materialize)

    args = run_local_daily.parse_args(
        [
            "--apply", "--skip-health",
            "--db", str(tmp_path / "agent.db"),
            "--codex-run", str(tmp_path / ".codex-run"),
            "--output-dir", str(output_dir),
        ]
    )

    patch_bundle(foundation_exit=0)
    exit_code = run_local_daily.run_daily_with_lock(args, output_dir=output_dir)
    status = json.loads((output_dir / "latest-status.json").read_text(encoding="utf-8"))
    assert exit_code == 0
    assert status["overall_status"] in {"ready", "ready_with_warnings"}
    assert status["daily_judgement_snapshot"]["status"] == "materialized"
    assert len(materialize_calls) == 1
    assert materialize_calls[0]["daily_chain_result"]["overall_status"] == status["overall_status"]

    # A blocked daily chain must not touch the snapshot writer at all.
    materialize_calls.clear()
    patch_bundle(foundation_exit=1)
    exit_code = run_local_daily.run_daily_with_lock(args, output_dir=output_dir)
    status = json.loads((output_dir / "latest-status.json").read_text(encoding="utf-8"))
    assert exit_code == 1
    assert status["overall_status"] == "blocked"
    assert status["daily_judgement_snapshot"]["status"] == "skipped"
    assert materialize_calls == []


def test_backup_includes_committed_wal_without_checkpoint(tmp_path: Path) -> None:
    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE market_observations (value TEXT)")
        writer.commit()
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        writer.execute("INSERT INTO market_observations VALUES ('committed-in-wal')")
        writer.commit()
        assert Path(f"{database}-wal").stat().st_size > 0
    report = run_local_daily.verify_backup(database, business_date="2026-09-17")
    assert report["backup_method"] == "sqlite_online_backup"
    assert report["integrity_check"] == "ok"
    assert report["restore_core_counts"]["market_observations"] == 1
    with closing(sqlite3.connect(report["backup_path"])) as backup:
        assert backup.execute("SELECT value FROM market_observations").fetchone()[0] == "committed-in-wal"


# ---------------------------------------------------------------------------
# Post-snapshot LLM steps (DESIGN §2.6 方案A，批次2/5 链内接线)。
# ---------------------------------------------------------------------------


def _post_snapshot_args(tmp_path: Path, *, apply: bool = True) -> object:
    return run_local_daily.parse_args(
        [
            "--apply" if apply else "--dry-run",
            "--skip-health",
            "--db", str(tmp_path / "agent.db"),
            "--codex-run", str(tmp_path / ".codex-run"),
            "--output-dir", str(tmp_path / "local-production"),
        ]
    )


def _ready_readiness() -> dict[str, object]:
    return {
        "overall_status": "ready",
        "started_at": "2026-09-17T01:30:00+00:00",
        "finished_at": "2026-09-17T01:38:12+00:00",
        "warnings": [],
    }


def test_post_snapshot_steps_skip_in_dry_run(tmp_path: Path, monkeypatch) -> None:
    from app import counter_scan as counter_scan_module
    from app import daily_interpretation as interpretation_module

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("post-snapshot agents must not run in dry-run")

    monkeypatch.setattr(counter_scan_module, "run_counter_scan", unexpected)
    monkeypatch.setattr(interpretation_module, "run_daily_interpretation", unexpected)
    args = _post_snapshot_args(tmp_path, apply=False)

    scan = run_local_daily.run_post_snapshot_counter_scan(args, readiness=_ready_readiness())
    interpretation = run_local_daily.run_post_snapshot_daily_interpretation(args, readiness=_ready_readiness())

    assert scan == {"step": "counter_scan", "status": "skipped", "reason": "dry_run"}
    assert interpretation == {"step": "daily_interpretation", "status": "skipped", "reason": "dry_run"}


def test_post_snapshot_steps_honor_env_kill_switch(tmp_path: Path, monkeypatch) -> None:
    from app import counter_scan as counter_scan_module
    from app import daily_interpretation as interpretation_module

    def unexpected(*args: object, **kwargs: object) -> None:
        raise AssertionError("kill switch must short-circuit before any agent work")

    monkeypatch.setattr(counter_scan_module, "run_counter_scan", unexpected)
    monkeypatch.setattr(interpretation_module, "run_daily_interpretation", unexpected)
    monkeypatch.setenv("AI_COUNTER_SCAN_ENABLED", "false")
    monkeypatch.setenv("AI_DAILY_INTERPRETATION_ENABLED", "0")
    args = _post_snapshot_args(tmp_path)

    scan = run_local_daily.run_post_snapshot_counter_scan(args, readiness=_ready_readiness())
    interpretation = run_local_daily.run_post_snapshot_daily_interpretation(args, readiness=_ready_readiness())

    assert scan["status"] == "skipped" and scan["reason"] == "disabled_by_env"
    assert interpretation["status"] == "skipped" and interpretation["reason"] == "disabled_by_env"


def test_post_snapshot_step_summarizes_completed_run(tmp_path: Path, monkeypatch) -> None:
    from app import counter_scan as counter_scan_module

    async def fake_scan(**kwargs: object) -> dict[str, object]:
        assert kwargs["business_date"] == "2026-09-17"
        assert kwargs["chain_status"] == "ready"
        assert kwargs["dry_run"] is False
        return {
            "status": "completed",
            "scan_outcome": "counter_evidence_found",
            "artifact_path": "/tmp/scan.json",
            "llm_cost_cny": 0.0088,
            "timings": {"total_ms": 8600},
            "findings": ["f1", "f2"],
        }

    monkeypatch.setattr(counter_scan_module, "run_counter_scan", fake_scan)
    args = _post_snapshot_args(tmp_path)

    summary = run_local_daily.run_post_snapshot_counter_scan(args, readiness=_ready_readiness())

    assert summary["status"] == "completed"
    assert summary["scan_outcome"] == "counter_evidence_found"
    assert summary["total_ms"] == 8600
    # Bulky fields stay in the artifact, not in readiness.
    assert "findings" not in summary


def test_post_snapshot_step_exception_is_captured_not_raised(tmp_path: Path, monkeypatch) -> None:
    from app import daily_interpretation as interpretation_module

    async def exploding(**kwargs: object) -> dict[str, object]:
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(interpretation_module, "run_daily_interpretation", exploding)
    args = _post_snapshot_args(tmp_path)

    summary = run_local_daily.run_post_snapshot_daily_interpretation(args, readiness=_ready_readiness())

    assert summary["status"] == "degraded"
    assert "RuntimeError" in summary["failure_reason"]


def test_run_daily_with_lock_keeps_exit_zero_when_post_snapshot_agents_fail(
    tmp_path: Path, monkeypatch
) -> None:
    """链内接线端到端：两步 LLM 全失败也不影响 overall/exit code（DESIGN 2d）。"""
    from app import counter_scan as counter_scan_module
    from app import daily_interpretation as interpretation_module

    output_dir = tmp_path / "local-production"
    output_dir.mkdir(parents=True)

    scan_calls: list[dict[str, object]] = []

    async def failing_scan(**kwargs: object) -> dict[str, object]:
        scan_calls.append(dict(kwargs))
        return {"status": "degraded", "failure_reason": "provider_timeout", "timings": {"total_ms": 90000}}

    async def failing_interpretation(**kwargs: object) -> dict[str, object]:
        return {"status": "degraded", "failure_reason": "budget_exhausted", "timings": {"total_ms": 10}}

    monkeypatch.setattr(counter_scan_module, "run_counter_scan", failing_scan)
    monkeypatch.setattr(interpretation_module, "run_daily_interpretation", failing_interpretation)

    def fake_materialize(**kwargs: object) -> dict[str, object]:
        return {
            "snapshot_id": "daily-2026-09-17-x",
            "business_date": "2026-09-17",
            "payload_sha256": "a" * 64,
            "source_run_id": "local-daily:2026-09-17",
            "idempotent_replay": False,
        }

    monkeypatch.setattr(
        run_local_daily, "run_source_automation",
        lambda args, output_dir, business_date="": {"exit_code": 0, "fresh_output": True},
    )
    monkeypatch.setattr(
        run_local_daily, "run_quality_gate",
        lambda args, output_dir, business_date="": {"exit_code": 0, "output": "q", "overall_status": "success"},
    )
    (output_dir / "source-automation").mkdir(parents=True, exist_ok=True)
    (output_dir / "source-automation" / "source-automation-latest.json").write_text(
        json.dumps({"status": "completed", "critical_failures": [], "after": {}}), encoding="utf-8"
    )
    (output_dir / "source-automation" / "delivery-data-quality.json").write_text(
        json.dumps({"overall_status": "success", "gates": [], "alerts": []}), encoding="utf-8"
    )

    def fake_foundation(args, output_dir, anchor_report=None):
        return {
            "status": "success",
            "exit_code": 0,
            "after": {"rag_chunks": 10, "graph_nodes": 10},
            "agent_trace": {
                "execution_semantics": "materialized_only",
                "job_count": 12, "turn_count": 0, "handoff_count": 0,
                "accepted_handoffs": 0, "report_agent_completed": False, "report_artifact_id": "",
            },
        }

    monkeypatch.setattr(run_local_daily, "run_foundation_materialization", fake_foundation)
    monkeypatch.setattr(
        run_local_daily, "run_seven_product_lifecycle",
        lambda args, output_dir, business_date="": {"status": "ready", "exit_code": 0, "warnings": []},
    )
    monkeypatch.setattr(
        run_local_daily, "run_seven_product_evaluation",
        # anchor_report kwarg added by the final backup-state reorder (DISK-MODEL §7).
        lambda args, output_dir, issued_business_date, anchor_report=None: {
            "status": "passed", "exit_code": 0, "fresh_output": True, "evidence_valid": True,
            "database_unchanged_during_run": True, "forecast_source": "issued_ledger",
            "cutoff_matches_forecast": True, "passed_count": 21,
            "evaluation_input_mode": "consistent_snapshot_after_issue",
            "forecast": {"business_date": "2026-09-17"},
        },
    )
    monkeypatch.setattr(run_local_daily, "inspect_database", lambda db_path: {})
    monkeypatch.setattr(
        run_local_daily, "verify_backup", lambda db_path, business_date="": {"integrity_check": "ok"}
    )
    monkeypatch.setattr(run_local_daily, "write_reports", lambda output_dir, readiness: None)
    monkeypatch.setattr(run_local_daily, "dispatch_alerts", lambda output_dir: {"exit_code": 0})
    monkeypatch.setattr(run_local_daily, "write_launchd_templates", lambda output_dir: None)
    monkeypatch.setattr(run_local_daily, "materialize_daily_judgement", fake_materialize)

    args = _post_snapshot_args(tmp_path)
    exit_code = run_local_daily.run_daily_with_lock(args, output_dir=output_dir)
    status = json.loads((output_dir / "latest-status.json").read_text(encoding="utf-8"))

    assert exit_code == 0  # degraded agents never fail the daily run
    assert status["overall_status"] in {"ready", "ready_with_warnings"}
    assert status["counter_scan"]["status"] == "degraded"
    assert status["daily_interpretation"]["status"] == "degraded"
    assert any(str(item).startswith("counter scan degraded") for item in status["warnings"])
    assert any(str(item).startswith("daily interpretation degraded") for item in status["warnings"])
    # The scan saw the real chain status and the business date derived from the
    # run's own started_at clock (Asia/Shanghai), not a hardcoded value.
    assert scan_calls[0]["chain_status"] == status["overall_status"]
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI

    expected_business_date = _dt.fromisoformat(
        str(status["started_at"]).replace("Z", "+00:00")
    ).astimezone(_ZI("Asia/Shanghai")).date().isoformat()
    assert scan_calls[0]["business_date"] == expected_business_date


def test_direction_upstream_timeout_surfaces_in_daily_warnings():
    result = build_readiness_for_foundation({
        'execution_semantics': 'materialized_only', 'job_count': 12,
        'upstream_gate': {'status': 'timeout', 'reason_code': 'summaries_pending'},
    })
    assert any(warning == 'direction review skipped: upstream gate timeout (summaries_pending)'
               for warning in result['warnings'])


def test_foundation_timeout_covers_gate_budget():
    assert run_local_daily.FOUNDATION_STEP_TIMEOUT_SECONDS >= (
        run_local_daily.SUBPROCESS_TIMEOUT_SECONDS
        + run_local_daily.DIRECTION_UPSTREAM_GATE_MAX_WAIT_SECONDS + 60)

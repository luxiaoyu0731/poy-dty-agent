from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = SERVER_ROOT / "scripts"
SCRIPT_PATH = SCRIPTS_ROOT / "run_source_automation.py"
for entry in (str(SERVER_ROOT), str(SCRIPTS_ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
SPEC = importlib.util.spec_from_file_location("run_source_automation_under_test", SCRIPT_PATH)
assert SPEC is not None
source_automation = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["run_source_automation_under_test"] = source_automation
SPEC.loader.exec_module(source_automation)


def test_legacy_ccf_import_failure_cannot_degrade_current_run() -> None:
    result = source_automation.evaluate_run_status(
        public_fetch={"items": []},
        ccf_import={"exit_code": 1},
        news_fetch={"status": "completed"},
        quality={"overall_status": "success"},
    )

    assert result["status"] == "completed"
    assert result["critical_failures"] == []
    assert result["degraded_components"] == []


def test_critical_public_source_failure_still_blocks_run() -> None:
    result = source_automation.evaluate_run_status(
        public_fetch={"items": [{"source_id": "eia_petroleum_api", "status": "error", "criticality": "critical"}]},
        ccf_import={"exit_code": 0},
        news_fetch={"status": "completed"},
        quality={"overall_status": "success"},
    )

    assert result["status"] == "blocked"
    assert result["critical_failures"] == ["eia_petroleum_api"]


def test_blocked_quality_gate_still_blocks_run() -> None:
    result = source_automation.evaluate_run_status(
        public_fetch={"items": []},
        ccf_import={"exit_code": 0},
        news_fetch={"status": "completed"},
        quality={"overall_status": "blocked"},
    )

    assert result["status"] == "blocked"
    assert result["critical_failures"] == []


def test_rejected_user_file_degrades_but_does_not_block_run() -> None:
    result = source_automation.evaluate_run_status(
        public_fetch={"items": []},
        ccf_import={"exit_code": 0},
        news_fetch={"status": "completed"},
        quality={"overall_status": "success"},
        user_files={"status": "degraded", "rejected_files": 1},
    )

    assert result["status"] == "degraded"
    assert result["critical_failures"] == []
    assert result["degraded_components"] == ["user_files"]


def test_short_cadence_benchmark_skip_is_not_a_failure() -> None:
    result = source_automation.evaluate_run_status(
        public_fetch={
            "items": [],
            "public_benchmark_refresh": {"status": "skipped", "reason": "short_cadence_structured_sources_only"},
        },
        ccf_import={"exit_code": 0},
        news_fetch={"status": "completed"},
        quality={"overall_status": "success"},
    )

    assert result["status"] == "completed"
    assert result["critical_failures"] == []


def test_structured_source_selection_is_repeatable_and_explicit() -> None:
    args = source_automation.parse_args(
        [
            "--fetch-public",
            "--force-public",
            "--public-source-id",
            "ofac_sanctions",
            "--public-source-id",
            "un_comtrade_api",
        ]
    )
    assert args.public_source_id == ["ofac_sanctions", "un_comtrade_api"]


def test_reported_fetch_errors_redact_query_credentials() -> None:
    error = RuntimeError("429 for https://example.test/data?api_key=super-secret-value&facet=x")
    rendered = source_automation._safe_exception(error)
    assert "super-secret-value" not in rendered
    assert "api_key=[redacted]" in rendered


def test_backup_defaults_match_disk_consolidation_retention_and_reuse() -> None:
    """DISK-MODEL §3.4 项3 -> §7 终版: retention 3 / reuse 24h only govern the
    emergency ``--backup-db`` path now; by default apply runs take no pre-write
    backup at all (covered by the unified daily anchor)."""
    args = source_automation.parse_args([])

    assert args.backup_retention_count == 3
    assert args.backup_reuse_seconds == 86400


def test_prewrite_backup_is_off_by_default_and_enabled_by_env_or_flag(monkeypatch) -> None:
    """Final backup state (DISK-MODEL §7): pre-write backups default to off;
    SOURCE_PREWRITE_BACKUP=1 (emergency switch) or --backup-db restores them."""
    import argparse

    monkeypatch.delenv(source_automation.SOURCE_PREWRITE_BACKUP_ENV, raising=False)
    assert source_automation.prewrite_backup_enabled(argparse.Namespace(backup_db=False)) is False

    monkeypatch.setenv(source_automation.SOURCE_PREWRITE_BACKUP_ENV, "1")
    assert source_automation.prewrite_backup_enabled(argparse.Namespace(backup_db=False)) is True

    monkeypatch.delenv(source_automation.SOURCE_PREWRITE_BACKUP_ENV, raising=False)
    assert source_automation.prewrite_backup_enabled(argparse.Namespace(backup_db=True)) is True


def test_apply_run_proceeds_without_pre_write_backup_and_reports_anchor_coverage(
    tmp_path: Path, monkeypatch
) -> None:
    """DISK-MODEL §7: an apply run must succeed without any backup_database call,
    keep backup_path empty, and mark guards as covered by the daily anchor."""
    import json
    import sqlite3
    from contextlib import closing

    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE market_observations (value TEXT)")
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    backup_calls: list[object] = []
    monkeypatch.delenv(source_automation.SOURCE_PREWRITE_BACKUP_ENV, raising=False)
    monkeypatch.setattr(
        source_automation,
        "backup_database",
        lambda *args, **kwargs: backup_calls.append((args, kwargs)) or tmp_path / "unused.sqlite",
    )
    monkeypatch.setattr(source_automation, "expire_stale_news_fetch_runs", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        source_automation, "build_source_automation_status", lambda **kwargs: {"tasks": []}
    )

    async def fake_news_fetches(args):
        return {"status": "completed", "writes_database": True}

    monkeypatch.setattr(source_automation, "run_news_fetches", fake_news_fetches)
    args = source_automation.parse_args(
        [
            "--db",
            str(database),
            "--codex-run",
            str(tmp_path / "codex-run"),
            "--output-dir",
            str(tmp_path / "out"),
            "--apply",
            "--fetch-news",
        ]
    )

    exit_code = source_automation.run_with_lock(args)

    assert exit_code == 0
    assert backup_calls == []
    summary = json.loads((tmp_path / "out" / "source-automation-latest.json").read_text(encoding="utf-8"))
    assert summary["status"] == "completed"
    assert summary["backup_path"] == ""
    assert summary["guards"]["backup_required_before_apply"] is False
    assert summary["guards"]["backup_covered_by"] == "daily_anchor"
    assert summary["guards"]["backup_created"] is False


def test_prewrite_backup_env_switch_restores_emergency_backup(tmp_path: Path, monkeypatch) -> None:
    """SOURCE_PREWRITE_BACKUP=1 restores the legacy pre-write full backup."""
    import json
    import sqlite3
    from contextlib import closing

    database = tmp_path / "agent.db"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE market_observations (value TEXT)")
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    backup_path = tmp_path / "emergency.sqlite"
    backup_path.write_bytes(b"")
    backup_calls: list[object] = []
    monkeypatch.setenv(source_automation.SOURCE_PREWRITE_BACKUP_ENV, "1")
    monkeypatch.setattr(
        source_automation,
        "backup_database",
        lambda *args, **kwargs: backup_calls.append((args, kwargs)) or backup_path,
    )
    monkeypatch.setattr(source_automation, "expire_stale_news_fetch_runs", lambda *args, **kwargs: 0)
    monkeypatch.setattr(
        source_automation, "build_source_automation_status", lambda **kwargs: {"tasks": []}
    )

    async def fake_news_fetches(args):
        return {"status": "completed", "writes_database": True}

    monkeypatch.setattr(source_automation, "run_news_fetches", fake_news_fetches)
    args = source_automation.parse_args(
        [
            "--db",
            str(database),
            "--codex-run",
            str(tmp_path / "codex-run"),
            "--output-dir",
            str(tmp_path / "out"),
            "--apply",
            "--fetch-news",
        ]
    )

    exit_code = source_automation.run_with_lock(args)

    assert exit_code == 0
    assert len(backup_calls) == 1
    summary = json.loads((tmp_path / "out" / "source-automation-latest.json").read_text(encoding="utf-8"))
    assert summary["backup_path"] == str(backup_path)
    assert summary["guards"]["backup_created"] is True
    assert summary["guards"]["backup_required_before_apply"] is False

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from app import storage
from app.settings import settings

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "run_seven_product_forecast_lifecycle.py"
SPEC = importlib.util.spec_from_file_location("run_seven_product_forecast_lifecycle", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
lifecycle_runner = importlib.util.module_from_spec(SPEC)
sys.modules["run_seven_product_forecast_lifecycle"] = lifecycle_runner
SPEC.loader.exec_module(lifecycle_runner)


def _write_chain_report(tmp_path: Path) -> Path:
    payload = {
        "schema_version": "event-agent-chain-report.v1",
        "business_date": "2026-09-02",
        "status": "ok",
        "as_of_time": "2026-09-02T07:50:00+00:00",
        "product_factors": {},
        "surviving_event_ids": [],
        "artifacts": [],
        "counters": {},
    }
    path = tmp_path / "event-agent-chain-latest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_lifecycle_apply_is_daily_idempotent_and_dry_run_does_not_append(tmp_path: Path) -> None:
    original_env = os.environ["SQLITE_PATH"]
    original_setting = settings.sqlite_path
    database = tmp_path / "lifecycle.db"
    output = tmp_path / "reports"
    apply_args = [
        "--apply",
        "--db",
        str(database),
        "--output-dir",
        str(output),
        "--as-of",
        datetime.now(UTC).isoformat(),
    ]

    try:
        assert lifecycle_runner.main(apply_args) == 0
        assert lifecycle_runner.main(apply_args) == 0
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
            assert connection.execute("SELECT COUNT(*) FROM seven_product_forecast_batches").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM seven_product_forecast_cells").fetchone()[0] == 21

        assert lifecycle_runner.main(
            [
                "--dry-run",
                "--db",
                str(database),
                "--output-dir",
                str(output),
                "--as-of",
                "2026-09-01T08:20:00+00:00",
            ]
        ) == 0
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM seven_product_forecast_batches").fetchone()[0] == 1

        # ADR-9 (2026-10-02): a passed chain report must surface its real fusion
        # outcome in the daily report — the no_chain_report sentinel may only
        # appear when no report was given (the clobber regression hid real fusions).
        fusion_output = tmp_path / "reports-fusion"
        fusion_args = [
            "--apply",
            "--db",
            str(tmp_path / "lifecycle-fusion.db"),
            "--output-dir",
            str(fusion_output),
            "--as-of",
            "2026-09-02T08:00:00+00:00",
            "--event-chain-report",
            str(_write_chain_report(tmp_path)),
        ]
        assert lifecycle_runner.main(fusion_args) == 0
        fusion_report = json.loads(
            (fusion_output / "seven-product-lifecycle-latest.json").read_text(encoding="utf-8")
        )
        assert fusion_report["event_fusion"]["status"] != "skipped"
        assert fusion_report["event_fusion"].get("reason") != "no_chain_report"

        report = json.loads((output / "seven-product-lifecycle-latest.json").read_text(encoding="utf-8"))
        assert report["mode"] == "dry_run"
        assert report["status"] == "ready_with_warnings"
        assert report["forecast"]["write_action"] == "would_append"
        assert len(report["report_sha256"]) == 64
        assert (output.stat().st_mode & 0o777) == 0o700
        assert ((output / "seven-product-lifecycle-latest.json").stat().st_mode & 0o777) == 0o600
    finally:
        os.environ["SQLITE_PATH"] = original_env
        object.__setattr__(settings, "sqlite_path", original_setting)
        storage._MIGRATED_PATHS.clear()


def test_lifecycle_warns_when_chain_report_unreadable(tmp_path: Path) -> None:
    """A missing/unreadable chain report must surface as a warning, never as
    a silent baseline-only issuance indistinguishable from "chain never ran"."""
    original_env = os.environ["SQLITE_PATH"]
    original_setting = settings.sqlite_path
    database = tmp_path / "lifecycle-unreadable-chain.db"
    output = tmp_path / "reports-unreadable-chain"
    corrupt = tmp_path / "event-agent-chain-latest.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    args = [
        "--apply",
        "--db",
        str(database),
        "--output-dir",
        str(output),
        "--as-of",
        datetime.now(UTC).isoformat(),
        "--event-chain-report",
        str(corrupt),
    ]

    try:
        assert lifecycle_runner.main(args) == 0
        report = json.loads(
            (output / "seven-product-lifecycle-latest.json").read_text(encoding="utf-8")
        )
        assert report["status"] == "ready_with_warnings"
        assert any("event chain report unreadable" in warning for warning in report["warnings"])
        assert report["event_fusion"]["status"] == "skipped"
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM seven_product_forecast_batches").fetchone()[0] == 1
    finally:
        os.environ["SQLITE_PATH"] = original_env
        object.__setattr__(settings, "sqlite_path", original_setting)
        storage._MIGRATED_PATHS.clear()

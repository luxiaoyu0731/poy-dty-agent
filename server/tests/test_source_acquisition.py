from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import source_acquisition
from app.fetchers import FetchResult
from app.settings import settings
from app.source_acquisition import build_source_automation_status, record_acquisition_run

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "run_source_automation.py"
SPEC = importlib.util.spec_from_file_location("run_source_automation", SCRIPT_PATH)
assert SPEC is not None
run_source_automation = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["run_source_automation"] = run_source_automation
SPEC.loader.exec_module(run_source_automation)


def _run_source_automation_main(argv: list[str]) -> int:
    sqlite_path_was_present = "SQLITE_PATH" in os.environ
    original_environment_path = os.environ.get("SQLITE_PATH")
    original_settings_path = settings.sqlite_path
    try:
        return run_source_automation.main(argv)
    finally:
        if sqlite_path_was_present:
            assert original_environment_path is not None
            os.environ["SQLITE_PATH"] = original_environment_path
        else:
            os.environ.pop("SQLITE_PATH", None)
        object.__setattr__(settings, "sqlite_path", original_settings_path)


def test_run_source_automation_main_restores_sqlite_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sqlite_path_was_present = "SQLITE_PATH" in os.environ
    original_environment_path = os.environ.get("SQLITE_PATH")
    original_settings_path = settings.sqlite_path
    sentinel = 907_531

    def fake_main(_argv: list[str]) -> int:
        os.environ["SQLITE_PATH"] = str(tmp_path / "mutated-environment.db")
        object.__setattr__(settings, "sqlite_path", str(tmp_path / "mutated-settings.db"))
        return sentinel

    try:
        monkeypatch.setattr(run_source_automation, "main", fake_main)

        result = _run_source_automation_main(["--restore-contract"])

        assert result == sentinel
        assert ("SQLITE_PATH" in os.environ) is sqlite_path_was_present
        assert os.environ.get("SQLITE_PATH") == original_environment_path
        assert settings.sqlite_path == original_settings_path
    finally:
        if sqlite_path_was_present:
            assert original_environment_path is not None
            os.environ["SQLITE_PATH"] = original_environment_path
        else:
            os.environ.pop("SQLITE_PATH", None)
        object.__setattr__(settings, "sqlite_path", original_settings_path)


def test_run_source_automation_main_restores_sqlite_configuration_after_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_sqlite_path_was_present = "SQLITE_PATH" in os.environ
    session_environment_path = os.environ.get("SQLITE_PATH")
    original_settings_path = settings.sqlite_path
    failure = RuntimeError("fixed source automation failure")

    def fake_main(_argv: list[str]) -> int:
        os.environ["SQLITE_PATH"] = str(tmp_path / "mutated-environment.db")
        object.__setattr__(settings, "sqlite_path", str(tmp_path / "mutated-settings.db"))
        raise failure

    try:
        os.environ.pop("SQLITE_PATH", None)
        monkeypatch.setattr(run_source_automation, "main", fake_main)

        with pytest.raises(RuntimeError) as caught:
            _run_source_automation_main(["--raise-fixed-error"])

        assert caught.value is failure
        assert str(caught.value) == "fixed source automation failure"
        assert "SQLITE_PATH" not in os.environ
        assert settings.sqlite_path == original_settings_path
    finally:
        if session_sqlite_path_was_present:
            assert session_environment_path is not None
            os.environ["SQLITE_PATH"] = session_environment_path
        else:
            os.environ.pop("SQLITE_PATH", None)
        object.__setattr__(settings, "sqlite_path", original_settings_path)


def test_source_automation_status_soft_removes_ccf_tasks_but_keeps_legacy_inventory(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE forecast_price_points (
              source_id TEXT,
              dataset_type TEXT,
              observed_at TEXT,
              product TEXT,
              spec TEXT,
              price REAL
            );
            INSERT INTO forecast_price_points VALUES
              ('ccf_dom_daily', 'ccf_spot', '2026-06-29', 'POY', 'POY 150D/48F', 7200),
              ('ccf_dom_daily', 'ccf_spot', '2026-06-29', 'POY', 'POY 150D/144F', 7210),
              ('ccf_dom_daily', 'ccf_spot', '2026-06-29', 'POY', 'POY 75D/36F', 7300),
              ('ccf_dom_daily', 'ccf_spot', '2026-06-29', 'POY', 'POY 75D/72F', 7310);
            """)
    status = build_source_automation_status(db_path=db_path, codex_run=tmp_path / ".codex-run")
    assert all(not str(task["source_id"]).startswith("ccf") for task in status["tasks"])
    assert status["legacy_sources"]["ccf"]["status"] == "soft_removed"
    assert status["legacy_sources"]["ccf"]["writes_database"] is False
    assert status["inventory"]["ccf_price"]["POY"]["rows"] == 4


def test_source_automation_status_soft_removes_dce_and_reports_sunsirs_meg_label(tmp_path: Path) -> None:
    status = build_source_automation_status(db_path=tmp_path / "missing.db", codex_run=tmp_path / ".codex-run")

    assert all(task["source_id"] != "dce_meg" for task in status["tasks"])
    assert status["legacy_sources"]["dce_meg"] == {
        "status": "soft_removed",
        "historical_rows_read_only": True,
        "scheduled": False,
        "writes_database": False,
        "current_target_gap": None,
        "current_target_replacement": "sunsirs_public_commodity_assessment",
    }
    assert status["guards"]["meg_current_source_gap"] is False
    assert status["guards"]["meg_current_label_source"] == "sunsirs_public_commodity_assessment"


def test_public_source_task_keeps_successful_check_visible_when_refresh_is_due(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "agent.db"
    checked_at = datetime.now(UTC) - timedelta(hours=4)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE source_fetch_audit (
              source_id TEXT, status TEXT, created_at TEXT
            );
            CREATE TABLE market_observations (
              source_id TEXT, observed_at TEXT
            );
            """)
        connection.execute(
            "INSERT INTO source_fetch_audit VALUES (?, ?, ?)",
            ("eia_petroleum_api", "no_new_data", checked_at.isoformat()),
        )
        connection.execute(
            "INSERT INTO market_observations VALUES (?, ?)",
            ("eia_petroleum_api", datetime.now(UTC).date().isoformat()),
        )
    monkeypatch.setattr(
        source_acquisition,
        "list_sources",
        lambda: [
            SimpleNamespace(
                source_id="eia_petroleum_api",
                source_name="EIA",
                auth_type="none",
                category="energy",
                products=["crude_oil"],
            )
        ],
    )

    task = source_acquisition._public_source_tasks(source_acquisition.inspect_source_inventory(db_path), db_path)[0]

    assert task["status"] == "due"
    assert task["last_run_status"] == "no_new_data"
    assert task["last_run_succeeded"] is True
    assert task["refresh_due"] is True


def test_due_full_automation_is_not_reported_as_manual_or_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = [
        {
            "source_id": "fred_macro_api",
            "status": "due",
            "automation_level": "full",
            "requires_human_action": False,
        },
        {
            "source_id": "sunsirs_mx_east_china_daily_assessment",
            "status": "blocked",
            "automation_level": "manual_config",
            "requires_human_action": True,
        },
    ]
    monkeypatch.setattr(source_acquisition, "inspect_source_inventory", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(source_acquisition, "build_acquisition_plan", lambda **_kwargs: tasks)

    status = build_source_automation_status(db_path=tmp_path / "missing.db", codex_run=tmp_path)

    assert status["not_ready"] == 2
    assert status["manual_or_blocked"] == 1
    summary = run_source_automation.summarize_status(status)
    assert summary["not_ready"] == 2
    assert summary["non_ccf_manual_or_blocked"] == 1


def test_czce_readiness_uses_official_futures_daily_bars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "agent.db"
    checked_at = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE source_fetch_audit (
              source_id TEXT, status TEXT, created_at TEXT
            );
            CREATE TABLE futures_daily_bars (
              source_id TEXT, trade_date TEXT
            );
            """)
        connection.execute(
            "INSERT INTO source_fetch_audit VALUES (?, ?, ?)",
            ("czce_pta_px", "ok", checked_at),
        )
        connection.execute(
            "INSERT INTO futures_daily_bars VALUES (?, ?)",
            ("czce_pta_px", datetime.now(UTC).date().isoformat()),
        )
    monkeypatch.setattr(
        source_acquisition,
        "list_sources",
        lambda: [
            SimpleNamespace(
                source_id="czce_pta_px",
                source_name="CZCE",
                auth_type="none",
                category="futures_daily",
                products=["pta", "px"],
            )
        ],
    )

    inventory = source_acquisition.inspect_source_inventory(db_path)
    task = source_acquisition._public_source_tasks(inventory, db_path)[0]

    assert inventory["futures_daily_bars"]["czce_pta_px"]["rows"] == 1
    assert task["status"] == "success"
    assert task["target_table"] == "futures_daily_bars"
    assert task["coverage_scope"].find(datetime.now(UTC).date().isoformat()) >= 0


def test_historical_context_readiness_uses_capture_success_not_historical_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "agent.db"
    checked_at = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE source_fetch_audit (
              source_id TEXT, status TEXT, created_at TEXT
            );
            CREATE TABLE market_observations (
              source_id TEXT, observed_at TEXT
            );
            """)
        connection.execute(
            "INSERT INTO source_fetch_audit VALUES (?, ?, ?)",
            ("un_comtrade_api", "unchanged", checked_at),
        )
        connection.execute(
            "INSERT INTO market_observations VALUES (?, ?)",
            ("un_comtrade_api", "2024-12-01"),
        )
    monkeypatch.setattr(
        source_acquisition,
        "list_sources",
        lambda: [
            SimpleNamespace(
                source_id="un_comtrade_api",
                source_name="UN Comtrade",
                auth_type="api_key",
                category="trade_flow",
                products=["trade_flow"],
                data_role="historical_context",
                current_formal_eligible=False,
            )
        ],
    )

    task = source_acquisition._public_source_tasks(source_acquisition.inspect_source_inventory(db_path), db_path)[0]

    assert task["status"] == "success"
    assert task["current_formal_eligible"] is False
    assert checked_at in task["coverage_scope"]


def test_ccf_price_workday_requires_fresh_capture_csv(tmp_path: Path, monkeypatch) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 7, 7)

    monkeypatch.setattr(source_acquisition, "date", FixedDate)
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE forecast_price_points (
              source_id TEXT,
              dataset_type TEXT,
              observed_at TEXT,
              product TEXT,
              spec TEXT,
              price REAL
            );
            INSERT INTO forecast_price_points VALUES
              ('ccf_dom_daily', 'ccf_spot', '2026-07-04', 'POY', 'POY 150D/48F', 7200),
              ('ccf_dom_daily', 'ccf_spot', '2026-07-04', 'POY', 'POY 150D/144F', 7210),
              ('ccf_dom_daily', 'ccf_spot', '2026-07-04', 'POY', 'POY 75D/36F', 7300),
              ('ccf_dom_daily', 'ccf_spot', '2026-07-04', 'POY', 'POY 75D/72F', 7310);
            """)

    status = build_source_automation_status(db_path=db_path, codex_run=tmp_path / ".codex-run")
    assert all(not str(task["source_id"]).startswith("ccf") for task in status["tasks"])
    assert status["legacy_sources"]["ccf"]["scheduled"] is False


def test_ccf_industry_workday_requires_fresh_capture_not_old_csv(tmp_path: Path, monkeypatch) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 7, 7)

    monkeypatch.setattr(source_acquisition, "date", FixedDate)
    db_path = tmp_path / "agent.db"
    capture_dir = tmp_path / ".codex-run" / "ccf-authorized-capture"
    capture_dir.mkdir(parents=True)
    stale_csv = capture_dir / "ccf_industry_observations.csv"
    stale_csv.write_text("metric,value\nx,1\n", encoding="utf-8")
    stale_mtime = datetime(2026, 6, 30, 8, 0, 0).timestamp()
    os.utime(stale_csv, (stale_mtime, stale_mtime))
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE industry_observations (
              created_at TEXT,
              source_id TEXT,
              observed_at TEXT,
              product TEXT,
              metric TEXT,
              value REAL,
              unit TEXT,
              frequency TEXT,
              notes TEXT,
              raw TEXT
            );
            INSERT INTO industry_observations VALUES
              (
                '2026-06-30T07:38:09+00:00', 'ccf_dom_daily', '2026-06-26',
                'POLYESTER', 'polyester_operating_rate', 80.0, '%', 'weekly',
                'captured_at=2026-06-30T07:36:44Z',
                '{"captured_at":"2026-06-30T07:36:44Z"}'
              );
            """)

    status = build_source_automation_status(db_path=db_path, codex_run=tmp_path / ".codex-run")
    assert all(not str(task["source_id"]).startswith("ccf") for task in status["tasks"])
    assert status["legacy_sources"]["ccf"]["status"] == "soft_removed"


def test_ccf_industry_workday_uses_today_csv_as_ready_to_import(tmp_path: Path, monkeypatch) -> None:
    class FixedDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 7, 7)

    monkeypatch.setattr(source_acquisition, "date", FixedDate)
    db_path = tmp_path / "agent.db"
    capture_dir = tmp_path / ".codex-run" / "ccf-authorized-capture"
    capture_dir.mkdir(parents=True)
    fresh_csv = capture_dir / "ccf_industry_observations.csv"
    fresh_csv.write_text("metric,value\nx,1\n", encoding="utf-8")
    fresh_mtime = datetime(2026, 7, 7, 8, 0, 0).timestamp()
    os.utime(fresh_csv, (fresh_mtime, fresh_mtime))
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE industry_observations (
              created_at TEXT,
              source_id TEXT,
              observed_at TEXT,
              product TEXT,
              metric TEXT,
              value REAL,
              unit TEXT,
              frequency TEXT,
              notes TEXT,
              raw TEXT
            );
            INSERT INTO industry_observations VALUES
              (
                '2026-06-30T07:38:09+00:00', 'ccf_dom_daily', '2026-06-26',
                'POLYESTER', 'polyester_operating_rate', 80.0, '%', 'weekly',
                'captured_at=2026-06-30T07:36:44Z',
                '{"captured_at":"2026-06-30T07:36:44Z"}'
              );
            """)

    status = build_source_automation_status(db_path=db_path, codex_run=tmp_path / ".codex-run")
    assert all(not str(task["source_id"]).startswith("ccf") for task in status["tasks"])
    assert status["legacy_sources"]["ccf"]["writes_database"] is False


def test_record_acquisition_run_persists_json_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    row = record_acquisition_run(
        db_path=db_path,
        source_id="ccf_dom_daily",
        task_name="CCF DTY 价格补数",
        dataset_type="ccf_spot",
        status="ready_for_computer_use",
        automation_level="semi",
        requires_computer_use=True,
        requires_human_action=False,
        target_table="forecast_price_points",
        expected_fields=["产品", "日期", "均价"],
        summary={"coverage_scope": "DTY"},
    )

    assert row["expected_fields"] == ["产品", "日期", "均价"]
    assert row["summary"]["coverage_scope"] == "DTY"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        count = connection.execute("SELECT COUNT(*) FROM source_acquisition_runs").fetchone()[0]
    assert count == 1


def test_source_automation_apply_no_longer_requires_a_pre_write_backup(tmp_path: Path) -> None:
    """Final backup state (DISK-MODEL §7, 2026-09-17): apply runs proceed without
    --backup-db; recovery is covered by the unified daily anchor, SQLite
    transactions, and re-fetchable news/price data. The legacy requirement
    (exit 2 without --backup-db) is retired; SOURCE_PREWRITE_BACKUP=1 or
    --backup-db restores the emergency pre-write backup."""
    db_path = tmp_path / "agent.db"
    sqlite3.connect(db_path).close()
    exit_code = _run_source_automation_main(
        [
            "--db",
            str(db_path),
            "--codex-run",
            str(tmp_path / ".codex-run"),
            "--output-dir",
            str(tmp_path / "out"),
            "--apply",
            "--record-plan",
        ]
    )

    assert exit_code == 0
    summary = json.loads((tmp_path / "out" / "source-automation-latest.json").read_text(encoding="utf-8"))
    assert summary["backup_path"] == ""
    assert summary["guards"]["backup_required_before_apply"] is False
    assert summary["guards"]["backup_covered_by"] == "daily_anchor"
    assert not list((tmp_path / "out" / "db-backups").glob("*.sqlite"))


def test_source_automation_dry_run_writes_summary_without_db_write(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    output_dir = tmp_path / "out"
    exit_code = _run_source_automation_main(
        [
            "--db",
            str(db_path),
            "--codex-run",
            str(tmp_path / ".codex-run"),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    summary = json.loads((output_dir / "source-automation-latest.json").read_text(encoding="utf-8"))
    assert summary["writes_database"] is False
    assert summary["before"]["task_count"] > 0


def test_source_automation_record_plan_dry_run_does_not_write_db(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    output_dir = tmp_path / "out"
    exit_code = _run_source_automation_main(
        [
            "--db",
            str(db_path),
            "--codex-run",
            str(tmp_path / ".codex-run"),
            "--output-dir",
            str(output_dir),
            "--record-plan",
        ]
    )

    assert exit_code == 0
    summary = json.loads((output_dir / "source-automation-latest.json").read_text(encoding="utf-8"))
    assert summary["writes_database"] is False
    assert summary["records_planned"] > 0
    assert summary["records_written"] == 0
    with closing(sqlite3.connect(db_path)) as connection, connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='source_acquisition_runs'"
        ).fetchone()
        assert table is None


def test_source_automation_news_fetch_dry_run_is_report_only(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    output_dir = tmp_path / "out"
    exit_code = _run_source_automation_main(
        [
            "--db",
            str(db_path),
            "--codex-run",
            str(tmp_path / ".codex-run"),
            "--output-dir",
            str(output_dir),
            "--fetch-news",
        ]
    )

    assert exit_code == 0
    summary = json.loads((output_dir / "source-automation-latest.json").read_text(encoding="utf-8"))
    assert summary["writes_database"] is False
    assert summary["news_fetch"]["status"] == "dry_run_skipped"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='news_fetch_runs'"
        ).fetchone()
        assert table is None


def test_source_automation_news_fetch_apply_uses_backup_and_records_summary(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "agent.db"
    sqlite3.connect(db_path).close()
    output_dir = tmp_path / "out"

    async def fake_fetch_news_sources(**_: object) -> dict[str, object]:
        return {
            "mode": "live",
            "runs": [
                {
                    "source_id": "official_news",
                    "status": "ok",
                    "articles_found": 2,
                    "clusters_upserted": 1,
                    "events_created": 1,
                    "error": "",
                }
            ],
            "articles_found": 2,
            "clusters_upserted": 1,
            "events_created": 1,
            "summaries_selected": 2,
            "summaries_completed": 1,
            "summaries_failed": 1,
        }

    monkeypatch.setattr(run_source_automation, "fetch_news_sources", fake_fetch_news_sources)
    exit_code = _run_source_automation_main(
        [
            "--db",
            str(db_path),
            "--codex-run",
            str(tmp_path / ".codex-run"),
            "--output-dir",
            str(output_dir),
            "--apply",
            "--backup-db",
            "--fetch-news",
        ]
    )

    assert exit_code == 0
    summary = json.loads((output_dir / "source-automation-latest.json").read_text(encoding="utf-8"))
    assert summary["writes_database"] is True
    assert summary["backup_path"]
    assert summary["news_fetch"]["status"] == "completed"
    assert summary["news_fetch"]["articles_found"] == 2
    assert summary["news_fetch"]["clusters_upserted"] == 1
    assert summary["news_fetch"]["events_created"] == 1
    assert summary["news_fetch"]["summaries_selected"] == 2
    assert summary["news_fetch"]["summaries_completed"] == 1
    assert summary["news_fetch"]["summaries_failed"] == 1


def test_source_automation_refuses_concurrent_run_when_lock_is_fresh(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    codex_run = tmp_path / ".codex-run"
    output_dir = tmp_path / "out"
    lock_dir = codex_run / "locks"
    lock_dir.mkdir(parents=True)
    (lock_dir / "source-automation.lock").write_text(json.dumps({"pid": os.getpid()}) + "\n", encoding="utf-8")

    exit_code = _run_source_automation_main(
        [
            "--db",
            str(db_path),
            "--codex-run",
            str(codex_run),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 75
    summary = json.loads((output_dir / "source-automation-latest.json").read_text(encoding="utf-8"))
    assert summary["status"] == "locked"


def test_source_automation_expires_stale_news_runs(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "agent.db"
    output_dir = tmp_path / "out"
    created_at = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("""
            CREATE TABLE news_fetch_runs (
              run_id TEXT PRIMARY KEY,
              created_at TEXT NOT NULL,
              finished_at TEXT,
              source_id TEXT NOT NULL,
              status TEXT NOT NULL,
              articles_found INTEGER NOT NULL,
              clusters_upserted INTEGER NOT NULL,
              events_created INTEGER NOT NULL,
              error TEXT NOT NULL
            )
            """)
        connection.execute(
            """
            INSERT INTO news_fetch_runs VALUES (?, ?, NULL, ?, ?, 0, 0, 0, ?)
            """,
            ("stale-run", created_at, "official_news", "running", ""),
        )

    async def fake_fetch_news_sources(**_: object) -> dict[str, object]:
        return {"mode": "live", "runs": [], "articles_found": 0, "clusters_upserted": 0, "events_created": 0}

    monkeypatch.setattr(run_source_automation, "fetch_news_sources", fake_fetch_news_sources)
    exit_code = _run_source_automation_main(
        [
            "--db",
            str(db_path),
            "--codex-run",
            str(tmp_path / ".codex-run"),
            "--output-dir",
            str(output_dir),
            "--apply",
            "--backup-db",
            "--fetch-news",
        ]
    )

    assert exit_code == 0
    summary = json.loads((output_dir / "source-automation-latest.json").read_text(encoding="utf-8"))
    assert summary["news_fetch"]["stale_running_runs_marked_before"] == 1
    with closing(sqlite3.connect(db_path)) as connection, connection:
        row = connection.execute(
            "SELECT status, finished_at FROM news_fetch_runs WHERE run_id = 'stale-run'"
        ).fetchone()
    assert row == ("timeout", row[1])
    assert row[1]


def test_public_source_failure_is_audited_and_blocks_critical_run(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "agent.db"
    sqlite3.connect(db_path).close()
    output_dir = tmp_path / "out"

    async def fake_fetch(_self: object, source: object) -> FetchResult:
        source_id = str(source.source_id)
        status = "upstream_error" if source_id == "eia_petroleum_api" else "ok"
        return FetchResult(
            source_id=source_id,
            fetched_at=datetime.now(UTC).isoformat(),
            status=status,
            content_type="application/json",
            content_preview="simulated",
        )

    async def fake_collect_intraday_prices(*, apply: bool) -> dict[str, object]:
        assert apply is True
        return {
            "attempted": len(run_source_automation.CHAIN_INSTRUMENTS),
            "collected": len(run_source_automation.CHAIN_INSTRUMENTS),
            "stored": len(run_source_automation.CHAIN_INSTRUMENTS),
            "writes_database": True,
            "items": [{"instrument": instrument} for instrument in run_source_automation.CHAIN_INSTRUMENTS],
            "errors": [],
        }

    monkeypatch.setattr(run_source_automation.Fetcher, "fetch", fake_fetch)
    monkeypatch.setattr(run_source_automation, "collect_intraday_prices", fake_collect_intraday_prices)
    monkeypatch.setattr(run_source_automation, "run_trade_futures_proxy_import", lambda _args: {"status": "skipped"})
    exit_code = _run_source_automation_main(
        [
            "--db",
            str(db_path),
            "--codex-run",
            str(tmp_path / ".codex-run"),
            "--output-dir",
            str(output_dir),
            "--apply",
            "--backup-db",
            "--fetch-public",
            "--force-public",
        ]
    )

    assert exit_code == 1
    summary = json.loads((output_dir / "source-automation-latest.json").read_text(encoding="utf-8"))
    assert summary["status"] == "blocked"
    assert summary["critical_failures"] == ["eia_petroleum_api"]
    with closing(sqlite3.connect(db_path)) as connection, connection:
        rows = connection.execute(
            "SELECT source_id, status, duration_ms, error FROM source_fetch_audit ORDER BY source_id"
        ).fetchall()
    assert len(rows) == len(run_source_automation.PUBLIC_FETCH_SOURCE_IDS)
    assert {row[0] for row in rows} == set(run_source_automation.PUBLIC_FETCH_SOURCE_IDS)
    assert next(row for row in rows if row[0] == "eia_petroleum_api")[1:] == (
        "upstream_error",
        next(row for row in rows if row[0] == "eia_petroleum_api")[2],
        "simulated",
    )


def test_no_new_data_does_not_degrade_public_fetch_component() -> None:
    items = [
        {"source_id": "eia_petroleum_api", "status": "no_new_data", "criticality": "critical"},
        {"source_id": "fred_macro_api", "status": "unchanged", "criticality": "critical"},
        {"source_id": "cftc_cot_petroleum", "status": "ok", "criticality": "important"},
    ]

    assert run_source_automation.component_status(items) == "completed"


def test_empty_user_files_inbox_is_ready_not_blocked(tmp_path: Path) -> None:
    task = source_acquisition._user_file_tasks(tmp_path / ".codex-run")[0]

    assert task["status"] == "ready"
    assert task["automation_level"] == "full"
    assert task["requires_human_action"] is False
    assert task["blocking_reason"] == ""


def test_opec_news_run_is_reused_as_source_readiness_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    checked_at = datetime.now(UTC).isoformat()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE news_fetch_runs (
              run_id TEXT, created_at TEXT, finished_at TEXT, source_id TEXT, status TEXT,
              articles_found INTEGER, clusters_upserted INTEGER, events_created INTEGER, error TEXT
            );
            INSERT INTO news_fetch_runs VALUES
              ('opec-run', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'opec_press', 'no_relevant_items', 0, 0, 0, '');
            """)
        connection.execute(
            "UPDATE news_fetch_runs SET created_at=?, finished_at=? WHERE run_id='opec-run'",
            (checked_at, checked_at),
        )

    states = source_acquisition._public_source_run_states(db_path)

    assert states["opec_press"]["last_run_status"] == "no_relevant_items"
    assert states["opec_press"]["last_success_at"] == checked_at

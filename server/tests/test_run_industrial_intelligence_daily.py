from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

import pytest

from app import storage
from app.settings import settings

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "run_industrial_intelligence_daily.py"
SPEC = importlib.util.spec_from_file_location("run_industrial_intelligence_daily", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
daily_runner = importlib.util.module_from_spec(SPEC)
sys.modules["run_industrial_intelligence_daily"] = daily_runner
SPEC.loader.exec_module(daily_runner)


@pytest.mark.parametrize("day", ["2026-09-26", "2026-09-27"])
def test_scheduled_weekend_skips_before_any_io(tmp_path, monkeypatch, day):
    class Weekend(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat(day + "T08:00:00+08:00")

    def unexpected(*args, **kwargs):
        pytest.fail("non-business day must not open a database or collect inputs")

    monkeypatch.setattr(daily_runner, "datetime", Weekend)
    monkeypatch.setattr(daily_runner.providers, "validate_geo_assets", unexpected)
    monkeypatch.setattr(daily_runner.storage, "connect", unexpected)
    database = tmp_path / "absent.db"
    output = tmp_path / "weekend"
    assert daily_runner.main(["--apply", "--db", str(database), "--output-dir", str(output)]) == 0
    report = json.loads((output / "industrial-intelligence-daily-latest.json").read_text())
    assert report["status"] == "skipped_non_business_day"
    assert report["business_date"] == day
    assert report["database_touched"] is False
    assert not database.exists()


def test_explicit_non_business_date_remains_blocked(tmp_path):
    output = tmp_path / "explicit"
    assert daily_runner.main([
        "--apply", "--business-date", "2026-09-26", "--db", str(tmp_path / "absent.db"),
        "--output-dir", str(output),
    ]) == 1
    report = json.loads((output / "industrial-intelligence-daily-latest.json").read_text())
    assert report["status"] == "blocked"


def test_dry_run_does_not_create_or_migrate_database(tmp_path: Path) -> None:
    database = tmp_path / "does-not-exist.db"
    output = tmp_path / "dry-reports"

    assert daily_runner.main(
        [
            "--dry-run",
            "--db",
            str(database),
            "--output-dir",
            str(output),
            "--business-date",
            "2026-09-04",
            "--skip-usgs",
        ]
    ) == 0
    assert not database.exists()
    report = json.loads(
        (output / "industrial-intelligence-daily-latest.json").read_text(encoding="utf-8")
    )
    assert report["mode"] == "dry_run"
    assert report["status"] == "ready"
    assert report["prediction_track_touched"] is False


def test_collection_does_not_freeze_a_brief(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "collect.db"
    monkeypatch.setenv("SQLITE_PATH", str(database))
    monkeypatch.setenv("INTELLIGENCE_RUN_DIR", str(tmp_path / "runs"))
    args = daily_runner.parse_args([
        "--apply", "--collect-only", "--skip-usgs", "--db", str(database),
        "--business-date", "2026-09-04",
    ])
    original_setting = settings.sqlite_path
    try:
        report = daily_runner._run_report(args)
        assert report["brief_created"] is False
        with closing(storage.connect()) as connection:
            assert connection.execute("SELECT COUNT(*) FROM intelligence_daily_briefs").fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM intelligence_runs WHERE run_type='projection'"
            ).fetchone()[0] == 1
    finally:
        object.__setattr__(settings, "sqlite_path", original_setting)
        storage._MIGRATED_PATHS.clear()


def test_empty_offline_daily_dag_blocks_and_replays_without_fabricating_evidence(tmp_path: Path) -> None:
    original_env = os.environ["SQLITE_PATH"]
    original_setting = settings.sqlite_path
    database = tmp_path / "intelligence-daily.db"
    output = tmp_path / "apply-reports"
    args = [
        "--apply",
        "--db",
        str(database),
        "--output-dir",
        str(output),
        "--business-date",
        "2026-09-04",
        "--skip-usgs",
    ]

    try:
        assert daily_runner.main(args) == 1
        assert daily_runner.main(args) == 1
        with closing(sqlite3.connect(database)) as connection:
            assert connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
            assert connection.execute(
                "SELECT COUNT(*) FROM intelligence_daily_briefs"
            ).fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 0
            assert connection.execute(
                "SELECT COUNT(*) FROM seven_product_forecast_batches"
            ).fetchone()[0] == 0
            run_types = {
                row[0]
                for row in connection.execute(
                    "SELECT DISTINCT run_type FROM intelligence_runs"
                ).fetchall()
            }
            assert run_types == {"projection", "clustering", "brief"}

        report = json.loads(
            (output / "industrial-intelligence-daily-latest.json").read_text(encoding="utf-8")
        )
        assert report["brief_replayed"] is True
        assert report["brief_status"] == "blocked"
        assert len(report["report_sha256"]) == 64
        assert (output.stat().st_mode & 0o777) == 0o700
        assert (
            (output / "industrial-intelligence-daily-latest.json").stat().st_mode & 0o777
        ) == 0o600
    finally:
        os.environ["SQLITE_PATH"] = original_env
        object.__setattr__(settings, "sqlite_path", original_setting)
        storage._MIGRATED_PATHS.clear()


@pytest.fixture()
def restore_runner_configuration():
    original_env = os.environ["SQLITE_PATH"]
    original_setting = settings.sqlite_path
    try:
        yield
    finally:
        os.environ["SQLITE_PATH"] = original_env
        object.__setattr__(settings, "sqlite_path", original_setting)


def test_preschedule_run_defers_brief_instead_of_failing(tmp_path, monkeypatch, restore_runner_configuration):
    """08:00 链在 09:31 发布时间之前运行：采集照常、brief 显式延期、rc=0。"""
    class Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat("2026-09-29T08:13:00+08:00")

    monkeypatch.setattr(daily_runner, "datetime", Morning)
    monkeypatch.setattr(daily_runner.providers, "validate_geo_assets", lambda: {"status": "ok"})
    collected = {
        "status": "ready",
        "blockers": [],
        "provider_run_ids": ["run-usgs"],
        "projection_run_id": "run-proj",
        "brief_created": False,
    }
    calls = {"pipeline": 0, "collect": 0}

    class FakeConn:
        def close(self): pass

    def fake_connect():
        calls["connect"] = True
        return FakeConn()

    def fake_collect(connection, **kwargs):
        calls["collect"] += 1
        return dict(collected)

    def unexpected_pipeline(*args, **kwargs):
        calls["pipeline"] += 1
        pytest.fail("preschedule run must not enter the brief pipeline")

    monkeypatch.setattr(daily_runner.storage, "connect", fake_connect)
    monkeypatch.setattr(daily_runner.service, "collect_daily_inputs", fake_collect)
    monkeypatch.setattr(daily_runner.service, "run_daily_pipeline", unexpected_pipeline)
    output = tmp_path / "deferred"
    rc = daily_runner.main([
        "--apply", "--db", str(tmp_path / "absent.db"), "--output-dir", str(output),
    ])
    assert rc == 0
    report = json.loads((output / "industrial-intelligence-daily-latest.json").read_text())
    assert report["status"] == "ready"
    assert report["brief_status"] == "brief_deferred_to_schedule"
    assert report["brief_replayed"] is False
    assert report["projection_run_id"] == "run-proj"
    assert calls["collect"] == 1
    assert calls["pipeline"] == 0


def test_preschedule_run_preserves_blocked_collection(tmp_path, monkeypatch, restore_runner_configuration):
    """发布时间前采集自身 blocked 时必须保留 blocked（不得包装成 ready）。"""
    class Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat("2026-09-29T08:13:00+08:00")

    monkeypatch.setattr(daily_runner, "datetime", Morning)
    monkeypatch.setattr(daily_runner.providers, "validate_geo_assets", lambda: {"status": "ok"})

    class FakeConn:
        def close(self): pass

    monkeypatch.setattr(daily_runner.storage, "connect", lambda: FakeConn())
    monkeypatch.setattr(
        daily_runner.service, "collect_daily_inputs",
        lambda connection, **kwargs: {
            "status": "blocked",
            "blockers": ["news projection needs a bounded retry before cutoff"],
            "brief_created": False,
        },
    )
    output = tmp_path / "deferred-blocked"
    rc = daily_runner.main([
        "--apply", "--db", str(tmp_path / "absent.db"), "--output-dir", str(output),
    ])
    assert rc == 1
    report = json.loads((output / "industrial-intelligence-daily-latest.json").read_text())
    assert report["status"] == "blocked"
    assert report["blockers"] == ["news projection needs a bounded retry before cutoff"]
    assert report["brief_status"] == "brief_deferred_to_schedule"


def test_postschedule_run_still_enters_full_pipeline(tmp_path, monkeypatch, restore_runner_configuration):
    """09:31 及以后运行（无显式日期）：照常进入完整管线（brief 阶段）。"""
    class AfterPublish(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.fromisoformat("2026-09-29T09:31:30+08:00")

    monkeypatch.setattr(daily_runner, "datetime", AfterPublish)
    monkeypatch.setattr(daily_runner.providers, "validate_geo_assets", lambda: {"status": "ok"})

    class FakeConn:
        def close(self): pass

    entered = {"pipeline": False}

    from types import SimpleNamespace
    def fake_pipeline(connection, **kwargs):
        entered["pipeline"] = True
        connection.execute = lambda query, params=(): [].__class__()  # runs 面板查询为空
        # fetchall() 返回空列表
        class _Empty:
            def fetchall(self_inner): return []
        connection.execute = lambda query, params=(): _Empty()
        return SimpleNamespace(
            brief_id="brief-x", brief_status="succeeded", brief_replayed=False,
            provider_run_ids=[], failed_provider_ids=[], projection_run_id="p",
            clustering_run_id="c", inserted_items={}, new_events={},
        )

    def unexpected_collect(*args, **kwargs):
        pytest.fail("postschedule full run must not re-run collect_daily_inputs")

    monkeypatch.setattr(daily_runner.storage, "connect", lambda: FakeConn())
    monkeypatch.setattr(daily_runner.service, "run_daily_pipeline", fake_pipeline)
    monkeypatch.setattr(daily_runner.service, "collect_daily_inputs", unexpected_collect)
    output = tmp_path / "full"
    rc = daily_runner.main([
        "--apply", "--db", str(tmp_path / "absent.db"), "--output-dir", str(output),
    ])
    assert rc == 0
    assert entered["pipeline"] is True
    report = json.loads((output / "industrial-intelligence-daily-latest.json").read_text())
    assert report["brief_id"] == "brief-x"

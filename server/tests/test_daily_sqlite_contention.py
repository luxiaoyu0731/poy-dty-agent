from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
from contextlib import closing

import pytest

from scripts import run_local_daily, run_seven_product_forecast_lifecycle, run_source_automation
from scripts.runtime_guards import is_sqlite_contention


def test_real_sqlite_writer_contention_is_distinct_from_corruption(tmp_path):
    path = tmp_path / "locked.db"
    with closing(sqlite3.connect(path)) as setup, setup:
        setup.execute("CREATE TABLE example(value)")
    first = sqlite3.connect(path)
    second = sqlite3.connect(path, timeout=0)
    try:
        first.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as info:
            second.execute("INSERT INTO example VALUES(1)")
        assert is_sqlite_contention(info.value)
        wrapped = RuntimeError("persistence failed")
        wrapped.__cause__ = info.value
        assert is_sqlite_contention(wrapped)
        assert not is_sqlite_contention(sqlite3.OperationalError("database disk image is malformed"))
    finally:
        first.rollback()
        first.close()
        second.close()


def test_source_contention_emits_fresh_retryable_report_and_releases_run_lock(tmp_path, monkeypatch):
    def busy(*args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(run_source_automation, "configure_runtime_sqlite_path", lambda *_: None)
    monkeypatch.setattr(run_source_automation, "run_with_lock", busy)
    output = tmp_path / "source"
    assert run_source_automation.main([
        "--db", str(tmp_path / "unused.db"), "--codex-run", str(tmp_path / "run"),
        "--output-dir", str(output),
    ]) == 75
    report = json.loads((output / "source-automation-latest.json").read_text())
    assert report["status"] == "locked"
    assert report["lock"]["kind"] == "sqlite"
    assert not (tmp_path / "run/locks/source-automation.lock").exists()


@pytest.mark.parametrize("cause,expected", [("database is locked", 75), ("malformed database", 1)])
def test_lifecycle_reports_only_nested_sqlite_lock_as_retryable(tmp_path, monkeypatch, cause, expected):
    def fail(**kwargs):
        raise RuntimeError("outcome_persistence_failed") from sqlite3.OperationalError(cause)

    runner = run_seven_product_forecast_lifecycle
    monkeypatch.setattr(runner, "configure_runtime_sqlite_path", lambda *_: None)
    monkeypatch.setattr(runner, "run_lifecycle", fail)
    assert runner.main(["--db", str(tmp_path / "unused.db"), "--output-dir", str(tmp_path)]) == expected
    report = json.loads((tmp_path / "seven-product-lifecycle-latest.json").read_text())
    assert report["status"] == "blocked"
    assert report["retryable"] == (expected == 75)


@pytest.mark.parametrize("codes,count", [([75, 0], 2), ([75, 75, 75], 3), ([1], 1)])
def test_daily_retries_busy_lifecycle_but_not_permanent_failure(tmp_path, monkeypatch, codes, count):
    output = tmp_path / "seven-product-lifecycle"
    output.mkdir()
    calls = []

    def run(command, *, timeout_seconds):
        code = codes[len(calls)]
        calls.append(command)
        (output / "seven-product-lifecycle-latest.json").write_text(json.dumps({
            "status": "ready" if code == 0 else "blocked", "forecast": {},
        }))
        return subprocess.CompletedProcess(command, code, "", "")

    monkeypatch.setattr(run_local_daily, "run_subprocess", run)
    monkeypatch.setattr(run_local_daily.time, "sleep", lambda *_: None)
    args = argparse.Namespace(db=tmp_path / "unused.db", apply=True, as_of="")
    result = run_local_daily.run_seven_product_lifecycle(args, output_dir=tmp_path)
    assert len(calls) == result["attempts"] == count
    assert result["exit_code"] == codes[-1]
    assert result["status"] == ("ready" if codes[-1] == 0 else "blocked")

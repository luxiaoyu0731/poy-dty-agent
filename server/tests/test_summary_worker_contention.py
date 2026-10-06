from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scripts import run_event_summary_worker as worker


def setup_worker(monkeypatch, tmp_path, *, once=False):
    args = worker.parse_args(["--output-dir", str(tmp_path), "--index-refresh-seconds", "0"])
    args.once = once
    client = SimpleNamespace()
    monkeypatch.setattr(worker, "parse_args", lambda _: args)
    monkeypatch.setattr(worker, "DeepSeekClient", lambda: client)
    sleeps = []
    monkeypatch.setattr(worker.time, "sleep", sleeps.append)
    worker._write_json(tmp_path / "daily-budget.json", {
        "date": datetime.now(UTC).date().isoformat(), "reserved_requests": 12,
    })
    return args, client, sleeps


def test_lock_after_provider_call_preserves_durable_usage(monkeypatch, tmp_path):
    _, client, sleeps = setup_worker(monkeypatch, tmp_path, once=True)

    async def cycle(*_, **__):
        client.http_attempt_callback(2)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "run_cycle", cycle)
    monkeypatch.setattr(worker, "maintain_semantic_index", lambda *_: pytest.fail("no index work on busy DB"))
    assert worker.main([]) == 75
    assert sleeps == []
    assert json.loads((tmp_path / "daily-budget.json").read_text())["reserved_requests"] == 14
    report = json.loads((tmp_path / "latest.json").read_text())
    assert report["status"] == "locked"
    assert report["provider_http_attempts"] == 2
    assert report["daily_reserved_requests"] == 14


def test_transient_lock_recovers_without_process_restart_or_budget_reset(monkeypatch, tmp_path):
    args, _, sleeps = setup_worker(monkeypatch, tmp_path)
    allowances = []

    async def cycle(*_, allowance, **__):
        allowances.append(allowance)
        if len(allowances) == 1:
            raise sqlite3.OperationalError("database is locked")
        args.once = True
        return {"status": "completed", "provider_http_attempts": 0}

    monkeypatch.setattr(worker, "run_cycle", cycle)
    assert worker.main([]) == 0
    assert allowances == [38, 38]
    assert sleeps == [30.0]
    assert json.loads((tmp_path / "latest.json").read_text())["status"] == "completed"
    assert json.loads((tmp_path / "daily-budget.json").read_text())["reserved_requests"] == 12


def test_persistent_lock_is_bounded_and_nonlock_error_is_not_hidden(monkeypatch, tmp_path):
    _, _, sleeps = setup_worker(monkeypatch, tmp_path)
    calls = []

    async def locked(*_, **__):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "run_cycle", locked)
    assert worker.main([]) == 75
    assert len(calls) == 3
    assert sleeps == [30.0, 30.0]

    async def corrupt(*_, **__):
        raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(worker, "run_cycle", corrupt)
    with pytest.raises(sqlite3.OperationalError, match="malformed"):
        worker.main([])

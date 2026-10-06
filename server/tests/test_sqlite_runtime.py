from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

from app import sqlite_runtime


def test_connection_lifecycle_is_serialized_without_serializing_connection_use(monkeypatch, tmp_path) -> None:
    real_connect = sqlite_runtime.sqlite3.connect
    real_close = sqlite_runtime._close_native
    state_lock = threading.Lock()
    active = 0
    peak = 0
    phases: set[str] = set()

    def traced_connect(*args, **kwargs):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
            phases.add("open")
        try:
            time.sleep(0.01)
            return real_connect(*args, **kwargs)
        finally:
            with state_lock:
                active -= 1

    def traced_close(connection):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
            phases.add("close")
        try:
            time.sleep(0.01)
            return real_close(connection)
        finally:
            with state_lock:
                active -= 1

    monkeypatch.setattr(sqlite_runtime.sqlite3, "connect", traced_connect)
    monkeypatch.setattr(sqlite_runtime, "_close_native", traced_close)
    database = tmp_path / "concurrent-open.db"

    def open_and_probe(_: int) -> int:
        connection = sqlite_runtime.connect_serialized(database)
        try:
            return int(connection.execute("SELECT 1").fetchone()[0])
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=24) as executor:
        results = list(executor.map(open_and_probe, range(48)))

    assert results == [1] * 48
    assert peak == 1
    assert phases == {"open", "close"}
    with closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"

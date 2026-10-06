from __future__ import annotations

import os
import sqlite3
import threading
import time
from contextlib import closing

from app import storage


def test_schema_audits_run_once_per_unchanged_file(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "audit-cache.db"
    monkeypatch.setattr(storage, "_configured_db_path", lambda: db_path)
    calls = {"count": 0}
    real_audit = storage._audit_current_v29

    def counting(connection):
        calls["count"] += 1
        return real_audit(connection)

    monkeypatch.setattr(storage, "_audit_current_v29", counting)
    storage._SCHEMA_AUDIT_FINGERPRINTS.pop(db_path.as_posix(), None)
    try:
        # Warm-up connect creates the database (migrations + WAL header), so
        # the measured sequence starts from a stable file identity.
        with closing(storage.connect()) as connection:
            connection.execute("SELECT 1")
        with closing(storage.connect()) as connection:
            connection.execute("SELECT 1")
        first_count = calls["count"]
        assert first_count >= 1
        with closing(storage.connect()) as connection:
            connection.execute("SELECT 1")
        assert calls["count"] == first_count
        # Any change to the database file invalidates the cached audit.
        stat = db_path.stat()
        os.utime(db_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        with closing(storage.connect()) as connection:
            connection.execute("SELECT 1")
        assert calls["count"] == first_count + 1
    finally:
        storage._SCHEMA_AUDIT_FINGERPRINTS.pop(db_path.as_posix(), None)


def test_readonly_retries_transient_sidecar_state(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "sidecar-retry.db"
    monkeypatch.setattr(storage, "_configured_db_path", lambda: db_path)
    with closing(storage.connect()) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS readiness_probe (id INTEGER PRIMARY KEY)")
    journal_path = db_path.with_name(db_path.name + "-journal")
    journal_path.write_bytes(b"transient")

    def clear_journal() -> None:
        time.sleep(0.05)
        journal_path.unlink(missing_ok=True)

    remover = threading.Thread(target=clear_journal)
    remover.start()
    try:
        with closing(storage.connect_readonly()) as connection:
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM readiness_probe").fetchone()[0] == 0
        # An immutable read-only open must not materialize WAL sidecars.
        assert not db_path.with_name(db_path.name + "-wal").exists()
        assert not db_path.with_name(db_path.name + "-shm").exists()
    finally:
        remover.join(timeout=2)
    assert not journal_path.exists()


def test_readonly_fails_closed_on_persistent_sidecar_state(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "sidecar-persistent.db"
    monkeypatch.setattr(storage, "_configured_db_path", lambda: db_path)
    with closing(storage.connect()) as connection, connection:
        connection.execute("CREATE TABLE IF NOT EXISTS readiness_probe (id INTEGER PRIMARY KEY)")
    journal_path = db_path.with_name(db_path.name + "-journal")
    journal_path.write_bytes(b"stuck")
    try:
        import pytest

        with pytest.raises(sqlite3.OperationalError, match="sidecar_state_invalid"):
            storage.connect_readonly()
    finally:
        journal_path.unlink(missing_ok=True)

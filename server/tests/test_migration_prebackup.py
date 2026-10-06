from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_backend_foundation import _create_version_24_database

from app import storage
from app.settings import settings

MIGRATION_BACKUP_DIRNAME = "migration-backups"


def _set_sqlite_path(path: Path) -> str:
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(path))
    return original


def _build_legacy_v13_database(db_path: Path) -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("""
            CREATE TABLE rag_evidence_reviews (
              doc_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              reviewer TEXT NOT NULL,
              notes TEXT NOT NULL,
              reviewed_at TEXT NOT NULL
            )
            """)
        connection.execute(
            "INSERT INTO rag_evidence_reviews VALUES (?, ?, ?, ?, ?)",
            ("legacy-doc", "reviewed", "old-reviewer", "old review", "2026-07-01T00:00:00Z"),
        )
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
        )
        for version, name, _ in storage._migrations():
            if version <= 13:
                connection.execute(
                    "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                    (version, name, "2026-07-01T00:00:00Z"),
                )
        connection.execute("PRAGMA user_version = 13")
        connection.commit()


def test_migration_creates_verified_pre_migration_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    _build_legacy_v13_database(db_path)

    original = _set_sqlite_path(db_path)
    try:
        with closing(storage.connect()) as connection:
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            legacy_rows = connection.execute(
                "SELECT COUNT(*) FROM rag_evidence_reviews"
            ).fetchone()[0]
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    backups = sorted((tmp_path / MIGRATION_BACKUP_DIRNAME).glob("legacy.pre-migration.v13-to-v39.*.sqlite"))
    assert user_version == storage.SCHEMA_VERSION == 39
    assert legacy_rows == 1
    assert len(backups) == 1
    assert backups[0].parent.stat().st_mode & 0o777 == 0o700
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(backups[0])) as snapshot:
        snapshot_version = int(snapshot.execute("PRAGMA user_version").fetchone()[0])
        snapshot_rows = snapshot.execute("SELECT COUNT(*) FROM rag_evidence_reviews").fetchone()[0]
        integrity = snapshot.execute("PRAGMA integrity_check").fetchone()[0]
    assert snapshot_version == 13
    assert snapshot_rows == 1
    assert integrity == "ok"


def test_version_24_upgrade_snapshot_preserves_pre_migration_state(tmp_path: Path) -> None:
    db_path = tmp_path / "v24-production.db"
    _create_version_24_database(db_path)

    original = _set_sqlite_path(db_path)
    try:
        with closing(storage.connect()) as connection:
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    backups = sorted(
        (tmp_path / MIGRATION_BACKUP_DIRNAME).glob("v24-production.pre-migration.v24-to-v39.*.sqlite")
    )
    assert user_version == storage.SCHEMA_VERSION == 39
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as snapshot:
        snapshot_version = int(snapshot.execute("PRAGMA user_version").fetchone()[0])
        snapshot_migrations = {
            int(row[0]) for row in snapshot.execute("SELECT version FROM schema_migrations")
        }
        integrity = snapshot.execute("PRAGMA integrity_check").fetchone()[0]
    assert snapshot_version == 24
    assert max(snapshot_migrations) == 24
    assert integrity == "ok"


def test_fresh_database_creates_no_pre_migration_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    original = _set_sqlite_path(db_path)
    try:
        with closing(storage.connect()) as connection:
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    assert user_version == storage.SCHEMA_VERSION
    assert not (tmp_path / MIGRATION_BACKUP_DIRNAME).exists()


def test_migration_backup_retention_keeps_recent_snapshots(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    backup_dir = tmp_path / MIGRATION_BACKUP_DIRNAME
    backup_dir.mkdir()
    for index in range(7):
        (backup_dir / f"legacy.pre-migration.v13-to-v36.2026010{index}T000000Z.sqlite").write_bytes(b"")
    _build_legacy_v13_database(db_path)

    original = _set_sqlite_path(db_path)
    try:
        with closing(storage.connect()):
            pass
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    remaining = sorted(path.name for path in backup_dir.glob("*.sqlite"))
    assert len(remaining) == storage._MIGRATION_BACKUP_RETENTION
    assert any(name.startswith("legacy.pre-migration.v13-to-v36.2026") for name in remaining)


def test_migration_backup_retention_uses_mtime_across_version_names(tmp_path: Path) -> None:
    backup_dir = tmp_path / MIGRATION_BACKUP_DIRNAME
    backup_dir.mkdir()
    paths = []
    for index, version in enumerate((99, 2, 80, 3, 70, 4, 60)):
        path = backup_dir / f"agent.pre-migration.v{version}-to-v100.20260101T000000Z.sqlite"
        path.write_bytes(b"")
        path.touch()
        timestamp = 1_700_000_000 + index
        os.utime(path, (timestamp, timestamp))
        paths.append(path)

    storage._prune_migration_backups(backup_dir, "agent")

    remaining = set(backup_dir.glob("*.sqlite"))
    assert remaining == set(paths[-storage._MIGRATION_BACKUP_RETENTION :])


def test_failed_pre_migration_backup_aborts_upgrade_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "legacy.db"
    _build_legacy_v13_database(db_path)

    real_connect = sqlite3.connect

    def failing_backup_connect(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        if MIGRATION_BACKUP_DIRNAME in str(database):
            raise RuntimeError("backup-unavailable")
        return real_connect(database, *args, **kwargs)  # type: ignore[arg-type]

    original = _set_sqlite_path(db_path)
    monkeypatch.setattr(sqlite3, "connect", failing_backup_connect)
    try:
        with pytest.raises(RuntimeError, match="backup-unavailable"):
            storage.connect()
    finally:
        monkeypatch.undo()
        object.__setattr__(settings, "sqlite_path", original)

    with closing(sqlite3.connect(db_path)) as probe:
        user_version = int(probe.execute("PRAGMA user_version").fetchone()[0])
        migrated = probe.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 14").fetchone()[0]
    leftovers = list((tmp_path / MIGRATION_BACKUP_DIRNAME).glob("*.sqlite"))
    assert user_version == 13
    assert migrated == 0
    assert leftovers == []

    original = _set_sqlite_path(db_path)
    try:
        with closing(storage.connect()) as connection:
            recovered_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        object.__setattr__(settings, "sqlite_path", original)

    recovered_backups = list((tmp_path / MIGRATION_BACKUP_DIRNAME).glob("*.sqlite"))
    assert recovered_version == storage.SCHEMA_VERSION
    assert len(recovered_backups) == 1

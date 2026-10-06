from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from scripts import rebuild_production_rag_index as rebuild


def make_database(path: Path, value: str = "production") -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES (?)", (value,))


def test_backup_uses_sqlite_online_backup_and_passes_integrity_check(
    tmp_path: Path,
) -> None:
    database = tmp_path / "agent.db"
    backup = tmp_path / "backups" / "agent.db"
    make_database(database)

    result = rebuild.backup_sqlite(database, backup)

    assert result["path"] == str(backup)
    assert result["bytes"] > 0
    assert backup.parent.stat().st_mode & 0o777 == 0o700
    assert backup.stat().st_mode & 0o777 == 0o600
    with closing(sqlite3.connect(backup)) as connection, connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "production"


def test_rebuild_report_uses_semantic_shadow_builder_and_confirms_activation(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "agent.db"
    make_database(database)
    calls: list[int | None] = []

    def fake_rebuild(*, limit: int | None = None):
        calls.append(limit)
        return {
            "status": "ready",
            "index_id": "semantic-new",
            "documents": 10,
            "chunks": 12,
            "vectors": 12,
            "embedding_mode": "semantic_embedding",
        }

    monkeypatch.setattr(rebuild, "rebuild_semantic_index", fake_rebuild)
    monkeypatch.setattr(
        rebuild,
        "semantic_index_status",
        lambda: {
            "status": "ready",
            "active_index": {
                "index_id": "semantic-new",
                "document_count": 10,
                "chunk_count": 12,
                "vector_count": 12,
                "embedding_mode": "semantic_embedding",
            },
            "stale_reason": "",
        },
    )

    report = rebuild.rebuild_with_report(
        db_path=database,
        output_dir=tmp_path / "reports",
        backup_db=True,
        limit=None,
    )

    assert calls == [None]
    assert report["status"] == "ready"
    assert report["result"]["index_id"] == "semantic-new"
    assert report["active_index"]["index_id"] == "semantic-new"
    assert report["backup"]["path"]


def test_rebuild_failure_reports_preserved_active_index(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "agent.db"
    make_database(database)
    monkeypatch.setattr(
        rebuild,
        "rebuild_semantic_index",
        lambda **_: {
            "status": "failed",
            "index_id": "semantic-failed",
            "error": "embedding unavailable",
            "active_index_preserved": True,
        },
    )
    monkeypatch.setattr(
        rebuild,
        "semantic_index_status",
        lambda: {
            "status": "ready",
            "active_index": {"index_id": "semantic-old"},
            "stale_reason": "",
        },
    )

    report = rebuild.rebuild_with_report(
        db_path=database,
        output_dir=tmp_path / "reports",
        backup_db=False,
        limit=None,
    )

    assert report["status"] == "failed"
    assert report["active_index"]["index_id"] == "semantic-old"
    assert report["result"]["active_index_preserved"] is True

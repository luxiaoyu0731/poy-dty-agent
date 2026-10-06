#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
sys.path.insert(0, str(SERVER_ROOT))

from app.semantic_index import rebuild_semantic_index, semantic_index_status  # noqa: E402
from app.settings import settings  # noqa: E402
from app.sqlite_permissions import (  # noqa: E402
    remove_sqlite_artifacts,
    secure_private_directory,
    secure_sqlite_artifacts,
)

DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "production-rag"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Back up the selected SQLite database, build a versioned shadow semantic "
            "index, and atomically activate it only after a complete build."
        )
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--mode", choices=["full"], default="full")
    parser.add_argument(
        "--limit", type=int, default=None, help="Optional emergency cap; omit for production all-eligible rebuild."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--backup-db", action="store_true")
    args = parser.parse_args(argv)
    db_path = args.db.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    original_sqlite_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(db_path))
    try:
        report = rebuild_with_report(
            db_path=db_path,
            output_dir=output_dir,
            backup_db=args.backup_db,
            limit=args.limit,
        )
    finally:
        object.__setattr__(settings, "sqlite_path", original_sqlite_path)

    secure_private_directory(output_dir)
    output = output_dir / "production-rag-rebuild-latest.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "status": report["status"]}, ensure_ascii=False))
    return 0 if report["status"] == "ready" else 1


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integrity(path: Path) -> None:
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        raise RuntimeError(f"SQLite integrity check failed for {path}: {result}")


def backup_sqlite(source: Path, destination: Path) -> dict[str, Any]:
    """Create a consistent online backup without stopping production readers."""
    source = source.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    _integrity(source)
    secure_sqlite_artifacts(source)
    secure_private_directory(destination.parent)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        with (
            closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)) as src,
            closing(sqlite3.connect(temporary, timeout=30)) as dst,
        ):
            src.backup(dst)
        _integrity(temporary)
        secure_sqlite_artifacts(temporary)
        temporary.replace(destination)
        secure_sqlite_artifacts(destination)
    finally:
        remove_sqlite_artifacts(temporary)
    return {
        "path": str(destination),
        "bytes": destination.stat().st_size,
        "sha256": _sha256(destination),
    }


def _prune_semantic_index_backups(backup_dir: Path, db_name: str, *, keep: int = 2) -> int:
    # Without this, every rebuild leaves a full-size pre_semantic_index copy and
    # the backup dir grows without bound (it reached 5.5GB before retention).
    candidates = sorted(
        (path for path in backup_dir.glob(f"{db_name}.pre_semantic_index_*.sqlite") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    stale = candidates[max(0, keep):]
    for path in stale:
        for suffix in ("", "-wal", "-shm"):
            artifact = Path(str(path) + suffix)
            if artifact.exists():
                artifact.unlink()
    return len(stale)


def rebuild_with_report(*, db_path: Path, output_dir: Path, backup_db: bool, limit: int | None) -> dict[str, Any]:
    db_path = db_path.expanduser().resolve()
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    _integrity(db_path)
    backup: dict[str, Any] | None = None
    if backup_db:
        backup_dir = output_dir / "db-backups"
        secure_private_directory(backup_dir)
        _prune_semantic_index_backups(backup_dir, db_path.name, keep=2)
        backup_path = (
            backup_dir / f"{db_path.name}.pre_semantic_index_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.sqlite"
        )
        backup = backup_sqlite(db_path, backup_path)

    result = rebuild_semantic_index(limit=limit)
    status = semantic_index_status()
    active = status.get("active_index")
    activated = bool(
        result.get("status") == "ready"
        and active
        and active.get("index_id") == result.get("index_id")
        and status.get("status") == "ready"
    )
    report_status = "ready" if activated else str(result.get("status") or "failed")
    if result.get("status") == "ready" and not activated:
        report_status = "failed"
    return {
        "schema_version": "production_semantic_index_rebuild.1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": report_status,
        "mode": "full",
        "limit": limit,
        "database": str(db_path),
        "backup": backup,
        "result": result,
        "active_index": active,
        "index_status": status,
        "policy": {
            "production_all_eligible": limit is None,
            "shadow_build": True,
            "atomic_activation": True,
            "failed_build_preserves_previous_active_index": True,
            "repository_database_never_copied": True,
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())

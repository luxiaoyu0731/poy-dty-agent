from __future__ import annotations

from pathlib import Path

import pytest

from app.sqlite_permissions import remove_sqlite_artifacts, secure_private_directory, secure_sqlite_artifacts


def test_secure_sqlite_artifacts_covers_database_and_sidecars(tmp_path: Path) -> None:
    private = secure_private_directory(tmp_path / "private")
    assert private.stat().st_mode & 0o777 == 0o700

    database = private / "agent.db"
    artifacts = [database, Path(f"{database}-wal"), Path(f"{database}-shm"), Path(f"{database}-journal")]
    for artifact in artifacts:
        artifact.write_bytes(b"private")
        artifact.chmod(0o644)

    secured = secure_sqlite_artifacts(database)

    assert secured == tuple(artifacts)
    assert all(artifact.stat().st_mode & 0o777 == 0o600 for artifact in artifacts)

    remove_sqlite_artifacts(database)
    assert all(not artifact.exists() for artifact in artifacts)


def test_secure_private_directory_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(OSError, match="private_directory_invalid"):
        secure_private_directory(link)

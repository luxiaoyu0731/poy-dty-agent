from __future__ import annotations

import stat
from pathlib import Path

SQLITE_AUXILIARY_SUFFIXES = ("-wal", "-shm", "-journal")


def secure_private_directory(path: Path) -> Path:
    """Create or tighten a directory that may contain private SQLite data."""

    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise OSError(f"private_directory_invalid:{path}")
    path.chmod(0o700)
    return path


def secure_sqlite_artifacts(path: Path) -> tuple[Path, ...]:
    """Set the database and any SQLite sidecars to owner-only permissions."""

    secured: list[Path] = []
    for candidate in (path, *(Path(f"{path}{suffix}") for suffix in SQLITE_AUXILIARY_SUFFIXES)):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise OSError(f"sqlite_artifact_invalid:{candidate}")
        candidate.chmod(0o600)
        secured.append(candidate)
    return tuple(secured)


def remove_sqlite_artifacts(path: Path) -> None:
    """Remove an incomplete SQLite copy together with its possible sidecars."""

    for candidate in (path, *(Path(f"{path}{suffix}") for suffix in SQLITE_AUXILIARY_SUFFIXES)):
        candidate.unlink(missing_ok=True)

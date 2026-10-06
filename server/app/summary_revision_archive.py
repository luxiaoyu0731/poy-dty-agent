"""Durable pre-change summary records; no database schema or retention changes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .sqlite_permissions import secure_private_directory


def archive_summary_revision(database: str, row: dict) -> Path:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True).encode()
    digest = hashlib.sha256(payload).hexdigest()
    root = secure_private_directory(Path(database).resolve().parent / "summary-revision-archive")
    destination = root / f"{digest}.json"
    if destination.exists():
        if destination.is_symlink() or destination.read_bytes() != payload:
            raise OSError("summary_revision_archive_mismatch")
        return destination
    fd, temporary = tempfile.mkstemp(prefix=".revision-", dir=root)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return destination

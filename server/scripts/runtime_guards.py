from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


class RunLockError(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("message", "run lock is held"))
        self.payload = payload


def is_sqlite_contention(error: BaseException) -> bool:
    """Recognize only retryable SQLite lock errors, including wrapped causes."""
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, sqlite3.OperationalError):
            code = getattr(error, "sqlite_errorcode", None)
            if isinstance(code, int) and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                return True
            if code is None and str(error).lower() in {
                "database is locked", "database table is locked", "database schema is locked",
            }:
                return True
        cause = error.__cause__ or error.__context__
        if cause is None:
            break
        error = cause
    return False


@contextmanager
def exclusive_run_lock(lock_path: Path, *, stale_after_seconds: int) -> Iterator[dict[str, Any]]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"pid": os.getpid(), "created_at": datetime.now(UTC).isoformat(), "lock_path": str(lock_path)}
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            break
        except FileExistsError as exc:
            existing = read_lock_payload(lock_path)
            owner_pid = existing.get("pid")
            if isinstance(owner_pid, int) and owner_pid > 0 and not _pid_is_alive(owner_pid):
                with suppress(FileNotFoundError):
                    lock_path.unlink()
                continue
            age_seconds = max(0.0, time.time() - lock_path.stat().st_mtime) if lock_path.exists() else 0.0
            if age_seconds > stale_after_seconds:
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    continue
                continue
            existing["message"] = f"another automation run is active; lock={lock_path}"
            existing["age_seconds"] = int(age_seconds)
            raise RunLockError(existing) from exc
    try:
        yield payload
    finally:
        try:
            current = read_lock_payload(lock_path)
            if current.get("pid") == os.getpid():
                lock_path.unlink()
        except FileNotFoundError:
            pass


def read_lock_payload(lock_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def configure_runtime_sqlite_path(db_path: Path) -> None:
    resolved = db_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    os.environ["SQLITE_PATH"] = str(resolved)
    try:
        from app.settings import settings
    except Exception:  # noqa: BLE001
        return
    object.__setattr__(settings, "sqlite_path", str(resolved))


def expire_stale_news_fetch_runs(
    db_path: Path,
    *,
    stale_after_minutes: int = 60,
    reason: str = "stale running run expired by automation guard",
) -> int:
    if not db_path.exists():
        return 0
    cutoff = datetime.now(UTC) - timedelta(minutes=stale_after_minutes)
    now = datetime.now(UTC).isoformat()
    try:
        with closing(sqlite3.connect(db_path)) as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'news_fetch_runs'"
            ).fetchone()
            if row is None:
                return 0
            cursor = connection.execute(
                """
                UPDATE news_fetch_runs
                SET status = ?, finished_at = ?, error = CASE WHEN error = '' THEN ? ELSE error END
                WHERE status = ? AND finished_at IS NULL AND created_at < ?
                """,
                ("timeout", now, reason, "running", cutoff.isoformat()),
            )
            connection.commit()
            return int(cursor.rowcount or 0)
    except sqlite3.Error:
        return 0

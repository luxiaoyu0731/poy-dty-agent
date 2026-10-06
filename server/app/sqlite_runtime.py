from __future__ import annotations

import sqlite3
import threading
from typing import Any

_CONNECTION_LIFECYCLE_LOCK = threading.Lock()


def _close_native(connection: sqlite3.Connection) -> None:
    connection.close()


class SerializedLifecycleConnection:
    """Transparent connection proxy that serializes native close operations."""

    __slots__ = ("_connection",)

    def __init__(self, connection: sqlite3.Connection) -> None:
        object.__setattr__(self, "_connection", connection)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._connection, name, value)

    def __enter__(self) -> SerializedLifecycleConnection:
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        return bool(self._connection.__exit__(exc_type, exc_value, traceback))

    def close(self) -> None:
        with _CONNECTION_LIFECYCLE_LOCK:
            _close_native(self._connection)

    def backup(self, target: Any, *args: Any, **kwargs: Any) -> None:
        destination = target._connection if isinstance(target, SerializedLifecycleConnection) else target
        self._connection.backup(destination, *args, **kwargs)


def connect_serialized(database: Any, *args: Any, **kwargs: Any) -> SerializedLifecycleConnection:
    """Open or close one SQLite connection at a time within this process.

    Queries still run concurrently on independent connections. Only the
    native connection lifecycle is serialized so a burst of synchronous ASGI
    handlers cannot deadlock SQLite's process-global reusable-file-descriptor
    mutex when one worker opens a connection while another closes one.
    """

    if len(args) >= 5 or "factory" in kwargs:
        raise ValueError("custom SQLite connection factories are not supported by connect_serialized")
    with _CONNECTION_LIFECYCLE_LOCK:
        return SerializedLifecycleConnection(sqlite3.connect(database, *args, **kwargs))

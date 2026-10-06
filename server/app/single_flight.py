"""Per-key single-flight execution with a bounded join.

A global execution lock makes cache hits and unrelated contexts wait behind one
long build. This helper shares one in-flight build per key instead: the first
caller runs ``build``; concurrent callers for the same key join its result or
failure. Nothing here serializes unrelated keys, and the wait is bounded by
``join_timeout`` rather than by the build itself.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


class _Slot:
    __slots__ = ("done", "error", "value")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.error: BaseException | None = None
        self.value: object | None = None


class SingleFlight(Generic[T]):
    """Share one build per key; waiters receive the owner's result or failure."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._slots: dict[str, _Slot] = {}

    def run(self, key: str, build: Callable[[], T], *, join_timeout: float) -> tuple[T, bool]:
        """Return ``(result, built_by_this_call)``.

        The owner runs ``build`` outside any lock and publishes the outcome;
        its exception propagates unchanged to every waiter. A waiter whose join
        deadline passes raises ``TimeoutError("single_flight_join_timeout")``
        while the owner keeps building — the caller decides whether stale data
        or an error response fits its contract.
        """
        with self._guard:
            slot = self._slots.get(key)
            owner = slot is None
            if owner:
                slot = _Slot()
                self._slots[key] = slot
        if not owner:
            if slot is None or not slot.done.wait(join_timeout):
                raise TimeoutError("single_flight_join_timeout")
            if slot.error is not None:
                raise slot.error
            return slot.value, False  # type: ignore[return-value]
        try:
            value = build()
        except BaseException as exc:
            slot.error = exc
            slot.done.set()
            with self._guard:
                if self._slots.get(key) is slot:
                    self._slots.pop(key, None)
            raise
        slot.value = value
        slot.done.set()
        with self._guard:
            if self._slots.get(key) is slot:
                self._slots.pop(key, None)
        return value, True

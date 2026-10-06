from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request

from .observability import observe_rate_limited
from .settings import settings

WINDOWS: defaultdict[str, deque[float]] = defaultdict(deque)


def rate_limit(bucket: str, limit: int) -> Callable[[Request], None]:
    def dependency(request: Request) -> None:
        window_seconds = settings.rate_limit_window_seconds
        now = time.monotonic()
        client = request.client.host if request.client else "unknown"
        key = f"{bucket}:{client}"
        entries = WINDOWS[key]
        while entries and now - entries[0] > window_seconds:
            entries.popleft()
        if len(entries) >= limit:
            observe_rate_limited(bucket=bucket)
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "RATE_LIMITED",
                    "message": "Too many requests for this endpoint.",
                    "retry_after_seconds": window_seconds,
                },
            )
        entries.append(now)

    return dependency

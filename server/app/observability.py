from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from uuid import uuid4

from fastapi import Request, Response

from .scheduler_observability_state import BOUNDED_STATUSES, scheduler_snapshot

logger = logging.getLogger("poy_dty_agent")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

REQUEST_COUNTER: Counter[str] = Counter()
REQUEST_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
REQUEST_DURATION_HISTOGRAM: defaultdict[str, Counter[str]] = defaultdict(Counter)
LLM_COUNTER: Counter[str] = Counter()
LLM_LATENCY_BUCKETS = (0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)
LLM_LATENCY_HISTOGRAM: defaultdict[str, Counter[str]] = defaultdict(Counter)
LLM_TOKEN_COUNTER: Counter[str] = Counter()
SOURCE_FETCH_COUNTER: Counter[str] = Counter()
RATE_LIMIT_COUNTER: Counter[str] = Counter()
SOURCE_FRESHNESS_SECONDS: dict[str, float] = {}
SCHEDULER_LAST_SUCCESS_UNIX_SECONDS: dict[str, float] = {}
SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS: dict[str, float] = {}
SCHEDULER_LAST_DURATION_SECONDS: dict[str, float] = {}
SCHEDULER_BACKLOG: dict[str, int] = {}
SCHEDULER_LAST_STATUS: dict[str, str] = {}
SCHEDULER_FAILURE_COUNTER: Counter[str] = Counter()
SCHEDULER_STATUSES = BOUNDED_STATUSES
EXTERNAL_SCHEDULER_STATUS_ENV = {
    "local_daily": "LOCAL_DAILY_SCHEDULER_STATUS_PATH",
    "news_scheduler": "NEWS_SCHEDULER_STATUS_PATH",
}


def _observe(
    histogram: defaultdict[str, Counter[str]],
    key: str,
    value_seconds: float,
    buckets: tuple[float, ...],
) -> None:
    for bucket in buckets:
        if value_seconds <= bucket:
            histogram[key][str(bucket)] += 1
    histogram[key]["+Inf"] += 1
    histogram[key]["sum"] += value_seconds


async def request_observability_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id", str(uuid4()))
    request.state.request_id = request_id
    started_at = time.perf_counter()
    status_code = 500
    try:
        response: Response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        duration_seconds = time.perf_counter() - started_at
        duration_ms = round(duration_seconds * 1000)
        route = request.scope.get("route")
        route_path = getattr(route, "path", request.url.path)
        key = f"{request.method} {route_path} {status_code}"
        REQUEST_COUNTER[key] += 1
        _observe(REQUEST_DURATION_HISTOGRAM, key, duration_seconds, REQUEST_DURATION_BUCKETS)
        logger.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": route_path,
                    "status": status_code,
                    "duration_ms": duration_ms,
                },
                ensure_ascii=False,
            )
        )


def observe_llm_call(
    *,
    provider: str,
    model: str,
    fallback: bool,
    latency_ms: int,
    prompt_tokens_est: int,
    completion_tokens_est: int,
) -> None:
    key = f"{provider} {model} {'fallback' if fallback else 'ok'}"
    LLM_COUNTER[key] += 1
    _observe(LLM_LATENCY_HISTOGRAM, key, latency_ms / 1000, LLM_LATENCY_BUCKETS)
    LLM_TOKEN_COUNTER[f"{provider} {model} prompt"] += prompt_tokens_est
    LLM_TOKEN_COUNTER[f"{provider} {model} completion"] += completion_tokens_est


def observe_source_fetch(*, source_id: str, status: str) -> None:
    SOURCE_FETCH_COUNTER[f"{source_id} {status}"] += 1


def observe_rate_limited(*, bucket: str) -> None:
    RATE_LIMIT_COUNTER[bucket] += 1


def observe_source_freshness(*, source_id: str, age_seconds: float) -> None:
    SOURCE_FRESHNESS_SECONDS[source_id] = max(0.0, age_seconds)


def observe_scheduler(
    *,
    scheduler: str,
    status: str = "success",
    last_attempt_unix_seconds: float | None = None,
    last_success_unix_seconds: float | None = None,
    duration_seconds: float,
    backlog: int,
) -> None:
    candidate = str(status or "unknown").strip().lower()
    normalized = candidate if candidate in SCHEDULER_STATUSES else "unknown"
    SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS[scheduler] = max(
        0.0, time.time() if last_attempt_unix_seconds is None else last_attempt_unix_seconds
    )
    SCHEDULER_LAST_STATUS[scheduler] = normalized
    if last_success_unix_seconds is not None:
        SCHEDULER_LAST_SUCCESS_UNIX_SECONDS[scheduler] = max(0.0, last_success_unix_seconds)
    if normalized in {"failed", "failure", "error", "timeout"}:
        SCHEDULER_FAILURE_COUNTER[f"{scheduler} {normalized}"] += 1
    SCHEDULER_LAST_DURATION_SECONDS[scheduler] = max(0.0, duration_seconds)
    SCHEDULER_BACKLOG[scheduler] = max(0, backlog)


def refresh_external_scheduler_metrics() -> None:
    """Import bounded snapshots written atomically by out-of-process schedulers."""

    for scheduler, environment_name in EXTERNAL_SCHEDULER_STATUS_ENV.items():
        raw_path = os.getenv(environment_name, "").strip()
        if not raw_path:
            continue
        try:
            payload = json.loads(Path(raw_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        snapshot = scheduler_snapshot(payload, scheduler=scheduler)
        if snapshot is None:
            continue
        SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS[scheduler] = snapshot["last_attempt_unix_seconds"]
        SCHEDULER_LAST_STATUS[scheduler] = snapshot["status"]
        SCHEDULER_LAST_DURATION_SECONDS[scheduler] = snapshot["duration_seconds"]
        SCHEDULER_BACKLOG[scheduler] = snapshot["backlog"]
        last_success = snapshot["last_success_unix_seconds"]
        if last_success is not None:
            SCHEDULER_LAST_SUCCESS_UNIX_SECONDS[scheduler] = last_success
        for key in [key for key in SCHEDULER_FAILURE_COUNTER if key.startswith(f"{scheduler} ")]:
            del SCHEDULER_FAILURE_COUNTER[key]
        for status, count in snapshot["failure_counts"].items():
            SCHEDULER_FAILURE_COUNTER[f"{scheduler} {status}"] = count


def _labels(values: dict[str, str]) -> str:
    return ",".join(f'{name}="{value}"' for name, value in values.items())


def _intelligence_metrics_lines() -> list[str]:
    """Bounded intelligence-domain families; import is deferred and isolated."""

    try:
        from .industrial_intelligence.metrics import metrics_lines

        return metrics_lines()
    except Exception:  # noqa: BLE001 - metrics must never break scraping
        return ["# intelligence metrics unavailable"]


def metrics_text() -> str:
    refresh_external_scheduler_metrics()
    lines = [
        "# HELP poy_dty_http_requests_total HTTP requests by method, route, and status.",
        "# TYPE poy_dty_http_requests_total counter",
    ]
    for key, count in sorted(REQUEST_COUNTER.items()):
        method, route, status = key.split(" ", 2)
        lines.append(f'poy_dty_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}')
    lines.extend(
        [
            "# HELP poy_dty_http_request_duration_seconds HTTP request latency histogram.",
            "# TYPE poy_dty_http_request_duration_seconds histogram",
        ]
    )
    for key, buckets in sorted(REQUEST_DURATION_HISTOGRAM.items()):
        method, route, status = key.split(" ", 2)
        base = {"method": method, "route": route, "status": status}
        for bucket in [*map(str, REQUEST_DURATION_BUCKETS), "+Inf"]:
            labels = _labels({**base, "le": bucket})
            lines.append(f"poy_dty_http_request_duration_seconds_bucket{{{labels}}} {buckets[bucket]}")
        lines.append(f"poy_dty_http_request_duration_seconds_sum{{{_labels(base)}}} {buckets['sum']}")
        lines.append(f"poy_dty_http_request_duration_seconds_count{{{_labels(base)}}} {buckets['+Inf']}")
    lines.extend(
        [
            "# HELP poy_dty_llm_calls_total LLM calls by provider, model, and status.",
            "# TYPE poy_dty_llm_calls_total counter",
        ]
    )
    for key, count in sorted(LLM_COUNTER.items()):
        provider, model, status = key.split(" ", 2)
        lines.append(f'poy_dty_llm_calls_total{{provider="{provider}",model="{model}",status="{status}"}} {count}')
    lines.extend(
        [
            "# HELP poy_dty_llm_latency_seconds LLM latency histogram.",
            "# TYPE poy_dty_llm_latency_seconds histogram",
        ]
    )
    for key, buckets in sorted(LLM_LATENCY_HISTOGRAM.items()):
        provider, model, status = key.split(" ", 2)
        base = {"provider": provider, "model": model, "status": status}
        for bucket in [*map(str, LLM_LATENCY_BUCKETS), "+Inf"]:
            lines.append(f"poy_dty_llm_latency_seconds_bucket{{{_labels({**base, 'le': bucket})}}} {buckets[bucket]}")
        lines.append(f"poy_dty_llm_latency_seconds_sum{{{_labels(base)}}} {buckets['sum']}")
        lines.append(f"poy_dty_llm_latency_seconds_count{{{_labels(base)}}} {buckets['+Inf']}")
    lines.extend(
        [
            "# HELP poy_dty_llm_tokens_est_total Estimated LLM prompt and completion tokens.",
            "# TYPE poy_dty_llm_tokens_est_total counter",
        ]
    )
    for key, count in sorted(LLM_TOKEN_COUNTER.items()):
        provider, model, token_type = key.split(" ", 2)
        labels = _labels({"provider": provider, "model": model, "type": token_type})
        lines.append(f"poy_dty_llm_tokens_est_total{{{labels}}} {count}")
    lines.extend(
        [
            "# HELP poy_dty_source_fetch_total Source fetch attempts by source and status.",
            "# TYPE poy_dty_source_fetch_total counter",
        ]
    )
    for key, count in sorted(SOURCE_FETCH_COUNTER.items()):
        source_id, status = key.split(" ", 1)
        lines.append(f'poy_dty_source_fetch_total{{source_id="{source_id}",status="{status}"}} {count}')
    lines.extend(
        [
            "# HELP poy_dty_rate_limited_total Requests rejected by rate limit bucket.",
            "# TYPE poy_dty_rate_limited_total counter",
        ]
    )
    for bucket, count in sorted(RATE_LIMIT_COUNTER.items()):
        lines.append(f'poy_dty_rate_limited_total{{bucket="{bucket}"}} {count}')
    lines.extend(
        [
            "# HELP poy_dty_source_freshness_seconds Age of the newest accepted record by source.",
            "# TYPE poy_dty_source_freshness_seconds gauge",
        ]
    )
    for source_id, age_seconds in sorted(SOURCE_FRESHNESS_SECONDS.items()):
        lines.append(f'poy_dty_source_freshness_seconds{{source_id="{source_id}"}} {age_seconds}')
    lines.extend(
        [
            "# HELP poy_dty_scheduler_last_success_unixtime Last successful scheduler completion time.",
            "# TYPE poy_dty_scheduler_last_success_unixtime gauge",
        ]
    )
    for scheduler, timestamp in sorted(SCHEDULER_LAST_SUCCESS_UNIX_SECONDS.items()):
        lines.append(f'poy_dty_scheduler_last_success_unixtime{{scheduler="{scheduler}"}} {timestamp}')
    lines.extend(
        [
            "# HELP poy_dty_scheduler_last_attempt_unixtime Latest scheduler attempt time, including failures.",
            "# TYPE poy_dty_scheduler_last_attempt_unixtime gauge",
        ]
    )
    for scheduler, timestamp in sorted(SCHEDULER_LAST_ATTEMPT_UNIX_SECONDS.items()):
        lines.append(f'poy_dty_scheduler_last_attempt_unixtime{{scheduler="{scheduler}"}} {timestamp}')
    lines.extend(
        [
            "# HELP poy_dty_scheduler_last_attempt_status Latest scheduler attempt status as a labeled gauge.",
            "# TYPE poy_dty_scheduler_last_attempt_status gauge",
        ]
    )
    for scheduler, status in sorted(SCHEDULER_LAST_STATUS.items()):
        lines.append(f'poy_dty_scheduler_last_attempt_status{{scheduler="{scheduler}",status="{status}"}} 1')
    lines.extend(
        [
            "# HELP poy_dty_scheduler_failures_total Scheduler failures by terminal status.",
            "# TYPE poy_dty_scheduler_failures_total counter",
        ]
    )
    for key, count in sorted(SCHEDULER_FAILURE_COUNTER.items()):
        scheduler, status = key.split(" ", 1)
        lines.append(f'poy_dty_scheduler_failures_total{{scheduler="{scheduler}",status="{status}"}} {count}')
    lines.extend(
        [
            "# HELP poy_dty_scheduler_last_duration_seconds Duration of the latest scheduler attempt.",
            "# TYPE poy_dty_scheduler_last_duration_seconds gauge",
        ]
    )
    for scheduler, duration in sorted(SCHEDULER_LAST_DURATION_SECONDS.items()):
        lines.append(f'poy_dty_scheduler_last_duration_seconds{{scheduler="{scheduler}"}} {duration}')
    lines.extend(
        [
            "# HELP poy_dty_scheduler_backlog Pending work items reported by a scheduler.",
            "# TYPE poy_dty_scheduler_backlog gauge",
        ]
    )
    for scheduler, backlog in sorted(SCHEDULER_BACKLOG.items()):
        lines.append(f'poy_dty_scheduler_backlog{{scheduler="{scheduler}"}} {backlog}')
    lines.extend(_intelligence_metrics_lines())
    return "\n".join(lines) + "\n"

"""Bounded, low-cardinality metrics for the intelligence domain.

Label values are constrained: ``provider`` comes from the declared source-id
set, ``status``/``reason``/``action`` come from frozen enums. No URLs, titles,
item/event IDs, or other high-cardinality values are ever used as labels.
``observability.metrics_text`` renders these families at ``/metrics``.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime

from .identity import BRIEF_STATUSES, FEEDBACK_ACTIONS, RUN_STATUSES, RUN_TYPES

_RUN_TOTAL: dict[tuple[str, str, str], int] = defaultdict(int)
_RUN_DURATION_SECONDS: dict[tuple[str, str, str], list[float]] = defaultdict(list)
_ITEMS_TOTAL: dict[tuple[str, str], int] = defaultdict(int)
_EVENTS_TOTAL: dict[str, int] = defaultdict(int)
_PROJECTION_LAG: dict[str, float] = {}
_BRIEF_LAST_SUCCESS_UNIXTIME: float | None = None
_BRIEF_STATUS: dict[str, int] = defaultdict(int)
_BRIEF_EVENT_COUNT: int = 0
_RIGHTS_BLOCKED_TOTAL: dict[tuple[str, str], int] = defaultdict(int)
_SOURCE_DRIFT: dict[str, int] = defaultdict(int)
_FEEDBACK_TOTAL: dict[str, int] = defaultdict(int)
_MAP_FEATURES: dict[str, int] = defaultdict(int)

_ALLOWED_REASON_CODES = frozenset(
    {
        "rights_tightened",
        "metadata_only_storage",
        "retention_expired",
        "content_unavailable",
    }
)
_ALLOWED_PROVIDERS = frozenset(
    {
        "none",
        "legacy_news_articles",
        "usgs_eq_m45_weekly",
        "gdelt_oil_geopolitics_rss",
    }
)
_ALLOWED_ITEM_RESULTS = frozenset({"inserted", "existing", "rejected"})
_ALLOWED_EVENT_RESULTS = frozenset({"inserted", "existing", "rejected"})


def _bound_provider(provider: str | None) -> str:
    candidate = provider or "none"
    return candidate if candidate in _ALLOWED_PROVIDERS else "other"


def observe_run(*, run_type: str, provider: str | None, status: str, duration_seconds: float) -> None:
    if run_type not in RUN_TYPES or status not in RUN_STATUSES:
        return
    key = (run_type, _bound_provider(provider), status)
    _RUN_TOTAL[key] += 1
    _RUN_DURATION_SECONDS[key].append(max(0.0, float(duration_seconds)))
    del _RUN_DURATION_SECONDS[key][:-50]  # bounded memory


def observe_projection_items(*, provider: str, result: str, count: int) -> None:
    if result in _ALLOWED_ITEM_RESULTS:
        _ITEMS_TOTAL[(_bound_provider(provider), result)] += max(0, int(count))


def observe_events(*, result: str, count: int) -> None:
    if result in _ALLOWED_EVENT_RESULTS:
        _EVENTS_TOTAL[result] += max(0, int(count))


def observe_projection_lag(*, provider: str, lag_seconds: float) -> None:
    _PROJECTION_LAG[_bound_provider(provider)] = max(0.0, float(lag_seconds))


def observe_brief(*, status: str, event_count: int | None = None) -> None:
    if status not in BRIEF_STATUSES:
        return
    _BRIEF_STATUS[status] += 1
    if status in ("ready", "ready_with_gaps", "no_material_events"):
        global _BRIEF_LAST_SUCCESS_UNIXTIME, _BRIEF_EVENT_COUNT
        import time

        _BRIEF_LAST_SUCCESS_UNIXTIME = float(time.time())
        if event_count is not None:
            _BRIEF_EVENT_COUNT = max(0, int(event_count))


def observe_rights_blocked(*, provider: str, reason: str) -> None:
    if reason not in _ALLOWED_REASON_CODES:
        reason = "content_unavailable"
    _RIGHTS_BLOCKED_TOTAL[(_bound_provider(provider), reason)] += 1


def observe_source_drift(*, provider: str) -> None:
    _SOURCE_DRIFT[_bound_provider(provider)] += 1


def observe_feedback(*, action: str) -> None:
    if action in FEEDBACK_ACTIONS:
        _FEEDBACK_TOTAL[action] += 1


def observe_map_features(count: int) -> None:
    bucket = (
        "0-99"
        if count < 100
        else "100-499"
        if count < 500
        else "500-999"
        if count < 1000
        else "1000+"
    )
    _MAP_FEATURES[bucket] += 1


def deep_health_summary() -> dict[str, object]:
    persisted = _persistent_snapshot()
    return {
        "runs_total": sum(persisted.get("run_total", _RUN_TOTAL).values()),
        "brief_status_counts": dict(persisted.get("brief_status", _BRIEF_STATUS)),
        "brief_last_success_unixtime": persisted.get(
            "brief_last_success", _BRIEF_LAST_SUCCESS_UNIXTIME
        ),
        "rights_blocked_total": sum(_RIGHTS_BLOCKED_TOTAL.values()),
        "source_drift_total": sum(_SOURCE_DRIFT.values()),
        "feedback_total": sum(persisted.get("feedback_total", _FEEDBACK_TOTAL).values()),
    }


def _persistent_snapshot() -> dict[str, object]:
    """Rebuild durable counters from v37 append-only tables for cross-process jobs."""

    from . import storage as domain_storage

    try:
        with closing(domain_storage.connect_domain_readonly()) as connection:
            run_rows = connection.execute(
                """
                SELECT latest.run_type, latest.provider_id, latest.status,
                       latest.duration_ms, latest.finished_at, grouped.n,
                       grouped.inserted_count, grouped.existing_count,
                       grouped.rejected_count
                FROM intelligence_runs latest
                JOIN (
                  SELECT run_type, COALESCE(provider_id, '') AS provider_key, status,
                         COUNT(*) AS n, MAX(append_seq) AS latest_seq,
                         SUM(inserted_count) AS inserted_count,
                         SUM(existing_count) AS existing_count,
                         SUM(rejected_count) AS rejected_count
                  FROM intelligence_runs
                  GROUP BY run_type, COALESCE(provider_id, ''), status
                ) grouped ON grouped.latest_seq = latest.append_seq
                ORDER BY latest.run_type, latest.provider_id, latest.status
                """
            ).fetchall()
            brief_rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM intelligence_daily_briefs GROUP BY status"
            ).fetchall()
            latest_brief = connection.execute(
                "SELECT status, released_at, selected_event_revision_ids_json "
                "FROM intelligence_daily_briefs "
                "WHERE status IN ('ready','ready_with_gaps','no_material_events') "
                "ORDER BY append_seq DESC LIMIT 1"
            ).fetchone()
            feedback_rows = connection.execute(
                "SELECT action, COUNT(*) AS n FROM intelligence_feedback GROUP BY action"
            ).fetchall()
    except (sqlite3.Error, OSError):
        return {}

    run_total: dict[tuple[str, str, str], int] = defaultdict(int)
    run_duration: dict[tuple[str, str, str], float] = {}
    item_total: dict[tuple[str, str], int] = defaultdict(int)
    event_total: dict[str, int] = defaultdict(int)
    projection_lag: dict[str, float] = {}
    now = datetime.now().astimezone()
    for row in run_rows:
        run_type = str(row["run_type"])
        status = str(row["status"])
        provider = _bound_provider(row["provider_id"])
        if run_type in RUN_TYPES and status in RUN_STATUSES:
            key = (run_type, provider, status)
            run_total[key] += max(0, int(row["n"]))
            run_duration[key] = max(0.0, float(row["duration_ms"]) / 1000.0)
        if run_type in ("provider", "projection"):
            for result, column in (
                ("inserted", "inserted_count"),
                ("existing", "existing_count"),
                ("rejected", "rejected_count"),
            ):
                item_total[(provider, result)] += max(0, int(row[column]))
            if row["finished_at"]:
                try:
                    finished = datetime.fromisoformat(str(row["finished_at"]).replace("Z", "+00:00"))
                    lag = max(0.0, (now - finished.astimezone()).total_seconds())
                    projection_lag[provider] = min(projection_lag.get(provider, lag), lag)
                except ValueError:
                    pass
        if run_type == "clustering":
            event_total["inserted"] += max(0, int(row["inserted_count"]))

    brief_status: dict[str, int] = defaultdict(int)
    brief_last_success: float | None = None
    brief_event_count = 0
    for row in brief_rows:
        status = str(row["status"])
        if status not in BRIEF_STATUSES:
            continue
        brief_status[status] += max(0, int(row["n"]))
    if latest_brief is not None:
        try:
            released = datetime.fromisoformat(str(latest_brief["released_at"]).replace("Z", "+00:00"))
            brief_last_success = released.timestamp()
            brief_event_count = len(json.loads(str(latest_brief["selected_event_revision_ids_json"])))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    feedback_total = {
        str(row["action"]): int(row["n"])
        for row in feedback_rows
        if str(row["action"]) in FEEDBACK_ACTIONS
    }
    return {
        "run_total": run_total,
        "run_duration": run_duration,
        "item_total": item_total,
        "event_total": event_total,
        "projection_lag": projection_lag,
        "brief_status": brief_status,
        "brief_last_success": brief_last_success,
        "brief_event_count": brief_event_count,
        "feedback_total": feedback_total,
    }


def metrics_lines() -> list[str]:
    lines: list[str] = []
    persisted = _persistent_snapshot()
    run_total = persisted.get("run_total", _RUN_TOTAL)
    run_duration = persisted.get("run_duration", {})
    item_total = persisted.get("item_total", _ITEMS_TOTAL)
    event_total = persisted.get("event_total", _EVENTS_TOTAL)
    projection_lag = persisted.get("projection_lag", _PROJECTION_LAG)
    brief_status = persisted.get("brief_status", _BRIEF_STATUS)
    brief_last_success = persisted.get("brief_last_success", _BRIEF_LAST_SUCCESS_UNIXTIME)
    brief_event_count = persisted.get("brief_event_count", _BRIEF_EVENT_COUNT)
    feedback_total = persisted.get("feedback_total", _FEEDBACK_TOTAL)

    def family(name: str, metric_type: str, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {metric_type}")

    family("poy_dty_intelligence_runs_total", "counter", "Intelligence pipeline runs by type/provider/status.")
    for (run_type, provider, status), value in sorted(run_total.items()):
        lines.append(
            f'poy_dty_intelligence_runs_total{{run_type="{run_type}",provider="{provider}",status="{status}"}} {value}'
        )
    family(
        "poy_dty_intelligence_run_duration_seconds",
        "gauge",
        "Last recorded intelligence run duration in seconds.",
    )
    duration_items = (
        run_duration.items()
        if run_duration
        else ((key, values[-1]) for key, values in _RUN_DURATION_SECONDS.items() if values)
    )
    for (run_type, provider, status), value in sorted(duration_items):
        label = (
            "poy_dty_intelligence_run_duration_seconds"
            f'{{run_type="{run_type}",provider="{provider}",status="{status}"}}'
        )
        lines.append(f"{label} {float(value):.3f}")
    family("poy_dty_intelligence_items_total", "counter", "Projected intelligence items by provider/result.")
    for (provider, result), value in sorted(item_total.items()):
        lines.append(f'poy_dty_intelligence_items_total{{provider="{provider}",result="{result}"}} {value}')
    family("poy_dty_intelligence_events_total", "counter", "Clustered intelligence events by result.")
    for result, value in sorted(event_total.items()):
        lines.append(f'poy_dty_intelligence_events_total{{result="{result}"}} {value}')
    family(
        "poy_dty_intelligence_projection_lag_seconds",
        "gauge",
        "Seconds since the last projection output per provider.",
    )
    for provider, lag in sorted(projection_lag.items()):
        lines.append(f'poy_dty_intelligence_projection_lag_seconds{{provider="{provider}"}} {lag:.3f}')
    family(
        "poy_dty_intelligence_brief_last_success_unixtime",
        "gauge",
        "Unix time of the last publishable brief terminal state.",
    )
    if brief_last_success is not None:
        lines.append(f"poy_dty_intelligence_brief_last_success_unixtime {float(brief_last_success):.0f}")
    family("poy_dty_intelligence_brief_status", "counter", "Brief terminal states observed.")
    for status, value in sorted(brief_status.items()):
        lines.append(f'poy_dty_intelligence_brief_status{{status="{status}"}} {value}')
    family("poy_dty_intelligence_brief_event_count", "gauge", "Event count of the last publishable brief.")
    lines.append(f"poy_dty_intelligence_brief_event_count {int(brief_event_count)}")
    family("poy_dty_intelligence_rights_blocked_total", "counter", "Rights-blocked storage/display actions.")
    for (provider, reason), value in sorted(_RIGHTS_BLOCKED_TOTAL.items()):
        lines.append(f'poy_dty_intelligence_rights_blocked_total{{provider="{provider}",reason="{reason}"}} {value}')
    family("poy_dty_intelligence_source_drift", "counter", "Source catalog metadata drift detections.")
    for provider, value in sorted(_SOURCE_DRIFT.items()):
        lines.append(f'poy_dty_intelligence_source_drift{{provider="{provider}"}} {value}')
    family("poy_dty_intelligence_feedback_total", "counter", "Operator feedback events by action.")
    for action, value in sorted(feedback_total.items()):
        lines.append(f'poy_dty_intelligence_feedback_total{{action="{action}"}} {value}')
    family("poy_dty_intelligence_map_features_returned_bucket", "counter", "Map response feature-count buckets.")
    for bucket, value in sorted(_MAP_FEATURES.items()):
        lines.append(f'poy_dty_intelligence_map_features_returned_bucket{{bucket="{bucket}"}} {value}')
    return lines

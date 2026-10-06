from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

POLICY_VERSION = "agent-production-readiness.v1"
DAILY_TIME_ZONE = ZoneInfo("Asia/Shanghai")
DAILY_CUTOFF_HOUR = 8
DAILY_CUTOFF_MINUTE = 10
LOW_CONFIDENCE_CAP = 0.35

# The generic aggregator accepts any valid policy for testing. This is the sole
# formal production policy and is intentionally versioned separately.
PRODUCTION_READINESS_POLICY: dict[str, int | float] = {
    "minimum_sample_count": 30,
    "minimum_pass_rate": 0.95,
    "maximum_review_rate": 0.20,
    "maximum_fail_rate": 0.02,
    "minimum_trace_complete_rate": 1.0,
    "minimum_redaction_safe_rate": 1.0,
    "maximum_fallback_rate": 0.10,
    "maximum_residual_state_count": 0,
}


def daily_governance_window(as_of: datetime) -> dict[str, str]:
    """Return the completed 24-hour Beijing window for one daily assessment."""

    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("agent_governance_as_of_requires_timezone")
    local = as_of.astimezone(DAILY_TIME_ZONE)
    cutoff = local.replace(
        hour=DAILY_CUTOFF_HOUR,
        minute=DAILY_CUTOFF_MINUTE,
        second=0,
        microsecond=0,
    )
    if local < cutoff:
        cutoff -= timedelta(days=1)
    return {
        "start": (cutoff - timedelta(days=1)).isoformat(),
        "end": cutoff.isoformat(),
    }


def daily_governance_report(
    aggregate: Mapping[str, Any],
    *,
    evaluated_at: datetime,
) -> dict[str, Any]:
    """Convert a governed aggregate into the fixed daily operating posture."""

    window = daily_governance_window(evaluated_at)
    if aggregate.get("window") != window:
        raise ValueError("agent_governance_window_mismatch")
    if aggregate.get("policy_applied") is not True:
        raise ValueError("agent_governance_policy_not_applied")
    production_ready = aggregate.get("production_ready")
    if type(production_ready) is not bool:
        raise ValueError("agent_governance_readiness_invalid")
    failed_checks = aggregate.get("failed_policy_checks")
    if not isinstance(failed_checks, list) or any(not isinstance(item, str) for item in failed_checks):
        raise ValueError("agent_governance_failed_checks_invalid")
    if production_ready != (not failed_checks):
        raise ValueError("agent_governance_readiness_inconsistent")

    return {
        "policy_version": POLICY_VERSION,
        "evaluated_at": evaluated_at.astimezone(DAILY_TIME_ZONE).isoformat(),
        "cadence": "daily_08:10_asia_shanghai",
        "window": window,
        "production_ready": production_ready,
        "delivery_mode": "standard" if production_ready else "low_confidence",
        "confidence_cap": None if production_ready else LOW_CONFIDENCE_CAP,
        "human_review_required": False,
        "failed_policy_checks": failed_checks,
    }


def unavailable_daily_governance_report(*, evaluated_at: datetime) -> dict[str, Any]:
    """Return the safe no-manual-review posture if the daily read cannot complete."""

    return {
        "policy_version": POLICY_VERSION,
        "evaluated_at": evaluated_at.astimezone(DAILY_TIME_ZONE).isoformat(),
        "cadence": "daily_08:10_asia_shanghai",
        "window": daily_governance_window(evaluated_at),
        "production_ready": False,
        "delivery_mode": "low_confidence",
        "confidence_cap": LOW_CONFIDENCE_CAP,
        "human_review_required": False,
        "failed_policy_checks": ["window_collection_unavailable"],
    }

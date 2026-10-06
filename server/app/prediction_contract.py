from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

# Read-time compatibility includes historical 14-day predictions. New formal
# write horizons are intentionally enforced by PredictionCreate instead.
HORIZON_DAYS = {"1d": 1, "7d": 7, "14d": 14, "30d": 30}
FORMAL_DIRECTIONS = {"利多", "利空", "中性", "偏强", "偏弱", "中性偏强", "中性偏弱", "上涨", "下跌", "上行", "下行"}


def is_legacy_directional_prediction_record(record: dict[str, Any]) -> bool:
    """Return whether a legacy ledger row is readable as historical direction data."""

    return str(record.get("direction", "")).strip() in FORMAL_DIRECTIONS


def is_formal_prediction_record(record: dict[str, Any]) -> bool:
    """Deprecated compatibility alias; this predicate never establishes formal status."""

    return is_legacy_directional_prediction_record(record)


def is_proof_verified_formal_batch(record: dict[str, Any]) -> bool:
    """Return whether a stored record carries the immutable formal-batch class."""

    return (
        record.get("record_kind") == "formal_batch_revision"
        and record.get("governance_status") == "proof_verified"
        and bool(record.get("prediction_batch_id"))
        and bool(record.get("revision_id"))
        and bool(record.get("assessment_id"))
        and bool(record.get("data_snapshot_id"))
    )


def enrich_prediction_record(record: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Add customer-facing schedule state without changing the persisted ledger."""
    enriched = dict(record)
    enriched["record_kind"] = "legacy_scalar"
    enriched["governance_status"] = "legacy_unverified"
    enriched["formal_status"] = "historical_legacy_contract"
    enriched["formal_eligible"] = False
    horizon_days = HORIZON_DAYS.get(str(record.get("horizon")))
    enriched["horizon_days"] = horizon_days
    due_at = _due_at(record, horizon_days)
    enriched["due_at"] = due_at.isoformat() if due_at else None
    if record.get("review_status") == "reviewed":
        lifecycle_status = "reviewed"
    elif due_at is None:
        lifecycle_status = "invalid_schedule"
    elif (now or datetime.now(UTC)) < due_at:
        lifecycle_status = "pending_due"
    else:
        lifecycle_status = "due_pending_data"
    enriched["lifecycle_status"] = lifecycle_status
    return enriched


def _due_at(record: dict[str, Any], horizon_days: int | None) -> datetime | None:
    if horizon_days is None:
        return None
    try:
        created_at = datetime.fromisoformat(str(record["created_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at + timedelta(days=horizon_days)

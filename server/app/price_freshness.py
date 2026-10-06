"""Display freshness against the established per-source publication budgets.

These budgets mirror the output-health policy; unavailable/future dates are
never silently fresh. Source identity is required for delayed daily series.
"""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

SOURCE_LAG_DAYS = {
    "eia_petroleum_api": 8, "public_spot_page_refresh": 3,
    "czce_pta_px": 4, "sunsirs_public_commodity_assessment": 3,
    "tnc_polyester_history": 4,
}


def display_price_freshness(observed_at: object, source_id: object, *, now: datetime | None = None) -> dict:
    text = str(observed_at or "")
    result = {"status": "missing", "latest_date": text[:10] or None, "observed_at": text or None}
    now = now or datetime.now(UTC)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    except ValueError:
        return result
    if stamp > now + timedelta(minutes=5):
        return {**result, "status": "available", "reason": "future_observation"}
    start, end = stamp.astimezone(ZoneInfo("Asia/Shanghai")).date(), now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    lag = sum((start + timedelta(days=i)).weekday() < 5 for i in range(1, max(0, (end-start).days) + 1))
    allowed = SOURCE_LAG_DAYS.get(str(source_id))
    if allowed is not None:
        state = "fresh" if lag <= allowed else "stale"
    else:
        state = "fresh" if start == end else "available"
    return {**result, "status": state, "business_day_lag": lag, "max_business_day_lag": allowed}

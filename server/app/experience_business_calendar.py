"""Versioned provisional business-day calendar for Experience maturity."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

CALENDAR_ID = "china-weekday-business-days.v1"
MIN_EXPECTED_DATES = 30


def expected_china_workdays_after(*, prediction_as_of_time: str, count: int = MIN_EXPECTED_DATES) -> list[str]:
    """Return weekday-only observation dates strictly after one prediction date.

    This is deliberately a provisional, versioned Monday-to-Friday rule. It
    excludes no statutory holidays or make-up workdays; a later official China
    calendar must use a new calendar version rather than silently changing
    historical Experience maturity.
    """

    if type(count) is not int or isinstance(count, bool) or count < MIN_EXPECTED_DATES:
        raise ValueError("experience_calendar_count_invalid")
    prediction_time = _zoned_time(prediction_as_of_time)
    cursor = prediction_time.date() + timedelta(days=1)
    result: list[str] = []
    while len(result) < count:
        if cursor.weekday() < 5:
            result.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return result


def _zoned_time(value: Any) -> datetime:
    if type(value) is not str:
        raise ValueError("experience_calendar_prediction_as_of_invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("experience_calendar_prediction_as_of_invalid") from None
    if parsed.tzinfo is None:
        raise ValueError("experience_calendar_prediction_as_of_invalid")
    return parsed

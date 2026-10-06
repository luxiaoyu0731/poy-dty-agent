from __future__ import annotations

import pytest

from app.experience_business_calendar import CALENDAR_ID, expected_china_workdays_after


def test_weekday_calendar_starts_after_prediction_and_skips_weekends() -> None:
    assert CALENDAR_ID == "china-weekday-business-days.v1"
    assert expected_china_workdays_after(prediction_as_of_time="2026-08-07T09:30:00+08:00", count=30)[:5] == [
        "2026-08-10",
        "2026-08-11",
        "2026-08-12",
        "2026-08-13",
        "2026-08-14",
    ]


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("2026-08-07T09:30:00", "experience_calendar_prediction_as_of_invalid"),
        ("not-a-time", "experience_calendar_prediction_as_of_invalid"),
    ],
)
def test_calendar_rejects_unzoned_or_malformed_prediction_time(value: str, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        expected_china_workdays_after(prediction_as_of_time=value)

"""Source-specific, versioned publication calendars (not settlement calendars).

CFETS parity is published at 09:15 on statutory working days, including
make-up working weekends. Do not reuse this calendar for futures or spot prices.
Sources: https://www.safe.gov.cn/safe/2014/0702/21881.html
https://www.chinamoney.com.cn/chinese/rdgz/20251219/3254577.html
The annual schedule must be reviewed before adding another year; unknown
years never grant an extension to the ordinary freshness limit.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

CALENDAR_ID = "cfets-parity-statutory-workdays.2026.v1"
SHANGHAI = ZoneInfo("Asia/Shanghai")
_CLOSURES = (
    ("2026-01-01", "2026-01-03"),
    ("2026-02-15", "2026-02-23"),
    ("2026-04-04", "2026-04-06"),
    ("2026-05-01", "2026-05-05"),
    ("2026-06-19", "2026-06-21"),
    ("2026-09-25", "2026-09-27"),
    ("2026-10-01", "2026-10-07"),
)
HOLIDAYS = frozenset(
    date.fromisoformat(start) + timedelta(days=offset)
    for start, end in _CLOSURES
    for offset in range((date.fromisoformat(end) - date.fromisoformat(start)).days + 1)
)
MAKEUP_DAYS = frozenset(date.fromisoformat(day) for day in (
    "2026-01-04", "2026-02-14", "2026-02-28", "2026-05-09",
    "2026-09-20", "2026-10-10",
))


def cfets_latest_due_date(now: datetime) -> date | None:
    local = now.astimezone(SHANGHAI)
    if local.year != 2026:
        return None
    cursor = local.date()
    if local.time() < time(9, 15):
        cursor -= timedelta(days=1)
    while cursor.year == 2026:
        if cursor not in HOLIDAYS and (cursor.weekday() < 5 or cursor in MAKEUP_DAYS):
            return cursor
        cursor -= timedelta(days=1)
    return None


def cfets_holiday_carry_reason(source_id: str, observed: datetime, now: datetime) -> str | None:
    """Only waive elapsed-time staleness for the latest due publication.

    Values/units, capture success and transport SLA remain separate checks.
    Date-only source records represent their labelled publication date.
    """
    if source_id != "cfets_cny_parity" or observed > now:
        return None
    due = cfets_latest_due_date(now)
    if due is None or observed.astimezone(SHANGHAI).date() != due:
        return None
    return f"scheduled_publication_carry:{CALENDAR_ID}:latest_due={due.isoformat()}"

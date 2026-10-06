"""Shared time boundary for present-tense evidence questions."""
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def is_current_question(query: str, as_of_time: str | None = None) -> bool:
    if as_of_time or re.search(r"历史|复盘|去年|上月|\bhistor(?:y|ical)\b", query, re.I):
        return False
    return bool(re.search(r"今天|今日|当前|目前|最新|现在|\b(?:today|current|latest)\b", query, re.I))


_HISTORICAL_QUERY = re.compile(r"历史|复盘|去年|上月|当时|\bhistor(?:y|ical)\b", re.I)


def is_historical_question(query: str) -> bool:
    """Historical-reconstruction phrasing: past facts viewed from a past moment.

    Such questions must be answered from the time-filtered immutable corpus,
    not from a live read: evidence arriving after the cutoff is excluded by
    visibility anyway, so the freshness rationale for live reads never applies.
    """
    return bool(_HISTORICAL_QUERY.search(query))


def current_evidence_allowed(kind: str, observed_at: str, *, now: datetime | None = None) -> bool:
    if kind == "knowledge_node":
        return True  # Stable chain definitions may explain the current facts.
    if kind in {"political_case_memory", "knowledge_edge"} or not observed_at:
        return False
    try:
        observed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError:
        try:
            observed = parsedate_to_datetime(observed_at)
        except (ValueError, TypeError, OverflowError):
            return False
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=UTC)
    age = ((now or datetime.now(UTC)) - observed).total_seconds()
    days = 7 if kind in {"market_observation", "industry_observation", "authorized_spot_observation"} else 14
    return 0 <= age <= days * 86400


def explicit_query_dates(query: str) -> set[str]:
    """Validated dates with explicit years; never guess a year or move a cutoff."""
    from .citation import _dates_in

    dates: set[str] = set()
    for year, month, day in _dates_in(query):
        if year is None:
            continue
        try:
            dates.add(datetime(year, month, day).date().isoformat())
        except ValueError:
            continue
    return dates

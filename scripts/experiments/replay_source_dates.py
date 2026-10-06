"""Repair only declared backfill case dates in a separate replay fixture.

No collection/publication substitution. A source case's explicit calendar day
is normalized to UTC midnight as a day boundary, not a precise observed time.
This does not create original article text or upgrade verification status.
"""
from __future__ import annotations

from datetime import date

POLICY = "backfill25y.v2-explicit-case-day"


def dated_records(case: dict, item: dict, event: dict) -> tuple[dict, dict] | None:
    if (item.get("collector_source_id") != "backfill_25y"
        or item.get("parser_version") != "backfill25y.v1"
        or item.get("projection_source_id") != case.get("case_id")
        or event.get("event_id") != case.get("case_id")
        or item.get("occurred_at") or item.get("published_at")):
        return None
    try:
        day = date.fromisoformat(case["event_date"]).isoformat()
    except (ValueError, KeyError, TypeError):
        return None
    title = str(case.get("title") or "")
    if (not title or item.get("title") != title[:120] or item.get("excerpt") != title[:200]
        or str(item.get("created_at", ""))[:10] != day
        or str(event.get("created_at", ""))[:10] != day):
        return None
    new_item = {**item, "occurred_at": day + "T00:00:00+00:00", "parser_version": POLICY}
    # Keep published_at unknown, grades and original facts intact. Only the
    # isolated replay date provenance and anchor identity will change.
    new_event = {**event, "analysis_version": POLICY,
                 "gaps": [*(event.get("gaps") or []),
                          f"隔离回放日期来自案例 {case['case_id']} 的 event_date={day}，精度为日；UTC零点仅为日期边界，未补造发布时间或原文"]}
    return new_item, new_event

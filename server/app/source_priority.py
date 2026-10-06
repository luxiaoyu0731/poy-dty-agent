from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Authorized CCF observations are the canonical first-tier business source.
# Lower-tier sources may be retained as separate reference observations, but
# must never replace or fill a CCF series implicitly.
CCF_SOURCE_PRIORITY = {
    "ccf_dom_daily": 0,
    "ccf_average_price": 1,
    "ccf_manual_export": 2,
}
CCF_SOURCE_IDS = frozenset(CCF_SOURCE_PRIORITY)


def is_ccf_source(source_id: object) -> bool:
    return str(source_id or "").strip().lower() in CCF_SOURCE_IDS


def preferred_ccf_source(rows: Iterable[dict[str, Any]]) -> str | None:
    source_ids = {
        str(row.get("source_id") or "").strip().lower() for row in rows if is_ccf_source(row.get("source_id"))
    }
    return min(source_ids, key=lambda item: CCF_SOURCE_PRIORITY[item], default=None)

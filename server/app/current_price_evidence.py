"""Read adapters for observational analysis; never promote forecast eligibility."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .seven_product_contract import LABEL_REGISTRY
from .seven_product_forecast import load_current_label_series


def current_price_rows(product: str, as_of: datetime) -> list[dict[str, Any]]:
    target = product.lower()
    if target not in LABEL_REGISTRY:
        return []
    loaded = load_current_label_series(target, as_of)
    groups: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for point in loaded.points:
        visible = datetime.fromisoformat(point.visible_at.replace("Z", "+00:00"))
        if visible.tzinfo is None:
            visible = visible.replace(tzinfo=UTC)
        if visible > as_of or point.observed_at[:10] > as_of.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat():
            continue
        if not math.isfinite(point.value) or point.value <= 0:
            continue
        key = (point.source_id, point.unit, point.semantic_series_id or LABEL_REGISTRY[target].series_id)
        day = point.observed_at[:10]
        group = groups.setdefault(key, {})
        row = {
            "observation_id": point.observation_id,
            "product": product.upper(),
            "metric": "spot_quote" if target not in {"px", "pta"} else "futures_settlement",
            "value": point.value,
            "unit": point.unit,
            "observed_at": day,
            "created_at": point.visible_at,
            "source_id": point.source_id,
            "evidence_url": point.source_url,
            "raw": {"series_id": key[2]},
            "source_matches_label": loaded.source_matches_label,
            "data_gaps": list(loaded.data_gaps),
        }
        if day not in group or visible > datetime.fromisoformat(group[day]["created_at"].replace("Z", "+00:00")):
            group[day] = row
    if not groups:
        return []
    # One native-unit/source/semantic basis only. A one-point new basis stays
    # insufficient; never borrow an old basis to manufacture a trend.
    chosen = max(groups.values(), key=lambda rows: max(rows))
    return [chosen[day] for day in sorted(chosen)]


def merge_current_price_rows(legacy: list[dict[str, Any]], *, as_of: datetime) -> list[dict[str, Any]]:
    rows = list(legacy)
    for product in ("PX", "PTA", "MEG", "POY", "DTY"):
        current = current_price_rows(product, as_of)
        if current:
            rows = [row for row in rows if str(row.get("product", "")).upper() != product] + current
    return rows

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx

from .fetchers import eia_brent_capture_revisions
from .models import PriceComparisonResponse, PricePoint, PriceSeriesSummary
from .settings import settings
from .storage import (
    TimestampInvalidError,
    bulk_create_market_observations_with_capture_revisions,
    create_market_observation,
    list_market_observations,
)

CRUDE_SERIES = {
    "RWTC": "WTI",
    "RBRTE": "Brent",
    "DCOILWTICO": "WTI",
    "DCOILBRENTEU": "Brent",
}

EIA_HISTORY_URL = "https://api.eia.gov/v2/petroleum/pri/spt/data/"
FRED_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"


def build_price_comparison(
    *,
    product: str = "crude_oil",
    start: str | None = None,
    end: str | None = None,
    limit: int = 240,
) -> PriceComparisonResponse:
    rows = [
        row
        for row in list_market_observations(
            product=product if product != "all" else None,
            start=start,
            end=end,
            units=tuple(_CRUDE_PRICE_UNIT_ALIASES) if product == "crude_oil" else None,
            limit=None,
        )
        if row.get("value") is not None and _in_window(str(row.get("observed_at", "")), start=start, end=end)
    ]
    if product == "crude_oil":
        rows = _canonical_crude_price_rows(rows)
    grouped: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        # A spot observation must never bridge into a futures close (nor an
        # open/high/low). Each comparison keeps its publisher and metric.
        label = (
            f"{_series_label(row)} · {row.get('source_id', '')} · {row.get('indicator', '')} · {row.get('unit', '')}"
        )
        grouped.setdefault(label, []).append(row)
    summaries = [
        _summarize(label, sorted(series_rows, key=lambda r: str(r.get("observed_at", "")))[-max(limit, 1) :])
        for label, series_rows in sorted(grouped.items())
    ]
    return PriceComparisonResponse(product=product, summaries=summaries, conclusion=_comparison_conclusion(summaries))


async def fetch_and_store_price_history(*, start: str, end: str) -> dict[str, object]:
    eia_observations: list[dict[str, object]] = []
    stored: list[dict[str, object]] = []
    if settings.eia_api_key:
        eia_observations = await _fetch_eia_history(start=start, end=end)
        captured_at = datetime.now(UTC).isoformat()
        stored.extend(
            bulk_create_market_observations_with_capture_revisions(
                eia_observations,
                eia_brent_capture_revisions(
                    eia_observations,
                    captured_at=captured_at,
                    source_url=EIA_HISTORY_URL,
                ),
                id_factory=lambda: str(uuid4()),
                capture_revision_id_factory=lambda: str(uuid4()),
            )
        )
    fred_observations: list[dict[str, object]] = []
    if settings.fred_api_key:
        fred_observations = await _fetch_fred_history(start=start, end=end)
    for item in fred_observations:
        try:
            stored.append(create_market_observation(observation_id=str(uuid4()), payload=item))
        except TimestampInvalidError:
            continue
    return {
        "fetched": len(eia_observations) + len(fred_observations),
        "stored": len(stored),
        "start": start,
        "end": end,
    }


async def _fetch_eia_history(*, start: str, end: str) -> list[dict[str, object]]:
    settings.require_outbound_url_allowed(EIA_HISTORY_URL)
    params = [
        ("frequency", "daily"),
        ("data[0]", "value"),
        ("facets[series][]", "RWTC"),
        ("facets[series][]", "RBRTE"),
        ("start", start),
        ("end", end),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
        ("offset", "0"),
        ("length", "500"),
        ("api_key", settings.eia_api_key),
    ]
    source_rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for page in range(20):
            page_params = [(key, str(page * 500) if key == "offset" else value) for key, value in params]
            response = await client.get(EIA_HISTORY_URL, params=page_params)
            response.raise_for_status()
            block = response.json().get("response", {})
            data = block.get("data")
            if not isinstance(data, list):
                raise ValueError("EIA history response missing data")
            if not data:
                break
            keys = {(str(row.get("series", "")), str(row.get("period", ""))) for row in data}
            if keys <= seen:
                raise ValueError("EIA history pagination did not advance")
            source_rows.extend(
                row for row in data if (str(row.get("series", "")), str(row.get("period", ""))) not in seen
            )
            seen.update(keys)
            total = int(block.get("total") or 0)
            if (total and (page * 500 + len(data)) >= total) or (not total and len(data) < 500):
                break
            await asyncio.sleep(0.2)
        else:
            raise ValueError("EIA history exceeds bounded page budget; use smaller date windows")
    rows = []
    for row in source_rows:
        if _to_float(row.get("value")) is None:
            continue
        rows.append(
            {
                "source_id": "eia_petroleum_api",
                "observed_at": str(row.get("period", "")),
                "indicator": str(row.get("series-description") or row.get("series") or "EIA petroleum series"),
                "product": "crude_oil",
                "value": _to_float(row.get("value")),
                "unit": str(row.get("units") or ""),
                "frequency": "daily",
                "region": str(row.get("area-name") or "global"),
                "evidence_url": "https://api.eia.gov/v2/petroleum/pri/spt/data/",
                "notes": "Fetched for price comparison.",
                "raw": row,
            }
        )
    return rows


async def _fetch_fred_history(*, start: str, end: str) -> list[dict[str, object]]:
    settings.require_outbound_url_allowed(FRED_OBSERVATIONS_URL)
    series = [
        ("DCOILWTICO", "WTI Crude Oil Spot Price", "dollars_per_barrel"),
        ("DCOILBRENTEU", "Brent Crude Oil Spot Price", "dollars_per_barrel"),
    ]
    rows = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
        for series_id, label, unit in series:
            response = await client.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": settings.fred_api_key,
                    "file_type": "json",
                    "observation_start": start,
                    "observation_end": end,
                    "sort_order": "asc",
                },
            )
            response.raise_for_status()
            payload = response.json()
            for row in payload.get("observations", []):
                value = _to_float(row.get("value"))
                if value is None:
                    continue
                rows.append(
                    {
                        "source_id": "fred_macro_api",
                        "observed_at": str(row.get("date", "")),
                        "indicator": label,
                        "product": "crude_oil",
                        "value": value,
                        "unit": unit,
                        "frequency": "daily",
                        "region": "global",
                        "evidence_url": f"https://fred.stlouisfed.org/series/{series_id}",
                        "notes": "Fetched for price comparison.",
                        "raw": {"series_id": series_id, **row},
                    }
                )
    return rows


def default_price_window(days: int = 30) -> tuple[str, str]:
    end = datetime.now(UTC).date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _summarize(label: str, rows: list[dict[str, object]]) -> PriceSeriesSummary:
    sorted_rows = sorted(rows, key=lambda item: str(item.get("observed_at", "")))
    points = [
        PricePoint(
            observed_at=str(row.get("observed_at", "")),
            value=float(row["value"]),
            unit=str(row.get("unit", "")),
            source_id=str(row.get("source_id", "")),
            indicator=str(row.get("indicator", "")),
        )
        for row in sorted_rows
        if row.get("value") is not None
    ]
    if not points:
        return PriceSeriesSummary(series_id=label, label=label, points=[], verdict="暂无价格观测")
    first = points[0]
    last = points[-1]
    change_abs = round(last.value - first.value, 4)
    change_pct = round((change_abs / abs(first.value)) * 100, 2) if first.value else 0
    peak = max(points, key=lambda item: item.value)
    trough = min(points, key=lambda item: item.value)
    return PriceSeriesSummary(
        series_id=label,
        label=label,
        points=points,
        first_value=first.value,
        last_value=last.value,
        change_abs=change_abs,
        change_pct=change_pct,
        peak_value=peak.value,
        trough_value=trough.value,
        verdict=_price_verdict(change_pct),
    )


_CRUDE_PRICE_UNIT_ALIASES = {
    "$/BBL": "USD/bbl",
    "USD/BBL": "USD/bbl",
    "DOLLARS_PER_BARREL": "USD/bbl",
    "DOLLARS PER BARREL": "USD/bbl",
}
_NON_PRICE_INDICATOR_TERMS = (
    "volume",
    "spread",
    "cftc",
    "managed money",
    "producer merchant",
    "open interest",
    "ending stocks",
    "inventory",
    "position",
)


def _canonical_crude_price_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return one comparable USD/barrel price observation per benchmark and date."""
    selected: dict[tuple[str, str, str, str], tuple[int, dict[str, object]]] = {}
    for row in rows:
        label = _series_label(row)
        unit = _canonical_crude_price_unit(row.get("unit"))
        if label not in {"WTI", "Brent"} or unit is None or not _is_crude_price_indicator(row):
            continue
        observed_at = str(row.get("observed_at") or "")
        if not observed_at:
            continue
        normalized = {**row, "unit": unit}
        priority = _crude_price_priority(str(row.get("indicator") or ""))
        key = (label, str(row.get("source_id", "")), str(row.get("indicator", "")), observed_at[:10])
        current = selected.get(key)
        if current is None or priority < current[0]:
            selected[key] = (priority, normalized)
    return [item[1] for item in selected.values()]


def _canonical_crude_price_unit(value: object) -> str | None:
    normalized = str(value or "").strip().upper()
    return _CRUDE_PRICE_UNIT_ALIASES.get(normalized)


def _is_crude_price_indicator(row: dict[str, object]) -> bool:
    raw = row.get("raw")
    if isinstance(raw, dict):
        series_id = str(raw.get("series_id") or raw.get("series") or "")
        if series_id in CRUDE_SERIES:
            return True
    indicator = str(row.get("indicator") or "").lower()
    if any(term in indicator for term in _NON_PRICE_INDICATOR_TERMS):
        return False
    return (
        "price" in indicator
        or "spot" in indicator
        or any(term in indicator for term in ("daily close", "daily open", "daily high", "daily low"))
    )


def _crude_price_priority(indicator: str) -> int:
    text = indicator.lower()
    if "daily close" in text:
        return 0
    if "spot" in text or "price" in text:
        return 1
    if "daily open" in text:
        return 2
    if "daily high" in text:
        return 3
    return 4


def _comparison_conclusion(summaries: list[PriceSeriesSummary]) -> str:
    changes = [summary.change_pct for summary in summaries if summary.change_pct is not None]
    if not changes:
        return "暂无可复盘价格序列。"
    average = sum(changes) / len(changes)
    if average >= 3:
        return "价格走势验证了偏强判断。"
    if average <= -3:
        return "价格走势反证了偏强判断，需检查事件冲击是否被需求或供应恢复抵消。"
    return "价格走势未给出单边确认，事件风险需要继续等待行业现货和库存验证。"


def _price_verdict(change_pct: float) -> str:
    if change_pct >= 3:
        return "明显上涨"
    if change_pct <= -3:
        return "明显下跌"
    if change_pct > 0:
        return "小幅上涨"
    if change_pct < 0:
        return "小幅下跌"
    return "持平"


def _series_label(row: dict[str, object]) -> str:
    raw = row.get("raw")
    if isinstance(raw, dict):
        value = raw.get("series_id") or raw.get("series")
        if value and str(value) in CRUDE_SERIES:
            return CRUDE_SERIES[str(value)]
    text = f"{row.get('indicator', '')} {row.get('product', '')}".lower()
    if "brent" in text:
        return "Brent"
    if "wti" in text or "cushing" in text:
        return "WTI"
    return str(row.get("indicator", "series"))


def _in_window(value: str, *, start: str | None, end: str | None) -> bool:
    date = value[:10]
    if start and date < start:
        return False
    return not (end and date > end)


def _to_float(value: object) -> float | None:
    if value in {None, "", "."}:
        return None
    try:
        return float(str(value))
    except ValueError:
        return None

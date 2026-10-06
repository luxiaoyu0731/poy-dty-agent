#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.settings import settings  # noqa: E402
from app.storage import bulk_create_market_observations  # noqa: E402

SOURCE_ID = "yahoo_futures_daily_proxy"
POLICY_NOTE = (
    "Public Yahoo chart futures daily proxy for personal research. "
    "Preserve attribution; do not present as licensed exchange settlement data."
)
# The chart endpoint throttles by source IP and answers back-to-back symbol
# requests with 429 (Brent lost every 08:00 import from 2026-09-27 while WTI,
# fetched first, kept succeeding). Retry with bounded backoff instead of
# dropping the label day.
RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
MAX_FETCH_ATTEMPTS = 4
RETRY_BASE_DELAY_SECONDS = 5.0
RETRY_MAX_DELAY_SECONDS = 30.0
INTER_SYMBOL_DELAY_SECONDS = 2.5
YAHOO_CHART_HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")

SYMBOLS = {
    "WTI": {"symbol": "CL=F", "product": "crude_oil", "unit": "USD/bbl", "label": "WTI front-month futures"},
    "Brent": {"symbol": "BZ=F", "product": "crude_oil", "unit": "USD/bbl", "label": "Brent front-month futures"},
    "RBOB": {"symbol": "RB=F", "product": "gasoline", "unit": "USD/gal", "label": "RBOB gasoline futures"},
    "ULSD": {"symbol": "HO=F", "product": "distillate", "unit": "USD/gal", "label": "ULSD/heating oil futures"},
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import public futures daily OHLCV proxies into market_observations.")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2026-06-02")
    parser.add_argument("--apply", action="store_true", help="Write to the main SQLite database.")
    parser.add_argument("--output-dir", default=".codex-run/trade-futures-proxy")
    parser.add_argument("--symbols", default="WTI,Brent,RBOB,ULSD")
    return parser.parse_args()


def day_to_epoch(day: str) -> int:
    return int(datetime.fromisoformat(day).replace(tzinfo=UTC).timestamp())


def _retry_delay_seconds(attempt: int, retry_after: object) -> float:
    if isinstance(retry_after, str) and retry_after.strip():
        try:
            return min(RETRY_MAX_DELAY_SECONDS, max(1.0, float(retry_after)))
        except ValueError:
            pass
    return min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2**attempt))


def fetch_yahoo_chart(symbol: str, *, start: str, end: str) -> dict:
    # Yahoo rate limits per host; query2 serves the same chart API. Try hosts in
    # order and fail over when a host exhausts retries, so a single throttled
    # host no longer stalls the whole crude label series.
    last_error: Exception | None = None
    for host in YAHOO_CHART_HOSTS:
        try:
            return _fetch_yahoo_chart_from(host, symbol, start=start, end=end)
        except httpx.HTTPStatusError as exc:
            # Non-retryable client errors (e.g. 404 unknown symbol) fail on every
            # host; re-raise immediately instead of failing over.
            if exc.response.status_code not in RETRYABLE_STATUS_CODES:
                raise
            last_error = exc
        except (httpx.TransportError, ValueError) as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def _fetch_yahoo_chart_from(host: str, symbol: str, *, start: str, end: str) -> dict:
    url = f"https://{host}/v8/finance/chart/{symbol}"
    # Yahoo period2 is exclusive enough in practice; add one day so the requested end date is included when present.
    period2 = (datetime.fromisoformat(end) + timedelta(days=1)).date().isoformat()
    params = {"period1": day_to_epoch(start), "period2": day_to_epoch(period2), "interval": "1d", "events": "history"}
    headers = {"User-Agent": "POY-DTY-Agent/1.0 personal-research"}
    settings.require_outbound_url_allowed(url)
    retry_after: object = None
    last_exc: Exception | None = None
    for attempt in range(MAX_FETCH_ATTEMPTS):
        if attempt:
            time.sleep(_retry_delay_seconds(attempt - 1, retry_after))
        try:
            response = httpx.get(url, params=params, headers=headers, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in RETRYABLE_STATUS_CODES:
                raise
            retry_after = exc.response.headers.get("Retry-After")
            last_exc = exc
        except httpx.TransportError as exc:
            retry_after = None
            last_exc = exc
    assert last_exc is not None
    raise last_exc


def parse_chart(payload: dict, *, instrument: str, config: dict, source_url: str) -> list[dict]:
    chart = payload.get("chart")
    result = chart.get("result") if isinstance(chart, dict) else None
    if not isinstance(result, list) or not result:
        error = chart.get("error") if isinstance(chart, dict) else None
        raise ValueError(f"Yahoo chart missing result for {instrument}: {error}")
    item = result[0]
    timestamps = item.get("timestamp")
    quote_list = item.get("indicators", {}).get("quote")
    if not isinstance(timestamps, list) or not isinstance(quote_list, list) or not quote_list:
        raise ValueError(f"Yahoo chart missing timestamps/quote for {instrument}")
    quote = quote_list[0]
    rows: list[dict] = []
    for idx, ts in enumerate(timestamps):
        close = numeric_at(quote.get("close"), idx)
        if close is None:
            continue
        day = datetime.fromtimestamp(int(ts), UTC).date().isoformat()
        rows.extend(
            observation_rows_for_ohlcv(
                day=day,
                instrument=instrument,
                symbol=config["symbol"],
                product=config["product"],
                label=config["label"],
                unit=config["unit"],
                source_url=source_url,
                open_value=numeric_at(quote.get("open"), idx),
                high_value=numeric_at(quote.get("high"), idx),
                low_value=numeric_at(quote.get("low"), idx),
                close_value=close,
                volume=numeric_at(quote.get("volume"), idx),
            )
        )
    return rows


def numeric_at(values: object, idx: int) -> float | None:
    if not isinstance(values, list) or idx >= len(values):
        return None
    value = values[idx]
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def observation_rows_for_ohlcv(
    *,
    day: str,
    instrument: str,
    symbol: str,
    product: str,
    label: str,
    unit: str,
    source_url: str,
    open_value: float | None,
    high_value: float | None,
    low_value: float | None,
    close_value: float,
    volume: float | None,
) -> list[dict]:
    base = {
        "source_id": SOURCE_ID,
        "observed_at": day,
        "product": product,
        "frequency": "daily",
        "region": "global",
        "evidence_url": source_url,
        "notes": f"{label}; {POLICY_NOTE}",
        "raw": {"provider": "yahoo_chart", "symbol": symbol, "instrument": instrument, "quality": "public_proxy"},
    }
    fields = [
        ("close", close_value, unit),
        ("open", open_value, unit),
        ("high", high_value, unit),
        ("low", low_value, unit),
        ("volume", volume, "contracts"),
    ]
    return [
        {
            **base,
            "indicator": f"{instrument} futures daily {field}",
            "value": value,
            "unit": field_unit,
        }
        for field, value, field_unit in fields
        if value is not None
    ]


def close_by_instrument(rows: list[dict]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for row in rows:
        indicator = str(row["indicator"])
        if not indicator.endswith("futures daily close"):
            continue
        instrument = indicator.split(" futures daily close", 1)[0]
        out[instrument][row["observed_at"]] = float(row["value"])
    return dict(out)


def derived_spread_rows(rows: list[dict]) -> list[dict]:
    closes = close_by_instrument(rows)
    days = sorted(set().union(*(set(series) for series in closes.values())) if closes else set())
    out: list[dict] = []
    for day in days:
        wti = closes.get("WTI", {}).get(day)
        brent = closes.get("Brent", {}).get(day)
        rbob = closes.get("RBOB", {}).get(day)
        ulsd = closes.get("ULSD", {}).get(day)
        derived = []
        if brent is not None and wti is not None:
            derived.append(("Brent-WTI futures spread", "crude_oil", brent - wti, "USD/bbl"))
        if rbob is not None and wti is not None:
            derived.append(("RBOB-WTI crack spread proxy", "gasoline", rbob * 42.0 - wti, "USD/bbl"))
        if ulsd is not None and wti is not None:
            derived.append(("ULSD-WTI crack spread proxy", "distillate", ulsd * 42.0 - wti, "USD/bbl"))
        if rbob is not None and ulsd is not None and wti is not None:
            derived.append(
                (
                    "3-2-1 WTI crack spread proxy",
                    "petroleum_products",
                    ((2.0 * rbob + ulsd) * 42.0 - 3.0 * wti) / 3.0,
                    "USD/bbl",
                )
            )
        for indicator, product, value, unit in derived:
            out.append(
                {
                    "source_id": SOURCE_ID,
                    "observed_at": day,
                    "indicator": indicator,
                    "product": product,
                    "value": round(value, 6),
                    "unit": unit,
                    "frequency": "daily",
                    "region": "global",
                    "evidence_url": "https://query1.finance.yahoo.com/v8/finance/chart/",
                    "notes": f"Derived from public Yahoo futures close proxies. {POLICY_NOTE}",
                    "raw": {"provider": "derived_yahoo_chart", "quality": "public_proxy"},
                }
            )
    return out


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    requested = [item.strip() for item in args.symbols.split(",") if item.strip()]
    rows: list[dict] = []
    errors: list[dict] = []
    for index, instrument in enumerate(requested):
        config = SYMBOLS.get(instrument)
        if config is None:
            errors.append({"instrument": instrument, "error": "unsupported_symbol"})
            continue
        if index:
            time.sleep(INTER_SYMBOL_DELAY_SECONDS)
        try:
            payload = fetch_yahoo_chart(config["symbol"], start=args.start, end=args.end)
            rows.extend(
                parse_chart(
                    payload,
                    instrument=instrument,
                    config=config,
                    source_url=f"https://query1.finance.yahoo.com/v8/finance/chart/{config['symbol']}",
                )
            )
        except Exception as exc:  # noqa: BLE001 - keep collecting other public symbols.
            errors.append(
                {"instrument": instrument, "symbol": config["symbol"], "error": f"{type(exc).__name__}: {exc}"}
            )
    rows.extend(derived_spread_rows(rows))
    stored = []
    if args.apply and rows:
        stored = bulk_create_market_observations(rows, lambda: str(uuid4()))
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "start": args.start,
        "end": args.end,
        "apply": bool(args.apply),
        "source_id": SOURCE_ID,
        "policy_note": POLICY_NOTE,
        "accepted_rows": len(rows),
        "stored_rows": len(stored),
        "errors": errors,
        "by_indicator": dict(sorted((indicator, count) for indicator, count in count_by(rows, "indicator").items())),
        "by_product": dict(sorted((product, count) for product, count in count_by(rows, "product").items())),
    }
    (output_dir / "trade-futures-proxy-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    (output_dir / "trade-futures-proxy-rows.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else "")
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def count_by(rows: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = str(row.get(key, ""))
        counts[value] = counts.get(value, 0) + 1
    return counts


if __name__ == "__main__":
    main()

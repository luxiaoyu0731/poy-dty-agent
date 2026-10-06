from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .futures_daily import assign_futures_daily_contract_roles
from .models import SourceConfig
from .settings import settings
from .seven_product_contract import LABEL_REGISTRY

CZCE_DAILY_URL = "https://www.czce.com.cn/cn/DFSStaticFiles/Future/{year}/{day}/FutureDataDaily.txt"
CZCE_SOURCE_ID = "czce_pta_px"
CZCE_PARSER_VERSION = "czce-future-data-daily.v2-delivery-rows"
CZCE_PRODUCTS = {"TA": "PTA", "PX": "PX", "MA": "METHANOL"}
CZCE_CAPTURE_SERIES = {
    "PTA": LABEL_REGISTRY["pta"].series_id,
    "PX": LABEL_REGISTRY["px"].series_id,
    "METHANOL": "methanol.czce.main.settlement.cny_mt",
}
MAIN_RULE = "largest open interest after close; volume tie-break; nearer delivery final tie-break"


@dataclass(slots=True)
class OfficialFuturesResult:
    status: str
    content_type: str
    content_preview: str
    bars: list[dict[str, Any]] = field(default_factory=list)
    capture_revisions: list[dict[str, Any]] = field(default_factory=list)


def parse_czce_daily_text(
    text: str,
    *,
    trade_date: str,
    source_url: str,
    captured_at: str,
    raw_sha256: str,
    skipped_rows: list[dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", trade_date):
        raise ValueError("czce_trade_date_invalid")
    rows: list[dict[str, Any]] = []
    for raw_line in text.lstrip("\ufeff").splitlines():
        cells = [cell.strip() for cell in raw_line.split("|")]
        if len(cells) < 13:
            continue
        contract = cells[0].replace(" ", "").upper()
        match = re.fullmatch(r"([A-Z]+)(\d{3,4})", contract)
        if match is None or match.group(1) not in CZCE_PRODUCTS:
            continue
        try:
            numbers = [_number(value) for value in cells[1:13]]
        except ValueError as exc:
            raise ValueError(f"czce_daily_row_invalid:{contract}") from exc
        pre_settle, open_price, high, low, close, settle = numbers[:6]
        volume, open_interest = numbers[8:10]
        if min(volume, open_interest) < 0 or min(pre_settle, settle) <= 0:
            raise ValueError(f"czce_daily_row_invalid:{contract}")
        if min(open_price, high, low, close) <= 0:
            # The official file can contain delivery-month settlement rows:
            # zero OHLC, positive delivery settlement, but a nonzero turnover
            # count and residual delivery positions. These are not trading bars and
            # must not abort unrelated PTA/PX contracts in the same file.
            token = match.group(2)
            trade_day = date.fromisoformat(trade_date)
            delivery_month = token[-2:] == f"{trade_day.month:02d}"
            delivery_year = token[:-2] == str(trade_day.year)[-len(token[:-2]):]
            delivery_settle = _number(cells[13]) if len(cells) > 13 and cells[13].strip() else 0
            if (all(value == 0 for value in (open_price, high, low, close))
                    and delivery_settle > 0 and delivery_month and delivery_year):
                if skipped_rows is not None:
                    skipped_rows.append({"contract": contract, "trade_date": trade_date,
                                         "reason": "delivery_settlement_without_trading_bar",
                                         "raw_line": raw_line.strip()})
                continue
            if volume == 0:
                continue
            raise ValueError(f"czce_daily_active_row_missing_price:{contract}")
        product = CZCE_PRODUCTS[match.group(1)]
        rows.append(
            {
                "trade_date": trade_date,
                "exchange": "CZCE",
                "product": product,
                "contract_code": contract,
                "contract_role": "listed_contract",
                "term_structure_rank": None,
                "is_main": False,
                "is_continuous": False,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "settle": settle,
                "volume": volume,
                "open_interest": open_interest,
                "change_pct": ((close / pre_settle) - 1) * 100 if pre_settle else None,
                "unit": "CNY/mt",
                "source_publish_time": captured_at,
                "visible_at": captured_at,
                "source_id": CZCE_SOURCE_ID,
                "source_name": "Zhengzhou Commodity Exchange daily futures data",
                "source_url": source_url,
                "source_note": (
                    "Official CZCE daily file; exact exchange publication timestamp is not asserted, "
                    "so first successful capture is the visibility boundary."
                ),
                "main_rule": MAIN_RULE,
                "revision_note": "A changed official file creates a new append-only capture revision.",
                "license_scope": "public_personal_reuse",
                "raw": {
                    "captured_at": captured_at,
                    "parser_version": CZCE_PARSER_VERSION,
                    "pre_settle": pre_settle,
                    "raw_line": raw_line.strip(),
                    "raw_sha256": raw_sha256,
                },
            }
        )
    assign_futures_daily_contract_roles(rows)
    return rows


async def fetch_czce_pta_px_daily(
    source: SourceConfig,
    *,
    lookback_days: int = 10,
    today: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> OfficialFuturesResult:
    if source.source_id != CZCE_SOURCE_ID:
        raise ValueError("czce_source_id_mismatch")
    if not 1 <= lookback_days <= 31:
        raise ValueError("czce_lookback_days_invalid")
    captured_at = datetime.now(UTC).isoformat()
    current_date = today or datetime.now(ZoneInfo("Asia/Shanghai")).date()
    own_client = client is None
    http_client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
    bars: list[dict[str, Any]] = []
    captures: list[dict[str, Any]] = []
    content_types: set[str] = set()
    skipped_rows: list[dict[str, str]] = []
    try:
        for offset in range(lookback_days):
            trade_day = current_date - timedelta(days=offset)
            day_token = trade_day.strftime("%Y%m%d")
            url = CZCE_DAILY_URL.format(year=trade_day.year, day=day_token)
            settings.require_outbound_url_allowed(url)
            response = await http_client.get(
                url,
                headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"},
            )
            if response.status_code == 404:
                continue
            response.raise_for_status()
            content_types.add(response.headers.get("content-type", "text/plain"))
            raw_hash = sha256(response.content).hexdigest()
            day_rows = parse_czce_daily_text(
                response.text,
                trade_date=trade_day.isoformat(),
                source_url=url,
                captured_at=captured_at,
                raw_sha256=raw_hash,
                skipped_rows=skipped_rows,
            )
            if not day_rows:
                continue
            bars.extend(day_rows)
            for product, semantic_series_id in CZCE_CAPTURE_SERIES.items():
                main = next((row for row in day_rows if row["product"] == product and row["is_main"]), None)
                if main is None:
                    continue
                captures.append(
                    {
                        "source_id": CZCE_SOURCE_ID,
                        "semantic_series_id": semantic_series_id,
                        "observed_at": trade_day.isoformat(),
                        "published_at": captured_at,
                        "visible_at": captured_at,
                        "captured_at": captured_at,
                        "source_url": url,
                        "raw_sha256": raw_hash,
                        "authorization_scope": "public_personal_reuse",
                        "contract_version": "seven-product-labels.v1",
                        "parser_version": CZCE_PARSER_VERSION,
                        "canonical_payload": main,
                    }
                )
    finally:
        if own_client:
            await http_client.aclose()
    if not bars:
        return OfficialFuturesResult(
            status="no_new_data",
            content_type="text/plain",
            content_preview=json.dumps({"records": 0, "lookback_days": lookback_days}),
        )
    return OfficialFuturesResult(
        status="ok",
        content_type=",".join(sorted(content_types)) or "text/plain",
        content_preview=json.dumps(
            {
                "records": len(bars),
                "capture_revisions": len(captures),
                "skipped_rows": skipped_rows,
                "products": sorted({str(row["product"]) for row in bars}),
                "first_trade_date": min(str(row["trade_date"]) for row in bars),
                "last_trade_date": max(str(row["trade_date"]) for row in bars),
            },
            ensure_ascii=False,
        ),
        bars=bars,
        capture_revisions=captures,
    )


def _number(value: str) -> float:
    normalized = value.replace(",", "").replace("\r", "").strip()
    if normalized in {"", "-", "--"}:
        raise ValueError("numeric_value_missing")
    return float(normalized)

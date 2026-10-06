from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from html import unescape
from urllib.parse import urljoin, urlparse
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from .observability import observe_scheduler
from .settings import settings
from .seven_product_contract import CURRENT_LABEL_REGISTRY_VERSION, LABEL_REGISTRY
from .storage import (
    latest_intraday_price_observations,
    list_intraday_price_observations,
    record_source_fetch,
    upsert_intraday_price_observation,
    upsert_intraday_price_observation_with_capture_revision,
)

logger = logging.getLogger(__name__)
_collection_lock = asyncio.Lock()
_scheduler_task: asyncio.Task[None] | None = None

CHAIN_INSTRUMENTS = ("Brent", "WTI", "NAPHTHA", "PX", "PTA", "MEG", "POY", "DTY")
POLICY_NOTE = (
    "分钟级通道只代表公开交易所/代理行情或公开页面刷新结果；MEG/POY/DTY 标记为非成交型现货评估价，"
    "不等同于逐笔成交行情。"
)
YAHOO_FUTURES = {
    "Brent": ("BZ=F", "Brent 原油期货代理价", "USD/bbl"),
    "WTI": ("CL=F", "WTI 原油期货代理价", "USD/bbl"),
}
# Sina global-board quotes are the availability fallback for Yahoo's
# exchange-proxy crude futures on the same already-allowlisted host.
SINA_GLOBAL_FUTURES = {
    "Brent": ("OIL", "布伦特原油外盘公开行情", "USD/bbl"),
    "WTI": ("CL", "纽约原油外盘公开行情", "USD/bbl"),
}
SINA_FUTURES = {
    "PTA": ("TA0", "PTA 主连期货代理价", "CNY/mt"),
    "MEG": ("EG0", "MEG 主连期货代理价", "CNY/mt"),
    "PX": ("PX0", "PX 主连期货代理价", "CNY/mt"),
}
EASTMONEY_FUTURES = {
    "PTA": ("115.TAM", "PTA 主连公开行情", "CNY/mt"),
    "PX": ("115.PXM", "PX 主连公开行情", "CNY/mt"),
    "MEG": ("114.EGM", "MEG 主连公开行情", "CNY/mt"),
}
PUBLIC_SPOT_PAGES = {
    # Crude spot pages share the naphtha channel's host and treatment: a dated
    # public sentence is a non-transaction valuation, never an exchange quote.
    "Brent": (
        "https://zh.tradingeconomics.com/commodity/brent-crude-oil",
        ("布伦特", "Brent"),
        "USD/bbl",
    ),
    "WTI": (
        "https://zh.tradingeconomics.com/commodity/crude-oil",
        ("原油", "Crude Oil"),
        "USD/bbl",
    ),
    "NAPHTHA": (
        "https://zh.tradingeconomics.com/commodity/naphtha",
        ("石脑油价格上涨至", "石脑油"),
        "USD/mt",
    ),
    "PX": ("https://www.sunsirs.com/uk/prodetail-968.html", ("PX", "Paraxylene"), "CNY/mt"),
    "PTA": ("https://www.sunsirs.com/uk/prodetail-356.html", ("PTA",), "CNY/mt"),
    "MEG": ("https://www.sunsirs.com/uk/prodetail-222.html", ("Ethylene glycol", "MEG"), "CNY/mt"),
    "POY": (
        "https://info.texnet.com.cn/list--20-.html",
        ("涤纶POY", "涤纶长丝POY", "polyester POY"),
        "CNY/mt",
    ),
    "DTY": ("https://info.texnet.com.cn/list--20-.html", ("涤纶DTY", "涤纶长丝DTY", "polyester DTY"), "CNY/mt"),
}
# Host-level availability fallback: same provider and series semantics as the
# primary page, used only when the primary host fails for an entire cycle.
NAPHTHA_FALLBACK_PUBLIC_SPOT_PAGE = (
    "https://www.tradingeconomics.com/commodity/naphtha",
    ("Naphtha",),
    "USD/mt",
)
# 生意社产业网参考价（dlpoy/dldty）is an independent-host daily aggregate for
# POY/DTY: a second provider whose dated reference value matched the texnet
# listing on every checked day. Parsed by _ppi_reference_quote, not the
# generic label-proximity extractor.
PPI_REFERENCE_HOSTS = {"dlpoy.100ppi.com": "POY", "dldty.100ppi.com": "DTY"}
PUBLIC_SPOT_PAGE_FALLBACKS = {
    "NAPHTHA": (NAPHTHA_FALLBACK_PUBLIC_SPOT_PAGE,),
    "POY": (("https://dlpoy.100ppi.com/", ("涤纶POY",), "CNY/mt"),),
    "DTY": (("https://dldty.100ppi.com/", ("涤纶DTY",), "CNY/mt"),),
}

PLAUSIBLE_PRICE_RANGES = {
    "Brent": (10.0, 300.0),
    "WTI": (10.0, 300.0),
    "NAPHTHA": (100.0, 2500.0),
    "PX": (500.0, 20_000.0),
    "PTA": (1_000.0, 20_000.0),
    "MEG": (1_000.0, 20_000.0),
    "POY": (3_000.0, 30_000.0),
    "DTY": (3_000.0, 30_000.0),
}
NAPHTHA_SPOT_PARSER_VERSION = "tradingeconomics-naphtha-public-page.v3-own-date"
SUNSIRS_MEG_SOURCE_ID = "sunsirs_public_commodity_assessment"
SUNSIRS_MEG_SERIES_ID = "meg.sunsirs.china.spot_assessment.cny_mt"
SUNSIRS_MEG_SPOT_PARSER_VERSION = "sunsirs-meg-public-page.v2"
SUNSIRS_MEG_METHODOLOGY_URL = "https://img.100ppi.com/uppic/2013/11/05/a5daa2bde36e799297ce74115da72b12.pdf"
SUNSIRS_MEG_LABEL_DEFINITION = {
    "assessment_type": "non_transaction_spot_assessment",
    "commodity_specification": "GB/T 4649-2008 industrial ethylene glycol premium grade",
    "price_basis": "ex_warehouse_net_water",
    "delivery_basis": "tank_farm_self_pickup",
    "payment_basis": "cash_full_payment",
    "standard_lot_tonnes": [50, 1000],
    "assessment_window_local": "09:00-10:30",
    "scheduled_publication_local": "11:30",
    "timezone": "Asia/Shanghai",
    "methodology_url": SUNSIRS_MEG_METHODOLOGY_URL,
    "methodology_document_date": "2013-11-01",
    "methodology_currency_status": "current_applicability_unverified",
    "visibility_policy": "first_successful_capture",
    "accepted_as_current_label_on": "2026-09-01",
}
PUBLIC_SPOT_SOURCE_IDS = {"MEG": SUNSIRS_MEG_SOURCE_ID}


@dataclass(frozen=True)
class ProviderError:
    instrument: str
    source_id: str
    error: str


async def _get_allowed(client: httpx.AsyncClient, url: str, **kwargs: object) -> httpx.Response:
    settings.require_outbound_url_allowed(url)
    return await client.get(url, **kwargs)


async def _get_with_retries(
    client: httpx.AsyncClient,
    url: str,
    *,
    attempts: int = 3,
    pause_seconds: float = 0.8,
    **kwargs: object,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await _get_allowed(client, url, **kwargs)
        except (
            httpx.ConnectError,
            httpx.ReadError,
            httpx.RemoteProtocolError,
            httpx.TimeoutException,
        ) as exc:
            last_exc = exc
            if attempt < attempts - 1:
                await asyncio.sleep(pause_seconds * (attempt + 1))
    if last_exc is not None:
        raise last_exc
    return await _get_allowed(client, url, **kwargs)


def list_intraday_prices(*, instrument: str | None = None, limit: int = 200) -> list[dict[str, object]]:
    return list_intraday_price_observations(instrument=instrument, limit=limit)


def build_latest_prices() -> dict[str, object]:
    generated_at = _now_iso()
    latest_by_instrument = {
        str(item["instrument"]): item
        for item in latest_intraday_price_observations(instruments=CHAIN_INSTRUMENTS)
        if is_plausible_intraday_price(str(item.get("instrument")), item.get("last"))
        and public_spot_quote_matches_instrument(str(item.get("instrument")), item)
    }
    items = []
    status_counts: dict[str, int] = {}
    for instrument in CHAIN_INSTRUMENTS:
        latest = latest_by_instrument.get(instrument)
        item = _latest_item(instrument, latest, generated_at)
        status_counts[item["freshness"]] = status_counts.get(item["freshness"], 0) + 1
        items.append(item)
    return {
        "generated_at": generated_at,
        "items": items,
        "policy_note": POLICY_NOTE,
        "status_counts": status_counts,
    }


async def collect_intraday_prices(*, instruments: list[str] | None = None, apply: bool = True) -> dict[str, object]:
    requested = _normalize_instruments(instruments)
    started_at = _now_iso()
    rows: list[dict[str, object]] = []
    errors: list[ProviderError] = []
    headers = {
        "User-Agent": "POY-DTY-Agent/1.0 personal-research",
        "Accept": "application/json,text/plain,text/html,*/*",
    }
    timeout = httpx.Timeout(12.0, connect=8.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        rows.extend(await _collect_yahoo_futures(client, requested, errors))
        covered = {str(row["instrument"]) for row in rows}
        rows.extend(await _collect_sina_global_futures(client, requested - covered, errors))
        covered = {str(row["instrument"]) for row in rows}
        sina_rows = await _collect_sina_futures(client, requested - covered, errors)
        rows.extend(sina_rows)
        sina_covered = {str(row["instrument"]) for row in sina_rows}
        eastmoney_rows = await _collect_eastmoney_futures(client, requested - sina_covered, errors)
        rows.extend(eastmoney_rows)
        covered = {str(row["instrument"]) for row in rows}
        rows.extend(await _collect_eastmoney_hist_last_bar(client, requested - covered, errors))
        rows.extend(await _collect_public_spot_pages(client, requested, errors))
    stored = [_store_observation(row) for row in rows] if apply else []
    if apply:
        # The intraday channel previously produced prices without any source
        # run audit, so the catalog incorrectly appeared never executed.
        for source_id in sorted({str(row["source_id"]) for row in rows} | {error.source_id for error in errors}):
            obtained = [row for row in rows if row["source_id"] == source_id]
            failed = [error for error in errors if error.source_id == source_id]
            record_source_fetch(
                audit_id=f"intraday-{uuid4().hex}",
                source_id=source_id,
                status="degraded" if obtained and failed else "ok" if obtained else "error",
                content_type="intraday_public_price",
                preview_chars=0,
                observations_fetched=len(obtained),
                error="; ".join(error.error for error in failed),
                started_at=started_at,
            )
    return {
        "started_at": started_at,
        "finished_at": _now_iso(),
        "stored": len(stored),
        "collected": len(rows),
        "attempted": len(requested),
        "items": stored if apply else rows,
        "errors": [error.__dict__ for error in errors],
        "writes_database": apply,
        "policy_note": POLICY_NOTE,
    }


async def run_scheduled_intraday_collection() -> dict[str, object]:
    """Run one collection without allowing overlapping provider requests."""
    if _collection_lock.locked():
        return {"status": "skipped_locked", "stored": 0, "errors": []}
    async with _collection_lock:
        try:
            result = await collect_intraday_prices()
        except Exception as exc:  # noqa: BLE001 - scheduler failures must not stop the API service.
            logger.exception("Scheduled intraday price collection failed")
            return {
                "status": "failed",
                "stored": 0,
                "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            }
        return {"status": "completed", **result}


async def _intraday_collection_loop(*, initial_delay_seconds: int, interval_seconds: int) -> None:
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    while True:
        started = time.monotonic()
        result = await run_scheduled_intraday_collection()
        duration_seconds = time.monotonic() - started
        status = str(result.get("status") or "unknown")
        attempt_time = time.time()
        if status == "failed":
            logger.warning("Intraday price scheduler cycle failed; retrying on next interval")
        observe_scheduler(
            scheduler="intraday_price",
            status=status,
            last_attempt_unix_seconds=attempt_time,
            last_success_unix_seconds=attempt_time if status == "completed" else None,
            duration_seconds=duration_seconds,
            backlog=0,
        )
        await asyncio.sleep(max(60, interval_seconds))


def start_intraday_price_scheduler() -> asyncio.Task[None] | None:
    global _scheduler_task
    if not settings.intraday_price_scheduler_enabled:
        return None
    if _scheduler_task is not None and not _scheduler_task.done():
        return _scheduler_task
    _scheduler_task = asyncio.create_task(
        _intraday_collection_loop(
            initial_delay_seconds=settings.intraday_price_initial_delay_seconds,
            interval_seconds=settings.intraday_price_interval_seconds,
        ),
        name="intraday-price-scheduler",
    )
    return _scheduler_task


async def stop_intraday_price_scheduler() -> None:
    global _scheduler_task
    task = _scheduler_task
    _scheduler_task = None
    if task is None or task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _collect_yahoo_futures(
    client: httpx.AsyncClient, requested: set[str], errors: list[ProviderError]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for instrument, (symbol, label, unit) in YAHOO_FUTURES.items():
        if instrument not in requested:
            continue
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        started = time.monotonic()
        last_exc: Exception | None = None
        for chart_range in ("1d", "5d"):
            try:
                response = await _get_allowed(client, url, params={"range": chart_range, "interval": "1m"})
                response.raise_for_status()
                rows.append(
                    _yahoo_chart_to_row(
                        response.json(),
                        instrument=instrument,
                        symbol=symbol,
                        label=label,
                        unit=unit,
                        source_url=str(response.url),
                        latency=time.monotonic() - started,
                    )
                )
                break
            except Exception as exc:  # noqa: BLE001 - retry a wider window before reporting the provider failure.
                last_exc = exc
        else:
            assert last_exc is not None
            errors.append(ProviderError(instrument, "yahoo_finance_proxy", f"{type(last_exc).__name__}: {last_exc}"))
    return rows


async def _collect_sina_global_futures(
    client: httpx.AsyncClient, requested: set[str], errors: list[ProviderError]
) -> list[dict[str, object]]:
    """Availability fallback for crude futures after a Yahoo failure.

    Sina's global board (``hf_*``) is a public proxy quote and may lag the
    exchange; the stored notes say so instead of implying real-time parity.
    """
    selected = {instrument: config for instrument, config in SINA_GLOBAL_FUTURES.items() if instrument in requested}
    if not selected:
        return []
    symbols = ",".join(f"hf_{symbol}" for symbol, _, _ in selected.values())
    url = f"https://hq.sinajs.cn/list={symbols}"
    started = time.monotonic()
    try:
        response = await _get_allowed(client, url, headers={"Referer": "https://finance.sina.com.cn/futures/"})
        response.raise_for_status()
        text = response.text
    except Exception as exc:  # noqa: BLE001 - all selected symbols share this public endpoint.
        for instrument in selected:
            errors.append(ProviderError(instrument, "sina_global_futures", f"{type(exc).__name__}: {exc}"))
        return []
    rows = []
    for instrument, (symbol, label, unit) in selected.items():
        try:
            rows.append(
                _sina_hf_to_row(
                    text,
                    instrument=instrument,
                    symbol=symbol,
                    label=label,
                    unit=unit,
                    source_url=url,
                    latency=time.monotonic() - started,
                )
            )
        except ValueError as exc:
            errors.append(ProviderError(instrument, "sina_global_futures", str(exc)))
    return rows


async def _collect_sina_futures(
    client: httpx.AsyncClient, requested: set[str], errors: list[ProviderError]
) -> list[dict[str, object]]:
    selected = {instrument: config for instrument, config in SINA_FUTURES.items() if instrument in requested}
    if not selected:
        return []
    symbols = ",".join(f"nf_{symbol}" for symbol, _, _ in selected.values())
    url = f"https://hq.sinajs.cn/list={symbols}"
    started = time.monotonic()
    try:
        response = await _get_allowed(client, url, headers={"Referer": "https://finance.sina.com.cn/futures/"})
        response.raise_for_status()
        text = response.text
    except Exception as exc:  # noqa: BLE001 - all selected symbols share this public endpoint.
        for instrument in selected:
            errors.append(ProviderError(instrument, "sina_futures_realtime", f"{type(exc).__name__}: {exc}"))
        return []

    rows = []
    for instrument, (symbol, label, unit) in selected.items():
        try:
            rows.append(
                _sina_hq_to_row(
                    text,
                    instrument=instrument,
                    symbol=symbol,
                    label=label,
                    unit=unit,
                    source_url=url,
                    latency=time.monotonic() - started,
                )
            )
        except ValueError as exc:
            errors.append(ProviderError(instrument, "sina_futures_realtime", str(exc)))
    return rows


async def _collect_eastmoney_futures(
    client: httpx.AsyncClient, requested: set[str], errors: list[ProviderError]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    selected = {instrument: config for instrument, config in EASTMONEY_FUTURES.items() if instrument in requested}
    if not selected:
        return rows
    secid_to_instrument = {config[0]: instrument for instrument, config in selected.items()}
    url = "https://push2.eastmoney.com/api/qt/ulist.np/get"
    params = {
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "secids": ",".join(secid_to_instrument),
        "fields": "f12,f14,f2,f3,f4,f5,f6,f15,f16,f17,f18,f124,f152",
        "fltt": "2",
        "invt": "2",
    }
    started = time.monotonic()
    try:
        response = await _get_with_retries(
            client,
            url,
            params=params,
            headers={
                "Referer": "https://quote.eastmoney.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                ),
            },
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - all selected symbols share this public endpoint.
        for instrument in selected:
            errors.append(ProviderError(instrument, "eastmoney_futures_realtime", f"{type(exc).__name__}: {exc}"))
        return rows
    diff = payload.get("data", {}).get("diff") if isinstance(payload.get("data"), dict) else None
    if not isinstance(diff, list):
        for instrument in selected:
            errors.append(ProviderError(instrument, "eastmoney_futures_realtime", "Eastmoney payload missing diff"))
        return rows
    by_code = {str(item.get("f12", "")).upper(): item for item in diff if isinstance(item, dict)}
    for instrument, (secid, label, unit) in selected.items():
        code = secid.split(".", 1)[1].upper().removesuffix("M")
        item = by_code.get(code) or by_code.get(code.lower()) or by_code.get(secid.split(".", 1)[1].upper())
        if item is None:
            errors.append(
                ProviderError(
                    instrument,
                    "eastmoney_futures_realtime",
                    f"Eastmoney response missing {secid}",
                )
            )
            continue
        try:
            rows.append(
                _eastmoney_quote_to_row(
                    item,
                    instrument=instrument,
                    symbol=secid,
                    label=label,
                    unit=unit,
                    source_url=str(response.url),
                    latency=time.monotonic() - started,
                )
            )
        except ValueError as exc:
            errors.append(ProviderError(instrument, "eastmoney_futures_realtime", str(exc)))
    return rows


async def _collect_eastmoney_hist_last_bar(
    client: httpx.AsyncClient, requested: set[str], errors: list[ProviderError]
) -> list[dict[str, object]]:
    """Last 1-minute bar from Eastmoney's history host.

    ``push2his`` stays reachable from overseas networks where the realtime
    ``push2`` cluster 302s into an empty delay pool, so the newest bar close
    is the minute-level fallback for domestic futures proxies.
    """
    rows: list[dict[str, object]] = []
    selected = {instrument: config for instrument, config in EASTMONEY_FUTURES.items() if instrument in requested}
    if not selected:
        return rows
    for instrument, (secid, label, unit) in selected.items():
        started = time.monotonic()
        try:
            response = await _get_with_retries(
                client,
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                params={
                    "secid": secid,
                    "fields1": "f1,f2,f3,f4,f5,f6",
                    "fields2": "f51,f52,f53,f54,f55,f56,f57",
                    "klt": "1",
                    "fqt": "1",
                    "end": "20500101",
                    "lmt": "2",
                },
                headers={
                    "Referer": "https://quote.eastmoney.com/",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                    ),
                },
            )
            response.raise_for_status()
            payload = response.json()
            klines = payload.get("data", {}).get("klines") if isinstance(payload.get("data"), dict) else None
            if not isinstance(klines, list) or len(klines) < 1:
                raise ValueError("Eastmoney hist payload missing klines")
            try:
                rows.append(
                    _eastmoney_hist_bar_to_row(
                        klines,
                        instrument=instrument,
                        symbol=secid,
                        label=label,
                        unit=unit,
                        source_url=str(response.url),
                        latency=time.monotonic() - started,
                    )
                )
            except ValueError as exc:
                errors.append(ProviderError(instrument, "eastmoney_futures_hist_kline", str(exc)))
        except Exception as exc:  # noqa: BLE001 - one instrument must not starve the others.
            errors.append(ProviderError(instrument, "eastmoney_futures_hist_kline", f"{type(exc).__name__}: {exc}"))
    return rows


def _eastmoney_hist_bar_to_row(
    klines: list[str],
    *,
    instrument: str,
    symbol: str,
    label: str,
    unit: str,
    source_url: str,
    latency: float,
) -> dict[str, object]:
    def _bar(line: str) -> dict[str, object]:
        parts = line.split(",")
        if len(parts) < 6:
            raise ValueError(f"Eastmoney hist kline row malformed: {line!r}")
        return {
            "datetime": parts[0],
            "open": _to_float(parts[1]),
            "close": _to_float(parts[2]),
            "high": _to_float(parts[3]),
            "low": _to_float(parts[4]),
            "volume": _to_float(parts[5]),
        }

    last = _bar(klines[-1])
    previous = _bar(klines[-2]) if len(klines) > 1 else None
    last_price = last["close"]
    if last_price is None or last_price <= 0:
        raise ValueError(f"Eastmoney hist bar has no close for {symbol}")
    try:
        bar_time = datetime.strptime(str(last["datetime"]), "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("Asia/Shanghai")
        )
    except ValueError as exc:
        raise ValueError(f"Eastmoney hist bar time malformed: {last['datetime']!r}") from exc
    change_pct = None
    if previous is not None and previous["close"]:
        change_pct = round((last_price / previous["close"] - 1) * 100, 4)
    return {
        "instrument": instrument,
        "symbol": symbol,
        "observed_at": bar_time.isoformat(),
        "interval_seconds": 60,
        "price_type": "near_realtime_public",
        "last": last_price,
        "open": last["open"],
        "high": last["high"],
        "low": last["low"],
        "volume": last["volume"],
        "change_pct": change_pct,
        "unit": unit,
        "source_id": "eastmoney_futures_hist_kline",
        "source_url": source_url,
        "source_latency_seconds": round(latency, 3),
        "quality": "public_json_quote",
        "notes": f"{label}；东方财富公开1分钟K线最近一根收盘，境外访问可能延迟，仅作个人研究参考。",
        "raw": {
            "provider": "eastmoney_push2his",
            "symbol": symbol,
            "bar": klines[-1],
            "previous_bar": klines[-2] if len(klines) > 1 else None,
        },
    }


async def _collect_public_spot_pages(
    client: httpx.AsyncClient, requested: set[str], errors: list[ProviderError]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    page_cache: dict[str, httpx.Response] = {}
    for instrument in PUBLIC_SPOT_PAGES:
        if instrument not in requested:
            continue
        candidates = (PUBLIC_SPOT_PAGES[instrument], *PUBLIC_SPOT_PAGE_FALLBACKS.get(instrument, ()))
        last_error: Exception | None = None
        primary_error: Exception | None = None
        best_row: dict[str, object] | None = None
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
        for url, labels, unit in candidates:
            started = time.monotonic()
            try:
                response = page_cache.get(url)
                if response is None:
                    response = await _get_allowed(client, url, headers={"Referer": url})
                    page_cache[url] = response
                response.raise_for_status()
                ppi_quote = None
                if PPI_REFERENCE_HOSTS.get(urlparse(url).hostname or "") == instrument:
                    ppi_quote = _ppi_reference_quote(_html_to_visible_text(response.text), instrument)
                    if ppi_quote is None:
                        raise ValueError("ppi channel has no dated reference quote")
                if instrument in {"POY", "DTY"} and ppi_quote is None:
                    daily_url = _texnet_latest_daily_url(response.text, str(response.url))
                    if daily_url:
                        try:
                            daily = page_cache.get(daily_url)
                            if daily is None:
                                daily = await _get_allowed(client, daily_url, headers={"Referer": url})
                                page_cache[daily_url] = daily
                            daily.raise_for_status()
                            daily_quote = _texnet_daily_quote(daily.text, instrument)
                            if daily_quote is None:
                                raise ValueError("daily table has no verified dated polyester row")
                            list_quote = _extract_public_spot_quote(_html_to_visible_text(response.text), labels)
                            if daily_quote and (not list_quote or daily_quote[1] >= list_quote[1]):
                                response = daily
                        except Exception as exc:  # noqa: BLE001 - retain the dated listing on source failure.
                            errors.append(ProviderError(instrument, "public_spot_page_refresh", f"daily_table: {exc}"))
                candidate_row = _public_spot_page_to_row(
                    response.text,
                    instrument=instrument,
                    symbol=f"{instrument}_PUBLIC_SPOT",
                    labels=labels,
                    unit=unit,
                    source_url=str(response.url),
                    latency=time.monotonic() - started,
                    source_id=PUBLIC_SPOT_SOURCE_IDS.get(instrument, "public_spot_page_refresh"),
                    quote=ppi_quote,
                )
                if best_row is None or str(candidate_row["observed_at"]) > str(best_row["observed_at"]):
                    best_row = candidate_row
                # A reachable primary can still lag its publisher mirror.
                # Compare dates only within the configured equivalent quote basis.
                if str(candidate_row["observed_at"]) >= today:
                    break
            except Exception as exc:  # noqa: BLE001 - try the next candidate before reporting failure.
                last_error = exc
                if primary_error is None:
                    primary_error = exc
        if best_row is not None:
            rows.append(best_row)
        if best_row is None and primary_error is not None:
            detail = f"{type(primary_error).__name__}: {primary_error}"
            if last_error is not None and last_error is not primary_error:
                detail += f"; fallback: {type(last_error).__name__}: {last_error}"
            errors.append(
                ProviderError(
                    instrument,
                    PUBLIC_SPOT_SOURCE_IDS.get(instrument, "public_spot_page_refresh"),
                    detail,
                )
            )
    return rows


def _yahoo_chart_to_row(
    payload: dict[str, object],
    *,
    instrument: str,
    symbol: str,
    label: str,
    unit: str,
    source_url: str,
    latency: float,
) -> dict[str, object]:
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise ValueError("Yahoo chart payload missing chart")
    result = chart.get("result")
    if not isinstance(result, list) or not result:
        raise ValueError("Yahoo chart payload missing result")
    item = result[0]
    if not isinstance(item, dict):
        raise ValueError("Yahoo chart result is invalid")
    timestamps = item.get("timestamp")
    indicators = item.get("indicators")
    if not isinstance(timestamps, list) or not timestamps or not isinstance(indicators, dict):
        raise ValueError("Yahoo chart payload missing timestamps or indicators")
    quote_list = indicators.get("quote")
    if not isinstance(quote_list, list) or not quote_list or not isinstance(quote_list[0], dict):
        raise ValueError("Yahoo chart payload missing quote")
    quote = quote_list[0]
    closes = quote.get("close")
    if not isinstance(closes, list):
        raise ValueError("Yahoo chart payload missing closes")
    latest_index = _latest_numeric_index(closes)
    if latest_index is None:
        raise ValueError("Yahoo chart payload has no numeric close")
    observed_at = datetime.fromtimestamp(int(timestamps[latest_index]), UTC).isoformat()
    last = _to_float(closes[latest_index])
    previous = _previous_numeric(closes, latest_index)
    return {
        "instrument": instrument,
        "symbol": symbol,
        "observed_at": observed_at,
        "interval_seconds": 60,
        "price_type": "exchange_proxy",
        "last": last,
        "open": _list_float(quote.get("open"), latest_index),
        "high": _list_float(quote.get("high"), latest_index),
        "low": _list_float(quote.get("low"), latest_index),
        "volume": _list_float(quote.get("volume"), latest_index),
        "change_pct": _change_pct(last, previous),
        "unit": unit,
        "source_id": "yahoo_finance_proxy",
        "source_url": source_url,
        "source_latency_seconds": round(latency, 3),
        "quality": "proxy_market_quote",
        "notes": label,
        "raw": {"provider": "yahoo_chart", "symbol": symbol},
    }


def _sina_hq_to_row(
    text: str,
    *,
    instrument: str,
    symbol: str,
    label: str,
    unit: str,
    source_url: str,
    latency: float,
) -> dict[str, object]:
    pattern = re.compile(rf"hq_str_nf_{re.escape(symbol)}=\"(?P<body>[^\"]*)\"", flags=re.IGNORECASE)
    match = pattern.search(text)
    if not match:
        raise ValueError(f"Sina response missing {symbol}")
    fields = [field.strip() for field in match.group("body").split(",")]
    numbers = [_to_float(field) for field in fields]
    last = _first_indexed_numeric(numbers, (8,))
    if last is None or last <= 0:
        raise ValueError(f"Sina response has no numeric price for {symbol}")
    previous = _first_indexed_numeric(numbers, (27, 7, 6))
    observed_at = _extract_datetime(fields)
    if not observed_at:
        raise ValueError(f"Sina response missing valid observation date for {symbol}")
    return {
        "instrument": instrument,
        "symbol": symbol,
        "observed_at": observed_at,
        "interval_seconds": 60,
        "price_type": "exchange_proxy",
        "last": last,
        "open": _first_indexed_numeric(numbers, (2,)),
        "high": _first_indexed_numeric(numbers, (3,)),
        "low": _first_indexed_numeric(numbers, (4,)),
        # Sina's ``nf_*`` futures schema places cumulative turnover at field
        # 14. Fields 10-12 are previous settlement and bid/ask quantities; using
        # them as volume silently turns a price-like value into a volume metric.
        "volume": _first_indexed_numeric(numbers, (14,)),
        "change_pct": _change_pct(last, previous),
        "unit": unit,
        "source_id": "sina_futures_realtime",
        "source_url": source_url,
        "source_latency_seconds": round(latency, 3),
        "quality": "public_proxy_quote",
        "notes": label,
        "raw": {"provider": "sina_hq", "symbol": symbol, "fields": fields},
    }


def _sina_hf_to_row(
    text: str,
    *,
    instrument: str,
    symbol: str,
    label: str,
    unit: str,
    source_url: str,
    latency: float,
) -> dict[str, object]:
    """Parse Sina's global-board ``hf_*`` quote.

    Documented field layout: 0 last, 2 bid, 3 ask, 4 high, 5 low, 6 time,
    7 previous settle, 8 open, 12 date, 13 name. Volume is not offered.
    """
    pattern = re.compile(rf"hq_str_hf_{re.escape(symbol)}=\"(?P<body>[^\"]*)\"")
    match = pattern.search(text)
    if not match:
        raise ValueError(f"Sina global response missing {symbol}")
    fields = [field.strip() for field in match.group("body").split(",")]
    if len(fields) < 14:
        raise ValueError(f"Sina global response truncated for {symbol}")
    last = _to_float(fields[0])
    if last is None or last <= 0:
        raise ValueError(f"Sina global response has no numeric price for {symbol}")
    high = _first_indexed_numeric([_to_float(f) for f in fields], (4,))
    low = _first_indexed_numeric([_to_float(f) for f in fields], (5,))
    open_price = _first_indexed_numeric([_to_float(f) for f in fields], (8,))
    previous = _first_indexed_numeric([_to_float(f) for f in fields], (7,))
    trade_date, trade_time = fields[12], fields[6]
    observed_at = _extract_datetime([trade_date, trade_time])
    if not observed_at:
        raise ValueError(f"Sina global response missing valid observation date for {symbol}")
    return {
        "instrument": instrument,
        "symbol": symbol,
        "observed_at": observed_at,
        "interval_seconds": 60,
        "price_type": "exchange_proxy",
        "last": last,
        "open": open_price,
        "high": high,
        "low": low,
        "volume": None,
        "change_pct": _change_pct(last, previous),
        "unit": unit,
        "source_id": "sina_global_futures",
        "source_url": source_url,
        "source_latency_seconds": round(latency, 3),
        "quality": "public_proxy_quote",
        "notes": f"{label}；公开外盘代理行情，可能存在延迟。",
        "raw": {"provider": "sina_hf", "symbol": symbol, "fields": fields},
    }


def _eastmoney_quote_to_row(
    payload: dict[str, object],
    *,
    instrument: str,
    symbol: str,
    label: str,
    unit: str,
    source_url: str,
    latency: float,
) -> dict[str, object]:
    data = payload.get("data") if "data" in payload else payload
    if not isinstance(data, dict):
        raise ValueError("Eastmoney payload missing data")
    last = _to_float(data.get("f43") if "f43" in data else data.get("f2"))
    if last is None or last <= 0:
        raise ValueError(f"Eastmoney payload has no numeric latest price for {symbol}")
    observed_at = _eastmoney_timestamp_to_iso(data.get("f86") if "f86" in data else data.get("f124"))
    change_pct = _to_float(data.get("f170") if "f170" in data else data.get("f3"))
    if change_pct is not None:
        change_pct = round(change_pct / 100 if abs(change_pct) > 20 else change_pct, 4)
    return {
        "instrument": instrument,
        "symbol": symbol,
        "observed_at": observed_at,
        "interval_seconds": 60,
        "price_type": "near_realtime_public",
        "last": last,
        "open": _to_float(data.get("f46") if "f46" in data else data.get("f17")),
        "high": _to_float(data.get("f44") if "f44" in data else data.get("f15")),
        "low": _to_float(data.get("f45") if "f45" in data else data.get("f16")),
        "volume": _to_float(data.get("f47") if "f47" in data else data.get("f5")),
        "change_pct": change_pct,
        "unit": unit,
        "source_id": "eastmoney_futures_realtime",
        "source_url": source_url,
        "source_latency_seconds": round(latency, 3),
        "quality": "public_json_quote",
        "notes": f"{label}；东方财富公开行情字段，仅作个人研究参考。",
        "raw": {"provider": "eastmoney_push2", "symbol": symbol, "name": data.get("f14"), "code": data.get("f12")},
    }


def _ppi_reference_quote(visible_text: str, instrument: str) -> tuple[float, str, str] | None:
    """Extract the dated aggregate reference value from 生意社产业网 channels.

    The channel page states e.g. "9月11日，涤纶POY参考价为9242.50". The page
    has no year, so a date in the future of today belongs to the previous year.
    """
    label = f"涤纶{instrument}"
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    match = re.search(
        rf"(\d{{1,2}})月(\d{{1,2}})日[，,]\s*{label}参考价为(?P<value>[0-9][0-9,]*(?:\.[0-9]+)?)",
        visible_text,
    )
    if not match:
        return None
    month, day = int(match.group(1)), int(match.group(2))
    value = float(match.group("value").replace(",", ""))
    try:
        observed = date(today.year, month, day)
        if observed > today:
            observed = date(today.year - 1, month, day)
    except ValueError:
        return None
    evidence = f"参考价转录：{observed.isoformat()} {label}为{value}；原文={label}参考价为{value}（生意社产业网）"
    return value, observed.isoformat(), evidence


def _public_spot_page_to_row(
    html: str,
    *,
    instrument: str,
    symbol: str,
    labels: tuple[str, ...],
    unit: str,
    source_url: str,
    latency: float,
    source_id: str = "public_spot_page_refresh",
    quote: tuple[float, str, str] | None = None,
) -> dict[str, object]:
    text = _html_to_visible_text(html)
    quote = (
        quote
        or (
            _texnet_daily_quote(html, instrument)
            if instrument in {"POY", "DTY"} and urlparse(source_url).hostname == "info.texnet.com.cn"
            else None
        )
        or _extract_public_spot_quote(text, labels)
    )
    if quote is None:
        raise ValueError(f"could not locate public spot valuation for {instrument}")
    value, observed_at, raw_quote = quote
    try:
        source_day = date.fromisoformat(observed_at)
    except ValueError as exc:
        raise ValueError("public spot quote has invalid observation date") from exc
    if source_day > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        raise ValueError("public spot quote has future observation date")
    if not is_plausible_intraday_price(instrument, value):
        raise ValueError(f"public spot valuation outside plausible range for {instrument}: {value}")
    captured_at = _now_iso()
    label_definition = (
        dict(SUNSIRS_MEG_LABEL_DEFINITION) if instrument == "MEG" and source_id == SUNSIRS_MEG_SOURCE_ID else None
    )
    raw_evidence_sha256 = hashlib.sha256(
        json.dumps(
            {
                **({"label_definition": label_definition} if label_definition is not None else {}),
                "instrument": instrument,
                "observed_at": observed_at,
                "quote": raw_quote,
                "source_url": source_url,
                "unit": unit,
                "value": value,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "instrument": instrument,
        "symbol": symbol,
        "observed_at": observed_at,
        "interval_seconds": 900,
        "price_type": "spot_public_valuation",
        "last": value,
        "open": None,
        "high": None,
        "low": None,
        "volume": None,
        "change_pct": None,
        "unit": unit,
        "source_id": source_id,
        "source_url": source_url,
        "source_latency_seconds": round(latency, 3),
        "quality": "non_transaction_public_valuation",
        "notes": (
            "SunSirs 中国乙二醇公开现货评估；已接受为当前 MEG 标签源，但不是逐笔成交行情，"
            "预测正式资格仍由真实 OOS 门禁决定。"
            if instrument == "MEG" and source_id == SUNSIRS_MEG_SOURCE_ID
            else "公开页面刷新结果；不是逐笔成交行情。"
        ),
        "raw": {
            "provider": "public_spot_page",
            "quote": raw_quote,
            "captured_at": captured_at,
            "raw_evidence_sha256": raw_evidence_sha256,
            **(
                {
                    "label_definition": label_definition,
                    "label_series_id": SUNSIRS_MEG_SERIES_ID,
                    "label_registry_version": CURRENT_LABEL_REGISTRY_VERSION,
                }
                if label_definition is not None
                else {}
            ),
        },
    }


def _store_observation(row: dict[str, object]) -> dict[str, object]:
    observation_id = str(uuid4())
    instrument = str(row.get("instrument") or "")
    source_id = str(row.get("source_id") or "")
    if instrument == "NAPHTHA" and source_id == LABEL_REGISTRY["naphtha"].source_id:
        semantic_series_id = LABEL_REGISTRY["naphtha"].series_id
        contract_version = CURRENT_LABEL_REGISTRY_VERSION
        parser_version = NAPHTHA_SPOT_PARSER_VERSION
    elif instrument == "MEG" and source_id == SUNSIRS_MEG_SOURCE_ID:
        semantic_series_id = LABEL_REGISTRY["meg"].series_id
        contract_version = CURRENT_LABEL_REGISTRY_VERSION
        parser_version = SUNSIRS_MEG_SPOT_PARSER_VERSION
    else:
        return upsert_intraday_price_observation(observation_id=observation_id, payload=row)
    raw = row.get("raw")
    if not isinstance(raw, dict):
        raise ValueError("public_spot_capture_raw_evidence_missing")
    captured_at = str(raw.get("captured_at") or "")
    raw_sha256 = str(raw.get("raw_evidence_sha256") or "")
    if not captured_at or not re.fullmatch(r"[0-9a-f]{64}", raw_sha256):
        raise ValueError("public_spot_capture_evidence_invalid")
    capture_revision = {
        "source_id": source_id,
        "semantic_series_id": semantic_series_id,
        "observed_at": str(row["observed_at"]),
        "published_at": captured_at,
        "visible_at": captured_at,
        "captured_at": captured_at,
        "source_url": str(row.get("source_url") or ""),
        "raw_sha256": raw_sha256,
        "authorization_scope": "public_personal_reuse",
        "contract_version": contract_version,
        "parser_version": parser_version,
        "canonical_payload": row,
    }
    return upsert_intraday_price_observation_with_capture_revision(
        observation_id=observation_id,
        payload=row,
        capture_revision_id=str(uuid4()),
        capture_revision=capture_revision,
    )


def is_plausible_intraday_price(instrument: str, value: object) -> bool:
    price = _to_float(value)
    limits = PLAUSIBLE_PRICE_RANGES.get(instrument)
    return price is not None and (limits is None or limits[0] <= price <= limits[1])


def public_spot_quote_matches_instrument(instrument: str, observation: dict[str, object]) -> bool:
    """Validate product identity, including the dated naphtha row behind a stored value."""
    key = str(instrument or "").strip().upper()
    if key == "NAPHTHA" and observation.get("source_id") == "public_spot_page_refresh":
        raw = observation.get("raw")
        quote = _naphtha_public_quote(str(raw.get("quote") or "")) if isinstance(raw, dict) else None
        selected = _to_float(observation.get("last"))
        return bool(
            quote
            and selected is not None
            and abs(quote[0] - selected) < 0.01
            and quote[1] == str(observation.get("observed_at") or "")[:10]
        )
    if key in {"BRENT", "WTI"} and observation.get("source_id") == "public_spot_page_refresh":
        raw = observation.get("raw")
        quote = _crude_public_quote(str(raw.get("quote") or ""), key.title()) if isinstance(raw, dict) else None
        selected = _to_float(observation.get("last"))
        return bool(
            quote
            and selected is not None
            and abs(quote[0] - selected) < 0.01
            and quote[1] == str(observation.get("observed_at") or "")[:10]
        )
    if key not in {"POY", "DTY"} or str(observation.get("source_id") or "") != "public_spot_page_refresh":
        return True
    raw = observation.get("raw")
    if not isinstance(raw, dict):
        return False
    quote = str(raw.get("quote") or "")
    selected = _to_float(observation.get("last"))
    if selected is None:
        return False
    pattern = re.compile(rf"涤纶\s*{re.escape(key)}\s*为\s*([0-9][0-9,]*(?:\.[0-9]+)?)", re.IGNORECASE)
    return any(abs(float(raw_value.replace(",", "")) - selected) < 0.01 for raw_value in pattern.findall(quote))


def _texnet_latest_daily_url(html: str, base_url: str) -> str | None:
    """Only follow a dated daily assessment article on the existing source host."""
    candidates = []
    for href, body in re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, re.I | re.S):
        title = _html_to_visible_text(body)
        match = re.fullmatch(r"(20\d{2})年(\d{1,2})月(\d{1,2})日纺织大宗商品价格涨跌榜", title.strip())
        target = urljoin(base_url, unescape(href))
        parsed = urlparse(target)
        if not match or parsed.hostname != "info.texnet.com.cn" or parsed.scheme not in {"https", "http"}:
            continue
        if not re.fullmatch(r"/detail-\d+\.html", parsed.path) or parsed.query or parsed.fragment:
            continue
        try:
            day = datetime(*map(int, match.groups())).date()
        except ValueError:
            continue
        if day <= datetime.now(ZoneInfo("Asia/Shanghai")).date():
            candidates.append((day, target))
    return max(candidates)[1] if candidates else None


def _texnet_daily_quote(html: str, instrument: str) -> tuple[float, str, str] | None:
    """Bind an exact polyester row to the dated current-day column in one table."""
    title = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日纺织大宗商品价格涨跌榜", _html_to_visible_text(html))
    if not title or instrument not in {"POY", "DTY"}:
        return None
    try:
        day = datetime(*map(int, title.groups())).date()
    except ValueError:
        return None
    if day > datetime.now(ZoneInfo("Asia/Shanghai")).date():
        return None
    expected_header = f"{day.month}月{day.day}日价格"
    for table in re.findall(r"<table\b[^>]*>(.*?)</table>", html, re.I | re.S):
        column = None
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", table, re.I | re.S):
            cells = [
                _html_to_visible_text(cell).strip()
                for cell in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, re.I | re.S)
            ]
            compact = [re.sub(r"\s+", "", cell) for cell in cells]
            if expected_header in compact and "商品" in compact:
                column = compact.index(expected_header)
                continue
            if column is None or len(cells) <= column or not compact or compact[0] != f"涤纶{instrument}":
                continue
            if not re.fullmatch(r"\d+(?:,\d{3})*(?:\.\d+)?", compact[column]):
                continue
            value = float(compact[column].replace(",", ""))
            # A normalized table transcription, with the original row retained.
            evidence = (
                f"表格转录：{day.isoformat()} 涤纶{instrument}为{value}；"
                f"列={expected_header}；原始行={' | '.join(cells)}"
            )
            return value, day.isoformat(), evidence
    return None


def _extract_public_spot_quote(text: str, labels: tuple[str, ...]) -> tuple[float, str, str] | None:
    if any(label in {"石脑油", "石脑油价格上涨至", "Naphtha"} for label in labels):
        return _naphtha_public_quote(text)
    if any(label in {"布伦特", "Brent"} for label in labels):
        return _crude_public_quote(text, "Brent")
    if any(label in {"原油", "Crude Oil"} for label in labels):
        return _crude_public_quote(text, "WTI")
    date_pattern = r"(?P<date>20\d{2}(?:[-/]\d{1,2}[-/]\d{1,2}|年\d{1,2}月\d{1,2}日))"
    value_pattern = r"(?P<value>(?:[1-9]\d{0,2}(?:,\d{3})+|[1-9]\d{2,5})(?:\.\d+)?)"
    for label in labels:
        escaped = re.escape(label)
        # Prefer an exact product-value phrase before proximity matching. A
        # commodity list can mention DTY in its ranking introduction and then
        # show an unrelated product's value first.
        patterns = (
            re.compile(
                rf"{date_pattern}.{{0,40}}?{escaped}(?:价格)?(?:上涨|下跌|持平)?(?:至|为)\s*{value_pattern}",
                re.IGNORECASE,
            ),
            re.compile(rf"{date_pattern}.{{0,40}}?{escaped}\s*为\s*{value_pattern}", re.IGNORECASE),
            re.compile(rf"{escaped}\s*为\s*{value_pattern}.{{0,80}}?{date_pattern}", re.IGNORECASE),
            re.compile(rf"{date_pattern}.{{0,40}}?{escaped}\s*{value_pattern}", re.IGNORECASE),
            re.compile(rf"{escaped}\s*{value_pattern}.{{0,80}}?{date_pattern}", re.IGNORECASE),
            re.compile(rf"{escaped}.{{0,120}}?{value_pattern}.{{0,80}}?{date_pattern}", re.IGNORECASE),
            re.compile(rf"{date_pattern}.{{0,120}}?{escaped}.{{0,120}}?{value_pattern}", re.IGNORECASE),
        )
        for pattern in patterns:
            match = pattern.search(text)
            # Do not attach a nearby newer article date to an older quote.
            # Search again from the next character so overlapping matches can
            # still bind the quote to its own date.
            while match and len(re.findall(date_pattern, match.group(0))) > 1:
                match = pattern.search(text, match.start() + 1)
            if not match:
                continue
            raw = text[max(match.start() - 80, 0) : min(match.end() + 80, len(text))]
            return (
                float(match.group("value").replace(",", "")),
                _normalize_date(match.group("date")),
                raw,
            )
    return None


def _naphtha_public_quote(text: str) -> tuple[float, str, str] | None:
    """Bind the price to its own sentence or numeric table row, never a neighbour's date."""
    day = r"(?P<date>20\d{2}(?:[-/]\d{1,2}[-/]\d{1,2}|年\d{1,2}月\d{1,2}日))"
    value = r"(?P<value>[0-9][0-9,]*(?:\.\d+)?)"
    patterns = (
        rf"(?:在)?{day}\s*[，,]?\s*石脑油(?:价格)?(?:上涨|下跌|持平)?(?:至|为)\s*{value}",
        # Commodity tables put the observation date AFTER this product's price and
        # changes. Only numeric columns are permitted between them (no other product).
        rf"(?:石脑油|\bNaphtha\b)\s+(?:Chemical\s+)?{value}"
        rf"(?:\s+[-+−]?[\d,.]+%?){{0,6}}\s+{day}",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                observed = _normalize_date(match.group("date"))
                date.fromisoformat(observed)
                price = float(match.group("value").replace(",", ""))
            except ValueError:
                continue
            if is_plausible_intraday_price("NAPHTHA", price):
                return price, observed, match.group(0)
    return None


CRUDE_SPOT_PARSER_VERSION = "tradingeconomics-crude-public-page.v1-own-date"


def _crude_public_quote(text: str, instrument: str) -> tuple[float, str, str] | None:
    """Bind the dated crude sentence to its own date, never a neighbour's.

    TE pages phrase WTI as ``DATE，原油价格跌至每桶95.68美元`` and Brent as
    ``布伦特在DATE跌至100.26美元/桶``; both must close with 美元 right after the
    value so a nearby ranking list cannot donate its number.
    """
    verb = r"(?:上涨|下跌|持平|跌|涨|升)?(?:至|为)"
    if instrument == "Brent":
        label = r"布伦特(?:原油)?(?:价格)?"
        barrel_prefix = r""
    else:
        label = r"原油(?:价格)?"
        barrel_prefix = r"(?:每桶)?\s*"
    day = r"(?P<date>20\d{2}(?:[-/]\d{1,2}[-/]\d{1,2}|年\d{1,2}月\d{1,2}日))"
    value = r"(?P<value>[0-9][0-9,]*(?:\.\d+)?)"
    patterns = (
        rf"(?:在)?{day}\s*[，,]?\s*{label}{verb}\s*{barrel_prefix}{value}\s*美元",
        rf"{label}在\s*{day}\s*{verb}\s*{barrel_prefix}{value}\s*美元",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                observed = _normalize_date(match.group("date"))
                date.fromisoformat(observed)
                price = float(match.group("value").replace(",", ""))
            except ValueError:
                continue
            if is_plausible_intraday_price(instrument, price):
                return price, observed, match.group(0)
    return None


def _latest_item(instrument: str, latest: dict[str, object] | None, generated_at: str) -> dict[str, object]:
    if latest is None:
        return {
            "instrument": instrument,
            "label": instrument,
            "latest": None,
            "freshness": "missing",
            "freshness_label": "暂无分钟级观测",
            "quote_type_label": "缺口",
            "is_transaction_price": False,
            "staleness_seconds": None,
            "quote_age_days": None,
            "gap_reason": "尚未采集到公开分钟/页面刷新价格。",
        }
    price_type = str(latest.get("price_type") or "")
    if price_type == "spot_public_valuation":
        quote_age_days = _days_between(str(latest["observed_at"]), generated_at)
        return {
            "instrument": instrument,
            "label": instrument,
            "latest": latest,
            "freshness": "valuation",
            "freshness_label": "公开现货评估",
            "quote_type_label": "非成交型现货评估价",
            "is_transaction_price": False,
            "staleness_seconds": _seconds_between(str(latest["created_at"]), generated_at),
            "quote_age_days": quote_age_days,
            "gap_reason": "",
        }
    if price_type == "near_realtime_public":
        staleness = _seconds_between(str(latest["observed_at"]), generated_at)
        freshness = _freshness_from_seconds(staleness)
        return {
            "instrument": instrument,
            "label": instrument,
            "latest": latest,
            "freshness": freshness,
            "freshness_label": {
                "realtime": "2分钟内",
                "near_realtime": "15分钟内",
                "delayed": "1小时内",
                "stale": "已延迟",
            }[freshness],
            "quote_type_label": "近实时公开行情",
            "is_transaction_price": True,
            "staleness_seconds": staleness,
            "quote_age_days": None,
            "gap_reason": "",
        }
    staleness = _seconds_between(str(latest["observed_at"]), generated_at)
    freshness = _freshness_from_seconds(staleness)
    return {
        "instrument": instrument,
        "label": instrument,
        "latest": latest,
        "freshness": freshness,
        "freshness_label": {
            "realtime": "2分钟内",
            "near_realtime": "15分钟内",
            "delayed": "1小时内",
            "stale": "已延迟",
        }[freshness],
        "quote_type_label": "交易所/代理行情",
        "is_transaction_price": True,
        "staleness_seconds": staleness,
        "quote_age_days": None,
        "gap_reason": "",
    }


def _normalize_instruments(instruments: list[str] | None) -> set[str]:
    if not instruments:
        return set(CHAIN_INSTRUMENTS)
    requested = {item.strip().upper() for item in instruments if item.strip()}
    aliases = {"原油": "Brent", "石脑油": "NAPHTHA"}
    normalized = set()
    for item in requested:
        if item in aliases:
            normalized.add(aliases[item])
        elif item == "BRENT":
            normalized.add("Brent")
        elif item in CHAIN_INSTRUMENTS:
            normalized.add(item)
    return normalized or set(CHAIN_INSTRUMENTS)


def _freshness_from_seconds(value: int | None) -> str:
    if value is None:
        return "missing"
    if value <= 120:
        return "realtime"
    if value <= 900:
        return "near_realtime"
    if value <= 3600:
        return "delayed"
    return "stale"


def _seconds_between(left: str, right: str) -> int | None:
    left_dt = _parse_datetime(left)
    right_dt = _parse_datetime(right)
    if left_dt is None or right_dt is None:
        return None
    return max(0, int((right_dt - left_dt).total_seconds()))


def _days_between(left: str, right: str) -> int | None:
    left_dt = _parse_datetime(left)
    right_dt = _parse_datetime(right)
    if left_dt is None or right_dt is None:
        return None
    return max(0, (right_dt.date() - left_dt.date()).days)


def _parse_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.fromisoformat(f"{text[:10]}T00:00:00+00:00")
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _eastmoney_timestamp_to_iso(value: object) -> str:
    timestamp = _to_float(value)
    if timestamp is None or timestamp <= 0:
        raise ValueError("Eastmoney response missing observation timestamp")
    return datetime.fromtimestamp(int(timestamp), UTC).isoformat()


def _extract_datetime(fields: list[str]) -> str | None:
    date = next((field for field in fields if re.fullmatch(r"20\d{2}-\d{1,2}-\d{1,2}", field)), "")
    time_field = next((field for field in fields if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", field)), "")
    compact_time = next((field for field in fields if re.fullmatch(r"\d{6}", field)), "")
    if not time_field and compact_time:
        time_field = f"{compact_time[:2]}:{compact_time[2:4]}:{compact_time[4:6]}"
    if date and time_field:
        value = f"{_normalize_date(date)}T{time_field if len(time_field.split(':')) == 3 else f'{time_field}:00'}+08:00"
        try:
            return datetime.fromisoformat(value).isoformat()
        except ValueError:
            return None
    if date:
        try:
            return datetime.fromisoformat(f"{_normalize_date(date)}T00:00:00+08:00").isoformat()
        except ValueError:
            return None
    return None


def _normalize_date(value: str) -> str:
    parts = re.split(r"[-/年月日]+", value)
    parts = [part for part in parts if part]
    year, month, day = (int(part) for part in parts[:3])
    return f"{year:04d}-{month:02d}-{day:02d}"


def _html_to_visible_text(html: str) -> str:
    without_scripts = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", without_scripts)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _latest_numeric_index(values: list[object]) -> int | None:
    for index in range(len(values) - 1, -1, -1):
        if _to_float(values[index]) is not None:
            return index
    return None


def _previous_numeric(values: list[object], before_index: int) -> float | None:
    for index in range(before_index - 1, -1, -1):
        value = _to_float(values[index])
        if value is not None:
            return value
    return None


def _list_float(values: object, index: int) -> float | None:
    if not isinstance(values, list) or index >= len(values):
        return None
    return _to_float(values[index])


def _first_indexed_numeric(values: list[float | None], indexes: tuple[int, ...]) -> float | None:
    for index in indexes:
        if index < len(values) and values[index] is not None:
            return values[index]
    return None


def _change_pct(last: float | None, previous: float | None) -> float | None:
    if last is None or previous in {None, 0}:
        return None
    return round(((last - previous) / abs(previous)) * 100, 4)


def _to_float(value: object) -> float | None:
    if value in {None, "", "-", "--", "null"}:
        return None
    try:
        parsed = float(str(value).replace(",", ""))
        return parsed if math.isfinite(parsed) else None
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()

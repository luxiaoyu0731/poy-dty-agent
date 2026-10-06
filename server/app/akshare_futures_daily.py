from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from .futures_daily import PRODUCT_META, assign_futures_daily_contract_roles

SOURCE_ID = "akshare_prototype"
SOURCE_NAME = "AkShare futures daily prototype"
SOURCE_URL = "https://akshare.akfamily.xyz/data/futures/futures.html"
SOURCE_NOTE = (
    "AkShare wrapper over public futures pages, currently used only as a temporary prototype "
    "source until a company-authorized vendor API is available."
)
LICENSE_SCOPE = (
    "prototype/internal validation only; do not use for production display, redistribution, or external reports"
)
MAIN_RULE = (
    "AkShare/Sina continuous symbol {root}0 is stored separately; "
    "listed-contract main is largest open interest after close."
)


@dataclass(frozen=True)
class AkShareFuturesProduct:
    product: str
    root: str
    exchange: str
    unit: str
    contract_months: tuple[int, ...] = (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)


PRODUCTS: dict[str, AkShareFuturesProduct] = {
    "SC": AkShareFuturesProduct(product="SC", root="SC", exchange="INE", unit=PRODUCT_META["SC"]["unit"]),
    "PTA": AkShareFuturesProduct(product="PTA", root="TA", exchange="CZCE", unit=PRODUCT_META["PTA"]["unit"]),
    "PX": AkShareFuturesProduct(product="PX", root="PX", exchange="CZCE", unit=PRODUCT_META["PX"]["unit"]),
    # 2026-09-21 运营决定解冻 DCE/MEG 期货口径：仅用于行情页曲线（双口径标注，
    # 不与生意社现货评估拼接）；七品种预测标签注册表不受此影响。
    "MEG": AkShareFuturesProduct(product="MEG", root="EG", exchange="DCE", unit=PRODUCT_META["MEG"]["unit"]),
}


def fetch_akshare_futures_daily_bars(
    *,
    start: str = "2025-01-01",
    end: str | None = None,
    products: Iterable[str] | None = None,
    fetcher: Callable[[str], Any] | None = None,
    include_listed_contracts: bool = True,
    include_main_continuous: bool = True,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch AkShare/Sina futures daily bars and normalize them to futures_daily_bars payloads."""
    start_date = _parse_date(start)
    end_date = _parse_date(end or date.today().isoformat())
    if end_date < start_date:
        raise ValueError("end must be on or after start")

    selected = [_normalize_product(product) for product in (products or PRODUCTS)]
    fetch_daily = fetcher or _akshare_daily_fetcher()
    fetched_at = datetime.now(UTC).isoformat()
    rows: list[dict[str, Any]] = []
    errors: list[str] = []

    for product in selected:
        config = PRODUCTS[product]
        symbols: list[tuple[str, str]] = []
        if include_main_continuous:
            symbols.append((f"{config.root}0", "main_continuous"))
        if include_listed_contracts:
            symbols.extend((symbol, "listed_contract") for symbol in _contract_symbols(config, start_date, end_date))

        for symbol, role in symbols:
            try:
                frame = fetch_daily(symbol)
            except Exception as exc:  # noqa: BLE001 - third-party source failures are summarized per symbol.
                errors.append(f"{symbol}: {exc.__class__.__name__}: {exc}")
                continue
            try:
                symbol_rows = _rows_from_frame(
                    frame,
                    config=config,
                    symbol=symbol,
                    role=role,
                    start_date=start_date,
                    end_date=end_date,
                    fetched_at=fetched_at,
                )
            except Exception as exc:  # noqa: BLE001 - keep the batch alive if one symbol shape changes.
                errors.append(f"{symbol}: {exc.__class__.__name__}: {exc}")
                continue
            if not symbol_rows and role == "listed_contract":
                continue
            rows.extend(symbol_rows)

    assign_futures_daily_contract_roles(rows)
    return rows, errors


def _akshare_daily_fetcher() -> Callable[[str], Any]:
    try:
        import akshare as ak  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:
        message = "akshare is not installed; run `uv sync` in server or install the project dependencies"
        raise RuntimeError(message) from exc
    return ak.futures_zh_daily_sina


def _rows_from_frame(
    frame: Any,
    *,
    config: AkShareFuturesProduct,
    symbol: str,
    role: str,
    start_date: date,
    end_date: date,
    fetched_at: str,
) -> list[dict[str, Any]]:
    if frame is None or getattr(frame, "empty", False):
        return []
    records = frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)
    rows: list[dict[str, Any]] = []
    previous_close: float | None = None
    for raw in records:
        trade_date = _parse_date(str(_pick(raw, "date", "日期", "trade_date")))
        close = _number(_pick(raw, "close", "收盘价"))
        change_pct = None
        if previous_close not in {None, 0} and close is not None:
            change_pct = round((close / previous_close - 1) * 100, 6)
        previous_close = close if close is not None else previous_close
        if trade_date < start_date or trade_date > end_date:
            continue
        row = {
            "trade_date": trade_date.isoformat(),
            "exchange": config.exchange,
            "product": config.product,
            "contract_code": symbol,
            "contract_role": role,
            "term_structure_rank": None,
            "is_main": role == "main_continuous",
            "is_continuous": role == "main_continuous",
            "open": _required_number(raw, "open", "开盘价"),
            "high": _required_number(raw, "high", "最高价"),
            "low": _required_number(raw, "low", "最低价"),
            "close": _required_number(raw, "close", "收盘价"),
            "settle": _required_number(raw, "settle", "结算价"),
            "volume": _required_number(raw, "volume", "成交量"),
            "open_interest": _required_number(raw, "hold", "open_interest", "持仓量"),
            "change_pct": change_pct,
            "unit": config.unit,
            "source_publish_time": fetched_at,
            "visible_at": fetched_at,
            "source_id": SOURCE_ID,
            "source_name": SOURCE_NAME,
            "source_url": SOURCE_URL,
            "source_note": SOURCE_NOTE,
            "main_rule": MAIN_RULE.format(root=config.root),
            "revision_note": "AkShare/Sina historical rows may be revised by the upstream public page without notice.",
            "license_scope": LICENSE_SCOPE,
            "raw": _stringify_raw(raw),
        }
        rows.append(row)
    return rows


def _contract_symbols(config: AkShareFuturesProduct, start_date: date, end_date: date) -> list[str]:
    years = range(start_date.year, end_date.year + 2)
    symbols: list[str] = []
    for year in years:
        for month in config.contract_months:
            symbols.append(f"{config.root}{year % 100:02d}{month:02d}")
    return symbols


def _normalize_product(product: str) -> str:
    normalized = product.upper()
    if normalized not in PRODUCTS:
        raise ValueError(f"unsupported AkShare futures product: {product}")
    return normalized


def _pick(raw: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in raw:
            return raw[name]
    return None


def _required_number(raw: dict[str, Any], *names: str) -> float:
    value = _number(_pick(raw, *names))
    if value is None:
        raise ValueError(f"missing numeric field: {'/'.join(names)}")
    return value


def _number(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    if text in {"", "-", "--", "nan", "NaN", "None"}:
        return None
    return float(text)


def _parse_date(value: str) -> date:
    return datetime.strptime(value[:10].replace("/", "-"), "%Y-%m-%d").date()


def _stringify_raw(raw: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if hasattr(value, "isoformat"):
            normalized[str(key)] = value.isoformat()
        else:
            normalized[str(key)] = value
    return normalized

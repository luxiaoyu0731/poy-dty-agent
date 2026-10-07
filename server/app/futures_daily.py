from __future__ import annotations

import csv
import io
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

PRODUCT_META: dict[str, dict[str, str]] = {
    "SC": {"exchange": "INE", "unit": "CNY/bbl", "display": "INE SC crude oil futures"},
    "PTA": {"exchange": "CZCE", "unit": "CNY/mt", "display": "PTA futures"},
    "PX": {"exchange": "CZCE", "unit": "CNY/mt", "display": "PX futures"},
    "MEG": {"exchange": "DCE", "unit": "CNY/mt", "display": "MEG futures"},
}

PRODUCT_ALIASES = {
    "SC": "SC",
    "原油": "SC",
    "原油期货": "SC",
    "INE SC": "SC",
    "PTA": "PTA",
    "TA": "PTA",
    "精对苯二甲酸": "PTA",
    "PX": "PX",
    "对二甲苯": "PX",
    "MEG": "MEG",
    "EG": "MEG",
    "乙二醇": "MEG",
    "乙二醇期货": "MEG",
}

HEADER_ALIASES = {
    "trade_date": ("trade_date", "date", "trading_date", "交易日期", "日期"),
    "exchange": ("exchange", "市场", "交易所"),
    "product": ("product", "variety", "symbol_root", "品种", "品种代码", "合约品种"),
    "contract_code": ("contract_code", "contract", "symbol", "ts_code", "wind_code", "thscode", "合约代码", "代码"),
    "contract_role": ("contract_role", "role", "continuous_type", "main_flag", "合约标识", "连续合约标识", "主力标识"),
    "open": ("open", "open_price", "开盘价", "今开盘"),
    "high": ("high", "high_price", "最高价", "最高"),
    "low": ("low", "low_price", "最低价", "最低"),
    "close": ("close", "close_price", "收盘价", "今收盘"),
    "settle": ("settle", "settlement", "settle_price", "结算价", "今结算"),
    "volume": ("volume", "vol", "成交量"),
    "open_interest": ("open_interest", "position", "oi", "持仓量", "空盘量"),
    "change_pct": ("change_pct", "pct_chg", "pct_change", "涨跌幅"),
    "unit": ("unit", "单位"),
    "source_publish_time": ("source_publish_time", "publish_time", "发布时间", "数据发布时间"),
    "visible_at": ("visible_at", "as_of_time", "可见时间", "入库可见时间"),
    "source_id": ("source_id", "来源ID", "数据源ID"),
    "source_name": ("source_name", "来源名称", "数据源名称"),
    "source_url": ("source_url", "url", "来源链接", "数据链接"),
    "source_note": ("source_note", "source_description", "来源说明", "数据来源说明"),
    "license_scope": ("license_scope", "授权使用范围", "license"),
    "revision_note": ("revision_note", "修订说明", "历史修订说明"),
}

ROLE_ALIASES = {
    "main": "main",
    "主力": "main",
    "主力合约": "main",
    "main_continuous": "main_continuous",
    "主连": "main_continuous",
    "主力连续": "main_continuous",
    "secondary_continuous": "secondary_continuous",
    "次主连": "secondary_continuous",
    "次主力连续": "secondary_continuous",
    "near_month": "near_month",
    "近月": "near_month",
    "next_month": "next_month",
    "次月": "next_month",
    "index_continuous": "index_continuous",
    "指数连续": "index_continuous",
    "listed_contract": "listed_contract",
    "真实合约": "listed_contract",
}


def parse_futures_daily_csv(csv_text: str) -> tuple[list[dict[str, Any]], list[str]]:
    reader = csv.DictReader(io.StringIO(csv_text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return [], ["CSV is missing a header row."]

    header_map = _header_map(reader.fieldnames)
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    now = datetime.now(UTC).isoformat()
    for line_number, raw_row in enumerate(reader, start=2):
        try:
            row = _normalize_row(raw_row, header_map, now)
        except ValueError:
            errors.append(f"line {line_number}: invalid_row")
            continue
        if row["exchange"] == "DCE" or row["source_id"] == "dce_meg":
            errors.append(f"line {line_number}: dce_source_soft_removed")
            continue
        rows.append(row)
    _assign_contract_roles(rows)
    return rows, errors


def assign_futures_daily_contract_roles(rows: list[dict[str, Any]]) -> None:
    _assign_contract_roles(rows)


def _header_map(fieldnames: list[str]) -> dict[str, str]:
    normalized = {_normalize_header(name): name for name in fieldnames}
    mapped: dict[str, str] = {}
    for target, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            key = _normalize_header(alias)
            if key in normalized:
                mapped[target] = normalized[key]
                break
    return mapped


def _normalize_row(raw_row: dict[str, str], header_map: dict[str, str], now: str) -> dict[str, Any]:
    def text(field: str, default: str = "") -> str:
        source = header_map.get(field)
        value = raw_row.get(source, "") if source else default
        return str(value or default).strip()

    trade_date = _normalize_date(text("trade_date"))
    contract_code = text("contract_code")
    product = _normalize_product(text("product") or contract_code)
    meta = PRODUCT_META[product]
    if not trade_date:
        raise ValueError("trade_date is required")
    if not contract_code:
        raise ValueError("contract_code is required")
    payload = {
        "trade_date": trade_date,
        "exchange": (text("exchange") or meta["exchange"]).upper(),
        "product": product,
        "contract_code": contract_code,
        "contract_role": _normalize_role(text("contract_role"), contract_code),
        "open": _number(text("open"), "open"),
        "high": _number(text("high"), "high"),
        "low": _number(text("low"), "low"),
        "close": _number(text("close"), "close"),
        "settle": _number(text("settle"), "settle"),
        "volume": _number(text("volume"), "volume"),
        "open_interest": _number(text("open_interest"), "open_interest"),
        "change_pct": _number(text("change_pct"), "change_pct", required=False),
        "unit": text("unit") or meta["unit"],
        "source_publish_time": text("source_publish_time"),
        "visible_at": text("visible_at") or now,
        "source_id": text("source_id") or "authorized_futures_daily_csv",
        "source_name": text("source_name") or "Authorized futures daily CSV",
        "source_url": text("source_url"),
        "source_note": text("source_note") or "Imported from a company-authorized futures daily data export.",
        "license_scope": text("license_scope")
        or "pending_internal_license_review: do not redistribute outside authorized internal use",
        "revision_note": text("revision_note"),
        "term_structure_rank": None,
        "is_main": False,
        "is_continuous": False,
        "main_rule": (
            "commodity futures main contract is assigned after close by largest open interest; volume breaks ties"
        ),
        "raw": dict(raw_row),
    }
    payload["is_continuous"] = payload["contract_role"] in {
        "main_continuous",
        "secondary_continuous",
        "index_continuous",
        "month_continuous",
    }
    payload["is_main"] = payload["contract_role"] in {"main", "main_continuous"}
    return payload


def _assign_contract_roles(rows: list[dict[str, Any]]) -> None:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["contract_role"] == "listed_contract":
            groups[(row["trade_date"], row["product"])].append(row)

    for grouped_rows in groups.values():
        dated_contracts = sorted(
            (row for row in grouped_rows if _contract_delivery_key(row["contract_code"]) is not None),
            key=lambda row: _contract_delivery_key(row["contract_code"]) or 999999,
        )
        for rank, row in enumerate(dated_contracts):
            row["term_structure_rank"] = rank
            if rank == 0:
                row["contract_role"] = "near_month"
            elif rank == 1:
                row["contract_role"] = "next_month"

        main_row = max(
            grouped_rows,
            key=lambda row: (
                float(row.get("open_interest") or 0),
                float(row.get("volume") or 0),
                -(_contract_delivery_key(row["contract_code"]) or 999999),
            ),
        )
        main_row["is_main"] = True
        if main_row["contract_role"] not in {"near_month", "next_month"}:
            main_row["contract_role"] = "main"


def _normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-()/（）%]+", "", value.strip().lower())


def _normalize_date(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        return ""
    match = re.fullmatch(r"(\d{4})[-/]?(\d{1,2})[-/]?(\d{1,2})", candidate)
    if not match:
        return candidate
    year, month, day = match.groups()
    return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"


def _normalize_product(value: str) -> str:
    token = value.strip().upper()
    token = re.sub(r"[^A-Z\u4e00-\u9fff]", "", token)
    for alias, product in PRODUCT_ALIASES.items():
        if alias.upper() in token or alias in value:
            return product
    raise ValueError(f"unsupported product: {value}")


def _normalize_role(value: str, contract_code: str) -> str:
    role = ROLE_ALIASES.get(value.strip(), "")
    if role:
        return role
    code = contract_code.strip().upper()
    if code.endswith(("888", "88")):
        return "main_continuous"
    if code.endswith("99"):
        return "index_continuous"
    if code.endswith("22"):
        return "secondary_continuous"
    return "listed_contract"


def _number(value: str, field: str, *, required: bool = True) -> float | None:
    cleaned = value.replace(",", "").replace("%", "").strip()
    if cleaned in {"", "-", "--", "nan", "None"}:
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        return float(cleaned)
    except ValueError as exc:
        raise ValueError(f"{field} must be numeric") from exc


def _contract_delivery_key(contract_code: str) -> int | None:
    match = re.search(r"(\d{3,4})$", contract_code.strip())
    if not match:
        return None
    digits = match.group(1)
    if len(digits) == 3:
        year = 2020 + int(digits[0])
        month = int(digits[1:])
    else:
        year = 2000 + int(digits[:2])
        month = int(digits[2:])
    if not 1 <= month <= 12:
        return None
    return year * 100 + month

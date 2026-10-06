from __future__ import annotations

import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import UTC, date, datetime
from typing import Any

from .seven_product_contract import LABEL_REGISTRY
from .seven_product_forecast import load_current_label_series
from .source_priority import is_ccf_source, preferred_ccf_source
from .storage import list_futures_daily_bars, list_market_observations, list_source_capture_revisions

# 加工差 is computed from the operator's own non-CCF label series instead of
# the retired CCF industry observations: product price minus the frozen
# industry raw-material coefficient (0.855 * PTA + 0.335 * MEG), the same
# coefficient the public benchmark v2 cost-pressure formula froze.
COMPUTED_SPREAD_WEIGHTS = {"PTA": 0.855, "MEG": 0.335}
COMPUTED_SPREAD_TARGETS = ("poy", "dty")
COMPUTED_SPREAD_SOURCE_ID = "computed_cost_spread"
# 库存/开工（行业观察口径）已按操作人决定软移除（ADR-0009）：不再读取、
# 不再投影、不参与新鲜度/就绪判定；历史行只读保留，新闻含定性佐证。
# 原油品种的库存/开工走 EIA 市场观测（inventory_market/operating_market），不受影响。
PRODUCTS: list[dict[str, Any]] = [
    {
        "key": "POY",
        "label": "POY",
        "price_product": "POY",
        "canonical_price": {
            "product": "POY",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "POY 150D/144F",
            "quote_type": "daily_average",
            "unit": "CNY/mt",
        },
        "inventory": ("POY", "poy_inventory", "POY 库存天数"),
        "operating": ("POLYESTER", "polyester_operating_rate", "聚酯开工率"),
        "profit": ("POY", "poy_profit", "POY 加工差（非净利润）"),
    },
    {
        "key": "DTY",
        "label": "DTY",
        "price_product": "DTY",
        "canonical_price": {
            "product": "DTY",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "DTY 150D/144F轻网",
            "quote_type": "daily_average",
            "unit": "CNY/mt",
        },
        "inventory": ("DTY", "dty_inventory", "DTY 库存天数"),
        "operating": ("POLYESTER", "polyester_operating_rate", "聚酯开工率"),
        "profit": ("DTY", "dty_profit", "DTY 加工差（非净利润）"),
    },
    {
        "key": "PX",
        "label": "PX",
        "price_product": "PX",
        "canonical_price": {
            "product": "PX",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "PX CFR中国",
            "quote_type": "daily_average",
            "unit": "USD/mt",
        },
        "inventory": ("POY", "poy_inventory", "下游库存天数"),
        "operating": ("POLYESTER", "polyester_operating_rate", "聚酯开工率"),
        "profit": ("POLYESTER", "polyester_profit", "聚酯加工差（非净利润）"),
    },
    {
        "key": "PTA",
        "label": "PTA",
        "price_product": "PTA",
        "canonical_price": {
            "product": "PTA",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "内盘PTA",
            "quote_type": "daily_average",
            "unit": "CNY/mt",
        },
        "inventory": ("POY", "poy_inventory", "下游库存天数"),
        "operating": ("POLYESTER", "polyester_operating_rate", "聚酯开工率"),
        "profit": ("POLYESTER", "polyester_profit", "聚酯加工差（非净利润）"),
    },
    {
        "key": "MEG",
        "label": "MEG",
        "price_product": "MEG",
        "canonical_price": {
            "product": "MEG",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "内盘MEG现货",
            "quote_type": "daily_average",
            "unit": "CNY/mt",
        },
        "inventory": ("DTY", "dty_inventory", "下游库存天数"),
        "operating": ("POLYESTER", "polyester_operating_rate", "聚酯开工率"),
        "profit": ("POLYESTER", "polyester_profit", "聚酯加工差（非净利润）"),
    },
    {
        "key": "NAPHTHA",
        "label": "石脑油",
        "price_product": "NAPHTHA",
        "canonical_price": {
            "product": "NAPHTHA",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "日本石脑油",
            "quote_type": "daily_average",
            "unit": "USD/mt",
        },
        "inventory": ("POY", "poy_inventory", "下游库存天数"),
        "operating": ("POLYESTER", "polyester_operating_rate", "聚酯开工率"),
        "profit": ("POLYESTER", "polyester_profit", "聚酯加工差（非净利润）"),
    },
    {
        "key": "CRUDE",
        "label": "原油",
        "price_product": "Brent",
        "canonical_price": {
            "product": "Brent",
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "spec": "Brent期货",
            "quote_type": "daily_average",
            "unit": "USD/bbl",
        },
        "profit": ("POLYESTER", "polyester_profit", "下游聚酯加工差（非净利润）"),
    },
]


def build_market_chain_workbench(*, as_of_time: str | None = None) -> dict[str, Any]:
    generated_at = datetime.now(UTC).isoformat()
    # 价格序列已重锚到自有标签账本（原生币种），FX 换算仅在个别 USD 观测
    # 展示路径（convert_latest_usd_per_ton）按需使用，不再整表预加载。
    products = [_build_product_view(config, as_of_time=as_of_time) for config in PRODUCTS]
    return {
        "generated_at": generated_at,
        "products": products,
        "coverage": {
            "product_count": len(products),
            "price_ready": sum(1 for item in products if item["latest_price"]["status"] == "available"),
            "indicator_ready": sum(1 for item in products if item["data_freshness"].get("indicator_ready") is True),
        },
    }


def _build_product_view(
    config: dict[str, Any],
    *,
    as_of_time: str | None = None,
    fx_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    # 价格主序列重锚（ADR-0009 后续）：CCF 冻结序列不再是图表基准，
    # 改用操作人自有标签账本的 point-in-time 序列（TNC/郑商所/SunSirs/TE/EIA）。
    cutoff = as_of_time or datetime.now(UTC).isoformat()
    try:
        label_cutoff = datetime.fromisoformat(cutoff.replace("Z", "+00:00"))
    except ValueError:
        label_cutoff = datetime.now(UTC)
    price_series, price_basis = _label_price_series(config, as_of=label_cutoff)
    # 库存/开工/运费维度已按运营决策整体下线：不读取、不投影、不出现在契约里。
    profit_metric_label = _metric_label(config, "profit")
    spread_product = str(config["profit"][0]) if "profit" in config else ""
    if spread_product.lower() in COMPUTED_SPREAD_TARGETS:
        profit_series = _computed_profit_series(
            config, as_of_time=as_of_time, metric_label=profit_metric_label
        )
    else:
        # A downstream POY/DTY-based spread is not defined for feedstock or
        # crude rows; keep it empty instead of implying a measurement exists.
        profit_series = []
    # 单一可审计基准：图表与摘要只用当前标签序列；CCF 历史保留在库但不再上图。
    summary_points = price_series
    price_summary = _price_summary(config["label"], price_series, summary_points)
    if price_basis is not None:
        price_summary = {**price_summary, "basis": price_basis}
    cutoff_date = _as_of_date(as_of_time)
    data_freshness = _freshness(
        price_series,
        profit_series,
        as_of_date=cutoff_date,
    )
    spread_summary = _change_summary(
        "价格", price_series, price_summary["metric_label"], up_tone="warning", down_tone="info"
    )
    latest_price_point = price_series[-1] if price_series else {}
    if isinstance(latest_price_point, dict) and latest_price_point.get("trend_eligible") is False:
        spread_summary = {
            **spread_summary,
            "status": "unavailable",
            "tag": "未形成",
            "tone": "muted",
            "trend_eligible": False,
            "detail": (
                f"{price_summary['metric_label']}已展示最新可用价格点；该点与历史基准口径不同，不生成同口径涨跌判断。"
            ),
        }
    elif data_freshness["categories"]["price"]["status"] == "stale" and spread_summary["status"] == "available":
        price_freshness = data_freshness["categories"]["price"]
        spread_summary = {
            **spread_summary,
            "status": "stale",
            "tag": "数据过期",
            "tone": "warning",
            "trend_eligible": False,
            "detail": (
                f"{price_summary['metric_label']}历史序列仅更新至"
                f"{price_freshness.get('latest_date') or '未知日期'}，"
                "不作为今日方向判断依据。"
            ),
        }
    else:
        spread_summary = {**spread_summary, "trend_eligible": spread_summary["status"] == "available"}

    return {
        "key": config["key"],
        "label": config["label"],
        "price_series": price_series,
        "profit_series": profit_series,
        "latest_price": price_summary,
        "latest_display_price": price_summary,
        "latest_display_freshness": data_freshness["categories"]["price"],
        "spread_summary": spread_summary,
        "profit_summary": _change_summary(
            "利润", profit_series, _metric_label(config, "profit"), up_tone="success", down_tone="warning"
        ),
        "data_freshness": data_freshness,
        "data_coverage": {
            "price_points": len(summary_points),
            "price_days": len(price_series),
            "profit_points": len(profit_series),
        },
        "quality_warnings": _quality_warnings(
            config["label"],
            config,
            price_series,
            profit_series,
        ),
    }


def _canonical_price_points(points: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    basis = config.get("canonical_price")
    if not isinstance(basis, dict):
        return []
    required = ("product", "dataset_type", "spec", "quote_type", "unit")
    matching = [
        point
        for point in points
        if is_ccf_source(point.get("source_id"))
        and all(str(point.get(field) or "").strip() == str(basis.get(field) or "").strip() for field in required)
    ]
    preferred_source = preferred_ccf_source(matching)
    return [point for point in matching if str(point.get("source_id") or "").strip().lower() == preferred_source]


def _aggregate_price_points(
    points: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    as_of_time: str | None = None,
    fx_rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    canonical = _canonical_price_points(points, config)
    by_date: dict[str, dict[str, Any]] = {}
    for point in canonical:
        value = point.get("price")
        observed_at = str(point.get("observed_at") or "")
        if value is None or not observed_at:
            continue
        point_date = observed_at[:10]
        existing = by_date.get(point_date)
        if existing is None or str(point.get("created_at") or "") >= str(existing.get("created_at") or ""):
            by_date[point_date] = point

    conversion_rows: list[dict[str, Any]] = []
    if str(config.get("canonical_price", {}).get("unit") or "") == "USD/mt":
        conversion_rows = (
            fx_rows
            if fx_rows is not None
            else _usd_cny_rate_rows(as_of_time=as_of_time)
            if as_of_time
            else _usd_cny_rate_rows()
        )
    fx_dates = [str(row.get("observed_at") or "")[:10] for row in conversion_rows]
    series: list[dict[str, Any]] = []
    for observed_at in sorted(by_date):
        point = by_date[observed_at]
        spec = str(point.get("spec") or "")
        unit = str(point.get("unit") or "")
        original_value = float(point["price"])
        row = {
            "date": observed_at,
            "value": round(original_value, 4),
            "unit": unit,
            "label": spec or "日度价格",
            "sample_count": 1,
            "spec_count": 1,
            "comparison_basis": {
                "product": str(config.get("key") or point.get("product") or ""),
                "market": str(point.get("market") or ""),
                "spec": spec,
                "quote_type": str(point.get("quote_type") or ""),
                "source_basis": str(point.get("source_id") or ""),
                "unit": unit,
            },
        }
        if unit == "USD/mt":
            conversion = _usd_per_ton_to_cny(original_value, observed_at, conversion_rows, fx_dates=fx_dates)
            if conversion is None:
                continue
            row.update(conversion)
            row["comparison_basis"] = {
                **row["comparison_basis"],
                "unit": "CNY/mt",
                "original_unit": "USD/mt",
                "conversion_method": "usd_cny_daily",
            }
        series.append(row)
    return series


def _usd_cny_rate_rows(*, as_of_time: str | None = None) -> list[dict[str, Any]]:
    rows = list_market_observations(
        source_id="fred_macro_api",
        product="fx",
        indicator="China / U.S. Foreign Exchange Rate (DEXCHUS)",
        end=as_of_time,
        as_of_time=as_of_time,
        limit=None,
    )
    return sorted(
        (
            row
            for row in rows
            if row.get("value") is not None
            and str(row.get("unit") or "") == "cny_per_usd"
            and str(row.get("observed_at") or "")
        ),
        key=lambda row: str(row.get("observed_at") or "")[:10],
    )


def _usd_per_ton_to_cny(
    value: float,
    observed_at: str,
    fx_rows: list[dict[str, Any]],
    *,
    fx_dates: list[str] | None = None,
    max_carry_days: int = 7,
) -> dict[str, Any] | None:
    price_date_text = str(observed_at or "")[:10]
    if not price_date_text or not fx_rows:
        return None
    indexed_dates = fx_dates if fx_dates is not None else [str(row.get("observed_at") or "")[:10] for row in fx_rows]
    index = bisect_right(indexed_dates, price_date_text) - 1
    if index < 0:
        return None
    fx_row = fx_rows[index]
    fx_date_text = indexed_dates[index]
    try:
        lag_days = (date.fromisoformat(price_date_text) - date.fromisoformat(fx_date_text)).days
        fx_rate = float(fx_row["value"])
    except (TypeError, ValueError):
        return None
    if lag_days < 0 or lag_days > max_carry_days or fx_rate <= 0:
        return None
    return {
        "value": round(value * fx_rate, 2),
        "unit": "CNY/mt",
        "original_value": round(value, 4),
        "original_unit": "USD/mt",
        "fx_rate": round(fx_rate, 6),
        "fx_date": fx_date_text,
        "fx_source_id": str(fx_row.get("source_id") or "fred_macro_api"),
        "conversion_status": "exact" if lag_days == 0 else "previous_available",
        "fx_lag_days": lag_days,
    }


def convert_latest_usd_per_ton(value: float, observed_at: str) -> dict[str, Any] | None:
    """Convert a current USD/mt quote with the same audited daily-FX policy."""
    return _usd_per_ton_to_cny(value, observed_at, _usd_cny_rate_rows())


def _append_latest_available_price_point(
    all_points: list[dict[str, Any]],
    series: list[dict[str, Any]],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    latest_series_date = str(series[-1].get("date") or "") if series else ""
    candidates = [
        point
        for point in all_points
        if point.get("price") is not None
        and str(point.get("observed_at") or "").strip()
        and str(point.get("product") or "").strip() == str(config.get("price_product") or "").strip()
    ]
    if not candidates:
        return series, None
    latest_point = max(
        candidates,
        key=lambda point: (
            str(point.get("observed_at") or "")[:10],
            str(point.get("created_at") or ""),
        ),
    )
    latest_date = str(latest_point.get("observed_at") or "")[:10]
    if not latest_date or (latest_series_date and latest_date <= latest_series_date):
        return series, None

    output = [row for row in series if str(row.get("date") or "") != latest_date]
    output.append(_price_point_to_series_row(latest_point, config, trend_eligible=False))
    output.sort(key=lambda row: str(row.get("date") or ""))
    return output, latest_point


def _price_point_to_series_row(
    point: dict[str, Any],
    config: dict[str, Any],
    *,
    trend_eligible: bool | None = None,
) -> dict[str, Any]:
    spec = str(point.get("spec") or "")
    unit = str(point.get("unit") or "")
    row = {
        "date": str(point.get("observed_at") or "")[:10],
        "value": round(float(point["price"]), 4),
        "unit": unit,
        "label": spec or "最新可用价格",
        "sample_count": 1,
        "spec_count": 1,
        "comparison_basis": {
            "product": str(config.get("key") or point.get("product") or ""),
            "market": str(point.get("market") or ""),
            "spec": spec,
            "quote_type": str(point.get("quote_type") or ""),
            "source_basis": str(point.get("source_id") or ""),
            "unit": unit,
        },
    }
    if trend_eligible is not None:
        row["trend_eligible"] = trend_eligible
    return row


LABEL_PRICE_TARGETS = {
    "POY": "poy",
    "DTY": "dty",
    "PX": "px",
    "PTA": "pta",
    "MEG": "meg",
    "NAPHTHA": "naphtha",
    "CRUDE": "crude",
}

# 展示层曲线口径覆盖（2026-09-21 运营决定）：POY/DTY 用纺织网日度文章序列、
# MEG 用大商所 EG 主力连续结算——各为单一同口径来源，不与原标签序列拼接；
# 七品种预测标签注册表不受影响。覆盖数据缺失时回退到原标签序列。
TEXNET_CURVE_PRODUCTS = {"POY": "poy", "DTY": "dty"}
TEXNET_CURVE_SOURCE_ID = "texnet_price_articles"
TEXNET_CURVE_SERIES_IDS = {
    "poy": "poy.texnet.daily_assessment.cny_mt",
    "dty": "dty.texnet.daily_assessment.cny_mt",
}
DCE_FUTURES_CURVE_PRODUCTS = {"MEG": "MEG"}
DCE_FUTURES_CURVE_SOURCE_ID = "akshare_prototype"
DCE_FUTURES_CURVE_SERIES_ID = "meg.dce.main_continuous.settlement.cny_mt"


def _texnet_curve_series(
    product_key: str,
    *,
    as_of: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None] | None:
    """POY/DTY 曲线改用纺织网日度文章序列（单一同口径，覆盖数据存在时启用）。"""
    target = TEXNET_CURVE_PRODUCTS.get(product_key)
    if target is None:
        return None
    captures = list_source_capture_revisions(
        source_id=TEXNET_CURVE_SOURCE_ID,
        semantic_series_id=TEXNET_CURVE_SERIES_IDS[target],
        as_of_time=as_of.isoformat(),
        limit=5000,
    )
    if not captures:
        return None
    by_date: dict[str, dict[str, Any]] = {}
    for capture in captures:
        payload = capture.get("canonical_payload")
        if not isinstance(payload, dict):
            continue
        value = payload.get("value")
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        day = str(capture.get("observed_at") or "")[:10]
        if not day:
            continue
        existing = by_date.get(day)
        visible_at = str(capture.get("visible_at") or "")
        if existing is None or visible_at >= str(existing["visible_at"]):
            by_date[day] = {
                "date": day,
                "value": float(value),
                "unit": "CNY/mt",
                "label": "POY" if target == "poy" else "DTY",
                "sample_count": 1,
                "source_id": TEXNET_CURVE_SOURCE_ID,
                "visible_at": visible_at,
                "comparison_basis": {
                    "product": product_key,
                    "series_id": TEXNET_CURVE_SERIES_IDS[target],
                    "source_basis": TEXNET_CURVE_SOURCE_ID,
                    "unit": "CNY/mt",
                },
            }
    if not by_date:
        return None
    kept_days, excluded = _reject_outlier_days(
        [(day, by_date[day]["value"]) for day in sorted(by_date)]
    )
    series = [{**by_date[day], "value": round(by_date[day]["value"], 2)} for day in kept_days]
    basis = {
        "series_id": TEXNET_CURVE_SERIES_IDS[target],
        "source_id": TEXNET_CURVE_SOURCE_ID,
        "source_matches_label": False,
        "data_gaps": [],
        "points": len(series),
        "outliers_excluded": excluded,
    }
    return series, basis


def _dce_futures_curve_series(
    product_key: str,
    *,
    as_of: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None] | None:
    """MEG 曲线改用大商所 EG 主力连续结算（覆盖数据存在时启用）。"""
    product = DCE_FUTURES_CURVE_PRODUCTS.get(product_key)
    if product is None:
        return None
    bars = list_futures_daily_bars(
        source_id=DCE_FUTURES_CURVE_SOURCE_ID,
        product=product,
        contract_role="main_continuous",
        end=as_of.isoformat(),
        limit=2000,
    )
    if not bars:
        return None
    by_date: dict[str, dict[str, Any]] = {}
    for bar in bars:
        value = bar.get("settle") or bar.get("close")
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        day = str(bar.get("trade_date") or "")[:10]
        if not day:
            continue
        by_date[day] = {
            "date": day,
            "value": float(value),
            "unit": str(bar.get("unit") or "CNY/mt"),
            "label": product_key,
            "sample_count": 1,
            "source_id": DCE_FUTURES_CURVE_SOURCE_ID,
            "visible_at": str(bar.get("visible_at") or bar.get("created_at") or day),
            "comparison_basis": {
                "product": product_key,
                "series_id": DCE_FUTURES_CURVE_SERIES_ID,
                "source_basis": DCE_FUTURES_CURVE_SOURCE_ID,
                "unit": str(bar.get("unit") or "CNY/mt"),
            },
        }
    kept_days, excluded = _reject_outlier_days(
        [(day, by_date[day]["value"]) for day in sorted(by_date)]
    )
    series = [{**by_date[day], "value": round(by_date[day]["value"], 2)} for day in kept_days]
    basis = {
        "series_id": DCE_FUTURES_CURVE_SERIES_ID,
        "source_id": DCE_FUTURES_CURVE_SOURCE_ID,
        "source_matches_label": False,
        "data_gaps": [],
        "points": len(series),
        "outliers_excluded": excluded,
    }
    return series, basis


def _label_price_series(
    config: dict[str, Any],
    *,
    as_of: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Build the workbench price trend from the operator's own label ledger.

    同一序列同时用于预测标签与图表：不再把已软移除的 CCF 冻结历史当作趋势基准。
    序列短于 30 日时按协议诚实显示，而不是回接旧口径历史。
    """
    target = LABEL_PRICE_TARGETS.get(str(config.get("key") or ""))
    if target is None:
        return [], None
    product_key = str(config.get("key") or "")
    override = _texnet_curve_series(product_key, as_of=as_of)
    if override is None:
        override = _dce_futures_curve_series(product_key, as_of=as_of)
    if override is not None and override[0]:
        return override
    loaded = load_current_label_series(target, as_of)
    by_date: dict[str, dict[str, Any]] = {}
    for point in loaded.points:
        day = str(point.observed_at)[:10]
        existing = by_date.get(day)
        if existing is None or str(point.visible_at) >= str(existing["visible_at"]):
            by_date[day] = {
                "date": day,
                "value": float(point.value),
                "unit": str(point.unit),
                "label": str(config.get("label") or target),
                "sample_count": 1,
                "source_id": str(point.source_id),
                "visible_at": str(point.visible_at),
                "comparison_basis": {
                    "product": str(config.get("key") or ""),
                    "series_id": LABEL_REGISTRY[target].series_id,
                    "source_basis": str(point.source_id),
                    "unit": str(point.unit),
                },
            }
    kept_days, excluded = _reject_outlier_days(
        [(day, by_date[day]["value"]) for day in sorted(by_date)]
    )
    series = []
    for day in kept_days:
        row = by_date[day]
        series.append({**row, "value": round(row["value"], 2)})
    basis = {
        "series_id": LABEL_REGISTRY[target].series_id,
        "source_id": LABEL_REGISTRY[target].source_id,
        "source_matches_label": loaded.source_matches_label,
        "data_gaps": list(loaded.data_gaps),
        "points": len(series),
        "outliers_excluded": excluded,
    }
    return series, basis


def _reject_outlier_days(
    day_values: list[tuple[str, float]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Drop ledger points whose value deviates wildly from neighboring days.

    解析错误的账本行（尖峰/近零）会拉伸图表纵轴；这里只在展示序列构建时隔离
    异常点，账本本身保持 append-only 不动。窗口为中位数 ±2 个有效邻点，偏离
    超过 1.6 倍判为异常；非正值直接剔除。
    """
    if len(day_values) < 5:
        kept = [(day, value) for day, value in day_values if value > 0]
        excluded = [
            {"date": day, "value": value, "reason": "non_positive"}
            for day, value in day_values
            if value <= 0
        ]
        return [day for day, _ in kept], excluded
    values = [value for _, value in day_values]
    kept_flags = [value > 0 for _, value in day_values]
    excluded: list[dict[str, Any]] = []
    changed = True
    while changed:
        changed = False
        for index, (day, value) in enumerate(day_values):
            if not kept_flags[index]:
                continue
            window = [
                values[neighbor]
                for neighbor in range(max(0, index - 2), min(len(values), index + 3))
                if neighbor != index and kept_flags[neighbor]
            ]
            local_median = statistics.median(window) if window else value
            if local_median > 0 and (value > local_median * 1.6 or value < local_median / 1.6):
                kept_flags[index] = False
                excluded.append({"date": day, "value": value, "reason": "outlier_vs_local_median"})
                changed = True
    kept = [day_values[index] for index, flag in enumerate(kept_flags) if flag]
    return [day for day, _ in kept], excluded


def _computed_profit_series(
    config: dict[str, Any],
    *,
    as_of_time: str | None,
    metric_label: str,
) -> list[dict[str, Any]]:
    """Derive the processing spread from the operator's own label ledger.

    margin = product price − (0.855 × PTA + 0.335 × MEG) on dates where all
    three point-in-time label series publish a value. The result is a derived
    indicator, not a captured observation: rows disclose their formula and the
    label series they were computed from.
    """
    spread_product = str(config["profit"][0])
    spread_target = spread_product.lower()
    if spread_target not in COMPUTED_SPREAD_TARGETS:
        return []
    cutoff_text = as_of_time or datetime.now(UTC).isoformat()
    try:
        cutoff = datetime.fromisoformat(cutoff_text.replace("Z", "+00:00"))
    except ValueError:
        cutoff = datetime.now(UTC)

    def day_values(target: str) -> dict[str, tuple[float, str]]:
        loaded = load_current_label_series(target, cutoff)
        by_date: dict[str, tuple[float, str]] = {}
        for point in loaded.points:
            if str(point.unit) != "CNY/mt":
                continue
            day = str(point.observed_at)[:10]
            visibility = str(point.visible_at)
            existing = by_date.get(day)
            if existing is None or visibility >= existing[1]:
                by_date[day] = (float(point.value), visibility)
        return by_date

    product_days = day_values(spread_target)
    pta_days = day_values("pta")
    meg_days = day_values("meg")
    common_dates = set(product_days) & set(pta_days) & set(meg_days)
    series: list[dict[str, Any]] = []
    for point_date in sorted(common_dates):
        margin = product_days[point_date][0] - (
            COMPUTED_SPREAD_WEIGHTS["PTA"] * pta_days[point_date][0]
            + COMPUTED_SPREAD_WEIGHTS["MEG"] * meg_days[point_date][0]
        )
        series.append(
            {
                "date": point_date,
                "value": round(margin, 2),
                "unit": "CNY/mt",
                "label": metric_label,
                "sample_count": 1,
                "source_id": COMPUTED_SPREAD_SOURCE_ID,
                "frequency": "daily",
                "basis": {
                    "formula": f"{spread_target.upper()} − (0.855 × PTA + 0.335 × MEG)",
                    "inputs": {
                        spread_target: LABEL_REGISTRY[spread_target].series_id,
                        "PTA": LABEL_REGISTRY["pta"].series_id,
                        "MEG": LABEL_REGISTRY["meg"].series_id,
                    },
                },
            }
        )
    return series
def _as_of_date(as_of_time: str | None) -> date | None:
    text = str(as_of_time or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _observation_series(rows: list[dict[str, Any]], metric_label: str) -> list[dict[str, Any]]:
    valid_rows = [row for row in rows if row.get("value") is not None and str(row.get("observed_at") or "")]
    if not valid_rows:
        return []

    def basis(row: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(row.get("source_id") or ""),
            str(row.get("unit") or ""),
            str(row.get("frequency") or ""),
        )

    basis_counts = Counter(basis(row) for row in valid_rows)
    canonical_basis = sorted(
        basis_counts,
        key=lambda item: (0 if is_ccf_source(item[0]) else 1, -basis_counts[item], item),
    )[0]

    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in valid_rows:
        if basis(row) != canonical_basis:
            continue
        observed_at = str(row["observed_at"])
        by_date[observed_at[:10]].append(row)

    series: list[dict[str, Any]] = []
    for observed_at in sorted(by_date):
        bucket = by_date[observed_at]
        latest = max(
            bucket,
            key=lambda row: (
                str(row.get("created_at") or ""),
                str(row.get("observed_at") or ""),
            ),
        )
        series.append(
            {
                "date": observed_at,
                "value": float(latest["value"]),
                "unit": canonical_basis[1],
                "label": metric_label,
                "sample_count": 1,
                "source_id": canonical_basis[0],
                "frequency": canonical_basis[2],
            }
        )
    return series


def _price_summary(label: str, series: list[dict[str, Any]], raw_points: list[dict[str, Any]]) -> dict[str, Any]:
    if not series:
        return {
            "status": "missing",
            "metric_label": f"{label} 价格",
            "quality_label": "价格序列未入库",
            "detail": f"{label} 尚未返回可绘制的价格序列。",
            "points": 0,
        }
    latest = series[-1]
    return {
        "status": "available",
        "metric_label": f"{label} 价格",
        "quality_label": "可用于观察",
        "date": latest["date"],
        "value": latest["value"],
        "unit": latest["unit"],
        "points": len(raw_points),
        "day_count": len(series),
        "spec_count": max((int(point.get("spec_count") or 0) for point in series), default=0),
        "detail": f"{label} 最新价格来自 {len(series)} 个交易日、{len(raw_points)} 条价格记录。",
    }
def _change_summary(
    title: str,
    series: list[dict[str, Any]],
    metric_label: str,
    *,
    up_tone: str,
    down_tone: str,
) -> dict[str, Any]:
    if not series:
        return {
            "status": "missing",
            "title": title,
            "metric_label": metric_label,
            "tag": "未入库",
            "tone": "warning",
            "detail": f"系统未返回{metric_label}序列，暂不作为本轮判断依据。",
            "points": 0,
        }
    latest = series[-1]
    previous = series[-2] if len(series) > 1 else None
    delta = float(latest["value"]) - float(previous["value"]) if previous else None
    if delta is None:
        tag = "已覆盖"
        tone = "success"
        change_text = "暂无上期对比"
    elif delta > 0:
        tag = "上行"
        tone = up_tone
        change_text = f"较上期增加 {abs(delta):.2f}"
    elif delta < 0:
        tag = "下行"
        tone = down_tone
        change_text = f"较上期减少 {abs(delta):.2f}"
    else:
        tag = "持平"
        tone = "info"
        change_text = "较上期基本持平"
    return {
        "status": "available",
        "title": title,
        "metric_label": metric_label,
        "tag": tag,
        "tone": tone,
        "date": latest["date"],
        "value": latest["value"],
        "unit": latest["unit"],
        "points": len(series),
        "detail": f"{metric_label}最新为 {latest['value']:.2f}{_unit_suffix(latest.get('unit'))}，{change_text}。",
    }


def _metric_label(config: dict[str, Any], kind: str) -> str:
    market_key = f"{kind}_market"
    if market_key in config:
        return str(config[market_key][2])
    if kind in config:
        return str(config[kind][2])
    return "业务指标"


def _freshness(
    price_series: list[dict[str, Any]],
    profit_series: list[dict[str, Any]],
    *,
    as_of_date: date | None = None,
) -> dict[str, Any]:
    effective_date = as_of_date or datetime.now(UTC).date()
    category_inputs = {
        "price": (price_series, 3),
        "profit": (profit_series, 3),
    }
    categories: dict[str, dict[str, Any]] = {}
    for name, (series, max_age_days) in category_inputs.items():
        if not series:
            categories[name] = {
                "status": "missing",
                "latest_date": None,
                "age_days": None,
                "max_age_days": max_age_days,
            }
            continue
        latest_date = str(series[-1].get("date") or "")[:10]
        try:
            age_days = max(0, (effective_date - date.fromisoformat(latest_date)).days)
        except ValueError:
            categories[name] = {
                "status": "missing",
                "latest_date": latest_date or None,
                "age_days": None,
                "max_age_days": max_age_days,
            }
            continue
        categories[name] = {
            "status": "fresh" if age_days <= max_age_days else "stale",
            "latest_date": latest_date,
            "age_days": age_days,
            "max_age_days": max_age_days,
        }
    latest_dates = [str(item["latest_date"]) for item in categories.values() if item.get("latest_date")]
    if not latest_dates:
        return {
            "status": "missing",
            "label": "暂无更新时间",
            "latest_date": None,
            "categories": categories,
            "indicator_ready": False,
        }
    latest = max(latest_dates)
    indicator_ready = categories["profit"]["status"] == "fresh"
    overall_status = "fresh" if categories["price"]["status"] == "fresh" and indicator_ready else "stale"
    category_label = "；".join(
        f"{name}至{item.get('latest_date') or '缺失'}({item['status']})" for name, item in categories.items()
    )
    return {
        "status": overall_status,
        "label": category_label,
        "latest_date": latest,
        "categories": categories,
        "indicator_ready": indicator_ready,
    }


def _quality_warnings(
    label: str,
    config: dict[str, Any],
    price_series: list[dict[str, Any]],
    profit_series: list[dict[str, Any]],
) -> list[str]:
    warnings: list[str] = []
    if not price_series:
        warnings.append(f"{label} 价格序列未返回。")
    if not profit_series:
        spread_product = str(config.get("profit", ("",))[0])
        if spread_product.lower() in COMPUTED_SPREAD_TARGETS:
            warnings.append(f"{label} 加工差自算缺少同日 PTA/MEG/产品观测。")
        else:
            warnings.append(f"{label} 下游加工差无结构化来源，不自算。")
    return warnings


def _unit_suffix(unit: Any) -> str:
    text = str(unit or "").strip()
    if not text:
        return ""
    replacements = {
        "CNY/mt": "元/吨",
        "USD/mt": "美元/吨",
        "USD/bbl": "美元/桶",
        "$/BBL": "美元/桶",
        "dollars_per_barrel": "美元/桶",
        "MBBL": "千桶",
        "MBBL/D": "千桶/日",
        "percent": "%",
    }
    return replacements.get(text, text)

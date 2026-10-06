from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

from .data_governance import validate_observed_at_syntax
from .event_identity import deduplicate_by_aliases, event_aliases
from .models import EventImpact, FactorScore, MorningBriefItem, PredictionReview
from .prediction import compute_cost_pressure_index
from .prediction_contract import enrich_prediction_record, is_legacy_directional_prediction_record
from .seven_product_contract import LABEL_REGISTRY
from .source_priority import is_ccf_source
from .source_registry import get_source
from .storage import (
    list_event_observations,
    list_forecast_price_points,
    list_futures_daily_bars,
    list_industry_observations,
    list_intraday_price_observations,
    list_market_observations,
    list_prediction_ledger_records,
)

HORIZON_DAYS = {"1d": 1, "7d": 7, "14d": 14, "30d": 30}
FULL_CHAIN_PRODUCTS = ("CRUDE", "PX", "PTA", "MEG", "POY", "DTY")
DEFAULT_READ_AS_OF_STABILITY_SECONDS = 5.0

_DEFAULT_READ_AS_OF_LOCK = threading.Lock()
_DEFAULT_READ_AS_OF_VALUE: datetime | None = None
_DEFAULT_READ_AS_OF_EXPIRES_AT = 0.0

_CRUDE_PRICE_UNITS = {
    "$/bbl",
    "dollars_per_barrel",
    "usd/bbl",
    "usd_per_barrel",
    "美元/桶",
}
_POLYESTER_PRICE_UNITS = {
    "cny/mt",
    "cny_per_mt",
    "usd/mt",
    "usd_per_mt",
    "元/吨",
    "美元/吨",
}
_NON_PRICE_INDICATOR_TOKENS = (
    "contract",
    "inventory",
    "open interest",
    "position",
    "production",
    "stock",
    "volume",
    "产量",
    "库存",
    "持仓",
    "成交量",
)

# Daily-series routing for the current accepted label sources (seven-product
# label registry v4). CCF stayed soft-removed: the legacy industry/ccf_spot
# selectors below remain for point-in-time audits but are no longer the only
# suppliers for these products.
#   PX/PTA -> czce_pta_px official daily settlement (futures_daily_bars)
#   MEG    -> sunsirs_public_commodity_assessment (intraday_price_observations)
#   POY/DTY -> tnc_polyester_history (market_observations)
_LABEL_INTRADAY_INSTRUMENTS = {"MEG": "MEG"}
QUOTE_TYPE_LABELS = {
    "daily_average": "授权现货日均价",
    "main_continuous_settlement": "交易所主力合约日结算价",
    "public_spot_assessment": "公开现货评估价",
    "public_recent_average": "公开近期均价",
}


def _finite_positive(value: object) -> bool:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return number > 0 and math.isfinite(number)


def _accepted_polyester_unit(unit: object) -> bool:
    return str(unit or "").strip().lower().replace(" ", "_") in _POLYESTER_PRICE_UNITS


def _current_label_observation(product: str, *, end: str, as_of_iso: str) -> dict[str, Any] | None:
    """Latest observation from the product's current accepted daily label source.

    Reads the table where each accepted source actually lands (the same routing
    the seven-product forecast contract uses). Falls back to ``None`` when the
    registry has no current label for the product, so honest missing reporting
    stays possible.
    """
    definition = LABEL_REGISTRY.get(product.lower())
    if definition is None:
        return None
    if product in {"PX", "PTA"}:
        rows = list_futures_daily_bars(
            source_id=definition.source_id,
            product=product,
            end=end,
            as_of_time=as_of_iso,
            limit=400,
        )
        main_rows = [
            row
            for row in rows
            if (row.get("is_main") or row.get("contract_role") == "main_continuous")
            and _accepted_polyester_unit(row.get("unit"))
        ]
        if not main_rows:
            return None
        latest_day = max(str(row.get("trade_date") or "") for row in main_rows)
        same_day = [row for row in main_rows if str(row.get("trade_date")) == latest_day]
        row = max(same_day, key=lambda item: 1 if item.get("is_main") else 0)
        value = row.get("settle") if _finite_positive(row.get("settle")) else row.get("close")
        if not _finite_positive(value):
            return None
        return {
            "observation_id": row.get("bar_id"),
            "product": product,
            "metric": definition.metric,
            "value": value,
            "unit": str(row.get("unit") or definition.unit),
            "observed_at": row.get("trade_date"),
            "source_id": row.get("source_id"),
            "evidence_url": row.get("source_url", ""),
            "source_kind": "official_futures_settlement",
            "quote_type": "main_continuous_settlement",
        }
    instrument = _LABEL_INTRADAY_INSTRUMENTS.get(product)
    if instrument is not None:
        rows = [
            row
            for row in list_intraday_price_observations(
                instrument=instrument, end=end, as_of_time=as_of_iso, limit=1000
            )
            if row.get("source_id") == definition.source_id
            and row.get("last") is not None
            and _accepted_polyester_unit(row.get("unit"))
        ]
        if not rows:
            return None
        row = max(rows, key=lambda item: str(item.get("observed_at") or ""))
        return {
            "observation_id": row.get("observation_id"),
            "product": product,
            "metric": definition.metric,
            "value": row.get("last"),
            "unit": str(row.get("unit") or definition.unit),
            "observed_at": row.get("observed_at"),
            "source_id": row.get("source_id"),
            "evidence_url": row.get("source_url", ""),
            "source_kind": "public_spot_assessment",
            "quote_type": "public_spot_assessment",
        }
    rows = [
        row
        for row in list_market_observations(
            source_id=definition.source_id,
            product=product.lower(),
            end=end,
            as_of_time=as_of_iso,
            limit=500,
        )
        if row.get("value") is not None and _accepted_polyester_unit(row.get("unit"))
    ]
    if not rows:
        return None
    row = rows[0]
    return {
        "observation_id": row.get("observation_id"),
        "product": product,
        "metric": row.get("indicator") or definition.metric,
        "value": row.get("value"),
        "unit": str(row.get("unit") or definition.unit),
        "observed_at": row.get("observed_at"),
        "source_id": row.get("source_id"),
        "evidence_url": row.get("evidence_url", ""),
        "source_kind": "public_recent_average",
        "quote_type": "public_recent_average",
    }


def build_full_chain_summary(*, as_of_time: str | None = None) -> dict[str, object]:
    """Build one read-only, temporally consistent view from real observations."""
    as_of = _parse_as_of(as_of_time)
    as_of_iso, as_of_date = as_of.isoformat(), as_of.date().isoformat()
    market_rows = list_market_observations(end=as_of_date, as_of_time=as_of_iso, limit=500)
    industry_rows = list_industry_observations(end=as_of_date, as_of_time=as_of_iso, limit=500)
    authorized_price_rows = list_forecast_price_points(
        dataset_type="ccf_spot", end=as_of_date, as_of_time=as_of_iso, limit=5000
    )

    selected: dict[str, dict[str, Any]] = {}
    crude = [
        row
        for row in market_rows
        if _is_price_slot_observation(row, product="CRUDE")
        and (
            _source_contract(str(row.get("source_id", "")), product="CRUDE")["formal_eligible"]
            # The crude slot follows the frozen label series. After
            # seven-product-labels.v5 that series is the public futures proxy,
            # which governance keeps flagged as reference-only; the row still
            # carries formal_eligible=false and its usage limit downstream.
            or str(row.get("source_id", "")) == LABEL_REGISTRY["crude"].source_id
        )
    ]
    if crude:
        selected["CRUDE"] = max(crude, key=lambda row: str(row.get("observed_at", "")))
    for product in FULL_CHAIN_PRODUCTS:
        candidates: list[dict[str, Any]] = []
        label_row = _current_label_observation(product, end=as_of_date, as_of_iso=as_of_iso)
        if label_row is not None and _source_contract(str(label_row.get("source_id", "")), product=product)[
            "formal_eligible"
        ]:
            candidates.append(label_row)
        rows = [
            row
            for row in industry_rows
            if _is_price_slot_observation(row, product=product)
            and _source_contract(str(row.get("source_id", "")), product=product)["formal_eligible"]
        ]
        if rows:
            candidates.append(max(rows, key=lambda row: str(row.get("observed_at", ""))))
        authorized = [
            row
            for row in authorized_price_rows
            if is_ccf_source(row.get("source_id"))
            and _source_contract(str(row.get("source_id", "")), product=product)["formal_eligible"]
            and _is_price_slot_observation(row, product=product, authorized=True)
        ]
        if authorized:
            latest = max(authorized, key=lambda row: str(row.get("observed_at", "")))
            raw = latest.get("raw") if isinstance(latest.get("raw"), dict) else {}
            candidates.append(
                {
                    "observation_id": latest.get("point_id"),
                    "product": product,
                    "metric": "authorized_spot_daily_average",
                    "value": latest.get("price"),
                    "unit": latest.get("unit"),
                    "observed_at": latest.get("observed_at"),
                    "source_id": latest.get("source_id"),
                    "evidence_url": raw.get("source_url", ""),
                    "source_kind": "authorized_spot_observation",
                }
            )
        if candidates:
            selected[product] = max(candidates, key=lambda row: str(row.get("observed_at") or ""))

    available = sorted(selected)
    missing = sorted(set(FULL_CHAIN_PRODUCTS) - set(selected))
    stale = sorted(
        product
        for product, row in selected.items()
        if (as_of - _parse_as_of(str(row.get("observed_at")))).days > (7 if product == "CRUDE" else 14)
    )
    identity = [f"{product}:{row.get('observation_id')}" for product, row in sorted(selected.items())]
    snapshot_id = (
        "full-chain-"
        + hashlib.sha256(json.dumps([as_of_iso, identity], ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    )
    summary = [
        {
            "product": product,
            "metric": row.get("metric") or row.get("indicator"),
            "value": row.get("value"),
            "unit": row.get("unit"),
            "observed_at": row.get("observed_at"),
            "source_id": row.get("source_id"),
            "evidence_url": row.get("evidence_url"),
            "source_kind": row.get("source_kind", "spot_or_industry_observation"),
            **_source_contract(str(row.get("source_id", "")), product=product),
            "price_type": _price_type(product, row),
            "quote_type": row.get("quote_type")
            or ("daily_average" if row.get("source_kind") == "authorized_spot_observation" else "spot_or_observation"),
        }
        for product, row in sorted(selected.items())
    ]
    status = "data_not_ready" if missing else "partial" if stale else "ready"
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of_time": as_of_iso,
        "data_snapshot_id": snapshot_id,
        "status": status,
        "summary": summary,
        "coverage": {
            "required_products": list(FULL_CHAIN_PRODUCTS),
            "available_products": available,
            "missing_products": missing,
            "stale_products": stale,
        },
        "oil": next((item for item in summary if item["product"] == "CRUDE"), {}),
        "transmission": {"products": [item for item in summary if item["product"] in {"PX", "PTA", "MEG"}]},
        "poy_dty_gate": {
            "qualified": status == "ready",
            "products": [item for item in summary if item["product"] in {"POY", "DTY"}],
            "reason": "all_required_products_fresh" if status == "ready" else "missing_or_stale_required_products",
        },
    }


def assess_prediction_snapshot(snapshot: dict[str, Any], *, target: str) -> dict[str, object]:
    """Return formalization eligibility without creating or mutating any record."""
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    market_rows = payload.get("market_observations", []) if isinstance(payload, dict) else []
    industry_rows = payload.get("industry_observations", []) if isinstance(payload, dict) else []
    authorized_rows = payload.get("authorized_price_observations", []) if isinstance(payload, dict) else []
    target_upper = target.upper()
    required = set(FULL_CHAIN_PRODUCTS if ("POY" in target_upper or "DTY" in target_upper) else ("CRUDE",))
    formal_candidates: list[tuple[str, dict[str, Any], bool]] = []
    for row in market_rows:
        if (
            "CRUDE" in required
            and _is_price_slot_observation(row, product="CRUDE")
            and _source_contract(str(row.get("source_id", "")), product="CRUDE")["formal_eligible"]
        ):
            formal_candidates.append(("CRUDE", row, False))
    for row in industry_rows:
        product = str(row.get("product", "")).upper()
        if (
            product in required
            and _is_price_slot_observation(row, product=product)
            and _source_contract(str(row.get("source_id", "")), product=product)["formal_eligible"]
        ):
            formal_candidates.append((product, row, False))
    for row in authorized_rows:
        product = str(row.get("product", "")).upper()
        if (
            product in required
            and _is_price_slot_observation(row, product=product, authorized=True)
            and _source_contract(str(row.get("source_id", "")), product=product)["formal_eligible"]
        ):
            formal_candidates.append((product, row, True))

    timestamp_ineligible: list[dict[str, str]] = []
    eligible_candidates: list[tuple[str, dict[str, Any], bool]] = []
    for product, row, authorized in formal_candidates:
        failure = validate_observed_at_syntax(row.get("observed_at"))
        if failure is not None:
            timestamp_ineligible.append(
                {
                    "field": "observed_at",
                    "product": product,
                    "source_id": str(row.get("source_id") or ""),
                    "failure": failure,
                }
            )
            continue
        eligible_candidates.append((product, row, authorized))

    available = {product for product, _, _ in eligible_candidates}
    missing = sorted(required - available)
    metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
    as_of_value = metadata.get("as_of_time") or snapshot.get("created_at") or ""
    as_of_failure = validate_observed_at_syntax(as_of_value) if as_of_value else None
    if as_of_failure is not None:
        timestamp_ineligible.append(
            {
                "field": "as_of_time",
                "product": "",
                "source_id": "snapshot",
                "failure": as_of_failure,
            }
        )
    if timestamp_ineligible:
        return {
            "qualified": False,
            "required_products": sorted(required),
            "available_products": sorted(available),
            "missing_products": missing,
            "stale_products": [],
            "reason": "snapshot_timestamp_ineligible",
            "timestamp_ineligible": timestamp_ineligible,
        }

    as_of = _parse_as_of(str(as_of_value))
    latest_by_product: dict[str, datetime] = {}
    normalized_authorized = [
        {
            "product": row.get("product"),
            "metric": "spot_quote",
            "value": row.get("price"),
            "unit": row.get("unit"),
            "observed_at": row.get("observed_at"),
        }
        for _, row, authorized in eligible_candidates
        if authorized
    ]
    eligible_rows = [row for _, row, authorized in eligible_candidates if not authorized]
    for row in [*eligible_rows, *normalized_authorized]:
        product = (
            "CRUDE" if str(row.get("product", "")).lower() == "crude_oil" else str(row.get("product", "")).upper()
        )
        observed = _parse_as_of(str(row.get("observed_at") or ""))
        latest_by_product[product] = max(observed, latest_by_product.get(product, observed))
    stale = sorted(
        product
        for product, observed in latest_by_product.items()
        if (as_of - observed).days > (7 if product == "CRUDE" else 14)
    )
    return {
        "qualified": not missing and not stale,
        "required_products": sorted(required),
        "available_products": sorted(available),
        "missing_products": missing,
        "stale_products": stale,
        "reason": "qualified" if not missing and not stale else "snapshot_missing_or_stale_required_products",
    }


def _source_contract(source_id: str, *, product: str) -> dict[str, object]:
    source = get_source(source_id)
    if source is None:
        return {
            "evidence_tier": "unknown",
            "formal_eligible": False,
            "usage_limits": "unknown_source_not_eligible_for_formal_judgment",
        }
    reference_only = source_id in {"yahoo_futures_daily_proxy", "akshare_prototype"} or "prototype" in source.category
    tier = str(source.tier.value if hasattr(source.tier, "value") else source.tier)
    source_role_eligible = source.current_formal_eligible and source.operational_status == "active"
    product_role = source.product_roles.get(product.lower(), source.product_roles.get(product.upper(), ""))
    product_role_eligible = product_role not in {"context_proxy_not_px", "historical_context"}
    formal_eligible = tier in {"A", "B"} and not reference_only and source_role_eligible and product_role_eligible
    if not source_role_eligible:
        usage_limits = "historical_context_not_current_formal_eligible"
    elif not product_role_eligible:
        usage_limits = f"product_role_not_formal:{product_role}"
    elif reference_only:
        usage_limits = "public_reference_only_not_exchange_certified_or_formal_prediction_evidence"
    else:
        usage_limits = source.license_note
    return {
        "evidence_tier": tier,
        "formal_eligible": formal_eligible,
        "usage_limits": usage_limits,
        "data_role": source.data_role,
        "product_role": product_role,
    }


def _is_price_slot_observation(row: dict[str, Any], *, product: str, authorized: bool = False) -> bool:
    """Reject observations whose product, metric and unit do not describe a price.

    Source tier alone is insufficient: CFTC positioning is A-tier and uses the
    ``crude_oil`` product, but its values are contract counts, not oil prices.
    """
    canonical_product = str(product or "").upper()
    if canonical_product not in FULL_CHAIN_PRODUCTS:
        return False
    expected_product = "crude_oil" if canonical_product == "CRUDE" else canonical_product
    actual_product = str(row.get("product") or "")
    if canonical_product == "CRUDE":
        if actual_product.lower() != expected_product:
            return False
    elif actual_product.upper() != expected_product:
        return False

    value_field = "price" if authorized else "value"
    if row.get(value_field) is None:
        return False
    unit = str(row.get("unit") or "").strip().lower().replace(" ", "_")
    allowed_units = _CRUDE_PRICE_UNITS if canonical_product == "CRUDE" else _POLYESTER_PRICE_UNITS
    if unit not in allowed_units:
        return False

    if authorized:
        actual_product = str(row.get("product") or "").strip().upper()
        product_matches = (
            actual_product == "BRENT" if canonical_product == "CRUDE" else actual_product == canonical_product
        )
        return (
            product_matches
            and is_ccf_source(row.get("source_id"))
            and str(row.get("dataset_type") or "").lower() == "ccf_spot"
            and str(row.get("quote_type") or "").lower() in {"daily_average", "报价", "成交价"}
        )
    if canonical_product != "CRUDE" and str(row.get("metric") or "").lower() != "spot_quote":
        return False
    indicator = str(row.get("indicator") or row.get("metric") or "").lower()
    return not any(token in indicator for token in _NON_PRICE_INDICATOR_TOKENS)


def _price_type(product: str, row: dict[str, Any]) -> str:
    if row.get("source_kind") == "authorized_spot_observation":
        return "authorized_spot_assessment"
    if row.get("source_kind") == "official_futures_settlement":
        return "futures_settlement_proxy"
    if row.get("source_kind") in {"public_spot_assessment", "public_recent_average"}:
        return "public_spot_assessment"
    indicator = str(row.get("indicator") or row.get("metric") or "").lower()
    if "futures" in indicator:
        return "futures_proxy"
    return "public_spot_or_official_observation" if product == "CRUDE" else "industry_spot_assessment"


def _parse_as_of(value: str | None) -> datetime:
    if not value:
        # Read endpoints invoked during one UI refresh must resolve to the same
        # immutable snapshot even when the refresh crosses a minute boundary.
        # A short monotonic lease is process-local and affects only implicit
        # live reads; explicit point-in-time audit requests remain unchanged.
        global _DEFAULT_READ_AS_OF_EXPIRES_AT, _DEFAULT_READ_AS_OF_VALUE
        monotonic_now = time.monotonic()
        with _DEFAULT_READ_AS_OF_LOCK:
            if (
                _DEFAULT_READ_AS_OF_VALUE is not None
                and monotonic_now < _DEFAULT_READ_AS_OF_EXPIRES_AT
            ):
                return _DEFAULT_READ_AS_OF_VALUE
            # Use the next minute boundary plus the lease duration. A lease
            # created immediately before the boundary must still include
            # records written during the remainder of that same lease.
            resolved = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(
                minutes=1,
                seconds=DEFAULT_READ_AS_OF_STABILITY_SECONDS,
            )
            _DEFAULT_READ_AS_OF_VALUE = resolved
            _DEFAULT_READ_AS_OF_EXPIRES_AT = monotonic_now + DEFAULT_READ_AS_OF_STABILITY_SECONDS
            return resolved
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def resolve_read_as_of(value: str | None = None) -> str:
    """Resolve an explicit or leased live-read cutoff to a canonical timestamp."""
    return _parse_as_of(value).isoformat()


def build_factor_scores(*, as_of_time: str | None = None) -> list[FactorScore]:
    from .current_price_evidence import merge_current_price_rows
    as_of_iso, as_of_date = _as_of_bounds(as_of_time)
    market_rows = list_market_observations(end=as_of_date, as_of_time=as_of_iso, limit=300)
    industry_rows = merge_current_price_rows(
        list_industry_observations(end=as_of_date, as_of_time=as_of_iso, limit=300),
        as_of=_parse_as_of(as_of_iso),
    )
    event_rows = deduplicate_by_aliases(
        list_event_observations(end=as_of_iso, as_of_time=as_of_iso, limit=1000),
        event_aliases,
    )[:100]
    factors = [
        _market_factor(
            rows=market_rows,
            name="原油同口径价格",
            symbol="CRUDE",
            keywords=("WTI", "Brent", "Crude Oil"),
            route="原油 -> 石脑油 -> PX -> PTA -> POY/DTY",
            as_of_time=as_of_iso,
        ),
        _market_factor(
            rows=market_rows,
            name="美元与利率",
            symbol="USD/RATES",
            keywords=("Trade Weighted U.S. Dollar Index", "Dollar Index", "Treasury", "DGS10", "DTWEXBGS"),
            route="美元/利率 -> 商品风险偏好 -> 进口成本",
            inverse=True,
            as_of_time=as_of_iso,
        ),
        _industry_factor(
            rows=industry_rows,
            name="PX/PTA 同口径价格",
            symbol="PX/PTA",
            products=("PX", "PTA"),
            route="PX -> PTA -> 聚酯原料成本",
            as_of_time=as_of_iso,
        ),
        _industry_factor(
            rows=industry_rows,
            name="MEG 同口径价格",
            symbol="MEG",
            products=("MEG", "EG", "乙二醇"),
            route="MEG -> 聚酯原料成本",
            inventory_offsets=False,
            as_of_time=as_of_iso,
        ),
        _industry_factor(
            rows=industry_rows,
            name="POY/DTY 同口径报价",
            symbol="POY/DTY",
            products=("POY", "DTY"),
            route="聚酯长丝报价 -> 成本压力复核",
            allowed_metrics=("spot_quote",),
            as_of_time=as_of_iso,
        ),
        _event_factor(rows=event_rows, as_of_time=as_of_iso),
    ]
    active = [factor for factor in factors if "等待" not in factor.reason]
    if active:
        return active
    return [
        FactorScore(
            name="真实观测数据",
            symbol="DATA",
            direction="中性",
            change="待导入",
            strength="弱",
            contribution=0,
            route="公开源/手工导入 -> 数据快照 -> 预测账本",
            reason="尚未导入足够真实观测；系统保持低置信，不使用模拟行情填补。",
        )
    ]


def build_overview(*, as_of_time: str | None = None) -> dict[str, object]:
    as_of_iso, as_of_date = _as_of_bounds(as_of_time)
    factors = build_factor_scores(as_of_time=as_of_iso)
    market_rows = list_market_observations(end=as_of_date, as_of_time=as_of_iso, limit=300)
    industry_rows = list_industry_observations(end=as_of_date, as_of_time=as_of_iso, limit=300)
    event_rows = list_event_observations(end=as_of_iso, as_of_time=as_of_iso, limit=100)
    overview = compute_cost_pressure_index(factors)
    source_ids = {row["source_id"] for row in [*market_rows, *industry_rows, *event_rows]}
    usable_industry_count = sum(1 for row in industry_rows if row.get("value") is not None)
    overview["confidence"] = _confidence(
        source_ids=source_ids,
        market_count=len(market_rows),
        industry_count=usable_industry_count,
        event_count=len(event_rows),
    )
    overview["trend_1d"] = _trend_label(factors, scale=0.35)
    overview["trend_7d"] = _trend_label(factors, scale=0.7)
    observation_count = len(market_rows) + len(industry_rows) + len(event_rows)
    overview["trend_30d"] = "待月度贸易流确认" if observation_count < 8 else _trend_label(factors, scale=1.0)
    overview["updated_at"] = _latest_timestamp([*market_rows, *industry_rows, *event_rows])
    overview["data_coverage"] = {
        "market_observations": len(market_rows),
        "industry_observations": len(industry_rows),
        "events": len(event_rows),
        "source_ids": sorted(source_ids),
    }
    full_chain = build_full_chain_summary(as_of_time=as_of_iso)
    overview["as_of_time"] = full_chain["as_of_time"]
    overview["data_snapshot_id"] = full_chain["data_snapshot_id"]
    overview["formal_conclusion_gate"] = {
        "qualified": False,
        "reasons": ["rag_adopted_evidence_required"],
        "required_snapshot_id": full_chain["data_snapshot_id"],
        "adopted_evidence_ids": [],
        "evidence_mapping": {},
        "direction_derivation": {
            "status": "insufficient_evidence",
            "direction": "",
            "method": "requires_rag_adopted_reviewed_evidence",
            "trace": [],
        },
    }
    overview["coverage_confidence"] = overview["confidence"]
    overview["retrieval_confidence"] = None
    overview["conclusion_confidence"] = 0.0
    overview["confidence_semantics"] = {
        "coverage_confidence": "数据来源数量、观测覆盖与缺失情况形成的覆盖置信度。",
        "retrieval_confidence": "不适用于总览；由本轮RAG检索单独提供。",
        "conclusion_confidence": "正式结论门禁未通过时固定为0。",
    }
    return overview


def build_morning_brief(*, as_of_time: str | None = None) -> list[MorningBriefItem]:
    as_of_iso, _ = _as_of_bounds(as_of_time)
    generated_at = datetime.now(UTC).isoformat()
    factors = build_factor_scores(as_of_time=as_of_iso)
    overview = build_overview(as_of_time=as_of_iso)
    items = []
    for factor in sorted(factors, key=lambda item: abs(item.contribution), reverse=True)[:3]:
        priority = "high" if abs(factor.contribution) >= 8 else "medium" if abs(factor.contribution) >= 3 else "low"
        items.append(
            MorningBriefItem(
                title=factor.name,
                body=(f"{factor.direction}，贡献 {factor.contribution:+d}。{factor.reason} 传导路径：{factor.route}。"),
                priority=priority,
                linked_factors=[factor.symbol],
                generated_at=generated_at,
                as_of_time=as_of_iso,
                observed_at=factor.observed_at,
                source_id=factor.source_id,
                data_status=factor.data_status,
            )
        )
    coverage = overview.get("data_coverage", {})
    if isinstance(coverage, dict):
        items.append(
            MorningBriefItem(
                title="数据覆盖状态",
                body=(
                    f"公开观测 {coverage.get('market_observations', 0)} 条，行业手工观测 "
                    f"{coverage.get('industry_observations', 0)} 条，事件 {coverage.get('events', 0)} 条。"
                    "缺失项会降低置信度，不会由模拟数据补齐。"
                ),
                priority="medium" if coverage.get("market_observations", 0) else "low",
                linked_factors=["DATA"],
                generated_at=generated_at,
                as_of_time=as_of_iso,
                data_status="ready" if coverage.get("market_observations", 0) else "missing",
            )
        )
    return items


def build_event_impacts() -> list[EventImpact]:
    as_of_iso, _ = _current_as_of_bounds()

    def public_event_aliases(row: dict[str, Any]) -> set[str]:
        aliases = event_aliases(row)
        title = re.sub(r"[\W_]+", "", str(row.get("title") or "").casefold())
        event_type = str(row.get("event_type") or "").casefold()
        if title:
            aliases.add(f"public:{title}|type:{event_type}")
        return aliases

    event_rows = deduplicate_by_aliases(
        list_event_observations(end=as_of_iso, as_of_time=as_of_iso, limit=1000),
        public_event_aliases,
    )[:100]
    impacts = []
    for row in event_rows:
        products = row["affected_products"] or ["upstream_cost_pressure"]
        direction = row["direction"] or "中性"
        impacts.append(
            EventImpact(
                event_id=row["event_record_id"],
                occurred_at=row.get("occurred_at") or row.get("created_at") or "",
                title=row["title"],
                event_type=row["event_type"],
                nature=_event_nature(direction, row["impact_strength"]),
                horizon="人工/公开事件，需结合日度数据复核",
                evidence_level=row["evidence_level"],
                confidence=0.78 if row["evidence_level"] == "A" else 0.62 if row["evidence_level"] == "B" else 0.46,
                affected_products=products,
                impact_chain=_event_chain(products, direction),
                judgement=row["summary"] or "事件已入库，等待与价格、库存、开工率观测交叉验证。",
                stakeholders=["公开事件源", "聚酯产业链参与者", "个人研究者"],
                beneficiaries=["需结合方向和持仓判断"],
                harmed=["需结合方向和持仓判断"],
                counter_evidence=["价格未跟随", "库存/开工率反向变化", "高等级官方数据未确认"],
            )
        )
    return impacts


def build_prediction_reviews(
    *, force: bool = False, records: list[dict[str, Any]] | None = None
) -> list[PredictionReview]:
    source_records = records if records is not None else list_prediction_ledger_records(limit=100)
    records = [record for record in source_records if is_legacy_directional_prediction_record(record)]
    if not records:
        return []
    needs_current_index = force or any(_prediction_due(record) for record in records)
    current_index = int(build_overview()["cost_pressure_index"]) if needs_current_index else 0
    as_of_iso, as_of_date = _current_as_of_bounds()
    market_rows = list_market_observations(end=as_of_date, as_of_time=as_of_iso, limit=500)
    industry_rows = list_industry_observations(end=as_of_date, as_of_time=as_of_iso, limit=500)
    reviews = []
    for record in records:
        contract = enrich_prediction_record(record)
        expected = record["direction"]
        if record.get("review_status") != "reviewed" and not force and not _prediction_due(record):
            reviews.append(
                PredictionReview(
                    prediction_id=record["prediction_id"],
                    horizon=record["horizon"],
                    expected_direction=expected,
                    actual_index=current_index,
                    deviation=None,
                    verdict="未到期",
                    learning=_prediction_learning(record),
                    weight_adjustments=[
                        "到期后用实际成本压力指数、POY/DTY全国报价和反证数据复盘。",
                        "缺失 PTA/MEG/PX 手工观测时，不上调高置信权重。",
                    ],
                    due_at=contract["due_at"],
                    review_status="pending_due",
                    scoreability="not_due",
                    scored_count=0,
                    total_count=len(_prediction_target_series(record)),
                    coverage=0,
                    leakage_check="not_run",
                )
            )
            continue
        verdict, learning, adjustments, deviation = _prediction_price_review(record, market_rows, industry_rows)
        targets = _prediction_target_series(record)
        scored_count = len(targets) if verdict in {"方向正确", "区间命中", "方向偏弱", "方向偏强"} else 0
        scoreability = "scored" if scored_count else "waiting_for_data"
        reviews.append(
            PredictionReview(
                prediction_id=record["prediction_id"],
                horizon=record["horizon"],
                expected_direction=expected,
                actual_index=current_index,
                deviation=round(abs(deviation), 2) if deviation is not None else None,
                verdict=verdict,
                learning=learning,
                weight_adjustments=adjustments,
                due_at=contract["due_at"],
                review_status="reviewed" if scored_count else "due_pending_data",
                scoreability=scoreability,
                scored_count=scored_count,
                total_count=len(targets),
                coverage=round(scored_count / len(targets), 4) if targets else 0,
                leakage_check="passed" if scored_count else "not_run",
            )
        )
    return reviews


def _current_as_of_bounds() -> tuple[str, str]:
    return _as_of_bounds(None)


def _as_of_bounds(value: str | None) -> tuple[str, str]:
    as_of = _parse_as_of(value)
    return as_of.isoformat(), as_of.date().isoformat()


def _market_factor(
    *,
    rows: list[dict[str, Any]],
    name: str,
    symbol: str,
    keywords: tuple[str, ...],
    route: str,
    inverse: bool = False,
    as_of_time: str,
) -> FactorScore:
    matches = _matching_rows(rows, keywords)
    current, previous = _latest_pair_same_series(matches)
    if current is None:
        return _waiting_factor(name=name, symbol=symbol, route=route, reason="等待公开 API 或 CSV 观测入库。")
    latest_value = current.get("value")
    previous_value = previous.get("value") if previous else None
    delta = _delta(latest_value, previous_value)
    stale = _factor_is_stale(current.get("observed_at"), as_of_time=as_of_time, max_age_days=7)
    contribution = 0 if stale else _contribution(delta, base=8, inverse=inverse)
    indicator = str(current.get("indicator", symbol))
    return FactorScore(
        name=name,
        symbol=symbol,
        direction="中性" if stale else _direction(contribution),
        change=_change_label(delta, latest_value, current.get("unit")),
        strength="弱" if stale else _strength(contribution),
        contribution=contribution,
        route=route,
        reason=(
            f"{indicator} 最新观测为 {latest_value} {current.get('unit', '')}，来源 {current.get('source_id')}。"
            + (" 该观测已陈旧，本轮降级为中性且不计入方向贡献。" if stale else "")
        ),
        observed_at=str(current.get("observed_at") or ""),
        source_id=str(current.get("source_id") or ""),
        metric=indicator,
        data_status="stale" if stale else "ready",
    )


def _industry_factor(
    *,
    rows: list[dict[str, Any]],
    name: str,
    symbol: str,
    products: tuple[str, ...],
    route: str,
    inventory_offsets: bool = False,
    allowed_metrics: tuple[str, ...] | None = None,
    as_of_time: str,
) -> FactorScore:
    matches = [row for row in rows if str(row.get("product", "")).upper() in {item.upper() for item in products}]
    if allowed_metrics is not None:
        normalized_metrics = {item.lower() for item in allowed_metrics}
        matches = [row for row in matches if str(row.get("metric") or "").lower() in normalized_metrics]
    current, previous = _latest_pair_same_series(matches)
    if current is None:
        return _waiting_factor(name=name, symbol=symbol, route=route, reason="等待合格来源的同口径价格观测。")
    delta = _delta(current.get("value"), previous.get("value") if previous else None)
    inverse = inventory_offsets and "库存" in str(current.get("metric", ""))
    stale = _factor_is_stale(current.get("observed_at"), as_of_time=as_of_time, max_age_days=14)
    contribution = 0 if stale else _contribution(delta, base=7, inverse=inverse)
    metric = str(current.get("metric") or "")
    return FactorScore(
        name=name,
        symbol=symbol,
        direction="中性" if stale else _direction(contribution),
        change=_change_label(delta, current.get("value"), current.get("unit")),
        strength="弱" if stale else _strength(contribution),
        contribution=contribution,
        route=route,
        reason=(
            f"{current.get('product')} {metric} 最新观测为 "
            f"{current.get('value')} {current.get('unit', '')}，来源 {current.get('source_id')}。"
            + (" 该观测已陈旧，本轮降级为中性且不计入方向贡献。" if stale else "")
        ),
        observed_at=str(current.get("observed_at") or ""),
        source_id=str(current.get("source_id") or ""),
        metric=metric,
        data_status="stale" if stale else "ready",
    )


def _event_factor(rows: list[dict[str, Any]], *, as_of_time: str | None = None) -> FactorScore:
    if not rows:
        return _waiting_factor(
            name="A/B级事件风险",
            symbol="EVENTS",
            route="政策/地缘/航运事件 -> 原油风险溢价 -> 聚酯成本压力",
            reason="等待官方新闻、公告或手工事件入库。",
        )

    scored_rows = [(row, _event_contribution(row)) for row in rows[:20]]
    net_contribution = _clamp(sum(contribution for _, contribution in scored_rows), -18, 18)
    top_rows = sorted(scored_rows, key=lambda item: abs(item[1]), reverse=True)[:2]
    titles = "；".join(str(row.get("title", "事件")) for row, _ in top_rows)
    source_count = len({str(row.get("source_id", "")) for row, _ in scored_rows if row.get("source_id")})
    latest_row = max(
        rows,
        key=lambda row: str(row.get("occurred_at") or row.get("created_at") or ""),
    )
    latest_at = str(latest_row.get("occurred_at") or latest_row.get("created_at") or "")
    effective_as_of = _parse_as_of(as_of_time).isoformat()
    stale = _factor_is_stale(latest_at, as_of_time=effective_as_of, max_age_days=7)
    effective_contribution = 0 if stale else net_contribution
    return FactorScore(
        name="A/B级事件风险",
        symbol="EVENTS",
        direction="中性" if stale else _direction(net_contribution),
        change=f"{len(rows)} 条事件，净贡献 {net_contribution:+d}",
        strength="弱" if stale else _strength(net_contribution),
        contribution=effective_contribution,
        route="政策/地缘/航运事件 -> 原油风险溢价 -> 聚酯成本压力",
        reason=(
            f"已入库 {len(rows)} 条事件，覆盖 {source_count} 个来源；主要事件：{titles}。"
            + (" 最新事件已陈旧，本轮降级为中性且不计入方向贡献。" if stale else "")
        ),
        observed_at=latest_at,
        source_id=str(latest_row.get("source_id") or ""),
        metric="event_risk",
        data_status="stale" if stale else "ready",
    )


def _matching_rows(rows: list[dict[str, Any]], keywords: tuple[str, ...]) -> list[dict[str, Any]]:
    normalized = tuple(_normalize_match_text(keyword) for keyword in keywords)
    return [row for row in rows if any(keyword in _row_search_text(row) for keyword in normalized)]


def _row_search_text(row: dict[str, Any]) -> str:
    fields = [
        row.get("indicator", ""),
        row.get("product", ""),
        row.get("metric", ""),
    ]
    raw = row.get("raw")
    if isinstance(raw, dict):
        fields.extend(
            [
                raw.get("series_id", ""),
                raw.get("series-description", ""),
                raw.get("instrument", ""),
                raw.get("dataset", ""),
                raw.get("raw_field_name", ""),
            ]
        )
    return _normalize_match_text(" ".join(str(field) for field in fields))


def _normalize_match_text(value: object) -> str:
    return str(value).replace("_", " ").replace("-", " ").lower()


def _latest_pair_same_series(rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("value") is None:
            continue
        groups.setdefault(_series_key(row), []).append(row)
    candidates = []
    for group in groups.values():
        current, previous = _latest_pair(group)
        if current is not None:
            candidates.append((current, previous))
    if not candidates:
        return None, None
    candidates.sort(
        key=lambda item: (
            str(item[0].get("observed_at", "")),
            str(item[0].get("created_at", "")),
            item[1] is not None,
        ),
        reverse=True,
    )
    return candidates[0]


def _latest_pair(rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    with_values = [row for row in rows if row.get("value") is not None]
    with_values.sort(key=lambda row: (str(row.get("observed_at", "")), str(row.get("created_at", ""))), reverse=True)
    if not with_values:
        return None, None
    current = with_values[0]
    previous = next((row for row in with_values[1:] if row.get("observed_at") != current.get("observed_at")), None)
    return current, previous


def _series_key(row: dict[str, Any]) -> str:
    raw = row.get("raw")
    if isinstance(raw, dict):
        for key in ("series_id", "series"):
            value = raw.get(key)
            if value:
                return f"{row.get('source_id', '')}:{value}:{row.get('unit', '')}"
    parts = [
        row.get("source_id", ""),
        row.get("indicator", ""),
        row.get("product", ""),
        row.get("metric", ""),
        row.get("unit", ""),
        row.get("region", ""),
    ]
    return "|".join(str(part).strip().lower() for part in parts)


def _delta(latest: object, previous: object) -> float | None:
    if latest is None or previous is None:
        return None
    try:
        latest_float = float(latest)
        previous_float = float(previous)
    except (TypeError, ValueError):
        return None
    if previous_float == 0:
        return None
    return (latest_float - previous_float) / abs(previous_float)


def _factor_is_stale(observed_at: object, *, as_of_time: str, max_age_days: int) -> bool:
    observed_text = str(observed_at or "").strip()
    if not observed_text:
        return True
    try:
        age = _parse_as_of(as_of_time) - _parse_as_of(observed_text)
    except ValueError:
        return True
    return age.total_seconds() > max_age_days * 86_400


def _contribution(delta: float | None, *, base: int, inverse: bool = False) -> int:
    if delta is None:
        return 2 if not inverse else -2
    magnitude = min(base + round(abs(delta) * 100), 18)
    contribution = magnitude if delta > 0 else -magnitude if delta < 0 else 0
    return -contribution if inverse else contribution


def _event_contribution(row: dict[str, Any]) -> int:
    direction = _normalize_event_direction(row.get("direction"))
    if direction == "利多":
        sign = 1
    elif direction == "利空":
        sign = -1
    else:
        sign = 0
    if sign == 0:
        return 0

    strength = _parse_event_strength(row.get("impact_strength"))
    evidence_weight = {"A": 1.0, "B": 0.75, "C": 0.45, "D": 0.25}.get(str(row.get("evidence_level", "C")), 0.45)
    product_weight = 1.0 if _touches_core_chain(row.get("affected_products")) else 0.6
    return sign * round(12 * strength * evidence_weight * product_weight)


def _normalize_event_direction(value: object) -> str:
    text = str(value or "").strip().lower()
    if any(term in text for term in ("利多", "偏强", "上涨", "上行", "bullish", "positive", "up")):
        return "利多"
    if any(term in text for term in ("利空", "偏弱", "下跌", "下行", "bearish", "negative", "down")):
        return "利空"
    if "中性" in text or "neutral" in text:
        return "中性"
    return "中性"


def _parse_event_strength(value: object) -> float:
    if value is None:
        return 0.45
    text = str(value).strip().lower()
    labels = {
        "low": 0.25,
        "weak": 0.25,
        "medium": 0.50,
        "mid": 0.50,
        "high": 0.80,
        "strong": 0.80,
        "弱": 0.25,
        "中": 0.50,
        "强": 0.80,
    }
    if text in labels:
        return labels[text]
    try:
        parsed = float(text)
    except ValueError:
        return 0.45
    return max(0.0, min(parsed, 1.0))


def _touches_core_chain(products: object) -> bool:
    if isinstance(products, str):
        normalized = products.replace("，", ",").replace("|", ",").replace(";", ",")
        items = {item.strip().lower() for item in normalized.split(",") if item.strip()}
    elif isinstance(products, list):
        items = {str(item).strip().lower() for item in products}
    else:
        items = set()
    core_chain = {"crude_oil", "brent", "wti", "lpg", "naphtha", "px", "pta", "meg", "poy", "dty"}
    return bool(items & core_chain)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _direction(contribution: int) -> str:
    if contribution > 2:
        return "利多"
    if contribution < -2:
        return "利空"
    return "中性"


def _strength(contribution: int) -> str:
    magnitude = abs(contribution)
    if magnitude >= 12:
        return "强"
    if magnitude >= 6:
        return "中"
    return "弱"


def _change_label(delta: float | None, latest: object, unit: object) -> str:
    if delta is None:
        return f"最新 {latest} {unit}".strip()
    return f"{delta * 100:+.2f}%"


def _waiting_factor(*, name: str, symbol: str, route: str, reason: str) -> FactorScore:
    return FactorScore(
        name=name,
        symbol=symbol,
        direction="中性",
        change="待数据",
        strength="弱",
        contribution=0,
        route=route,
        reason=reason,
        data_status="missing",
    )


def _confidence(*, source_ids: set[str], market_count: int, industry_count: int, event_count: int) -> float:
    observation_count = market_count + industry_count + event_count
    if observation_count == 0:
        return 0.2
    score = 0.35 + min(observation_count, 20) * 0.015 + min(len(source_ids), 6) * 0.06
    cap = 0.88
    if market_count == 0:
        cap = min(cap, 0.5)
    if industry_count == 0:
        cap = min(cap, 0.68)
    if event_count == 0:
        cap = min(cap, 0.76)
    return round(min(score, cap), 2)


def _trend_label(factors: list[FactorScore], *, scale: float) -> str:
    score = round(sum(item.contribution for item in factors) * scale)
    if score >= 10:
        return "偏强"
    if score >= 3:
        return "中性偏强"
    if score <= -10:
        return "偏弱"
    if score <= -3:
        return "中性偏弱"
    return "震荡"


def _latest_timestamp(rows: list[dict[str, Any]]) -> str:
    timestamps = [
        str(row.get("created_at") or row.get("observed_at"))
        for row in rows
        if row.get("created_at") or row.get("observed_at")
    ]
    return max(timestamps) if timestamps else datetime.now(UTC).isoformat()


def _prediction_learning(record: dict[str, Any]) -> str:
    if record.get("data_snapshot_id"):
        return "预测已绑定真实数据快照。"
    return "预测尚未绑定快照，后续可信度较低。"


def _prediction_due(record: dict[str, Any]) -> bool:
    try:
        created_at = datetime.fromisoformat(str(record["created_at"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    horizon_days = HORIZON_DAYS.get(str(record.get("horizon")))
    if horizon_days is None:
        return False
    now = datetime.now(UTC)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return (now - created_at).days >= horizon_days


def _prediction_price_review(
    record: dict[str, Any],
    market_rows: list[dict[str, Any]],
    industry_rows: list[dict[str, Any]],
) -> tuple[str, str, list[str], float | None]:
    window = _prediction_review_window(record)
    if window is None:
        return (
            "未到期",
            "预测时间无法解析，暂不进入评分。",
            ["修复预测 created_at 或重新写入带快照的预测。"],
            None,
        )
    start, end, created_at = window
    targets = _prediction_target_series(record)
    if not targets:
        return (
            "待目标品种",
            "预测没有明确 POY/DTY/PX/PTA/MEG、Brent 或 WTI 目标，暂不使用代理品种评分。",
            ["补充明确目标品种后再复盘", "不要用默认原油代理替代未声明的产业链目标"],
            None,
        )
    scored_changes: list[float] = []
    missing_targets: list[str] = []
    for target in targets:
        rows = _target_review_rows(
            target,
            market_rows,
            industry_rows,
            start=start,
            end=end,
            prediction_created_at=created_at,
        )
        if len(rows) < 2:
            missing_targets.append(target)
            continue
        scored_changes.append(_series_change_pct(rows))
    if not scored_changes:
        target_text = "/".join(targets)
        return (
            "待后验价格",
            f"{target_text} 在 {start.isoformat()}..{end.isoformat()} 缺少至少两个可审计后验价格点，暂不评分。",
            ["补齐目标品种真实后验价格后再复盘", "不要用代理价、插值或当前价格提前评分"],
            None,
        )
    if missing_targets:
        return (
            "部分待后验价格",
            (
                f"{'/'.join(missing_targets)} 在 {start.isoformat()}..{end.isoformat()} "
                "缺少至少两个可审计后验价格点，暂不输出最终评分。"
            ),
            ["补齐所有目标品种后再给最终 verdict", "当前已评分品种只能作为 partial evidence"],
            None,
        )
    average_price_change = sum(scored_changes) / len(scored_changes)
    gap_text = f"；未评分目标：{', '.join(missing_targets)}" if missing_targets else ""
    expected = str(record.get("direction", ""))
    expects_up = any(term in expected for term in ("偏强", "利多", "上涨", "上行"))
    expects_down = any(term in expected for term in ("偏弱", "利空", "下跌", "下行"))
    if abs(average_price_change) < 1:
        return (
            "区间命中" if not expects_up and not expects_down else "方向偏强",
            f"{'/'.join(targets)} 后验均值变化 {average_price_change:+.2f}%，价格没有给出单边确认{gap_text}。",
            ["降低事件冲击持续性权重", "等待 POY/DTY、PX、PTA、MEG 现货验证"],
            average_price_change,
        )
    if (expects_up and average_price_change > 0) or (expects_down and average_price_change < 0):
        return (
            "方向正确",
            f"预测方向与 {'/'.join(targets)} 后验均值变化 {average_price_change:+.2f}% 同向{gap_text}。",
            ["保留当前方向性因子", "继续检查行业现货和库存是否确认"],
            average_price_change,
        )
    if expects_up and average_price_change < 0:
        return (
            "方向偏强",
            f"预测偏强，但 {'/'.join(targets)} 后验均值变化 {average_price_change:+.2f}%，价格形成反证{gap_text}。",
            ["下调事件风险溢价权重", "上调需求走弱、供应恢复或库存反证权重"],
            average_price_change,
        )
    if expects_down and average_price_change > 0:
        return (
            "方向偏弱",
            f"预测偏弱，但 {'/'.join(targets)} 后验均值变化 {average_price_change:+.2f}%，价格形成反证{gap_text}。",
            ["上调原油价格动量和政策/制裁事件权重", "复核供应扰动持续时间"],
            average_price_change,
        )
    return (
        "区间命中",
        f"预测未给出明确方向，{'/'.join(targets)} 后验均值变化 {average_price_change:+.2f}%{gap_text}。",
        ["后续预测需明确方向、时间窗口和反证条件"],
        average_price_change,
    )


def _prediction_review_window(record: dict[str, Any]) -> tuple[date, date, datetime] | None:
    try:
        created_at = datetime.fromisoformat(str(record["created_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    horizon_days = HORIZON_DAYS.get(str(record.get("horizon")))
    if horizon_days is None:
        return None
    start = created_at.date()
    end = (created_at + timedelta(days=horizon_days)).date()
    return start, end, created_at


def _prediction_target_series(record: dict[str, Any]) -> list[str]:
    text = f"{record.get('target', '')} {record.get('rationale', '')}".upper()
    targets = []
    for product in ("POY", "DTY", "PX", "PTA", "MEG"):
        if product in text:
            targets.append(product)
    explicit_crude_targets = False
    if "BRENT" in text:
        targets.append("Brent")
        explicit_crude_targets = True
    if "WTI" in text:
        targets.append("WTI")
        explicit_crude_targets = True
    if ("原油" in text or "CRUDE" in text) and not explicit_crude_targets:
        targets.extend(["Brent", "WTI"])
    if targets:
        return list(dict.fromkeys(targets))
    return []


def _target_review_rows(
    target: str,
    market_rows: list[dict[str, Any]],
    industry_rows: list[dict[str, Any]],
    *,
    start: date,
    end: date,
    prediction_created_at: datetime,
) -> list[dict[str, Any]]:
    prior_rows = []
    posterior_rows = []
    source_rows = market_rows if target in {"Brent", "WTI"} else industry_rows
    for row in source_rows:
        if row.get("value") is None:
            continue
        observed_at = _parse_observed_date(row.get("observed_at"))
        if observed_at is None:
            continue
        if target in {"Brent", "WTI"}:
            if _market_series_label(row) != target:
                continue
        elif str(row.get("product", "")).upper() != target.upper():
            continue
        item = {
            "observed_at": observed_at,
            "value": float(row["value"]),
            "source_id": row.get("source_id", ""),
            "created_at": row.get("created_at", ""),
        }
        if observed_at < start and _created_no_later_than(row.get("created_at"), prediction_created_at):
            prior_rows.append(item)
        elif start < observed_at <= end:
            posterior_rows.append(item)
    if not prior_rows or not posterior_rows:
        return []
    anchor = max(prior_rows, key=lambda item: (item["observed_at"], str(item["created_at"])))
    posterior_by_date = {row["observed_at"]: row for row in posterior_rows}
    terminal = posterior_by_date[max(posterior_by_date)]
    return [anchor, terminal]


def _series_change_pct(rows: list[dict[str, Any]]) -> float:
    first = rows[0]["value"]
    last = rows[-1]["value"]
    if first == 0:
        return 0.0
    return (last - first) / abs(first) * 100


def _created_no_later_than(value: object, cutoff: datetime) -> bool:
    if not value:
        return False
    try:
        created_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at <= cutoff


def _market_series_label(row: dict[str, Any]) -> str:
    raw = row.get("raw")
    if isinstance(raw, dict):
        series = str(raw.get("series_id") or raw.get("series") or "")
        if series in {"RBRTE", "DCOILBRENTEU"}:
            return "Brent"
        if series in {"RWTC", "DCOILWTICO"}:
            return "WTI"
    text = f"{row.get('indicator', '')} {row.get('product', '')}".lower()
    if "brent" in text:
        return "Brent"
    if "wti" in text or "cushing" in text:
        return "WTI"
    return ""


def _parse_observed_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _event_nature(direction: str, strength: str) -> str:
    return f"{direction or '中性'}事件，强度 {strength or '待确认'}"


def _event_chain(products: list[str], direction: str) -> list[str]:
    return [
        "公开/手工事件入库",
        f"影响对象：{', '.join(products)}",
        f"初始方向：{direction or '中性'}",
        "等待价格、库存、开工率和贸易流交叉验证",
    ]

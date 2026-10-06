from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from .models import SevenProductForecastBatch, SevenProductForecastCell
from .price_intraday import public_spot_quote_matches_instrument
from .seven_product_contract import (
    CRUDE_EIA_SPOT_LABEL_REGISTRY_VERSION,
    CRUDE_EIA_SPOT_SERIES_ID,
    CRUDE_EIA_SPOT_SOURCE_ID,
    CURRENT_FORECAST_SCHEMA_VERSION,
    CURRENT_FORMAL_CELL_COUNT,
    CURRENT_FORMAL_HORIZONS,
    CURRENT_FORMAL_TARGETS,
    CURRENT_LABEL_REGISTRY_VERSION,
    CURRENT_NAIVE_SEASONAL_LAG,
    CURRENT_NEUTRAL_BAND_POLICY_VERSION,
    CURRENT_SOURCE_GAPS,
    LABEL_REGISTRY,
)
from .seven_product_model_governance import (
    approved_champion_evidence,
    formal_runtime_decision,
    load_model_registry,
    reference_runtime_decision,
)
from .storage import (
    list_futures_daily_bars,
    list_intraday_price_observations,
    list_market_observations,
    list_source_capture_revisions,
)

MODEL_VERSION = "robust-drift-reference.v1"
CALENDAR_MODEL_VERSION = "robust-calendar-drift-reference.v2"
CALENDAR_FEATURE_VERSION = "univariate-calendar-log-price.v2"
FEATURE_VERSION = "univariate-log-return.v1"
PERSISTENCE_MODEL_VERSION = "persistence.v1"
PERSISTENCE_FEATURE_VERSION = "last-observation-level.v1"
RUNTIME_POLICY_VERSION = "seven-product-runtime-policy.v1"
MIN_TREND_POINTS = 20
LOOKBACK_POINTS = 120
MAX_EVIDENCE_POINTS = max(3, CURRENT_NAIVE_SEASONAL_LAG + 1)
MAX_ABS_DAILY_LOG_RETURN = 0.15
CONFIDENCE_HISTORY_POINTS = 60
CONFIDENCE_BASE = 0.2
CONFIDENCE_HISTORY_WEIGHT = 0.25
CONFIDENCE_SIGNAL_WEIGHT = 0.2
CONFIDENCE_CAP = 0.65
# EIA publishes the daily Brent observations in a weekly release. Ten calendar
# days matches the source policy observation SLA and prevents a newly released
# official batch from being marked stale merely because its newest row is dated
# before the publication day. The other labels retain their near-daily SLA.
FRESHNESS_DAYS = {"crude": 4, "naphtha": 4, "px": 4, "pta": 4, "meg": 4, "poy": 4, "dty": 4}
NEUTRAL_FLOOR_PCT = {
    "crude": 0.006,
    "naphtha": 0.008,
    "px": 0.008,
    "pta": 0.008,
    "meg": 0.008,
    "poy": 0.006,
    "dty": 0.006,
}
TARGET_INSTRUMENT = {
    "naphtha": "NAPHTHA",
    "px": "PX",
    "pta": "PTA",
    "meg": "MEG",
    "poy": "POY",
    "dty": "DTY",
}
TARGET_FUTURES_PRODUCT = {"px": "PX", "pta": "PTA"}


@dataclass(frozen=True, slots=True)
class PricePoint:
    observation_id: str
    observed_at: str
    visible_at: str
    value: float
    unit: str
    source_id: str
    source_url: str
    raw_sha256: str = ""
    semantic_series_id: str = ""
    contract_version: str = ""


@dataclass(frozen=True, slots=True)
class LoadedLabelSeries:
    points: tuple[PricePoint, ...]
    source_matches_label: bool
    data_gaps: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RobustProjection:
    point_forecast: float
    interval_low: float
    interval_high: float
    predicted_change_pct: float
    neutral_band_pct: float
    direction: str
    median_daily_log_return: float
    realized_volatility: float


SeriesLoader = Callable[[str, datetime], LoadedLabelSeries]


def build_seven_product_forecast(
    *,
    as_of_time: str | None = None,
    series_loader: SeriesLoader | None = None,
    model_registry: dict[str, Any] | None = None,
    runtime_model_history: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]] | None = None,
    forecast_contract: str = "observation-horizon.v1",
    candidate_builder: Callable | None = None,
    input_snapshot_sha256: str | None = None,
) -> SevenProductForecastBatch:
    if forecast_contract not in {"observation-horizon.v1", "issue-calendar.v1"}:
        raise ValueError("unknown_forecast_contract")
    as_of = _timestamp(as_of_time) if as_of_time else datetime.now(UTC)
    loader = series_loader or load_current_label_series
    registry = model_registry or load_model_registry()
    cells: list[SevenProductForecastCell] = []
    for target in CURRENT_FORMAL_TARGETS:
        loaded = loader(target, as_of)
        for horizon in CURRENT_FORMAL_HORIZONS:
            cell = _forecast_cell(
                    target=target,
                    horizon_days=horizon,
                    as_of=as_of,
                    loaded=loaded,
                    model_registry=registry,
                    settled_history=(runtime_model_history or {}).get((target, horizon), ()),
                    forecast_contract=forecast_contract,
                )
            cell = cell.model_copy(update={
                "forecast_contract": forecast_contract,
                "target_date": (as_of.astimezone(ZoneInfo("Asia/Shanghai")).date()
                                + timedelta(days=horizon)).isoformat()
                    if forecast_contract == "issue-calendar.v1" else None,
                "input_snapshot_sha256": input_snapshot_sha256,
            })
            if candidate_builder is not None:
                try:
                    candidates = candidate_builder(cell)
                    cell = cell.model_copy(update={"candidates": candidates, "candidate_status": "ready"})
                except Exception as exc:  # Candidate isolation must not suppress the main issuance.
                    cell = cell.model_copy(update={
                        "candidate_status": "degraded", "candidate_error": type(exc).__name__,
                    })
            if input_snapshot_sha256:
                cell = cell.model_copy(update={"configuration_sha256": _digest({
                    "main_configuration": cell.configuration_sha256,
                    "input_snapshot_sha256": input_snapshot_sha256,
                    "candidate_policy": "shared-input-comparisons.v1",
                })})
            cells.append(cell)
    if len(cells) != CURRENT_FORMAL_CELL_COUNT:
        raise RuntimeError("seven_product_forecast_grid_incomplete")
    snapshot_digest = _digest([cell.data_snapshot_sha256 for cell in cells])
    configuration_digest = _digest([cell.configuration_sha256 for cell in cells])
    batch_identity = _digest(
        {
            "as_of_time": as_of.isoformat(),
            "configuration_sha256": configuration_digest,
            "data_snapshot_sha256": snapshot_digest,
            "schema_version": CURRENT_FORECAST_SCHEMA_VERSION,
        }
    )
    unavailable = {"insufficient_data", "model_unavailable"}
    return SevenProductForecastBatch(
        schema_version=CURRENT_FORECAST_SCHEMA_VERSION,
        batch_id=f"seven-{batch_identity[:24]}",
        generated_at=datetime.now(UTC).isoformat(),
        as_of_time=as_of.isoformat(),
        targets=list(CURRENT_FORMAL_TARGETS),
        horizons=list(CURRENT_FORMAL_HORIZONS),
        cells=cells,
        formal_count=sum(cell.formal_status == "formal" for cell in cells),
        reference_count=sum(cell.formal_status in {"reference", "degraded", "low_confidence"} for cell in cells),
        unavailable_count=sum(cell.formal_status in unavailable for cell in cells),
        contract_complete=len({(cell.target, cell.horizon_days) for cell in cells}) == CURRENT_FORMAL_CELL_COUNT,
        customer_boundary=(
            "价格和方向仅用于个人上游市场研判；不使用用户库存、采购量、生产计划、加工利润或成交价，"
            "不生成采购、销售、套保或交易指令。OOS 门禁通过前所有数值均为非正式参考。"
        ),
    )


def load_current_label_series(target: str, as_of: datetime) -> LoadedLabelSeries:
    if target not in LABEL_REGISTRY:
        raise ValueError("unknown_seven_product_target")
    if target in CURRENT_SOURCE_GAPS:
        return LoadedLabelSeries(
            points=(),
            source_matches_label=False,
            data_gaps=(CURRENT_SOURCE_GAPS[target],),
        )
    if target == "crude":
        return _load_crude_futures(as_of)
    if target in TARGET_FUTURES_PRODUCT:
        official = _load_futures(target, as_of)
        if official.points:
            return official
        fallback = _load_intraday(target, as_of)
        return LoadedLabelSeries(
            points=fallback.points,
            source_matches_label=False,
            data_gaps=("official_daily_label_missing_using_public_proxy", *fallback.data_gaps),
        )
    if target in {"poy", "dty"}:
        captured = _load_captured_market_spot(target, as_of)
        if captured.points:
            return captured
        public_history = _load_market_spot_history(target, as_of)
        if public_history.points:
            return LoadedLabelSeries(
                points=public_history.points,
                source_matches_label=False,
                data_gaps=("append_only_capture_revision_missing", *public_history.data_gaps),
            )
        fallback = _load_intraday(target, as_of)
        return LoadedLabelSeries(
            points=fallback.points,
            source_matches_label=False,
            data_gaps=(
                "configured_tnc_history_missing_using_public_spot_fallback",
                "append_only_capture_revision_missing",
                *fallback.data_gaps,
            ),
        )
    if target in {"naphtha", "meg"}:
        captured = _load_captured_spot(target, as_of)
        if captured.points:
            return captured
        fallback = _load_intraday(target, as_of)
        return LoadedLabelSeries(
            points=fallback.points,
            source_matches_label=False,
            data_gaps=("append_only_capture_revision_missing", *fallback.data_gaps),
        )
    return _load_intraday(target, as_of)


def _load_crude_eia_spot_legacy(as_of: datetime) -> LoadedLabelSeries:
    """EIA Brent spot FOB label used by seven-product-labels.v1..v4.

    Kept read-only for settlement only: forecasts issued before v5 must be
    scored against the series their contract named, otherwise every matured
    pre-v5 crude cell is rejected with label_series_identity_mismatch once the
    live loader starts returning the v5 futures series. New issuance never
    uses this path.
    """
    captures = list_source_capture_revisions(
        source_id=CRUDE_EIA_SPOT_SOURCE_ID,
        semantic_series_id=CRUDE_EIA_SPOT_SERIES_ID,
        as_of_time=as_of.isoformat(),
        limit=5000,
    )
    points: list[PricePoint] = []
    for capture in captures:
        payload = capture.get("canonical_payload")
        if not isinstance(payload, dict):
            continue
        point = _point(
            {
                **payload,
                "capture_revision_id": capture.get("capture_revision_id"),
                "observed_at": capture.get("observed_at"),
                "visible_at": capture.get("visible_at"),
                "source_id": CRUDE_EIA_SPOT_SOURCE_ID,
                "source_url": capture.get("source_url"),
                "raw_sha256": capture.get("raw_sha256"),
                "semantic_series_id": CRUDE_EIA_SPOT_SERIES_ID,
                "contract_version": CRUDE_EIA_SPOT_LABEL_REGISTRY_VERSION,
                "unit": payload.get("unit", "USD/bbl"),
            },
            observation_id_field="capture_revision_id",
            observed_at_field="observed_at",
            visible_at_field="visible_at",
            value_field="value",
            url_field="source_url",
            expected_unit="USD/bbl",
        )
        if point:
            points.append(point)
    if points:
        return LoadedLabelSeries(points=_daily_points(points, as_of), source_matches_label=True)

    rows = list_market_observations(
        source_id=CRUDE_EIA_SPOT_SOURCE_ID,
        product="crude_oil",
        end=as_of.date().isoformat(),
        as_of_time=as_of.isoformat(),
        limit=5000,
    )
    fallback: list[PricePoint] = []
    for row in rows:
        indicator = str(row.get("indicator") or "").casefold()
        unit = _unit(str(row.get("unit") or ""))
        if "europe brent spot price fob" not in indicator or unit != "USD/bbl":
            continue
        point = _point(
            {
                **row,
                "semantic_series_id": CRUDE_EIA_SPOT_SERIES_ID,
                "contract_version": CRUDE_EIA_SPOT_LABEL_REGISTRY_VERSION,
            },
            observation_id_field="observation_id",
            observed_at_field="observed_at",
            visible_at_field="created_at",
            value_field="value",
            url_field="evidence_url",
            expected_unit="USD/bbl",
        )
        if point:
            fallback.append(point)
    return LoadedLabelSeries(points=_daily_points(fallback, as_of), source_matches_label=bool(fallback))


def load_label_series_for_identity(target: str, as_of: datetime, *, series_id: str) -> LoadedLabelSeries:
    """Load the series a frozen label identity names, current or retired."""
    if target == "crude" and series_id == CRUDE_EIA_SPOT_SERIES_ID:
        return _load_crude_eia_spot_legacy(as_of)
    return load_current_label_series(target, as_of)


def _load_crude_futures(as_of: datetime) -> LoadedLabelSeries:
    """Crude OOS label: ICE Brent front-month daily close (BZ=F).

    Switched from EIA spot FOB in seven-product-labels.v5 (2026-09-24): the
    EIA spot assessment diverged 15-25 USD/bbl from every market-facing series
    (futures, news) and its history arrived in one bulk backfill, which made
    direction scoring against it meaningless. The futures close is daily,
    market-consistent, and imported each trading day by the proxy importer.
    """
    rows = list_market_observations(
        source_id="yahoo_futures_daily_proxy",
        product="crude_oil",
        end=as_of.date().isoformat(),
        as_of_time=as_of.isoformat(),
        limit=5000,
    )
    # The proxy importer writes plain market rows, so the frozen label identity
    # is stamped from the query itself (source + indicator + unit are already
    # pinned above). Without it settlement rejects every crude point with
    # label_series_identity_mismatch because market_observations carries no
    # semantic_series_id/contract_version column.
    definition = LABEL_REGISTRY["crude"]
    points = []
    for row in rows:
        if str(row.get("indicator") or "") != "Brent futures daily close":
            continue
        point = _point(
            {
                **row,
                "semantic_series_id": definition.series_id,
                "contract_version": CURRENT_LABEL_REGISTRY_VERSION,
                "raw_sha256": str(row.get("raw_sha256") or "") or _proxy_evidence_sha256(row),
            },
            observation_id_field="observation_id",
            observed_at_field="observed_at",
            visible_at_field="created_at",
            value_field="value",
            url_field="evidence_url",
            expected_unit="USD/bbl",
        )
        if point:
            points.append(point)
    return LoadedLabelSeries(points=_daily_points(points, as_of), source_matches_label=bool(points))


def _load_futures(target: str, as_of: datetime) -> LoadedLabelSeries:
    definition = LABEL_REGISTRY[target]
    captures = list_source_capture_revisions(
        source_id=definition.source_id,
        semantic_series_id=definition.series_id,
        as_of_time=as_of.isoformat(),
        limit=5000,
    )
    captured_points: list[PricePoint] = []
    for capture in captures:
        payload = capture.get("canonical_payload")
        if not isinstance(payload, dict):
            continue
        value_field = "settle" if _finite_positive(payload.get("settle")) else "close"
        point = _point(
            {
                **payload,
                "capture_revision_id": capture.get("capture_revision_id"),
                "observed_at": capture.get("observed_at"),
                "visible_at": capture.get("visible_at"),
                "source_id": capture.get("source_id"),
                "source_url": capture.get("source_url"),
                "raw_sha256": capture.get("raw_sha256"),
                "semantic_series_id": capture.get("semantic_series_id"),
                "contract_version": capture.get("contract_version"),
                "unit": payload.get("unit", definition.unit),
            },
            observation_id_field="capture_revision_id",
            observed_at_field="observed_at",
            visible_at_field="visible_at",
            value_field=value_field,
            url_field="source_url",
            expected_unit="CNY/mt",
        )
        if point:
            captured_points.append(point)
    if captured_points:
        return LoadedLabelSeries(points=_daily_points(captured_points, as_of), source_matches_label=True)

    rows = list_futures_daily_bars(
        source_id=definition.source_id,
        product=TARGET_FUTURES_PRODUCT[target],
        end=as_of.date().isoformat(),
        as_of_time=as_of.isoformat(),
        limit=5000,
    )
    points = []
    for row in rows:
        if not (row.get("is_main") or row.get("contract_role") == "main_continuous"):
            continue
        value_field = "settle" if _finite_positive(row.get("settle")) else "close"
        point = _point(
            row,
            observation_id_field="bar_id",
            observed_at_field="trade_date",
            visible_at_field="visible_at",
            value_field=value_field,
            url_field="source_url",
            expected_unit="CNY/mt",
        )
        if point:
            points.append(point)
    return LoadedLabelSeries(
        points=_daily_points(points, as_of),
        source_matches_label=False,
        data_gaps=("official_capture_revision_missing",) if points else (),
    )


def _load_intraday(target: str, as_of: datetime) -> LoadedLabelSeries:
    definition = LABEL_REGISTRY[target]
    rows = list_intraday_price_observations(
        instrument=TARGET_INSTRUMENT[target],
        end=as_of.isoformat(),
        as_of_time=as_of.isoformat(),
        limit=1000,
    )
    points = []
    for row in rows:
        if target == "naphtha" and row.get("source_id") != definition.source_id:
            continue
        if target == "naphtha" and not public_spot_quote_matches_instrument("NAPHTHA", row):
            continue
        point = _point(
            row,
            observation_id_field="observation_id",
            observed_at_field="observed_at",
            visible_at_field="created_at",
            value_field="last",
            url_field="source_url",
            expected_unit=_unit(definition.unit),
        )
        if point:
            points.append(point)
    source_matches = bool(points) and all(point.source_id == definition.source_id for point in points)
    gaps = () if source_matches else ("configured_label_source_has_no_usable_history",)
    return LoadedLabelSeries(points=_daily_points(points, as_of), source_matches_label=source_matches, data_gaps=gaps)


def _load_captured_spot(target: str, as_of: datetime) -> LoadedLabelSeries:
    return _load_captured_label(target, as_of, value_field="last")


def _load_captured_market_spot(target: str, as_of: datetime) -> LoadedLabelSeries:
    return _load_captured_label(target, as_of, value_field="value")


def _load_captured_label(target: str, as_of: datetime, *, value_field: str) -> LoadedLabelSeries:
    definition = LABEL_REGISTRY[target]
    captures = list_source_capture_revisions(
        source_id=definition.source_id,
        semantic_series_id=definition.series_id,
        as_of_time=as_of.isoformat(),
        limit=5000,
    )
    points: list[PricePoint] = []
    for capture in captures:
        payload = capture.get("canonical_payload")
        if not isinstance(payload, dict):
            continue
        if target == "naphtha" and not public_spot_quote_matches_instrument(
            "NAPHTHA", {**payload, "source_id": capture.get("source_id"), "observed_at": capture.get("observed_at")}
        ):
            continue
        point = _point(
            {
                **payload,
                "capture_revision_id": capture.get("capture_revision_id"),
                "observed_at": capture.get("observed_at"),
                "visible_at": capture.get("visible_at"),
                "source_id": capture.get("source_id"),
                "source_url": capture.get("source_url"),
                "raw_sha256": capture.get("raw_sha256"),
                "semantic_series_id": capture.get("semantic_series_id"),
                "contract_version": capture.get("contract_version"),
                "unit": payload.get("unit", definition.unit),
            },
            observation_id_field="capture_revision_id",
            observed_at_field="observed_at",
            visible_at_field="visible_at",
            value_field=value_field,
            url_field="source_url",
            expected_unit=_unit(definition.unit),
        )
        if point:
            points.append(point)
    return LoadedLabelSeries(
        points=_daily_points(points, as_of),
        source_matches_label=bool(points),
        data_gaps=() if points else ("append_only_capture_revision_missing",),
    )


def _load_market_spot_history(target: str, as_of: datetime) -> LoadedLabelSeries:
    definition = LABEL_REGISTRY[target]
    rows = list_market_observations(
        source_id=definition.source_id,
        product=target,
        end=as_of.date().isoformat(),
        as_of_time=as_of.isoformat(),
        limit=5000,
    )
    points = []
    for row in rows:
        point = _point(
            row,
            observation_id_field="observation_id",
            observed_at_field="observed_at",
            visible_at_field="created_at",
            value_field="value",
            url_field="evidence_url",
            expected_unit="CNY/mt",
        )
        if point:
            points.append(point)
    return LoadedLabelSeries(
        points=_daily_points(points, as_of),
        source_matches_label=bool(points),
        data_gaps=() if points else ("configured_label_source_has_no_usable_history",),
    )


def _forecast_cell(
    *,
    target: str,
    horizon_days: int,
    as_of: datetime,
    loaded: LoadedLabelSeries,
    model_registry: dict[str, Any],
    settled_history: Sequence[Mapping[str, Any]],
    forecast_contract: str = "observation-horizon.v1",
) -> SevenProductForecastCell:
    definition = LABEL_REGISTRY[target]
    points = loaded.points[-LOOKBACK_POINTS:]
    data_digest = _digest(
        [
            [
                point.observation_id,
                point.observed_at,
                point.visible_at,
                point.value,
                point.unit,
                point.source_id,
                point.raw_sha256,
            ]
            for point in points
        ]
    )
    config = {
        "forecast_contract": forecast_contract,
        "feature_version": FEATURE_VERSION,
        "horizon_days": horizon_days,
        "label_registry_version": CURRENT_LABEL_REGISTRY_VERSION,
        "label_series_id": definition.series_id,
        "model_version": MODEL_VERSION,
        "neutral_band_floor_pct": NEUTRAL_FLOOR_PCT[target],
        "neutral_band_policy_version": CURRENT_NEUTRAL_BAND_POLICY_VERSION,
        "runtime_policy": {
            "confidence_base": CONFIDENCE_BASE,
            "confidence_cap": CONFIDENCE_CAP,
            "confidence_history_points": CONFIDENCE_HISTORY_POINTS,
            "confidence_history_weight": CONFIDENCE_HISTORY_WEIGHT,
            "confidence_signal_weight": CONFIDENCE_SIGNAL_WEIGHT,
            "freshness_days": FRESHNESS_DAYS[target],
            "lookback_points": LOOKBACK_POINTS,
            "max_abs_daily_log_return": MAX_ABS_DAILY_LOG_RETURN,
            "max_evidence_points": MAX_EVIDENCE_POINTS,
            "min_trend_points": MIN_TREND_POINTS,
            "naive_seasonal_lag": CURRENT_NAIVE_SEASONAL_LAG,
            "version": RUNTIME_POLICY_VERSION,
        },
        "target": target,
    }
    config_digest = _digest(config)
    evidence = [
        {
            "observation_id": point.observation_id,
            "source_id": point.source_id,
            "source_url": point.source_url,
            "observed_at": point.observed_at,
            "visible_at": point.visible_at,
            "value": point.value,
            "unit": point.unit,
            "raw_sha256": point.raw_sha256,
        }
        for point in points[-MAX_EVIDENCE_POINTS:]
    ]
    if not points:
        return _cell(
            target=target,
            horizon_days=horizon_days,
            as_of=as_of,
            definition=definition,
            data_snapshot_sha256=data_digest,
            configuration_sha256=config_digest,
            evidence=evidence,
            formal_status="model_unavailable",
            status_reason="no point-in-time label observations are available",
            data_status="missing",
            source_matches_label=loaded.source_matches_label,
            history_points=0,
            data_gaps=[*loaded.data_gaps, "label_history_missing"],
            model_registry_revision=str(model_registry["registry_revision"]),
        )

    latest = points[-1]
    age_days = max(0, (as_of.date() - _timestamp(latest.observed_at).date()).days)
    stale = age_days > FRESHNESS_DAYS[target]
    if len(points) < 2:
        return _cell(
            target=target,
            horizon_days=horizon_days,
            as_of=as_of,
            definition=definition,
            latest=latest,
            point_forecast=latest.value,
            interval_low=latest.value,
            interval_high=latest.value,
            predicted_change_pct=0.0,
            neutral_band_pct=NEUTRAL_FLOOR_PCT[target],
            direction="uncertain",
            confidence=0.15,
            formal_status="insufficient_data",
            status_reason="fewer than two distinct point-in-time label observations",
            data_status="stale" if stale else "insufficient",
            source_matches_label=loaded.source_matches_label,
            history_points=len(points),
            data_gaps=[*loaded.data_gaps, "label_history_needs_at_least_two_points"],
            evidence=evidence,
            data_snapshot_sha256=data_digest,
            configuration_sha256=config_digest,
            model_registry_revision=str(model_registry["registry_revision"]),
        )

    projection_days = horizon_days
    if forecast_contract == "issue-calendar.v1":
        projection_days += max(0, (as_of.astimezone(ZoneInfo("Asia/Shanghai")).date()
                                   - date.fromisoformat(latest.observed_at[:10])).days)
    projection = robust_drift_projection(
        [point.value for point in points],
        horizon_days=projection_days,
        neutral_floor_pct=NEUTRAL_FLOOR_PCT[target],
        observation_days=[point.observed_at[:10] for point in points]
        if forecast_contract == "issue-calendar.v1" else None,
    )
    feature_readiness = {
        "target": bool(loaded.source_matches_label and not stale and len(points) >= MIN_TREND_POINTS),
    }
    runtime_decision = formal_runtime_decision(
        registry=model_registry,
        target=target,
        horizon_days=horizon_days,
        feature_readiness=feature_readiness,
        settled_history=settled_history,
    ) if forecast_contract == "observation-horizon.v1" else {
        "governance_tier": "reference", "selected_model_version": CALENDAR_MODEL_VERSION,
        "fallback_triggered": False, "fallback_reason": "new_contract_requires_forward_evaluation",
        "consecutive_losses_to_best_naive": 0,
    }
    if runtime_decision is None:
        runtime_decision = {
            "governance_tier": "reference",
            **reference_runtime_decision(
                registry=model_registry,
                target=target,
                horizon_days=horizon_days,
                feature_readiness=feature_readiness,
                settled_history=settled_history,
                default_model_version=MODEL_VERSION,
            ),
        }
    selected_model_version = str(runtime_decision["selected_model_version"])
    if selected_model_version == PERSISTENCE_MODEL_VERSION:
        projection = persistence_projection(
            latest_value=latest.value,
            realized_volatility=projection.realized_volatility,
            horizon_days=projection_days,
            neutral_floor_pct=NEUTRAL_FLOOR_PCT[target],
        )
        selected_feature_version = PERSISTENCE_FEATURE_VERSION
    elif selected_model_version in {MODEL_VERSION, CALENDAR_MODEL_VERSION}:
        selected_feature_version = (
            CALENDAR_FEATURE_VERSION if selected_model_version == CALENDAR_MODEL_VERSION else FEATURE_VERSION
        )
    else:
        raise ValueError("runtime_model_not_implemented")
    config = {
        **config,
        "feature_version": selected_feature_version,
        "model_version": selected_model_version,
        "runtime_model_decision": runtime_decision,
    }
    config_digest = _digest(config)
    history_quality = min(1.0, len(points) / CONFIDENCE_HISTORY_POINTS)
    signal_to_noise = min(
        1.0,
        abs(projection.predicted_change_pct) / max(projection.neutral_band_pct, 1e-9),
    )
    confidence = round(
        min(
            CONFIDENCE_CAP,
            CONFIDENCE_BASE + CONFIDENCE_HISTORY_WEIGHT * history_quality + CONFIDENCE_SIGNAL_WEIGHT * signal_to_noise,
        ),
        4,
    )
    gaps = list(loaded.data_gaps)
    if len(points) < MIN_TREND_POINTS:
        gaps.append(f"model_history_below_{MIN_TREND_POINTS}_points")
    if stale:
        gaps.append(f"latest_label_is_{age_days}_days_old")
    if not loaded.source_matches_label:
        gaps.append("active_observation_does_not_match_frozen_label_source")
    if runtime_decision["fallback_triggered"]:
        gaps.append(f"{runtime_decision['governance_tier']}_champion_fallback:{runtime_decision['fallback_reason']}")
    data_status = "stale" if stale else "proxy" if not loaded.source_matches_label else "fresh"
    approval = approved_champion_evidence(
        model_registry,
        target=target,
        horizon_days=horizon_days,
        model_version=selected_model_version,
    ) if forecast_contract == "observation-horizon.v1" else None
    formal_eligible = bool(approval) and not gaps
    formal_status = "formal" if formal_eligible else "degraded" if gaps else "reference"
    if formal_eligible:
        reason = "active per-cell champion is bound to an explicitly approved rolling OOS evaluation"
    elif gaps:
        reason = "runtime forecast degraded by model governance, label coverage, freshness, or source mismatch"
    else:
        reason = "reference forecast only: the 21-cell rolling OOS promotion gate has not passed"
    return _cell(
        target=target,
        horizon_days=horizon_days,
        as_of=as_of,
        definition=definition,
        latest=latest,
        point_forecast=projection.point_forecast,
        interval_low=projection.interval_low,
        interval_high=projection.interval_high,
        predicted_change_pct=projection.predicted_change_pct,
        neutral_band_pct=projection.neutral_band_pct,
        direction=projection.direction,
        confidence=confidence,
        formal_status=formal_status,
        formal_eligible=formal_eligible,
        evaluation_id=approval["evaluation_id"] if approval else None,
        evaluation_result_sha256=approval["evaluation_result_sha256"] if approval else None,
        model_registry_revision=str(model_registry["registry_revision"]),
        model_version=selected_model_version,
        feature_version=selected_feature_version,
        status_reason=reason,
        data_status=data_status,
        source_matches_label=loaded.source_matches_label,
        history_points=len(points),
        key_drivers=[
            f"robust_median_daily_log_return={projection.median_daily_log_return:.8f}",
            f"realized_volatility={projection.realized_volatility:.8f}",
            f"neutral_band_pct={projection.neutral_band_pct:.8f}",
            f"runtime_model_tier={runtime_decision['governance_tier']}",
            f"runtime_model_decision={runtime_decision['fallback_reason'] or 'configured_or_default'}",
            f"runtime_model_best_naive_loss_streak={runtime_decision['consecutive_losses_to_best_naive']}",
        ],
        data_gaps=gaps,
        evidence=evidence,
        data_snapshot_sha256=data_digest,
        configuration_sha256=config_digest,
    )


def robust_drift_projection(
    values: list[float] | tuple[float, ...],
    *,
    horizon_days: int,
    neutral_floor_pct: float,
    observation_days: list[str] | None = None,
) -> RobustProjection:
    """Return the frozen reference-model projection from training values only."""

    if horizon_days < 1 or len(values) < 2:
        raise ValueError("robust_projection_requires_two_points_and_positive_horizon")
    prices = np.asarray(values, dtype=np.float64)
    if not np.all(np.isfinite(prices)) or np.any(prices <= 0):
        raise ValueError("robust_projection_requires_finite_positive_values")
    returns = np.diff(np.log(prices))
    if observation_days is None:
        gaps = np.ones(len(returns))
    else:
        if len(observation_days) != len(values):
            raise ValueError("calendar_projection_date_count_mismatch")
        ordinals = np.asarray([date.fromisoformat(day).toordinal() for day in observation_days])
        gaps = np.diff(ordinals)
        if np.any(gaps <= 0):
            raise ValueError("calendar_projection_requires_strictly_increasing_dates")
    if observation_days is None:
        # Preserve the original calculation exactly for legacy contracts.
        median_return = float(np.median(returns))
        mad = float(np.median(np.abs(returns - median_return)))
        robust_sigma = max(1.4826 * mad, float(np.std(returns)), 1e-6)
    else:
        median_return = float(np.median(returns / gaps))
        # Multi-day moves estimate daily drift and daily residual volatility;
        # never count a missing quote as one day or invent an observation.
        residuals = (returns - median_return * gaps) / np.sqrt(gaps)
        mad = float(np.median(np.abs(residuals - np.median(residuals))))
        robust_sigma = max(1.4826 * mad, float(np.std(residuals)), 1e-6)
    clipped_return = float(np.clip(median_return, -MAX_ABS_DAILY_LOG_RETURN, MAX_ABS_DAILY_LOG_RETURN))
    horizon_scale = math.sqrt(horizon_days)
    predicted_change = math.exp(clipped_return * horizon_days) - 1
    latest = float(prices[-1])
    point_forecast = float(latest * (1 + predicted_change))
    interval_log_width = 1.2816 * robust_sigma * horizon_scale
    interval_low = float(latest * math.exp(clipped_return * horizon_days - interval_log_width))
    interval_high = float(latest * math.exp(clipped_return * horizon_days + interval_log_width))
    neutral_band = max(neutral_floor_pct, 0.5 * robust_sigma * horizon_scale)
    direction = "up" if predicted_change > neutral_band else "down" if predicted_change < -neutral_band else "neutral"
    return RobustProjection(
        point_forecast=point_forecast,
        interval_low=interval_low,
        interval_high=interval_high,
        predicted_change_pct=predicted_change,
        neutral_band_pct=neutral_band,
        direction=direction,
        median_daily_log_return=median_return,
        realized_volatility=robust_sigma,
    )


def persistence_projection(
    *, latest_value: float, realized_volatility: float, horizon_days: int, neutral_floor_pct: float
) -> RobustProjection:
    if not math.isfinite(latest_value) or latest_value <= 0 or horizon_days < 1:
        raise ValueError("persistence_projection_requires_positive_value_and_horizon")
    volatility = max(float(realized_volatility), 1e-6)
    interval_scale = 1.96 * volatility * math.sqrt(horizon_days)
    return RobustProjection(
        point_forecast=latest_value,
        interval_low=latest_value * math.exp(-interval_scale),
        interval_high=latest_value * math.exp(interval_scale),
        predicted_change_pct=0.0,
        neutral_band_pct=neutral_floor_pct,
        direction="neutral",
        median_daily_log_return=0.0,
        realized_volatility=volatility,
    )


def _cell(
    *,
    target: str,
    horizon_days: int,
    as_of: datetime,
    definition: Any,
    data_snapshot_sha256: str,
    configuration_sha256: str,
    formal_status: str,
    status_reason: str,
    data_status: str,
    source_matches_label: bool,
    history_points: int,
    data_gaps: list[str],
    evidence: list[dict[str, Any]],
    latest: PricePoint | None = None,
    point_forecast: float | None = None,
    interval_low: float | None = None,
    interval_high: float | None = None,
    predicted_change_pct: float | None = None,
    neutral_band_pct: float | None = None,
    direction: str = "uncertain",
    confidence: float = 0,
    key_drivers: list[str] | None = None,
    formal_eligible: bool = False,
    evaluation_id: str | None = None,
    evaluation_result_sha256: str | None = None,
    model_registry_revision: str = "seven-registry-initial",
    model_version: str = MODEL_VERSION,
    feature_version: str = FEATURE_VERSION,
) -> SevenProductForecastCell:
    return SevenProductForecastCell(
        target=target,
        horizon_days=horizon_days,
        label_series_id=definition.series_id,
        label_registry_version=CURRENT_LABEL_REGISTRY_VERSION,
        neutral_band_policy_version=CURRENT_NEUTRAL_BAND_POLICY_VERSION,
        model_version=model_version,
        feature_version=feature_version,
        as_of_time=as_of.isoformat(),
        latest_observation_at=latest.observed_at if latest else None,
        latest_visible_at=latest.visible_at if latest else None,
        latest_value=latest.value if latest else None,
        unit=definition.unit,
        point_forecast=_rounded(point_forecast),
        interval_low=_rounded(interval_low),
        interval_high=_rounded(interval_high),
        predicted_change_pct=_rounded(predicted_change_pct, 8),
        neutral_band_pct=_rounded(neutral_band_pct, 8),
        direction=direction,
        confidence=confidence,
        formal_status=formal_status,
        formal_eligible=formal_eligible,
        status_reason=status_reason,
        data_status=data_status,
        source_matches_label=source_matches_label,
        history_points=history_points,
        evaluation_status="passed" if formal_eligible else "not_evaluated",
        evaluation_id=evaluation_id,
        evaluation_result_sha256=evaluation_result_sha256,
        model_registry_revision=model_registry_revision,
        key_drivers=key_drivers or [],
        data_gaps=sorted(set(data_gaps)),
        evidence=evidence,
        data_snapshot_sha256=data_snapshot_sha256,
        configuration_sha256=configuration_sha256,
    )


def _point(
    row: dict[str, Any],
    *,
    observation_id_field: str,
    observed_at_field: str,
    visible_at_field: str,
    value_field: str,
    url_field: str,
    expected_unit: str,
) -> PricePoint | None:
    value = row.get(value_field)
    observed_at = str(row.get(observed_at_field) or "")
    visible_at = str(row.get(visible_at_field) or "")
    unit = _unit(str(row.get("unit") or expected_unit))
    if not _finite_positive(value) or not observed_at or not visible_at or unit != expected_unit:
        return None
    try:
        _timestamp(observed_at)
        _timestamp(visible_at)
    except ValueError:
        return None
    return PricePoint(
        observation_id=str(row.get(observation_id_field) or ""),
        observed_at=observed_at,
        visible_at=visible_at,
        value=float(value),
        unit=unit,
        source_id=str(row.get("source_id") or ""),
        source_url=str(row.get(url_field) or ""),
        raw_sha256=_raw_sha256(row),
        semantic_series_id=str(row.get("semantic_series_id") or ""),
        contract_version=str(row.get("contract_version") or ""),
    )


_PROXY_EVIDENCE_FIELDS = ("source_id", "observed_at", "indicator", "product", "value", "unit", "evidence_url")


def _proxy_evidence_sha256(row: dict[str, Any]) -> str:
    """Bind a proxy-import row to its stored canonical fields.

    The Yahoo chart importer writes plain market_observations rows with no
    capture revision and no raw payload digest, so the evidence-hash contract
    would otherwise reject every crude point. Hashing the stored canonical
    fields is deterministic (same row -> same digest) and preserves the
    append-only evidence binding without inventing a raw payload digest; a real
    capture hash on the row always wins.
    """

    return _digest({field: row.get(field) for field in _PROXY_EVIDENCE_FIELDS})


def _raw_sha256(row: dict[str, Any]) -> str:
    direct = str(row.get("raw_sha256") or "")
    if len(direct) == 64:
        return direct
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ""
    if isinstance(raw, dict):
        candidate = str(raw.get("raw_sha256") or raw.get("source_sha256") or "")
        if len(candidate) == 64:
            return candidate
    return ""


def _daily_points(points: list[PricePoint], as_of: datetime) -> tuple[PricePoint, ...]:
    by_date: dict[date, PricePoint] = {}
    for point in sorted(points, key=lambda item: (_timestamp(item.observed_at), _timestamp(item.visible_at))):
        observed = _timestamp(point.observed_at)
        visible = _timestamp(point.visible_at)
        if observed > as_of or visible > as_of:
            continue
        by_date[observed.date()] = point
    return tuple(by_date[key] for key in sorted(by_date))


def _timestamp(value: str | None) -> datetime:
    if not value:
        raise ValueError("timestamp_required")
    normalized = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("timestamp_invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _finite_positive(value: object) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _unit(value: str) -> str:
    normalized = value.strip().casefold().replace(" ", "")
    aliases = {
        "$/bbl": "USD/bbl",
        "dollars_per_barrel": "USD/bbl",
        "usd/bbl": "USD/bbl",
        "usd/mt": "USD/mt",
        "cny/mt": "CNY/mt",
        "元/吨": "CNY/mt",
    }
    return aliases.get(normalized, value.strip())


def _rounded(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

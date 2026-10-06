from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

CURRENT_FORECAST_SCHEMA_VERSION = "seven-product-forecast.v1"
CURRENT_LABEL_REGISTRY_VERSION = "seven-product-labels.v5"
# EIA Brent spot FOB was the crude OOS label through v4. It stays registered as
# evidence only (crosscheck of the futures label) and keeps resolving for every
# forecast issued before the v5 switch.
CRUDE_EIA_SPOT_SERIES_ID = "crude.brent.eia.spot.usd_bbl"
CRUDE_EIA_SPOT_SOURCE_ID = "eia_petroleum_api"
CRUDE_EIA_SPOT_LABEL_REGISTRY_VERSION = "seven-product-labels.v4"
CURRENT_NEUTRAL_BAND_POLICY_VERSION = "seven-product-neutral-bands.v1"
CURRENT_FORMAL_TARGETS = ("crude", "naphtha", "px", "pta", "meg", "poy", "dty")
CURRENT_FORMAL_HORIZONS = (1, 7, 30)
CURRENT_FORMAL_CELL_COUNT = len(CURRENT_FORMAL_TARGETS) * len(CURRENT_FORMAL_HORIZONS)
CURRENT_NAIVE_SEASONAL_LAG = 5
CURRENT_FORMAL_STATUSES = {
    "formal",
    "low_confidence",
    "reference",
    "degraded",
    "insufficient_data",
    "model_unavailable",
}
HISTORICAL_ONLY_SOURCE_IDS = frozenset({"ccf_dom_daily", "ccf_manual_export"})
SOFT_REMOVED_LABEL_SOURCE_IDS = frozenset({"dce_meg"})
CURRENT_SOURCE_GAPS = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class LabelDefinition:
    target: str
    series_id: str
    source_id: str
    metric: str
    market: str
    frequency: Literal["business_day", "published_day"]
    unit: str
    timezone: str
    value_field: str
    publication_rule: str
    visibility_rule: str
    revision_rule: str
    missing_value_rule: str
    continuity_rule: str
    proxy: bool
    qualification_status: Literal["candidate", "qualified"] = "candidate"


@dataclass(frozen=True, slots=True)
class FrozenLabelIdentity:
    """The source/series/unit identity that was frozen when a forecast was issued."""

    target: str
    registry_version: str
    series_id: str
    source_id: str
    unit: str


_LABELS = {
    "crude": LabelDefinition(
        target="crude",
        series_id="crude.brent.futures.yahoo.usd_bbl",
        source_id="yahoo_futures_daily_proxy",
        metric="Brent futures daily close (BZ=F)",
        market="ICE Brent front-month futures",
        frequency="business_day",
        unit="USD/bbl",
        timezone="America/New_York",
        value_field="value",
        publication_rule=(
            "front-month daily close published after the US session; imported once per trading day "
            "from the public chart API"
        ),
        visibility_rule="visible_at is the first successful import time, never the observation date midnight",
        revision_rule="append each import as a new observation; point-in-time reads select the latest visible row",
        missing_value_rule="do not forward-fill labels; advance to the next published business-day close",
        continuity_rule=(
            "front-month roll follows the chart API continuous contract; EIA spot (FOB) is kept as "
            "evidence only after the 2026-09 wide-basis review showed it diverging from the market"
        ),
        proxy=True,
    ),
    "naphtha": LabelDefinition(
        target="naphtha",
        series_id="naphtha.public.spot_assessment.usd_mt",
        source_id="public_spot_page_refresh",
        metric="public naphtha spot assessment",
        market="public international naphtha assessment",
        frequency="business_day",
        unit="USD/mt",
        timezone="Asia/Shanghai",
        value_field="last",
        publication_rule="use the dated public assessment shown on the source page",
        visibility_rule="visible_at is the first successful page capture time",
        revision_rule="retain every content/capture revision and never overwrite an earlier visible version",
        missing_value_rule="no forward fill for evaluation labels",
        continuity_rule="fixed assessment series; monthly customs volume cannot substitute for price",
        proxy=True,
    ),
    "px": LabelDefinition(
        target="px",
        series_id="px.czce.main_continuous.settlement.cny_mt",
        source_id="czce_pta_px",
        metric="CZCE PX main continuous daily settlement",
        market="CZCE",
        frequency="business_day",
        unit="CNY/mt",
        timezone="Asia/Shanghai",
        value_field="settle",
        publication_rule="after-close official daily publication",
        visibility_rule="visible_at is the first successful official-file capture time",
        revision_rule="append official-file revision by source hash",
        missing_value_rule="skip exchange non-trading days; never substitute broad xylenes",
        continuity_rule="largest open interest, volume tie-break, next-day roll, no back adjustment",
        proxy=True,
    ),
    "pta": LabelDefinition(
        target="pta",
        series_id="pta.czce.main_continuous.settlement.cny_mt",
        source_id="czce_pta_px",
        metric="CZCE PTA main continuous daily settlement",
        market="CZCE",
        frequency="business_day",
        unit="CNY/mt",
        timezone="Asia/Shanghai",
        value_field="settle",
        publication_rule="after-close official daily publication",
        visibility_rule="visible_at is the first successful official-file capture time",
        revision_rule="append official-file revision by source hash",
        missing_value_rule="skip exchange non-trading days without forward-filled labels",
        continuity_rule="largest open interest, volume tie-break, next-day roll, no back adjustment",
        proxy=True,
    ),
    "meg": LabelDefinition(
        target="meg",
        series_id="meg.sunsirs.china.spot_assessment.cny_mt",
        source_id="sunsirs_public_commodity_assessment",
        metric="SunSirs China ethylene glycol spot assessment",
        market="China ethylene glycol spot assessment",
        frequency="business_day",
        unit="CNY/mt",
        timezone="Asia/Shanghai",
        value_field="last",
        publication_rule=(
            "the published 2013 methodology schedules assessment at 09:00-10:30 and publication at 11:30; "
            "current applicability is unverified, so scheduled time is descriptive only"
        ),
        visibility_rule="visible_at is the first successful page capture time, never the assessment date midnight",
        revision_rule="append every page-content revision by evidence hash and never overwrite an earlier version",
        missing_value_rule="no forward fill for evaluation labels",
        continuity_rule=(
            "fixed non-transaction China spot assessment; DCE, Sina and Eastmoney futures are features only"
        ),
        proxy=True,
    ),
    "poy": LabelDefinition(
        target="poy",
        series_id="poy.public.polyester_spot_assessment.cny_mt",
        source_id="tnc_polyester_history",
        metric="polyester POY public spot assessment",
        market="China polyester POY public assessment",
        frequency="published_day",
        unit="CNY/mt",
        timezone="Asia/Shanghai",
        value_field="last",
        publication_rule="use the dated polyester-POY assessment shown on the source page",
        visibility_rule="visible_at is the first successful page capture time",
        revision_rule="append every page-content revision; never merge nylon POY into polyester POY",
        missing_value_rule="no forward fill for evaluation labels",
        continuity_rule="fixed polyester product-family parser and versioned specification aggregation",
        proxy=True,
    ),
    "dty": LabelDefinition(
        target="dty",
        series_id="dty.public.polyester_spot_assessment.cny_mt",
        source_id="tnc_polyester_history",
        metric="polyester DTY public spot assessment",
        market="China polyester DTY public assessment",
        frequency="published_day",
        unit="CNY/mt",
        timezone="Asia/Shanghai",
        value_field="last",
        publication_rule="use the dated polyester-DTY assessment shown on the source page",
        visibility_rule="visible_at is the first successful page capture time",
        revision_rule="append every page-content revision and preserve the matched polyester quote phrase",
        missing_value_rule="no forward fill for evaluation labels",
        continuity_rule="fixed polyester product-family parser and versioned specification aggregation",
        proxy=True,
    ),
}

LABEL_REGISTRY = MappingProxyType(_LABELS)
CURRENT_FORMAL_LABEL_SERIES_IDS = tuple(_LABELS[target].series_id for target in CURRENT_FORMAL_TARGETS)

# v1/v2 differ from v3/v4 only for MEG source identity; v4 additionally
# corrects POY/DTY frequency metadata without changing series/source/unit.
# v5 switches the crude label from EIA Brent spot FOB to the ICE Brent
# front-month futures close (BZ=F) after the 2026-09 wide-basis review;
# forecasts issued under v1-v4 keep settling against the EIA identity.
# outcome is always checked against the contract that issued the forecast,
# never against whichever registry happens to be current at settlement time.
_FROZEN_LABEL_IDENTITIES: dict[tuple[str, str], FrozenLabelIdentity] = {}
for _registry_version in (
    "seven-product-labels.v1",
    "seven-product-labels.v2",
    "seven-product-labels.v3",
    "seven-product-labels.v4",
    CURRENT_LABEL_REGISTRY_VERSION,
):
    for _target, _definition in _LABELS.items():
        _series_id = _definition.series_id
        _source_id = _definition.source_id
        _unit = _definition.unit
        if _target == "meg" and _registry_version in {"seven-product-labels.v1", "seven-product-labels.v2"}:
            _series_id = "meg.dce.main_continuous.settlement.cny_mt"
            _source_id = "dce_meg"
        if _target == "crude" and _registry_version != CURRENT_LABEL_REGISTRY_VERSION:
            _series_id = CRUDE_EIA_SPOT_SERIES_ID
            _source_id = CRUDE_EIA_SPOT_SOURCE_ID
        _FROZEN_LABEL_IDENTITIES[(_registry_version, _target)] = FrozenLabelIdentity(
            target=_target,
            registry_version=_registry_version,
            series_id=_series_id,
            source_id=_source_id,
            unit=_unit,
        )
FROZEN_LABEL_IDENTITIES = MappingProxyType(_FROZEN_LABEL_IDENTITIES)


def frozen_label_identity(*, target: str, registry_version: str, series_id: str) -> FrozenLabelIdentity:
    """Resolve and verify an issued label identity, failing closed when unknown."""

    identity = FROZEN_LABEL_IDENTITIES.get((registry_version, target))
    if identity is None or identity.series_id != series_id:
        raise ValueError("unknown_frozen_label_identity")
    return identity


def validate_current_contract() -> None:
    if tuple(_LABELS) != CURRENT_FORMAL_TARGETS:
        raise RuntimeError("seven_product_target_order_mismatch")
    if CURRENT_FORMAL_CELL_COUNT != 21 or len(set(CURRENT_FORMAL_LABEL_SERIES_IDS)) != 7:
        raise RuntimeError("seven_product_grid_mismatch")
    if any(label.target != target for target, label in _LABELS.items()):
        raise RuntimeError("seven_product_label_target_mismatch")
    if any(label.source_id in HISTORICAL_ONLY_SOURCE_IDS or ".ccf." in label.series_id for label in _LABELS.values()):
        raise RuntimeError("historical_ccf_cannot_qualify_current_forecast")
    if set(CURRENT_SOURCE_GAPS) - set(CURRENT_FORMAL_TARGETS):
        raise RuntimeError("seven_product_source_gap_target_unknown")
    if any(label.source_id in SOFT_REMOVED_LABEL_SOURCE_IDS for label in _LABELS.values()):
        raise RuntimeError("seven_product_soft_removed_source_cannot_be_current_label")


validate_current_contract()

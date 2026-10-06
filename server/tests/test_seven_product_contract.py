from __future__ import annotations

from app import formal_series_eligibility
from app.seven_product_contract import (
    CURRENT_FORECAST_SCHEMA_VERSION,
    CURRENT_FORMAL_CELL_COUNT,
    CURRENT_FORMAL_HORIZONS,
    CURRENT_FORMAL_LABEL_SERIES_IDS,
    CURRENT_FORMAL_TARGETS,
    CURRENT_SOURCE_GAPS,
    HISTORICAL_ONLY_SOURCE_IDS,
    LABEL_REGISTRY,
    SOFT_REMOVED_LABEL_SOURCE_IDS,
    validate_current_contract,
)
from app.source_registry import get_source


def test_current_contract_is_exactly_seven_targets_by_three_horizons() -> None:
    validate_current_contract()
    assert CURRENT_FORECAST_SCHEMA_VERSION == "seven-product-forecast.v1"
    assert CURRENT_FORMAL_TARGETS == ("crude", "naphtha", "px", "pta", "meg", "poy", "dty")
    assert CURRENT_FORMAL_HORIZONS == (1, 7, 30)
    assert CURRENT_FORMAL_CELL_COUNT == 21
    assert len(CURRENT_FORMAL_LABEL_SERIES_IDS) == 7


def test_ccf_is_historical_only_and_cannot_qualify_current_labels() -> None:
    assert set(HISTORICAL_ONLY_SOURCE_IDS) == {"ccf_dom_daily", "ccf_manual_export"}
    assert all(label.source_id not in HISTORICAL_ONLY_SOURCE_IDS for label in LABEL_REGISTRY.values())
    assert all(".ccf." not in series_id for series_id in CURRENT_FORMAL_LABEL_SERIES_IDS)
    assert any(".ccf." in series_id for series_id in formal_series_eligibility.PHASE_A_V7_HISTORICAL_SERIES_IDS)


def test_dce_stays_soft_removed_while_sunsirs_is_the_current_meg_label() -> None:
    assert dict(CURRENT_SOURCE_GAPS) == {}
    assert set(SOFT_REMOVED_LABEL_SOURCE_IDS) == {"dce_meg"}
    assert LABEL_REGISTRY["meg"].source_id == "sunsirs_public_commodity_assessment"
    assert LABEL_REGISTRY["meg"].series_id == "meg.sunsirs.china.spot_assessment.cny_mt"
    assert all(label.source_id not in SOFT_REMOVED_LABEL_SOURCE_IDS for label in LABEL_REGISTRY.values())
    source = get_source("sunsirs_public_commodity_assessment")
    assert source is not None
    assert source.data_role == "current_label"
    assert source.current_formal_eligible is True

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.derived_cost_pressure import CostPressureInputError, derive_cost_pressure_observations

PTA = "pta.ccf.domestic.daily_assessment.cny_mt"
MEG = "meg.ccf.domestic.daily_assessment.cny_mt"


def _points(days: int = 21) -> list[dict[str, object]]:
    start = date(2026, 1, 1)
    points: list[dict[str, object]] = []
    for offset in range(days):
        effective_date = (start + timedelta(days=offset)).isoformat()
        for series_id, value in ((PTA, 5000 + offset * 10), (MEG, 4000 + offset * 5)):
            points.append(
                {
                    "series_id": series_id,
                    "effective_date": effective_date,
                    "value": value,
                    "unit": "CNY/mt",
                    "quote_type": "authorized_daily_assessment",
                    "first_visible_at": f"{effective_date}T07:30:00+08:00",
                    "quality_status": "eligible",
                    "visibility_mode": "strict_as_of",
                    "observation_id": f"{series_id}:{effective_date}",
                    "capture_revision_id": f"capture:{series_id}:{effective_date}",
                }
            )
    return points


def test_derives_both_targets_from_exact_common_dates_and_capture_lineage() -> None:
    result = derive_cost_pressure_observations(_points())

    assert result["status"] == "ready"
    assert len(result["common_effective_dates"]) == 21
    assert len(result["observations"]) == 4
    poy, dty, poy_latest, dty_latest = result["observations"]
    assert poy["series_id"] == "poy.upstream_cost_pressure.index"
    assert dty["series_id"] == "dty.upstream_cost_pressure.index"
    assert poy["effective_date"] == dty["effective_date"] == "2026-01-20"
    assert poy["value"] == dty["value"]
    assert poy_latest["effective_date"] == dty_latest["effective_date"] == "2026-01-21"
    assert poy_latest["value"] > 0
    assert poy_latest["value"] != poy["value"]
    assert len(poy_latest["capture_revision_ids"]) == 40
    assert poy_latest["capture_revision_ids"][:2] == [
        f"capture:{PTA}:2026-01-02",
        f"capture:{MEG}:2026-01-02",
    ]
    assert poy_latest["capture_revision_ids"][-2:] == [
        f"capture:{PTA}:2026-01-21",
        f"capture:{MEG}:2026-01-21",
    ]
    assert poy_latest["revision_id"] != dty_latest["revision_id"]
    assert poy_latest["quality_status"] == "eligible"


def test_missing_common_history_is_unavailable_without_fill_or_proxy() -> None:
    result = derive_cost_pressure_observations(_points(19))

    assert result == {
        "status": "unavailable",
        "reason_codes": ["cost_pressure_common_history_insufficient"],
        "common_effective_dates": [(date(2026, 1, 1) + timedelta(days=index)).isoformat() for index in range(19)],
        "observations": [],
    }


def test_duplicate_component_date_and_missing_capture_lineage_fail_closed() -> None:
    duplicate = _points(20)
    duplicate.append(duplicate[0])
    with pytest.raises(CostPressureInputError, match="cost_pressure_duplicate_component_date"):
        derive_cost_pressure_observations(duplicate)

    missing_lineage = _points(20)
    missing_lineage[0] = {**missing_lineage[0], "capture_revision_id": ""}
    with pytest.raises(CostPressureInputError, match="cost_pressure_lineage_missing"):
        derive_cost_pressure_observations(missing_lineage)


def test_component_quality_is_preserved_without_promoting_the_derived_point() -> None:
    points = _points(20)
    points[0] = {**points[0], "quality_status": "blocked"}
    result = derive_cost_pressure_observations(points)

    assert result["observations"]
    assert {item["quality_status"] for item in result["observations"]} == {"blocked"}

from copy import deepcopy
from datetime import date, timedelta

import pytest
from scripts.experiments.frozen_settlement import LEGACY_POLICY, POLICY, settle_frozen_cell

IDENTITY = {
    "series_id": "matched.crude",
    "source_id": "official",
    "unit": "USD/barrel",
    "contract_version": "frozen-v1",
}


def observations():
    start = date(2024, 11, 20)
    return [
        {
            **IDENTITY,
            "observation_id": str(i),
            "observed_at": (start + timedelta(days=i)).isoformat(),
            "visible_at": (start + timedelta(days=i)).isoformat() + "T00:00:00Z",
            "value": 100 * (1.03 if i % 2 else 1),
        }
        for i in range(100)
    ]


def issued(horizon=7):
    return {
        "business_date": "2025-01-02",
        "as_of_time": "2025-01-02T08:00:00+08:00",
        "target": "crude",
        "horizon_days": horizon,
        "origin_observation_id": "42",
        "label_identity": IDENTITY,
        "baseline_direction": "down",
        "event_adjusted_direction": "up",
        "neutral_band": 0.005,
    }


def test_day_boundary_does_not_expose_same_day_close_and_settlement_waits_for_actual_availability():
    prices = observations()
    result = settle_frozen_cell(issued(), prices, known_at="2025-02-15T00:00:00Z", policy=LEGACY_POLICY)
    assert result["origin_observation_id"] == "42"  # Jan1, not Jan2's midnight-stamped close.
    assert result["due_date"] == "2025-01-09"
    assert result["settled_available_at"] == "2025-01-10T00:00:00+00:00"
    pending = settle_frozen_cell(issued(), prices, known_at="2025-01-09T12:00:00Z", policy=LEGACY_POLICY)
    assert pending["status"] == "pending_maturity" and "actual_direction" not in pending
    with pytest.raises(ValueError, match="origin_not_latest"):
        settle_frozen_cell(
            {**issued(), "origin_observation_id": "43"}, prices, known_at="2025-02-15T00:00:00Z", policy=LEGACY_POLICY
        )


def test_reflection_label_band_uses_only_preissuance_prices():
    prices = observations()
    first = settle_frozen_cell(issued(30), prices, known_at="2025-02-15T00:00:00Z", policy=POLICY)
    changed = deepcopy(prices)
    for row in changed[43:]:
        row["value"] *= 4
    second = settle_frozen_cell(issued(30), changed, known_at="2025-02-15T00:00:00Z", policy=POLICY)
    assert first["band"] == second["band"]
    assert first["actual_change_pct"] != second["actual_change_pct"]
    assert first["band"]["neutral_band"] > 0.005
    d1 = settle_frozen_cell(issued(1), prices, known_at="2025-02-15T00:00:00Z", policy=POLICY)
    assert d1["band"]["neutral_band"] == 0.005


def test_mixed_units_or_duplicate_price_revisions_cannot_be_scored():
    for mutation in ("unit", "duplicate", "nan"):
        prices = observations()
        if mutation == "unit":
            prices[-1]["unit"] = "CNY/ton"
        elif mutation == "duplicate":
            prices.append({**prices[0], "observation_id": "revised"})
        else:
            prices[-1]["value"] = float("nan")
        with pytest.raises(ValueError):
            settle_frozen_cell(issued(), prices, known_at="2025-02-15T00:00:00Z", policy=LEGACY_POLICY)


def test_equivalent_intraday_timezones_preserve_origin_and_maturity():
    prices = observations()
    # Same instants expressed with another UTC offset must not reorder Jan1/Jan2.
    for row in prices:
        row["observed_at"] += "T20:00:00Z"
        row["visible_at"] = row["observed_at"]
    contract = issued()
    first = settle_frozen_cell(contract, prices, known_at="2025-02-15T00:00:00Z", policy=LEGACY_POLICY)
    shifted = deepcopy(prices)
    from datetime import datetime, timezone

    for row in shifted:
        at = datetime.fromisoformat(row["observed_at"].replace("Z", "+00:00"))
        row["observed_at"] = at.astimezone(timezone(timedelta(hours=8))).isoformat()
    second = settle_frozen_cell(contract, shifted, known_at="2025-02-15T00:00:00Z", policy=LEGACY_POLICY)
    for key in (
        "origin_observation_id",
        "actual_observation_id",
        "actual_change_pct",
        "actual_direction",
        "settled_available_at",
    ):
        assert first[key] == second[key]
    with pytest.raises(ValueError, match="ambiguous_price_revisions"):
        settle_frozen_cell(
            contract,
            prices + [{**shifted[0], "observation_id": "offset-copy"}],
            known_at="2025-02-15T00:00:00Z",
            policy=LEGACY_POLICY,
        )

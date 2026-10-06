import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[2] / "scripts/experiments/reflection_protocol.py"
spec = importlib.util.spec_from_file_location("reflection_protocol", path)
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def test_band_uses_past_prices_and_grows_by_horizon():
    prices = [
        {
            "observation_id": str(i),
            "observed_at": f"2025-01-{i + 1:02d}",
            "visible_at": "2025-01-31T00:00:00Z",
            "value": 100 * (1.03 if i % 2 else 1),
        }
        for i in range(30)
    ]
    cutoff = "2025-02-01T08:00:00+08:00"
    d1 = p.settlement_band(prices, as_of=cutoff, horizon=1)
    d7 = p.settlement_band(prices, as_of=cutoff, horizon=7)
    d30 = p.settlement_band(prices, as_of=cutoff, horizon=30)
    assert d1["neutral_band"] == 0.005 < d7["neutral_band"] < d30["neutral_band"]
    future = {
        "observation_id": "future",
        "observed_at": "2025-01-31",
        "visible_at": "2025-02-02T00:00:00Z",
        "value": 1e6,
    }
    assert p.settlement_band(prices + [future], as_of=cutoff, horizon=30) == d30
    with pytest.raises(ValueError, match="insufficient_or_invalid"):
        p.settlement_band(prices[:20], as_of=cutoff, horizon=7)
    with pytest.raises(ValueError, match="future_price_observation"):
        p.settlement_band(
            prices + [{**future, "observed_at": "2025-02-02", "visible_at": "2025-01-31T00:00:00Z"}],
            as_of=cutoff,
            horizon=7,
        )
    with pytest.raises(ValueError, match="duplicate_price_day"):
        p.settlement_band(prices + [{**prices[0], "observation_id": "second-revision"}], as_of=cutoff, horizon=7)


def test_distill_only_known_switched_cells_and_recompute_outcomes():
    row = {
        "business_date": "2025-01-01",
        "target": "crude",
        "horizon_days": 7,
        "settlement_policy": p.POLICY,
        "settled_available_at": "2025-01-09T01:00:00Z",
        "baseline_direction": "neutral",
        "event_adjusted_direction": "up",
        "actual_direction": "up",
        "actual_change_pct": 2,
        "band": {"policy": p.POLICY, "neutral_band": 0.01},
        "outcome_baseline": "miss",
        "outcome_adjusted": "hit",
    }
    report = p.reflection_corpus(
        [
            row,
            row,
            {**row, "settled_available_at": "2025-01-12T00:00:00Z"},
            {**row, "settlement_policy": "old"},
            {**row, "outcome_adjusted": "miss"},
            {**row, "event_adjusted_direction": "neutral"},
        ],
        known_at="2025-01-10T08:00:00+08:00",
    )
    assert report["cells"] == [row]
    assert report["excluded"] == {
        "duplicate_cell": 1,
        "not_known_settled": 1,
        "different_settlement_policy": 1,
        "outcome_mismatch": 1,
        "not_switched": 1,
    }


def test_reflection_corpus_recomputes_labels_from_frozen_term_band():
    row = {
        "business_date": "2025-01-01",
        "target": "crude",
        "horizon_days": 30,
        "settlement_policy": p.POLICY,
        "settled_available_at": "2025-02-02T00:00:00Z",
        "baseline_direction": "neutral",
        "event_adjusted_direction": "up",
        "actual_direction": "up",
        "actual_change_pct": 1,
        "band": {"policy": p.POLICY, "neutral_band": 0.05},
        "outcome_baseline": "miss",
        "outcome_adjusted": "hit",
    }
    report = p.reflection_corpus(
        [row, {**row, "band": {}}, {**row, "actual_change_pct": float("nan")}], known_at="2025-02-03T00:00:00Z"
    )
    assert report["cells"] == []
    assert report["excluded"] == {"actual_label_mismatch": 1, "missing_frozen_label_band": 2}


def test_intraday_future_and_incomplete_daily_close_cannot_enter_band():
    prices = [
        {
            "observation_id": str(i),
            "observed_at": f"2025-01-{i + 1:02d}",
            "visible_at": "2025-01-31T00:00:00Z",
            "value": 100 + i,
        }
        for i in range(30)
    ]
    cutoff = "2025-02-01T08:00:00+08:00"
    original = p.settlement_band(prices, as_of=cutoff, horizon=30)
    # A claimed midnight availability does not reveal the Feb1 closing value.
    daily = {
        "observation_id": "same-day-close",
        "observed_at": "2025-02-01",
        "visible_at": "2025-02-01T00:00:00Z",
        "value": 1e6,
    }
    assert p.settlement_band(prices + [daily], as_of=cutoff, horizon=30) == original
    with pytest.raises(ValueError, match="future_price_observation"):
        p.settlement_band(prices + [{**daily, "observed_at": "2025-02-01T09:00:00+08:00"}], as_of=cutoff, horizon=30)


def test_price_clocks_normalize_timezone_before_order_and_duplicate_detection():
    assert p.observation_clock({"observed_at": "2025-02-02T01:00:00+08:00"}).isoformat() == "2025-02-01T17:00:00+00:00"
    assert p.observation_day({"observed_at": "2025-02-02T01:00:00+08:00"}) == "2025-02-01"
    prices = [
        {
            "observation_id": str(i),
            "observed_at": f"2025-01-{i + 1:02d}",
            "visible_at": "2025-01-31T00:00:00Z",
            "value": 100 + i,
        }
        for i in range(30)
    ]
    duplicate = {
        "observation_id": "timezone-copy",
        "observed_at": "2025-01-31T01:00:00+08:00",
        "visible_at": "2025-01-31T00:00:00Z",
        "value": 129,
    }
    with pytest.raises(ValueError, match="duplicate_price_day"):
        p.settlement_band(prices + [duplicate], as_of="2025-02-01T00:00:00Z", horizon=7)

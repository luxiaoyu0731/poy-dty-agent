from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.seven_product_evaluation import _block_bootstrap, _rolling_samples
from app.seven_product_experiment import _block_bootstrap as research_bootstrap
from app.seven_product_forecast import LOOKBACK_POINTS, NEUTRAL_FLOOR_PCT, PricePoint, robust_drift_projection


def test_rolling_oos_matches_runtime_window_after_market_reversal() -> None:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    values = [100.0]
    for index in range(409):
        values.append(values[-1] * np.exp(0.01 if index < 280 else -0.02))
    points = tuple(
        PricePoint(
            observation_id=str(index),
            observed_at=(start + timedelta(days=index)).isoformat(),
            visible_at=(start + timedelta(days=index, hours=1)).isoformat(),
            value=value,
            unit="USD/bbl",
            source_id="test",
            source_url="https://example.com/price",
        )
        for index, value in enumerate(values)
    )
    sample = next(
        row
        for row in _rolling_samples(target="crude", horizon_days=7, points=points)
        if row["origin_observation_id"] == "399"
    )
    live = robust_drift_projection(
        values[400 - LOOKBACK_POINTS : 400],
        horizon_days=7,
        neutral_floor_pct=NEUTRAL_FLOOR_PCT["crude"],
    )
    assert live.direction == "down"
    assert sample["candidate_direction"] == live.direction
    assert sample["candidate"] == pytest.approx(live.point_forecast)


@pytest.mark.parametrize("count", [1, 20, 30, 59])
@pytest.mark.parametrize("bootstrap", [_block_bootstrap, research_bootstrap])
def test_insufficient_blocks_do_not_claim_degenerate_confidence_interval(count: int, bootstrap) -> None:
    errors = np.arange(count, dtype=float) + 1
    improvement, accuracy = bootstrap(
        candidate_error=errors,
        persistence_error=errors + 1,
        seasonal_error=errors + 2,
        direction_hit=np.arange(count) % 2,
        block_length=min(30, count),
        replicates=100,
        seed=7,
    )
    assert improvement == (None, None)
    assert accuracy == (None, None)


def test_sufficient_varying_blocks_keep_reproducible_uncertainty() -> None:
    arguments = dict(
        candidate_error=np.arange(90, dtype=float) + 1,
        persistence_error=np.full(90, 55.0),
        seasonal_error=np.full(90, 60.0),
        direction_hit=np.asarray([0.0] * 45 + [1.0] * 45),
        block_length=30,
        replicates=100,
        seed=7,
    )
    first = _block_bootstrap(**arguments)
    assert first == _block_bootstrap(**arguments)
    assert first[1][0] < first[1][1]

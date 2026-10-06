from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.seven_product_evaluation import _rolling_samples, evaluate_seven_product_forecast
from app.seven_product_forecast import LoadedLabelSeries, PricePoint


def _series(
    target: str,
    _as_of: datetime,
    *,
    source_matches_label: bool = True,
    flat: bool = False,
    visible_before_observed: bool = False,
) -> LoadedLabelSeries:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    points = []
    for index in range(200):
        observed = start + timedelta(days=index)
        visible = observed - timedelta(hours=1) if visible_before_observed else observed + timedelta(hours=1)
        points.append(
            PricePoint(
                observation_id=f"{target}-{index}",
                observed_at=observed.isoformat(),
                visible_at=visible.isoformat(),
                value=100.0 if flat else 100.0 * (1.01**index),
                unit="USD/mt",
                source_id=f"source-{target}",
                source_url=f"https://example.com/{target}/{index}",
            )
        )
    return LoadedLabelSeries(points=tuple(points), source_matches_label=source_matches_label)


def test_all_21_cells_must_pass_independently_on_predictable_oos_series() -> None:
    report = evaluate_seven_product_forecast(
        as_of_time="2026-01-01T00:00:00Z",
        series_loader=_series,
        bootstrap_replicates=100,
    )

    assert report.contract_complete is True
    assert len(report.cells) == 21
    assert report.passed_count == 21
    assert report.overall_status == "passed"
    assert all(cell.error_improvement is not None and cell.error_improvement >= 0.05 for cell in report.cells)
    assert all(cell.direction_accuracy is not None and cell.direction_accuracy >= 0.55 for cell in report.cells)
    assert all(cell.leakage_status == "passed" for cell in report.cells)
    assert all(len(cell.recent_outcomes) == 5 for cell in report.cells)
    latest = report.cells[0].recent_outcomes[-1]
    assert latest.actual_observed_at > latest.origin_observed_at
    assert latest.actual_visible_at > latest.origin_visible_at
    assert latest.absolute_error >= 0
    assert latest.direction_hit is True


def test_one_target_failure_cannot_be_hidden_by_aggregate_scores() -> None:
    def loader(target: str, as_of: datetime) -> LoadedLabelSeries:
        return _series(target, as_of, flat=target == "dty")

    report = evaluate_seven_product_forecast(
        as_of_time="2026-01-01T00:00:00Z",
        series_loader=loader,
        bootstrap_replicates=100,
    )

    assert report.passed_count == 18
    assert report.overall_status == "blocked"
    failed = [cell for cell in report.cells if not cell.promotion_eligible]
    assert {(cell.target, cell.horizon_days) for cell in failed} == {("dty", 1), ("dty", 7), ("dty", 30)}
    assert all("error_improvement_below_0.05" in cell.gate_reasons for cell in failed)


def test_point_in_time_leakage_and_source_mismatch_fail_closed() -> None:
    def loader(target: str, as_of: datetime) -> LoadedLabelSeries:
        return _series(
            target,
            as_of,
            source_matches_label=target != "poy",
            visible_before_observed=target == "crude",
        )

    report = evaluate_seven_product_forecast(
        as_of_time="2026-01-01T00:00:00Z",
        series_loader=loader,
        bootstrap_replicates=100,
    )

    crude = [cell for cell in report.cells if cell.target == "crude"]
    poy = [cell for cell in report.cells if cell.target == "poy"]
    assert all(cell.leakage_status == "failed" and not cell.promotion_eligible for cell in crude)
    assert all("frozen_label_source_mismatch" in cell.gate_reasons for cell in poy)


def test_same_time_historical_backfill_cannot_masquerade_as_point_in_time_oos() -> None:
    capture_time = datetime(2025, 12, 31, tzinfo=UTC)

    def loader(target: str, _as_of: datetime) -> LoadedLabelSeries:
        start = datetime(2025, 1, 1, tzinfo=UTC)
        return LoadedLabelSeries(
            points=tuple(
                PricePoint(
                    observation_id=f"{target}-archive-{index}",
                    observed_at=(start + timedelta(days=index)).isoformat(),
                    visible_at=capture_time.isoformat(),
                    value=100.0 * (1.01**index),
                    unit="USD/mt",
                    source_id=f"source-{target}",
                    source_url=f"https://example.com/archive/{target}",
                )
                for index in range(200)
            ),
            source_matches_label=True,
        )

    report = evaluate_seven_product_forecast(
        as_of_time="2026-01-01T00:00:00Z",
        series_loader=loader,
        bootstrap_replicates=100,
    )

    assert report.passed_count == 0
    assert report.overall_status == "blocked"
    assert all(cell.leakage_status == "passed" for cell in report.cells)
    assert all(cell.sample_count == 0 for cell in report.cells)
    assert all("insufficient_effective_samples:0<20" in cell.gate_reasons for cell in report.cells)


def test_batch_backfill_becomes_training_only_for_later_genuinely_visible_targets() -> None:
    capture_time = datetime(2025, 3, 1, tzinfo=UTC)
    points = [
        PricePoint(
            observation_id=f"archive-{index}",
            observed_at=(datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
            visible_at=capture_time.isoformat(),
            value=100.0 * (1.01**index),
            unit="USD/mt",
            source_id="source-crude",
            source_url="https://example.com/archive/crude",
        )
        for index in range(40)
    ]
    points.extend(
        PricePoint(
            observation_id=f"live-{index}",
            observed_at=(capture_time + timedelta(days=index)).isoformat(),
            visible_at=(capture_time + timedelta(days=index, hours=1)).isoformat(),
            value=150.0 * (1.01**index),
            unit="USD/mt",
            source_id="source-crude",
            source_url="https://example.com/live/crude",
        )
        for index in range(40)
    )

    samples = _rolling_samples(target="crude", horizon_days=1, points=tuple(points))

    assert samples
    assert all(
        datetime.fromisoformat(sample["actual_visible_at"])
        > datetime.fromisoformat(sample["origin_visible_at"])
        for sample in samples
    )
    assert all(
        datetime.fromisoformat(sample["actual_observed_at"]).date()
        >= datetime.fromisoformat(sample["origin_visible_at"]).date()
        for sample in samples
    )
    assert {sample["origin_observation_id"] for sample in samples}.isdisjoint(
        {f"archive-{index}" for index in range(40)}
    )


def test_actual_already_visible_at_origin_is_never_scored() -> None:
    capture_time = datetime(2025, 1, 21, 12, tzinfo=UTC)
    points = tuple(
        PricePoint(
            observation_id=f"point-{index}",
            observed_at=(datetime(2025, 1, 1, tzinfo=UTC) + timedelta(days=index)).isoformat(),
            visible_at=capture_time.isoformat(),
            value=100.0 + index,
            unit="USD/mt",
            source_id="source-crude",
            source_url="https://example.com/crude",
        )
        for index in range(21)
    )

    samples = _rolling_samples(target="crude", horizon_days=1, points=points)

    assert all(sample["origin_observation_id"] != "point-19" for sample in samples)


def test_evaluation_identity_is_stable_for_same_data_and_configuration() -> None:
    first = evaluate_seven_product_forecast(
        as_of_time="2026-01-01T00:00:00Z",
        series_loader=_series,
        bootstrap_replicates=100,
    )
    second = evaluate_seven_product_forecast(
        as_of_time="2026-01-01T00:00:00Z",
        series_loader=_series,
        bootstrap_replicates=100,
    )

    assert first.evaluation_id == second.evaluation_id
    assert first.report_sha256 == second.report_sha256
    assert [cell.result_sha256 for cell in first.cells] == [cell.result_sha256 for cell in second.cells]

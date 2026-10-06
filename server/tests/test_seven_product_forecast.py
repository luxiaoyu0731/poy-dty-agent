from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app import seven_product_forecast as forecast_module
from app.seven_product_contract import CURRENT_FORMAL_TARGETS, CURRENT_NAIVE_SEASONAL_LAG
from app.seven_product_forecast import (
    LoadedLabelSeries,
    PricePoint,
    build_seven_product_forecast,
    load_current_label_series,
)


def _loader(point_count: int = 40, *, source_matches_label: bool = True):
    def load(target: str, as_of: datetime) -> LoadedLabelSeries:
        points = tuple(
            PricePoint(
                observation_id=f"{target}-{index}",
                observed_at=(as_of - timedelta(days=point_count - index)).isoformat(),
                visible_at=(as_of - timedelta(days=point_count - index) + timedelta(hours=1)).isoformat(),
                value=100 + index * 0.4,
                unit="USD/bbl" if target == "crude" else "USD/mt" if target == "naphtha" else "CNY/mt",
                source_id="test-source",
                source_url="https://example.test/evidence",
            )
            for index in range(point_count)
        )
        return LoadedLabelSeries(points=points, source_matches_label=source_matches_label)

    return load


def test_forecast_grid_is_exactly_21_reference_cells_and_never_formal_without_evaluation() -> None:
    result = build_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_loader(),
    )

    assert result.schema_version == "seven-product-forecast.v1"
    assert result.targets == list(CURRENT_FORMAL_TARGETS)
    assert len(result.cells) == 21
    assert len({(cell.target, cell.horizon_days) for cell in result.cells}) == 21
    assert result.formal_count == 0
    assert result.reference_count == 21
    assert result.unavailable_count == 0
    assert all(cell.formal_eligible is False and cell.evaluation_status == "not_evaluated" for cell in result.cells)
    assert all(cell.point_forecast and cell.interval_low and cell.interval_high for cell in result.cells)
    assert all(cell.interval_low <= cell.point_forecast <= cell.interval_high for cell in result.cells)
    assert all(len(cell.evidence) == CURRENT_NAIVE_SEASONAL_LAG + 1 for cell in result.cells)
    assert all(cell.evidence[0].observation_id.endswith("-34") for cell in result.cells)
    assert all(cell.evidence[-1].observation_id.endswith("-39") for cell in result.cells)


def test_sparse_series_still_returns_complete_grid_with_honest_status() -> None:
    result = build_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_loader(point_count=1),
    )

    assert len(result.cells) == 21
    assert result.unavailable_count == 21
    assert all(cell.formal_status == "insufficient_data" for cell in result.cells)
    assert all(cell.direction == "uncertain" for cell in result.cells)
    assert all(cell.point_forecast == cell.latest_value for cell in result.cells)


def test_missing_series_never_fabricates_a_numeric_result() -> None:
    result = build_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=lambda target, as_of: LoadedLabelSeries(points=(), source_matches_label=False),
    )

    assert result.unavailable_count == 21
    assert all(cell.formal_status == "model_unavailable" for cell in result.cells)
    assert all(cell.point_forecast is None and cell.direction == "uncertain" for cell in result.cells)


def test_meg_without_sunsirs_capture_never_falls_back_to_dce_or_a_formal_proxy(monkeypatch) -> None:
    def forbidden(**_kwargs: object) -> list[object]:
        raise AssertionError("soft-removed DCE history must not feed the current MEG label")

    monkeypatch.setattr(forecast_module, "list_source_capture_revisions", lambda **_kwargs: [])
    monkeypatch.setattr(forecast_module, "list_futures_daily_bars", forbidden)
    monkeypatch.setattr(forecast_module, "list_intraday_price_observations", lambda **_kwargs: [])

    loaded = load_current_label_series("meg", datetime(2026, 9, 1, tzinfo=UTC))

    assert loaded.points == ()
    assert loaded.source_matches_label is False
    assert loaded.data_gaps == (
        "append_only_capture_revision_missing",
        "configured_label_source_has_no_usable_history",
    )


def test_meg_reads_only_the_accepted_sunsirs_capture_revision(monkeypatch) -> None:
    monkeypatch.setattr(
        forecast_module,
        "list_source_capture_revisions",
        lambda **kwargs: [
            {
                "capture_revision_id": "capture-meg-1",
                "source_id": kwargs["source_id"],
                "observed_at": "2026-09-01",
                "visible_at": "2026-09-01T04:00:00+00:00",
                "source_url": "https://www.sunsirs.com/uk/prodetail-222.html",
                "raw_sha256": "a" * 64,
                "canonical_payload": {"last": 5930.0, "unit": "CNY/mt"},
            }
        ],
    )
    monkeypatch.setattr(
        forecast_module,
        "list_futures_daily_bars",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("DCE must not be read")),
    )
    monkeypatch.setattr(
        forecast_module,
        "list_intraday_price_observations",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("proxy must not be read after capture")),
    )

    loaded = load_current_label_series("meg", datetime(2026, 9, 2, tzinfo=UTC))

    assert len(loaded.points) == 1
    assert loaded.points[0].value == 5930.0
    assert loaded.points[0].source_id == "sunsirs_public_commodity_assessment"
    assert loaded.points[0].raw_sha256 == "a" * 64
    assert loaded.source_matches_label is True
    assert loaded.data_gaps == ()


def test_batch_identity_is_stable_for_same_as_of_data_and_configuration() -> None:
    first = build_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_loader(),
    )
    second = build_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_loader(),
    )

    assert first.batch_id == second.batch_id
    assert [cell.data_snapshot_sha256 for cell in first.cells] == [cell.data_snapshot_sha256 for cell in second.cells]


def test_output_affecting_runtime_policy_changes_configuration_and_batch_identity(monkeypatch) -> None:
    as_of = "2026-08-31T08:20:00+00:00"
    baseline = build_seven_product_forecast(as_of_time=as_of, series_loader=_loader())
    baseline_snapshots = [cell.data_snapshot_sha256 for cell in baseline.cells]

    monkeypatch.setattr(forecast_module, "MIN_TREND_POINTS", forecast_module.MIN_TREND_POINTS + 1)
    changed = build_seven_product_forecast(as_of_time=as_of, series_loader=_loader())

    assert [cell.data_snapshot_sha256 for cell in changed.cells] == baseline_snapshots
    assert [cell.configuration_sha256 for cell in changed.cells] != [
        cell.configuration_sha256 for cell in baseline.cells
    ]
    assert changed.batch_id != baseline.batch_id


def test_freshness_policy_changes_configuration_identity_without_changing_snapshot(monkeypatch) -> None:
    as_of = "2026-08-31T08:20:00+00:00"
    baseline = build_seven_product_forecast(as_of_time=as_of, series_loader=_loader())
    freshness = {**forecast_module.FRESHNESS_DAYS, "crude": forecast_module.FRESHNESS_DAYS["crude"] + 1}
    monkeypatch.setattr(forecast_module, "FRESHNESS_DAYS", freshness)

    changed = build_seven_product_forecast(as_of_time=as_of, series_loader=_loader())
    baseline_crude = [cell for cell in baseline.cells if cell.target == "crude"]
    changed_crude = [cell for cell in changed.cells if cell.target == "crude"]

    assert [cell.data_snapshot_sha256 for cell in changed_crude] == [
        cell.data_snapshot_sha256 for cell in baseline_crude
    ]
    assert [cell.configuration_sha256 for cell in changed_crude] != [
        cell.configuration_sha256 for cell in baseline_crude
    ]
    assert changed.batch_id != baseline.batch_id


def test_proxy_source_is_degraded_even_with_sufficient_history() -> None:
    result = build_seven_product_forecast(
        as_of_time=datetime(2026, 8, 31, 8, 20, tzinfo=UTC).isoformat(),
        series_loader=_loader(source_matches_label=False),
    )

    assert all(cell.formal_status == "degraded" for cell in result.cells)
    assert all("active_observation_does_not_match_frozen_label_source" in cell.data_gaps for cell in result.cells)


def test_crude_freshness_matches_daily_futures_cadence_and_flags_a_missed_import() -> None:
    as_of = datetime(2026, 9, 2, 8, 20, tzinfo=UTC)

    def load_with_age(age_days: int):
        def load(target: str, _as_of: datetime) -> LoadedLabelSeries:
            age = age_days if target == "crude" else 1
            points = tuple(
                PricePoint(
                    observation_id=f"{target}-{index}",
                    observed_at=(as_of - timedelta(days=age + 39 - index)).isoformat(),
                    visible_at=(as_of - timedelta(days=max(0, 39 - index))).isoformat(),
                    value=100 + index,
                    unit="USD/bbl" if target == "crude" else "CNY/mt",
                    source_id="yahoo_futures_daily_proxy" if target == "crude" else "test-source",
                    source_url="https://example.test/evidence",
                )
                for index in range(40)
            )
            return LoadedLabelSeries(points=points, source_matches_label=True)

        return load

    fresh = build_seven_product_forecast(as_of_time=as_of.isoformat(), series_loader=load_with_age(3))
    assert all(cell.data_status == "fresh" for cell in fresh.cells if cell.target == "crude")
    assert all(
        not any(gap.startswith("latest_label_is_") for gap in cell.data_gaps)
        for cell in fresh.cells
        if cell.target == "crude"
    )

    stale = build_seven_product_forecast(as_of_time=as_of.isoformat(), series_loader=load_with_age(6))
    stale_crude = [cell for cell in stale.cells if cell.target == "crude"]
    assert all(cell.data_status == "stale" for cell in stale_crude)
    assert all("latest_label_is_6_days_old" in cell.data_gaps for cell in stale_crude)


def test_official_futures_label_reads_append_only_capture_revision_before_projection_table(monkeypatch) -> None:
    monkeypatch.setattr(
        forecast_module,
        "list_source_capture_revisions",
        lambda **_kwargs: [
            {
                "capture_revision_id": "capture-pta-1",
                "source_id": "czce_pta_px",
                "observed_at": "2026-08-28",
                "visible_at": "2026-08-28T08:00:00+00:00",
                "source_url": "https://www.czce.com.cn/official.txt",
                "canonical_payload": {"settle": 5050.0, "close": 5048.0, "unit": "CNY/mt"},
            }
        ],
    )
    monkeypatch.setattr(
        forecast_module,
        "list_futures_daily_bars",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("projection fallback must not be read")),
    )

    loaded = load_current_label_series("pta", datetime(2026, 8, 31, tzinfo=UTC))

    assert loaded.source_matches_label is True
    assert len(loaded.points) == 1
    assert loaded.points[0].observation_id == "capture-pta-1"
    assert loaded.points[0].value == 5050.0


def test_crude_label_reads_yahoo_futures_close_not_eia_capture(monkeypatch) -> None:
    monkeypatch.setattr(
        forecast_module,
        "list_source_capture_revisions",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("crude label no longer reads capture revisions")),
    )
    monkeypatch.setattr(
        forecast_module,
        "list_market_observations",
        lambda **_kwargs: [
            {
                "observation_id": "brent-futures-1",
                "source_id": "yahoo_futures_daily_proxy",
                "observed_at": "2026-08-28",
                "created_at": "2026-08-28T22:35:00+00:00",
                "evidence_url": "https://finance.yahoo.com/quote/BZ=F/",
                "indicator": "Brent futures daily close",
                "value": 99.25,
                "unit": "USD/bbl",
            }
        ],
    )

    loaded = load_current_label_series("crude", datetime(2026, 9, 1, tzinfo=UTC))

    assert loaded.source_matches_label is True
    assert loaded.data_gaps == ()
    assert len(loaded.points) == 1
    assert loaded.points[0].observation_id == "brent-futures-1"
    assert loaded.points[0].value == 99.25
    assert loaded.points[0].unit == "USD/bbl"
    # Settlement requires the frozen identity on every point; market_observations
    # has no such column, so the loader stamps it from the pinned query.
    assert loaded.points[0].semantic_series_id == "crude.brent.futures.yahoo.usd_bbl"
    assert loaded.points[0].contract_version == "seven-product-labels.v5"
    assert len(loaded.points[0].raw_sha256) == 64


def test_crude_label_ignores_eia_spot_rows_after_the_v5_switch(monkeypatch) -> None:
    monkeypatch.setattr(
        forecast_module,
        "list_market_observations",
        lambda **_kwargs: [
            {
                "observation_id": "legacy-eia-brent",
                "source_id": "eia_petroleum_api",
                "observed_at": "2026-08-28",
                "created_at": "2026-08-31T12:00:01+00:00",
                "evidence_url": "https://api.eia.gov/v2/petroleum/pri/spt/data/",
                "indicator": "Europe Brent Spot Price FOB (Dollars per Barrel)",
                "value": 124.5,
                "unit": "$/BBL",
            }
        ],
    )

    loaded = load_current_label_series("crude", datetime(2026, 9, 1, tzinfo=UTC))

    assert len(loaded.points) == 0
    assert loaded.source_matches_label is False


def test_naphtha_label_reads_append_only_capture_revision_before_mutable_projection(monkeypatch) -> None:
    monkeypatch.setattr(
        forecast_module,
        "list_source_capture_revisions",
        lambda **_kwargs: [
            {
                "capture_revision_id": "capture-naphtha-1",
                "source_id": "public_spot_page_refresh",
                "observed_at": "2026-08-28",
                "visible_at": "2026-08-28T09:00:00+00:00",
                "source_url": "https://zh.tradingeconomics.com/commodity/naphtha",
                "raw_sha256": "a" * 64,
                "canonical_payload": {"last": 745.84, "unit": "USD/mt", "raw": {"quote": "石脑油 745.84 2026-08-28"}},
            }
        ],
    )
    monkeypatch.setattr(
        forecast_module,
        "list_intraday_price_observations",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("mutable projection fallback must not be read")),
    )

    loaded = load_current_label_series("naphtha", datetime(2026, 8, 31, tzinfo=UTC))

    assert loaded.source_matches_label is True
    assert loaded.data_gaps == ()
    assert len(loaded.points) == 1
    assert loaded.points[0].observation_id == "capture-naphtha-1"
    assert loaded.points[0].value == 745.84
    assert loaded.points[0].raw_sha256 == "a" * 64


def test_naphtha_mutable_projection_without_capture_revision_is_degraded(monkeypatch) -> None:
    monkeypatch.setattr(forecast_module, "list_source_capture_revisions", lambda **_kwargs: [])
    monkeypatch.setattr(
        forecast_module,
        "list_intraday_price_observations",
        lambda **_kwargs: [
            {
                "observation_id": "legacy-projection",
                "source_id": "public_spot_page_refresh",
                "observed_at": "2026-08-28",
                "created_at": "2026-08-28T09:00:00+00:00",
                "source_url": "https://zh.tradingeconomics.com/commodity/naphtha",
                "last": 745.84,
                "unit": "USD/mt",
                "raw": {"quote": "石脑油 745.84 2026-08-28"},
            }
        ],
    )

    loaded = load_current_label_series("naphtha", datetime(2026, 8, 31, tzinfo=UTC))

    assert len(loaded.points) == 1
    assert loaded.source_matches_label is False
    assert "append_only_capture_revision_missing" in loaded.data_gaps


def test_poy_label_reads_append_only_capture_revision_before_mutable_projection(monkeypatch) -> None:
    monkeypatch.setattr(
        forecast_module,
        "list_source_capture_revisions",
        lambda **_kwargs: [
            {
                "capture_revision_id": "capture-poy-1",
                "source_id": "tnc_polyester_history",
                "observed_at": "2026-08-28",
                "visible_at": "2026-08-28T09:00:00+00:00",
                "source_url": "https://www.tnc.com.cn/market/average-price-d92.html",
                "raw_sha256": "c" * 64,
                "canonical_payload": {"value": 7_150.0, "unit": "CNY/mt"},
            }
        ],
    )
    monkeypatch.setattr(
        forecast_module,
        "list_market_observations",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("mutable projection fallback must not be read")),
    )

    loaded = load_current_label_series("poy", datetime(2026, 8, 31, tzinfo=UTC))

    assert loaded.source_matches_label is True
    assert loaded.data_gaps == ()
    assert len(loaded.points) == 1
    assert loaded.points[0].observation_id == "capture-poy-1"
    assert loaded.points[0].value == 7_150.0
    assert loaded.points[0].raw_sha256 == "c" * 64


def test_dty_mutable_projection_without_capture_revision_is_degraded(monkeypatch) -> None:
    monkeypatch.setattr(forecast_module, "list_source_capture_revisions", lambda **_kwargs: [])
    monkeypatch.setattr(
        forecast_module,
        "list_market_observations",
        lambda **_kwargs: [
            {
                "observation_id": "legacy-dty-projection",
                "source_id": "tnc_polyester_history",
                "observed_at": "2026-08-28",
                "created_at": "2026-08-28T09:00:00+00:00",
                "evidence_url": "https://www.tnc.com.cn/market/average-price-d94.html",
                "value": 8_350.0,
                "unit": "CNY/mt",
            }
        ],
    )

    loaded = load_current_label_series("dty", datetime(2026, 8, 31, tzinfo=UTC))

    assert len(loaded.points) == 1
    assert loaded.source_matches_label is False
    assert "append_only_capture_revision_missing" in loaded.data_gaps

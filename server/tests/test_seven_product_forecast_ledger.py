from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import seven_product_forecast_ledger as ledger_module
from app import storage
from app.main import seven_product_forecast
from app.settings import settings
from app.seven_product_contract import (
    CRUDE_EIA_SPOT_LABEL_REGISTRY_VERSION,
    CRUDE_EIA_SPOT_SERIES_ID,
    CRUDE_EIA_SPOT_SOURCE_ID,
    CURRENT_LABEL_REGISTRY_VERSION,
    LABEL_REGISTRY,
)
from app.seven_product_forecast import LoadedLabelSeries, PricePoint, build_seven_product_forecast
from app.seven_product_forecast_ledger import (
    SevenProductForecastLedgerError,
    _reference_runtime_history,
    list_seven_product_forecast_history,
    record_daily_seven_product_forecast,
    save_seven_product_forecast_batch,
    settle_pending_seven_product_forecasts,
)


@pytest.fixture
def isolated_database(tmp_path: Path):
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "ledger.db"))
    storage._MIGRATED_PATHS.clear()
    try:
        with closing(storage.connect()):
            pass
        yield tmp_path / "ledger.db"
    finally:
        storage._MIGRATED_PATHS.clear()
        object.__setattr__(settings, "sqlite_path", original)


def _issue_loader(target: str, as_of: datetime) -> LoadedLabelSeries:
    unit = LABEL_REGISTRY[target].unit
    points = tuple(
        PricePoint(
            observation_id=f"{target}-issue-{index}",
            observed_at=(as_of - timedelta(days=40 - index)).isoformat(),
            visible_at=(as_of - timedelta(days=40 - index) + timedelta(hours=1)).isoformat(),
            value=100 + index * 0.4,
            unit=unit,
            source_id=LABEL_REGISTRY[target].source_id,
            source_url=f"https://example.test/{target}/{index}",
            raw_sha256=f"{index % 10}" * 64,
            semantic_series_id=LABEL_REGISTRY[target].series_id,
            contract_version=CURRENT_LABEL_REGISTRY_VERSION,
        )
        for index in range(40)
    )
    return LoadedLabelSeries(points=points, source_matches_label=True)


def _settlement_loader(target: str, as_of: datetime) -> LoadedLabelSeries:
    issue_as_of = datetime(2026, 8, 31, 8, 20, tzinfo=UTC)
    historical = list(_issue_loader(target, issue_as_of).points)
    future = [
        PricePoint(
            observation_id=f"{target}-actual-{index}",
            observed_at=(issue_as_of + timedelta(days=index)).isoformat(),
            visible_at=(issue_as_of + timedelta(days=index, hours=1)).isoformat(),
            value=116 + index * 0.3,
            unit=LABEL_REGISTRY[target].unit,
            source_id=LABEL_REGISTRY[target].source_id,
            source_url=f"https://example.test/{target}/actual/{index}",
            raw_sha256=f"{(index + 3) % 10}" * 64,
            semantic_series_id=LABEL_REGISTRY[target].series_id,
            contract_version=CURRENT_LABEL_REGISTRY_VERSION,
        )
        for index in range(1, 31)
    ]
    return LoadedLabelSeries(points=tuple([*historical, *future]), source_matches_label=True)


def _date_only_settlement_loader(target: str, as_of: datetime) -> LoadedLabelSeries:
    loaded = _settlement_loader(target, as_of)
    return LoadedLabelSeries(
        points=tuple(
            PricePoint(
                observation_id=point.observation_id,
                observed_at=point.observed_at[:10],
                visible_at=point.visible_at,
                value=point.value,
                unit=point.unit,
                source_id=point.source_id,
                source_url=point.source_url,
                raw_sha256=point.raw_sha256,
                semantic_series_id=point.semantic_series_id,
                contract_version=point.contract_version,
            )
            for point in loaded.points
        ),
        source_matches_label=True,
    )


def test_v36_migration_creates_immutable_forecast_ledger(isolated_database: Path) -> None:
    batch = record_daily_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_issue_loader,
    )
    with closing(sqlite3.connect(isolated_database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        with pytest.raises(sqlite3.IntegrityError, match="seven_product_forecast_batch_immutable"):
            connection.execute(
                """
                UPDATE seven_product_forecast_batches SET persisted_at='changed'
                WHERE batch_id=?
                """,
                (batch.batch_id,),
            )


def test_legal_v34_database_upgrades_once_with_verified_backup(isolated_database: Path) -> None:
    with closing(sqlite3.connect(isolated_database)) as connection, connection:
        connection.execute("DROP TABLE seven_product_forecast_outcome_invalidations")
        connection.execute("DROP TABLE seven_product_forecast_outcomes")
        connection.execute("DROP TABLE seven_product_forecast_cells")
        connection.execute("DROP TABLE seven_product_forecast_batches")
        connection.execute("DROP TABLE intelligence_feedback")
        connection.execute("DROP TABLE intelligence_runs")
        connection.execute("DROP TABLE intelligence_daily_briefs")
        connection.execute("DROP TABLE intelligence_event_evidence")
        connection.execute("DROP TABLE intelligence_event_revisions")
        connection.execute("DROP TABLE intelligence_item_revisions")
        connection.execute("DROP TABLE intelligence_search_fts")
        connection.execute("DROP TABLE IF EXISTS agent_lessons")
        connection.execute("DROP TABLE IF EXISTS forecast_event_factors")
        connection.execute("DROP TABLE IF EXISTS event_agent_analyses")
        connection.execute("DROP TABLE IF EXISTS agent_chain_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version=39")
        connection.execute("DELETE FROM schema_migrations WHERE version=38")
        connection.execute("DELETE FROM schema_migrations WHERE version=37")
        connection.execute("DELETE FROM schema_migrations WHERE version=36")
        connection.execute("DELETE FROM schema_migrations WHERE version=35")
        connection.execute("PRAGMA user_version=34")
    storage._MIGRATED_PATHS.clear()

    with closing(storage.connect()) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=35").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=36").fetchone()[0] == 1
        storage._validate_seven_product_forecast_ledger_schema(connection)

    backups = list(
        (isolated_database.parent / "migration-backups").glob(
            f"{isolated_database.stem}.pre-migration.v34-to-v39.*.sqlite"
        )
    )
    assert len(backups) == 1


def test_daily_record_is_exactly_once_and_reaudited(isolated_database: Path) -> None:
    first = record_daily_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_issue_loader,
    )
    replay = record_daily_seven_product_forecast(
        as_of_time="2026-08-31T12:00:00+00:00",
        series_loader=lambda *_args: (_ for _ in ()).throw(AssertionError("retry must not recompute")),
    )

    assert replay == first
    assert first.business_date == "2026-08-31"
    assert len(first.cells) == 21
    assert {cell.settlement_status for cell in first.cells} == {"pending"}
    with closing(storage.connect_readonly()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM seven_product_forecast_batches").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM seven_product_forecast_cells").fetchone()[0] == 21


def test_default_current_forecast_reads_the_frozen_batch(isolated_database: Path) -> None:
    issued = record_daily_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_issue_loader,
    )

    current = seven_product_forecast()

    assert current.batch_id == issued.batch_id
    assert current.generated_at == issued.generated_at
    assert [cell.point_forecast for cell in current.cells] == [cell.forecast.point_forecast for cell in issued.cells]


def test_settlement_uses_later_visible_observations_and_is_idempotent(isolated_database: Path) -> None:
    issued = record_daily_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_issue_loader,
    )

    assert all(len(cell.forecast.evidence) == 6 for cell in issued.cells)
    assert all(cell.forecast.evidence[0].value == pytest.approx(113.6) for cell in issued.cells)
    assert all(cell.forecast.evidence[-1].value == pytest.approx(115.6) for cell in issued.cells)

    first = settle_pending_seven_product_forecasts(
        evaluation_as_of="2026-10-15T08:20:00+00:00",
        series_loader=_settlement_loader,
    )
    second = settle_pending_seven_product_forecasts(
        evaluation_as_of="2026-10-15T08:20:00+00:00",
        series_loader=_settlement_loader,
    )
    history = list_seven_product_forecast_history()

    assert first["status"] == "ready"
    assert first["inserted"] == 21
    assert second["pending_cell_count"] == 0
    assert second["inserted"] == 0
    assert {cell.settlement_status for cell in history[0].cells} == {"scored"}
    assert all(cell.outcome and cell.outcome.actual_visible_at > history[0].as_of_time for cell in history[0].cells)

    runtime_history = _reference_runtime_history(history)
    crude_d1 = runtime_history[("crude", 1)][0]
    assert crude_d1["persistence_absolute_error"] == pytest.approx(0.7)
    assert crude_d1["seasonal_naive_absolute_error"] == pytest.approx(2.7)
    assert crude_d1["best_naive_absolute_error"] == pytest.approx(0.7)
    assert crude_d1["best_naive_model_version"] == "persistence.v1"

    crude_cell = next(
        cell
        for cell in history[0].cells
        if cell.forecast.target == "crude" and cell.forecast.horizon_days == 1
    )
    assert crude_cell.outcome is not None
    seasonal_actual = 113.8
    seasonal_outcome = crude_cell.outcome.model_copy(
        update={
            "actual_value": seasonal_actual,
            "absolute_error": abs(crude_cell.forecast.point_forecast - seasonal_actual),
        }
    )
    seasonal_cells = [
        cell.model_copy(update={"outcome": seasonal_outcome}) if cell.cell_id == crude_cell.cell_id else cell
        for cell in history[0].cells
    ]
    seasonal_history = _reference_runtime_history(
        [history[0].model_copy(update={"cells": seasonal_cells})]
    )
    seasonal_winner = seasonal_history[("crude", 1)][0]
    assert seasonal_winner["persistence_absolute_error"] == pytest.approx(1.8)
    assert seasonal_winner["seasonal_naive_absolute_error"] == pytest.approx(0.2)
    assert seasonal_winner["best_naive_model_version"] == "seasonal-naive-lag5.v1"

    legacy_cells = [
        cell.model_copy(
            update={
                "forecast": cell.forecast.model_copy(update={"evidence": cell.forecast.evidence[-3:]})
            }
        )
        for cell in history[0].cells
    ]
    legacy_history = _reference_runtime_history([history[0].model_copy(update={"cells": legacy_cells})])
    assert legacy_history[("crude", 1)][0]["seasonal_naive_absolute_error"] is None
    assert legacy_history[("crude", 1)][0]["best_naive_absolute_error"] is None

    incomplete_seasonal_evidence = crude_cell.forecast.evidence[0].model_copy(
        update={"raw_sha256": ""}
    )
    incomplete_forecast = crude_cell.forecast.model_copy(
        update={
            "evidence": [incomplete_seasonal_evidence, *crude_cell.forecast.evidence[1:]],
        }
    )
    incomplete_cells = [
        cell.model_copy(update={"forecast": incomplete_forecast})
        if cell.cell_id == crude_cell.cell_id
        else cell
        for cell in history[0].cells
    ]
    incomplete_history = _reference_runtime_history(
        [history[0].model_copy(update={"cells": incomplete_cells})]
    )
    assert incomplete_history[("crude", 1)][0]["seasonal_naive_absolute_error"] is None
    assert incomplete_history[("crude", 1)][0]["best_naive_absolute_error"] is None


def test_settlement_accepts_official_daily_date_observation_identity(isolated_database: Path) -> None:
    def issue_loader(target: str, as_of: datetime) -> LoadedLabelSeries:
        loaded = _issue_loader(target, as_of)
        return LoadedLabelSeries(
            points=tuple(
                PricePoint(
                    observation_id=point.observation_id,
                    observed_at=point.observed_at[:10],
                    visible_at=point.visible_at,
                    value=point.value,
                    unit=point.unit,
                    source_id=point.source_id,
                    source_url=point.source_url,
                    raw_sha256=point.raw_sha256,
                    semantic_series_id=point.semantic_series_id,
                    contract_version=point.contract_version,
                )
                for point in loaded.points
            ),
            source_matches_label=True,
        )

    record_daily_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=issue_loader,
    )

    result = settle_pending_seven_product_forecasts(
        evaluation_as_of="2026-10-15T08:20:00+00:00",
        series_loader=_date_only_settlement_loader,
    )

    assert result["status"] == "ready"
    assert result["inserted"] == 21


def test_v36_migration_appends_cross_contract_outcome_invalidation(isolated_database: Path) -> None:
    record_daily_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_issue_loader,
    )
    settle_pending_seven_product_forecasts(
        evaluation_as_of="2026-10-15T08:20:00+00:00",
        series_loader=_settlement_loader,
    )
    with closing(sqlite3.connect(isolated_database)) as connection, connection:
        connection.row_factory = sqlite3.Row
        actual = connection.execute(
            """
            SELECT o.* FROM seven_product_forecast_outcomes AS o
            WHERE o.target='meg' AND o.horizon_days=1
            """
        ).fetchone()
        assert actual is not None
        storage.append_source_capture_revision_with_connection(
            connection,
            capture_revision_id=str(actual["actual_observation_id"]),
            source_id="sunsirs_public_commodity_assessment",
            semantic_series_id="meg.sunsirs.china.spot_assessment.cny_mt",
            observed_at=str(actual["actual_observed_at"]),
            published_at=str(actual["actual_visible_at"]),
            visible_at=str(actual["actual_visible_at"]),
            captured_at=str(actual["actual_visible_at"]),
            source_url=str(actual["actual_source_url"]),
            raw_sha256=str(actual["actual_raw_sha256"]),
            authorization_scope="public_personal_noncommercial",
            contract_version="seven-product-labels.v3",
            parser_version="test.v1",
            canonical_payload={"observation_id": str(actual["actual_observation_id"])},
        )
        cell = connection.execute(
            """
            SELECT cell_id,cell_payload FROM seven_product_forecast_cells
            WHERE target='meg' AND horizon_days=1
            """
        ).fetchone()
        payload = json.loads(str(cell["cell_payload"]))
        payload["label_series_id"] = "meg.dce.main_continuous.settlement.cny_mt"
        payload["label_registry_version"] = "seven-product-labels.v1"
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        connection.execute("DROP TRIGGER trg_seven_product_forecast_cells_no_update")
        connection.execute(
            """
            UPDATE seven_product_forecast_cells
            SET label_series_id=?,label_registry_version=?,cell_payload=?,cell_sha256=?
            WHERE cell_id=?
            """,
            (
                payload["label_series_id"],
                payload["label_registry_version"],
                canonical,
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                str(cell["cell_id"]),
            ),
        )
        connection.execute(
            """
            CREATE TRIGGER trg_seven_product_forecast_cells_no_update
            BEFORE UPDATE ON seven_product_forecast_cells
            BEGIN SELECT RAISE(ABORT, 'seven_product_forecast_cell_immutable'); END
            """
        )
        connection.execute("DROP TABLE intelligence_feedback")
        connection.execute("DROP TABLE intelligence_runs")
        connection.execute("DROP TABLE intelligence_daily_briefs")
        connection.execute("DROP TABLE intelligence_event_evidence")
        connection.execute("DROP TABLE intelligence_event_revisions")
        connection.execute("DROP TABLE intelligence_item_revisions")
        connection.execute("DROP TABLE intelligence_search_fts")
        connection.execute("DROP TABLE IF EXISTS agent_lessons")
        connection.execute("DROP TABLE IF EXISTS forecast_event_factors")
        connection.execute("DROP TABLE IF EXISTS event_agent_analyses")
        connection.execute("DROP TABLE IF EXISTS agent_chain_runs")
        connection.execute("DELETE FROM schema_migrations WHERE version=39")
        connection.execute("DELETE FROM schema_migrations WHERE version=38")
        connection.execute("DELETE FROM schema_migrations WHERE version=37")
        connection.execute("DROP TABLE seven_product_forecast_outcome_invalidations")
        connection.execute("DELETE FROM schema_migrations WHERE version=36")
        connection.execute("PRAGMA user_version=35")

    storage._MIGRATED_PATHS.clear()
    with closing(storage.connect()) as connection:
        row = connection.execute("SELECT * FROM seven_product_forecast_outcome_invalidations").fetchone()
        assert row is not None
        assert row["reason"] == "contract_mismatch"
        assert row["expected_source_id"] == "dce_meg"
        assert row["actual_source_id"] == "sunsirs_public_commodity_assessment"

    history = list_seven_product_forecast_history()
    invalidated = [cell for cell in history[0].cells if cell.settlement_status == "invalidated_contract_mismatch"]
    assert len(invalidated) == 1
    assert invalidated[0].invalidation is not None
    assert invalidated[0].invalidation.reason == "contract_mismatch"
    runtime_history = _reference_runtime_history(history)
    meg_d1 = runtime_history[("meg", 1)]
    assert len(meg_d1) == 1
    assert meg_d1[0]["invalidated"] is True


def test_unmature_source_mismatch_stays_pending_until_an_actual_exists(isolated_database: Path) -> None:
    issue_at = datetime(2026, 8, 31, 8, 20, tzinfo=UTC)
    record_daily_seven_product_forecast(
        as_of_time=issue_at.isoformat(),
        series_loader=_issue_loader,
    )

    def unavailable_formal_source(target: str, as_of: datetime) -> LoadedLabelSeries:
        return LoadedLabelSeries(
            points=_issue_loader(target, issue_at).points,
            source_matches_label=False,
        )

    result = settle_pending_seven_product_forecasts(
        evaluation_as_of=(issue_at + timedelta(hours=1)).isoformat(),
        series_loader=unavailable_formal_source,
    )

    assert result["status"] == "ready"
    assert result["pending_maturity"] == 21
    assert result["blocked_data_quality"] == 0


def test_settlement_rejects_actual_that_was_visible_at_issue(isolated_database: Path) -> None:
    record_daily_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_issue_loader,
    )

    def leaky_loader(target: str, as_of: datetime) -> LoadedLabelSeries:
        loaded = _settlement_loader(target, as_of)
        future = tuple(
            PricePoint(
                observation_id=point.observation_id,
                observed_at=point.observed_at,
                visible_at="2026-08-31T08:00:00+00:00",
                value=point.value,
                unit=point.unit,
                source_id=point.source_id,
                source_url=point.source_url,
                raw_sha256=point.raw_sha256,
                semantic_series_id=point.semantic_series_id,
                contract_version=point.contract_version,
            )
            for point in loaded.points[-30:]
        )
        return LoadedLabelSeries(points=tuple([*loaded.points[:-30], *future]), source_matches_label=True)

    result = settle_pending_seven_product_forecasts(
        evaluation_as_of="2026-10-15T08:20:00+00:00",
        series_loader=leaky_loader,
    )

    assert result["status"] == "blocked"
    assert result["blocked_data_quality"] == 21
    assert result["inserted"] == 0
    assert all("actual_was_visible_at_forecast_cutoff" in reason for reason in result["blocked_reasons"])


def test_history_fails_closed_after_payload_tampering(isolated_database: Path) -> None:
    batch = record_daily_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_issue_loader,
    )
    with closing(sqlite3.connect(isolated_database)) as connection:
        connection.execute("DROP TRIGGER trg_seven_product_forecast_cells_no_update")
        connection.execute(
            "UPDATE seven_product_forecast_cells SET cell_payload='{}' WHERE cell_id=?",
            (batch.cells[0].cell_id,),
        )
        connection.execute(
            """
            CREATE TRIGGER trg_seven_product_forecast_cells_no_update
            BEFORE UPDATE ON seven_product_forecast_cells
            BEGIN SELECT RAISE(ABORT, 'seven_product_forecast_cell_immutable'); END
            """
        )
        connection.commit()

    with pytest.raises(SevenProductForecastLedgerError, match="seven_product_payload_hash_mismatch"):
        list_seven_product_forecast_history()


def _crude_eia_issue_loader(target: str, as_of: datetime) -> LoadedLabelSeries:
    loaded = _issue_loader(target, as_of)
    if target != "crude":
        return loaded
    return LoadedLabelSeries(
        points=tuple(
            replace(
                point,
                source_id=CRUDE_EIA_SPOT_SOURCE_ID,
                semantic_series_id=CRUDE_EIA_SPOT_SERIES_ID,
                contract_version=CRUDE_EIA_SPOT_LABEL_REGISTRY_VERSION,
            )
            for point in loaded.points
        ),
        source_matches_label=True,
    )


def _identity_aware_loader(target: str, as_of: datetime, *, series_id: str) -> LoadedLabelSeries:
    retired = series_id == CRUDE_EIA_SPOT_SERIES_ID
    loaded = _settlement_loader(target, as_of)
    return LoadedLabelSeries(
        points=tuple(
            replace(
                point,
                source_id=CRUDE_EIA_SPOT_SOURCE_ID if retired else LABEL_REGISTRY[target].source_id,
                semantic_series_id=series_id,
                contract_version=(
                    CRUDE_EIA_SPOT_LABEL_REGISTRY_VERSION if retired else CURRENT_LABEL_REGISTRY_VERSION
                ),
            )
            for point in loaded.points
        ),
        source_matches_label=True,
    )


def _store_pre_v5_crude_batch() -> None:
    batch = build_seven_product_forecast(
        as_of_time="2026-08-31T08:20:00+00:00",
        series_loader=_crude_eia_issue_loader,
    )
    cells = [
        cell.model_copy(
            update={
                "label_registry_version": CRUDE_EIA_SPOT_LABEL_REGISTRY_VERSION,
                "label_series_id": CRUDE_EIA_SPOT_SERIES_ID,
            }
        )
        if cell.target == "crude"
        else cell
        for cell in batch.cells
    ]
    save_seven_product_forecast_batch(batch.model_copy(update={"cells": cells}))


def test_pre_v5_crude_contract_settles_on_the_retired_eia_series(isolated_database: Path) -> None:
    """A v4 crude forecast is scored against the series its contract named."""

    _store_pre_v5_crude_batch()

    result = settle_pending_seven_product_forecasts(
        evaluation_as_of="2026-10-15T08:20:00+00:00",
        series_loader=_identity_aware_loader,
    )

    assert result["status"] == "ready"
    assert result["blocked_data_quality"] == 0
    assert result["inserted"] == 21
    history = list_seven_product_forecast_history()
    crude_cells = [cell for cell in history[0].cells if cell.forecast.target == "crude"]
    assert len(crude_cells) == 3
    assert {cell.settlement_status for cell in crude_cells} == {"scored"}


def test_pre_v5_crude_contract_blocks_the_replacement_series(isolated_database: Path) -> None:
    """Scoring a v4 crude forecast on the v5 futures series stays blocked."""

    _store_pre_v5_crude_batch()

    result = settle_pending_seven_product_forecasts(
        evaluation_as_of="2026-10-15T08:20:00+00:00",
        series_loader=_settlement_loader,
    )

    assert result["status"] == "blocked"
    assert result["blocked_data_quality"] == 3
    assert result["inserted"] == 18
    assert all("label_series_identity_mismatch" in reason for reason in result["blocked_reasons"])


def test_default_settlement_dispatches_each_frozen_identity(isolated_database, monkeypatch):
    """Exercise the production default, not an injected series_loader bypass."""
    _store_pre_v5_crude_batch()
    record_daily_seven_product_forecast(
        as_of_time="2026-09-01T08:20:00+00:00", series_loader=_issue_loader,
    )
    calls = []

    def identity_loader(target, as_of, *, series_id):
        calls.append((target, series_id))
        return _identity_aware_loader(target, as_of, series_id=series_id)

    monkeypatch.setattr(ledger_module, "load_label_series_for_identity", identity_loader)
    result = settle_pending_seven_product_forecasts(evaluation_as_of="2026-10-15T08:20:00+00:00")
    assert result["inserted"] == 42
    assert result["blocked_data_quality"] == 0
    assert ("crude", CRUDE_EIA_SPOT_SERIES_ID) in calls
    assert ("crude", LABEL_REGISTRY["crude"].series_id) in calls
    assert len(calls) == 8  # Seven current series plus the retired EIA series.

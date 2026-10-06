from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from inspect import signature
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    SevenProductForecastBatch,
    SevenProductForecastCell,
    SevenProductForecastLedgerBatch,
    SevenProductForecastLedgerCell,
    SevenProductForecastOutcome,
    SevenProductForecastOutcomeInvalidation,
)
from .seven_product_contract import (
    CURRENT_FORMAL_CELL_COUNT,
    CURRENT_FORMAL_HORIZONS,
    CURRENT_FORMAL_TARGETS,
    CURRENT_NAIVE_SEASONAL_LAG,
    frozen_label_identity,
)
from .seven_product_forecast import (
    LoadedLabelSeries,
    SeriesLoader,
    build_seven_product_forecast,
    load_label_series_for_identity,
)
from .storage import connect, connect_readonly

LEDGER_TIMEZONE = ZoneInfo("Asia/Shanghai")
OUTCOME_SCHEMA_VERSION = "seven-product-forecast-outcome.v1"


class SevenProductForecastLedgerError(ValueError):
    """Stable, bounded failure for live forecast persistence and settlement."""


def record_daily_seven_product_forecast(
    *,
    as_of_time: str | None = None,
    series_loader: SeriesLoader | None = None,
    model_registry: dict[str, Any] | None = None,
    main_contract: bool = False,
) -> SevenProductForecastLedgerBatch:
    if os.environ.get("PREDICTION_WRITES_PAUSED") == "1":
        raise SevenProductForecastLedgerError("prediction_writes_paused")
    as_of = _timestamp(as_of_time) if as_of_time else datetime.now(UTC)
    business_date = as_of.astimezone(LEDGER_TIMEZONE).date().isoformat()
    existing = get_seven_product_forecast_batch(business_date=business_date)
    if existing is not None:
        return existing
    snapshot = None
    if main_contract:
        from .prediction_main import capture_main_inputs

        age = (datetime.now(UTC) - as_of).total_seconds()
        if age < 0 or age > 300:
            raise SevenProductForecastLedgerError("main_prediction_requires_current_issue_cutoff")
        snapshot = capture_main_inputs(as_of)
        snapshot.persist()
        series_loader = snapshot.load
    runtime_model_history = _reference_runtime_history(list_seven_product_forecast_history(limit=366))
    batch = build_seven_product_forecast(
        as_of_time=as_of.isoformat(),
        series_loader=series_loader,
        model_registry=model_registry,
        runtime_model_history=runtime_model_history,
        forecast_contract="issue-calendar.v1" if main_contract else "observation-horizon.v1",
        candidate_builder=snapshot.candidates if snapshot else None,
        input_snapshot_sha256=snapshot.sha256 if snapshot else None,
    )
    if (main_contract
        and datetime.now(UTC).astimezone(LEDGER_TIMEZONE).date() != as_of.astimezone(LEDGER_TIMEZONE).date()
    ):
        raise SevenProductForecastLedgerError("main_prediction_issue_crossed_business_date")
    return save_seven_product_forecast_batch(batch)


def save_seven_product_forecast_batch(
    batch: SevenProductForecastBatch, *, event_factor_rows: list[dict[str, Any]] | None = None,
) -> SevenProductForecastLedgerBatch:
    if os.environ.get("PREDICTION_WRITES_PAUSED") == "1":
        raise SevenProductForecastLedgerError("prediction_writes_paused")
    prepared = _prepare_batch(batch)
    try:
        with closing(connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT payload_sha256 FROM seven_product_forecast_batches WHERE batch_id=?",
                (batch.batch_id,),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_sha256"]) != prepared["payload_sha256"]:
                    raise SevenProductForecastLedgerError("seven_product_batch_identity_conflict")
                if event_factor_rows is not None:
                    stored = connection.execute(
                        "SELECT target,horizon_days,business_date,baseline_direction,"
                        "event_adjusted_direction,fusion_rule "
                        "FROM forecast_event_factors WHERE batch_id=?", (batch.batch_id,),
                    ).fetchall()
                    expected_audit = {(row["target"], int(row["horizon_days"]), row["business_date"],
                                       row["baseline_direction"], row["event_adjusted_direction"], row["fusion_rule"])
                                      for row in event_factor_rows}
                    if len(event_factor_rows) != len(batch.cells) or len(stored) != len(batch.cells) or {
                        tuple(row) for row in stored
                    } != expected_audit:
                        raise SevenProductForecastLedgerError("seven_product_existing_batch_audit_mismatch")
                result = _load_batch_locked(connection, batch_id=batch.batch_id)
                connection.commit()
                return result
            same_day = connection.execute(
                "SELECT batch_id FROM seven_product_forecast_batches WHERE business_date=?",
                (prepared["business_date"],),
            ).fetchone()
            if same_day is not None:
                raise SevenProductForecastLedgerError("seven_product_business_date_conflict")
            connection.execute(
                """
                INSERT INTO seven_product_forecast_batches(
                  batch_id,business_date,schema_version,as_of_time,generated_at,persisted_at,
                  model_registry_revision,data_snapshot_sha256,configuration_sha256,
                  formal_count,reference_count,unavailable_count,contract_complete,payload,payload_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                prepared["batch_row"],
            )
            connection.executemany(
                """
                INSERT INTO seven_product_forecast_cells(
                  cell_id,batch_id,target,horizon_days,label_series_id,model_version,feature_version,
                  label_registry_version,neutral_band_policy_version,evaluation_status,evaluation_id,
                  evaluation_result_sha256,model_registry_revision,origin_observation_id,
                  origin_observed_at,origin_visible_at,point_forecast,neutral_band_pct,
                  predicted_direction,unit,data_snapshot_sha256,configuration_sha256,cell_payload,cell_sha256
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                prepared["cell_rows"],
            )
            if event_factor_rows is not None:
                from .event_fusion import store_event_factor_rows

                expected = {(cell.target, cell.horizon_days) for cell in batch.cells}
                actual = {(row["target"], int(row["horizon_days"])) for row in event_factor_rows}
                if len(event_factor_rows) != len(expected) or actual != expected:
                    raise SevenProductForecastLedgerError("seven_product_fusion_audit_grid_mismatch")
                issued = {(cell.target, cell.horizon_days): cell.direction for cell in batch.cells}
                if any(row["batch_id"] != batch.batch_id
                       or row["business_date"] != prepared["business_date"]
                       or row["event_adjusted_direction"] != issued[(row["target"], int(row["horizon_days"]))]
                       for row in event_factor_rows):
                    raise SevenProductForecastLedgerError("seven_product_fusion_audit_identity_mismatch")
                store_event_factor_rows(event_factor_rows, connection=connection)
            result = _load_batch_locked(connection, batch_id=batch.batch_id)
            connection.commit()
            return result
    except SevenProductForecastLedgerError:
        raise
    except (sqlite3.Error, ValueError) as exc:
        raise SevenProductForecastLedgerError("seven_product_forecast_persistence_failed") from exc


def get_seven_product_forecast_batch(
    *,
    batch_id: str | None = None,
    business_date: str | None = None,
) -> SevenProductForecastLedgerBatch | None:
    if (batch_id is None) == (business_date is None):
        raise ValueError("provide_exactly_one_batch_selector")
    try:
        with closing(connect_readonly()) as connection:
            if batch_id is not None:
                row = connection.execute(
                    "SELECT batch_id FROM seven_product_forecast_batches WHERE batch_id=?",
                    (batch_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT batch_id FROM seven_product_forecast_batches WHERE business_date=?",
                    (business_date,),
                ).fetchone()
            return None if row is None else _load_batch_locked(connection, batch_id=str(row["batch_id"]))
    except SevenProductForecastLedgerError:
        raise
    except (sqlite3.Error, ValueError) as exc:
        if "readonly_database_unavailable" in str(exc):
            return None
        raise SevenProductForecastLedgerError("seven_product_forecast_audit_failed") from exc


def list_seven_product_forecast_history(*, limit: int = 30) -> list[SevenProductForecastLedgerBatch]:
    bounded_limit = max(1, min(int(limit), 366))
    try:
        with closing(connect_readonly()) as connection:
            rows = connection.execute(
                """
                SELECT batch_id FROM seven_product_forecast_batches
                ORDER BY business_date DESC,as_of_time DESC LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
            return [_load_batch_locked(connection, batch_id=str(row["batch_id"])) for row in rows]
    except SevenProductForecastLedgerError:
        raise
    except (sqlite3.Error, ValueError) as exc:
        raise SevenProductForecastLedgerError("seven_product_forecast_audit_failed") from exc


def _reference_runtime_history(
    batches: list[SevenProductForecastLedgerBatch],
) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Project immutable outcomes into the best-naive champion-loss contract."""

    history: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for batch in sorted(batches, key=lambda item: item.as_of_time):
        for cell in batch.cells:
            if cell.outcome is None or cell.forecast.latest_value is None:
                continue
            forecast = cell.forecast
            if forecast.forecast_contract != "observation-horizon.v1":
                continue  # Never blend different horizon contracts into legacy champion history.
            key = (forecast.target, forecast.horizon_days)
            persistence_error = abs(forecast.latest_value - cell.outcome.actual_value)
            seasonal_value = _seasonal_naive_issue_value(forecast)
            seasonal_error = (
                abs(seasonal_value - cell.outcome.actual_value) if seasonal_value is not None else None
            )
            if seasonal_error is None:
                best_naive_error = None
                best_naive_model_version = None
            elif persistence_error <= seasonal_error:
                best_naive_error = persistence_error
                best_naive_model_version = "persistence.v1"
            else:
                best_naive_error = seasonal_error
                best_naive_model_version = f"seasonal-naive-lag{CURRENT_NAIVE_SEASONAL_LAG}.v1"
            history.setdefault(key, []).append(
                {
                    "batch_id": batch.batch_id,
                    "model_version": forecast.model_version,
                    "model_absolute_error": cell.outcome.absolute_error,
                    "persistence_absolute_error": persistence_error,
                    "seasonal_naive_absolute_error": seasonal_error,
                    "best_naive_absolute_error": best_naive_error,
                    "best_naive_model_version": best_naive_model_version,
                    "invalidated": cell.invalidation is not None,
                }
            )
    return history


def _seasonal_naive_issue_value(forecast: SevenProductForecastCell) -> float | None:
    """Return only a seasonal baseline that was frozen in the issued cell.

    Forecasts issued before the lag-5 evidence contract retained only three
    evidence rows.  Do not reconstruct their missing issue-time baseline from
    today's database because that would destroy point-in-time auditability.
    """

    if forecast.history_points < 1:
        return None
    required = min(forecast.history_points, CURRENT_NAIVE_SEASONAL_LAG + 1)
    if len(forecast.evidence) < required:
        return None
    index = -(CURRENT_NAIVE_SEASONAL_LAG + 1) if forecast.history_points > CURRENT_NAIVE_SEASONAL_LAG else 0
    evidence = forecast.evidence[index]
    try:
        identity = frozen_label_identity(
            target=forecast.target,
            registry_version=forecast.label_registry_version,
            series_id=forecast.label_series_id,
        )
        issue_as_of = _timestamp(forecast.as_of_time)
        observed_at = _observation_timestamp(evidence.observed_at)
        visible_at = _timestamp(evidence.visible_at)
    except ValueError:
        return None
    if (
        evidence.source_id != identity.source_id
        or evidence.unit != identity.unit
        or not evidence.source_url
        or len(evidence.raw_sha256) != 64
        or observed_at > issue_as_of
        or visible_at > issue_as_of
        or not math.isfinite(evidence.value)
        or evidence.value <= 0
    ):
        return None
    return float(evidence.value)


def get_latest_issued_seven_product_forecast() -> SevenProductForecastBatch | None:
    try:
        with closing(connect_readonly()) as connection:
            row = connection.execute(
                """
                SELECT payload,payload_sha256 FROM seven_product_forecast_batches
                ORDER BY business_date DESC,as_of_time DESC LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            return SevenProductForecastBatch.model_validate(
                _load_exact_json(str(row["payload"]), str(row["payload_sha256"]))
            )
    except SevenProductForecastLedgerError:
        raise
    except (sqlite3.Error, ValueError) as exc:
        raise SevenProductForecastLedgerError("seven_product_forecast_audit_failed") from exc


def _loader_for_identity(loader: SeriesLoader | None) -> Any:
    """Adapt the configured loader to the identity-aware signature.

    Injected two-argument loaders (tests, replay harnesses) keep working; only
    the default loader resolves retired series.
    """
    if loader is None:
        return load_label_series_for_identity
    try:
        if "series_id" in signature(loader).parameters:
            return loader
    except (TypeError, ValueError):  # builtins without inspectable signature
        pass

    def _ignores_series_id(target: str, as_of: datetime, *, series_id: str) -> LoadedLabelSeries:
        return loader(target, as_of)

    return _ignores_series_id


def settle_pending_seven_product_forecasts(
    *,
    evaluation_as_of: str | None = None,
    series_loader: SeriesLoader | None = None,
    apply: bool = True,
) -> dict[str, Any]:
    if apply and os.environ.get("PREDICTION_WRITES_PAUSED") == "1":
        raise SevenProductForecastLedgerError("prediction_writes_paused")
    as_of = _timestamp(evaluation_as_of) if evaluation_as_of else datetime.now(UTC)
    loader = _loader_for_identity(series_loader)
    pending = _load_pending_cells()
    loaded_by_target: dict[tuple[str, str, str], LoadedLabelSeries] = {}
    main_inputs = None
    proposals: list[dict[str, Any]] = []
    status_counts = {
        "pending_maturity": 0,
        "unscoreable_at_issue": 0,
        "blocked_data_quality": 0,
        "proposed": 0,
        "inserted": 0,
    }
    blocked_reasons: list[str] = []
    unscoreable_reasons: list[str] = []
    for item in pending:
        cell = item["forecast"]
        issue_failure = _issue_quality_failure(cell)
        if issue_failure:
            status_counts["unscoreable_at_issue"] += 1
            unscoreable_reasons.append(f"{item['cell_id']}:{issue_failure}")
            continue
        try:
            identity = frozen_label_identity(
                target=cell.target,
                registry_version=cell.label_registry_version,
                series_id=cell.label_series_id,
            )
        except ValueError:
            status_counts["unscoreable_at_issue"] += 1
            unscoreable_reasons.append(f"{item['cell_id']}:unknown_frozen_label_identity")
            continue
        # Each cell is scored against the series its own frozen contract named.
        # A retired series (crude EIA spot before v5) still settles on that
        # series; mixing the live series into an older contract would reject
        # every matured cell as label_series_identity_mismatch.
        cache_key = (cell.target, identity.series_id, cell.forecast_contract)
        loaded = loaded_by_target.get(cache_key)
        if loaded is None:
            if cell.forecast_contract == "issue-calendar.v1" and series_loader is None:
                from .prediction_main import capture_main_inputs

                if main_inputs is None:
                    main_inputs = capture_main_inputs(as_of, include_evidence=False)
                if main_inputs.series[cell.target].identity["series_id"] != identity.series_id:
                    status_counts["blocked_data_quality"] += 1
                    blocked_reasons.append(f"{item['cell_id']}:frozen_series_retired_requires_identity_loader")
                    continue
                loaded = main_inputs.load(cell.target, as_of)
            else:
                loaded = loader(cell.target, as_of, series_id=identity.series_id)
            loaded_by_target[cache_key] = loaded
        if cell.forecast_contract == "issue-calendar.v1" and not loaded.source_matches_label:
            status_counts["blocked_data_quality"] += 1
            blocked_reasons.append(f"{item['cell_id']}:main_outcome_quality_blocked")
            continue
        if cell.forecast_contract == "issue-calendar.v1":
            target_date = (_timestamp(cell.as_of_time).astimezone(LEDGER_TIMEZONE).date()
                           + timedelta(days=cell.horizon_days)).isoformat()
            if cell.target_date != target_date:
                status_counts["blocked_data_quality"] += 1
                blocked_reasons.append(f"{item['cell_id']}:frozen_target_date_mismatch")
                continue
            future = [point for point in loaded.points if point.observed_at[:10] >= target_date]
        else:
            target_time = _observation_timestamp(cell.latest_observation_at) + timedelta(days=cell.horizon_days)
            future = [point for point in loaded.points if _observation_timestamp(point.observed_at) >= target_time]
        if not future:
            status_counts["pending_maturity"] += 1
            continue
        series_failure = _loaded_series_quality_failure(identity, loaded, as_of)
        if series_failure:
            status_counts["blocked_data_quality"] += 1
            blocked_reasons.append(f"{item['cell_id']}:{series_failure}")
            continue
        actual = future[0]
        reason = _actual_quality_failure(
            item=item,
            cell=cell,
            actual=actual,
            as_of=as_of,
            expected_identity=identity,
        )
        if reason:
            status_counts["blocked_data_quality"] += 1
            blocked_reasons.append(f"{item['cell_id']}:{reason}")
            continue
        proposals.append(_prepare_outcome(item=item, cell=cell, actual=actual, settled_at=as_of))
    status_counts["proposed"] = len(proposals)
    if apply and proposals:
        status_counts["inserted"] = _insert_outcomes(proposals)
    return {
        "schema_version": "seven-product-forecast-settlement.v1",
        "evaluation_as_of": as_of.isoformat(),
        "apply": apply,
        "pending_cell_count": len(pending),
        **status_counts,
        "blocked_reasons": sorted(blocked_reasons),
        "unscoreable_reasons": sorted(unscoreable_reasons),
        "status": "blocked" if blocked_reasons else "ready",
    }


def _prepare_batch(batch: SevenProductForecastBatch) -> dict[str, Any]:
    expected_grid = {(target, horizon) for target in CURRENT_FORMAL_TARGETS for horizon in CURRENT_FORMAL_HORIZONS}
    actual_grid = {(cell.target, cell.horizon_days) for cell in batch.cells}
    if len(batch.cells) != CURRENT_FORMAL_CELL_COUNT or actual_grid != expected_grid or not batch.contract_complete:
        raise SevenProductForecastLedgerError("seven_product_forecast_grid_incomplete")
    as_of = _timestamp(batch.as_of_time)
    if any(_timestamp(cell.as_of_time) != as_of for cell in batch.cells):
        raise SevenProductForecastLedgerError("seven_product_cell_cutoff_mismatch")
    for cell in batch.cells:
        if any(_timestamp(evidence.visible_at) > as_of for evidence in cell.evidence):
            raise SevenProductForecastLedgerError("seven_product_future_evidence_rejected")
        if cell.forecast_contract == "issue-calendar.v1":
            expected_date = (as_of.astimezone(LEDGER_TIMEZONE).date() + timedelta(days=cell.horizon_days)).isoformat()
            if cell.target_date != expected_date:
                raise SevenProductForecastLedgerError("seven_product_target_date_mismatch")
        if any(candidate.input_sha256 != cell.input_snapshot_sha256 for candidate in cell.candidates):
            raise SevenProductForecastLedgerError("seven_product_candidate_input_mismatch")
    registry_revisions = {cell.model_registry_revision for cell in batch.cells}
    if len(registry_revisions) != 1:
        raise SevenProductForecastLedgerError("seven_product_registry_revision_mixed")
    formal_count = sum(cell.formal_status == "formal" for cell in batch.cells)
    reference_count = sum(cell.formal_status in {"reference", "degraded", "low_confidence"} for cell in batch.cells)
    unavailable_count = sum(cell.formal_status in {"insufficient_data", "model_unavailable"} for cell in batch.cells)
    if (formal_count, reference_count, unavailable_count) != (
        batch.formal_count,
        batch.reference_count,
        batch.unavailable_count,
    ):
        raise SevenProductForecastLedgerError("seven_product_batch_count_mismatch")
    batch_payload = batch.model_dump(mode="json")
    payload = _canonical_json(batch_payload)
    payload_sha256 = _digest_text(payload)
    data_sha = _digest([cell.data_snapshot_sha256 for cell in batch.cells])
    config_sha = _digest([cell.configuration_sha256 for cell in batch.cells])
    persisted_at = datetime.now(UTC).isoformat()
    business_date = as_of.astimezone(LEDGER_TIMEZONE).date().isoformat()
    cell_rows = []
    for cell in sorted(batch.cells, key=lambda item: (item.target, item.horizon_days)):
        cell_payload = _canonical_json(cell.model_dump(mode="json"))
        cell_sha = _digest_text(cell_payload)
        cell_id = f"seven-cell-{_digest([batch.batch_id, cell.target, cell.horizon_days])[:24]}"
        origin = _origin_evidence(cell)
        cell_rows.append(
            (
                cell_id,
                batch.batch_id,
                cell.target,
                cell.horizon_days,
                cell.label_series_id,
                cell.model_version,
                cell.feature_version,
                cell.label_registry_version,
                cell.neutral_band_policy_version,
                cell.evaluation_status,
                cell.evaluation_id,
                cell.evaluation_result_sha256,
                cell.model_registry_revision,
                origin["observation_id"] if origin else None,
                cell.latest_observation_at,
                cell.latest_visible_at,
                cell.point_forecast,
                cell.neutral_band_pct,
                cell.direction,
                cell.unit,
                cell.data_snapshot_sha256,
                cell.configuration_sha256,
                cell_payload,
                cell_sha,
            )
        )
    return {
        "business_date": business_date,
        "payload_sha256": payload_sha256,
        "batch_row": (
            batch.batch_id,
            business_date,
            batch.schema_version,
            batch.as_of_time,
            batch.generated_at,
            persisted_at,
            next(iter(registry_revisions)),
            data_sha,
            config_sha,
            batch.formal_count,
            batch.reference_count,
            batch.unavailable_count,
            1,
            payload,
            payload_sha256,
        ),
        "cell_rows": cell_rows,
    }


def _origin_evidence(cell: SevenProductForecastCell) -> dict[str, Any] | None:
    if cell.latest_observation_at is None:
        return None
    matches = [
        evidence.model_dump(mode="json")
        for evidence in cell.evidence
        if evidence.observed_at == cell.latest_observation_at
        and evidence.visible_at == cell.latest_visible_at
        and evidence.value == cell.latest_value
    ]
    if len(matches) != 1:
        raise SevenProductForecastLedgerError("seven_product_origin_evidence_missing")
    return matches[0]


def _load_pending_cells() -> list[dict[str, Any]]:
    try:
        with closing(connect_readonly()) as connection:
            rows = connection.execute(
                """
                SELECT c.cell_id,c.batch_id,b.as_of_time,c.cell_payload,c.cell_sha256
                FROM seven_product_forecast_cells AS c
                JOIN seven_product_forecast_batches AS b ON b.batch_id=c.batch_id
                LEFT JOIN seven_product_forecast_outcomes AS o ON o.cell_id=c.cell_id
                WHERE o.cell_id IS NULL ORDER BY b.as_of_time,c.target,c.horizon_days
                """
            ).fetchall()
            result = []
            for row in rows:
                cell = _load_cell_payload(str(row["cell_payload"]), str(row["cell_sha256"]))
                result.append(
                    {
                        "cell_id": str(row["cell_id"]),
                        "batch_id": str(row["batch_id"]),
                        "batch_as_of_time": str(row["as_of_time"]),
                        "forecast": cell,
                    }
                )
            return result
    except SevenProductForecastLedgerError:
        raise
    except (sqlite3.Error, ValueError) as exc:
        raise SevenProductForecastLedgerError("seven_product_forecast_audit_failed") from exc


def _actual_quality_failure(
    *,
    item: dict[str, Any],
    cell: SevenProductForecastCell,
    actual: Any,
    as_of: datetime,
    expected_identity: Any,
) -> str:
    if _timestamp(actual.visible_at) > as_of:
        return "actual_not_visible_at_evaluation_cutoff"
    if _timestamp(actual.visible_at) <= _timestamp(item["batch_as_of_time"]):
        return "actual_was_visible_at_forecast_cutoff"
    if actual.source_id != expected_identity.source_id:
        return "actual_source_identity_mismatch"
    if actual.semantic_series_id != expected_identity.series_id:
        return "actual_semantic_series_mismatch"
    if not actual.contract_version:
        return "actual_contract_version_missing"
    if actual.unit != expected_identity.unit or actual.unit != cell.unit:
        return "actual_unit_mismatch"
    if not actual.observation_id or not actual.source_url:
        return "actual_identity_incomplete"
    if len(actual.raw_sha256) != 64:
        return "actual_raw_sha256_missing"
    if not math.isfinite(actual.value) or actual.value <= 0:
        return "actual_value_invalid"
    return ""


def _loaded_series_quality_failure(expected_identity: Any, loaded: LoadedLabelSeries, as_of: datetime) -> str:
    if not loaded.source_matches_label:
        return "frozen_label_source_mismatch"
    previous_observed: datetime | None = None
    for point in loaded.points:
        observed = _observation_timestamp(point.observed_at)
        visible = _timestamp(point.visible_at)
        if previous_observed is not None and observed <= previous_observed:
            return "label_observations_not_strictly_increasing"
        if visible > as_of:
            return "label_observation_not_visible_at_evaluation_cutoff"
        if (
            point.source_id != expected_identity.source_id
            or point.semantic_series_id != expected_identity.series_id
            or not point.contract_version
            or point.unit != expected_identity.unit
        ):
            return "label_series_identity_mismatch"
        if not math.isfinite(point.value) or point.value <= 0:
            return "label_value_invalid"
        previous_observed = observed
    return ""


def _prepare_outcome(
    *, item: dict[str, Any], cell: SevenProductForecastCell, actual: Any, settled_at: datetime
) -> dict[str, Any]:
    if cell.latest_value is None or cell.point_forecast is None or cell.neutral_band_pct is None:
        raise SevenProductForecastLedgerError("seven_product_outcome_origin_incomplete")
    actual_change = actual.value / cell.latest_value - 1
    actual_direction = _direction(actual_change, cell.neutral_band_pct)
    predicted_direction = cell.direction
    if predicted_direction == "uncertain":
        raise SevenProductForecastLedgerError("seven_product_uncertain_forecast_unscoreable")
    absolute_error = abs(cell.point_forecast - actual.value)
    absolute_percentage_error = absolute_error / actual.value
    identity = _digest([item["cell_id"], actual.observation_id, actual.visible_at, actual.raw_sha256])
    payload = {
        "outcome_id": f"seven-outcome-{identity[:24]}",
        "cell_id": item["cell_id"],
        "batch_id": item["batch_id"],
        "target": cell.target,
        "horizon_days": cell.horizon_days,
        "settled_at": settled_at.isoformat(),
        "actual_observation_id": actual.observation_id,
        "actual_observed_at": actual.observed_at,
        "actual_visible_at": actual.visible_at,
        "actual_source_id": actual.source_id,
        "actual_source_url": actual.source_url,
        "actual_raw_sha256": actual.raw_sha256,
        "actual_value": actual.value,
        "actual_unit": actual.unit,
        "point_forecast": cell.point_forecast,
        "absolute_error": absolute_error,
        "absolute_percentage_error": absolute_percentage_error,
        "predicted_direction": predicted_direction,
        "actual_direction": actual_direction,
        "direction_hit": predicted_direction == actual_direction,
    }
    validated = SevenProductForecastOutcome.model_validate(payload)
    canonical = _canonical_json(validated.model_dump(mode="json"))
    return {"payload": validated, "canonical": canonical, "sha256": _digest_text(canonical)}


def _insert_outcomes(proposals: list[dict[str, Any]]) -> int:
    inserted = 0
    try:
        with closing(connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            for proposal in proposals:
                outcome = proposal["payload"]
                existing = connection.execute(
                    "SELECT outcome_sha256 FROM seven_product_forecast_outcomes WHERE cell_id=?",
                    (outcome.cell_id,),
                ).fetchone()
                if existing is not None:
                    if str(existing["outcome_sha256"]) != proposal["sha256"]:
                        raise SevenProductForecastLedgerError("seven_product_outcome_conflict")
                    continue
                connection.execute(
                    """
                    INSERT INTO seven_product_forecast_outcomes(
                      outcome_id,cell_id,batch_id,target,horizon_days,settled_at,actual_observation_id,
                      actual_observed_at,actual_visible_at,actual_source_id,actual_source_url,actual_raw_sha256,
                      actual_value,actual_unit,point_forecast,absolute_error,absolute_percentage_error,
                      predicted_direction,actual_direction,direction_hit,outcome_payload,outcome_sha256
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        outcome.outcome_id,
                        outcome.cell_id,
                        outcome.batch_id,
                        outcome.target,
                        outcome.horizon_days,
                        outcome.settled_at,
                        outcome.actual_observation_id,
                        outcome.actual_observed_at,
                        outcome.actual_visible_at,
                        outcome.actual_source_id,
                        outcome.actual_source_url,
                        outcome.actual_raw_sha256,
                        outcome.actual_value,
                        outcome.actual_unit,
                        outcome.point_forecast,
                        outcome.absolute_error,
                        outcome.absolute_percentage_error,
                        outcome.predicted_direction,
                        outcome.actual_direction,
                        int(outcome.direction_hit),
                        proposal["canonical"],
                        proposal["sha256"],
                    ),
                )
                inserted += 1
            connection.commit()
        return inserted
    except SevenProductForecastLedgerError:
        raise
    except sqlite3.Error as exc:
        raise SevenProductForecastLedgerError("seven_product_outcome_persistence_failed") from exc


def _load_batch_locked(connection: sqlite3.Connection, *, batch_id: str) -> SevenProductForecastLedgerBatch:
    batch = connection.execute(
        "SELECT * FROM seven_product_forecast_batches WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    if batch is None:
        raise SevenProductForecastLedgerError("seven_product_batch_missing")
    batch_payload = _load_exact_json(str(batch["payload"]), str(batch["payload_sha256"]))
    parsed_batch = SevenProductForecastBatch.model_validate(batch_payload)
    if parsed_batch.batch_id != batch_id:
        raise SevenProductForecastLedgerError("seven_product_batch_payload_mismatch")
    _audit_batch_projection(batch, parsed_batch)
    rows = connection.execute(
        """
        SELECT c.*,o.outcome_payload,o.outcome_sha256,
               i.invalidation_payload,i.invalidation_sha256,i.invalidation_id
        FROM seven_product_forecast_cells AS c
        LEFT JOIN seven_product_forecast_outcomes AS o ON o.cell_id=c.cell_id
        LEFT JOIN seven_product_forecast_outcome_invalidations AS i ON i.outcome_id=o.outcome_id
        WHERE c.batch_id=? ORDER BY c.target,c.horizon_days
        """,
        (batch_id,),
    ).fetchall()
    if len(rows) != CURRENT_FORMAL_CELL_COUNT:
        raise SevenProductForecastLedgerError("seven_product_forecast_grid_incomplete")
    cells = []
    for row in rows:
        forecast = _load_cell_payload(str(row["cell_payload"]), str(row["cell_sha256"]))
        _audit_cell_projection(row, forecast)
        outcome = None
        invalidation = None
        if row["outcome_payload"] is not None:
            outcome_payload = _load_exact_json(str(row["outcome_payload"]), str(row["outcome_sha256"]))
            outcome = SevenProductForecastOutcome.model_validate(outcome_payload)
            if outcome.cell_id != str(row["cell_id"]) or outcome.batch_id != batch_id:
                raise SevenProductForecastLedgerError("seven_product_outcome_payload_mismatch")
            outcome_row = connection.execute(
                "SELECT * FROM seven_product_forecast_outcomes WHERE cell_id=?",
                (str(row["cell_id"]),),
            ).fetchone()
            _audit_outcome_projection(outcome_row, outcome)
        if row["invalidation_payload"] is not None:
            invalidation_payload = _load_exact_json(str(row["invalidation_payload"]), str(row["invalidation_sha256"]))
            invalidation_payload["invalidation_id"] = str(row["invalidation_id"])
            invalidation = SevenProductForecastOutcomeInvalidation.model_validate(invalidation_payload)
            if (
                invalidation.cell_id != str(row["cell_id"])
                or invalidation.batch_id != batch_id
                or outcome is None
                or invalidation.outcome_id != outcome.outcome_id
            ):
                raise SevenProductForecastLedgerError("seven_product_invalidation_payload_mismatch")
            invalidation_row = connection.execute(
                "SELECT * FROM seven_product_forecast_outcome_invalidations WHERE invalidation_id=?",
                (invalidation.invalidation_id,),
            ).fetchone()
            _audit_invalidation_projection(invalidation_row, invalidation)
        settlement_status = (
            "invalidated_contract_mismatch" if invalidation else "scored" if outcome else _unsettled_status(forecast)
        )
        cells.append(
            SevenProductForecastLedgerCell(
                cell_id=str(row["cell_id"]),
                settlement_status=settlement_status,
                forecast=forecast,
                outcome=outcome,
                invalidation=invalidation,
            )
        )
    return SevenProductForecastLedgerBatch(
        batch_id=batch_id,
        business_date=str(batch["business_date"]),
        as_of_time=str(batch["as_of_time"]),
        generated_at=str(batch["generated_at"]),
        persisted_at=str(batch["persisted_at"]),
        model_registry_revision=str(batch["model_registry_revision"]),
        data_snapshot_sha256=str(batch["data_snapshot_sha256"]),
        configuration_sha256=str(batch["configuration_sha256"]),
        formal_count=int(batch["formal_count"]),
        reference_count=int(batch["reference_count"]),
        unavailable_count=int(batch["unavailable_count"]),
        contract_complete=True,
        payload_sha256=str(batch["payload_sha256"]),
        cells=cells,
    )


def _audit_batch_projection(row: sqlite3.Row, payload: SevenProductForecastBatch) -> None:
    registry_revisions = {cell.model_registry_revision for cell in payload.cells}
    expected = {
        "business_date": _timestamp(payload.as_of_time).astimezone(LEDGER_TIMEZONE).date().isoformat(),
        "schema_version": payload.schema_version,
        "as_of_time": payload.as_of_time,
        "generated_at": payload.generated_at,
        "model_registry_revision": next(iter(registry_revisions)) if len(registry_revisions) == 1 else "",
        "data_snapshot_sha256": _digest([cell.data_snapshot_sha256 for cell in payload.cells]),
        "configuration_sha256": _digest([cell.configuration_sha256 for cell in payload.cells]),
        "formal_count": payload.formal_count,
        "reference_count": payload.reference_count,
        "unavailable_count": payload.unavailable_count,
        "contract_complete": int(payload.contract_complete),
    }
    if any(row[key] != value for key, value in expected.items()):
        raise SevenProductForecastLedgerError("seven_product_batch_projection_mismatch")


def _audit_cell_projection(row: sqlite3.Row, payload: SevenProductForecastCell) -> None:
    origin = _origin_evidence(payload)
    expected = {
        "cell_id": (f"seven-cell-{_digest([str(row['batch_id']), payload.target, payload.horizon_days])[:24]}"),
        "target": payload.target,
        "horizon_days": payload.horizon_days,
        "label_series_id": payload.label_series_id,
        "model_version": payload.model_version,
        "feature_version": payload.feature_version,
        "label_registry_version": payload.label_registry_version,
        "neutral_band_policy_version": payload.neutral_band_policy_version,
        "evaluation_status": payload.evaluation_status,
        "evaluation_id": payload.evaluation_id,
        "evaluation_result_sha256": payload.evaluation_result_sha256,
        "model_registry_revision": payload.model_registry_revision,
        "origin_observation_id": origin["observation_id"] if origin else None,
        "origin_observed_at": payload.latest_observation_at,
        "origin_visible_at": payload.latest_visible_at,
        "point_forecast": payload.point_forecast,
        "neutral_band_pct": payload.neutral_band_pct,
        "predicted_direction": payload.direction,
        "unit": payload.unit,
        "data_snapshot_sha256": payload.data_snapshot_sha256,
        "configuration_sha256": payload.configuration_sha256,
    }
    if any(row[key] != value for key, value in expected.items()):
        raise SevenProductForecastLedgerError("seven_product_cell_projection_mismatch")


def _audit_outcome_projection(row: sqlite3.Row | None, payload: SevenProductForecastOutcome) -> None:
    if row is None:
        raise SevenProductForecastLedgerError("seven_product_outcome_missing")
    expected = payload.model_dump(mode="json")
    expected["direction_hit"] = int(payload.direction_hit)
    if any(row[key] != value for key, value in expected.items()):
        raise SevenProductForecastLedgerError("seven_product_outcome_projection_mismatch")


def _audit_invalidation_projection(row: sqlite3.Row | None, payload: SevenProductForecastOutcomeInvalidation) -> None:
    if row is None:
        raise SevenProductForecastLedgerError("seven_product_invalidation_missing")
    expected = payload.model_dump(mode="json")
    expected.pop("schema_version")
    if any(row[key] != value for key, value in expected.items()):
        raise SevenProductForecastLedgerError("seven_product_invalidation_projection_mismatch")


def _load_cell_payload(payload: str, sha256: str) -> SevenProductForecastCell:
    return SevenProductForecastCell.model_validate(_load_exact_json(payload, sha256))


def _load_exact_json(payload: str, sha256: str) -> dict[str, Any]:
    if _digest_text(payload) != sha256:
        raise SevenProductForecastLedgerError("seven_product_payload_hash_mismatch")
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SevenProductForecastLedgerError("seven_product_payload_invalid") from exc
    if not isinstance(parsed, dict) or _canonical_json(parsed) != payload:
        raise SevenProductForecastLedgerError("seven_product_payload_not_canonical")
    return parsed


def _unsettled_status(cell: SevenProductForecastCell) -> str:
    if _issue_quality_failure(cell):
        return "unscoreable_at_issue"
    return "pending"


def _issue_quality_failure(cell: SevenProductForecastCell) -> str:
    if (
        cell.point_forecast is None
        or cell.latest_value is None
        or cell.latest_observation_at is None
        or cell.latest_visible_at is None
        or cell.neutral_band_pct is None
        or cell.direction == "uncertain"
    ):
        return "forecast_origin_incomplete"
    if not cell.source_matches_label:
        return "forecast_source_does_not_match_frozen_label"
    try:
        identity = frozen_label_identity(
            target=cell.target,
            registry_version=cell.label_registry_version,
            series_id=cell.label_series_id,
        )
    except ValueError:
        return "unknown_frozen_label_identity"
    try:
        evidence = _origin_evidence(cell)
    except SevenProductForecastLedgerError:
        return "forecast_origin_evidence_missing"
    if evidence is None:
        return "forecast_origin_evidence_missing"
    if evidence["source_id"] != identity.source_id or evidence["unit"] != identity.unit:
        return "forecast_origin_identity_mismatch"
    if len(str(evidence.get("raw_sha256") or "")) != 64 or not evidence.get("source_url"):
        return "forecast_origin_lineage_incomplete"
    return ""


def _direction(change: float, neutral_band_pct: float) -> str:
    return "up" if change > neutral_band_pct else "down" if change < -neutral_band_pct else "neutral"


def _timestamp(value: str | None) -> datetime:
    if not value:
        raise ValueError("timestamp_required")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp_invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamp_timezone_required")
    return parsed.astimezone(UTC)


def _observation_timestamp(value: str | None) -> datetime:
    """Normalize a market observation date without weakening visibility checks.

    Several official daily series intentionally identify an observation by a
    calendar date (YYYY-MM-DD). Treat that identity as midnight UTC only for
    chronological ordering. Publication/visibility timestamps still flow
    through the strict timezone-required parser above.
    """

    if value and len(value) == 10:
        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ValueError("observation_date_invalid") from exc
    return _timestamp(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return _digest_text(_canonical_json(value))


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

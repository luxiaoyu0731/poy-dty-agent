"""Shared inputs and internal comparisons for the one issued forecast ledger.

No independent schedule, candidate database, model calls or automatic promotion.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path

from .models import ForecastCandidate
from .prediction_benchmark import calibrate, nonoverlapping_matured
from .prediction_features import DRIVERS, calendar_return
from .prediction_inputs import export_vintages
from .prediction_replay import SHANGHAI, direction, load_export
from .settings import settings
from .seven_product_forecast import FRESHNESS_DAYS, MIN_TREND_POINTS, LoadedLabelSeries
from .sqlite_permissions import secure_private_directory
from .storage import connect_readonly

MAIN_CONTRACT = "issue-calendar.v1"
CANDIDATE_POLICY = "shared-input-comparisons.v1"


class MainInputSnapshot:
    def __init__(self, exported: dict):
        self.exported = exported
        self.evidence_features = {}
        self.evidence_dossier = None
        if exported.get("schema_version") in {"main-price-evidence-input.v1", "main-price-evidence-input.v2"}:
            from .prediction_evidence_runtime import DOSSIER_SCHEMA, joint_input, verify

            verify(exported)
            # Rebuild, rather than trusting supplied feature numbers or a rehashed packet.
            if (
                joint_input(
                    exported["price_input"],
                    exported["evidence_receipt"],
                    include_dossier=exported["schema_version"] == DOSSIER_SCHEMA,
                    dossier_policy=(exported.get("evidence_dossier") or {}).get("schema_version"),
                )
                != exported
            ):
                raise ValueError("main_evidence_reconstruction_mismatch")
            self.evidence_features = exported["evidence_features"]
            self.evidence_dossier = exported.get("evidence_dossier")
            price_export = exported["price_input"]
        else:
            price_export = exported
        self.series = load_export(price_export)
        self.as_of = datetime.fromisoformat(exported["as_of_time"])
        self.sha256 = exported["content_sha256"]
        self.views = {target: series.view(self.as_of) for target, series in self.series.items()}

    def persist(self) -> Path:
        directory = Path(settings.sqlite_path).resolve().parent / "prediction-inputs"
        secure_private_directory(directory)
        destination = directory / f"{self.sha256}.json"
        body = json.dumps(self.exported, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        if destination.exists():
            if destination.read_bytes() != body:
                raise ValueError("frozen_input_archive_conflict")
            return destination
        fd, temporary = tempfile.mkstemp(prefix=".inputs-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.read_bytes() != body:
                    raise ValueError("frozen_input_archive_conflict") from None
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.unlink(temporary)
        return destination

    def load(self, target: str, as_of: datetime) -> LoadedLabelSeries:
        if as_of != self.as_of:
            raise ValueError("main_input_cutoff_mismatch")
        view = self.views[target]
        # Never fall back to yesterday's valid value when today's value is corrupt.
        return LoadedLabelSeries(
            points=() if view.latest_blocked else view.points,
            source_matches_label=bool(view.points) and not view.latest_blocked,
            data_gaps=("latest_quote_quality_blocked",) if view.latest_blocked else (),
        )

    def candidates(self, cell) -> list[ForecastCandidate]:
        models = ("persistence.v1", "target-calendar-momentum.v1", "upstream-calendar-momentum.v1")
        blocked = cell.data_status != "fresh" or cell.history_points < MIN_TREND_POINTS
        values: list[tuple[str, float | None, str | None]] = []
        if blocked or cell.latest_value is None:
            values = [(model, None, "main_input_unavailable") for model in models]
        else:
            day = self.as_of.astimezone(SHANGHAI).date()
            days = cell.horizon_days + max(0, (day - date.fromisoformat(cell.latest_observation_at[:10])).days)
            own = calendar_return(self.views[cell.target].points, day, 7)
            values.append((models[0], cell.latest_value, None))
            values.append(
                (
                    models[1],
                    cell.latest_value * math.exp(own[0] * days / 7) if own else None,
                    None if own else "target_lag_missing",
                )
            )
            moves = []
            drivers = DRIVERS.get(cell.target, ())
            for driver in drivers:
                view = self.views.get(driver)
                if view is None or not view.points or view.latest_blocked:
                    break
                latest = date.fromisoformat(view.points[-1].observed_at[:10])
                move = calendar_return(view.points, day, 7)
                if (day - latest).days > FRESHNESS_DAYS[driver] or move is None:
                    break
                moves.append(move[0])
            ready = bool(drivers) and len(moves) == len(drivers)
            values.append(
                (
                    models[2],
                    cell.latest_value * math.exp(sum(moves) / len(moves) * days / 7) if ready else None,
                    None if ready else "upstream_lag_missing",
                )
            )
        candidates = [
            ForecastCandidate(
                model_id=model,
                point_forecast=value,
                direction=direction(value / cell.latest_value - 1, cell.neutral_band_pct)
                if value is not None
                else None,
                reason=reason,
                input_sha256=self.sha256,
            )
            for model, value, reason in values
        ]
        packet = self.evidence_features.get(f"{cell.target}:{cell.horizon_days}")
        if packet is not None:
            # These are internal comparisons in the same issue, not additional forecasts.
            # Until OOS qualification, do not invent weights or silently call absence zero.
            for name, fields in (
                ("price-current", ("current_support",)),
                ("price-history", ("history_support",)),
                ("price-current-history", ("current_support", "history_support")),
            ):
                missing = any(packet["values"][f] is None for f in fields)
                reason = "evidence_features_unavailable" if missing else packet["model_gate"]
                if packet["model_gate"] == "evidence_capture_failed":
                    reason = packet["model_gate"]
                candidates.append(
                    ForecastCandidate(
                        model_id=f"{name}-ridge.v1",
                        reason=reason,
                        input_sha256=self.sha256,
                    )
                )
        return candidates


def capture_main_inputs(as_of: datetime, *, include_evidence: bool = True) -> MainInputSnapshot:
    from .event_summary_quality import grounding_validation_scope

    with grounding_validation_scope():
        return _capture_main_inputs(as_of, include_evidence=include_evidence)


def _capture_main_inputs(as_of: datetime, *, include_evidence: bool) -> MainInputSnapshot:
    with closing(connect_readonly()) as connection:
        exported = export_vintages(connection, as_of=as_of.isoformat())
        if not include_evidence:
            return MainInputSnapshot(exported)
        from .prediction_evidence_runtime import collect_articles, failed_receipt, joint_input

        try:
            receipt = collect_articles(connection, as_of=as_of)
        except (sqlite3.Error, ValueError, TimeoutError) as exc:
            receipt = failed_receipt(as_of, type(exc).__name__)
    return MainInputSnapshot(joint_input(exported, receipt, include_dossier=True))


def comparison_scorecard(batches: list, *, as_of: datetime | None = None) -> dict:
    """Score candidates on the *existing* main outcome, never a second settlement."""
    cutoff = as_of or datetime.now(UTC)
    groups: dict[tuple, dict] = {}
    for batch in sorted(batches, key=lambda item: item.as_of_time):
        for item in batch.cells:
            cell, actual = item.forecast, item.outcome
            if cell.forecast_contract != MAIN_CONTRACT:
                continue
            for candidate in cell.candidates:
                key = (
                    cell.target,
                    cell.horizon_days,
                    cell.label_registry_version,
                    cell.label_series_id,
                    cell.neutral_band_policy_version,
                    cell.model_version,
                    candidate.model_id,
                )
                row = groups.setdefault(
                    key,
                    {
                        "target": cell.target,
                        "horizon_days": cell.horizon_days,
                        "model_id": candidate.model_id,
                        "issued": 0,
                        "called": 0,
                        "paired": 0,
                        "candidate_correct": 0,
                        "main_correct": 0,
                        "nonoverlapping_pairs": 0,
                        "label_series_id": cell.label_series_id,
                        "label_registry_version": cell.label_registry_version,
                        "main_model_version": cell.model_version,
                        "neutral_band_policy_version": cell.neutral_band_policy_version,
                        "episodes": [],
                        "latest_direction": None,
                    },
                )
                row["latest_direction"] = candidate.direction
                row["issued"] += 1
                row["called"] += candidate.direction is not None
                if (
                    actual is None
                    or item.invalidation
                    or candidate.direction is None
                    or datetime.fromisoformat(actual.settled_at) > cutoff
                    or datetime.fromisoformat(actual.actual_visible_at) > cutoff
                ):
                    continue
                row["paired"] += 1
                row["candidate_correct"] += candidate.direction == actual.actual_direction
                row["main_correct"] += cell.direction == actual.actual_direction
                row["episodes"].append(
                    {
                        "issue_at": cell.as_of_time,
                        "models": {candidate.model_id: {"raw_direction": candidate.direction}},
                        "outcome": {
                            "state": "scored",
                            "direction": actual.actual_direction,
                            "revision_id": actual.actual_observation_id,
                            "settled_at": actual.settled_at,
                        },
                    }
                )
    for row in groups.values():
        episodes = nonoverlapping_matured(row.pop("episodes"), before=cutoff)
        row["nonoverlapping_pairs"] = len(episodes)
        row["calibration"] = (
            calibrate(episodes, model_id=row["model_id"], raw_direction=row["latest_direction"])
            if row["latest_direction"]
            else {"status": "abstained", "probabilities": None}
        )
        row["calibration_usage"] = "diagnostic_only_not_applied_to_issued_confidence"
    return {
        "contract": MAIN_CONTRACT,
        "policy": CANDIDATE_POLICY,
        "cells": list(groups.values()),
        "effect_validated": False,
        "automatic_promotion": False,
    }

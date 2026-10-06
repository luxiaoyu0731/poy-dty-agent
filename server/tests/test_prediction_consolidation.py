from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime, timedelta

import pytest
from test_seven_product_forecast_ledger import isolated_database  # noqa: F401

from app import seven_product_forecast_ledger as ledger
from app.prediction_main import MainInputSnapshot, comparison_scorecard
from app.prediction_presentation import forecast_projection, report_forecast_lines
from app.prediction_replay import digest
from app.seven_product_contract import LABEL_REGISTRY
from app.seven_product_forecast import LoadedLabelSeries, PricePoint, build_seven_product_forecast

pytestmark = pytest.mark.usefixtures("isolated_database")

CUTOFF = datetime(2026, 9, 26, 0, 0, tzinfo=UTC)


def inputs() -> dict:
    result = {"schema_version": "prediction-vintages.v1", "as_of_time": CUTOFF.isoformat(), "series": {}}
    for target, label in LABEL_REGISTRY.items():
        records = []
        for i in range(40):
            day = (CUTOFF - timedelta(days=40 - i)).date().isoformat()
            visible = f"{day}T10:00:00+00:00"
            records.append(
                {
                    "revision_id": f"{target}-{i}",
                    "observed_at": day,
                    "value": 100 + i,
                    "visible_at": visible,
                    "captured_at": visible,
                    "created_at": visible,
                    "source_id": label.source_id,
                    "series_id": label.series_id,
                    "unit": label.unit,
                    "source_url": "https://example.test/price",
                    "evidence_sha256": "a" * 64,
                    "payload_hash_verified": True,
                    "instrument_matches": True,
                    "contract_version": "v5",
                }
            )
        result["series"][target] = {
            "source_id": label.source_id,
            "series_id": label.series_id,
            "unit": label.unit,
            "truncated": False,
            "records": records,
        }
    return {**result, "content_sha256": digest(result)}


def build(snapshot=None, candidate_builder=None):
    snapshot = snapshot or MainInputSnapshot(inputs())
    return build_seven_product_forecast(
        as_of_time=CUTOFF.isoformat(),
        series_loader=snapshot.load,
        forecast_contract="issue-calendar.v1",
        input_snapshot_sha256=snapshot.sha256,
        candidate_builder=candidate_builder or snapshot.candidates,
    )


def test_new_issue_contract_and_candidates_share_inputs_and_band():
    batch = build()
    assert len(batch.cells) == 21 and batch.formal_count == 0
    for cell in batch.cells:
        assert cell.target_date == (CUTOFF.date() + timedelta(days=cell.horizon_days)).isoformat()
        assert cell.forecast_contract == "issue-calendar.v1"
        assert cell.confidence_kind == "heuristic_score"
        assert cell.model_version == "robust-calendar-drift-reference.v2"
        assert len(cell.candidates) == 3
        for candidate in cell.candidates:
            assert candidate.input_sha256 == cell.input_snapshot_sha256
            if candidate.point_forecast is not None:
                change = candidate.point_forecast / cell.latest_value - 1
                expected = (
                    "up" if change > cell.neutral_band_pct else "down" if change < -cell.neutral_band_pct else "neutral"
                )
                assert candidate.direction == expected


def test_candidate_exception_does_not_block_main():
    def broken(cell):
        raise RuntimeError("test")

    batch = build(candidate_builder=broken)
    assert all(c.point_forecast is not None and c.candidate_status == "degraded" for c in batch.cells)
    assert all(c.candidate_error == "RuntimeError" and not c.candidates for c in batch.cells)


@pytest.mark.parametrize("field,value", [("value", 10000), ("unit", "wrong")])
def test_bad_latest_quote_does_not_reuse_old_valid_quote(field, value):
    exported = inputs()
    exported["series"]["poy"]["records"][-1][field] = value
    exported.pop("content_sha256")
    exported["content_sha256"] = digest(exported)
    cells = [c for c in build(MainInputSnapshot(exported)).cells if c.target == "poy"]
    assert all(c.point_forecast is None and "latest_quote_quality_blocked" in c.data_gaps for c in cells)


def test_late_created_revision_not_visible_at_cutoff():
    exported = inputs()
    latest = exported["series"]["poy"]["records"][-1]
    latest["created_at"] = "2026-09-27T00:00:00+00:00"
    exported.pop("content_sha256")
    exported["content_sha256"] = digest(exported)
    snapshot = MainInputSnapshot(exported)
    assert snapshot.load("poy", CUTOFF).points[-1].observed_at == "2026-09-24"


def test_archive_is_content_addressed_and_idempotent():
    snapshot = MainInputSnapshot(inputs())
    path = snapshot.persist()
    before = path.read_bytes()
    assert snapshot.persist() == path and path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="archive_conflict"):
        snapshot.persist()


def test_old_cell_payload_keeps_legacy_contract_without_rewriting():
    raw = build().cells[0].model_dump(mode="json")
    for key in (
        "forecast_contract",
        "target_date",
        "input_snapshot_sha256",
        "confidence_kind",
        "candidate_status",
        "candidate_error",
        "candidates",
    ):
        raw.pop(key)
    serialized = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    sha = hashlib.sha256(serialized.encode()).hexdigest()
    restored = ledger._load_cell_payload(serialized, sha)
    assert restored.forecast_contract == "observation-horizon.v1"
    assert hashlib.sha256(serialized.encode()).hexdigest() == sha


def test_main_settlement_uses_issue_date_and_same_actual():
    batch = build()
    saved = ledger.save_seven_product_forecast_batch(batch)

    def loader(target, as_of, **kwargs):
        label = LABEL_REGISTRY[target]
        points = tuple(
            PricePoint(
                observation_id=f"{target}-actual-{d}",
                observed_at=f"2026-09-{d}",
                visible_at=f"2026-09-{d}T10:00:00+00:00",
                value=140 + d,
                unit=label.unit,
                source_id=label.source_id,
                source_url="https://example.test/actual",
                raw_sha256="b" * 64,
                semantic_series_id=label.series_id,
                contract_version="v5",
            )
            for d in (26, 28)
        )
        return LoadedLabelSeries(points=points, source_matches_label=True)

    ledger.settle_pending_seven_product_forecasts(
        evaluation_as_of="2026-09-29T00:00:00+00:00", series_loader=loader, apply=True
    )
    settled = ledger.get_seven_product_forecast_batch(batch_id=saved.batch_id)
    for cell in settled.cells:
        if cell.forecast.horizon_days == 1:
            assert cell.outcome.actual_observed_at == "2026-09-28"
        else:
            assert cell.outcome is None
    score = comparison_scorecard([settled], as_of=datetime(2026, 9, 30, tzinfo=UTC))
    assert all(row["calibration"]["probabilities"] is None for row in score["cells"])
    earlier = comparison_scorecard([settled], as_of=CUTOFF)
    assert all(row["paired"] == 0 for row in earlier["cells"])
    assert all(row["paired"] == (1 if row["horizon_days"] == 1 and row["called"] else 0) for row in score["cells"])


def test_existing_business_day_is_reused_before_input_capture(monkeypatch):
    batch = ledger.save_seven_product_forecast_batch(build())
    from app import prediction_main

    def forbidden(*args):
        pytest.fail("existing issue must not capture or backfill candidates")

    monkeypatch.setattr(prediction_main, "capture_main_inputs", forbidden)
    assert (
        ledger.record_daily_seven_product_forecast(as_of_time=CUTOFF.isoformat(), main_contract=True).batch_id
        == batch.batch_id
    )


def test_backdated_main_issue_refused():
    with pytest.raises(ValueError, match="current_issue_cutoff"):
        ledger.record_daily_seven_product_forecast(as_of_time="2026-01-01T00:00:00+00:00", main_contract=True)


def test_report_reads_exact_batch_and_does_not_recalculate():
    batch = build()
    projection = forecast_projection(batch)
    assert projection["batch_id"] == batch.batch_id
    text = "\n".join(report_forecast_lines({"main_prediction": projection}))
    assert batch.batch_id in text
    assert len(projection["cells"]) == 21
    assert str(batch.cells[0].point_forecast) in text


def test_context_uses_issued_batch_and_never_backdates_visibility():
    from app.prediction_presentation import forecast_context

    saved = ledger.save_seven_product_forecast_batch(build())
    cutoff = (datetime.fromisoformat(saved.persisted_at) + timedelta(seconds=1)).isoformat()
    current = forecast_context(as_of_time=cutoff)
    assert current["batch_id"] == saved.batch_id
    assert len(current["cells"]) == 21
    assert "不是市场事实" in current["text"]
    before = (datetime.fromisoformat(saved.persisted_at) - timedelta(seconds=1)).isoformat()
    assert forecast_context(as_of_time=before)["status"] == "unavailable"


def test_rollback_write_pause_preserves_read_access(monkeypatch):
    saved = ledger.save_seven_product_forecast_batch(build())
    monkeypatch.setenv("PREDICTION_WRITES_PAUSED", "1")
    assert ledger.get_seven_product_forecast_batch(batch_id=saved.batch_id).payload_sha256 == saved.payload_sha256
    for action in (
        lambda: ledger.record_daily_seven_product_forecast(main_contract=True),
        lambda: ledger.save_seven_product_forecast_batch(build()),
        lambda: ledger.settle_pending_seven_product_forecasts(apply=True),
    ):
        with pytest.raises(ledger.SevenProductForecastLedgerError, match="prediction_writes_paused"):
            action()


def test_calendar_drift_does_not_treat_weekend_gaps_as_one_day():
    from app.seven_product_forecast import robust_drift_projection

    values = [100 * math.exp(.001 * day) for day in (0, 3, 6, 9)]
    daily = robust_drift_projection(
        values, horizon_days=7, neutral_floor_pct=.006,
        observation_days=["2026-09-01", "2026-09-04", "2026-09-07", "2026-09-10"],
    )
    legacy = robust_drift_projection(values, horizon_days=7, neutral_floor_pct=.006)
    assert daily.predicted_change_pct == pytest.approx(math.exp(.001 * 7) - 1)
    assert legacy.predicted_change_pct == pytest.approx(math.exp(.003 * 7) - 1)

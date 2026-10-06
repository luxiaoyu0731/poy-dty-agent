from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from app.prediction_benchmark import (
    calibrate,
    classification_metrics,
    nonoverlapping_matured,
    probability_metrics,
    run_benchmark,
)
from app.prediction_replay import TargetContract, VintageSeries, digest, load_export, outcome_for, timestamp


def record(day: int, value: float = 100, *, visible: str | None = None, suffix: str = "") -> dict:
    observation = date(2026, 1, 1) + timedelta(days=day)
    visible = visible or f"{observation.isoformat()}T09:00:00+00:00"
    return {
        "revision_id": f"r{day}{suffix}",
        "observed_at": observation.isoformat(),
        "value": value,
        "visible_at": visible,
        "created_at": visible,
        "captured_at": visible,
        "source_id": "test",
        "series_id": "poy.test",
        "unit": "CNY/mt",
        "source_url": "https://example.test/price",
        "evidence_sha256": "a" * 64,
        "hash_kind": "source_capture",
        "payload_hash_verified": True,
        "instrument_matches": True,
        "contract_version": "test.v1",
    }


def body(records: list[dict]) -> dict:
    return {"source_id": "test", "series_id": "poy.test", "unit": "CNY/mt", "records": records, "truncated": False}


def series(records: list[dict]) -> VintageSeries:
    return VintageSeries("poy", body(records))


def bundle(records: list[dict], cutoff: str) -> dict:
    result = {"schema_version": "prediction-vintages.v1", "series": {"poy": body(records)}, "as_of_time": cutoff}
    return {**result, "content_sha256": digest(result)}


def test_historical_revision_is_restored_and_future_correction_not_used():
    original = record(0, 100)
    correction = record(0, 110, visible="2026-01-03T09:00:00Z", suffix="b")
    data = series([original, correction])
    assert data.view(timestamp("2026-01-02T12:00:00Z")).points[0].value == 100
    assert data.view(timestamp("2026-01-04T12:00:00Z")).points[0].value == 110
    assert len(series([correction]).view(timestamp("2026-01-02T12:00:00Z")).points) == 0


def test_backfill_cannot_backdate_availability_and_invalid_revision_is_not_replaced_by_old_good_one():
    late = record(0)
    late["created_at"] = "2026-01-10T09:00:00Z"
    assert not series([late]).view(timestamp("2026-01-09T12:00:00Z")).points
    invalid = record(0, -1, visible="2026-01-03T09:00:00Z", suffix="bad")
    view = series([record(0), invalid]).view(timestamp("2026-01-04T12:00:00Z"))
    assert view.latest_blocked and not view.points
    assert "invalid_price" in view.flags[0]["reasons"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("unit", "USD/mt", "unit_mismatch"),
        ("source_id", "other", "source_id_mismatch"),
        ("series_id", "dty.test", "series_id_mismatch"),
        ("instrument_matches", False, "instrument_mismatch"),
        ("payload_hash_verified", False, "payload_integrity_unverified"),
        ("evidence_sha256", "", "evidence_hash_missing"),
        ("value", 0, "invalid_price"),
    ],
)
def test_quality_identity_and_provenance(field, value, reason):
    r = record(0)
    r[field] = value
    view = series([r]).view(timestamp("2026-01-03T12:00:00Z"))
    assert reason in view.flags[0]["reasons"]


def test_causal_anomaly_is_quarantined_without_future_neighbours_or_raw_mutation():
    records = [record(i, 9000 + i) for i in range(10)] + [record(10, 887.5)]
    before = copy.deepcopy(records)
    cutoff = timestamp("2026-01-11T12:00:00Z")
    data = series(records)
    view = data.view(cutoff)
    assert view.latest_blocked and len(view.points) == 10
    assert view.flags[0]["value"] == 887.5
    assert records == before
    expanded = series(records + [record(11, 9020)])
    assert expanded.view(cutoff) == view
    later = expanded.view(timestamp("2026-01-12T12:00:00Z"))
    assert not later.latest_blocked and len(later.points) == 11


def test_persistent_regime_shift_blocks_instead_of_extrapolating_stale_level():
    records = [record(i) for i in range(10)] + [record(i, 180) for i in range(10, 15)]
    view = series(records).view(timestamp("2026-01-16T12:00:00Z"))
    assert view.latest_blocked and len(view.flags) == 5


def test_ambiguous_equal_time_prices_fail_closed():
    view = series([record(0, 100), record(0, 101, suffix="b")]).view(timestamp("2026-01-02T12:00:00Z"))
    assert "ambiguous_simultaneous_revision" in view.flags[0]["reasons"]


def test_calendar_clock_starts_at_issue_and_delayed_old_quote_is_not_a_future_outcome():
    issue = timestamp("2026-01-05T01:30:00Z")
    data = series([record(0), record(1, 110, visible="2026-01-05T10:00:00Z"), record(5, 101)])
    base = data.view(issue).points[-1]
    contract = TargetContract("calendar", 1)
    assert contract.expected_projection_days(issue, (base,)) == 5
    result = outcome_for(data, issue=issue, cutoff=timestamp("2026-01-07T12:00:00Z"), contract=contract, base=base)
    assert result["observed_at"] == "2026-01-06"
    assert result["direction"] == "up" and result["elapsed_calendar_days"] == 1


def test_publication_steps_do_not_mean_calendar_days_and_do_not_look_at_actual_future_spacing():
    issue = timestamp("2026-01-02T12:00:00Z")
    data = series([record(0), record(1), record(4, 101), record(7, 102)])
    base = data.view(issue).points[-1]
    result = outcome_for(
        data,
        issue=issue,
        cutoff=timestamp("2026-01-09T12:00:00Z"),
        contract=TargetContract("publication", 1),
        base=base,
    )
    assert result["observed_at"] == "2026-01-05"
    assert result["elapsed_calendar_days"] == 3


def test_first_arrival_settlement_not_rewritten_by_late_backfill_or_correction():
    issue = timestamp("2026-01-02T12:00:00Z")
    records = [
        record(1),
        record(3, 101),
        record(2, 103, visible="2026-01-07T12:00:00Z"),
        record(3, 110, visible="2026-01-08T12:00:00Z", suffix="b"),
    ]
    data = series(records)
    result = outcome_for(
        data,
        issue=issue,
        cutoff=timestamp("2026-01-09T12:00:00Z"),
        contract=TargetContract("calendar", 1),
        base=data.view(issue).points[-1],
    )
    assert result["revision_id"] == "r3" and result["value"] == 101
    assert result["later_price_revision"] is True


def test_bad_outcome_is_not_skipped_to_choose_a_better_later_quote():
    records = [record(i) for i in range(10)] + [record(10, 1), record(11, 101)]
    data = series(records)
    issue = timestamp("2026-01-10T12:00:00Z")
    result = outcome_for(
        data,
        issue=issue,
        cutoff=timestamp("2026-01-13T12:00:00Z"),
        contract=TargetContract("calendar", 1),
        base=data.view(issue).points[-1],
    )
    assert result["state"] == "outcome_quality_blocked"


def test_missing_outcome_is_bounded_and_pending_is_distinct():
    data = series([record(0)])
    issue = timestamp("2026-01-02T12:00:00Z")
    base = data.view(issue).points[-1]
    args = {"issue": issue, "contract": TargetContract("calendar", 1), "base": base}
    assert outcome_for(data, cutoff=timestamp("2026-01-03T12:00:00Z"), **args)["state"] == "pending"
    assert outcome_for(data, cutoff=timestamp("2026-01-20T12:00:00Z"), **args)["state"] == "outcome_timeout"


def test_export_integrity_and_missing_timezone_fail_closed():
    x = bundle([record(0)], "2026-01-03T12:00:00Z")
    x["series"]["poy"]["records"][0]["value"] = 101
    with pytest.raises(ValueError, match="integrity"):
        load_export(x)
    with pytest.raises(ValueError, match="timezone"):
        series([record(0, visible="2026-01-01")])


def learning_row(i: int, *, truth: str = "up", raw: str = "up", duration: int = 1) -> dict:
    issue = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i * 2)
    return {
        "issue_at": issue.isoformat(),
        "outcome": {
            "state": "scored",
            "direction": truth,
            "revision_id": f"r{i}",
            "settled_at": (issue + timedelta(days=duration)).isoformat(),
        },
        "models": {"m": {"raw_direction": raw}},
    }


def test_calibration_only_uses_matured_nonoverlapping_outcomes():
    rows = [learning_row(i, duration=3) for i in range(6)]
    accepted = nonoverlapping_matured(rows, before=timestamp("2026-01-12T00:00:00Z"))
    assert [r["outcome"]["revision_id"] for r in accepted] == ["r0", "r2"]
    assert calibrate(accepted, model_id="m", raw_direction="up")["probabilities"] is None


def test_calibration_is_empirical_and_requires_truth_class_coverage():
    rows = [learning_row(i, truth=("up", "neutral", "down")[i % 3]) for i in range(30)]
    estimate = calibrate(rows, model_id="m", raw_direction="up")
    assert estimate["probabilities"] == [1 / 3] * 3
    assert calibrate([learning_row(i) for i in range(30)], model_id="m", raw_direction="up")["probabilities"] is None


def test_accuracy_cannot_hide_missing_direction_class_and_bad_probability():
    metrics = classification_metrics(["up"] * 8 + ["down"] * 2, ["up"] * 10)
    assert metrics["accuracy"] == 0.8 and metrics["balanced_accuracy"] == 0.5
    assert metrics["recall"]["down"] == 0
    assert probability_metrics(["up"], [[0, 0, 1]])["brier"] == 0
    with pytest.raises(ValueError, match="invalid_probability"):
        probability_metrics(["up"], [[1, 1, 1]])


def test_delayed_label_changes_cannot_change_past_calibration():
    rows = [learning_row(i, truth=("up", "neutral", "down")[i % 3]) for i in range(40)]
    cutoff = timestamp("2026-03-05T00:00:00Z")
    rows[30]["outcome"]["settled_at"] = "2026-03-20T00:00:00Z"
    past = nonoverlapping_matured(rows, before=cutoff)
    changed = copy.deepcopy(rows)
    changed[30]["outcome"]["direction"] = "down"
    assert calibrate(past, model_id="m", raw_direction="up") == calibrate(
        nonoverlapping_matured(changed, before=cutoff), model_id="m", raw_direction="up"
    )


def test_end_to_end_common_truth_no_future_change_to_issued_prediction_and_no_production_promotion():
    records = [record(i, 100 + 0.2 * i) for i in range(45)]
    kwargs = {"start": date(2026, 1, 25), "end": date(2026, 1, 28)}
    a = run_benchmark(bundle(records, "2026-02-16T12:00:00Z"), **kwargs)
    b = run_benchmark(
        bundle(records + [record(1, 105, visible="2026-02-10T12:00:00Z", suffix="correction")], "2026-02-16T12:00:00Z"),
        **kwargs,
    )
    assert len(a["cells"]) == 6
    for left, right in zip(a["records"], b["records"], strict=True):
        assert left["input_sha256"] == right["input_sha256"]
        assert left["models"] == right["models"]
        assert "direction" in left["outcome"] or left["outcome"]["state"] == "pending"
    for cell in a["cells"]:
        models = cell["models"]
        assert all(m["raw"]["truth_counts"] == models[0]["raw"]["truth_counts"] for m in models)
        assert all(m["promotion_allowed"] is False for m in models)
        assert all(m["calibrated_probability_metrics"]["count"] == 0 for m in models)


def test_readonly_export_preserves_all_revisions_and_does_not_mutate_tables():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "export_prediction_vintages.py"
    spec = importlib.util.spec_from_file_location("export_prediction_vintages", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE market_observations(observation_id,source_id,product,indicator,observed_at,"
        "created_at,value,unit,evidence_url)"
    )
    conn.execute(
        "CREATE TABLE source_capture_revisions(capture_revision_id,source_id,semantic_series_id,"
        "observed_at,visible_at,created_at,captured_at,canonical_payload,canonical_payload_hash,"
        "source_url,raw_sha256,contract_version,parser_version)"
    )
    definition = module.LABEL_REGISTRY["poy"]
    payload = {"value": 100, "unit": "CNY/mt"}
    for i in range(3):
        visible = f"2026-01-0{i + 2}T09:00:00+00:00" if i < 2 else "2026-01-05T19:00:00+08:00"
        conn.execute(
            "INSERT INTO source_capture_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"r{i}",
                definition.source_id,
                definition.series_id,
                "2026-01-01",
                visible,
                visible,
                visible,
                json.dumps(payload),
                module.digest(payload),
                "https://example.test",
                "a" * 64,
                "v1",
                "p1",
            ),
        )
    conn.commit()
    exported = module.export_vintages(conn, as_of="2026-01-05T12:00:00Z")
    assert len(exported["series"]["poy"]["records"]) == 3
    assert conn.execute("SELECT COUNT(*) FROM source_capture_revisions").fetchone()[0] == 3
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM source_capture_revisions")
    conn.rollback()
    with pytest.raises(ValueError, match="truncated"):
        module.export_vintages(conn, as_of="2026-01-05T12:00:00Z", row_limit=1)
    conn.close()

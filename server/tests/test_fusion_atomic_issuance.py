from __future__ import annotations

import importlib.util
import sys
from contextlib import closing
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import event_fusion, storage
from app import seven_product_forecast as forecast_module
from app.settings import settings
from app.seven_product_forecast import LoadedLabelSeries, build_seven_product_forecast
from app.seven_product_forecast_ledger import save_seven_product_forecast_batch

path = Path(__file__).resolve().parents[1] / "scripts/run_seven_product_forecast_lifecycle.py"
spec = importlib.util.spec_from_file_location("atomic_lifecycle_runner", path)
runner = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = runner
spec.loader.exec_module(runner)


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    previous_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "atomic.db"))
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "atomic.db"))
    storage._MIGRATED_PATHS.clear()
    with closing(storage.connect()):
        pass
    batch = build_seven_product_forecast(
        as_of_time="2026-10-06T00:00:00+00:00",
        series_loader=lambda target, as_of: LoadedLabelSeries(points=(), source_matches_label=False),
    )
    try:
        yield batch
    finally:
        object.__setattr__(settings, "sqlite_path", previous_path)
        storage._MIGRATED_PATHS.clear()


def rows_for(batch, *, change=True):
    return [
        {
            "factor_id": f"{batch.batch_id}:{cell.target}:{cell.horizon_days}",
            "batch_id": batch.batch_id,
            "business_date": "2026-10-06",
            "target": cell.target,
            "horizon_days": cell.horizon_days,
            "baseline_direction": cell.direction,
            "event_factor_direction": "up",
            "event_factor_confidence": 0.8,
            "event_adjusted_direction": "up" if change and cell.horizon_days != 1 else cell.direction,
            "fusion_rule": "R2" if change and cell.horizon_days != 1 else "R4",
            "switch_reason": None,
            "supporting_event_ids": ["event-1"],
            "metadata": {},
        }
        for cell in batch.cells
    ]


def mock_issue(monkeypatch, baseline, rows):
    from app import prediction_main

    snapshot = SimpleNamespace(persist=lambda: None, load=None, candidates=None, sha256="a" * 64)
    monkeypatch.setattr(prediction_main, "capture_main_inputs", lambda cutoff: snapshot)
    monkeypatch.setattr(forecast_module, "build_seven_product_forecast", lambda **kwargs: baseline)
    monkeypatch.setattr(event_fusion, "detect_contradictions", lambda factors: [])
    monkeypatch.setattr(event_fusion, "fuse_batch", lambda **kwargs: {"rows": rows, "switch_count": 14})
    return lambda: runner._issue_fused_or_baseline(datetime(2026, 10, 6, tzinfo=UTC), {"product_factors": {}})


def test_real_switch_branch_is_validated_audited_and_baseline_untouched(baseline, monkeypatch):
    original = baseline.model_dump(mode="json")
    issue = mock_issue(monkeypatch, baseline, rows_for(baseline))
    saved, fusion = issue()
    assert fusion["status"] == "ok"
    assert fusion["cells_changed"] == 14
    assert fusion["audit_status"] == "committed_with_forecast"
    assert baseline.model_dump(mode="json") == original
    assert saved.batch_id != baseline.batch_id
    assert all(
        item.forecast.direction == ("uncertain" if item.forecast.horizon_days == 1 else "up") for item in saved.cells
    )
    audits = storage.list_forecast_event_factors(batch_id=saved.batch_id)
    assert len(audits) == 21
    assert {row["event_adjusted_direction"] for row in audits if row["horizon_days"] == 7} == {"up"}
    assert all(len(item.forecast.configuration_sha256) == 64 for item in saved.cells)
    again, summary = issue()
    assert again.payload_sha256 == saved.payload_sha256
    assert summary["reason"] == "existing_batch_reused"
    assert len(storage.list_forecast_event_factors(batch_id=saved.batch_id)) == 21


@pytest.mark.parametrize("fault", ["missing", "duplicate", "target", "direction", "baseline", "d1", "date"])
def test_invalid_results_issue_complete_baseline_without_audit(baseline, monkeypatch, fault):
    rows = rows_for(baseline)
    if fault == "missing":
        rows.pop()
    elif fault == "duplicate":
        rows[-1] = deepcopy(rows[0])
    elif fault == "target":
        rows[0]["target"] = "unknown"
    elif fault == "direction":
        rows[1]["event_adjusted_direction"] = "bogus"
    elif fault == "baseline":
        rows[1]["baseline_direction"] = "up"
    elif fault == "d1":
        rows[0]["event_adjusted_direction"] = "up"
    elif fault == "date":
        rows[1]["business_date"] = "2026-10-05"
    original = baseline.model_dump(mode="json")
    saved, fusion = mock_issue(monkeypatch, baseline, rows)()
    assert fusion["status"] == "degraded"
    assert fusion["fallback"] == "complete_price_baseline"
    assert fusion["cells_changed"] == 0
    assert saved.batch_id == baseline.batch_id
    assert baseline.model_dump(mode="json") == original
    assert [item.forecast.direction for item in saved.cells] == [cell.direction for cell in baseline.cells]
    assert storage.list_forecast_event_factors(batch_id=saved.batch_id) == []


def test_mid_audit_failure_rolls_back_ledger_and_all_audits_before_fallback(baseline, monkeypatch):
    real_upsert = event_fusion.upsert_forecast_event_factor
    count = 0

    def fail_after_write(**kwargs):
        nonlocal count
        result = real_upsert(**kwargs)
        count += 1
        if count == 3:
            raise RuntimeError("injected audit failure after third SQL write")
        return result

    monkeypatch.setattr(event_fusion, "upsert_forecast_event_factor", fail_after_write)
    saved, fusion = mock_issue(monkeypatch, baseline, rows_for(baseline))()
    assert fusion["status"] == "degraded"
    assert saved.batch_id == baseline.batch_id
    with closing(storage.connect_readonly()) as db:
        assert db.execute("SELECT COUNT(*) FROM forecast_event_factors").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM seven_product_forecast_batches").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM seven_product_forecast_cells").fetchone()[0] == 21


def test_no_switch_keeps_baseline_identity_and_writes_complete_audit(baseline, monkeypatch):
    saved, fusion = mock_issue(monkeypatch, baseline, rows_for(baseline, change=False))()
    assert fusion["status"] == "ok"
    assert fusion["cells_changed"] == 0
    assert saved.batch_id == baseline.batch_id
    assert len(storage.list_forecast_event_factors(batch_id=saved.batch_id)) == 21


def test_ledger_commit_failure_rolls_back_audits(baseline, monkeypatch):
    from app import seven_product_forecast_ledger as ledger

    candidate, rows, changed = runner._validated_fused_candidate(baseline, rows_for(baseline))

    def fail_readback(*args, **kwargs):
        raise ValueError("injected readback failure")

    monkeypatch.setattr(ledger, "_load_batch_locked", fail_readback)
    with pytest.raises(ledger.SevenProductForecastLedgerError):
        save_seven_product_forecast_batch(candidate, event_factor_rows=rows)
    with closing(storage.connect_readonly()) as db:
        assert db.execute("SELECT COUNT(*) FROM forecast_event_factors").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM seven_product_forecast_batches").fetchone()[0] == 0


def test_real_rule_engine_switch_is_audited_as_same_issued_direction(baseline, monkeypatch):
    from app import live_memory_policy

    monkeypatch.setattr(live_memory_policy, "reflection_enabled", lambda: False)
    report = {
        "business_date": "2026-10-06",
        "product_factors": {
            "naphtha": {
                "factor_by_horizon": {"d7": {"direction": "down", "confidence": 0.8}},
                "supporting_event_ids": ["ev-a"],
            }
        },
        "artifacts": [
            {
                "stage": "historical_analog",
                "input_refs": {"event_id": "ev-a"},
                "output": {"prior": {"direction": "down", "support_count": 3}, "analog_top3": []},
            }
        ],
    }
    fusion = event_fusion.fuse_batch(batch=baseline, chain_report=report)
    candidate, rows, changed = runner._validated_fused_candidate(baseline, fusion["rows"])
    assert changed == 1
    saved = save_seven_product_forecast_batch(candidate, event_factor_rows=rows)
    selected = next(
        item.forecast for item in saved.cells if item.forecast.target == "naphtha" and item.forecast.horizon_days == 7
    )
    assert selected.direction == "down"
    audit = next(
        row
        for row in storage.list_forecast_event_factors(batch_id=saved.batch_id)
        if row["target"] == "naphtha" and row["horizon_days"] == 7
    )
    assert audit["fusion_rule"] == "R2"
    assert audit["baseline_direction"] == "uncertain"
    assert audit["event_adjusted_direction"] == selected.direction


def test_replay_never_silently_repairs_missing_legacy_audit(baseline):
    from app import seven_product_forecast_ledger as ledger

    candidate, rows, changed = runner._validated_fused_candidate(baseline, rows_for(baseline))
    saved = save_seven_product_forecast_batch(candidate)
    with pytest.raises(ledger.SevenProductForecastLedgerError, match="existing_batch_audit_mismatch"):
        save_seven_product_forecast_batch(candidate, event_factor_rows=rows)
    assert storage.list_forecast_event_factors(batch_id=saved.batch_id) == []


def test_standalone_factor_batch_write_is_atomic(baseline, monkeypatch):
    real_upsert = event_fusion.upsert_forecast_event_factor
    count = 0

    def fail_midway(**kwargs):
        nonlocal count
        result = real_upsert(**kwargs)
        count += 1
        if count == 2:
            raise RuntimeError("failed second audit")
        return result

    monkeypatch.setattr(event_fusion, "upsert_forecast_event_factor", fail_midway)
    with pytest.raises(RuntimeError):
        event_fusion.store_event_factor_rows(rows_for(baseline))
    assert storage.list_forecast_event_factors(batch_id=baseline.batch_id) == []

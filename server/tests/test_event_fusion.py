from __future__ import annotations

from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from test_intelligence_migration import isolated_database  # noqa: F401

from app import storage as app_storage
from app.event_fusion import (
    detect_contradictions,
    fuse_batch,
    fuse_cell,
    prior_by_event_from_chain_report,
    run_event_fusion_step,
    settle_event_factor_outcomes,
    store_event_factor_rows,
)


def _factor(direction: str, confidence: float) -> dict:
    return {"direction": direction, "strength": 0.5, "confidence": confidence}


def test_fuse_cell_rules_r1_through_r4() -> None:
    # R1: same direction, strong factor → confirm baseline.
    result = fuse_cell(baseline_direction="up", factor_direction="up", factor_confidence=0.7)
    assert result.rule == "R1" and result.adjusted_direction == "up"
    # R2: neutral baseline + directional factor + supporting prior → switch.
    result = fuse_cell(
        baseline_direction="neutral",
        factor_direction="up",
        factor_confidence=0.7,
        prior_direction="up",
        prior_support_count=3,
    )
    assert result.rule == "R2" and result.adjusted_direction == "up"
    # R2 also covers an opposite directional baseline with prior support.
    result = fuse_cell(
        baseline_direction="up",
        factor_direction="down",
        factor_confidence=0.7,
        prior_direction="down",
        prior_support_count=4,
    )
    assert result.rule == "R2" and result.adjusted_direction == "down"
    # R3: disagreement without prior support → keep baseline, record分歧.
    result = fuse_cell(
        baseline_direction="up",
        factor_direction="down",
        factor_confidence=0.7,
        prior_direction=None,
    )
    assert result.rule == "R3" and result.adjusted_direction == "up"
    result = fuse_cell(
        baseline_direction="neutral",
        factor_direction="up",
        factor_confidence=0.7,
        prior_direction=None,
        prior_support_count=0,
    )
    assert result.rule == "R3" and result.adjusted_direction == "neutral"
    # support 1 alone stays R3 (sup1 challenger rejected on clean screen)
    result = fuse_cell(
        baseline_direction="neutral",
        factor_direction="up",
        factor_confidence=0.7,
        prior_direction="up",
        prior_support_count=1,
    )
    assert result.rule == "R3" and result.adjusted_direction == "neutral"
    # R3 even with a mismatched prior.
    result = fuse_cell(
        baseline_direction="up",
        factor_direction="down",
        factor_confidence=0.7,
        prior_direction="up",
        prior_support_count=5,
    )
    assert result.rule == "R3"
    # R4: weak factor never moves anything.
    result = fuse_cell(
        baseline_direction="up",
        factor_direction="down",
        factor_confidence=0.4,
        prior_direction="down",
        prior_support_count=9,
    )
    assert result.rule == "R4" and result.adjusted_direction == "up"
    # A neutral factor cannot create a direction against a directional baseline.
    result = fuse_cell(
        baseline_direction="down",
        factor_direction="neutral",
        factor_confidence=0.9,
        prior_direction="up",
        prior_support_count=9,
    )
    assert result.rule == "R4" and result.adjusted_direction == "down"


def _chain_report() -> dict:
    return {
        "schema_version": "event_agent_chain_report.v1",
        "status": "ok",
        "business_date": "2026-09-26",
        "input_sha256": "c" * 64,
        "product_factors": {
            product: {
                "factor_by_horizon": {
                    "d1": {"direction": "neutral", "strength": 0.0, "confidence": 0.0},
                    "d7": _factor("up", 0.7),
                    "d30": {"direction": "neutral", "strength": 0.0, "confidence": 0.0},
                },
                "skeptic_verdict": "维持",
                "supporting_event_ids": ["ev-0"],
            }
            for product in ("crude", "naphtha", "px", "pta", "meg", "poy", "dty")
        },
        "artifacts": [
            {
                "stage": "historical_analog",
                "input_refs": {"event_id": "ev-0"},
                "output": {
                    "prior": {"direction": "up", "support_count": 3},
                    "analog_top3": [],
                },
            }
        ],
    }


def _batch() -> SimpleNamespace:
    return SimpleNamespace(
        batch_id="batch-1",
        cells=[
            SimpleNamespace(target=product, horizon_days=7, direction="neutral", formal_status="formal")
            for product in ("crude", "naphtha", "px", "pta", "meg", "poy", "dty")
        ],
    )


def test_prior_lookup_and_batch_fusion_r2_switch() -> None:
    chain_report = _chain_report()
    priors = prior_by_event_from_chain_report(chain_report)
    # Legacy single prior is projected to every horizon (term-structured shape
    # with an optional magnitude channel).
    assert priors == {
        "ev-0": {
            "d1": {"direction": "up", "support_count": 3, "median_magnitude_pct": None},
            "d7": {"direction": "up", "support_count": 3, "median_magnitude_pct": None},
            "d30": {"direction": "up", "support_count": 3, "median_magnitude_pct": None},
        }
    }
    report = fuse_batch(batch=_batch(), chain_report=chain_report)
    assert report["schema_version"] == "event_fusion_report.v1"
    assert report["batch_id"] == "batch-1"
    assert len(report["rows"]) == 7
    # Baseline neutral + confident up factor + supporting prior → R2 switch on d7.
    assert report["switch_count"] == 7
    for row in report["rows"]:
        assert row["fusion_rule"] == "R2"
        assert row["event_adjusted_direction"] == "up"
        assert row["factor_id"].startswith("batch-1:")
        assert row["metadata"]["prior_support"] == 3


def test_contradiction_detection_and_adjudicated_override() -> None:
    factors = {
        "crude": {"factor_by_horizon": {"d7": _factor("up", 0.7)}},
        "naphtha": {"factor_by_horizon": {"d7": _factor("down", 0.7)}},
    }
    contradictions = detect_contradictions(factors)
    assert len(contradictions) == 1
    assert contradictions[0]["products"] == ["crude", "naphtha"]
    # Neutral or agreeing factors produce no contradiction.
    assert detect_contradictions({"crude": {"factor_by_horizon": {"d7": _factor("up", 0.7)}}}) == []

    adjudicated = {
        "crude": {
            "factor_by_horizon": {"d7": _factor("down", 0.8)},
            "supporting_event_ids": ["ev-0"],
        }
    }
    report = fuse_batch(batch=_batch(), chain_report=_chain_report(), adjudicated_factors=adjudicated)
    crude_row = next(row for row in report["rows"] if row["target"] == "crude")
    # Audit P0-2: adjudicated factors re-run the guardrails. Prior supports up
    # at d7 (up x3) but the adjudicated factor is down -> R5R3 keeps baseline.
    assert crude_row["fusion_rule"].startswith("R5")
    assert crude_row["event_factor_direction"] == "down"
    assert crude_row["event_adjusted_direction"] == "neutral"  # guardrail held


def test_store_and_dual_track_settlement(isolated_database: Path) -> None:  # noqa: F811
    report = fuse_batch(batch=_batch(), chain_report=_chain_report())
    assert store_event_factor_rows(report["rows"]) == 7
    sha_a = "a" * 64
    sha_e = "e" * 64
    sha_f = "f" * 64
    with closing(app_storage.connect()) as connection:
        assert (
            connection.execute("SELECT outcome_baseline FROM forecast_event_factors LIMIT 1").fetchone()[
                "outcome_baseline"
            ]
            is None
        )

    # Simulate the issued batch + one settled ledger outcome: baseline (neutral)
    # missed, actual moved up. Baseline hit=0; adjusted (up) hit=1.
    with closing(app_storage.connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO seven_product_forecast_batches (
              batch_id, business_date, schema_version, as_of_time, generated_at,
              persisted_at, model_registry_revision, data_snapshot_sha256,
              configuration_sha256, formal_count, reference_count, unavailable_count,
              contract_complete, payload, payload_sha256
            ) VALUES (?, ?, 'seven-product-forecast.v1', ?, ?, ?, 'rev-test', ?, ?, 21, 0, 0, 1, '{}', ?)
            """,
            (
                "batch-1",
                "2026-09-26",
                "2026-09-26T00:00:00+00:00",
                "2026-09-26T00:00:00+00:00",
                "2026-09-26T00:00:00+00:00",
                sha_a,
                sha_a,
                sha_f,
            ),
        )
        connection.execute(
            """
            INSERT INTO seven_product_forecast_cells (
              cell_id, batch_id, target, horizon_days, label_series_id, model_version,
              feature_version, label_registry_version, neutral_band_policy_version,
              evaluation_status, model_registry_revision, point_forecast,
              neutral_band_pct, predicted_direction, unit, data_snapshot_sha256,
              configuration_sha256, cell_payload, cell_sha256
            ) VALUES (?, ?, 'crude', 7, 'ls', 'mv', 'fv', 'lrv', 'policy', 'passed',
                      'rev-test', 78.0, 0.01, 'neutral', 'USD/bbl', ?, ?, '{}', ?)
            """,
            ("cell-1", "batch-1", sha_a, sha_a, sha_f),
        )
        connection.execute(
            """
            INSERT INTO seven_product_forecast_outcomes (
              outcome_id, cell_id, batch_id, target, horizon_days, settled_at,
              actual_observation_id, actual_observed_at, actual_visible_at,
              actual_source_id, actual_source_url, actual_raw_sha256,
              actual_value, actual_unit, point_forecast, absolute_error,
              absolute_percentage_error, predicted_direction, actual_direction,
              direction_hit, outcome_payload, outcome_sha256
            ) VALUES (?, 'cell-1', 'batch-1', 'crude', 7, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'neutral', 'up', 0, '{}', ?)
            """,  # noqa: E501 -- Preserve this SQL fixture text verbatim.
            (
                "out-1",
                "2026-10-03T00:00:00+00:00",
                "obs-1",
                "2026-10-03T00:00:00+00:00",
                "2026-10-03T00:00:00+00:00",
                "src",
                "https://example.test",
                sha_e,
                80.0,
                "USD/bbl",
                78.0,
                2.0,
                0.025,
                sha_f,
            ),
        )

    summary = settle_event_factor_outcomes()
    # Only the crude:7 cell has a settled ledger outcome; the other six factor
    # rows stay pending until their own cells mature.
    assert summary["settled"] == 1
    with closing(app_storage.connect()) as connection:
        crude = connection.execute(
            "SELECT outcome_baseline, outcome_adjusted FROM forecast_event_factors WHERE target='crude'"
        ).fetchone()
        others = connection.execute(
            "SELECT outcome_baseline, outcome_adjusted FROM forecast_event_factors WHERE target!='crude' LIMIT 1"
        ).fetchone()
    # Baseline neutral vs actual up → miss; adjusted up vs actual up → hit.
    assert crude["outcome_baseline"] == "miss"
    assert crude["outcome_adjusted"] == "hit"
    assert others["outcome_baseline"] is None  # Only the matching cell settles.

    # Idempotent: already-settled rows are skipped.
    assert settle_event_factor_outcomes()["settled"] == 0


def test_fusion_step_without_port_records_contradictions(isolated_database: Path) -> None:  # noqa: F811
    chain_report = _chain_report()
    chain_report["product_factors"]["naphtha"]["factor_by_horizon"]["d7"] = _factor("down", 0.7)
    report = run_event_fusion_step(
        batch=_batch(),
        chain_report=chain_report,
        business_date="2026-09-26",
        as_of_time="2026-09-26T12:00:00+00:00",
        port=None,
        persist=True,
    )
    assert report["contradictions"] != []
    assert report["adjudicated_products"] == []
    assert report["stored_rows"] == 7

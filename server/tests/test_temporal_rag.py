from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_ROOT))
sys.path.insert(0, str(SERVER_ROOT / "scripts"))

from app.storage import SCHEMA  # noqa: E402

SCRIPT_PATH = SERVER_ROOT / "scripts" / "audit_temporal_rag.py"
SPEC = importlib.util.spec_from_file_location("audit_temporal_rag", SCRIPT_PATH)
assert SPEC is not None
audit_temporal_rag = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["audit_temporal_rag"] = audit_temporal_rag
SPEC.loader.exec_module(audit_temporal_rag)


def test_temporal_rag_detects_future_doc_reference(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        connection.execute(
            """
            INSERT INTO event_observations (
                event_record_id, created_at, source_id, occurred_at, title, event_type,
                evidence_level, summary, affected_products, direction, impact_strength,
                evidence_url, requires_human_review, notes, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "evt_future",
                "2026-01-03T00:00:00+00:00",
                "test",
                "2026-01-03",
                "Future visible event",
                "test",
                "A",
                "summary",
                "[]",
                "中性",
                "low",
                "",
                0,
                "",
                "{}",
            ),
        )
        connection.execute(
            """
            INSERT INTO llm_event_directions (
                judgment_id, created_at, event_id, as_of_time, record_type, source_id,
                category, title, rule_direction, llm_direction, confidence, evidence_level,
                reasoning, counter_evidence, cited_doc_ids, risk_premium_decay,
                demand_weakness_offset, supply_recovery_offset, should_enter_backtest,
                provider, model, latency_ms, prompt_tokens_est, completion_tokens_est,
                fallback, error, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "judgment_1",
                "2026-01-01T00:00:00+00:00",
                "evt_future",
                "2026-01-01T00:00:00+00:00",
                "event",
                "test",
                "test",
                "as-of leak sample",
                "中性",
                "利多",
                0.8,
                "A",
                "Future evidence is incorrectly cited.",
                "",
                '["event:evt_future"]',
                0,
                0,
                0,
                1,
                "deepseek",
                "model",
                1,
                1,
                1,
                0,
                "",
                "{}",
            ),
        )
        connection.commit()

    report = audit_temporal_rag.audit_temporal_rag(
        db_path,
        start=audit_temporal_rag.DEFAULT_START,
        end=audit_temporal_rag.DEFAULT_END,
    )

    assert report["metrics"]["future_leak_count"] == 1
    assert report["metrics"]["visible_at_lte_as_of_time_violation_count"] == 1
    assert report["metrics"]["future_leak_count_entering_backtest"] == 1
    assert report["guardrails"]["visible_at_lte_as_of_time"] is False
    assert report["guardrails"]["formal_scoring_future_leak_count_must_be_zero"] is False


def test_temporal_rag_blocks_prediction_records_and_posterior_payloads(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(SCHEMA)
        connection.execute(
            """
            INSERT INTO prediction_ledger (
                prediction_id, created_at, target, horizon, direction, confidence, rationale,
                counter_evidence, source_status, tags, data_snapshot_id, review_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pred_1",
                "2026-01-01T00:00:00+00:00",
                "Brent",
                "14d",
                "利多",
                0.7,
                "posterior prediction record",
                "",
                "connected",
                "[]",
                None,
                "unreviewed",
            ),
        )
        connection.execute(
            """
            INSERT INTO llm_event_directions (
                judgment_id, created_at, event_id, as_of_time, record_type, source_id,
                category, title, rule_direction, llm_direction, confidence, evidence_level,
                reasoning, counter_evidence, cited_doc_ids, risk_premium_decay,
                demand_weakness_offset, supply_recovery_offset, should_enter_backtest,
                provider, model, latency_ms, prompt_tokens_est, completion_tokens_est,
                fallback, error, raw
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "judgment_2",
                "2026-01-02T00:00:00+00:00",
                "evt_2",
                "2026-01-02T00:00:00+00:00",
                "event",
                "test",
                "test",
                "posterior payload sample",
                "中性",
                "利多",
                0.8,
                "A",
                "as-of reasoning",
                "",
                '["prediction:pred_1"]',
                0,
                0,
                0,
                1,
                "deepseek",
                "model",
                1,
                1,
                1,
                0,
                "",
                json.dumps({"miss_reason": "priced_in", "verdict": "miss"}),
            ),
        )
        connection.commit()

    report = audit_temporal_rag.audit_temporal_rag(
        db_path,
        start=audit_temporal_rag.DEFAULT_START,
        end=audit_temporal_rag.DEFAULT_END,
    )

    assert report["suite"] == "eval-v2-rag-leak-audit"
    assert report["metrics"]["prediction_doc_refs"] == 1
    assert report["metrics"]["backtest_or_error_refs"] == 1
    assert report["guardrails"]["prediction_phase_blocks_posterior_price_docs"] is False
    assert report["guardrails"]["posterior_backtest_error_context_blocked"] is False

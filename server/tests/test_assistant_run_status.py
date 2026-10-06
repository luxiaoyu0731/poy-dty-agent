"""assistant-status.v2 read-layer projection tests (2026-09-16 口径修复).

Delivery status and quality gates are separated. The ledger is never
rewritten: legacy ``needs_human_review`` runs are projected to ``completed``
with ``derived_from_legacy`` plus a quality annotation derived from the
persisted guard-stage risk flags.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app import assistant_run_status, pipeline_graph, storage
from app.agent_trace_ledger import finalize_assistant_run
from app.settings import settings


@pytest.fixture()
def isolated_db(tmp_path: Path) -> Path:
    database = tmp_path / "assistant-status-test.db"
    object.__setattr__(settings, "sqlite_path", str(database))
    with closing(storage.connect()):
        pass
    return database


def _insert_run(database: Path, run_id: str, status: str, *, source: str = "assistant_pipeline",
                trace_type: str = "assistant_governed_run", metadata: dict | None = None,
                guard_flags: list[str] | None = None) -> None:
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(
            """
            INSERT INTO agent_runs (run_id, created_at, updated_at, name, agent_name, goal, status,
              source, trace_type, started_at, finished_at, metadata)
            VALUES (?, '2026-09-16T05:00:00+00:00', '2026-09-16T05:00:20+00:00',
              'Assistant governed answer', '任务编排', ?, ?, ?, ?, NULL, NULL, ?)
            """,
            (run_id, f"question for {run_id}", status, source, trace_type,
             json.dumps(metadata or {})),
        )
        if guard_flags is not None:
            connection.execute(
                """
                INSERT INTO agent_turns (
                  turn_id, run_id, agent_name, agent_role, round_index, started_at, status,
                  input_summary, input_payload_ref, prompt_version, tool_permissions_version,
                  evidence_ids, graph_path_ids, memory_item_ids, output_summary, output_payload_ref,
                  output_type, confidence, risk_flags, handoff_to, handoff_reason, retry_count,
                  failure_reason, human_review_status, human_review_result, metadata
                ) VALUES (?, ?, '质量复核', '质量复核', 3, '2026-09-16T05:00:10+00:00', 'degraded',
                  's', '', 'pv', '1', '[]', '[]', '[]', 's', '', 'reasoning_result', 0.0, ?,
                  '', '', 0, ?, '待复核', '', '{}')
                """,
                (f"{run_id}-guard", run_id, json.dumps(guard_flags), ",".join(guard_flags)),
            )
        connection.commit()


def test_legacy_review_run_is_derived_to_completed_with_gate_annotation(isolated_db: Path) -> None:
    _insert_run(
        isolated_db,
        "legacy-flagged",
        "needs_human_review",
        guard_flags=["formal_evidence_gate_not_passed", "claim_entailment_gate_not_passed"],
    )

    presented = assistant_run_status.present_assistant_run(storage.get_agent_run("legacy-flagged"))

    assert presented["status"] == "completed"
    assert presented["derived_status"] == "derived_from_legacy"
    quality = presented["quality"]
    assert quality["overall"] == "passed_with_flags"
    failed = {gate["name"] for gate in quality["gates"] if not gate["passed"]}
    assert failed == {"formal_evidence_gate", "claim_entailment_gate"}
    assert all(gate["reason"] for gate in quality["gates"] if not gate["passed"])
    # The ledger row itself is untouched.
    with closing(sqlite3.connect(isolated_db)) as connection:
        assert connection.execute(
            "SELECT status FROM agent_runs WHERE run_id='legacy-flagged'"
        ).fetchone()[0] == "needs_human_review"


def test_legacy_clean_run_derives_all_passed(isolated_db: Path) -> None:
    _insert_run(isolated_db, "legacy-clean", "needs_human_review", guard_flags=[])

    presented = assistant_run_status.present_assistant_run(storage.get_agent_run("legacy-clean"))

    assert presented["status"] == "completed"
    assert presented["derived_status"] == "derived_from_legacy"
    assert presented["quality"]["overall"] == "all_passed"
    assert all(gate["passed"] for gate in presented["quality"]["gates"])


def test_v2_run_metadata_quality_passes_through(isolated_db: Path) -> None:
    _insert_run(
        isolated_db,
        "v2-run",
        "completed",
        metadata={
            "status_vocabulary": "assistant-status.v2",
            "quality": {"overall": "passed_with_flags", "gates": [
                {"name": "evidence_conflict", "label": "冲突检查", "passed": False, "reason": "r"}
            ], "flags": ["evidence_conflict"]},
        },
    )

    presented = assistant_run_status.present_assistant_run(storage.get_agent_run("v2-run"))

    assert presented["status"] == "completed"
    assert "derived_status" not in presented
    assert presented["status_vocabulary"] == "assistant-status.v2"
    assert presented["quality"]["overall"] == "passed_with_flags"


def test_non_assistant_and_failed_runs_pass_through(isolated_db: Path) -> None:
    _insert_run(isolated_db, "other-run", "needs_human_review", source="legacy_role")
    _insert_run(isolated_db, "failed-run", "failed", source="assistant_pipeline")

    other = assistant_run_status.present_assistant_run(storage.get_agent_run("other-run"))
    failed = assistant_run_status.present_assistant_run(storage.get_agent_run("failed-run"))

    assert other["status"] == "needs_human_review"
    assert "derived_status" not in other
    assert failed["status"] == "failed"
    assert "derived_status" not in failed


def test_finalize_rejects_legacy_vocabulary_for_new_writes(isolated_db: Path) -> None:

    run_id = "finalize-vocab"
    storage.create_agent_run(
        run_id=run_id,
        payload={
            "name": "Assistant governed answer",
            "agent_name": "任务编排",
            "goal": "q",
            "status": "running",
            "source": "assistant_pipeline",
            "trace_type": "assistant_governed_run",
            "metadata": {},
        },
    )
    with pytest.raises(ValueError, match="unsupported assistant run terminal status"):
        finalize_assistant_run(run_id=run_id, status="needs_human_review")


def test_agent_runs_api_projects_legacy_vocabulary(isolated_db: Path) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    _insert_run(
        isolated_db,
        "api-legacy",
        "needs_human_review",
        guard_flags=["formal_evidence_gate_not_passed"],
    )
    original_enforce = settings.enforce_internal_token
    object.__setattr__(settings, "enforce_internal_token", False)
    try:
        with TestClient(app) as client:
            listed = client.get("/api/v1/agent-runs", params={"limit": 10}).json()
            item = next(run for run in listed if run["run_id"] == "api-legacy")
            assert item["status"] == "completed"
            assert item["derived_status"] == "derived_from_legacy"
            assert item["quality"]["overall"] == "passed_with_flags"

            detail = client.get("/api/v1/agent-runs/api-legacy").json()
            assert detail["status"] == "completed"
            assert detail["derived_status"] == "derived_from_legacy"
            failed_gates = {gate["name"] for gate in detail["quality"]["gates"] if not gate["passed"]}
            assert failed_gates == {"formal_evidence_gate"}
    finally:
        object.__setattr__(settings, "enforce_internal_token", original_enforce)


def test_pipeline_graph_assistant_node_uses_derived_vocabulary(
    isolated_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PIPELINE_GRAPH_STATE_ROOT", str(tmp_path))
    _insert_run(
        isolated_db,
        "graph-legacy",
        "needs_human_review",
        guard_flags=["formal_evidence_gate_not_passed"],
    )

    node = next(
        node for node in pipeline_graph.build_pipeline_graph("2026-09-16")["nodes"]
        if node["id"] == "assistant"
    )
    assert node["status"] == "ok"

    detail = pipeline_graph.build_pipeline_node_detail("assistant", "2026-09-16")
    run = detail["agent_extra"]["recent_runs"][0]
    assert run["run_id"] == "graph-legacy"
    assert run["status"] == "completed"
    assert run["derived_status"] == "derived_from_legacy"
    assert "formal_evidence_gate_not_passed" in run["gate_result"]

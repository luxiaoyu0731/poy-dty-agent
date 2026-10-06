from __future__ import annotations

import json
from contextlib import closing

import pytest

from app import storage
from app.agent_runtime import (
    bootstrap_agent_jobs,
    create_agent_job,
    create_checkpoint,
    record_context_runtime_steps,
    record_job_attempt,
    summarize_agent_execution,
)
from app.settings import settings


def test_agent_execution_summary_does_not_call_materialized_jobs_executed(
    tmp_path,
) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "agent-execution.db"))
    run_id = "execution-semantics"
    try:
        storage.create_agent_run(
            run_id=run_id,
            payload={"name": "daily", "goal": "daily", "status": "running"},
        )
        jobs = bootstrap_agent_jobs(run_id)["jobs"]

        assert {job["status"] for job in jobs} == {"materialized"}

        materialized = summarize_agent_execution(run_id)

        assert materialized["mode"] == "materialized_only"
        assert materialized["materialized_job_count"] == 12
        assert materialized["executed_job_count"] == 0
        assert materialized["customer_label"] == "流程账本已建立，任务尚未实际执行"

        record_job_attempt(run_id=run_id, job_id=jobs[0]["job_id"], status="completed")
        executed = summarize_agent_execution(run_id)

        assert executed["mode"] == "runtime_execution"
        assert executed["executed_job_count"] == 1
        assert executed["completed_job_count"] == 1

        degraded_attempt = record_job_attempt(
            run_id=run_id,
            job_id=jobs[1]["job_id"],
            status="failed",
            failure_reason="provider unavailable",
        )
        assert degraded_attempt["status"] == "degraded"
        rejected_attempt = record_job_attempt(
            run_id=run_id,
            job_id=jobs[2]["job_id"],
            status="rejected",
            failure_reason="policy denied",
        )
        assert rejected_attempt["status"] == "rejected"
        with pytest.raises(ValueError, match="not a runtime attempt"):
            record_job_attempt(run_id=run_id, job_id=jobs[3]["job_id"], status="pending")

        mixed = summarize_agent_execution(run_id)
        assert mixed["executed_job_count"] == 2
        assert mixed["completed_job_count"] == 1

        attempted = record_job_attempt(run_id=run_id, job_id=jobs[4]["job_id"], status="running")
        assert attempted["status"] == "attempted"
        assert summarize_agent_execution(run_id)["executed_job_count"] == 3

        rejected_only_run_id = "rejected-only-semantics"
        storage.create_agent_run(
            run_id=rejected_only_run_id,
            payload={"name": "rejected", "goal": "rejected", "status": "running"},
        )
        rejected_jobs = bootstrap_agent_jobs(rejected_only_run_id)["jobs"]
        record_job_attempt(
            run_id=rejected_only_run_id,
            job_id=rejected_jobs[0]["job_id"],
            status="rejected",
            failure_reason="policy denied",
        )
        rejected_only = summarize_agent_execution(rejected_only_run_id)
        assert rejected_only["mode"] == "materialized_only"
        assert rejected_only["executed_job_count"] == 0
        assert rejected_only["completed_job_count"] == 0
        assert rejected_only["customer_label"] == "流程账本已建立，任务尚未实际执行"
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def test_context_runtime_steps_record_completed_and_degraded_provenance(tmp_path) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "context-runtime-provenance.db"))
    run_id = "context-runtime-provenance"
    try:
        storage.create_agent_run(
            run_id=run_id,
            payload={"name": "context runtime", "goal": "record actual steps", "status": "running"},
        )
        bootstrap_agent_jobs(run_id)

        result = record_context_runtime_steps(
            run_id=run_id,
            contribution={
                "runtime_steps": [
                    {"name": "graph_snapshot", "status": "completed", "latency_ms": 4, "output": {}},
                    {"name": "memory_retrieval", "status": "degraded", "latency_ms": 2, "output": {}},
                    {"name": "report_generation", "status": "completed", "latency_ms": 1, "output": {}},
                ]
            },
        )

        assert result["actual_step_count"] == 2
        assert [item["status"] for item in result["recorded"]] == ["completed", "degraded"]
        assert result["ignored"] == ["report_generation"]
        summary = summarize_agent_execution(run_id)
        assert summary["executed_job_count"] == 2
        assert summary["completed_job_count"] == 1
        with closing(storage.connect()) as connection, connection:
            payloads = [
                json.loads(row["payload"])
                for row in connection.execute(
                    "SELECT payload FROM agent_checkpoints WHERE run_id = ? ORDER BY created_at, checkpoint_id",
                    (run_id,),
                ).fetchall()
            ]
        assert {item["step_name"] for item in payloads} == {"graph_snapshot", "memory_retrieval"}
        assert all(item["provenance"] == "graph_memory_context.runtime_steps" for item in payloads)
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def test_unknown_role_job_and_cross_run_checkpoint_are_zero_write(tmp_path) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "agent-runtime-integrity.db"))
    try:
        storage.create_agent_run(run_id="run-a", payload={"name": "a", "goal": "a", "status": "running"})
        storage.create_agent_run(run_id="run-b", payload={"name": "b", "goal": "b", "status": "running"})
        with pytest.raises(PermissionError, match="unknown agent role"):
            create_agent_job(run_id="run-a", agent_name="unknown-agent", title="must fail")

        job = create_agent_job(run_id="run-a", agent_name="任务编排", title="known")
        with pytest.raises(ValueError, match="job does not belong to run"):
            create_checkpoint(
                run_id="run-b",
                job_id=job["job_id"],
                checkpoint_type="invalid",
                summary="must fail",
            )

        with closing(storage.connect()) as connection, connection:
            assert connection.execute("SELECT COUNT(*) FROM agent_jobs").fetchone()[0] == 1
            assert connection.execute("SELECT COUNT(*) FROM agent_checkpoints").fetchone()[0] == 0
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def test_job_attempt_transition_and_retry_contract(tmp_path) -> None:
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "agent-attempt-transitions.db"))
    try:
        storage.create_agent_run(run_id="attempt-run", payload={"name": "a", "goal": "a", "status": "running"})
        jobs = bootstrap_agent_jobs("attempt-run")["jobs"]

        record_job_attempt(run_id="attempt-run", job_id=jobs[0]["job_id"], status="attempted")
        with pytest.raises(ValueError, match="already in progress"):
            record_job_attempt(run_id="attempt-run", job_id=jobs[0]["job_id"], status="attempted")
        record_job_attempt(run_id="attempt-run", job_id=jobs[0]["job_id"], status="completed")
        with pytest.raises(ValueError, match="terminal"):
            record_job_attempt(run_id="attempt-run", job_id=jobs[0]["job_id"], status="attempted")

        record_job_attempt(
            run_id="attempt-run",
            job_id=jobs[1]["job_id"],
            status="degraded",
            failure_reason="temporary",
            retryable=True,
        )
        record_job_attempt(run_id="attempt-run", job_id=jobs[1]["job_id"], status="attempted")
        record_job_attempt(run_id="attempt-run", job_id=jobs[1]["job_id"], status="completed")

        record_job_attempt(
            run_id="attempt-run",
            job_id=jobs[2]["job_id"],
            status="degraded",
            failure_reason="permanent",
        )
        with pytest.raises(ValueError, match="not retryable"):
            record_job_attempt(run_id="attempt-run", job_id=jobs[2]["job_id"], status="attempted")

        with pytest.raises(ValueError, match="requires failure_reason"):
            record_job_attempt(run_id="attempt-run", job_id=jobs[3]["job_id"], status="rejected")
        with pytest.raises(ValueError, match="cannot be retryable"):
            record_job_attempt(
                run_id="attempt-run",
                job_id=jobs[3]["job_id"],
                status="completed",
                retryable=True,
            )
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)

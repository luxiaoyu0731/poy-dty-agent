from __future__ import annotations

from contextlib import closing
from typing import Any

from .agent_permissions import permissions_for_agent, validate_agent_role
from .foundation_utils import json_dumps, json_loads, new_id, now_iso, safe_summary
from .storage import connect, get_agent_run

AGENT_SEQUENCE = [
    "任务编排",
    "数据接入",
    "数据清洗",
    "行情分析",
    "事件识别",
    "新闻核验",
    "证据检索",
    "图谱构建",
    "推理判断",
    "反证检查",
    "质量复核",
    "报告生成",
]

TRACE_STATUSES = {"materialized", "attempted", "completed", "degraded", "rejected"}
_LEGACY_STATUS_MAP = {
    "pending": "materialized",
    "running": "attempted",
    "success": "completed",
    "succeeded": "completed",
    "failed": "degraded",
    "blocked": "degraded",
}


def create_agent_job(
    *,
    run_id: str,
    agent_name: str,
    title: str,
    input_ref: str = "",
    priority: int = 50,
) -> dict[str, Any]:
    if get_agent_run(run_id) is None:
        raise ValueError("agent run not found")
    validate_agent_role(agent_name)
    job_id = new_id("agent_job")
    now = now_iso()
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO agent_jobs (
              job_id, run_id, created_at, updated_at, agent_name, title, status,
              priority, input_ref, output_ref, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                run_id,
                now,
                now,
                agent_name,
                title,
                "materialized",
                priority,
                input_ref,
                "",
                json_dumps({"permissions": permissions_for_agent(agent_name)}),
            ),
        )
    return get_agent_job(job_id) or {
        "job_id": job_id,
        "run_id": run_id,
        "agent_name": agent_name,
        "status": "materialized",
    }


def bootstrap_agent_jobs(run_id: str) -> dict[str, Any]:
    existing = list_agent_jobs(run_id=run_id)
    if existing:
        return {"created": 0, "jobs": existing}
    jobs = [
        create_agent_job(
            run_id=run_id,
            agent_name=agent_name,
            title=f"{agent_name}任务",
            priority=index * 10,
        )
        for index, agent_name in enumerate(AGENT_SEQUENCE, start=1)
    ]
    return {"created": len(jobs), "jobs": jobs}


def get_agent_job(job_id: str) -> dict[str, Any] | None:
    with closing(connect()) as connection:
        row = connection.execute("SELECT * FROM agent_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return _job_row(row) if row else None


def list_agent_jobs(*, run_id: str, limit: int = 200) -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        rows = connection.execute(
            """
            SELECT * FROM agent_jobs
            WHERE run_id = ?
            ORDER BY priority ASC, created_at ASC
            LIMIT ?
            """,
            (run_id, min(max(limit, 1), 500)),
        ).fetchall()
    return [_job_row(row) for row in rows]


def summarize_agent_execution(run_id: str) -> dict[str, Any]:
    """Separate planned/materialized jobs from jobs with recorded runtime attempts."""
    jobs = list_agent_jobs(run_id=run_id, limit=500)
    with closing(connect()) as connection:
        attempts = connection.execute(
            """
            SELECT job_id, status
            FROM agent_job_attempts
            WHERE run_id = ?
            ORDER BY created_at ASC
            """,
            (run_id,),
        ).fetchall()
    latest_status_by_job: dict[str, str] = {}
    for row in attempts:
        latest_status_by_job[str(row["job_id"])] = str(row["status"] or "")
    executed_count = sum(status in {"attempted", "completed", "degraded"} for status in latest_status_by_job.values())
    completed_count = sum(status in {"completed", "success", "succeeded"} for status in latest_status_by_job.values())
    if executed_count:
        mode = "runtime_execution"
        customer_label = f"已实际执行 {executed_count}/{len(jobs)} 项任务"
    elif jobs:
        mode = "materialized_only"
        customer_label = "流程账本已建立，任务尚未实际执行"
    else:
        mode = "not_started"
        customer_label = "流程任务尚未建立"
    return {
        "run_id": run_id,
        "mode": mode,
        "materialized_job_count": len(jobs),
        "executed_job_count": executed_count,
        "completed_job_count": completed_count,
        "customer_label": customer_label,
    }


def record_job_attempt(
    *,
    run_id: str,
    job_id: str,
    status: str,
    failure_reason: str = "",
    retryable: bool = False,
) -> dict[str, Any]:
    """DEPRECATED (2026-09-16, DESIGN §2.3 layer 2 / 批次4): do not call in production.

    Production callers: 0. The generic agent state machine was superseded by
    ``agent_trace_ledger.begin/finish_assistant_stage`` (the only writer with
    production history), whose atomic job+attempt+turn writes already absorb
    this function's status-machine validation (attempted→completed/degraded is
    single-direction, degraded requires failure_reason). Kept un-deleted as a
    rollback insurance for existing worktree tests; new code must use the
    trace-ledger governed-stage API instead.
    """
    normalized_status = _normalize_trace_status(status)
    if normalized_status == "materialized":
        raise ValueError("materialized is not a runtime attempt status")
    failure_reason = safe_summary(failure_reason, max_chars=500)
    if normalized_status in {"degraded", "rejected"} and not failure_reason:
        raise ValueError(f"{normalized_status} attempt requires failure_reason")
    if normalized_status in {"attempted", "completed", "rejected"} and retryable:
        raise ValueError(f"{normalized_status} attempt cannot be retryable")
    attempt_id = new_id("agent_attempt")
    now = now_iso()
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            job = connection.execute(
                "SELECT run_id, status FROM agent_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if job is None or str(job["run_id"] or "") != run_id:
                raise ValueError("agent job does not belong to run")
            previous_attempt = connection.execute(
                """
                SELECT status, retryable
                FROM agent_job_attempts
                WHERE job_id = ? AND run_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (job_id, run_id),
            ).fetchone()
            expected_status = str(job["status"] or "")
            if previous_attempt is not None and str(previous_attempt["status"] or "") != expected_status:
                raise ValueError("agent job status does not match latest attempt")
            previous_retryable = bool(previous_attempt["retryable"]) if previous_attempt else False
            _validate_attempt_transition(
                current_status=expected_status,
                next_status=normalized_status,
                previous_retryable=previous_retryable,
            )
            connection.execute(
                """
                INSERT INTO agent_job_attempts (
                  attempt_id, job_id, run_id, created_at, started_at, finished_at, status,
                  retryable, failure_reason, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    job_id,
                    run_id,
                    now,
                    now,
                    now,
                    normalized_status,
                    int(retryable),
                    failure_reason,
                    json_dumps({}),
                ),
            )
            cursor = connection.execute(
                """
                UPDATE agent_jobs
                SET status = ?, updated_at = ?
                WHERE job_id = ? AND run_id = ? AND status = ?
                """,
                (normalized_status, now, job_id, run_id, expected_status),
            )
            if cursor.rowcount != 1:
                raise ValueError("agent job status changed concurrently")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {"attempt_id": attempt_id, "job_id": job_id, "run_id": run_id, "status": normalized_status}


def create_checkpoint(
    *,
    run_id: str,
    checkpoint_type: str,
    summary: str,
    status: str = "recorded",
    job_id: str | None = None,
    turn_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if get_agent_run(run_id) is None:
        raise ValueError("agent run not found")
    if job_id is not None:
        job = get_agent_job(job_id)
        if job is None or str(job.get("run_id") or "") != run_id:
            raise ValueError("agent job does not belong to run")
    if turn_id is not None:
        _validate_turn_reference(run_id=run_id, turn_id=turn_id, job_id=job_id)
    checkpoint_id = new_id("checkpoint")
    now = now_iso()
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO agent_checkpoints (
              checkpoint_id, run_id, job_id, turn_id, created_at, checkpoint_type,
              status, summary, payload, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint_id,
                run_id,
                job_id,
                turn_id,
                now,
                checkpoint_type,
                status,
                safe_summary(summary, max_chars=800),
                json_dumps(payload or {}),
                json_dumps({}),
            ),
        )
    return {"checkpoint_id": checkpoint_id, "run_id": run_id, "status": status}


def record_context_runtime_steps(
    *,
    run_id: str,
    contribution: dict[str, Any],
) -> dict[str, Any]:
    """DEPRECATED (2026-09-16, DESIGN §2.3 layer 2 / 批次4): legacy 12-role scaffold helper.

    Production callers: 0 (same retirement as ``record_job_attempt``). The
    12-role taxonomy is read-only history per CONSENSUS 共识4; retained only as
    rollback insurance for existing worktree tests.

    This function never marks unrelated jobs as completed and rejects unknown
    synthetic step names, so the materialized Agent view cannot imply execution
    that did not occur.
    """

    jobs = {job["agent_name"]: job for job in list_agent_jobs(run_id=run_id)}
    agent_by_step = {
        "graph_snapshot": "图谱构建",
        "graph_reasoning": "推理判断",
        "memory_retrieval": "证据检索",
    }
    recorded: list[dict[str, Any]] = []
    ignored: list[str] = []
    for step in contribution.get("runtime_steps", []):
        step_name = str(step.get("name") or "")
        agent_name = agent_by_step.get(step_name)
        if agent_name is None or agent_name not in jobs:
            ignored.append(step_name)
            continue
        job = jobs[agent_name]
        step_status = str(step.get("status") or "degraded")
        attempt_status = "completed" if step_status == "completed" else "degraded"
        attempt = record_job_attempt(
            run_id=run_id,
            job_id=job["job_id"],
            status=attempt_status,
            failure_reason="" if attempt_status == "completed" else f"{step_name} degraded",
            retryable=attempt_status != "completed",
        )
        checkpoint = create_checkpoint(
            run_id=run_id,
            job_id=job["job_id"],
            checkpoint_type="actual_context_step",
            status=attempt_status,
            summary=f"{agent_name}实际执行 {step_name}",
            payload={
                "step_name": step_name,
                "latency_ms": step.get("latency_ms", 0),
                "output": step.get("output", {}),
                "provenance": "graph_memory_context.runtime_steps",
            },
        )
        recorded.append(
            {
                "step_name": step_name,
                "agent_name": agent_name,
                "attempt_id": attempt["attempt_id"],
                "checkpoint_id": checkpoint["checkpoint_id"],
                "status": attempt_status,
            }
        )
    return {"recorded": recorded, "ignored": ignored, "actual_step_count": len(recorded)}


def _job_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["metadata"] = json_loads(item.get("metadata"), {})
    return item


def _normalize_trace_status(status: str) -> str:
    normalized = _LEGACY_STATUS_MAP.get(str(status or "").strip().lower(), str(status or "").strip().lower())
    if normalized not in TRACE_STATUSES:
        raise ValueError(f"unsupported agent trace status: {status}")
    return normalized


def _validate_attempt_transition(*, current_status: str, next_status: str, previous_retryable: bool) -> None:
    if current_status in {"completed", "rejected"}:
        raise ValueError(f"agent job is terminal: {current_status}")
    if current_status == "attempted" and next_status == "attempted":
        raise ValueError("agent job attempt is already in progress")
    if current_status == "degraded" and not previous_retryable:
        raise ValueError("agent job attempt is not retryable")
    if current_status not in TRACE_STATUSES:
        raise ValueError(f"unsupported current agent job status: {current_status}")


def _validate_turn_reference(*, run_id: str, turn_id: str, job_id: str | None) -> None:
    with closing(connect()) as connection:
        turn = connection.execute(
            "SELECT run_id, job_id FROM agent_turns WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
    if turn is None or str(turn["run_id"] or "") != run_id:
        raise ValueError("agent turn does not belong to run")
    if job_id is not None and str(turn["job_id"] or "") != job_id:
        raise ValueError("agent turn does not belong to job")

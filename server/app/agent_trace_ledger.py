from __future__ import annotations

from contextlib import closing
from typing import Any

from .agent_permissions import permissions_for_agent, validate_agent_role, validate_tool_permission
from .foundation_utils import json_dumps, json_loads, new_id, now_iso, safe_summary
from .storage import connect, get_agent_run


def create_agent_turn(*, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    turn_id = payload.get("turn_id") or new_id("agent_turn")
    now = now_iso()
    agent_name = str(payload.get("agent_name") or "任务编排")
    validate_agent_role(agent_name)
    permissions = permissions_for_agent(agent_name)
    permission_version = str(payload.get("tool_permissions_version") or permissions["version"])
    if permission_version != permissions["version"]:
        raise PermissionError("tool permission version mismatch")
    status = _normalize_trace_status(str(payload.get("status") or "completed"))
    job_id = payload.get("job_id")
    parent_turn_id = payload.get("parent_turn_id")
    retry_count = int(payload.get("retry_count", 0))
    metadata = dict(payload.get("metadata") or {})
    retryable = bool(payload.get("retryable", metadata.get("retryable", False)))
    metadata["retryable"] = retryable
    failure_reason = safe_summary(payload.get("failure_reason", ""), max_chars=500)
    _validate_turn_status_contract(
        status=status,
        failure_reason=failure_reason,
        retryable=retryable,
    )
    handoff_to = str(payload.get("handoff_to") or "")
    if handoff_to:
        validate_agent_role(handoff_to)
        if handoff_to == agent_name:
            raise ValueError("agent handoff must target a different role")
    tool_calls = list(payload.get("tool_calls", []) or [])
    tool_call_ids: set[str] = set()
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            raise ValueError("agent tool call must be an object")
        validate_tool_permission(
            agent_name=agent_name,
            tool_name=str(tool_call.get("tool_name") or ""),
            permission_version=permission_version,
        )
        tool_call_id = str(tool_call.get("tool_call_id") or "")
        if tool_call_id and tool_call_id in tool_call_ids:
            raise ValueError("duplicate agent tool_call_id")
        if tool_call_id:
            tool_call_ids.add(tool_call_id)
        tool_status = _normalize_trace_status(str(tool_call.get("status") or "completed"))
        _validate_tool_call_contract(status=tool_status, payload=tool_call)
        _validate_turn_tool_status(turn_status=status, tool_status=tool_status)
    values = {
        "turn_id": turn_id,
        "run_id": run_id,
        "job_id": job_id,
        "agent_name": agent_name,
        "agent_role": payload.get("agent_role") or _default_role(agent_name),
        "round_index": int(payload["round_index"]) if payload.get("round_index") is not None else 0,
        "parent_turn_id": parent_turn_id,
        "started_at": payload.get("started_at") or now,
        "finished_at": payload.get("finished_at"),
        "status": status,
        "input_summary": safe_summary(payload.get("input_summary") or payload.get("input") or "", max_chars=800),
        "input_payload_ref": payload.get("input_payload_ref", ""),
        "context_pack_id": payload.get("context_pack_id"),
        "prompt_version": payload.get("prompt_version", "agent-turn.unknown"),
        "tool_permissions_version": permission_version,
        "evidence_ids": payload.get("evidence_ids", []),
        "graph_path_ids": payload.get("graph_path_ids", []),
        "memory_item_ids": payload.get("memory_item_ids", []),
        "output_summary": safe_summary(payload.get("output_summary") or payload.get("output") or "", max_chars=1000),
        "output_payload_ref": payload.get("output_payload_ref", ""),
        "output_type": payload.get("output_type", "reasoning_result"),
        "confidence": float(payload.get("confidence", 0.0)),
        "risk_flags": payload.get("risk_flags", []),
        "handoff_to": handoff_to,
        "handoff_reason": payload.get("handoff_reason", ""),
        "retry_count": retry_count,
        "failure_reason": failure_reason,
        "human_review_status": payload.get("human_review_status", "未触发"),
        "human_review_result": safe_summary(payload.get("human_review_result", ""), max_chars=500),
        "metadata": metadata,
    }
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if connection.execute("SELECT 1 FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone() is None:
                raise ValueError("agent run not found")
            _validate_turn_dependencies(
                connection=connection,
                run_id=run_id,
                agent_name=agent_name,
                job_id=job_id,
                parent_turn_id=parent_turn_id,
                retry_count=retry_count,
                status=status,
            )
            if payload.get("round_index") is None:
                values["round_index"] = _next_round_index(connection=connection, run_id=run_id)
            _insert_turn(connection, values)
            _record_default_io(connection=connection, run_id=run_id, turn_id=turn_id, payload=payload)
            if values["handoff_to"]:
                _create_agent_handoff(
                    connection=connection,
                    run_id=run_id,
                    payload={
                        "from_agent": values["agent_name"],
                        "to_agent": values["handoff_to"],
                        "from_turn_id": turn_id,
                        "handoff_summary": values["handoff_reason"] or values["output_summary"],
                        "required_checks": ["证据引用", "质量门禁"],
                        "status": "pending",
                    },
                )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return get_agent_turn(turn_id) or {"turn_id": turn_id, **values}


def _insert_turn(connection: Any, values: dict[str, Any]) -> None:
    connection.execute(
        """
        INSERT INTO agent_turns (
          turn_id, run_id, job_id, agent_name, agent_role, round_index, parent_turn_id, started_at, finished_at,
          status, input_summary, input_payload_ref, context_pack_id, prompt_version, tool_permissions_version,
          evidence_ids, graph_path_ids, memory_item_ids, output_summary, output_payload_ref, output_type,
          confidence, risk_flags, handoff_to, handoff_reason, retry_count, failure_reason,
          human_review_status, human_review_result, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            values["turn_id"],
            values["run_id"],
            values["job_id"],
            values["agent_name"],
            values["agent_role"],
            values["round_index"],
            values["parent_turn_id"],
            values["started_at"],
            values["finished_at"],
            values["status"],
            values["input_summary"],
            values["input_payload_ref"],
            values["context_pack_id"],
            values["prompt_version"],
            values["tool_permissions_version"],
            json_dumps(values["evidence_ids"]),
            json_dumps(values["graph_path_ids"]),
            json_dumps(values["memory_item_ids"]),
            values["output_summary"],
            values["output_payload_ref"],
            values["output_type"],
            values["confidence"],
            json_dumps(values["risk_flags"]),
            values["handoff_to"],
            values["handoff_reason"],
            values["retry_count"],
            values["failure_reason"],
            values["human_review_status"],
            values["human_review_result"],
            json_dumps(values["metadata"]),
        ),
    )


def create_agent_io_record(*, run_id: str, turn_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with closing(connect()) as connection, connection:
        return _create_agent_io_record(connection=connection, run_id=run_id, turn_id=turn_id, payload=payload)


def _create_agent_io_record(*, connection: Any, run_id: str, turn_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    _require_turn_in_run(connection=connection, run_id=run_id, turn_id=turn_id)
    record_id = payload.get("record_id") or new_id("agent_io")
    now = now_iso()
    values = {
        "record_id": record_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "created_at": now,
        "direction": payload.get("direction", "input"),
        "payload_kind": payload.get("payload_kind", "summary"),
        "summary": safe_summary(payload.get("summary", ""), max_chars=800),
        "payload_ref": payload.get("payload_ref", ""),
        "source_kind": payload.get("source_kind", "agent"),
        "source_id": payload.get("source_id", ""),
        "metadata": payload.get("metadata", {}),
    }
    connection.execute(
        """
        INSERT INTO agent_io_records (
          record_id, turn_id, run_id, created_at, direction, payload_kind, summary,
          payload_ref, source_kind, source_id, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            values["record_id"],
            values["turn_id"],
            values["run_id"],
            values["created_at"],
            values["direction"],
            values["payload_kind"],
            values["summary"],
            values["payload_ref"],
            values["source_kind"],
            values["source_id"],
            json_dumps(values["metadata"]),
        ),
    )
    return values


def create_agent_tool_call(*, run_id: str, turn_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    tool_call_id = payload.get("tool_call_id") or new_id("tool_call")
    now = now_iso()
    started = payload.get("started_at") or now
    tool_name = str(payload.get("tool_name") or "")
    with closing(connect()) as connection:
        turn = _require_turn_in_run(connection=connection, run_id=run_id, turn_id=turn_id)
        try:
            validate_tool_permission(
                agent_name=str(turn.get("agent_name") or ""),
                tool_name=tool_name,
                permission_version=str(turn.get("tool_permissions_version") or ""),
            )
        except PermissionError as exc:
            rejected = _tool_call_values(
                tool_call_id=tool_call_id,
                turn_id=turn_id,
                run_id=run_id,
                created_at=now,
                started_at=started,
                payload={
                    **payload,
                    "tool_name": tool_name,
                    "status": "rejected",
                    "output_summary": "",
                    "latency_ms": 0,
                    "error_type": "tool_permission_denied",
                    "error_message_safe": str(exc),
                    "retryable": False,
                },
            )
            _insert_tool_call(connection, rejected)
            connection.commit()
            raise
        values = _tool_call_values(
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            run_id=run_id,
            created_at=now,
            started_at=started,
            payload=payload,
        )
        _validate_tool_call_contract(status=values["status"], payload=values)
        _validate_turn_tool_status(turn_status=str(turn.get("status") or ""), tool_status=values["status"])
        _insert_tool_call(connection, values)
        connection.commit()
    return values


def _tool_call_values(
    *,
    tool_call_id: str,
    turn_id: str,
    run_id: str,
    created_at: str,
    started_at: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    values = {
        "tool_call_id": tool_call_id,
        "turn_id": turn_id,
        "run_id": run_id,
        "created_at": created_at,
        "tool_name": str(payload.get("tool_name") or ""),
        "tool_category": payload.get("tool_category", "internal"),
        "input_summary": safe_summary(payload.get("input_summary", ""), max_chars=500),
        "output_summary": safe_summary(payload.get("output_summary", ""), max_chars=800),
        "status": _normalize_trace_status(str(payload.get("status") or "completed")),
        "latency_ms": int(payload.get("latency_ms", 0)),
        "error_type": payload.get("error_type", ""),
        "error_message_safe": safe_summary(payload.get("error_message_safe", ""), max_chars=500),
        "retryable": bool(payload.get("retryable", False)),
        "started_at": started_at,
        "finished_at": payload.get("finished_at") or created_at,
    }
    return values


def _insert_tool_call(connection: Any, values: dict[str, Any]) -> None:
    connection.execute(
        """
            INSERT INTO agent_tool_calls (
              tool_call_id, turn_id, run_id, created_at, tool_name, tool_category, input_summary, output_summary,
              status, latency_ms, error_type, error_message_safe, retryable, started_at, finished_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
        (
            values["tool_call_id"],
            values["turn_id"],
            values["run_id"],
            values["created_at"],
            values["tool_name"],
            values["tool_category"],
            values["input_summary"],
            values["output_summary"],
            values["status"],
            values["latency_ms"],
            values["error_type"],
            values["error_message_safe"],
            int(values["retryable"]),
            values["started_at"],
            values["finished_at"],
        ),
    )


def create_agent_handoff(*, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    with closing(connect()) as connection, connection:
        return _create_agent_handoff(connection=connection, run_id=run_id, payload=payload)


def _create_agent_handoff(*, connection: Any, run_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if connection.execute("SELECT 1 FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone() is None:
        raise ValueError("agent run not found")
    from_agent = validate_agent_role(str(payload.get("from_agent") or ""))
    to_agent = validate_agent_role(str(payload.get("to_agent") or ""))
    if from_agent == to_agent:
        raise ValueError("agent handoff must target a different role")
    from_turn_id = str(payload.get("from_turn_id") or "")
    if not from_turn_id:
        raise ValueError("agent handoff requires from_turn_id")
    from_turn = _require_turn_in_run(connection=connection, run_id=run_id, turn_id=from_turn_id)
    if str(from_turn.get("agent_name") or "") != from_agent:
        raise ValueError("agent handoff source role does not match turn")
    if payload.get("status", "pending") != "pending":
        raise ValueError("new agent handoff must be pending")
    if payload.get("to_turn_id") is not None or payload.get("accepted_at") is not None:
        raise ValueError("pending agent handoff cannot be pre-accepted")
    required_checks = payload.get("required_checks", [])
    if not isinstance(required_checks, list) or not required_checks:
        raise ValueError("agent handoff requires checks")
    handoff_id = payload.get("handoff_id") or new_id("handoff")
    now = now_iso()
    values = {
        "handoff_id": handoff_id,
        "run_id": run_id,
        "from_agent": from_agent,
        "to_agent": to_agent,
        "from_turn_id": from_turn_id,
        "to_turn_id": None,
        "handoff_payload_ref": payload.get("handoff_payload_ref", ""),
        "handoff_summary": safe_summary(payload.get("handoff_summary", ""), max_chars=800),
        "required_checks": required_checks,
        "blocked_reason": safe_summary(payload.get("blocked_reason", ""), max_chars=500),
        "created_at": now,
        "accepted_at": None,
        "status": "pending",
        "metadata": payload.get("metadata", {}),
    }
    connection.execute(
        """
        INSERT INTO agent_handoffs (
          handoff_id, run_id, from_agent, to_agent, from_turn_id, to_turn_id, handoff_payload_ref,
          handoff_summary, required_checks, blocked_reason, created_at, accepted_at, status, metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            values["handoff_id"],
            values["run_id"],
            values["from_agent"],
            values["to_agent"],
            values["from_turn_id"],
            values["to_turn_id"],
            values["handoff_payload_ref"],
            values["handoff_summary"],
            json_dumps(values["required_checks"]),
            values["blocked_reason"],
            values["created_at"],
            values["accepted_at"],
            values["status"],
            json_dumps(values["metadata"]),
        ),
    )
    return values


def begin_assistant_stage(
    *,
    run_id: str,
    agent_name: str,
    tool_name: str,
    title: str,
    parent_turn_id: str | None = None,
    handoff_id: str | None = None,
    completed_checks: list[str] | None = None,
    context_pack_id: str | None = None,
    prompt_version: str = "assistant-governance.v1",
    priority: int = 50,
) -> dict[str, Any]:
    """Permission-check and commit an attempted stage before execution."""

    validate_agent_role(agent_name)
    permission_version = str(permissions_for_agent(agent_name)["version"])
    validate_tool_permission(
        agent_name=agent_name,
        tool_name=tool_name,
        permission_version=permission_version,
    )
    now = now_iso()
    stage = {
        "run_id": run_id,
        "job_id": new_id("agent_job"),
        "attempt_id": new_id("agent_attempt"),
        "turn_id": new_id("agent_turn"),
        "tool_call_id": new_id("tool_call"),
        "agent_name": agent_name,
        "tool_name": tool_name,
        "started_at": now,
    }
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            run = connection.execute("SELECT status FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None or str(run["status"] or "") != "running":
                raise ValueError("agent run is not running")
            if parent_turn_id is not None:
                parent = connection.execute(
                    "SELECT status FROM agent_turns WHERE turn_id = ? AND run_id = ?",
                    (parent_turn_id, run_id),
                ).fetchone()
                if parent is None or str(parent["status"] or "") not in {"completed", "degraded"}:
                    raise ValueError("agent stage parent is not terminal")
            if handoff_id is not None:
                handoff = connection.execute(
                    "SELECT * FROM agent_handoffs WHERE handoff_id = ? AND run_id = ?",
                    (handoff_id, run_id),
                ).fetchone()
                if handoff is None or str(handoff["status"] or "") != "pending":
                    raise ValueError("agent handoff is not pending")
                if str(handoff["to_agent"] or "") != agent_name:
                    raise ValueError("agent handoff target role mismatch")
                if str(handoff["from_turn_id"] or "") != str(parent_turn_id or ""):
                    raise ValueError("agent handoff target parent mismatch")
                missing = set(json_loads(handoff["required_checks"], [])) - set(completed_checks or [])
                if missing:
                    raise ValueError(f"agent handoff checks incomplete: {','.join(sorted(missing))}")
            elif parent_turn_id is not None:
                raise ValueError("agent stage with parent requires handoff")

            connection.execute(
                """
                INSERT INTO agent_jobs (
                  job_id, run_id, created_at, updated_at, agent_name, title, status,
                  priority, input_ref, output_ref, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, 'attempted', ?, '', '', ?)
                """,
                (
                    stage["job_id"],
                    run_id,
                    now,
                    now,
                    agent_name,
                    safe_summary(title, max_chars=300),
                    priority,
                    json_dumps({"actual_execution": True, "permissions": permissions_for_agent(agent_name)}),
                ),
            )
            connection.execute(
                """
                INSERT INTO agent_job_attempts (
                  attempt_id, job_id, run_id, created_at, started_at, finished_at,
                  status, retryable, failure_reason, metadata
                ) VALUES (?, ?, ?, ?, ?, NULL, 'attempted', 0, '', ?)
                """,
                (
                    stage["attempt_id"],
                    stage["job_id"],
                    run_id,
                    now,
                    now,
                    json_dumps({"actual_execution": True}),
                ),
            )
            turn = {
                "turn_id": stage["turn_id"],
                "run_id": run_id,
                "job_id": stage["job_id"],
                "agent_name": agent_name,
                "agent_role": _default_role(agent_name),
                "round_index": _next_round_index(connection=connection, run_id=run_id),
                "parent_turn_id": parent_turn_id,
                "started_at": now,
                "finished_at": None,
                "status": "attempted",
                "input_summary": "Assistant阶段输入已登记，正文未入账",
                "input_payload_ref": "",
                "context_pack_id": context_pack_id,
                "prompt_version": prompt_version,
                "tool_permissions_version": permission_version,
                "evidence_ids": [],
                "graph_path_ids": [],
                "memory_item_ids": [],
                "output_summary": "",
                "output_payload_ref": "",
                "output_type": "reasoning_result",
                "confidence": 0.0,
                "risk_flags": [],
                "handoff_to": "",
                "handoff_reason": "",
                "retry_count": 0,
                "failure_reason": "",
                "human_review_status": "未触发",
                "human_review_result": "",
                "metadata": {"actual_execution": True},
            }
            _insert_turn(connection, turn)
            _create_agent_io_record(
                connection=connection,
                run_id=run_id,
                turn_id=str(stage["turn_id"]),
                payload={"direction": "input", "summary": turn["input_summary"], "source_kind": "request"},
            )
            _insert_tool_call(
                connection,
                _tool_call_values(
                    tool_call_id=str(stage["tool_call_id"]),
                    turn_id=str(stage["turn_id"]),
                    run_id=run_id,
                    created_at=now,
                    started_at=now,
                    payload={"tool_name": tool_name, "status": "attempted", "finished_at": None},
                ),
            )
            if handoff_id is not None:
                accepted = connection.execute(
                    """
                    UPDATE agent_handoffs SET to_turn_id = ?, accepted_at = ?, status = 'accepted'
                    WHERE handoff_id = ? AND run_id = ? AND status = 'pending' AND to_turn_id IS NULL
                    """,
                    (stage["turn_id"], now, handoff_id, run_id),
                )
                if accepted.rowcount != 1:
                    raise ValueError("agent handoff changed concurrently")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return stage


def finish_assistant_stage(
    *,
    stage: dict[str, Any],
    status: str,
    output_summary: str,
    latency_ms: int,
    context_pack_id: str | None = None,
    prompt_version: str | None = None,
    evidence_ids: list[str] | None = None,
    graph_path_ids: list[str] | None = None,
    memory_item_ids: list[str] | None = None,
    confidence: float = 0.0,
    risk_flags: list[str] | None = None,
    failure_reason: str = "",
    output_payload_ref: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """CAS the attempted job, attempt, turn, and tool call to one terminal state."""

    terminal = _normalize_trace_status(status)
    safe_failure = safe_summary(failure_reason, max_chars=500)
    if terminal not in {"completed", "degraded", "rejected"}:
        raise ValueError("assistant stage requires terminal status")
    if terminal in {"degraded", "rejected"} and not safe_failure:
        raise ValueError(f"{terminal} stage requires failure_reason")
    run_id, now = str(stage["run_id"]), now_iso()
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT metadata FROM agent_turns WHERE turn_id = ? AND run_id = ? AND status = 'attempted'",
                (stage["turn_id"], run_id),
            ).fetchone()
            if row is None:
                raise ValueError("assistant stage bundle is not attempted")
            turn_metadata = json_loads(row["metadata"], {})
            turn_metadata.update(metadata or {})
            cursors = [
                connection.execute(
                    """
                    UPDATE agent_jobs SET status=?, updated_at=?, output_ref=?
                    WHERE job_id=? AND run_id=? AND status='attempted'
                    """,
                    (terminal, now, output_payload_ref, stage["job_id"], run_id),
                ),
                connection.execute(
                    """
                    UPDATE agent_job_attempts SET finished_at=?, status=?, failure_reason=?
                    WHERE attempt_id=? AND job_id=? AND run_id=? AND status='attempted'
                    """,
                    (now, terminal, safe_failure, stage["attempt_id"], stage["job_id"], run_id),
                ),
                connection.execute(
                    """
                    UPDATE agent_turns SET finished_at=?, status=?, context_pack_id=COALESCE(?, context_pack_id),
                      prompt_version=COALESCE(?, prompt_version), evidence_ids=?, graph_path_ids=?, memory_item_ids=?,
                      output_summary=?, output_payload_ref=?, confidence=?, risk_flags=?, failure_reason=?,
                      human_review_status=?, metadata=?
                    WHERE turn_id=? AND run_id=? AND status='attempted'
                    """,
                    (
                        now,
                        terminal,
                        context_pack_id,
                        prompt_version,
                        json_dumps(evidence_ids or []),
                        json_dumps(graph_path_ids or []),
                        json_dumps(memory_item_ids or []),
                        safe_summary(output_summary, max_chars=1000),
                        output_payload_ref,
                        float(confidence),
                        json_dumps(risk_flags or []),
                        safe_failure,
                        "待复核" if terminal == "degraded" else "未触发",
                        json_dumps(turn_metadata),
                        stage["turn_id"],
                        run_id,
                    ),
                ),
                connection.execute(
                    """
                    UPDATE agent_tool_calls SET output_summary=?, status=?, latency_ms=?, error_type=?,
                      error_message_safe=?, retryable=0, finished_at=?
                    WHERE tool_call_id=? AND turn_id=? AND run_id=? AND status='attempted'
                    """,
                    (
                        safe_summary(output_summary, max_chars=800),
                        terminal,
                        max(int(latency_ms), 0),
                        "" if terminal == "completed" else "assistant_stage_degraded",
                        safe_failure,
                        now,
                        stage["tool_call_id"],
                        stage["turn_id"],
                        run_id,
                    ),
                ),
            ]
            if any(cursor.rowcount != 1 for cursor in cursors):
                raise ValueError("assistant stage bundle changed concurrently")
            _create_agent_io_record(
                connection=connection,
                run_id=run_id,
                turn_id=str(stage["turn_id"]),
                payload={
                    "direction": "output",
                    "payload_kind": "reasoning_result",
                    "summary": output_summary,
                    "payload_ref": output_payload_ref,
                    "source_kind": "agent",
                    "source_id": stage["turn_id"],
                },
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return {**stage, "status": terminal}


def finalize_assistant_run(*, run_id: str, status: str, failure_reason: str = "",
                           provider_calls: int | None = None,
                           quality: dict[str, Any] | None = None) -> dict[str, Any]:
    """CAS a governed Assistant run to its published terminal status.

    Vocabulary v2 (2026-09-16): ``completed`` = answer delivered (gate outcomes
    ride along as the structured ``quality`` annotation and never change the
    delivery status), ``failed`` = no answer delivered. The legacy
    ``needs_human_review`` value is retired for new writes; historical rows are
    projected at read time instead of rewritten.
    """

    from .assistant_run_status import ASSISTANT_STATUS_VOCABULARY

    if status not in {"completed", "failed"}:
        raise ValueError("unsupported assistant run terminal status")
    now = now_iso()
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            run = connection.execute(
                "SELECT metadata FROM agent_runs WHERE run_id=? AND status='running'", (run_id,)
            ).fetchone()
            if run is None:
                raise ValueError("agent run status changed concurrently")
            if status != "failed":
                jobs = connection.execute("SELECT status FROM agent_jobs WHERE run_id=?", (run_id,)).fetchall()
                if len(jobs) != 4 or any(str(row["status"] or "") not in {"completed", "degraded"} for row in jobs):
                    raise ValueError("assistant run four-stage contract incomplete")
                pending = connection.execute(
                    "SELECT COUNT(*) FROM agent_handoffs WHERE run_id=? AND status!='accepted'", (run_id,)
                ).fetchone()[0]
                if int(pending):
                    raise ValueError("assistant run has pending handoffs")
            metadata = json_loads(run["metadata"], {})
            metadata.update({"governance_version": "assistant-governance.v1", "actual_stage_count": 4})
            metadata["status_vocabulary"] = ASSISTANT_STATUS_VOCABULARY
            if isinstance(quality, dict) and quality:
                metadata["quality"] = quality
            if provider_calls is not None:
                if not isinstance(provider_calls, int) or provider_calls < 0:
                    raise ValueError("invalid_assistant_provider_count")
                metadata["provider_calls"] = provider_calls
            if failure_reason:
                metadata["failure_reason"] = safe_summary(failure_reason, max_chars=500)
            cursor = connection.execute(
                """
                UPDATE agent_runs SET status=?, updated_at=?, finished_at=?, metadata=?
                WHERE run_id=? AND status='running'
                """,
                (status, now, now, json_dumps(metadata), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("agent run status changed concurrently")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return get_agent_run(run_id) or {"run_id": run_id, "status": status}


def fail_assistant_run(*, run_id: str, failure_reason: str) -> dict[str, Any] | None:
    """Atomically close every non-terminal record for one failed Assistant run."""

    now = now_iso()
    safe_failure = safe_summary(failure_reason, max_chars=500) or "assistant_pipeline_failure"
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            run = connection.execute(
                "SELECT status, metadata FROM agent_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run is None:
                connection.rollback()
                return None
            if str(run["status"] or "") != "running":
                active = connection.execute(
                    """
                    SELECT
                      (SELECT COUNT(*) FROM agent_jobs WHERE run_id=? AND status='attempted') +
                      (SELECT COUNT(*) FROM agent_job_attempts WHERE run_id=? AND status='attempted') +
                      (SELECT COUNT(*) FROM agent_turns WHERE run_id=? AND status='attempted') +
                      (SELECT COUNT(*) FROM agent_tool_calls WHERE run_id=? AND status='attempted') +
                      (SELECT COUNT(*) FROM agent_handoffs WHERE run_id=? AND status='pending') AS count
                    """,
                    (run_id, run_id, run_id, run_id, run_id),
                ).fetchone()["count"]
                if int(active):
                    raise ValueError("terminal assistant run retains active trace records")
                connection.rollback()
                return get_agent_run(run_id)

            connection.execute(
                """
                UPDATE agent_jobs SET status='degraded', updated_at=?
                WHERE run_id=? AND status='attempted'
                """,
                (now, run_id),
            )
            connection.execute(
                """
                UPDATE agent_job_attempts
                SET status='degraded', finished_at=?, retryable=0, failure_reason=?
                WHERE run_id=? AND status='attempted'
                """,
                (now, safe_failure, run_id),
            )
            connection.execute(
                """
                UPDATE agent_turns
                SET status='degraded', finished_at=?, failure_reason=?, human_review_status='待复核'
                WHERE run_id=? AND status='attempted'
                """,
                (now, safe_failure, run_id),
            )
            connection.execute(
                """
                UPDATE agent_tool_calls
                SET status='degraded', finished_at=?, error_type='assistant_pipeline_failure',
                    error_message_safe=?, retryable=0
                WHERE run_id=? AND status='attempted'
                """,
                (now, safe_failure, run_id),
            )
            connection.execute(
                """
                UPDATE agent_handoffs
                SET status='rejected', blocked_reason=?
                WHERE run_id=? AND status='pending'
                """,
                (safe_failure, run_id),
            )
            metadata = json_loads(run["metadata"], {})
            metadata.update(
                {
                    "governance_version": "assistant-governance.v1",
                    "failure_reason": safe_failure,
                    "fail_closed": True,
                }
            )
            cursor = connection.execute(
                """
                UPDATE agent_runs SET status='failed', updated_at=?, finished_at=?, metadata=?
                WHERE run_id=? AND status='running'
                """,
                (now, now, json_dumps(metadata), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("assistant run status changed concurrently")
            remaining = connection.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM agent_jobs WHERE run_id=? AND status='attempted') +
                  (SELECT COUNT(*) FROM agent_job_attempts WHERE run_id=? AND status='attempted') +
                  (SELECT COUNT(*) FROM agent_turns WHERE run_id=? AND status='attempted') +
                  (SELECT COUNT(*) FROM agent_tool_calls WHERE run_id=? AND status='attempted') +
                  (SELECT COUNT(*) FROM agent_handoffs WHERE run_id=? AND status='pending') AS count
                """,
                (run_id, run_id, run_id, run_id, run_id),
            ).fetchone()["count"]
            if int(remaining):
                raise ValueError("assistant fail-closed cleanup incomplete")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return get_agent_run(run_id)


def get_agent_turn(turn_id: str) -> dict[str, Any] | None:
    with closing(connect()) as connection:
        row = connection.execute("SELECT * FROM agent_turns WHERE turn_id = ?", (turn_id,)).fetchone()
    return _turn_row(row) if row else None


def list_agent_turns(*, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        rows = connection.execute(
            """
            SELECT * FROM agent_turns
            WHERE run_id = ?
            ORDER BY round_index ASC, started_at ASC
            LIMIT ?
            """,
            (run_id, min(max(limit, 1), 1000)),
        ).fetchall()
    return [_turn_row(row) for row in rows]


def list_agent_handoffs(*, run_id: str, limit: int = 500) -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        rows = connection.execute(
            """
            SELECT * FROM agent_handoffs
            WHERE run_id = ?
            ORDER BY created_at ASC
            LIMIT ?
            """,
            (run_id, min(max(limit, 1), 1000)),
        ).fetchall()
    return [_handoff_row(row) for row in rows]


def accept_pending_handoff(
    *,
    run_id: str,
    handoff_id: str,
    to_agent: str,
    to_turn_id: str,
    completed_checks: list[str],
) -> dict[str, Any]:
    """Atomically bind one explicit pending handoff after all checks pass."""
    validate_agent_role(to_agent)
    accepted_at = now_iso()
    with closing(connect()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            row = connection.execute(
                "SELECT * FROM agent_handoffs WHERE handoff_id = ? AND run_id = ?",
                (handoff_id, run_id),
            ).fetchone()
            if row is None:
                raise ValueError("agent handoff does not belong to run")
            handoff = _handoff_row(row)
            if handoff["status"] != "pending":
                raise ValueError("agent handoff was already accepted")
            validate_agent_role(str(handoff["from_agent"]))
            validate_agent_role(str(handoff["to_agent"]))
            if str(handoff["to_agent"]) != to_agent:
                raise ValueError("agent handoff target role mismatch")
            source_turn = _require_turn_in_run(
                connection=connection,
                run_id=run_id,
                turn_id=str(handoff["from_turn_id"] or ""),
            )
            if str(source_turn.get("agent_name") or "") != str(handoff["from_agent"]):
                raise ValueError("agent handoff source role does not match turn")
            target_turn = _require_turn_in_run(connection=connection, run_id=run_id, turn_id=to_turn_id)
            if str(target_turn.get("agent_name") or "") != to_agent:
                raise ValueError("agent handoff target role does not match turn")
            if str(target_turn.get("parent_turn_id") or "") != str(handoff["from_turn_id"] or ""):
                raise ValueError("agent handoff target parent does not match source turn")
            required_checks = {str(item) for item in handoff["required_checks"]}
            supplied_checks = {str(item) for item in completed_checks}
            missing_checks = sorted(required_checks - supplied_checks)
            if missing_checks:
                raise ValueError(f"agent handoff checks incomplete: {','.join(missing_checks)}")
            already_accepted = connection.execute(
                """
                SELECT 1 FROM agent_handoffs
                WHERE run_id = ? AND to_turn_id = ? AND status = 'accepted'
                LIMIT 1
                """,
                (run_id, to_turn_id),
            ).fetchone()
            if already_accepted is not None:
                raise ValueError("agent turn already accepted a handoff")
            cursor = connection.execute(
                """
                UPDATE agent_handoffs
                SET to_turn_id = ?, accepted_at = ?, status = 'accepted'
                WHERE handoff_id = ? AND run_id = ? AND status = 'pending' AND to_turn_id IS NULL
                """,
                (to_turn_id, accepted_at, handoff_id, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("agent handoff was already accepted")
            accepted = connection.execute(
                "SELECT * FROM agent_handoffs WHERE handoff_id = ?",
                (handoff_id,),
            ).fetchone()
            if accepted is None:
                raise ValueError("agent handoff acceptance was not persisted")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return _handoff_row(accepted)


def agent_run_trace(run_id: str) -> dict[str, Any]:
    run = get_agent_run(run_id)
    if run is None:
        return {"run": None, "turns": [], "handoffs": [], "timeline": []}
    turns = [enrich_turn(turn) for turn in list_agent_turns(run_id=run_id)]
    handoffs = list_agent_handoffs(run_id=run_id)
    timeline = sorted(
        [
            *[
                {
                    "time": turn["started_at"],
                    "type": "agent_turn",
                    "title": turn["agent_name"],
                    "summary": turn["output_summary"] or turn["input_summary"],
                    "status": turn["status"],
                }
                for turn in turns
            ],
            *[
                {
                    "time": handoff["created_at"],
                    "type": "handoff",
                    "title": f"{handoff['from_agent']} -> {handoff['to_agent']}",
                    "summary": handoff["handoff_summary"],
                    "status": handoff["status"],
                }
                for handoff in handoffs
            ],
        ],
        key=lambda item: item["time"] or "",
    )
    return {"run": run, "turns": turns, "handoffs": handoffs, "timeline": timeline}


def enrich_turn(turn: dict[str, Any]) -> dict[str, Any]:
    turn_id = turn["turn_id"]
    with closing(connect()) as connection:
        io_rows = connection.execute(
            "SELECT * FROM agent_io_records WHERE turn_id = ? ORDER BY created_at ASC", (turn_id,)
        ).fetchall()
        tool_rows = connection.execute(
            "SELECT * FROM agent_tool_calls WHERE turn_id = ? ORDER BY created_at ASC", (turn_id,)
        ).fetchall()
        review_rows = connection.execute(
            "SELECT * FROM agent_review_records WHERE turn_id = ? ORDER BY created_at ASC", (turn_id,)
        ).fetchall()
    return {
        **turn,
        "io_records": [_io_row(row) for row in io_rows],
        "tool_calls": [_tool_row(row) for row in tool_rows],
        "review_records": [_review_row(row) for row in review_rows],
        "tool_permissions": permissions_for_agent(turn["agent_name"]),
    }


def _record_default_io(*, connection: Any, run_id: str, turn_id: str, payload: dict[str, Any]) -> None:
    if payload.get("input_summary") or payload.get("input"):
        _create_agent_io_record(
            connection=connection,
            run_id=run_id,
            turn_id=turn_id,
            payload={
                "direction": "input",
                "payload_kind": "summary",
                "summary": payload.get("input_summary") or payload.get("input"),
                "source_kind": payload.get("input_source_kind", "agent"),
                "source_id": payload.get("input_source_id", ""),
            },
        )
    if payload.get("output_summary") or payload.get("output"):
        _create_agent_io_record(
            connection=connection,
            run_id=run_id,
            turn_id=turn_id,
            payload={
                "direction": "output",
                "payload_kind": payload.get("output_type", "reasoning_result"),
                "summary": payload.get("output_summary") or payload.get("output"),
                "source_kind": "agent",
                "source_id": turn_id,
            },
        )
    for tool_call in payload.get("tool_calls", []) or []:
        values = _tool_call_values(
            tool_call_id=tool_call.get("tool_call_id") or new_id("tool_call"),
            turn_id=turn_id,
            run_id=run_id,
            created_at=now_iso(),
            started_at=tool_call.get("started_at") or now_iso(),
            payload=tool_call,
        )
        _insert_tool_call(connection, values)


def _require_turn_in_run(*, connection: Any, run_id: str, turn_id: str) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM agent_turns WHERE turn_id = ?", (turn_id,)).fetchone()
    if row is None or str(row["run_id"] or "") != run_id:
        raise ValueError("agent turn does not belong to run")
    return _turn_row(row)


def _validate_turn_dependencies(
    *,
    connection: Any,
    run_id: str,
    agent_name: str,
    job_id: str | None,
    parent_turn_id: str | None,
    retry_count: int,
    status: str,
) -> None:
    if job_id is not None:
        job = connection.execute(
            "SELECT run_id, agent_name, status FROM agent_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if job is None or str(job["run_id"] or "") != run_id:
            raise ValueError("agent job does not belong to run")
        if str(job["agent_name"] or "") != agent_name:
            raise ValueError("agent job role does not match turn")
        if str(job["status"] or "") != status:
            raise ValueError("agent job status does not match turn")
    if retry_count == 0:
        if parent_turn_id is not None:
            _require_turn_in_run(connection=connection, run_id=run_id, turn_id=parent_turn_id)
        return
    if job_id is None:
        raise ValueError("agent retry requires job_id")
    if parent_turn_id is None:
        raise ValueError("agent retry requires parent_turn_id")
    parent = _require_turn_in_run(connection=connection, run_id=run_id, turn_id=parent_turn_id)
    if str(parent.get("agent_name") or "") != agent_name:
        raise ValueError("agent retry role does not match parent turn")
    if (parent.get("job_id") or None) != job_id:
        raise ValueError("agent retry job does not match parent turn")
    if str(parent.get("status") or "") != "degraded":
        raise ValueError("agent retry requires degraded parent turn")
    if not bool(parent.get("metadata", {}).get("retryable", False)):
        raise ValueError("agent retry parent is not retryable")
    if retry_count != int(parent.get("retry_count") or 0) + 1:
        raise ValueError("agent retry_count is not consecutive")
    latest = connection.execute(
        """
        SELECT turn_id FROM agent_turns
        WHERE run_id = ? AND job_id = ?
        ORDER BY round_index DESC, started_at DESC, rowid DESC
        LIMIT 1
        """,
        (run_id, job_id),
    ).fetchone()
    if latest is None or str(latest["turn_id"] or "") != parent_turn_id:
        raise ValueError("agent retry parent is not the latest job turn")
    existing_child = connection.execute(
        "SELECT 1 FROM agent_turns WHERE run_id = ? AND parent_turn_id = ? LIMIT 1",
        (run_id, parent_turn_id),
    ).fetchone()
    if existing_child is not None:
        raise ValueError("agent retry parent already has a child turn")


def _next_round_index(*, connection: Any, run_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(round_index), 0) + 1 AS next_round FROM agent_turns WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row["next_round"] if row else 1)


def _validate_turn_status_contract(*, status: str, failure_reason: str, retryable: bool) -> None:
    if status in {"degraded", "rejected"} and not failure_reason:
        raise ValueError(f"{status} turn requires failure_reason")
    if status in {"completed", "rejected"} and retryable:
        raise ValueError(f"{status} turn cannot be retryable")


def _validate_tool_call_contract(*, status: str, payload: dict[str, Any]) -> None:
    retryable = bool(payload.get("retryable", False))
    error_reason = str(payload.get("error_type") or payload.get("error_message_safe") or "").strip()
    output_summary = str(payload.get("output_summary") or "")
    if status == "completed" and retryable:
        raise ValueError("completed tool call cannot be retryable")
    if status in {"degraded", "rejected"} and not error_reason:
        raise ValueError(f"{status} tool call requires error reason")
    if status == "rejected" and output_summary:
        raise ValueError("rejected tool call output must be empty")


def _validate_turn_tool_status(*, turn_status: str, tool_status: str) -> None:
    allowed = {
        "materialized": {"materialized", "rejected"},
        "attempted": {"materialized", "attempted", "completed", "degraded", "rejected"},
        "completed": {"completed", "degraded", "rejected"},
        "degraded": {"completed", "degraded", "rejected"},
        "rejected": {"materialized", "rejected"},
    }
    if tool_status not in allowed.get(turn_status, set()):
        raise ValueError(f"tool status {tool_status} is invalid for {turn_status} turn")


def _default_role(agent_name: str) -> str:
    return {
        "任务编排": "确认目标、拆分任务并分发给后续 Agent",
        "数据接入": "读取可用数据来源并形成输入清单",
        "数据清洗": "去重、标准化、校验单位和口径",
        "行情分析": "分析价格、库存、开工、利润和价差",
        "事件识别": "识别新闻、公告、供需和宏观事件",
        "新闻核验": "核验来源等级和事件可信度",
        "证据检索": "检索 RAG 证据并形成引用集合",
        "图谱构建": "生成证据图谱和推理路径",
        "推理判断": "基于证据形成业务判断",
        "反证检查": "查找抵消因素和冲突证据",
        "质量复核": "执行质量门禁和人工复核",
        "报告生成": "生成客户可读研判报告",
    }.get(agent_name, "执行指定业务步骤")


def _turn_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    for field in ("evidence_ids", "graph_path_ids", "memory_item_ids", "risk_flags"):
        item[field] = json_loads(item.get(field), [])
    item["metadata"] = json_loads(item.get("metadata"), {})
    return item


def _io_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["metadata"] = json_loads(item.get("metadata"), {})
    return item


def _tool_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["retryable"] = bool(item["retryable"])
    return item


def _handoff_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["required_checks"] = json_loads(item.get("required_checks"), [])
    item["metadata"] = json_loads(item.get("metadata"), {})
    return item


def _review_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["metadata"] = json_loads(item.get("metadata"), {})
    return item


def _normalize_trace_status(status: str) -> str:
    legacy = {
        "pending": "materialized",
        "running": "attempted",
        "success": "completed",
        "succeeded": "completed",
        "failed": "degraded",
        "blocked": "degraded",
    }
    normalized = legacy.get(status.strip().lower(), status.strip().lower())
    if normalized not in {"materialized", "attempted", "completed", "degraded", "rejected"}:
        raise ValueError(f"unsupported agent trace status: {status}")
    return normalized

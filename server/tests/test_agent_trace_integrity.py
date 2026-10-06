from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

import pytest

from app import storage
from app.agent_runtime import create_agent_job, record_job_attempt
from app.agent_trace_ledger import (
    accept_pending_handoff,
    create_agent_handoff,
    create_agent_io_record,
    create_agent_tool_call,
    create_agent_turn,
    list_agent_handoffs,
    list_agent_turns,
)
from app.settings import settings


@pytest.fixture
def isolated_runs(tmp_path: Path):
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "agent-trace-integrity.db"))
    run_a = storage.create_agent_run(
        run_id="trace-integrity-a",
        payload={"name": "a", "goal": "a", "status": "running"},
    )
    run_b = storage.create_agent_run(
        run_id="trace-integrity-b",
        payload={"name": "b", "goal": "b", "status": "running"},
    )
    try:
        yield run_a, run_b
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def test_turn_and_io_reject_cross_run_or_role_mismatched_references(isolated_runs) -> None:
    run_a, run_b = isolated_runs
    job = create_agent_job(run_id=run_a["run_id"], agent_name="证据检索", title="retrieve")
    with pytest.raises(ValueError, match="job status does not match turn"):
        create_agent_turn(
            run_id=run_a["run_id"],
            payload={"job_id": job["job_id"], "agent_name": "证据检索", "status": "completed"},
        )
    record_job_attempt(run_id=run_a["run_id"], job_id=job["job_id"], status="completed")
    turn = create_agent_turn(
        run_id=run_a["run_id"],
        payload={"job_id": job["job_id"], "agent_name": "证据检索"},
    )

    with pytest.raises(ValueError, match="job does not belong to run"):
        create_agent_turn(
            run_id=run_b["run_id"],
            payload={"job_id": job["job_id"], "agent_name": "证据检索"},
        )
    with pytest.raises(ValueError, match="job role does not match turn"):
        create_agent_turn(
            run_id=run_a["run_id"],
            payload={"job_id": job["job_id"], "agent_name": "图谱构建"},
        )
    with pytest.raises(ValueError, match="turn does not belong to run"):
        create_agent_io_record(
            run_id=run_b["run_id"],
            turn_id=turn["turn_id"],
            payload={"summary": "must fail"},
        )

    assert len(list_agent_turns(run_id=run_a["run_id"])) == 1
    assert list_agent_turns(run_id=run_b["run_id"]) == []


def test_turn_bundle_rolls_back_when_embedded_tool_insert_fails(isolated_runs) -> None:
    run_a, _ = isolated_runs
    existing_turn = create_agent_turn(run_id=run_a["run_id"], payload={"agent_name": "证据检索"})
    create_agent_tool_call(
        run_id=run_a["run_id"],
        turn_id=existing_turn["turn_id"],
        payload={"tool_call_id": "duplicate-tool", "tool_name": "retrieve_rag", "status": "completed"},
    )
    with closing(storage.connect()) as connection, connection:
        before = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("agent_turns", "agent_io_records", "agent_tool_calls", "agent_handoffs")
        }

    with pytest.raises(sqlite3.IntegrityError):
        create_agent_turn(
            run_id=run_a["run_id"],
            payload={
                "agent_name": "证据检索",
                "input_summary": "input must roll back",
                "output_summary": "output must roll back",
                "handoff_to": "图谱构建",
                "tool_calls": [
                    {
                        "tool_call_id": "duplicate-tool",
                        "tool_name": "retrieve_rag",
                        "status": "completed",
                    }
                ],
            },
        )

    with closing(storage.connect()) as connection, connection:
        after = {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in before}
    assert after == before


def test_turn_and_tool_status_contracts_fail_before_write(isolated_runs) -> None:
    run_a, _ = isolated_runs
    with pytest.raises(ValueError, match="degraded turn requires failure_reason"):
        create_agent_turn(run_id=run_a["run_id"], payload={"agent_name": "推理判断", "status": "degraded"})
    with pytest.raises(ValueError, match="rejected turn cannot be retryable"):
        create_agent_turn(
            run_id=run_a["run_id"],
            payload={
                "agent_name": "推理判断",
                "status": "rejected",
                "failure_reason": "denied",
                "retryable": True,
            },
        )
    with pytest.raises(ValueError, match="tool status completed is invalid for materialized turn"):
        create_agent_turn(
            run_id=run_a["run_id"],
            payload={
                "agent_name": "证据检索",
                "status": "materialized",
                "tool_calls": [{"tool_name": "retrieve_rag", "status": "completed"}],
            },
        )

    turn = create_agent_turn(run_id=run_a["run_id"], payload={"agent_name": "证据检索"})
    with pytest.raises(ValueError, match="completed tool call cannot be retryable"):
        create_agent_tool_call(
            run_id=run_a["run_id"],
            turn_id=turn["turn_id"],
            payload={"tool_name": "retrieve_rag", "status": "completed", "retryable": True},
        )
    with pytest.raises(ValueError, match="degraded tool call requires error reason"):
        create_agent_tool_call(
            run_id=run_a["run_id"],
            turn_id=turn["turn_id"],
            payload={"tool_name": "retrieve_rag", "status": "degraded"},
        )
    with pytest.raises(ValueError, match="rejected tool call output must be empty"):
        create_agent_tool_call(
            run_id=run_a["run_id"],
            turn_id=turn["turn_id"],
            payload={
                "tool_name": "retrieve_rag",
                "status": "rejected",
                "error_type": "policy",
                "output_summary": "must not survive",
            },
        )

    with closing(storage.connect()) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_turns").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM agent_tool_calls").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("turn_status", "tool_status"),
    [
        ("materialized", "attempted"),
        ("materialized", "completed"),
        ("materialized", "degraded"),
        ("completed", "materialized"),
        ("completed", "attempted"),
        ("degraded", "materialized"),
        ("degraded", "attempted"),
        ("rejected", "attempted"),
        ("rejected", "completed"),
        ("rejected", "degraded"),
    ],
)
@pytest.mark.parametrize("entry", ["embedded", "direct"])
def test_turn_tool_status_matrix_rejects_invalid_pairs(
    isolated_runs,
    turn_status: str,
    tool_status: str,
    entry: str,
) -> None:
    run_a, _ = isolated_runs
    turn_payload = {
        "agent_name": "证据检索",
        "status": turn_status,
        "failure_reason": "turn failed" if turn_status in {"degraded", "rejected"} else "",
    }
    tool_payload = {
        "tool_name": "retrieve_rag",
        "status": tool_status,
        "error_type": "tool_failed" if tool_status in {"degraded", "rejected"} else "",
    }
    if entry == "embedded":
        turn_payload["tool_calls"] = [tool_payload]
        with pytest.raises(ValueError, match="tool status .* is invalid"):
            create_agent_turn(run_id=run_a["run_id"], payload=turn_payload)
        assert list_agent_turns(run_id=run_a["run_id"]) == []
        return

    turn = create_agent_turn(run_id=run_a["run_id"], payload=turn_payload)
    with pytest.raises(ValueError, match="tool status .* is invalid"):
        create_agent_tool_call(
            run_id=run_a["run_id"],
            turn_id=turn["turn_id"],
            payload=tool_payload,
        )
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_tool_calls").fetchone()[0] == 0


@pytest.mark.parametrize("status", ["materialized", "attempted", "completed", "degraded", "rejected"])
def test_every_turn_status_allows_no_tool_calls(isolated_runs, status: str) -> None:
    run_a, _ = isolated_runs
    turn = create_agent_turn(
        run_id=run_a["run_id"],
        payload={
            "agent_name": "证据检索",
            "status": status,
            "failure_reason": "turn failed" if status in {"degraded", "rejected"} else "",
        },
    )
    assert turn["status"] == status
    with closing(storage.connect()) as connection, connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_tool_calls WHERE turn_id = ?",
                (turn["turn_id"],),
            ).fetchone()[0]
            == 0
        )


def test_concurrent_attempt_retry_and_handoff_each_have_one_winner(isolated_runs) -> None:
    run_a, _ = isolated_runs

    def compete(operation):
        barrier = Barrier(2)

        def worker():
            barrier.wait()
            try:
                operation()
            except ValueError as exc:
                return f"rejected:{exc}"
            return "accepted"

        with ThreadPoolExecutor(max_workers=2) as executor:
            return list(executor.map(lambda _: worker(), range(2)))

    attempt_job = create_agent_job(run_id=run_a["run_id"], agent_name="任务编排", title="attempt race")
    attempt_results = compete(
        lambda: record_job_attempt(
            run_id=run_a["run_id"],
            job_id=attempt_job["job_id"],
            status="completed",
        )
    )
    assert attempt_results.count("accepted") == 1

    retry_job = create_agent_job(run_id=run_a["run_id"], agent_name="推理判断", title="retry race")
    record_job_attempt(
        run_id=run_a["run_id"],
        job_id=retry_job["job_id"],
        status="degraded",
        failure_reason="temporary",
        retryable=True,
    )
    retry_parent = create_agent_turn(
        run_id=run_a["run_id"],
        payload={
            "job_id": retry_job["job_id"],
            "agent_name": "推理判断",
            "status": "degraded",
            "failure_reason": "temporary",
            "retryable": True,
        },
    )
    retry_results = compete(
        lambda: create_agent_turn(
            run_id=run_a["run_id"],
            payload={
                "job_id": retry_job["job_id"],
                "agent_name": "推理判断",
                "status": "degraded",
                "failure_reason": "retry result",
                "parent_turn_id": retry_parent["turn_id"],
                "retry_count": 1,
            },
        )
    )
    assert retry_results.count("accepted") == 1

    source = create_agent_turn(run_id=run_a["run_id"], payload={"agent_name": "证据检索"})
    target = create_agent_turn(
        run_id=run_a["run_id"],
        payload={"agent_name": "图谱构建", "parent_turn_id": source["turn_id"]},
    )
    handoffs = [
        create_agent_handoff(
            run_id=run_a["run_id"],
            payload={
                "from_agent": "证据检索",
                "to_agent": "图谱构建",
                "from_turn_id": source["turn_id"],
                "required_checks": ["evidence"],
            },
        )
        for _ in range(2)
    ]
    handoff_barrier = Barrier(2)

    def accept_one(handoff):
        handoff_barrier.wait()
        try:
            accept_pending_handoff(
                run_id=run_a["run_id"],
                handoff_id=handoff["handoff_id"],
                to_agent="图谱构建",
                to_turn_id=target["turn_id"],
                completed_checks=["evidence"],
            )
        except ValueError as exc:
            return f"rejected:{exc}"
        return "accepted"

    with ThreadPoolExecutor(max_workers=2) as executor:
        handoff_results = list(executor.map(accept_one, handoffs))
    assert handoff_results.count("accepted") == 1

    with closing(storage.connect()) as connection, connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_job_attempts WHERE job_id = ?",
                (attempt_job["job_id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_turns WHERE parent_turn_id = ?",
                (retry_parent["turn_id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM agent_handoffs WHERE to_turn_id = ? AND status = 'accepted'",
                (target["turn_id"],),
            ).fetchone()[0]
            == 1
        )


def test_turn_retry_requires_consecutive_degraded_parent(isolated_runs) -> None:
    run_a, _ = isolated_runs
    job = create_agent_job(run_id=run_a["run_id"], agent_name="推理判断", title="reason")
    record_job_attempt(
        run_id=run_a["run_id"],
        job_id=job["job_id"],
        status="degraded",
        failure_reason="temporary",
        retryable=True,
    )
    degraded = create_agent_turn(
        run_id=run_a["run_id"],
        payload={
            "job_id": job["job_id"],
            "agent_name": "推理判断",
            "status": "degraded",
            "failure_reason": "temporary",
            "retryable": True,
        },
    )
    record_job_attempt(run_id=run_a["run_id"], job_id=job["job_id"], status="completed")
    retry = create_agent_turn(
        run_id=run_a["run_id"],
        payload={
            "job_id": job["job_id"],
            "agent_name": "推理判断",
            "status": "completed",
            "parent_turn_id": degraded["turn_id"],
            "retry_count": 1,
        },
    )
    assert retry["retry_count"] == 1

    with pytest.raises(ValueError, match="not the latest job turn"):
        create_agent_turn(
            run_id=run_a["run_id"],
            payload={
                "job_id": job["job_id"],
                "agent_name": "推理判断",
                "status": "completed",
                "parent_turn_id": degraded["turn_id"],
                "retry_count": 1,
            },
        )

    with pytest.raises(ValueError, match="requires parent_turn_id"):
        create_agent_turn(
            run_id=run_a["run_id"],
            payload={"job_id": job["job_id"], "agent_name": "推理判断", "retry_count": 1},
        )
    with pytest.raises(ValueError, match="requires degraded parent"):
        create_agent_turn(
            run_id=run_a["run_id"],
            payload={
                "job_id": job["job_id"],
                "agent_name": "推理判断",
                "parent_turn_id": retry["turn_id"],
                "retry_count": 2,
            },
        )

    nonretryable_job = create_agent_job(
        run_id=run_a["run_id"],
        agent_name="反证检查",
        title="counter evidence",
    )
    record_job_attempt(
        run_id=run_a["run_id"],
        job_id=nonretryable_job["job_id"],
        status="degraded",
        failure_reason="permanent",
    )
    nonretryable_parent = create_agent_turn(
        run_id=run_a["run_id"],
        payload={
            "job_id": nonretryable_job["job_id"],
            "agent_name": "反证检查",
            "status": "degraded",
            "failure_reason": "permanent",
        },
    )
    with pytest.raises(ValueError, match="parent is not retryable"):
        create_agent_turn(
            run_id=run_a["run_id"],
            payload={
                "job_id": nonretryable_job["job_id"],
                "agent_name": "反证检查",
                "status": "degraded",
                "failure_reason": "must fail",
                "parent_turn_id": nonretryable_parent["turn_id"],
                "retry_count": 1,
            },
        )


def test_handoff_validates_roles_references_and_accepts_once(isolated_runs) -> None:
    run_a, run_b = isolated_runs
    source = create_agent_turn(
        run_id=run_a["run_id"],
        payload={
            "agent_name": "证据检索",
            "handoff_to": "图谱构建",
            "handoff_reason": "evidence ready",
        },
    )
    first_handoff = list_agent_handoffs(run_id=run_a["run_id"])[0]
    wrong_target = create_agent_turn(
        run_id=run_a["run_id"],
        payload={"agent_name": "报告生成", "parent_turn_id": source["turn_id"]},
    )
    with pytest.raises(ValueError, match="target role does not match turn"):
        accept_pending_handoff(
            run_id=run_a["run_id"],
            handoff_id=first_handoff["handoff_id"],
            to_agent="图谱构建",
            to_turn_id=wrong_target["turn_id"],
            completed_checks=["证据引用", "质量门禁"],
        )

    target = create_agent_turn(
        run_id=run_a["run_id"],
        payload={"agent_name": "图谱构建", "parent_turn_id": source["turn_id"]},
    )
    with pytest.raises(ValueError, match="checks incomplete"):
        accept_pending_handoff(
            run_id=run_a["run_id"],
            handoff_id=first_handoff["handoff_id"],
            to_agent="图谱构建",
            to_turn_id=target["turn_id"],
            completed_checks=["证据引用"],
        )
    accepted = accept_pending_handoff(
        run_id=run_a["run_id"],
        handoff_id=first_handoff["handoff_id"],
        to_agent="图谱构建",
        to_turn_id=target["turn_id"],
        completed_checks=["证据引用", "质量门禁"],
    )
    assert accepted is not None
    assert accepted["from_turn_id"] == source["turn_id"]
    assert accepted["to_turn_id"] == target["turn_id"]
    assert accepted["status"] == "accepted"
    second_handoff = create_agent_handoff(
        run_id=run_a["run_id"],
        payload={
            "from_agent": "证据检索",
            "to_agent": "图谱构建",
            "from_turn_id": source["turn_id"],
            "required_checks": ["evidence"],
        },
    )
    with pytest.raises(ValueError, match="already accepted a handoff"):
        accept_pending_handoff(
            run_id=run_a["run_id"],
            handoff_id=second_handoff["handoff_id"],
            to_agent="图谱构建",
            to_turn_id=target["turn_id"],
            completed_checks=["evidence"],
        )
    second_target = create_agent_turn(
        run_id=run_a["run_id"],
        payload={"agent_name": "图谱构建", "parent_turn_id": source["turn_id"]},
    )
    second_accepted = accept_pending_handoff(
        run_id=run_a["run_id"],
        handoff_id=second_handoff["handoff_id"],
        to_agent="图谱构建",
        to_turn_id=second_target["turn_id"],
        completed_checks=["evidence"],
    )
    assert second_accepted is not None
    with pytest.raises(ValueError, match="already accepted"):
        accept_pending_handoff(
            run_id=run_a["run_id"],
            handoff_id=second_handoff["handoff_id"],
            to_agent="图谱构建",
            to_turn_id=second_target["turn_id"],
            completed_checks=["evidence"],
        )

    with pytest.raises(ValueError, match="source role does not match turn"):
        create_agent_handoff(
            run_id=run_a["run_id"],
            payload={
                "from_agent": "任务编排",
                "to_agent": "图谱构建",
                "from_turn_id": source["turn_id"],
                "required_checks": ["evidence"],
            },
        )
    with pytest.raises(ValueError, match="turn does not belong to run"):
        create_agent_handoff(
            run_id=run_b["run_id"],
            payload={
                "from_agent": "证据检索",
                "to_agent": "图谱构建",
                "from_turn_id": source["turn_id"],
                "required_checks": ["evidence"],
            },
        )

    assert len(list_agent_handoffs(run_id=run_a["run_id"])) == 2
    assert list_agent_handoffs(run_id=run_b["run_id"]) == []

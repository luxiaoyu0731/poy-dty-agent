from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app import storage
from app.agent_permissions import AGENT_TOOL_SCOPES, TOOL_PERMISSION_VERSION, permissions_for_agent
from app.agent_trace_ledger import (
    create_agent_tool_call,
    create_agent_turn,
    enrich_turn,
    list_agent_turns,
)
from app.settings import settings


@pytest.fixture
def isolated_agent_run(tmp_path: Path):
    original_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "agent-tool-permissions.db"))
    run = storage.create_agent_run(
        run_id="permission-run",
        payload={"name": "permission test", "goal": "verify tool boundary", "status": "running"},
    )
    try:
        yield run
    finally:
        object.__setattr__(settings, "sqlite_path", original_path)


def test_permission_table_is_exact_and_unknown_roles_have_no_default_tool() -> None:
    assert AGENT_TOOL_SCOPES
    assert all(tools and "*" not in tools for tools in AGENT_TOOL_SCOPES.values())
    assert permissions_for_agent("unknown-agent")["allowed_tools"] == []


def test_allowed_embedded_tool_call_uses_current_policy_and_completed_status(isolated_agent_run) -> None:
    turn = create_agent_turn(
        run_id=isolated_agent_run["run_id"],
        payload={
            "agent_name": "证据检索",
            "status": "success",
            "tool_calls": [{"tool_name": "retrieve_rag", "status": "success"}],
        },
    )

    detail = enrich_turn(turn)
    assert detail["status"] == "completed"
    assert detail["tool_permissions_version"] == TOOL_PERMISSION_VERSION
    assert [(item["tool_name"], item["status"]) for item in detail["tool_calls"]] == [("retrieve_rag", "completed")]


@pytest.mark.parametrize("tool_name", ["compute_spread", "", "*"])
def test_disallowed_empty_or_wildcard_embedded_tool_is_preflight_zero_write(isolated_agent_run, tool_name: str) -> None:
    with pytest.raises(PermissionError, match="tool not allowed"):
        create_agent_turn(
            run_id=isolated_agent_run["run_id"],
            payload={
                "agent_name": "证据检索",
                "tool_calls": [{"tool_name": tool_name, "status": "success"}],
            },
        )

    assert list_agent_turns(run_id=isolated_agent_run["run_id"]) == []
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_tool_calls").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("agent_name", "permission_version", "message"),
    [
        ("证据检索", "forged-policy-version", "permission version mismatch"),
        ("unknown-agent", TOOL_PERMISSION_VERSION, "unknown agent role"),
    ],
)
def test_forged_version_or_unknown_role_embedded_call_is_preflight_zero_write(
    isolated_agent_run,
    agent_name: str,
    permission_version: str,
    message: str,
) -> None:
    with pytest.raises(PermissionError, match=message):
        create_agent_turn(
            run_id=isolated_agent_run["run_id"],
            payload={
                "agent_name": agent_name,
                "tool_permissions_version": permission_version,
                "tool_calls": [{"tool_name": "retrieve_rag", "status": "success"}],
            },
        )

    assert list_agent_turns(run_id=isolated_agent_run["run_id"]) == []
    with closing(storage.connect()) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM agent_tool_calls").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("permission_version", "tool_name", "message"),
    [
        (TOOL_PERMISSION_VERSION, "compute_spread", "tool not allowed"),
        (TOOL_PERMISSION_VERSION, "", "tool not allowed"),
        (TOOL_PERMISSION_VERSION, "*", "tool not allowed"),
    ],
)
def test_denied_direct_tool_call_is_a_rejected_audit_not_success(
    isolated_agent_run,
    permission_version: str,
    tool_name: str,
    message: str,
) -> None:
    turn = create_agent_turn(
        run_id=isolated_agent_run["run_id"],
        payload={
            "agent_name": "证据检索",
            "status": "materialized",
            "tool_permissions_version": permission_version,
        },
    )

    with pytest.raises(PermissionError, match=message):
        create_agent_tool_call(
            run_id=isolated_agent_run["run_id"],
            turn_id=turn["turn_id"],
            payload={"tool_name": tool_name, "status": "success", "output_summary": "must not persist"},
        )

    tool_calls = enrich_turn(turn)["tool_calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_name"] == tool_name
    assert tool_calls[0]["status"] == "rejected"
    assert tool_calls[0]["error_type"] == "tool_permission_denied"
    assert tool_calls[0]["output_summary"] == ""


def test_unknown_role_without_tool_call_is_preflight_zero_write(isolated_agent_run) -> None:
    with pytest.raises(PermissionError, match="unknown agent role"):
        create_agent_turn(
            run_id=isolated_agent_run["run_id"],
            payload={"agent_name": "unknown-agent", "status": "materialized"},
        )

    assert list_agent_turns(run_id=isolated_agent_run["run_id"]) == []

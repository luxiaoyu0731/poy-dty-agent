from __future__ import annotations

from typing import Any

TOOL_PERMISSION_VERSION = "agent-tools-2026-06-27.v1"

AGENT_TOOL_SCOPES: dict[str, list[str]] = {
    "任务编排": ["create_job", "assign_agent", "read_status"],
    "数据接入": ["read_source_registry", "import_authorized_file", "read_market_data"],
    "数据清洗": ["validate_schema", "dedupe_records", "normalize_units"],
    "行情分析": ["read_price_points", "compute_spread", "summarize_market"],
    "事件识别": ["read_news_events", "cluster_events", "classify_event"],
    "新闻核验": ["check_source_tier", "compare_articles", "flag_low_evidence"],
    "证据检索": ["retrieve_rag", "read_context_pack", "read_memory"],
    "图谱构建": ["read_graph", "write_graph_snapshot", "build_reasoning_path"],
    "推理判断": ["read_context_pack", "read_graph_path", "draft_judgement"],
    "反证检查": ["search_counter_evidence", "check_quality_gates", "downgrade_if_needed"],
    "质量复核": ["run_guardrails", "request_human_review", "approve_handoff"],
    "报告生成": ["read_approved_evidence", "draft_report", "export_summary"],
}


def validate_agent_role(agent_name: str) -> str:
    """Return a known Agent role or fail before any ledger write."""
    if agent_name not in AGENT_TOOL_SCOPES:
        raise PermissionError(f"unknown agent role: {agent_name}")
    return agent_name


def permissions_for_agent(agent_name: str) -> dict[str, Any]:
    tools = AGENT_TOOL_SCOPES.get(agent_name, [])
    return {
        "version": TOOL_PERMISSION_VERSION,
        "agent_name": agent_name,
        "allowed_tools": list(tools),
        "restricted": [
            "不能绕过授权、登录、验证码、付费墙或许可证限制",
            "不能输出或保存账号、密码、API key",
            "不能把未通过质量复核的结论交给报告生成",
        ],
    }


def can_use_tool(agent_name: str, tool_name: str) -> bool:
    return tool_name in AGENT_TOOL_SCOPES.get(agent_name, [])


def validate_tool_permission(*, agent_name: str, tool_name: str, permission_version: str) -> None:
    """Fail closed unless a known role uses its exact versioned tool allowlist."""
    validate_agent_role(agent_name)
    if permission_version != TOOL_PERMISSION_VERSION:
        raise PermissionError("tool permission version mismatch")
    if not tool_name or tool_name == "*" or not can_use_tool(agent_name, tool_name):
        raise PermissionError(f"tool not allowed for agent role: {agent_name}/{tool_name or '<empty>'}")

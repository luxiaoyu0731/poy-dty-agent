"""Read-only ADR-10 resources, separate from the frozen eighteen node IDs."""

from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from .agent_chain_limits import DEFAULT_HTTP_ATTEMPT_CAP

BASIS = "HTTP 尝试级，含 schema 重试"
SHANGHAI = ZoneInfo("Asia/Shanghai")


class PipelineMemoryBlock(BaseModel):
    schema_version: Literal["pipeline-memory.v1"] = "pipeline-memory.v1"
    recall_enabled: bool
    capability_source: str = "AGENT_MEMORY_RECALL_ENABLED"
    voting_enabled: bool = False
    voting_source: str = "AGENT_MEMORY_VOTING_ENABLED"
    reflection_background_enabled: bool = False
    effect_acceptance: Literal["not_validated"] = "not_validated"
    index_docs: int | None
    index_docs_source: str = "semantic_index.active_index.document_count"
    lessons_active: int | None
    lessons_active_source: str = "agent_lessons:active.validity_window"
    data_as_of: str
    status: Literal["ok", "degraded", "unknown"]
    status_evidence: dict[str, Any]


class PipelineChainBudgetBlock(BaseModel):
    attempts_used: int | None
    cap: int | None
    business_date: str
    basis: Literal["HTTP 尝试级，含 schema 重试"] = BASIS
    source: str = "event-agent-chain-latest.json:budget"
    status: Literal["ok", "unknown"]
    status_detail: str


class PipelineGraphEnvelope(BaseModel):
    schema_version: str
    business_date: str
    generated_at: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    memory: PipelineMemoryBlock
    chain_budget: PipelineChainBudgetBlock


def _clock(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def resource_blocks(
    *, business_date: str, now: datetime, report: dict, report_state: str, index_documents: int | None
) -> dict:
    from .storage import connect_readonly

    configured = os.getenv("AGENT_CHAIN_DAILY_CAP", str(DEFAULT_HTTP_ATTEMPT_CAP))
    try:
        configured_cap = int(configured)
        if configured_cap <= 0:
            configured_cap = None
    except ValueError:
        configured_cap = None
    observed = _clock(report.get("as_of_time"))
    dated = (
        not report_state
        and report.get("business_date") == business_date
        and observed is not None
        and observed <= now
        and observed.astimezone(SHANGHAI).date().isoformat() == business_date
    )
    budget = report.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    used, cap = budget.get("attempts_used"), budget.get("cap")
    valid = (
        dated
        and budget.get("status", "ok") == "ok"
        and type(used) is int
        and type(cap) is int
        and cap > 0
        and 0 <= used <= cap
        and budget.get("basis") == BASIS
    )
    chain = PipelineChainBudgetBlock(
        attempts_used=used if valid else None,
        cap=cap if valid else configured_cap,
        business_date=business_date,
        status="ok" if valid else "unknown",
        status_detail="链报告硬顶快照" if valid else "当日 HTTP 尝试账本未知",
    )
    data_as_of = now.isoformat()
    current = now.astimezone(SHANGHAI).date().isoformat() == business_date
    lessons = None
    if current:
        try:
            with closing(connect_readonly()) as connection:
                lessons = connection.execute(
                    "SELECT count(*) FROM agent_lessons WHERE status='active' "
                    "AND julianday(valid_from)<=julianday(?) "
                    "AND (valid_until IS NULL OR julianday(valid_until)>julianday(?))",
                    (data_as_of, data_as_of),
                ).fetchone()[0]
        except Exception:
            # Resource read failure is unknown, never zero or a successful recall.
            lessons = None
    memory_report = report.get("memory")
    recalls = memory_report.get("recalls") if isinstance(memory_report, dict) else None
    shape_ok = (
        isinstance(recalls, list)
        and bool(recalls)
        and all(
            isinstance(item, dict)
            and item.get("status") in {"ok", "degraded", "unknown"}
            and isinstance(item.get("fragments"), list)
            for item in recalls
        )
    )
    enabled = os.getenv("AGENT_MEMORY_RECALL_ENABLED", "0") == "1"
    status = "unknown"
    if enabled and dated and report.get("run_id") and shape_ok:
        status = "ok" if all(item["status"] == "ok" for item in recalls) else "degraded"
    evidence = {
        "source": "event-agent-chain-latest.json:memory.recalls",
        "business_date": business_date,
        "run_id": report.get("run_id") if dated else None,
        "observed_at": observed.isoformat() if dated else None,
        "recall_count": len(recalls) if dated and shape_ok else None,
        "fragment_count": sum(len(item["fragments"]) for item in recalls) if dated and shape_ok else None,
    }
    memory = PipelineMemoryBlock(
        recall_enabled=enabled,
        voting_enabled=enabled and os.getenv("AGENT_MEMORY_VOTING_ENABLED", "0") == "1",
        reflection_background_enabled=os.getenv("AGENT_REFLECTION_BACKGROUND_ENABLED", "0") == "1",
        index_docs=index_documents if current else None,
        lessons_active=lessons,
        data_as_of=data_as_of,
        status=status,
        status_evidence=evidence,
    )
    return {"memory": memory.model_dump(), "chain_budget": chain.model_dump()}

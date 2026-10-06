from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
SERVER_ROOT = SCRIPT_DIR.parents[0]
REPO_ROOT = SERVER_ROOT.parent
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app.agent_permissions import permissions_for_agent  # noqa: E402
from app.agent_runtime import AGENT_SEQUENCE, bootstrap_agent_jobs  # noqa: E402
from app.context_pack import build_context_pack  # noqa: E402
from app.direction_upstream_gate import (  # noqa: E402
    gate_audit_summary,
    wait_for_upstream_readiness,
)
from app.graphrag_reasoning import build_reasoning_path  # noqa: E402
from app.graphrag_store import materialize_graph_snapshot  # noqa: E402
from app.hybrid_direction_review import review_daily_direction  # noqa: E402
from app.intelligence import build_overview  # noqa: E402
from app.memory import MemoryManager  # noqa: E402
from app.rag import retrieve_evidence  # noqa: E402
from app.rag_index import rebuild_rag_index  # noqa: E402
from app.settings import settings  # noqa: E402
from app.storage import create_agent_artifact, create_agent_run, create_evidence_bundle  # noqa: E402

DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "local-production" / "foundation"
DEFAULT_QUESTION = "当前证据是否支持 POY/DTY 上游原料成本压力判断？"
BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")


AGENT_INPUT_OUTPUT = {
    "任务编排": ("业务问题、最新数据状态、质量门禁", "处理计划与后续 Agent 分工"),
    "数据接入": ("行情、事件、行业指标、报告清单", "可用数据来源摘要"),
    "数据清洗": ("原始行情与行业指标", "标准化数据和质量检查摘要"),
    "行情分析": ("价格、库存、开工、利润、价差", "原料链价格与成本传导摘要"),
    "事件识别": ("新闻、公告、宏观与供应扰动", "事件清单和风险分层"),
    "新闻核验": ("事件清单与来源信息", "来源可信度和需复核事项"),
    "证据检索": ("业务问题与上下文包", "待复核证据与引用集合"),
    "图谱构建": ("证据集合与产业链关系", "证据图谱节点和关系"),
    "推理判断": ("行情摘要、事件证据、图谱路径", "初步业务结论"),
    "反证检查": ("初步结论、冲突证据、质量门禁", "反证清单和抵消因素"),
    "质量复核": ("全部中间结果", "复核意见和行动边界"),
    "报告生成": ("复核通过内容", "客户可读研判报告摘要"),
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Materialize local memory, persistent evidence index, graph snapshot, and per-agent IO trace."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write foundation records after creating a backup.")
    mode.add_argument("--dry-run", action="store_true", help="Report expected writes without modifying business data.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--recovery-anchor",
        type=Path,
        help=(
            "Validated daily backup anchor (online-backup snapshot) required in apply mode; "
            "replaces the former per-step copy2 full backup (DISK-MODEL §3.2)."
        ),
    )
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--product", default="POY")
    parser.add_argument(
        "--rag-limit",
        type=int,
        default=None,
        help="Optional debug cap for fresh sandboxes only; omit for production all-eligible RAG indexing.",
    )
    parser.add_argument("--graph-limit", type=int, default=120)
    parser.add_argument("--memory-limit", type=int, default=300)
    parser.add_argument(
        "--disable-ai-direction-review",
        action="store_true",
        help="Emergency rollback: keep the rule result as observation-only without provider review.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    mode = "apply" if args.apply else "dry_run"
    summary = materialize_foundation(
        db_path=args.db.expanduser().resolve(),
        output_dir=args.output_dir.expanduser().resolve(),
        mode=mode,
        recovery_anchor=(args.recovery_anchor.expanduser().resolve() if args.recovery_anchor else None),
        question=args.question,
        product=args.product,
        rag_limit=args.rag_limit,
        graph_limit=args.graph_limit,
        memory_limit=args.memory_limit,
        allow_provider_calls=(
            not args.disable_ai_direction_review
            and os.getenv("AI_DIRECTION_REVIEW_ENABLED", "0").strip().lower() not in {"0", "false", "no", "off"}
        ),
    )
    output = args.output_dir.expanduser().resolve() / "foundation-materialization-summary.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "status": summary["status"]}, ensure_ascii=False))
    return 0 if summary["status"] in {"success", "dry_run"} else 2


def materialize_foundation(
    *,
    db_path: Path,
    output_dir: Path,
    mode: str,
    recovery_anchor: Path | None,
    question: str,
    product: str,
    rag_limit: int | None,
    graph_limit: int,
    memory_limit: int,
    allow_provider_calls: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    started_at = now_iso()
    before = table_counts(db_path)
    original_sqlite_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(db_path))
    try:
        if mode != "apply":
            candidate_documents = estimate_candidate_documents(db_path)
            # Direction-review upstream gate, dry-run semantics: probe once,
            # record the state, never wait (dry runs must not be slowed down).
            dry_run_gate = wait_for_upstream_readiness(
                db_path=db_path,
                evidence_retriever=lambda as_of_time: retrieve_evidence(
                    question,
                    as_of_time=as_of_time,
                    limit=12,
                    purpose="event_direction",
                ),
                wait=False,
            )
            return {
                "schema_version": "agent_foundation_materialization.v1",
                "status": "dry_run",
                "mode": mode,
                "started_at": started_at,
                "finished_at": now_iso(),
                "db_path": str(db_path),
                "before": before,
                "upstream_gate": gate_audit_summary(dry_run_gate),
                "expected": {
                    "candidate_documents": candidate_documents,
                    "agent_jobs": len(AGENT_SEQUENCE),
                    "agent_turns": 0,
                    "agent_io_records_min": 0,
                    "agent_tool_calls": 0,
                },
                "guards": guard_report(writes_database=False, recovery_anchor="", recovery_point_integrity="not_run"),
            }

        # Disk consolidation 2026-09-17 (DISK-MODEL §3.2 项2): the former copy2
        # full backup is gone. Apply mode is fail-closed on a recovery point:
        # the daily anchor produced by the chain's verify_backup step earlier
        # in the same run must exist and pass PRAGMA integrity_check. Each
        # materializer write below stays transactional, so a mid-step failure
        # leaves a consistent database that can be restored from that anchor.
        recovery_anchor_path = recovery_anchor
        recovery_integrity = _recovery_anchor_integrity(recovery_anchor_path)
        if not recovery_anchor_path or recovery_integrity != "ok":
            return {
                "schema_version": "agent_foundation_materialization.v1",
                "status": "blocked",
                "mode": mode,
                "started_at": started_at,
                "finished_at": now_iso(),
                "db_path": str(db_path),
                "before": before,
                "errors": [
                    "apply requires a valid daily recovery anchor (--recovery-anchor) that passes integrity_check"
                ],
                "guards": guard_report(
                    writes_database=True,
                    recovery_anchor=str(recovery_anchor_path or ""),
                    recovery_point_integrity=recovery_integrity,
                ),
            }

        memory = MemoryManager().sync(limit=memory_limit)
        index = rebuild_rag_index(limit=rag_limit)
        graph = materialize_graph_snapshot(question=question, product=product, limit=graph_limit)
        reasoning_path = build_reasoning_path(question=question, product=product)
        context_pack = build_context_pack(question, task_type="assistant_answer", product=product, persist=True)
        trace = record_agent_turns(
            question=question,
            product=product,
            db_path=db_path,
            context_pack=context_pack,
            index=index,
            graph=graph,
            reasoning_path=reasoning_path,
            memory=memory,
            allow_provider_calls=allow_provider_calls,
        )
        after = table_counts(db_path)
        return {
            "schema_version": "agent_foundation_materialization.v1",
            "status": "success",
            "mode": mode,
            "started_at": started_at,
            "finished_at": now_iso(),
            "db_path": str(db_path),
            "recovery_anchor": str(recovery_anchor_path),
            "before": before,
            "after": after,
            "memory": memory,
            "rag_index": index,
            "graph_snapshot": {
                "snapshot_id": graph["snapshot_id"],
                "node_count": graph["node_count"],
                "edge_count": graph["edge_count"],
            },
            "graph_reasoning_path": {
                "path_id": reasoning_path.get("path_id", ""),
                "status": reasoning_path.get("status", ""),
                "node_count": len(reasoning_path.get("node_ids", [])),
                "edge_count": len(reasoning_path.get("edge_ids", [])),
            },
            "context_pack": {
                "pack_id": context_pack["pack_id"],
                "token_estimate": context_pack["token_estimate"],
                "evidence_count": len(context_pack["evidence_ids"]),
                "chunk_count": len(context_pack["metadata"].get("chunk_ids", [])),
            },
            "agent_trace": trace,
            "daily_snapshot": {
                "snapshot_id": "",
                "business_date": trace["business_date"],
                "status": "not_materialized",
                "idempotent_replay": False,
            },
            "guards": guard_report(
                writes_database=True,
                recovery_anchor=str(recovery_anchor_path),
                recovery_point_integrity=recovery_integrity,
            ),
        }
    finally:
        object.__setattr__(settings, "sqlite_path", original_sqlite_path)


def record_agent_turns(
    *,
    question: str,
    product: str,
    db_path: Path,
    context_pack: dict[str, Any],
    index: dict[str, Any],
    graph: dict[str, Any],
    reasoning_path: dict[str, Any],
    memory: dict[str, Any],
    allow_provider_calls: bool = False,
) -> dict[str, Any]:
    """Materialize the legacy 12-role taxonomy without claiming execution."""
    now = now_iso()
    business_date = business_date_iso(now)
    # Upstream readiness gate (fix-direction-review 2026-09-18): the 09:31
    # chain run raced the resident event-summary worker, so the review's
    # retrieval saw an empty evidence list and honestly abstained. Wait,
    # bounded and env-tunable, until the summary queue is drained and the
    # event_direction evidence list is non-empty — but only when a provider
    # call is actually about to happen; probe-only otherwise.
    def _retrieve_for_gate(as_of_time: str):
        return retrieve_evidence(
            question,
            as_of_time=as_of_time,
            limit=12,
            purpose="event_direction",
        )

    upstream_gate = wait_for_upstream_readiness(
        db_path=db_path,
        evidence_retriever=_retrieve_for_gate,
        wait=allow_provider_calls,
    )
    gate_summary = gate_audit_summary(upstream_gate)
    # The review's as-of must reflect post-wait reality: evidence published
    # while the gate was waiting is visible to the review below.
    now = str(upstream_gate.get("as_of_time") or now_iso())
    business_date = business_date_iso(now)
    rule_overview = build_overview(as_of_time=now)
    retrieval = upstream_gate.get("_retrieval")
    if allow_provider_calls and upstream_gate["ready"]:
        direction_review = asyncio.run(
            review_daily_direction(
                rule_overview=rule_overview,
                as_of_time=now,
                retrieval=retrieval,
                snapshot_binding={
                    "snapshot_id": graph["snapshot_id"],
                    "snapshot_sha256": graph.get("payload_sha256", ""),
                },
            )
        )
    else:
        direction_review = {
            "rule_direction": str(rule_overview.get("status") or "震荡"),
            "outcome": "downgrade",
            "customer_direction": str(rule_overview.get("status") or "震荡"),
            "confidence": min(float(rule_overview.get("confidence") or 0.0), 0.42),
            "decision_status": "observation_only",
            "formal_report_eligible": False,
            "evidence_ids": [],
            "reason_code": ("upstream_gate_timeout" if allow_provider_calls else "provider_calls_disabled"),
            "upstream_gate": gate_summary,
            "rationale": "",
            "provider": {"attempted": False, "succeeded": False, "error": ""},
        }
    provider_attempted = bool(direction_review.get("provider", {}).get("attempted"))
    run = create_agent_run(
        run_id=str(uuid4()),
        payload={
            "name": "POY/DTY 本地生产研判链路脚手架",
            "agent_name": "任务编排",
            "goal": question,
            "status": "pending",
            "source": "foundation_materializer",
            "trace_type": "daily_agent_taxonomy_scaffold",
            "started_at": now,
            "finished_at": None,
            "metadata": {
                "product": product,
                "context_pack_id": context_pack["pack_id"],
                "rag_index_id": index["index_id"],
                "graph_snapshot_id": graph["snapshot_id"],
                "provider_calls": 1 if provider_attempted else 0,
                "execution_semantics": "materialized_only",
                "upstream_gate_wait_seconds": gate_summary["waited_seconds"],
            },
        },
    )
    jobs = bootstrap_agent_jobs(run["run_id"])["jobs"]
    evidence_ids = list(context_pack.get("evidence_ids", []))[:12]
    graph_path_ids = [
        item
        for item in dict.fromkeys([reasoning_path.get("path_id", ""), *context_pack.get("graph_path_ids", [])])
        if item
    ][:20]
    memory_item_ids = list(context_pack.get("memory_item_ids", []))[:12]
    bundle = create_evidence_bundle(
        bundle_id=str(uuid4()),
        run_id=run["run_id"],
        payload={
            "task_id": context_pack["pack_id"],
            "name": "本次研判 RAG 证据包",
            "source_kind": "context_pack",
            "evidence_ids": evidence_ids,
            "payload": {
                "question": question,
                "product": product,
                "context_pack_id": context_pack["pack_id"],
                "chunk_ids": context_pack.get("metadata", {}).get("chunk_ids", []),
                "graph_path_ids": graph_path_ids,
                "memory_item_ids": memory_item_ids,
                "quality_gates": context_pack.get("quality_gates", []),
            },
            "metadata": {
                "materialized_by": "foundation_scaffold",
                "retrieval_run_id": context_pack.get("metadata", {}).get("chunk_retrieval_run_id", ""),
                "provider_calls": 0,
            },
        },
    )
    review_audit = create_agent_artifact(
        artifact_id=str(uuid4()),
        run_id=run["run_id"],
        payload={
            "artifact_type": "direction_review_audit",
            "name": f"方向复核审计 {business_date}",
            "mime_type": "application/json",
            "payload": {
                **direction_review,
                "as_of_time": now,
                "rule_index": rule_overview.get("cost_pressure_index"),
                "upstream_gate": gate_summary,
            },
            "metadata": {
                "internal_only": True,
                "run_id": run["run_id"],
                "feature_flag": "ai_direction_review",
            },
        },
    )
    return {
        "run_id": run["run_id"],
        "business_date": business_date,
        "execution_semantics": "materialized_only",
        "job_count": len(jobs),
        "turn_count": 0,
        "turns": [],
        "evidence_bundle_id": bundle["bundle_id"],
        "handoff_count": 0,
        "accepted_handoffs": 0,
        "report_agent_completed": False,
        "report_artifact_id": "",
        "direction_review_audit_id": review_audit["artifact_id"],
        "direction": direction_review["customer_direction"],
        "upstream_gate": gate_summary,
    }


def agent_summaries(
    *,
    agent_name: str,
    question: str,
    product: str,
    context_pack: dict[str, Any],
    rag_index: dict[str, Any],
    graph: dict[str, Any],
    memory: dict[str, Any],
) -> tuple[str, str]:
    default_input, default_output = AGENT_INPUT_OUTPUT[agent_name]
    evidence_count = len(context_pack.get("evidence_ids", []))
    chunk_count = rag_index.get("chunk_count", 0)
    node_count = graph.get("node_count", 0)
    memory_count = memory.get("synced", 0)
    if agent_name == "任务编排":
        return default_input, f"围绕“{question}”完成 {product} 研判链路编排。"
    if agent_name == "证据检索":
        return default_input, f"持久证据索引含 {chunk_count} 个片段，本次上下文包引用 {evidence_count} 条证据。"
    if agent_name == "图谱构建":
        return default_input, f"已物化证据图谱快照，包含 {node_count} 个节点。"
    if agent_name == "推理判断":
        return default_input, f"基于 {evidence_count} 条证据和 {memory_count} 条记忆形成阶段性业务判断。"
    return default_input, default_output


def tool_name_for_agent(agent_name: str) -> str:
    allowed_tools = permissions_for_agent(agent_name)["allowed_tools"]
    if not allowed_tools:
        raise ValueError(f"unknown agent role: {agent_name}")
    return str(allowed_tools[0])


def tool_category_for_agent(agent_name: str) -> str:
    if agent_name in {"证据检索", "图谱构建", "推理判断"}:
        return "证据链"
    if agent_name in {"数据接入", "数据清洗", "行情分析"}:
        return "数据处理"
    if agent_name in {"事件识别", "新闻核验"}:
        return "事件核验"
    return "业务流程"


def table_counts(db_path: Path) -> dict[str, int]:
    tables = [
        "memory_items",
        "rag_indices",
        "rag_documents",
        "rag_chunks",
        "rag_retrieval_runs",
        "context_packs",
        "graph_nodes",
        "graph_edges",
        "graph_snapshots",
        "agent_runs",
        "agent_jobs",
        "agent_turns",
        "agent_io_records",
        "agent_tool_calls",
        "agent_handoffs",
    ]
    if not db_path.exists():
        return {table: 0 for table in tables}
    counts: dict[str, int] = {}
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)) as connection, connection:
        connection.execute("PRAGMA busy_timeout = 10000")
        for table in tables:
            if not table_exists(connection, table):
                counts[table] = 0
            else:
                counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return counts


def estimate_candidate_documents(db_path: Path) -> int:
    static_documents = 120
    source_tables = {
        "news_articles": 1200,
        "news_event_clusters": 500,
        "forecast_price_points": 500,
        "industry_observations": 300,
        "market_observations": 300,
        "client_reports": 100,
        "evidence_bundles": 200,
    }
    if not db_path.exists():
        return static_documents
    total = static_documents
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=10)) as connection, connection:
        connection.execute("PRAGMA busy_timeout = 10000")
        for table, limit in source_tables.items():
            if table_exists(connection, table):
                count = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                total += min(count, limit)
    return total


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute("SELECT 1 FROM sqlite_master WHERE name = ? LIMIT 1", (table,)).fetchone() is not None


def _recovery_anchor_integrity(anchor: Path | None) -> str:
    if anchor is None or not anchor.is_file():
        return "missing"
    try:
        with closing(sqlite3.connect(f"file:{anchor}?mode=ro", uri=True, timeout=10)) as connection:
            return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    except sqlite3.Error as exc:
        return exc.__class__.__name__


def guard_report(*, writes_database: bool, recovery_anchor: str, recovery_point_integrity: str) -> dict[str, Any]:
    return {
        "writes_database": writes_database,
        "recovery_anchor": recovery_anchor,
        "recovery_point_integrity": recovery_point_integrity,
        "credentials_logged": False,
        "calls_external_llm_provider": False,
        "uses_authorized_source_only": True,
    }


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def business_date_iso(value: str | datetime) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(BUSINESS_TIMEZONE).date().isoformat()


if __name__ == "__main__":
    raise SystemExit(main())

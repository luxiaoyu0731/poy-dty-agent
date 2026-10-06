from __future__ import annotations

from contextlib import closing
from typing import Any

from .context_pack import build_context_pack
from .foundation_utils import is_at_or_before, json_dumps, json_loads
from .storage import connect


def create_assistant_context_pack(
    question: str,
    *,
    context_event_id: str | None = None,
    as_of_time: str | None = None,
    persist: bool,
) -> dict[str, Any]:
    """Build one reproducible answer input; preview builds never write."""
    # The complete user question remains the primary query. A short, explicit
    # domain suffix only resolves terse follow-ups; it never replaces intent.
    retrieval_question = question
    explicit_products = ("poy", "dty", "px", "pta", "meg", "原油", "石脑油", "乙二醇", "naphtha", "brent", "wti")
    if not any(term in question.lower() for term in explicit_products):
        retrieval_question += "\n领域上下文：POY DTY 上游原料"
    if context_event_id:
        retrieval_question += f"\n关联事件ID：{context_event_id}"
    pack = build_context_pack(
        question,
        task_type="assistant_answer",
        product=_infer_product(question),
        context_event_id=context_event_id,
        as_of_time=as_of_time,
        persist=persist,
        retrieval_query=retrieval_question,
    )
    metadata = dict(pack.get("metadata") or {})
    chunk_result = _chunk_metadata(pack)
    data_snapshot_id = _latest_visible_snapshot_id(as_of_time)
    retrieval = pack.get("retrieval")
    documents = list(getattr(retrieval, "documents", []) or [])
    graph = pack.get("graph") if isinstance(pack.get("graph"), dict) else {}
    graph_memory = pack.get("graph_memory") if isinstance(pack.get("graph_memory"), dict) else {}
    degradations = list(pack.get("quality_gates") or [])
    if not documents:
        degradations.append("no_retrieval_evidence")
    if chunk_result.get("index_status") != "ready":
        degradations.append(f"index_status:{chunk_result.get('index_status', 'unknown')}")
    if graph.get("status") != "ready":
        degradations.append(f"graphrag_status:{graph.get('status', 'degraded')}")
    degradations.extend(str(item) for item in graph_memory.get("warnings", []))
    if not data_snapshot_id:
        degradations.append("data_snapshot_unavailable")
    degradations = sorted(set(degradations))
    leak_check = str(metadata.get("leak_check_result") or "")
    if not documents or not data_snapshot_id:
        leak_check = "indeterminate"
    metadata.update(
        {
            "data_snapshot_id": data_snapshot_id,
            "embedding_model": chunk_result.get("embedding_model", ""),
            "embedding_version": chunk_result.get("embedding_version", ""),
            "index_version": chunk_result.get("index_version") or metadata.get("index_version", ""),
            "retrieval_mode": chunk_result.get("retrieval_mode", "compatibility"),
            "index_status": chunk_result.get("index_status", "unknown"),
            "preview": not persist,
            "quality_gate_status": "degraded" if degradations else "passed",
            "quality_gate_reasons": degradations,
            "leak_check_result": leak_check,
        }
    )
    pack["metadata"] = metadata
    if persist:
        with closing(connect()) as connection, connection:
            connection.execute(
                "UPDATE context_packs SET metadata=? WHERE pack_id=?",
                (json_dumps(metadata), pack["pack_id"]),
            )
    return pack


def _chunk_metadata(pack: dict[str, Any]) -> dict[str, Any]:
    # Context-pack v1 records the stable index/run fields. New retriever versions
    # can add richer fields without changing the assistant contract.
    metadata = dict(pack.get("metadata") or {})
    return {
        "embedding_model": metadata.get("embedding_model", ""),
        "embedding_version": metadata.get("embedding_version", ""),
        "index_version": metadata.get("index_version", ""),
        "retrieval_mode": metadata.get("retrieval_mode", "hybrid_chunk"),
        "index_status": metadata.get("index_status", "ready") if metadata.get("index_version") else "fallback",
    }


def _latest_visible_snapshot_id(as_of_time: str | None) -> str:
    try:
        with closing(connect()) as connection:
            rows = connection.execute(
                """
                SELECT snapshot_id, created_at, metadata
                FROM data_snapshots
                ORDER BY created_at DESC
                """
            ).fetchall()
    except Exception:
        return ""
    row = next(
        (
            candidate
            for candidate in rows
            if not as_of_time
            or is_at_or_before(
                json_loads(candidate["metadata"], {}).get("visible_at") or candidate["created_at"],
                as_of_time,
            )
        ),
        None,
    )
    if row is None:
        return ""
    metadata = json_loads(row["metadata"], {})
    if as_of_time and metadata.get("visible_at") and not is_at_or_before(metadata["visible_at"], as_of_time):
        return ""
    return str(row["snapshot_id"])


def _infer_product(question: str) -> str:
    upper = question.upper()
    if "DTY" in upper and "POY" not in upper:
        return "DTY"
    return "POY"

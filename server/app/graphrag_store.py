from __future__ import annotations

import hashlib
from contextlib import closing
from typing import Any

from .foundation_utils import (
    is_at_or_before,
    json_dumps,
    json_loads,
    new_id,
    now_iso,
    safe_summary,
    stable_hash,
)
from .knowledge_graph import graph_payload
from .storage import connect

NODE_TYPE_LABELS = {
    "question": "业务问题",
    "price": "价格证据",
    "commodity": "上游原料",
    "supply_demand": "聚酯供需",
    "event": "新闻事件",
    "judgement": "成本传导判断",
    "counter": "反证检查",
    "conclusion": "业务结论",
    "quality": "质量门禁",
    "report": "报告结论",
}

RELATION_LABELS = {
    "supports": "支持",
    "refutes": "反驳",
    "impacts": "影响",
    "transmits": "传导",
    "cites": "引用",
    "conflicts": "冲突",
    "needs_review": "需要复核",
    "generated_from": "生成自",
}

GRAPH_SCHEMA_VERSION = "graphrag-snapshot-v2"


def materialize_graph_snapshot(
    *,
    question: str = "",
    product: str = "POY",
    limit: int = 80,
    as_of_time: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    payload = graph_payload(q=question, product=product, include_technical=True, limit=limit)
    payload = _filter_payload_as_of(payload, as_of_time)
    now = now_iso()
    payload_sha256 = hashlib.sha256(json_dumps(payload).encode("utf-8")).hexdigest()
    if not persist:
        return {
            "snapshot_id": "",
            "node_count": len(payload.get("nodes", [])),
            "edge_count": len(payload.get("edges", [])),
            "payload": payload,
            "payload_sha256": payload_sha256,
            "graph_version": GRAPH_SCHEMA_VERSION,
            "as_of_time": as_of_time or "",
            "persisted": False,
        }
    node_ids: list[str] = []
    edge_ids: list[str] = []
    with closing(connect()) as connection, connection:
        for node in payload.get("nodes", []):
            node_id = str(node.get("id"))
            node_ids.append(node_id)
            connection.execute(
                """
                INSERT OR REPLACE INTO graph_nodes (
                  node_id, created_at, updated_at, node_type, label, summary, evidence_level, status, metadata
                ) VALUES (?, COALESCE((SELECT created_at FROM graph_nodes WHERE node_id = ?), ?), ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    node_id,
                    now,
                    now,
                    _node_type(node),
                    safe_summary(str(node.get("label") or node_id), max_chars=240),
                    safe_summary(str(node.get("summary") or node.get("description") or ""), max_chars=1200),
                    _tier(node),
                    str(node.get("status") or "已采用"),
                    json_dumps(node),
                ),
            )
        for edge in payload.get("edges", []):
            source = str(edge.get("source"))
            target = str(edge.get("target"))
            relation = _relation(edge)
            edge_key = {"source": source, "target": target, "relation": relation}
            edge_id = str(edge.get("id") or f"edge_{stable_hash(edge_key)}")
            edge_ids.append(edge_id)
            connection.execute(
                """
                INSERT OR REPLACE INTO graph_edges (
                  edge_id, created_at, updated_at, source_node_id, target_node_id, relation,
                  confidence, evidence_ids, metadata
                ) VALUES (?, COALESCE((SELECT created_at FROM graph_edges WHERE edge_id = ?), ?), ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    edge_id,
                    edge_id,
                    now,
                    now,
                    source,
                    target,
                    relation,
                    float(edge.get("confidence") or edge.get("weight") or 0.65),
                    json_dumps(_edge_evidence_ids(edge, payload.get("nodes", []))),
                    json_dumps(edge),
                ),
            )
        snapshot_id = new_id("graph_snapshot")
        connection.execute(
            """
            INSERT INTO graph_snapshots (
              snapshot_id, created_at, question, product, node_ids, edge_ids, payload, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                now,
                question,
                product,
                json_dumps(node_ids),
                json_dumps(edge_ids),
                json_dumps(payload),
                json_dumps(
                    {
                        "node_count": len(node_ids),
                        "edge_count": len(edge_ids),
                        "graph_version": GRAPH_SCHEMA_VERSION,
                        "as_of_time": as_of_time or "",
                    }
                ),
            ),
        )
    return {
        "snapshot_id": snapshot_id,
        "node_count": len(node_ids),
        "edge_count": len(edge_ids),
        "payload": payload,
        "payload_sha256": payload_sha256,
        "graph_version": GRAPH_SCHEMA_VERSION,
        "as_of_time": as_of_time or "",
        "persisted": True,
    }


def list_graph_nodes(*, limit: int = 120) -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        rows = connection.execute(
            """
            SELECT * FROM graph_nodes
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (min(max(limit, 1), 500),),
        ).fetchall()
    return [_node_row(row) for row in rows]


def list_graph_edges(*, limit: int = 240) -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        rows = connection.execute(
            """
            SELECT * FROM graph_edges
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (min(max(limit, 1), 1000),),
        ).fetchall()
    return [_edge_row(row) for row in rows]


def latest_graph_snapshot(*, as_of_time: str | None = None) -> dict[str, Any] | None:
    with closing(connect()) as connection:
        if as_of_time:
            rows = connection.execute("SELECT * FROM graph_snapshots ORDER BY created_at DESC").fetchall()
            row = next(
                (
                    candidate
                    for candidate in rows
                    if str(json_loads(candidate["metadata"], {}).get("as_of_time") or "") == as_of_time
                ),
                None,
            )
        else:
            row = connection.execute("SELECT * FROM graph_snapshots ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    item = dict(row)
    item["node_ids"] = json_loads(item.get("node_ids"), [])
    item["edge_ids"] = json_loads(item.get("edge_ids"), [])
    item["payload"] = json_loads(item.get("payload"), {})
    item["metadata"] = json_loads(item.get("metadata"), {})
    return item


def get_graph_snapshot(snapshot_id: str) -> dict[str, Any] | None:
    """Load the immutable payload belonging to one exact graph snapshot."""

    with closing(connect()) as connection:
        row = connection.execute(
            "SELECT * FROM graph_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["node_ids"] = json_loads(item.get("node_ids"), [])
    item["edge_ids"] = json_loads(item.get("edge_ids"), [])
    item["payload"] = json_loads(item.get("payload"), {})
    item["metadata"] = json_loads(item.get("metadata"), {})
    return item


def _node_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["metadata"] = json_loads(item.get("metadata"), {})
    item["business_type"] = NODE_TYPE_LABELS.get(item["node_type"], item["node_type"])
    return item


def _edge_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["evidence_ids"] = json_loads(item.get("evidence_ids"), [])
    item["metadata"] = json_loads(item.get("metadata"), {})
    item["business_relation"] = RELATION_LABELS.get(item["relation"], item["relation"])
    return item


def _node_type(node: dict[str, Any]) -> str:
    raw = str(node.get("type") or node.get("node_type") or "").lower()
    label = str(node.get("label") or "")
    if "quality" in raw or "门禁" in label:
        return "quality"
    if "event" in raw or "事件" in label or "新闻" in label:
        return "event"
    if "price" in raw or "价格" in label:
        return "price"
    if "prediction" in raw or "结论" in label or "判断" in label:
        return "judgement"
    if "counter" in raw or "反证" in label:
        return "counter"
    if "report" in raw or "报告" in label:
        return "report"
    if any(item in label.upper() for item in ("PX", "PTA", "MEG", "POY", "DTY")):
        return "commodity"
    return "supply_demand" if "库存" in label or "开工" in label else "commodity"


def _relation(edge: dict[str, Any]) -> str:
    raw = str(edge.get("label") or edge.get("relation") or edge.get("type") or "").lower()
    if "refute" in raw or "反" in raw:
        return "refutes"
    if "conflict" in raw or "冲突" in raw:
        return "conflicts"
    if "review" in raw or "复核" in raw:
        return "needs_review"
    if "cite" in raw or "引用" in raw:
        return "cites"
    if "trans" in raw or "传导" in raw:
        return "transmits"
    if "impact" in raw or "影响" in raw:
        return "impacts"
    if "generate" in raw or "生成" in raw:
        return "generated_from"
    return "supports"


def _tier(node: dict[str, Any]) -> str:
    tier = str(node.get("evidence_level") or node.get("tier") or "C").upper()
    return tier if tier in {"A", "B", "C", "D"} else "C"


def _filter_payload_as_of(payload: dict[str, Any], as_of_time: str | None) -> dict[str, Any]:
    if not as_of_time:
        nodes = [node for node in payload.get("nodes", []) if not _is_rejected_record(node)]
    else:
        nodes = [node for node in payload.get("nodes", []) if _record_visible_as_of(node, as_of_time)]
    node_ids = {str(node.get("id")) for node in nodes}
    original_node_ids = {str(node.get("id")) for node in payload.get("nodes", [])}
    edges = [
        edge
        for edge in payload.get("edges", [])
        if str(edge.get("source")) in node_ids
        and str(edge.get("target")) in node_ids
        and all(
            str(evidence_id) not in original_node_ids or str(evidence_id) in node_ids
            for evidence_id in edge.get("evidence_ids", [])
        )
        and (not as_of_time or _record_visible_as_of(edge, as_of_time, require_timestamp=False))
        and not _is_rejected_record(edge)
    ]
    filtered = dict(payload)
    filtered["nodes"] = nodes
    filtered["edges"] = edges
    summary = dict(payload.get("summary") or {})
    summary["node_count"] = len(nodes)
    summary["edge_count"] = len(edges)
    filtered["summary"] = summary
    return filtered


def _record_visible_as_of(
    record: dict[str, Any],
    as_of_time: str,
    *,
    require_timestamp: bool | None = None,
) -> bool:
    if _is_rejected_record(record):
        return False
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    timestamps = [
        str(value)
        for field in (
            "observed_at",
            "visible_at",
            "generated_at",
            "created_at",
            "updated_at",
        )
        for value in (record.get(field), metadata.get(field))
        if value
    ]
    if any(not is_at_or_before(value, as_of_time) for value in timestamps):
        return False
    if require_timestamp is None:
        raw_type = str(record.get("type") or record.get("node_type") or "").lower()
        require_timestamp = any(
            marker in raw_type for marker in ("news", "event", "price", "observation", "prediction", "report")
        )
    return bool(timestamps) or not require_timestamp


def _is_rejected_record(record: dict[str, Any]) -> bool:
    metadata = record.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    states = {
        str(record.get("review_status") or "").lower(),
        str(record.get("status") or "").lower(),
        str(metadata.get("review_status") or "").lower(),
        str(metadata.get("status") or "").lower(),
    }
    return "rejected" in states or "已拒绝" in states


def _edge_evidence_ids(edge: dict[str, Any], nodes: list[dict[str, Any]]) -> list[str]:
    explicit = [canonical_graph_doc_id(str(item)) for item in edge.get("evidence_ids") or [] if item]
    node_map = {str(node.get("id")): node for node in nodes}
    for endpoint in (str(edge.get("source") or ""), str(edge.get("target") or "")):
        node = node_map.get(endpoint, {})
        node_type = str(node.get("type") or node.get("node_type") or "")
        if node_type in {
            "NewsArticle",
            "EventCluster",
            "EventObservation",
            "PriceObservation",
            "ProjectDocument",
        }:
            explicit.append(canonical_graph_doc_id(endpoint))
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        explicit.extend(str(item) for item in metadata.get("cited_doc_ids", []) if item)
    return list(dict.fromkeys(explicit))


def canonical_graph_doc_id(node_id: str) -> str:
    """Map graph node namespaces to the persisted RAG document namespace."""

    mappings = (
        ("article:", "news_article:"),
        ("event_cluster:", "news_event:"),
        ("price:market:", "market:"),
        ("price:industry:", "industry:"),
        ("intelligence:", "event_intelligence:"),
    )
    for graph_prefix, document_prefix in mappings:
        if node_id.startswith(graph_prefix):
            return f"{document_prefix}{node_id.removeprefix(graph_prefix)}"
    return node_id

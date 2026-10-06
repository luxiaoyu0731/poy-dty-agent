from __future__ import annotations

import re
from collections import deque
from contextlib import closing
from typing import Any

from .foundation_utils import json_dumps, json_loads, new_id, now_iso, safe_summary
from .graphrag_store import (
    GRAPH_SCHEMA_VERSION,
    canonical_graph_doc_id,
    get_graph_snapshot,
    materialize_graph_snapshot,
)
from .storage import connect

_EVIDENCE_NODE_TYPES = {
    "NewsArticle",
    "EventCluster",
    "EventObservation",
    "PriceObservation",
    "ProjectDocument",
}
_CONFLICT_RELATIONS = {"refutes", "conflicts", "judgment_has_counter_evidence", "backtest_explains_error"}


def build_reasoning_path(
    *,
    question: str,
    product: str = "POY",
    snapshot_id: str | None = None,
    as_of_time: str | None = None,
    persist: bool = True,
    snapshot_payload: dict[str, Any] | None = None,
    allowed_evidence_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Discover a traceable path inside one immutable graph snapshot.

    The result is retrieval/explanation context, never an autonomous business
    conclusion. A path without source evidence is reported as degraded instead
    of being synthesized from product labels.
    """

    snapshot = (
        {
            "snapshot_id": "",
            "payload": snapshot_payload,
            "metadata": {
                "as_of_time": as_of_time or "",
                "graph_version": GRAPH_SCHEMA_VERSION,
            },
        }
        if snapshot_payload is not None
        else get_graph_snapshot(snapshot_id)
        if snapshot_id
        else None
    )
    if snapshot is None:
        created = materialize_graph_snapshot(
            question=question,
            product=product,
            as_of_time=as_of_time,
            persist=True,
        )
        snapshot = get_graph_snapshot(created["snapshot_id"])
    if snapshot is None:  # defensive: persistence failure must not create a pseudo path
        return _degraded_path(question, product, as_of_time, "graph_snapshot_unavailable")

    snapshot_as_of = str(snapshot.get("metadata", {}).get("as_of_time") or "")
    if as_of_time and snapshot_as_of and snapshot_as_of != as_of_time:
        return _degraded_path(
            question,
            product,
            as_of_time,
            "graph_snapshot_as_of_mismatch",
            snapshot_id=str(snapshot["snapshot_id"]),
        )

    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    nodes = {str(node.get("id")): node for node in payload.get("nodes", [])}
    edges = [edge for edge in payload.get("edges", []) if _edge_is_in_snapshot(edge, nodes)]
    evidence_starts = _select_evidence_starts(
        nodes,
        question,
        product,
        allowed_evidence_ids=allowed_evidence_ids,
    )
    targets = _select_targets(nodes, question, product)
    node_ids, edge_ids = _best_evidence_path(evidence_starts, targets, edges)
    path_edges = [edge for edge in edges if str(edge.get("id")) in set(edge_ids)]
    evidence_doc_ids = _path_evidence_ids(node_ids, path_edges, nodes)
    conflicts = _discover_conflicts(edges, nodes, set(node_ids))
    warnings: list[str] = []
    if not node_ids or not evidence_doc_ids:
        node_ids, edge_ids, path_edges, evidence_doc_ids = [], [], [], []
        status = "degraded_no_evidence_path"
        warnings.append("GraphRAG 未找到绑定原始证据的有效路径，已降级为无图增强。")
    else:
        status = "ready"

    path_id = new_id("graph_path") if persist else ""
    now = now_iso()
    explanation = _path_explanation(nodes, node_ids, question, evidence_doc_ids)
    graph_version = str(snapshot.get("metadata", {}).get("graph_version") or GRAPH_SCHEMA_VERSION)
    metadata = {
        "node_count": len(node_ids),
        "edge_count": len(edge_ids),
        "snapshot_id": snapshot["snapshot_id"],
        "graph_version": graph_version,
        "as_of_time": snapshot_as_of or as_of_time or "",
        "evidence_doc_ids": evidence_doc_ids,
        "conflicts": conflicts,
        "role": "retrieval_and_explanation_only",
    }
    if persist:
        with closing(connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO graph_reasoning_paths (
                  path_id, created_at, question, product, start_node_id, end_node_id, node_ids,
                  edge_ids, conclusion, status, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    path_id,
                    now,
                    question,
                    product,
                    node_ids[0] if node_ids else "",
                    node_ids[-1] if node_ids else "",
                    json_dumps(node_ids),
                    json_dumps(edge_ids),
                    explanation,
                    status,
                    json_dumps(metadata),
                ),
            )
    return {
        "path_id": path_id,
        "created_at": now,
        "question": question,
        "product": product,
        "snapshot_id": snapshot["snapshot_id"],
        "graph_version": graph_version,
        "as_of_time": snapshot_as_of or as_of_time or "",
        "node_ids": node_ids,
        "edge_ids": edge_ids,
        "entities": [
            {
                "node_id": node_id,
                "label": str(nodes.get(node_id, {}).get("label") or node_id),
                "node_type": str(nodes.get(node_id, {}).get("type") or nodes.get(node_id, {}).get("node_type") or ""),
            }
            for node_id in node_ids
        ],
        "evidence_doc_ids": evidence_doc_ids,
        "conflicts": conflicts,
        "explanation": explanation,
        "conclusion": explanation,  # compatibility: explicitly non-conclusive wording
        "status": status,
        "warnings": warnings,
    }


def list_reasoning_paths(*, limit: int = 50) -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        rows = connection.execute(
            """
            SELECT * FROM graph_reasoning_paths
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (min(max(limit, 1), 200),),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["node_ids"] = json_loads(item.get("node_ids"), [])
        item["edge_ids"] = json_loads(item.get("edge_ids"), [])
        item["metadata"] = json_loads(item.get("metadata"), {})
        item["snapshot_id"] = item["metadata"].get("snapshot_id", "")
        item["evidence_doc_ids"] = item["metadata"].get("evidence_doc_ids", [])
        items.append(item)
    return items


def _select_evidence_starts(
    nodes: dict[str, dict[str, Any]],
    question: str,
    product: str,
    *,
    allowed_evidence_ids: set[str] | None = None,
) -> list[str]:
    terms = _terms(f"{question} {product}")
    allowed = set(allowed_evidence_ids or ())
    ranked: list[tuple[int, int, str]] = []
    for node_id, node in nodes.items():
        node_type = str(node.get("type") or node.get("node_type"))
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        cited = {str(item) for item in metadata.get("cited_doc_ids", []) if item}
        canonical_id = canonical_graph_doc_id(node_id)
        bound_allowed = canonical_id in allowed or bool(cited & allowed)
        if allowed and not bound_allowed:
            continue
        if node_type == "ProjectDocument" and allowed:
            continue
        if node_type not in _EVIDENCE_NODE_TYPES and not cited:
            continue
        text = f"{node.get('label', '')} {node.get('summary', '')}".lower()
        score = sum(1 for term in terms if term in text)
        if score or bound_allowed:
            ranked.append((1 if bound_allowed else 0, score, node_id))
    ranked.sort(reverse=True)
    return [node_id for _, _, node_id in ranked[:20]]


def _select_targets(
    nodes: dict[str, dict[str, Any]],
    question: str,
    product: str,
) -> set[str]:
    terms = _terms(f"{question} {product}")
    targets = {
        node_id
        for node_id, node in nodes.items()
        if str(node.get("type") or node.get("node_type")) in {"Product", "commodity", "judgement", "conclusion"}
        and any(term in f"{node.get('label', '')} {node_id}".lower() for term in terms)
    }
    product_target = f"product:{product}"
    if product_target in nodes:
        targets.add(product_target)
    return targets


def _best_evidence_path(
    starts: list[str],
    targets: set[str],
    edges: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    if not starts or not targets:
        return [], []
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for edge in edges:
        source = str(edge.get("source"))
        target = str(edge.get("target"))
        edge_id = str(edge.get("id"))
        adjacency.setdefault(source, []).append((target, edge_id))
    for start in starts:
        queue: deque[tuple[str, list[str], list[str]]] = deque([(start, [start], [])])
        visited = {start}
        while queue:
            node_id, path_nodes, path_edges = queue.popleft()
            if node_id in targets and node_id != start:
                return path_nodes, path_edges
            if len(path_edges) >= 8:
                continue
            for next_node, edge_id in adjacency.get(node_id, []):
                if next_node in visited:
                    continue
                visited.add(next_node)
                queue.append((next_node, [*path_nodes, next_node], [*path_edges, edge_id]))
    return [], []


def _path_evidence_ids(
    node_ids: list[str],
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
) -> list[str]:
    result: list[str] = []
    for node_id in node_ids:
        node = nodes.get(node_id, {})
        if str(node.get("type") or node.get("node_type")) in _EVIDENCE_NODE_TYPES:
            result.append(canonical_graph_doc_id(node_id))
        metadata = node.get("metadata") if isinstance(node.get("metadata"), dict) else {}
        result.extend(str(item) for item in metadata.get("cited_doc_ids", []) if item)
    for edge in edges:
        result.extend(canonical_graph_doc_id(str(item)) for item in edge.get("evidence_ids", []) if item)
    return list(dict.fromkeys(result))


def _discover_conflicts(
    edges: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    path_nodes: set[str],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    polarities: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for edge in edges:
        relation = str(edge.get("relation") or "")
        target = str(edge.get("target") or "")
        polarity = str(edge.get("polarity") or "neutral")
        if relation in _CONFLICT_RELATIONS and (str(edge.get("source")) in path_nodes or target in path_nodes):
            doc_ids = _path_evidence_ids(
                [str(edge.get("source")), target],
                [edge],
                nodes,
            )
            conflicts.append(
                {
                    "edge_id": str(edge.get("id")),
                    "relation": relation,
                    "evidence_doc_ids": doc_ids,
                }
            )
        if polarity in {"up", "down"}:
            polarities.setdefault(target, {}).setdefault(polarity, []).append(edge)
    for target, by_direction in polarities.items():
        if not by_direction.get("up") or not by_direction.get("down"):
            continue
        relevant = [*by_direction["up"], *by_direction["down"]]
        if (
            path_nodes
            and target not in path_nodes
            and not any(str(item.get("source")) in path_nodes for item in relevant)
        ):
            continue
        doc_ids = _path_evidence_ids(
            [str(item.get("source")) for item in relevant],
            relevant,
            nodes,
        )
        conflicts.append(
            {
                "target_node_id": target,
                "relation": "opposing_polarity",
                "evidence_doc_ids": doc_ids,
            }
        )
    return conflicts


def _path_explanation(
    nodes: dict[str, dict[str, Any]],
    node_ids: list[str],
    question: str,
    evidence_doc_ids: list[str],
) -> str:
    if not node_ids or not evidence_doc_ids:
        return f"围绕“{safe_summary(question, max_chars=80)}”未发现可追溯至原始证据的图路径。"
    labels = [str(nodes[node_id].get("label") or node_id) for node_id in node_ids if node_id in nodes]
    return (
        f"图检索发现路径 {' -> '.join(labels[:9])}；"
        f"其原始证据为 {', '.join(evidence_doc_ids[:8])}。该路径只用于扩展检索和解释，不是正式结论。"
    )


def _degraded_path(
    question: str,
    product: str,
    as_of_time: str | None,
    warning: str,
    *,
    snapshot_id: str = "",
) -> dict[str, Any]:
    return {
        "path_id": "",
        "created_at": now_iso(),
        "question": question,
        "product": product,
        "snapshot_id": snapshot_id,
        "graph_version": GRAPH_SCHEMA_VERSION,
        "as_of_time": as_of_time or "",
        "node_ids": [],
        "edge_ids": [],
        "entities": [],
        "evidence_doc_ids": [],
        "conflicts": [],
        "explanation": "未形成可追溯图路径。",
        "conclusion": "未形成可追溯图路径。",
        "status": "degraded_no_evidence_path",
        "warnings": [warning],
    }


def _edge_is_in_snapshot(edge: dict[str, Any], nodes: dict[str, dict[str, Any]]) -> bool:
    return str(edge.get("source")) in nodes and str(edge.get("target")) in nodes


def _terms(text: str) -> list[str]:
    ascii_terms = re.findall(r"[a-z0-9_+\-]{2,}", text.lower())
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    chinese_terms: list[str] = []
    for block in chinese:
        chinese_terms.extend(block[index : index + 2] for index in range(max(1, len(block) - 1)))
    return list(dict.fromkeys([*ascii_terms, *chinese_terms]))

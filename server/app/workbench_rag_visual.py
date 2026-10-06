from __future__ import annotations

import re
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime

from .formal_judgement import derive_formal_direction
from .intelligence import QUOTE_TYPE_LABELS, build_full_chain_summary
from .semantic_index import semantic_index_status
from .storage import connect, get_evidence_review_map, list_forecast_price_points, list_industry_observations
from .unified_retriever import retrieve_chunks


def _formal_rows_from_snapshot(snapshot: dict[str, object]) -> list[dict[str, object]]:
    payload = snapshot.get("payload") if isinstance(snapshot.get("payload"), dict) else {}
    rows: list[dict[str, object]] = []
    for key, prefix, id_key in (
        ("market_observations", "market", "observation_id"),
        ("industry_observations", "industry", "observation_id"),
        ("authorized_price_observations", "ccf_spot", "point_id"),
    ):
        for source in payload.get(key, []) if isinstance(payload.get(key), list) else []:
            if not isinstance(source, dict) or not source.get(id_key):
                continue
            rows.append(
                {
                    "doc_id": f"{prefix}:{source[id_key]}",
                    "product": source.get("product") or source.get("instrument"),
                    "observed_at": source.get("observed_at"),
                    "value": source.get("value") if source.get("value") is not None else source.get("price"),
                }
            )
    return rows


def _rag_visual_doc_category(doc_type: str) -> str:
    categories = {
        "project_document": "项目规则",
        "knowledge_node": "产业链知识",
        "knowledge_edge": "传导关系",
        "source_config": "数据来源说明",
        "news_article": "新闻与公告",
        "news_event_cluster": "事件线索",
        "event_observation": "结构化事件",
        "market_observation": "市场观测",
        "authorized_spot_observation": "授权现货观测",
        "industry_observation": "行业指标",
        "prediction_record": "历史复盘",
        "event_intelligence_snapshot": "事件快照",
        "political_case_memory": "政治事件复盘",
    }
    return categories.get(doc_type, "业务证据")


def _rag_visual_tone_from_tier(tier: str) -> str:
    if tier == "A":
        return "success"
    if tier == "B":
        return "info"
    if tier == "C":
        return "warning"
    return "muted"


def _rag_visual_has_event_intent(question: str) -> bool:
    normalized = question.casefold()
    return any(
        term in normalized
        for term in (
            "事件",
            "制裁",
            "战争",
            "冲突",
            "政策",
            "公告",
            "新闻",
            "风险事件",
            "地缘",
            "sanction",
            "war",
            "conflict",
            "policy",
            "event",
            "news",
            "geopolit",
            "ofac",
            "opec",
            "hormuz",
            "霍尔木兹",
            "红海",
        )
    )


def _rag_visual_requested_price_products(question: str) -> list[str]:
    normalized = question.upper()
    return [product for product in ("PX", "PTA", "MEG", "POY", "DTY") if product in normalized]


def _rag_visual_intent_eligible(document: dict[str, object], *, question: str) -> bool:
    doc_type = str(document.get("doc_type") or "")
    event_only_types = {
        "news_article",
        "news_event_cluster",
        "event_observation",
        "political_case_memory",
        "event_intelligence_snapshot",
    }
    return doc_type not in event_only_types or _rag_visual_has_event_intent(question)


def _rag_visual_compact(value: object, fallback: str = "", limit: int = 92) -> str:
    text = str(value or fallback).replace("\n", " ").strip()
    replacements = {
        "internal_weak_signal": "人工市场线索",
        "realtime": "实时更新",
        "Internal notes; require human review before prediction.": "人工市场笔记，进入判断前需要复核。",
        "Internal notes": "人工市场笔记",
        "commodity": "商品链路",
        "provider": "数据服务",
        "license": "使用边界",
        "login": "访问",
        "API key": "接口凭证",
        "GraphRAG": "证据图谱",
        "RAG": "证据检索",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"\b[A-Za-z]+(?:_[A-Za-z0-9]+)+\b", "业务标识", text)
    text = re.sub(r"\b[a-z]+[A-Z][A-Za-z0-9]*\b", "业务标识", text)
    return text if len(text) <= limit else f"{text[:limit]}…"


def _rag_visual_node_for_doc_type(doc_type: str) -> str:
    normalized = doc_type.lower()
    if normalized in {"project_document", "source_config"}:
        return "boundary"
    if normalized in {"knowledge_node", "knowledge_edge"}:
        return "chain"
    if normalized == "market_observation":
        return "price"
    if normalized == "industry_observation":
        return "industry"
    if normalized in {"news_article", "news_event_cluster", "event_observation"}:
        return "event"
    if normalized in {"prediction_record", "event_intelligence_snapshot", "political_case_memory"}:
        return "history"
    return "conclusion"


def _rag_visual_evidence_item(
    document: object, *, fallback_status: str = "已采用", node_id: str | None = None
) -> dict[str, object]:
    doc = document.model_dump() if hasattr(document, "model_dump") else dict(document)  # type: ignore[arg-type]
    doc_type = str(doc.get("doc_type") or "")
    category = _rag_visual_doc_category(str(doc.get("doc_type") or ""))
    tier = str(doc.get("tier") or "D")
    observed_at = str(doc.get("observed_at") or "")
    if fallback_status == "背景知识":
        reason = "用于解释产业链关系，不作为当前价格方向或正式结论的证据。"
    elif fallback_status == "已排除":
        reason = (
            f"证据等级为 {tier}，不足以进入正式结论。"
            if tier in {"C", "D"}
            else "与本次业务问题意图不一致，未进入证据包。"
        )
    elif fallback_status == "存在冲突":
        reason = "材料包含冲突、反证或质量风险标记，等待进一步复核。"
    else:
        reason = "与本次业务问题相关，进入证据复核链路。"
    return {
        "id": str(doc.get("doc_id") or doc.get("title") or category),
        "node_id": node_id or _rag_visual_node_for_doc_type(doc_type),
        "title": _rag_visual_compact(doc.get("title"), category, 64),
        "summary": _rag_visual_compact(doc.get("snippet") or doc.get("summary"), "当前证据仅返回摘要。", 118),
        "category": category,
        "status": fallback_status,
        "tone": _rag_visual_tone_from_tier(tier),
        "tier": tier,
        "observed_label": observed_at[:10] if observed_at else "时间待确认",
        "url": str(doc.get("url") or ""),
        "reason": reason,
    }


def _rag_visual_first_documents_by_type(
    documents: list[object], doc_types: set[str], limit: int = 4
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for document in documents:
        doc = document.model_dump() if hasattr(document, "model_dump") else dict(document)  # type: ignore[arg-type]
        if str(doc.get("doc_type") or "") in doc_types:
            items.append(_rag_visual_evidence_item(document))
        if len(items) >= limit:
            break
    return items


def _rag_visual_index_documents(limit: int) -> tuple[list[dict[str, object]], dict[str, int]]:
    groups: list[tuple[set[str], int]] = [
        ({"knowledge_node", "knowledge_edge"}, 3),
        ({"market_observation"}, 3),
        ({"industry_observation"}, 2),
        ({"news_article", "news_event_cluster", "event_observation"}, 4),
        ({"political_case_memory", "event_intelligence_snapshot", "prediction_record"}, 3),
    ]
    documents: list[dict[str, object]] = []
    seen: set[str] = set()
    counts: dict[str, int] = {}
    with closing(connect()) as connection:
        rows = connection.execute(
            """
            SELECT source_kind, COUNT(*) AS count
            FROM rag_documents
            WHERE can_use_pre_forecast = 1
            GROUP BY source_kind
            """
        ).fetchall()
        counts = {str(row["source_kind"]): int(row["count"]) for row in rows}
        for doc_types, group_limit in groups:
            placeholders = ",".join("?" for _ in doc_types)
            rows = connection.execute(
                f"""
                SELECT
                  document_id, source_kind, source_id, title, summary, body,
                  observed_at, visible_at, evidence_level, url
                FROM rag_documents
                WHERE can_use_pre_forecast = 1
                  AND source_kind IN ({placeholders})
                ORDER BY
                  CASE evidence_level WHEN 'A' THEN 4 WHEN 'B' THEN 3 WHEN 'C' THEN 2 ELSE 1 END DESC,
                  COALESCE(NULLIF(visible_at, ''), NULLIF(observed_at, ''), updated_at) DESC
                LIMIT ?
                """,
                (*sorted(doc_types), group_limit),
            ).fetchall()
            for row in rows:
                if row["document_id"] in seen:
                    continue
                seen.add(row["document_id"])
                documents.append(
                    {
                        "doc_id": row["document_id"],
                        "doc_type": row["source_kind"],
                        "source_id": row["source_id"],
                        "title": row["title"],
                        "summary": row["summary"],
                        "snippet": row["summary"] or row["body"],
                        "observed_at": row["visible_at"] or row["observed_at"],
                        "tier": row["evidence_level"],
                        "url": row["url"],
                        "risk_flags": [],
                    }
                )
                if len(documents) >= limit:
                    break
            if len(documents) >= limit:
                break
    return documents[:limit], counts


def _rag_visual_fast_counts() -> dict[str, int]:
    with closing(connect()) as connection:
        row = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM rag_documents) AS documents,
              (SELECT COUNT(*) FROM rag_chunks) AS chunks,
              (SELECT COUNT(*) FROM graph_nodes) AS graph_nodes,
              (SELECT COUNT(*) FROM graph_edges) AS graph_edges
            """
        ).fetchone()
    return {
        "documents": int(row["documents"] if row else 0),
        "chunks": int(row["chunks"] if row else 0),
        "graph_nodes": int(row["graph_nodes"] if row else 0),
        "graph_edges": int(row["graph_edges"] if row else 0),
    }


def build_rag_visual_workbench(
    *,
    question: str,
    product: str = "POY",
    limit: int = 8,
    as_of_time: str | None = None,
) -> dict[str, object]:
    full_chain = build_full_chain_summary(as_of_time=as_of_time)
    canonical_as_of_time = str(full_chain["as_of_time"])
    data_snapshot_id = str(full_chain["data_snapshot_id"])
    semantic_status = semantic_index_status()
    active_index = semantic_status.get("active_index") or {}
    index_status = {
        "status": semantic_status.get("status", "missing"),
        "document_count": active_index.get("document_count", 0),
        "chunk_count": active_index.get("chunk_count", 0),
        "created_at": active_index.get("created_at", ""),
        "index_id": active_index.get("index_id", ""),
        "index_version": active_index.get("index_version", ""),
        "embedding_model": semantic_status.get("embedding_model", ""),
        "embedding_model_version": semantic_status.get("embedding_model_version", ""),
        "embedding_mode": active_index.get("embedding_mode", ""),
        "stale_reason": semantic_status.get("stale_reason", ""),
        "last_error": semantic_status.get("last_error", ""),
    }
    now = datetime.now(UTC).isoformat()
    business_types = {
        "knowledge_node",
        "knowledge_edge",
        "market_observation",
        "authorized_spot_observation",
        "industry_observation",
        "news_article",
        "news_event_cluster",
        "event_observation",
        "political_case_memory",
        "event_intelligence_snapshot",
    }
    chunk_search = retrieve_chunks(
        question,
        limit=max(80, limit * 8),
        as_of_time=canonical_as_of_time,
        persist_run=False,
        allowed_doc_types=business_types,
    )
    documents: list[dict[str, object]] = []
    seen_documents: set[str] = set()
    seen_titles: set[str] = set()
    chunk_items = list(chunk_search.get("items", []))
    knowledge_items = [item for item in chunk_items if item.get("source_kind") in {"knowledge_node", "knowledge_edge"}]
    if not knowledge_items:
        background = retrieve_chunks(
            question, limit=3, as_of_time=canonical_as_of_time, persist_run=False,
            allowed_doc_types={"knowledge_node", "knowledge_edge"},
        )
        knowledge_items = list(background.get("items", []))
    # Reserve a background relation slot; news volume must not erase the chain
    # context. These documents are not counted as current directional proof.
    chunk_items = knowledge_items[:1] + chunk_items
    requested_price_products = _rag_visual_requested_price_products(question)
    if requested_price_products:
        with closing(connect()) as connection:
            for requested_product in requested_price_products:
                row = connection.execute(
                    """
                    SELECT document_id, source_kind, source_id, title, summary, body,
                           observed_at, visible_at, evidence_level, url
                    FROM rag_documents d
                    LEFT JOIN rag_evidence_reviews r ON r.doc_id = d.document_id
                    WHERE can_use_pre_forecast = 1
                      AND source_kind = 'authorized_spot_observation'
                      AND upper(title) LIKE ?
                    ORDER BY
                      CASE WHEN r.status = 'reviewed' AND r.result = 'approved' THEN 1 ELSE 0 END DESC,
                      d.observed_at DESC,
                      COALESCE(NULLIF(d.visible_at, ''), d.updated_at) DESC
                    LIMIT 1
                    """,
                    (f"%{requested_product}%",),
                ).fetchone()
                if row is None:
                    continue
                document_id = str(row["document_id"])
                normalized_title = " ".join(str(row["title"] or "").split()).casefold()
                if document_id in seen_documents or normalized_title in seen_titles:
                    continue
                seen_documents.add(document_id)
                seen_titles.add(normalized_title)
                documents.append(
                    {
                        "doc_id": document_id,
                        "doc_type": str(row["source_kind"]),
                        "source_id": str(row["source_id"]),
                        "title": str(row["title"]),
                        "summary": str(row["summary"] or row["body"] or ""),
                        "snippet": str(row["summary"] or row["body"] or ""),
                        "observed_at": str(row["visible_at"] or row["observed_at"] or ""),
                        "tier": str(row["evidence_level"] or "D"),
                        "url": str(row["url"] or ""),
                        "risk_flags": [],
                        "rerank_score": 0.0,
                        "rerank_reasons": {"coverage_policy": "explicit_requested_authorized_price_product"},
                    }
                )

        def price_priority(item: dict[str, object]) -> tuple[int, int]:
            title = str(item.get("title") or "").upper()
            source_kind = str(item.get("source_kind") or "")
            for index, requested_product in enumerate(requested_price_products):
                if source_kind == "authorized_spot_observation" and requested_product in title:
                    return (0, index)
            return (1 if source_kind == "authorized_spot_observation" else 2, 0)

        chunk_items.sort(key=price_priority)
        if knowledge_items:
            chunk_items = knowledge_items[:1] + [item for item in chunk_items if item not in knowledge_items[:1]]
    effective_limit = min(limit, 8)
    for item in chunk_items:
        document_id = str(item.get("document_id") or "")
        doc_type = str(item.get("source_kind") or "")
        normalized_title = " ".join(str(item.get("title") or "").split()).casefold()
        if (
            not document_id
            or document_id in seen_documents
            or (normalized_title and normalized_title in seen_titles)
            or doc_type not in business_types
        ):
            continue
        seen_documents.add(document_id)
        if normalized_title:
            seen_titles.add(normalized_title)
        documents.append(
            {
                "doc_id": document_id,
                "doc_type": doc_type,
                "source_id": str(item.get("source_id") or ""),
                "title": str(item.get("title") or ""),
                "summary": str(item.get("text") or ""),
                "snippet": str(item.get("text") or ""),
                "observed_at": str(item.get("visible_at") or item.get("observed_at") or ""),
                "tier": str(item.get("evidence_level") or "D"),
                "url": str(item.get("url") or ""),
                "risk_flags": list(item.get("risk_flags") or []),
                "rerank_score": float(item.get("rerank_score") or 0),
                "rerank_reasons": dict(item.get("rerank_reasons") or {}),
            }
        )
        if len(documents) >= effective_limit:
            break
    # The customer evidence bundle must use the same immutable price snapshot
    # as full-chain and market-chain.  Search results can legitimately contain
    # older observations, but they must not replace the canonical observation
    # for the current POY/DTY judgement snapshot.
    canonical_price_documents: list[dict[str, object]] = []
    for row in full_chain.get("summary", []):
        if not isinstance(row, dict) or str(row.get("product") or "") not in {"POY", "DTY"}:
            continue
        product_name = str(row["product"])
        observed_at = str(row.get("observed_at") or "")
        value = row.get("value")
        unit = str(row.get("unit") or "")
        quote_label = QUOTE_TYPE_LABELS.get(
            str(row.get("quote_type") or ""), QUOTE_TYPE_LABELS["public_recent_average"]
        )
        canonical_price_documents.append(
            {
                "doc_id": f"canonical:{data_snapshot_id}:{product_name}",
                "doc_type": "authorized_spot_observation",
                "source_id": str(row.get("source_id") or ""),
                "title": f"{product_name} {quote_label}",
                "summary": f"{product_name} {quote_label} {observed_at[:10]}：{value} {unit}",
                "snippet": f"{product_name} {quote_label} {observed_at[:10]}：{value} {unit}",
                "observed_at": observed_at,
                "tier": str(row.get("evidence_tier") or "D"),
                "url": str(row.get("evidence_url") or ""),
                "risk_flags": [],
                "rerank_score": 1.0,
                "rerank_reasons": {"term_coverage": 1.0, "vector_score": 1.0, "canonical_snapshot": True},
            }
        )
    canonical_products = {str(row.get("title") or "").split(" ", 1)[0] for row in canonical_price_documents}
    documents = canonical_price_documents + [
        item
        for item in documents
        if not (
            str(item.get("doc_type") or "") == "authorized_spot_observation"
            and str(item.get("title") or "").split(" ", 1)[0] in canonical_products
        )
    ]
    documents = documents[:limit]
    coverage = dict(Counter(str(item.get("doc_type") or "") for item in documents))
    source_counts = dict(coverage)
    returned_count = len(documents)
    retrieval_metadata = dict(chunk_search.get("metadata") or {})
    candidate_count = int(retrieval_metadata.get("candidate_count") or 0)
    reviewed_count = returned_count

    if returned_count == 0:
        return {
            "generated_at": now,
            "as_of_time": canonical_as_of_time,
            "data_snapshot_id": data_snapshot_id,
            "question": question,
            "product": product,
            "caption": "证据检索与证据链可视化",
            "capability_note": "真实检索证据暂未返回；页面不会用流程示意代替本轮记录。",
            "index_status": {
                **index_status,
                "built_at": str(index_status.get("created_at") or ""),
                "retrieval_status": str(chunk_search.get("status") or ""),
                "retrieval_mode": str(retrieval_metadata.get("retrieval_mode") or ""),
                "fallback": "fallback" in str(retrieval_metadata.get("retrieval_mode") or ""),
            },
            "summary": {
                "candidate_count": candidate_count,
                "reviewed_count": 0,
                "entered_count": 0,
                "evidence_level": "D",
                "confidence_label": "数据未就绪",
                "confidence": 0,
                "graph_nodes": 0,
                "graph_edges": 0,
                "reviewed_evidence": 0,
            },
            "retrieval_path": [],
            "graph": {"nodes": [], "edges": []},
            "selected_node_id": "",
            "node_details": {},
            "evidence_buckets": {"adopted": [], "excluded": [], "conflicts": []},
            "empty_states": {
                "excluded": "暂无本轮检索结果。",
                "conflicts": "暂无本轮检索结果。",
            },
            "warnings": ["当前 RAG 索引未返回可用于本轮判断的业务证据。"],
            "formal_conclusion_gate": {
                "qualified": False,
                "reasons": ["rag_adopted_evidence_required"],
                "required_snapshot_id": data_snapshot_id,
                "adopted_evidence_ids": [],
                "evidence_mapping": {},
                "direction_derivation": {
                    "status": "insufficient_evidence",
                    "direction": "",
                    "method": "reviewed_directional_evidence_required",
                    "trace": [],
                },
            },
            "coverage_confidence": None,
            "retrieval_confidence": 0.0,
            "conclusion_confidence": 0.0,
            "confidence_semantics": {
                "coverage_confidence": "由full-chain接口提供的数据覆盖置信度。",
                "retrieval_confidence": "本轮查询与返回证据的检索相关性。",
                "conclusion_confidence": "仅在正式门禁通过后才可大于0。",
            },
        }

    boundary_count = 1
    knowledge_count = coverage.get("knowledge_node", 0) + coverage.get("knowledge_edge", 0)
    price_count = (
        coverage.get("market_observation", 0)
        + coverage.get("authorized_spot_observation", 0)
        + coverage.get("industry_observation", 0)
    )
    event_count = (
        coverage.get("news_article", 0) + coverage.get("news_event_cluster", 0) + coverage.get("event_observation", 0)
    )
    history_count = (
        coverage.get("prediction_record", 0)
        + coverage.get("event_intelligence_snapshot", 0)
        + coverage.get("political_case_memory", 0)
    )

    tier_rank = {"A": 4, "B": 3, "C": 2, "D": 1}
    confidence_documents = [
        item
        for item in documents[:8]
        if str(item.get("tier") or "") in {"A", "B"}
        and item.get("doc_type") not in {"knowledge_node", "knowledge_edge"}
        and _rag_visual_intent_eligible(item, question=question)
    ]
    evidence_level = min(
        (str(item.get("tier") or "D") for item in confidence_documents),
        key=lambda tier: tier_rank.get(tier, 1),
        default="D",
    )
    relevance_values = []
    for item in confidence_documents:
        reasons = dict(item.get("rerank_reasons") or {})
        term_coverage = float(reasons.get("term_coverage") or 0)
        vector_score = max(0.0, float(reasons.get("vector_score") or 0))
        tier_component = tier_rank.get(str(item.get("tier") or "D"), 1) / 4
        relevance_values.append(min(1.0, term_coverage * 0.55 + vector_score * 0.25 + tier_component * 0.2))
    retrieval_confidence = round(sum(relevance_values) / len(relevance_values), 3) if relevance_values else 0.0
    confidence_label = (
        "较高" if retrieval_confidence >= 0.7 else "需复核" if retrieval_confidence >= 0.4 else "相关性不足"
    )

    conflict_candidates = [
        _rag_visual_evidence_item(item, fallback_status="存在冲突")
        for item in documents
        if str(item.get("status") or "").lower() == "conflict"
        or any(marker in str(flag) for flag in item.get("risk_flags", []) for marker in ("冲突", "矛盾", "反证"))
    ][:4]
    rejected_items = [
        _rag_visual_evidence_item(item, fallback_status="已排除")
        for item in documents
        if str(item.get("tier") or "") in {"C", "D"} or not _rag_visual_intent_eligible(item, question=question)
    ][:4]

    adopted_items = [
        _rag_visual_evidence_item(
            item,
            fallback_status="背景知识" if item.get("doc_type") in {"knowledge_node", "knowledge_edge"} else "已采用",
        )
        for item in documents[:8]
        if str(item.get("tier") or "") in {"A", "B"}
        and _rag_visual_intent_eligible(item, question=question)
    ]
    adopted_business_documents = [
        item
        for item in documents[:8]
        if str(item.get("tier") or "") in {"A", "B"}
        and str(item.get("doc_type") or "")
        in {"market_observation", "authorized_spot_observation", "industry_observation"}
        and _rag_visual_intent_eligible(item, question=question)
    ]
    adopted_business_ids = [str(item.get("doc_id") or "") for item in adopted_business_documents if item.get("doc_id")]
    as_of_date = canonical_as_of_time[:10]
    formal_rows = _formal_rows_from_snapshot(
        {
            "payload": {
                "industry_observations": list_industry_observations(
                    end=as_of_date, as_of_time=canonical_as_of_time, limit=1000
                ),
                "authorized_price_observations": list_forecast_price_points(
                    dataset_type="ccf_spot", end=as_of_date, as_of_time=canonical_as_of_time, limit=5000
                ),
            }
        }
    )
    # Only an explicit persisted review of evidence adopted by this retrieval
    # may satisfy the review gate. Retrieval/entered counts are never reviews.
    formal_review_map = get_evidence_review_map(row["doc_id"] for row in formal_rows)
    latest_date_by_product: dict[str, str] = {}
    for row in formal_rows:
        product_key = str(row.get("product") or "").upper()
        observed_date = str(row.get("observed_at") or "")[:10]
        latest_date_by_product[product_key] = max(latest_date_by_product.get(product_key, ""), observed_date)
    adopted_id_set = set(adopted_business_ids)
    # Canonical POY/DTY cards deliberately use snapshot-scoped synthetic ids so
    # the UI cannot accidentally display an older search hit.  The persisted
    # review, however, is attached to the underlying ccf_spot document id.  Map
    # those canonical cards back to the latest real row for the same product;
    # otherwise an approved latest observation can never pass the formal gate.
    canonical_adopted_products = {
        str(item.get("title") or "").split(" ", 1)[0].upper()
        for item in adopted_business_documents
        if str(item.get("doc_id") or "").startswith("canonical:")
        and str(item.get("doc_type") or "") == "authorized_spot_observation"
    }
    adopted_review_map = {
        str(row["doc_id"]): formal_review_map[str(row["doc_id"])]
        for row in formal_rows
        if str(row["doc_id"]) in formal_review_map
        and (
            str(row.get("observed_at") or "")[:10]
            < latest_date_by_product.get(str(row.get("product") or "").upper(), "")
            or str(row["doc_id"]) in adopted_id_set
            or (
                str(row.get("product") or "").upper() in canonical_adopted_products
                and str(row.get("observed_at") or "")[:10]
                == latest_date_by_product.get(str(row.get("product") or "").upper(), "")
            )
        )
    }
    direction_gate = derive_formal_direction(
        rows=formal_rows,
        required_products=("POY", "DTY"),
        review_map=adopted_review_map,
        required_snapshot_id=data_snapshot_id,
        required_evidence_roles=("upstream_cost_driver", "transmission_path", "downstream_transmission"),
    )
    review_count = len(direction_gate["reviewed_evidence_ids"])
    gate_reasons: list[str] = []
    if full_chain.get("status") != "ready":
        gate_reasons.append("full_chain_not_ready")
    poy_dty_gate = full_chain.get("poy_dty_gate") if isinstance(full_chain.get("poy_dty_gate"), dict) else {}
    if not poy_dty_gate.get("qualified"):
        gate_reasons.append("poy_dty_gate_not_qualified")
    if not adopted_business_ids:
        gate_reasons.append("rag_adopted_business_evidence_required")
    gate_reasons.extend(str(reason) for reason in direction_gate["reasons"])
    if confidence_label == "需复核" or retrieval_confidence < 0.7:
        gate_reasons.append("retrieval_confidence_below_formal_threshold")
    direction_derivation = dict(direction_gate["direction_derivation"])
    evidence_mapping = dict(direction_gate["evidence_mapping"])
    entered_count = len(adopted_items)
    retrieval_path = [
        {
            "id": "question",
            "node_ids": ["question"],
            "step": "明确业务问题",
            "status": "已完成",
            "tone": "success",
            "count_label": "1 项",
            "description": "限定 POY/DTY 上游原料、短周期变化与成本压力判断边界。",
        },
        {
            "id": "boundary",
            "node_ids": ["boundary"],
            "step": "检索判断边界",
            "status": "已完成",
            "tone": "success",
            "count_label": "1 项",
            "description": "读取价格口径和客户可用边界。",
        },
        {
            "id": "knowledge",
            "node_ids": ["chain"],
            "step": "检索产业链知识",
            "status": "已完成" if knowledge_count else "待补",
            "tone": "success" if knowledge_count else "warning",
            "count_label": f"{knowledge_count} 条",
            "description": "整理原油、石脑油、PX、PTA、MEG 到 POY/DTY 的传导关系。",
        },
        {
            "id": "price",
            "node_ids": ["price", "industry"],
            "step": "检索价格与行业观测",
            "status": "已完成" if price_count else "待补",
            "tone": "success" if price_count else "warning",
            "count_label": f"{price_count} 条",
            "description": "寻找价格、库存、开工、利润等可支撑判断的观测。",
        },
        {
            "id": "event",
            "node_ids": ["event"],
            "step": "检索新闻事件",
            "status": "已完成" if event_count else "待补",
            "tone": "success" if event_count else "warning",
            "count_label": f"{event_count} 条",
            "description": "识别公告、宏观、供应扰动与需求变化。",
        },
        {
            "id": "history",
            "node_ids": ["history"],
            "step": "检索历史复盘",
            "status": "已完成" if history_count else "待补",
            "tone": "success" if history_count else "muted",
            "count_label": f"{history_count} 条",
            "description": "对照历史判断、复盘记录和相似场景。",
        },
        {
            "id": "review",
            "node_ids": ["counter"],
            "step": "证据复核",
            "status": "已完成" if reviewed_count else "待补",
            "tone": "success" if reviewed_count else "warning",
            "count_label": f"{reviewed_count} 条",
            "description": "从候选材料中筛出与本次问题相关的证据。",
        },
        {
            "id": "bundle",
            "node_ids": ["conclusion"],
            "step": "形成证据包",
            "status": "已完成" if entered_count else "待补",
            "tone": "success" if entered_count else "warning",
            "count_label": f"{entered_count} 条",
            "description": "进入页面图谱与底部证据清单。",
        },
    ]

    graph_layers = {"产品链": source_counts.get("knowledge_node", 0) + source_counts.get("knowledge_edge", 0)}
    visual_nodes = [
        {
            "id": "question",
            "label": "业务问题",
            "subtitle": "1 项业务问题",
            "status": "已采用",
            "tone": "info",
            "kind": "question",
            "metric": "1 项",
            "summary": "本页围绕当前证据是否支持 POY/DTY 上游成本压力判断展开。",
            "x": 330,
            "y": 42,
        },
        {
            "id": "boundary",
            "label": "判断边界",
            "subtitle": "1 项边界",
            "status": "已采用",
            "tone": "success",
            "kind": "boundary",
            "metric": "1 项",
            "summary": "明确价格口径、证据等级和客户使用边界。",
            "x": 560,
            "y": 42,
        },
        {
            "id": "chain",
            "label": "产业链结构",
            "subtitle": f"{knowledge_count or graph_layers.get('产品链', 0)} 条证据",
            "status": "已采用" if knowledge_count else "待补",
            "tone": "success" if knowledge_count else "warning",
            "kind": "evidence",
            "metric": f"{knowledge_count or graph_layers.get('产品链', 0)} 条",
            "summary": "解释上游原料到 POY/DTY 的成本传导路径。",
            "x": 100,
            "y": 145,
        },
        {
            "id": "price",
            "label": "价格证据",
            "subtitle": f"{price_count} 条证据",
            "status": "已采用" if price_count else "待补",
            "tone": "success" if price_count else "warning",
            "kind": "evidence",
            "metric": f"{price_count} 条",
            "summary": "价格和行业观测用于判断成本压力是否传导。",
            "x": 330,
            "y": 145,
        },
        {
            "id": "industry",
            "label": "行业指标",
            "subtitle": f"{coverage.get('industry_observation', 0)} 条证据",
            "status": "已采用" if coverage.get("industry_observation", 0) else "待补",
            "tone": "success" if coverage.get("industry_observation", 0) else "warning",
            "kind": "evidence",
            "metric": f"{coverage.get('industry_observation', 0)} 条",
            "summary": "库存、开工、利润等指标用于校验价格传导是否成立。",
            "x": 610,
            "y": 145,
        },
        {
            "id": "event",
            "label": "新闻事件",
            "subtitle": f"{event_count} 条证据",
            "status": "已采用" if event_count else "待补",
            "tone": "success" if event_count else "warning",
            "kind": "evidence",
            "metric": f"{event_count} 条",
            "summary": "新闻、公告和事件簇用于判断外部冲击是否改变研判。",
            "x": 175,
            "y": 275,
        },
        {
            "id": "history",
            "label": "历史复盘",
            "subtitle": f"{history_count} 条证据",
            "status": "已采用" if history_count else "待补",
            "tone": "info" if history_count else "muted",
            "kind": "review",
            "metric": f"{history_count} 条",
            "summary": "历史判断和复盘用于检查相似情形下的误判来源。",
            "x": 430,
            "y": 275,
        },
        {
            "id": "counter",
            "label": "反证检查",
            "subtitle": f"{len(conflict_candidates)} 项提醒",
            "status": "存在冲突" if conflict_candidates else "已检查",
            "tone": "warning" if conflict_candidates else "success",
            "kind": "counter",
            "metric": f"{len(conflict_candidates)} 项",
            "summary": "检查需求抵消、供应恢复、宏观反向和证据过期等风险。",
            "x": 700,
            "y": 275,
        },
        {
            "id": "conclusion",
            "label": "今日结论",
            "subtitle": f"{entered_count} 条证据",
            "status": "已采用" if entered_count else "需复核",
            "tone": "success" if entered_count >= 6 else "warning",
            "kind": "conclusion",
            "metric": f"{entered_count} 条",
            "summary": "综合证据后形成今日研判辅助结论和下一步关注项。",
            "x": 382,
            "y": 405,
        },
    ]
    visual_edges = [
        {"id": "question-boundary", "source": "question", "target": "boundary", "label": "限定边界", "tone": "support"},
        {"id": "question-chain", "source": "question", "target": "chain", "label": "定位链路", "tone": "support"},
        {"id": "boundary-price", "source": "boundary", "target": "price", "label": "约束口径", "tone": "citation"},
        {
            "id": "boundary-industry",
            "source": "boundary",
            "target": "industry",
            "label": "约束口径",
            "tone": "citation",
        },
        {"id": "chain-price", "source": "chain", "target": "price", "label": "成本传导", "tone": "support"},
        {"id": "price-industry", "source": "price", "target": "industry", "label": "相互校验", "tone": "support"},
        {"id": "price-event", "source": "price", "target": "event", "label": "触发解释", "tone": "citation"},
        {"id": "price-history", "source": "price", "target": "history", "label": "历史对照", "tone": "support"},
        {"id": "industry-counter", "source": "industry", "target": "counter", "label": "反证约束", "tone": "counter"},
        {"id": "event-counter", "source": "event", "target": "counter", "label": "反证检查", "tone": "counter"},
        {
            "id": "history-conclusion",
            "source": "history",
            "target": "conclusion",
            "label": "复盘支持",
            "tone": "citation",
        },
        {"id": "price-conclusion", "source": "price", "target": "conclusion", "label": "支持判断", "tone": "support"},
        {
            "id": "counter-conclusion",
            "source": "counter",
            "target": "conclusion",
            "label": "影响置信边界",
            "tone": "counter",
        },
    ]

    node_details = {
        "question": {
            "title": "业务问题",
            "status": "已采用",
            "tone": "info",
            "what": "本次图谱围绕当前证据是否支持 POY/DTY 上游成本压力判断展开。",
            "why": "先固定问题边界，避免把泛化行情评论误当成业务建议。",
            "supports": ["限定产品范围", "限定研判周期", "限定证据使用边界"],
            "risks": ["问题范围变化时需要重新生成证据链。"],
            "next_step": "继续检索判断边界、产业链知识和价格事件证据。",
            "sources": [],
        },
        "boundary": {
            "title": "判断边界",
            "status": "已采用" if boundary_count else "待补",
            "tone": "success" if boundary_count else "warning",
            "what": "价格口径、客户可用边界和数据质量要求。",
            "why": "这些边界用于约束页面结论的可用范围，防止误宣传或超出业务边界。",
            "supports": ["客户可读口径", "价格使用边界", "证据等级约束"],
            "risks": ["如果业务边界变化，需要重新生成图谱。"],
            "next_step": "结合价格、事件和行业指标继续校验。",
            "sources": [],
        },
        "chain": {
            "title": "产业链结构",
            "status": "已采用" if knowledge_count else "待补",
            "tone": "success" if knowledge_count else "warning",
            "what": "原油、石脑油、PX、PTA、MEG 到 POY/DTY 的成本传导关系。",
            "why": "把单点价格变化放回产业链，判断是否能传导到目标产品。",
            "supports": ["成本传导", "上下游影响路径", "产品关系解释"],
            "risks": ["传导链上的缺口会降低结论置信度。"],
            "next_step": "结合价格证据和行业指标继续校验。",
            "sources": _rag_visual_first_documents_by_type(documents, {"knowledge_node", "knowledge_edge"}, 3),
        },
        "price": {
            "title": "价格证据",
            "status": "已采用" if price_count else "待补",
            "tone": "success" if price_count else "warning",
            "what": "市场价格、行业价格和上游原料观测。",
            "why": "价格证据用于判断成本压力方向是否真实存在。",
            "supports": ["价格方向", "成本压力", "短周期判断"],
            "risks": ["价格序列不足时不应单独形成行动建议。"],
            "next_step": "继续对照库存、开工和利润变化。",
            "sources": _rag_visual_first_documents_by_type(
                documents, {"market_observation", "authorized_spot_observation", "industry_observation"}, 8
            ),
        },
        "industry": {
            "title": "行业指标",
            "status": "已采用" if coverage.get("industry_observation", 0) else "待补",
            "tone": "success" if coverage.get("industry_observation", 0) else "warning",
            "what": "库存、开工、利润和聚酯供需指标。",
            "why": "行业指标用于判断价格上涨是否被需求或库存抵消。",
            "supports": ["供需校验", "利润约束", "库存变化"],
            "risks": ["指标滞后时，只能作为解释信号。"],
            "next_step": "持续跟踪行业指标更新频率。",
            "sources": _rag_visual_first_documents_by_type(documents, {"industry_observation"}, 4),
        },
        "event": {
            "title": "新闻事件",
            "status": "已采用" if event_count else "待补",
            "tone": "success" if event_count else "warning",
            "what": "新闻、公告、宏观与供应扰动。",
            "why": "事件证据用于解释价格变化是否有外部触发因素。",
            "supports": ["事件触发", "供应扰动", "宏观影响"],
            "risks": ["单条新闻不能直接支撑高置信结论。"],
            "next_step": "保留来源链接并进行交叉验证。",
            "sources": _rag_visual_first_documents_by_type(
                documents, {"news_article", "news_event_cluster", "event_observation"}, 4
            ),
        },
        "history": {
            "title": "历史复盘",
            "status": "已采用" if history_count else "待补",
            "tone": "info" if history_count else "muted",
            "what": "历史判断、事件快照和复盘材料。",
            "why": "对照相似情景，识别过去误判来源。",
            "supports": ["相似案例", "误判归因", "历史边界"],
            "risks": ["历史样本不足时不能作为主要行动依据。"],
            "next_step": "后续补齐更多复盘样本。",
            "sources": _rag_visual_first_documents_by_type(
                documents, {"prediction_record", "event_intelligence_snapshot", "political_case_memory"}, 3
            ),
        },
        "counter": {
            "title": "反证检查",
            "status": "存在冲突" if conflict_candidates else "已检查",
            "tone": "warning" if conflict_candidates else "success",
            "what": "需求抵消、供应恢复、宏观反向、证据过期和低等级证据。",
            "why": "反证用于防止把单侧证据误判为确定结论。",
            "supports": ["置信边界", "风险提示", "可审计复核"],
            "risks": [item["title"] for item in conflict_candidates[:3]],
            "next_step": "继续检查新增反证与冲突证据。",
            "sources": conflict_candidates[:3],
        },
        "conclusion": {
            "title": "今日结论",
            "status": "已采用" if entered_count else "需复核",
            "tone": "success" if entered_count >= 6 else "warning",
            "what": "由本次证据检索和图谱关系整理出的业务研判辅助结论。",
            "why": "结论必须同时受到价格、事件、供需和反证检查约束。",
            "supports": [f"纳入证据包 {entered_count} 条", "证据来自本地 RAG 索引"],
            "risks": ["仍需持续跟踪新增事件和价格变化。"],
            "next_step": "关注新增新闻事件、价格链变化和质量门禁提示。",
            "sources": adopted_items[:4],
        },
    }

    # The graph is a view of this retrieval snapshot.  Do not render empty
    # process-template nodes as though they were retrieved evidence.
    active_evidence_nodes: list[str] = []
    if knowledge_count:
        active_evidence_nodes.append("chain")
    if price_count:
        active_evidence_nodes.append("price")
    if coverage.get("industry_observation", 0):
        active_evidence_nodes.append("industry")
    if event_count:
        active_evidence_nodes.append("event")
    if history_count:
        active_evidence_nodes.append("history")
    if conflict_candidates:
        active_evidence_nodes.append("counter")
    active_node_ids = {"question", "conclusion", *active_evidence_nodes}
    visual_nodes = [node for node in visual_nodes if str(node.get("id")) in active_node_ids]
    for node in visual_nodes:
        if node.get("id") == "conclusion":
            node.update(
                {
                    "label": "本次证据包",
                    "summary": "汇总本次问题实际检索并通过可见性门禁的业务证据。",
                    "status": "已形成" if entered_count else "待补",
                }
            )
    visual_edges = []
    for node_id in active_evidence_nodes:
        visual_edges.append(
            {
                "id": f"question-{node_id}",
                "source": "question",
                "target": node_id,
                "label": "检索命中",
                "tone": "citation",
            }
        )
        visual_edges.append(
            {
                "id": f"{node_id}-conclusion",
                "source": node_id,
                "target": "conclusion",
                "label": "纳入证据包",
                "tone": "support",
            }
        )
    if not active_evidence_nodes:
        visual_edges.append(
            {
                "id": "question-conclusion",
                "source": "question",
                "target": "conclusion",
                "label": "未命中业务证据",
                "tone": "counter",
            }
        )
    node_details = {key: value for key, value in node_details.items() if key in active_node_ids}
    if "conclusion" in node_details:
        node_details["conclusion"]["title"] = "本次证据包"
        node_details["conclusion"]["what"] = "本次问题实际检索并通过可见性门禁的业务证据集合。"

    return {
        "generated_at": now,
        "as_of_time": canonical_as_of_time,
        "data_snapshot_id": data_snapshot_id,
        "question": question,
        "product": product,
        "caption": "证据检索与证据链可视化",
        "capability_note": "当前图谱由本次检索到的证据、政治事件复盘和项目知识关系整理生成。",
        "index_status": {
            **index_status,
            "built_at": str(index_status.get("created_at") or ""),
            "retrieval_status": str(chunk_search.get("status") or ""),
            "retrieval_mode": str(retrieval_metadata.get("retrieval_mode") or ""),
            "fallback": "fallback" in str(retrieval_metadata.get("retrieval_mode") or ""),
        },
        "summary": {
            "candidate_count": candidate_count,
            "reviewed_count": reviewed_count,
            "entered_count": entered_count,
            "evidence_level": evidence_level,
            "confidence_label": confidence_label,
            "confidence": retrieval_confidence,
            "graph_nodes": len(visual_nodes),
            "graph_edges": len(visual_edges),
            "reviewed_evidence": review_count,
        },
        "retrieval_path": retrieval_path,
        "graph": {"nodes": visual_nodes, "edges": visual_edges},
        "selected_node_id": "conclusion",
        "node_details": node_details,
        "evidence_buckets": {
            "adopted": adopted_items,
            "excluded": rejected_items,
            "conflicts": conflict_candidates,
        },
        "empty_states": {
            "excluded": "暂无被排除证据，系统会在合规复核或质量门禁发现问题后更新。",
            "conflicts": "暂无显著冲突项，仍需持续跟踪新事件和价格变化。",
        },
        "warnings": [] if entered_count else ["本轮没有 A/B 级业务证据进入证据包。"],
        "formal_conclusion_gate": {
            "qualified": not gate_reasons,
            "reasons": list(dict.fromkeys(gate_reasons)),
            "required_snapshot_id": data_snapshot_id,
            "adopted_evidence_ids": list(direction_gate["reviewed_evidence_ids"]),
            "evidence_mapping": evidence_mapping,
            "direction_derivation": direction_derivation,
            "review_audit": list(direction_gate["review_audit"]),
        },
        "coverage_confidence": None,
        "retrieval_confidence": retrieval_confidence,
        "conclusion_confidence": retrieval_confidence if not gate_reasons else 0.0,
        "confidence_semantics": {
            "coverage_confidence": "由full-chain接口提供的数据覆盖置信度。",
            "retrieval_confidence": "本轮查询与返回证据的检索相关性，不等同于结论置信度。",
            "conclusion_confidence": "只有方向推导可验证、证据已复核且正式门禁通过后才可大于0。",
        },
    }

from __future__ import annotations

from collections import Counter
from contextlib import closing
from time import perf_counter
from typing import Any

from .foundation_utils import estimate_tokens, json_dumps, json_loads, new_id, now_iso, safe_summary
from .graph_memory_context import build_graph_memory_contribution
from .graphrag_store import materialize_graph_snapshot
from .models import RagEvidence, RagSearchResponse
from .prompt_registry import get_prompt, list_few_shots, seed_prompt_registry
from .rag import build_rag_context
from .source_registry import get_source
from .storage import (
    connect,
    latest_intraday_price_observations,
    list_futures_daily_bars,
    list_market_observations,
)
from .unified_retriever import retrieve_chunks

CONTEXT_PACK_VERSION = "context-pack-v1"
ASSISTANT_ALLOWED_DOC_TYPES = {
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


def _retrieval_response(
    question: str,
    result: dict[str, Any],
    *,
    as_of_time: str | None,
) -> RagSearchResponse:
    documents: list[RagEvidence] = []
    seen: set[str] = set()
    for item in result.get("items", []):
        doc_id = str(item.get("document_id") or "")
        if not doc_id or doc_id in seen:
            continue
        seen.add(doc_id)
        document_metadata = item.get("document_metadata")
        if not isinstance(document_metadata, dict):
            document_metadata = {}
        nested_metadata = document_metadata.get("metadata")
        if not isinstance(nested_metadata, dict):
            nested_metadata = document_metadata
        review_status = str(item.get("review_status") or document_metadata.get("review_status") or "unreviewed")
        if review_status not in {"unreviewed", "reviewed", "rejected"}:
            review_status = "unreviewed"
        risk_flags = list(nested_metadata.get("risk_flags") or [])
        if item.get("stale"):
            risk_flags.append("stale_index")
        documents.append(
            RagEvidence(
                doc_id=doc_id,
                doc_type=str(item.get("source_kind") or "indexed_document"),
                source_id=str(item.get("source_id") or ""),
                tier=str(item.get("evidence_level") or "D"),
                title=str(item.get("title") or ""),
                summary=str(document_metadata.get("evidence_text") or item.get("text") or "")[:2000],
                snippet=str(document_metadata.get("evidence_text") or item.get("text") or "")[:600],
                url=str(item.get("url") or ""),
                observed_at=str(item.get("observed_at") or ""),
                visible_at=str(item.get("visible_at") or ""),
                score=float(item.get("rerank_score") or 0),
                risk_flags=sorted(set(risk_flags)),
                review_status=review_status,
                metadata={
                    **nested_metadata,
                    "chunk_id": item.get("chunk_id", ""),
                    "lexical_score": item.get("lexical_score", 0),
                    "vector_score": item.get("vector_score", 0),
                    "rerank_score": item.get("rerank_score", 0),
                    "embedding_model": item.get("embedding_model", ""),
                    "embedding_model_version": item.get("embedding_model_version", ""),
                    "retrieval_mode": item.get("retrieval_mode", ""),
                    "index_version": item.get("index_version", ""),
                },
            )
        )
    tier_rank = {"A": 4, "B": 3, "C": 2, "D": 1}
    evidence_level = max((item.tier for item in documents), key=lambda tier: tier_rank[tier]) if documents else "D"
    confidence = 0.0
    if documents:
        confidence = min(
            1.0,
            max(0.0, sum(max(item.score, 0) for item in documents) / len(documents)),
        )
    metadata = dict(result.get("metadata") or {})
    metadata["status"] = result.get("status", "")
    return RagSearchResponse(
        query=question,
        documents=documents,
        evidence_level=evidence_level,
        confidence=round(confidence, 4),
        as_of_time=as_of_time,
        coverage=dict(Counter(item.doc_type for item in documents)),
        warnings=list(result.get("warnings") or []),
        retrieval_metadata=metadata,
    )


def _merge_retrieval_results(
    base: dict[str, Any],
    expanded: dict[str, Any],
    *,
    limit: int = 20,
) -> dict[str, Any]:
    by_chunk: dict[str, dict[str, Any]] = {}
    for item in [*base.get("items", []), *expanded.get("items", [])]:
        chunk_id = str(item.get("chunk_id") or "")
        if not chunk_id:
            continue
        current = by_chunk.get(chunk_id)
        if current is None or float(item.get("rerank_score") or 0) > float(current.get("rerank_score") or 0):
            by_chunk[chunk_id] = item
    items = sorted(
        by_chunk.values(),
        key=lambda item: (-float(item.get("rerank_score") or 0), str(item.get("chunk_id"))),
    )[:limit]
    metadata = dict(base.get("metadata") or {})
    metadata["graph_expansion_applied"] = True
    metadata["graph_expansion_returned_count"] = len(expanded.get("items", []))
    warnings = sorted(set([*base.get("warnings", []), *expanded.get("warnings", [])]))
    return {
        **base,
        "status": "ready" if items else base.get("status", "no_results"),
        "items": items,
        "count": len(items),
        "warnings": warnings,
        "metadata": metadata,
    }


_PRICE_INTENT_TERMS = ("价格", "最新", "报价", "多少钱", "行情", "当前", "今日", "涨", "跌", "price", "quote")
_PRICE_PRODUCT_TERMS: dict[str, tuple[str, ...]] = {
    "poy": ("poy", "涤纶poy"),
    "dty": ("dty", "涤纶dty"),
    "px": ("px", "对二甲苯"),
    "pta": ("pta", "精对苯二甲酸"),
    "meg": ("meg", "乙二醇"),
    "naphtha": ("naphtha", "石脑油"),
    "crude_oil": ("原油", "布伦特", "brent", "wti", "crude"),
}
_FUTURES_PRODUCTS = ("px", "pta")
# MEG/naphtha spot assessments live in the intraday quote table, not
# market_observations (which only carries their customs/macro rows).
_INTRADAY_INSTRUMENTS: dict[str, str] = {"meg": "MEG", "naphtha": "NAPHTHA", "poy": "POY", "dty": "DTY"}


def latest_price_evidence(question: str, *, as_of_time: str | None = None) -> list[RagEvidence]:
    """Latest same-basis curve observations for explicit price questions.

    The rows are real audited observations promoted ahead of news retrieval so
    a "latest price" answer cites the structured observation first; citation
    binding and quality gates treat them exactly like retrieved documents.
    """
    lowered = question.lower()
    if not any(term in lowered for term in _PRICE_INTENT_TERMS):
        return []
    products = [
        product
        for product, terms in _PRICE_PRODUCT_TERMS.items()
        if any(term in lowered for term in terms)
    ]
    if not products:
        return []
    documents: list[RagEvidence] = []
    covered_products: set[str] = set()
    for product in products[:4]:
        # Price-like units keep customs volumes and macro counters out of the
        # promoted row; a "latest price" question needs a price observation.
        for row in list_market_observations(
            product=product,
            end=as_of_time,
            units=("CNY/MT", "CNY/T", "USD/BBL", "USD/MT", "USD/GAL"),
            limit=1,
        ):
            source = get_source(str(row["source_id"]))
            value = "" if row["value"] is None else f"{row['value']} {row['unit']}"
            documents.append(
                RagEvidence(
                    doc_id=f"market:{row['observation_id']}",
                    doc_type="market_observation",
                    source_id=str(row["source_id"]),
                    tier=source.tier if source else "B",
                    title=f"{row['product']} {row['indicator']} {row['observed_at']}",
                    summary=(
                        f"{row['indicator']}={value}；地区 {row['region']}；"
                        f"频率 {row['frequency']}；备注 {row['notes']}"
                    ),
                    url=str(row["evidence_url"]),
                    observed_at=str(row["observed_at"]),
                    visible_at=str(row["created_at"] or row["observed_at"]),
                    metadata={"product": row["product"], "indicator": row["indicator"], "structured_price": True},
                )
            )
            covered_products.add(product)
        if product in _FUTURES_PRODUCTS:
            # Recent main contracts can carry contract_role='next_month' with
            # is_main=1 (the documented PX611 case), so select by is_main.
            bars = [
                bar
                for bar in list_futures_daily_bars(
                    source_id="czce_pta_px",
                    product=product.upper(),
                    end=as_of_time,
                    limit=10,
                )
                if bar.get("is_main")
            ][:1]
            for bar in bars:
                documents.append(
                    RagEvidence(
                        doc_id=f"futures:{bar['bar_id']}",
                        doc_type="futures_main_settlement",
                        source_id=str(bar["source_id"]),
                        tier="A",
                        title=f"{bar['product']} {bar['contract_code']} 主力结算 {bar['trade_date']}",
                        summary=(
                            f"{bar['product']}期货主力合约{bar['contract_code']}结算价={bar['settle']} {bar['unit']}；"
                            f"交易日 {bar['trade_date']}；来源 郑商所"
                        ),
                        url=str(bar.get("source_url") or ""),
                        observed_at=str(bar["trade_date"]),
                        visible_at=str(bar.get("visible_at") or bar["trade_date"]),
                        metadata={"product": bar["product"], "structured_price": True},
                    )
                )
            covered_products.add(product)
    # Products whose spot assessments live in the intraday quote table.
    intraday_products = [
        product for product in products if product in _INTRADAY_INSTRUMENTS and product not in covered_products
    ]
    if intraday_products:
        by_instrument = {
            str(row.get("instrument")): row
            for row in latest_intraday_price_observations(
                instruments=[_INTRADAY_INSTRUMENTS[product] for product in intraday_products]
            )
        }
        for product in intraday_products:
            row = by_instrument.get(_INTRADAY_INSTRUMENTS[product])
            if not row:
                continue
            documents.append(
                RagEvidence(
                    doc_id=f"intraday:{row['observation_id']}",
                    doc_type="market_observation",
                    source_id=str(row["source_id"]),
                    tier="B",
                    title=f"{row['instrument']} {row.get('price_type', '')} {row['observed_at']}",
                    summary=(
                        f"{row['instrument']}={row.get('last')} {row.get('unit')}；观测 {row['observed_at']}；"
                        f"来源 {row['source_id']}"
                    ),
                    url=str(row.get("source_url") or ""),
                    observed_at=str(row["observed_at"]),
                    visible_at=str(row.get("created_at") or row["observed_at"]),
                    metadata={"product": product, "structured_price": True},
                )
            )
    return documents[:8]


def _prepend_structured_price_documents(
    retrieval: RagSearchResponse, question: str, *, as_of_time: str | None
) -> None:
    additions = latest_price_evidence(question, as_of_time=as_of_time)
    if not additions:
        return
    existing = {document.doc_id for document in retrieval.documents}
    fresh = [document for document in additions if document.doc_id not in existing]
    if fresh:
        retrieval.documents[:0] = fresh


def build_context_pack(
    question: str,
    *,
    task_type: str = "assistant_answer",
    product: str = "POY",
    context_event_id: str | None = None,
    as_of_time: str | None = None,
    persist: bool = True,
    retrieval_query: str | None = None,
) -> dict[str, Any]:
    timings: dict[str, int] = {}
    timing_started = perf_counter()

    def mark_timing(name: str) -> None:
        nonlocal timing_started
        now = perf_counter()
        timings[name] = round((now - timing_started) * 1000)
        timing_started = now

    if persist:
        seed_prompt_registry()
    prompt = get_prompt(task_type=task_type)
    mark_timing("prompt_ms")
    base_chunk_result = retrieve_chunks(
        retrieval_query or question,
        limit=20,
        as_of_time=as_of_time,
        persist_run=persist,
        allowed_doc_types=ASSISTANT_ALLOWED_DOC_TYPES,
    )
    retrieval = _retrieval_response(
        retrieval_query or question,
        base_chunk_result,
        as_of_time=as_of_time,
    )
    allowed_doc_ids = {item.doc_id for item in retrieval.documents}
    mark_timing("base_retrieval_ms")
    # The graph snapshot and memory rows only depend on question/product/as_of,
    # so one materialization serves both passes; re-materializing the full graph
    # payload per pass was pure repeated CPU/IO on the answer critical path.
    shared_snapshot = materialize_graph_snapshot(
        question=question,
        product=product,
        as_of_time=as_of_time,
        persist=persist,
    )
    mark_timing("graph_snapshot_ms")
    discovery = build_graph_memory_contribution(
        question=question,
        product=product,
        as_of_time=as_of_time,
        persist=False,
        allowed_evidence_ids=allowed_doc_ids,
        snapshot=shared_snapshot,
    )
    mark_timing("graph_discovery_ms")
    expansion_terms = list((discovery.get("retrieval_expansion") or {}).get("terms") or [])[:6]
    chunk_result = base_chunk_result
    if expansion_terms:
        expanded = retrieve_chunks(
            f"{retrieval_query or question}\nGraphRAG实体扩展：{' '.join(expansion_terms)}",
            limit=20,
            as_of_time=as_of_time,
            persist_run=False,
            allowed_doc_types=ASSISTANT_ALLOWED_DOC_TYPES,
        )
        chunk_result = _merge_retrieval_results(base_chunk_result, expanded)
        retrieval = _retrieval_response(
            retrieval_query or question,
            chunk_result,
            as_of_time=as_of_time,
        )
        allowed_doc_ids = {item.doc_id for item in retrieval.documents}
    mark_timing("expanded_retrieval_ms")
    graph_memory = build_graph_memory_contribution(
        question=question,
        product=product,
        as_of_time=as_of_time,
        persist=persist,
        allowed_evidence_ids=allowed_doc_ids,
        snapshot=shared_snapshot,
        memory=discovery.get("memory"),
    )
    mark_timing("graph_final_ms")
    # Explicit price questions must see the latest same-basis observation
    # ahead of news retrieval, so "最新价格" cites the structured row first.
    _prepend_structured_price_documents(retrieval, question, as_of_time=as_of_time)
    mark_timing("structured_price_ms")
    graph_doc_ids = list(graph_memory.get("allowed_graph_doc_ids") or [])
    memory = graph_memory.get("memory", {})
    memory_items = list(memory.get("items", []))
    few_shots = list_few_shots(task_type)[:3]
    evidence_ids = [item.doc_id for item in retrieval.documents]
    excluded_evidence_ids: list[str] = []
    conflict_evidence_ids = [item.doc_id for item in retrieval.documents if item.risk_flags or item.tier in {"C", "D"}]
    graph_path_ids = _graph_ids(graph_memory.get("graph", {}))
    memory_item_ids = [item["item_id"] for item in memory_items[:10]]
    content = _pack_content(
        question=question,
        prompt=prompt,
        retrieval_context=build_rag_context(retrieval),
        chunk_result=chunk_result,
        graph=graph_memory.get("graph", {}),
        memory_items=memory_items,
        few_shots=few_shots,
        graph_memory_context=str(graph_memory.get("context_text", "")),
    )
    main_prediction = None
    business_evidence = None
    if task_type == "assistant_answer":
        from .evidence_context import evidence_text, try_freeze_evidence
        from .prediction_replay import timestamp
        business_evidence = try_freeze_evidence(
            timestamp(as_of_time or now_iso()),
            source_urls=[item.url for item in retrieval.documents if item.url],
            source_texts={item.url: item.summary + "\n" + item.snippet for item in retrieval.documents if item.url},
        )
        content += ("\n\n[统一证明关系：仅为当次检索材料的分析分类；不新增可引用事实或数字，"
                    "引用仍须来自上方RAG证据；历史反应不能作为当期预测]\n"
                    + evidence_text(business_evidence, allowed_quotes=False))
        mark_timing("business_evidence_ms")
        from .prediction_presentation import forecast_context

        try:
            main_prediction = forecast_context(as_of_time=as_of_time)
        except (ValueError, RuntimeError):
            main_prediction = {"status": "unavailable", "batch_id": None,
                               "text": "主预测读取失败，不替代生成另一份价格预测。"}
        content += "\n\n[已发行主预测：用于解释模型输出，不充当事实证据]\n" + main_prediction["text"]
    pack_id = new_id("context_pack")
    now = now_iso()
    pack = {
        "pack_id": pack_id,
        "version": CONTEXT_PACK_VERSION,
        "created_at": now,
        "question": question,
        "task_type": task_type,
        "product": product,
        "as_of_time": as_of_time or "",
        "evidence_ids": evidence_ids,
        "graph_path_ids": graph_path_ids,
        "memory_item_ids": memory_item_ids,
        "quality_gates": retrieval.warnings + (["business_evidence_unavailable"] if business_evidence
            and (business_evidence.get("status") == "unavailable"
                 or not business_evidence.get("dossier", {}).get("capture_complete")) else []),
        "content": content,
        "token_estimate": estimate_tokens(content),
        "metadata": {
            "business_evidence": business_evidence,
            "main_prediction_batch_id": main_prediction.get("batch_id") if main_prediction else None,
            "main_prediction_status": main_prediction.get("status") if main_prediction else None,
            "stage_timings_ms": timings,
            "prompt_version": prompt["version"],
            "prompt_id": prompt["prompt_id"],
            "retrieval_policy_version": retrieval.retrieval_metadata.get("config_version", ""),
            "retrieval_config_hash": retrieval.retrieval_metadata.get("config_hash", ""),
            "index_version": chunk_result.get("metadata", {}).get("index_version", ""),
            "index_id": chunk_result.get("metadata", {}).get("index_id", ""),
            "embedding_model": chunk_result.get("metadata", {}).get("embedding_model", ""),
            "embedding_version": chunk_result.get("metadata", {}).get("embedding_model_version", ""),
            "embedding_mode": chunk_result.get("metadata", {}).get("embedding_mode", ""),
            "retrieval_mode": chunk_result.get("metadata", {}).get("retrieval_mode", ""),
            "index_status": chunk_result.get("status", ""),
            "index_stale": bool(chunk_result.get("metadata", {}).get("stale", False)),
            "index_stale_reason": chunk_result.get("metadata", {}).get("stale_reason", ""),
            "case_memory_version": "political_case_memory.v1",
            "retrieval_confidence": retrieval.confidence,
            "retrieval_evidence_level": retrieval.evidence_level,
            "chunk_retrieval_run_id": chunk_result.get("retrieval_run_id", ""),
            "chunk_ids": [item["chunk_id"] for item in chunk_result["items"]],
            "selected_evidence_ids": evidence_ids,
            "excluded_evidence_ids": excluded_evidence_ids,
            "conflict_evidence_ids": conflict_evidence_ids,
            "graph_snapshot_id": graph_memory.get("graph", {}).get("snapshot_id", ""),
            "graph_version": graph_memory.get("graph", {}).get("graph_version", ""),
            "graph_allowed_evidence_ids": graph_doc_ids,
            "memory_retrieval_mode": memory.get("retrieval_mode", ""),
            "graph_memory_version": graph_memory.get("version", ""),
            "graph_memory_warnings": graph_memory.get("warnings", []),
            "leak_check_result": (
                "passed" if not any("future" in item.risk_flags for item in retrieval.documents) else "blocked"
            ),
        },
        "retrieval": retrieval,
        "graph": graph_memory.get("graph", {}),
        "memory": memory,
        "graph_memory": graph_memory,
    }
    if persist:
        _store_context_pack(pack)
    return pack


def get_context_pack(pack_id: str) -> dict[str, Any] | None:
    with closing(connect()) as connection:
        row = connection.execute("SELECT * FROM context_packs WHERE pack_id = ?", (pack_id,)).fetchone()
    return _row_to_pack(row) if row else None


def list_context_packs(*, limit: int = 50) -> list[dict[str, Any]]:
    with closing(connect()) as connection:
        rows = connection.execute(
            """
            SELECT * FROM context_packs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (min(max(limit, 1), 200),),
        ).fetchall()
    return [_row_to_pack(row) for row in rows]


def _store_context_pack(pack: dict[str, Any]) -> None:
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO context_packs (
              pack_id, version, created_at, question, task_type, product, as_of_time,
              evidence_ids, graph_path_ids, memory_item_ids, quality_gates, content, token_estimate, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                pack["pack_id"],
                pack["version"],
                pack["created_at"],
                safe_summary(pack["question"], max_chars=1000),
                pack["task_type"],
                pack["product"],
                pack["as_of_time"],
                json_dumps(pack["evidence_ids"]),
                json_dumps(pack["graph_path_ids"]),
                json_dumps(pack["memory_item_ids"]),
                json_dumps(pack["quality_gates"]),
                pack["content"],
                pack["token_estimate"],
                json_dumps(pack["metadata"]),
            ),
        )


def _pack_content(
    *,
    question: str,
    prompt: dict[str, Any],
    retrieval_context: str,
    chunk_result: dict[str, Any],
    graph: dict[str, Any],
    memory_items: list[dict[str, Any]],
    few_shots: list[dict[str, Any]],
    graph_memory_context: str = "",
) -> str:
    selected_node = graph.get("selected_node")
    selected_label = selected_node.get("label") if isinstance(selected_node, dict) else ""
    graph_summary = {
        "selected_node": selected_label,
        "warnings": graph.get("warnings", []),
        "upstream_path": graph.get("upstream_path", []),
    }
    chunks = [
        f"- {item['title']}：{safe_summary(item['text'], max_chars=220)}" for item in chunk_result.get("items", [])[:6]
    ]
    memories = [
        f"- {item.get('memory_type', item.get('label', 'memory'))} / "
        f"{item.get('title', item.get('source_id', ''))}：{item.get('summary', '')}"
        for item in memory_items[:8]
    ]
    examples = [f"- 问：{item['input']} 答：{item['output']}" for item in few_shots]
    return "\n\n".join(
        [
            f"【任务问题】{safe_summary(question, max_chars=1000)}",
            f"【提示版本】{prompt['prompt_id']} / {prompt['version']}",
            f"【系统约束】{prompt['content']}",
            f"【RAG 证据】\n{retrieval_context}",
            "【持久索引补充】\n" + ("\n".join(chunks) if chunks else "无可用 chunk。"),
            f"【证据图谱摘要】{json_dumps(graph_summary)}",
            f"【GraphRAG 与 Memory 信任边界】\n{graph_memory_context}",
            "【相关记忆】\n" + ("\n".join(memories) if memories else "暂无相关记忆。"),
            "【few-shot 行为示例】\n" + ("\n".join(examples) if examples else "暂无示例。"),
            "【输出要求】必须区分事实、推断、反证、风险和下一步；只能引用上下文包给出的证据。",
        ]
    )


def _graph_ids(graph: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for field in ("path_id", "snapshot_id"):
        if graph.get(field):
            ids.append(str(graph[field]))
    for field in ("node_ids", "edge_ids"):
        value = graph.get(field)
        if isinstance(value, list):
            ids.extend(str(item) for item in value if item)
    selected = graph.get("selected_node")
    if isinstance(selected, dict) and selected.get("id"):
        ids.append(str(selected["id"]))
    for key in ("upstream_path", "stakeholder_paths", "counter_evidence_paths", "prediction_paths", "backtest_paths"):
        value = graph.get(key)
        if isinstance(value, list):
            for item in value[:20]:
                if isinstance(item, dict):
                    ids.extend(str(item.get(field)) for field in ("id", "source", "target") if item.get(field))
                elif item:
                    ids.append(str(item))
    return list(dict.fromkeys(ids))[:80]


def _row_to_pack(row: Any) -> dict[str, Any]:
    item = dict(row)
    for field in ("evidence_ids", "graph_path_ids", "memory_item_ids", "quality_gates"):
        item[field] = json_loads(item.get(field), [])
    item["metadata"] = json_loads(item.get("metadata"), {})
    return item

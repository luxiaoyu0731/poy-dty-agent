from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from statistics import mean
from typing import Any

from .assistant_pipeline import run_assistant_pipeline
from .context_pack import build_context_pack
from .memory import MemoryManager
from .retrieval_metrics import (
    RelevanceJudgement,
    compare_ablations,
    distribution_metrics,
    leakage_metrics,
    retrieval_ranking_metrics,
)
from .semantic_embedding import EmbeddingConfig, EmbeddingService
from .semantic_index import IndexDocument, SemanticIndexBuilder, retrieve_semantic_chunks, semantic_index_status
from .storage import create_market_observation
from .unified_retriever import retrieve_chunks

FIXTURE_PATH = Path(__file__).parents[1] / "data" / "evals" / "rag_regression_v2.json"
K = 5

LABEL_TERMS = {
    "crude": ("原油", "crude", "wti", "brent"),
    "naphtha": ("石脑油", "naphtha"),
    "inventory": ("库存", "inventory", "stock"),
    "operating_rate": ("开工", "operating rate", "utilization"),
    "profit": ("利润", "margin", "profit"),
    "polyester": ("聚酯", "polyester", "涤纶"),
    "shipping": ("航运", "运价", "shipping", "freight", "tanker"),
    "sanctions": ("制裁", "sanction", "ofac"),
    "memory_provenance": ("memory", "记忆", "来源"),
    "evidence_boundary": ("证据", "边界", "review", "审核"),
}


class _OfflineBackend:
    def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        del texts, kind
        raise RuntimeError("lexical_ablation")


def bootstrap_fixed_rag_eval_corpus() -> dict[str, Any]:
    """Build the fixed suite's evidence corpus in the currently selected database.

    The daily evaluator intentionally owns a small corpus instead of inheriting
    whatever happens to be present in a developer or production database.  The
    documents retain production document identifiers so the suite exercises the
    real retriever, citation binder, time filter, and graph provenance join.
    """

    create_market_observation(
        observation_id="rag_eval_crude_cost",
        payload={
            "source_id": "eia",
            "observed_at": "2026-06-30T08:00:00+00:00",
            "indicator": "crude_oil_cost",
            "product": "crude_oil",
            "value": 82.0,
            "unit": "USD/bbl",
            "frequency": "daily",
            "region": "global",
            "evidence_url": "https://www.eia.gov/",
            "notes": "原油上涨通过石脑油、PX、PTA向POY和DTY传导成本压力。",
            "raw": {"fixture": "rag-regression-v2"},
        },
    )
    documents = [
        _eval_document(
            "market:rag_eval_crude_cost",
            "market_observation",
            "eia",
            "原油成本观测",
            "原油上涨通过石脑油 naphtha、PX、PTA向POY和DTY传导成本压力。",
        ),
        _eval_document(
            "kg:node:crude_oil",
            "knowledge_node",
            "knowledge_graph",
            "原油 crude oil",
            "原油上涨先影响石脑油，再沿PX、PTA传导至POY和DTY成本。",
        ),
        _eval_document(
            "kg:edge:crude_oil:naphtha",
            "knowledge_edge",
            "knowledge_graph",
            "crude_oil -> naphtha",
            "原油到石脑油的成本传导边，继而影响PX、PTA、POY和DTY。",
        ),
        _eval_document(
            "kg:node:PX",
            "knowledge_node",
            "knowledge_graph",
            "PX",
            "PX连接石脑油与PTA，是POY和DTY聚酯成本链的上游节点。",
        ),
        _eval_document(
            "kg:node:PTA",
            "knowledge_node",
            "knowledge_graph",
            "PTA库存与开工率",
            "截至2026-07-01，PTA库存和开工率用于判断聚酯供需及POY报价。",
            visible_at="2026-06-30T12:00:00+00:00",
        ),
        _eval_document(
            "kg:node:POY",
            "knowledge_node",
            "knowledge_graph",
            "POY涤纶长丝",
            "涤纶长丝POY的开工率、库存和利润共同影响报价。",
        ),
        _eval_document(
            "kg:node:DTY",
            "knowledge_node",
            "knowledge_graph",
            "DTY涤纶长丝",
            "涤纶长丝DTY的开工率、库存和利润共同影响报价。",
        ),
        _eval_document(
            "kg:node:MEG",
            "knowledge_node",
            "knowledge_graph",
            "MEG inventory",
            "MEG inventory库存上升会改变polyester聚酯成本和margin利润。",
        ),
        _eval_document(
            "kg:edge:meg:cost_pressure_index",
            "knowledge_edge",
            "knowledge_graph",
            "MEG -> cost pressure",
            "MEG inventory up增加库存压力并影响polyester margin和POY、DTY利润。",
        ),
        _eval_document(
            "kg:edge:opec:crude_oil",
            "knowledge_edge",
            "knowledge_graph",
            "OPEC -> crude oil",
            "OPEC、OFAC制裁和航运受阻会影响原油供给及聚酯链风险。",
        ),
        _eval_document(
            "project_doc:docs-rag-event-direction-md",
            "project_document",
            "project_docs",
            "RAG证据边界",
            "模型记忆和历史猜测不是事实证据；结论必须追溯到已审核来源。",
        ),
        _eval_document(
            "project_doc:docs-system-introduction-md",
            "project_document",
            "project_docs",
            "Memory provenance",
            "memory仅作上下文，不能把上次模型猜测当作本次事实依据。",
        ),
        # A future item must be physically present in the index so the historical
        # case proves filtering rather than merely observing an absent record.
        _eval_document(
            "eval:future:pta",
            "market_observation",
            "eval_future",
            "PTA未来库存",
            "2026-07-10 PTA库存和开工率变化。",
            visible_at="2026-07-10T00:00:00+00:00",
        ),
    ]
    result = SemanticIndexBuilder().rebuild(documents)
    if result.get("status") != "ready":
        raise RuntimeError(f"rag_eval_index_bootstrap_failed:{result}")
    return result


def _eval_document(
    document_id: str,
    source_kind: str,
    source_id: str,
    title: str,
    body: str,
    *,
    visible_at: str = "2026-06-30T00:00:00+00:00",
) -> IndexDocument:
    return IndexDocument(
        document_id=document_id,
        source_kind=source_kind,
        source_id=source_id,
        title=title,
        body=body,
        observed_at=visible_at,
        visible_at=visible_at,
        evidence_level="B",
        review_status="reviewed",
        metadata={"fixture": "rag-regression-v2"},
    )


async def run_fixed_rag_quality_eval() -> dict[str, Any]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    cases: list[dict[str, Any]] = []
    started = time.perf_counter()
    for case in fixture["cases"]:
        cases.append(await _evaluate_case(case))
    aggregates = _aggregate(cases)
    aggregates.update(
        {
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "provider_model_calls": 0,
            "estimated_provider_cost": 0.0,
            "provider_eval": "disabled_by_default",
        }
    )
    return {
        "suite": fixture["version"],
        "case_count": len(cases),
        "aggregates": aggregates,
        "cases": cases,
    }


async def _evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    query = str(case["query"])
    as_of_time = case.get("as_of_time")
    started = time.perf_counter()
    base = retrieve_chunks(query, limit=20, as_of_time=as_of_time)
    pack = build_context_pack(query, as_of_time=as_of_time, persist=False)
    graph_memory_result = pack.get("graph_memory") or {}
    enhanced_documents = list(pack["retrieval"].documents)
    base_items = list(base.get("items") or [])
    base_ids = _unique_document_ids(base_items)
    enhanced_ids = [item.doc_id for item in enhanced_documents]
    judgements = _judgements(dict(case.get("relevance_judgements") or {}))
    hybrid = retrieval_ranking_metrics(base_ids, judgements, k=K)
    graph_on = retrieval_ranking_metrics(enhanced_ids, judgements, k=K)
    lexical = _lexical_metrics(query, as_of_time, judgements)
    graph = graph_memory_result.get("graph") or {}
    memory = graph_memory_result.get("memory") or {}
    memory_off = MemoryManager().query(
        query=query,
        product="POY",
        as_of_time=as_of_time,
        memory_types=(),
        limit=10,
    )
    rows = [_metric_row(item) for item in base_items]
    leaks = (
        leakage_metrics(rows, as_of_time=_parse_as_of(as_of_time))
        if as_of_time
        else {
            "future_leakage_count": 0,
            "rejected_evidence_leakage_count": sum(str(row.get("review_status")) == "rejected" for row in rows),
        }
    )
    expected_abstention = bool(case.get("expected_abstention"))
    assistant = await run_assistant_pipeline(query, as_of_time=as_of_time, preview=True)
    # A bounded answer can still cite reviewed background evidence while refusing
    # the unsupported action or fact requested by a negative case. Conversely,
    # ordinary cases must actually adopt reviewed evidence; merely emitting the
    # generic safety boundary is not a useful answer.
    assistant_abstained = (
        _has_explicit_abstention(assistant.answer)
        if expected_abstention
        else not bool(assistant.evidence_groups.adopted)
    )
    assistant_citation_validity = _warning_metric(assistant.warnings, "citation_validity")
    assistant_citation_entailment = _warning_metric(assistant.warnings, "citation_entailment")
    return {
        "id": case["id"],
        "intent": case["intent"],
        "query": query,
        "hybrid": hybrid,
        "lexical_only": lexical,
        "hybrid_minus_lexical": compare_ablations(lexical, hybrid),
        "graphrag_on": graph_on,
        "graphrag_gain": compare_ablations(hybrid, graph_on),
        "memory_on_relevant_count": sum(
            float(item.get("relevance_score") or 0) > 0 for item in memory.get("items", [])
        ),
        "memory_off_relevant_count": len(memory_off["items"]),
        "graph_path_valid": bool(
            graph.get("status") == "ready"
            and graph_memory_result.get("allowed_graph_doc_ids")
            and set(graph_memory_result.get("allowed_graph_doc_ids") or []) <= set(enhanced_ids)
        ),
        "answer_abstention_quality": (1.0 if expected_abstention == assistant_abstained else 0.0),
        "citation_validity": assistant_citation_validity,
        "citation_entailment": assistant_citation_entailment,
        "citation_conflict_count": int(_warning_metric(assistant.warnings, "citation_conflicts")),
        **distribution_metrics(rows, k=K),
        **leaks,
        "retrieval_mode": (base.get("metadata") or {}).get("retrieval_mode", ""),
        "index_version": (base.get("metadata") or {}).get("index_version", ""),
        "warnings": base.get("warnings", []),
        "latency_ms": round((time.perf_counter() - started) * 1000),
    }


def _lexical_metrics(
    query: str,
    as_of_time: str | None,
    judgements: list[RelevanceJudgement],
) -> dict[str, float]:
    status = semantic_index_status()
    active = status.get("active_index")
    if not active:
        return {f"recall@{K}": 0.0, f"precision@{K}": 0.0, "mrr": 0.0, f"ndcg@{K}": 0.0}
    config = EmbeddingConfig.from_settings()
    lexical_service = EmbeddingService(
        replace(config, fallback_policy="lexical_only"),
        _OfflineBackend(),
    )
    result = retrieve_semantic_chunks(
        query,
        limit=20,
        as_of_time=as_of_time,
        embedding_service=lexical_service,
    )
    return retrieval_ranking_metrics(
        _unique_document_ids(result.get("items") or []),
        judgements,
        k=K,
    )


def _judgements(expected: dict[str, object]) -> list[RelevanceJudgement]:
    return [RelevanceJudgement(str(doc_id), int(relevance)) for doc_id, relevance in sorted(expected.items())]


def _terms_for_label(label: str) -> tuple[str, ...]:
    return LABEL_TERMS.get(label, (label,))


def _unique_document_ids(items: list[dict[str, Any]]) -> list[str]:
    return list(dict.fromkeys(str(item["document_id"]) for item in items if item.get("document_id")))


def _item_text(item: dict[str, Any]) -> str:
    return f"{item.get('title', '')} {item.get('text', '')}"


def _warning_metric(warnings: list[str], name: str) -> float:
    prefix = f"{name}="
    for warning in warnings:
        if warning.startswith(prefix):
            try:
                return float(warning.removeprefix(prefix))
            except ValueError:
                return 0.0
    return 0.0


def _has_explicit_abstention(answer: str) -> bool:
    normalized = " ".join(answer.split()).lower()
    return any(
        marker in normalized
        for marker in (
            "不形成自动执行指令",
            "没有足够可引用证据，无法形成结论",
            "不能作为事实依据",
            "cannot form a conclusion",
            "cannot provide",
        )
    )


def _metric_row(item: dict[str, Any]) -> dict[str, object]:
    metadata = item.get("document_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    products = metadata.get("products") or metadata.get("affected_products") or []
    if isinstance(products, str):
        products = [products]
    return {
        "source_id": item.get("source_id", ""),
        "tier": item.get("evidence_level", "unknown"),
        "products": products,
        "visible_at": item.get("visible_at", ""),
        "embedding_generated_at": (item.get("metadata") or {}).get("embedding_generated_at", ""),
        "review_status": item.get("review_status", ""),
    }


def _parse_as_of(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    def average(path: tuple[str, str]) -> float:
        return round(mean(float(case[path[0]][path[1]]) for case in cases), 6)

    return {
        f"hybrid_recall@{K}": average(("hybrid", f"recall@{K}")),
        f"hybrid_precision@{K}": average(("hybrid", f"precision@{K}")),
        "hybrid_mrr": average(("hybrid", "mrr")),
        f"hybrid_ndcg@{K}": average(("hybrid", f"ndcg@{K}")),
        f"lexical_recall@{K}": average(("lexical_only", f"recall@{K}")),
        f"graphrag_recall@{K}": average(("graphrag_on", f"recall@{K}")),
        "future_leakage_count": sum(case["future_leakage_count"] for case in cases),
        "rejected_evidence_leakage_count": sum(case["rejected_evidence_leakage_count"] for case in cases),
        "graph_path_validity": round(mean(float(case["graph_path_valid"]) for case in cases), 6),
        "answer_abstention_quality": round(mean(float(case["answer_abstention_quality"]) for case in cases), 6),
        "citation_validity": round(mean(float(case["citation_validity"]) for case in cases), 6),
        "citation_entailment": round(mean(float(case["citation_entailment"]) for case in cases), 6),
        "citation_conflict_count": sum(int(case["citation_conflict_count"]) for case in cases),
        "mean_source_diversity": round(mean(float(case["source_diversity"]) for case in cases), 6),
        "mean_memory_relevant_count": round(mean(float(case["memory_on_relevant_count"]) for case in cases), 6),
    }

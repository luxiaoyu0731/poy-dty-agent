from __future__ import annotations

from uuid import uuid4

from .rag_quality_eval import bootstrap_fixed_rag_eval_corpus, run_fixed_rag_quality_eval
from .storage import record_eval_run

DAILY_RAG_CASES = [
    {
        "name": "cost chain retrieves knowledge graph",
        "query": "原油 石脑油 PX PTA POY DTY 成本传导链",
        "min_docs": 2,
        "expected_doc_types": {"knowledge_node", "knowledge_edge"},
    },
    {
        "name": "source boundary retrieves source registry",
        "query": "EIA FRED API key 免费 数据源 授权边界",
        "min_docs": 2,
        "expected_doc_types": {"source_config"},
    },
    {
        "name": "geopolitical risk retrieves official source context",
        "query": "OPEC OFAC 制裁 航运 中东 原油 风险",
        "min_docs": 2,
        "expected_doc_types": {"news_source", "source_config", "knowledge_node"},
    },
    {
        "name": "prediction review retrieves ledger or knowledge",
        "query": "预测复盘 反证 权重调整 证据",
        "min_docs": 1,
        "expected_doc_types": {"prediction_record", "knowledge_node", "source_config"},
    },
]


async def run_daily_rag_eval_suite() -> dict[str, object]:
    bootstrap = bootstrap_fixed_rag_eval_corpus()
    quality = await run_fixed_rag_quality_eval()
    results: list[dict[str, object]] = [
        {
            "name": str(case["id"]),
            "passed": (
                int(case["future_leakage_count"]) == 0
                and int(case["rejected_evidence_leakage_count"]) == 0
                and float(case["answer_abstention_quality"]) == 1.0
                and (
                    float(case["hybrid"]["recall@5"]) > 0
                    or case["intent"] in {"no_answer", "prompt_injection", "memory_boundary"}
                )
                and (
                    case["intent"] in {"no_answer", "prompt_injection", "memory_boundary"}
                    or (float(case["citation_validity"]) >= 0.5 and float(case["citation_entailment"]) >= 0.5)
                )
                and (case["intent"] != "multi_hop" or bool(case["graph_path_valid"]))
            ),
            **case,
        }
        for case in quality["cases"]
    ]

    passed = sum(1 for result in results if result["passed"])
    payload = {
        "suite": "daily-rag",
        "passed": passed,
        "total": len(results),
        "results": results,
        "quality_metrics": quality["aggregates"],
        "fixed_regression_version": quality["suite"],
        "bootstrap": {
            "status": bootstrap["status"],
            "documents": bootstrap["documents"],
            "chunks": bootstrap["chunks"],
            "index_version": bootstrap["index_version"],
        },
        "quality_gates": {
            "hybrid_recall@5_min": 0.2,
            "citation_validity_min": 0.5,
            "citation_entailment_min": 0.5,
            "graph_path_validity_min": 0.4,
            "future_leakage_max": 0,
            "rejected_leakage_max": 0,
        },
    }
    record_eval_run(
        eval_id=str(uuid4()),
        suite="daily-rag",
        passed=passed,
        total=len(results),
        results=results,
    )
    return payload

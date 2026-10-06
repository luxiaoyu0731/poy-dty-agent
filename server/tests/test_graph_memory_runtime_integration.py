from __future__ import annotations

from contextlib import closing
from pathlib import Path

import pytest

from app import agent_runtime, graphrag_store, rag, storage, unified_retriever
from app.graph_memory_context import build_graph_memory_contribution
from app.graphrag_reasoning import build_reasoning_path
from app.knowledge_graph import _query_relevance
from app.memory import MemoryManager
from app.rag import retrieve_persisted_evidence
from app.settings import settings
from app.storage import upsert_evidence_review


@pytest.fixture()
def isolated_db(tmp_path: Path):
    original_sqlite_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "graph-memory.db"))
    yield Path(settings.sqlite_path)
    object.__setattr__(settings, "sqlite_path", original_sqlite_path)


def _graph_payload(*, observed_at: str, doc_id: str, direction: str = "up") -> dict[str, object]:
    return {
        "nodes": [
            {
                "id": doc_id,
                "label": "原油供应事件",
                "type": "NewsArticle",
                "layer": "新闻事件",
                "summary": "供应扰动影响原油。",
                "tier": "B",
                "status": "available",
                "observed_at": observed_at,
                "metadata": {},
            },
            {
                "id": "product:crude_oil",
                "label": "原油",
                "type": "Product",
                "layer": "产品链路",
                "summary": "能源成本。",
                "tier": "B",
                "status": "available",
                "observed_at": "",
                "metadata": {},
            },
            {
                "id": "product:POY",
                "label": "POY",
                "type": "Product",
                "layer": "产品链路",
                "summary": "下游产品。",
                "tier": "B",
                "status": "available",
                "observed_at": "",
                "metadata": {},
            },
        ],
        "edges": [
            {
                "id": f"{doc_id}:oil",
                "source": doc_id,
                "target": "product:crude_oil",
                "relation": "event_affects_product",
                "polarity": direction,
                "confidence": 0.8,
                "evidence_ids": [doc_id],
                "metadata": {},
            },
            {
                "id": "oil:poy",
                "source": "product:crude_oil",
                "target": "product:POY",
                "relation": "product_transmits_to_product",
                "polarity": direction,
                "confidence": 0.7,
                "evidence_ids": [doc_id],
                "metadata": {},
            },
        ],
        "summary": {"node_count": 3, "edge_count": 2, "layers": {}, "missing_documents": []},
        "warnings": [],
    }


def test_reasoning_path_is_bound_to_exact_snapshot_and_doc_ids(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = iter(
        [
            _graph_payload(observed_at="2026-07-01T00:00:00+00:00", doc_id="article:old"),
            _graph_payload(observed_at="2026-07-20T00:00:00+00:00", doc_id="article:future"),
        ]
    )
    monkeypatch.setattr(graphrag_store, "graph_payload", lambda **_: next(payloads))
    old = graphrag_store.materialize_graph_snapshot(
        question="原油如何传导到 POY",
        product="POY",
        as_of_time="2026-07-10T00:00:00+00:00",
    )
    graphrag_store.materialize_graph_snapshot(question="原油如何传导到 POY", product="POY")

    path = build_reasoning_path(
        question="原油如何传导到 POY",
        product="POY",
        snapshot_id=old["snapshot_id"],
        as_of_time="2026-07-10T00:00:00+00:00",
    )

    assert path["snapshot_id"] == old["snapshot_id"]
    assert path["graph_version"]
    assert path["status"] == "ready"
    assert "news_article:old" in path["evidence_doc_ids"]
    assert "news_article:future" not in path["evidence_doc_ids"]
    assert path["as_of_time"] == "2026-07-10T00:00:00+00:00"


def test_graph_snapshot_excludes_late_backfill_and_rejected_evidence(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _graph_payload(
        observed_at="2026-07-01T00:00:00+00:00",
        doc_id="article:late",
    )
    payload["nodes"][0]["visible_at"] = "2026-07-20T00:00:00+00:00"  # type: ignore[index]
    rejected = dict(payload["nodes"][0])  # type: ignore[index]
    rejected.update(
        {
            "id": "article:rejected",
            "visible_at": "2026-07-02T00:00:00+00:00",
            "review_status": "rejected",
        }
    )
    payload["nodes"].append(rejected)  # type: ignore[union-attr]
    monkeypatch.setattr(graphrag_store, "graph_payload", lambda **_: payload)

    snapshot = graphrag_store.materialize_graph_snapshot(
        question="原油如何传导到POY",
        product="POY",
        as_of_time="2026-07-10T00:00:00+00:00",
        persist=False,
    )
    node_ids = {item["id"] for item in snapshot["payload"]["nodes"]}

    assert "article:late" not in node_ids
    assert "article:rejected" not in node_ids
    assert {"product:crude_oil", "product:POY"} <= node_ids
    assert snapshot["payload"]["edges"] == []


def test_reasoning_path_does_not_invent_path_without_evidence(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _graph_payload(observed_at="", doc_id="article:removed")
    payload["nodes"] = [node for node in payload["nodes"] if node["id"].startswith("product:")]  # type: ignore[index]
    payload["edges"] = [edge for edge in payload["edges"] if edge["id"] == "oil:poy"]  # type: ignore[index]
    monkeypatch.setattr(graphrag_store, "graph_payload", lambda **_: payload)
    snapshot = graphrag_store.materialize_graph_snapshot(question="原油 POY", product="POY")

    path = build_reasoning_path(question="原油 POY", product="POY", snapshot_id=snapshot["snapshot_id"])

    assert path["status"] == "degraded_no_evidence_path"
    assert path["node_ids"] == []
    assert path["edge_ids"] == []
    assert path["evidence_doc_ids"] == []
    assert path["warnings"]


def test_reasoning_path_reports_opposing_evidence_as_conflict(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _graph_payload(
        observed_at="2026-07-01T00:00:00+00:00",
        doc_id="article:support",
    )
    payload["nodes"].append(  # type: ignore[union-attr]
        {
            "id": "article:counter",
            "label": "需求走弱反证",
            "type": "NewsArticle",
            "layer": "新闻事件",
            "summary": "需求下降压制原油。",
            "tier": "B",
            "status": "available",
            "observed_at": "2026-07-01T00:00:00+00:00",
            "metadata": {},
        }
    )
    payload["edges"].append(  # type: ignore[union-attr]
        {
            "id": "counter:oil",
            "source": "article:counter",
            "target": "product:crude_oil",
            "relation": "event_affects_product",
            "polarity": "down",
            "confidence": 0.8,
            "evidence_ids": ["article:counter"],
            "metadata": {},
        }
    )
    monkeypatch.setattr(graphrag_store, "graph_payload", lambda **_: payload)
    snapshot = graphrag_store.materialize_graph_snapshot(
        question="原油如何传导到 POY",
        product="POY",
    )

    path = build_reasoning_path(
        question="原油如何传导到 POY",
        product="POY",
        snapshot_id=snapshot["snapshot_id"],
    )

    opposing = next(item for item in path["conflicts"] if item["relation"] == "opposing_polarity")
    assert set(opposing["evidence_doc_ids"]) == {"news_article:support", "news_article:counter"}


def test_memory_query_is_relevant_time_safe_and_marks_generated_memory_untrusted(isolated_db: Path) -> None:
    with closing(storage.connect()) as connection, connection:
        connection.executemany(
            """
            INSERT INTO memory_items (
              item_id, created_at, updated_at, memory_type, title, summary, source_table,
              source_id, product, observed_at, evidence_level, payload, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "mem-old",
                    "2026-07-02T00:00:00+00:00",
                    "2026-07-02T00:00:00+00:00",
                    "perceptual",
                    "PTA 库存下降",
                    "PTA 库存下降，关注 POY 成本。",
                    "industry_observations",
                    "industry-old",
                    "PTA",
                    "2026-07-01T00:00:00+00:00",
                    "B",
                    "{}",
                    '{"visible_at":"2026-07-01T00:00:00+00:00","trust_boundary":"external_evidence","generation_version":"memory-v2","doc_id":"industry:industry-old"}',
                ),
                (
                    "mem-future",
                    "2026-07-21T00:00:00+00:00",
                    "2026-07-21T00:00:00+00:00",
                    "perceptual",
                    "PTA 库存未来回填",
                    "未来材料。",
                    "industry_observations",
                    "industry-future",
                    "POY",
                    "2026-07-20T00:00:00+00:00",
                    "A",
                    "{}",
                    '{"visible_at":"2026-07-20T00:00:00+00:00","trust_boundary":"external_evidence","generation_version":"memory-v2"}',
                ),
                (
                    "mem-model",
                    "2026-07-03T00:00:00+00:00",
                    "2026-07-03T00:00:00+00:00",
                    "episodic",
                    "模型总结",
                    "PTA 库存下降。",
                    "llm_traces",
                    "trace-1",
                    "POY",
                    "2026-07-03T00:00:00+00:00",
                    "A",
                    "{}",
                    '{"visible_at":"2026-07-03T00:00:00+00:00","trust_boundary":"model_generated","generation_version":"memory-v2"}',
                ),
            ],
        )

    result = MemoryManager().query(
        query="PTA库存对POY成本有什么影响",
        product="POY",
        as_of_time="2026-07-10T00:00:00+00:00",
        limit=5,
    )

    ids = [item["item_id"] for item in result["items"]]
    assert "mem-old" in ids
    assert "mem-future" not in ids
    model_item = next(item for item in result["items"] if item["item_id"] == "mem-model")
    assert model_item["evidence_eligible"] is False
    assert result["future_filtered_count"] == 1
    assert result["retrieval_mode"] == "relevance_time_filtered"

    upsert_evidence_review(
        doc_id="industry:industry-old",
        status="rejected",
        reviewer="pytest",
        notes="rejected memory must not reach answer context",
    )
    rejected = MemoryManager().query(
        query="PTA库存对POY成本有什么影响",
        product="POY",
        as_of_time="2026-07-10T00:00:00+00:00",
        limit=5,
    )
    assert "mem-old" not in rejected["item_ids"]
    assert rejected["rejected_filtered_count"] == 1


def test_context_contribution_contains_only_actual_graph_and_memory_steps(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        graphrag_store,
        "graph_payload",
        lambda **_: _graph_payload(observed_at="2026-07-01T00:00:00+00:00", doc_id="article:old"),
    )
    contribution = build_graph_memory_contribution(
        question="原油如何传导到 POY",
        product="POY",
        as_of_time="2026-07-10T00:00:00+00:00",
        persist=False,
        allowed_evidence_ids={"news_article:old"},
        conversation_context=["用户说：请沿用上一轮口径。"],
    )

    assert contribution["graph"]["status"] == "ready"
    assert contribution["graph"]["evidence_doc_ids"] == ["news_article:old"]
    assert contribution["allowed_graph_doc_ids"] == ["news_article:old"]
    assert contribution["excluded_graph_doc_ids"] == []
    assert contribution["provenance"]["graph_version"]
    assert contribution["conversation_context"]["persisted_as_memory"] is False
    assert [step["name"] for step in contribution["runtime_steps"]] == [
        "graph_snapshot",
        "graph_reasoning",
        "memory_retrieval",
    ]
    assert all(step["status"] in {"completed", "degraded"} for step in contribution["runtime_steps"])
    assert contribution["warnings"] == []
    with closing(storage.connect()) as connection:
        assert connection.execute("SELECT COUNT(*) FROM graph_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM graph_reasoning_paths").fetchone()[0] == 0


def test_agent_runtime_records_only_steps_that_really_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        agent_runtime,
        "list_agent_jobs",
        lambda **_: [
            {"job_id": "job-graph", "agent_name": "图谱构建"},
            {"job_id": "job-reason", "agent_name": "推理判断"},
            {"job_id": "job-retrieve", "agent_name": "证据检索"},
        ],
    )
    attempts: list[dict[str, object]] = []
    checkpoints: list[dict[str, object]] = []
    monkeypatch.setattr(
        agent_runtime,
        "record_job_attempt",
        lambda **kwargs: attempts.append(kwargs) or {"attempt_id": f"a-{len(attempts)}"},
    )
    monkeypatch.setattr(
        agent_runtime,
        "create_checkpoint",
        lambda **kwargs: checkpoints.append(kwargs) or {"checkpoint_id": f"c-{len(checkpoints)}"},
    )

    result = agent_runtime.record_context_runtime_steps(
        run_id="run-1",
        contribution={
            "runtime_steps": [
                {"name": "graph_snapshot", "status": "completed", "output": {}},
                {"name": "memory_retrieval", "status": "completed", "output": {}},
                {"name": "report_generation", "status": "completed", "output": {}},
            ]
        },
    )

    assert result["actual_step_count"] == 2
    assert result["ignored"] == ["report_generation"]
    assert [item["job_id"] for item in attempts] == ["job-graph", "job-retrieve"]
    assert all(item["payload"]["provenance"] == "graph_memory_context.runtime_steps" for item in checkpoints)


def test_persisted_retrieval_adapter_preserves_unified_scores_and_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        unified_retriever,
        "retrieve_chunks",
        lambda *args, **kwargs: {
            "status": "ready",
            "warnings": [],
            "metadata": {
                "retrieval_mode": "hybrid_semantic",
                "index_version": "index-v2",
                "embedding_model": "bge-small-zh",
            },
            "items": [
                {
                    "chunk_id": "chunk-1",
                    "document_id": "news:1",
                    "source_kind": "news_article",
                    "source_id": "official",
                    "title": "PTA 库存下降",
                    "text": "PTA 库存下降。",
                    "observed_at": "2026-07-01T00:00:00+00:00",
                    "visible_at": "2026-07-01T00:00:00+00:00",
                    "evidence_level": "B",
                    "review_status": "reviewed",
                    "lexical_score": 0.4,
                    "vector_score": 0.8,
                    "rerank_score": 0.72,
                    "embedding_model": "bge-small-zh",
                    "embedding_model_version": "1",
                    "retrieval_mode": "hybrid_semantic",
                    "index_version": "index-v2",
                }
            ],
        },
    )

    result = retrieve_persisted_evidence(
        "PTA库存",
        as_of_time="2026-07-10T00:00:00+00:00",
    )

    assert result.documents[0].metadata["lexical_score"] == 0.4
    assert result.documents[0].metadata["vector_score"] == 0.8
    assert result.documents[0].metadata["rerank_score"] == 0.72
    assert result.retrieval_metadata["retrieval_mode"] == "hybrid_semantic"
    assert result.retrieval_metadata["adapter"] == "rag.retrieve_persisted_evidence.v1"


def test_unified_stale_fallback_preserves_reason_versions_and_scores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        unified_retriever,
        "retrieve_semantic_chunks",
        lambda *args, **kwargs: {
            "status": "ready",
            "items": [],
            "warnings": ["stale_index:embedding_config_changed"],
            "metadata": {
                "stale": True,
                "stale_reason": "embedding_config_changed",
                "index_version": "semantic-old",
            },
        },
    )
    monkeypatch.setattr(
        unified_retriever,
        "retrieve_index_chunks",
        lambda *args, **kwargs: {
            "status": "ready",
            "items": [
                {
                    "chunk_id": "legacy-1",
                    "document_id": "market:1",
                    "lexical_score": 0.6,
                    "vector_score": 0.4,
                    "rerank_score": 0.7,
                }
            ],
            "warnings": [],
            "metadata": {"index_version": "legacy-v1"},
        },
    )

    result = unified_retriever.retrieve_chunks("PTA库存")

    assert result["metadata"]["stale"] is True
    assert result["metadata"]["stale_reason"] == "embedding_config_changed"
    assert result["metadata"]["stale_semantic_index_version"] == "semantic-old"
    assert result["metadata"]["index_version"] == "legacy-v1"
    assert result["items"][0]["lexical_score"] == 0.6
    assert result["items"][0]["vector_score"] == 0.4
    assert result["items"][0]["retrieval_mode"] == "legacy_hash_fallback"


def test_graph_entity_discovery_uses_terms_from_full_natural_language_question() -> None:
    assert (
        _query_relevance(
            "原油供应扰动如何经过 PX、PTA 传导到 POY？",
            "原油供应扰动推升风险溢价，并影响 PX 与 PTA 成本。",
        )
        >= 3
    )
    assert (
        _query_relevance(
            "原油供应扰动如何经过 PX、PTA 传导到 POY？",
            "与聚酯产业链无关的通用软件发布公告。",
        )
        == 0
    )


def test_graph_visibility_normalizes_timezone_offsets() -> None:
    assert graphrag_store._record_visible_as_of(
        {"visible_at": "2026-07-09T16:00:00+00:00"},
        "2026-07-10T00:00:00+08:00",
    )
    assert not graphrag_store._record_visible_as_of(
        {"visible_at": "2026-07-09T20:00:00+00:00"},
        "2026-07-10T00:00:00+08:00",
    )


def test_stale_merge_prefers_live_document_with_same_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        unified_retriever,
        "retrieve_chunks",
        lambda *args, **kwargs: {
            "status": "ready",
            "warnings": ["stale_index:x"],
            "metadata": {"retrieval_mode": "hybrid_semantic", "stale": True},
            "items": [
                {
                    "chunk_id": "old",
                    "document_id": "market:1",
                    "source_kind": "market_observation",
                    "source_id": "fixture",
                    "title": "旧索引",
                    "text": "旧索引内容",
                    "evidence_level": "B",
                    "review_status": "reviewed",
                    "rerank_score": 0.8,
                }
            ],
        },
    )
    live = rag.RagSearchResponse(
        query="PTA",
        documents=[
            rag.RagEvidence(
                doc_id="market:1",
                doc_type="market_observation",
                source_id="fixture",
                tier="B",
                title="实时",
                summary="实时新内容",
                review_status="reviewed",
            )
        ],
        evidence_level="B",
        confidence=0.5,
        coverage={"market_observation": 1},
    )
    monkeypatch.setattr(rag, "retrieve_evidence", lambda *args, **kwargs: live)
    result = retrieve_persisted_evidence("PTA")
    assert result.documents[0].summary == "实时新内容"
    assert "persisted_index_returned_no_results" not in result.warnings


@pytest.mark.parametrize("stale_reason", ["snapshot_expired", "source_time_policy_changed", "grounded_summary_changed"])
def test_expired_semantic_snapshot_uses_current_corpus(monkeypatch: pytest.MonkeyPatch, stale_reason: str) -> None:
    from app.models import RagEvidence, RagSearchResponse

    monkeypatch.setattr(unified_retriever, "retrieve_semantic_chunks", lambda *a, **k: {
        "status": "ready", "items": [{"document_id": "old-price"}],
        "metadata": {"stale": True, "stale_reason": stale_reason},
    })
    seen = {}
    def current(query, **kwargs):
        seen.update(kwargs)
        return RagSearchResponse(query=query, documents=[RagEvidence(
            doc_id="market:current", doc_type="market_observation", source_id="official",
            tier="A", title="Current price", summary="New observation", score=4,
            observed_at="2026-09-13", visible_at="2026-09-13", url="https://example.org/price",
        )], evidence_level="A", confidence=0.8)
    monkeypatch.setattr(rag, "retrieve_evidence", current)
    result = unified_retriever.retrieve_chunks(
        "price", as_of_time="2026-09-13", allowed_doc_types={"market_observation"},
    )
    assert [item["document_id"] for item in result["items"]] == ["market:current"]
    assert result["metadata"]["retrieval_mode"] == "live_corpus_fallback"
    assert seen["as_of_time"] == "2026-09-13"
    assert seen["allowed_doc_types"] == {"market_observation"}


def test_current_question_reads_new_arrivals_without_waiting_for_embedding(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.models import RagEvidence, RagSearchResponse

    def slow_snapshot(*args, **kwargs):
        pytest.fail("current facts must not depend on snapshot/model availability")

    monkeypatch.setattr(unified_retriever, "retrieve_semantic_chunks", slow_snapshot)
    monkeypatch.setattr(rag, "retrieve_evidence", lambda query, **kwargs: RagSearchResponse(
        query=query, documents=[RagEvidence(doc_id="new-arrival", doc_type="news_article",
        source_id="official", tier="A", title="New arrival", summary="Published after index build")],
        evidence_level="A", confidence=0.8,
    ))
    result = unified_retriever.retrieve_chunks("今天上游成本压力怎么看？")
    assert result["items"][0]["document_id"] == "new-arrival"
    assert result["metadata"]["retrieval_mode"] == "live_current_corpus"

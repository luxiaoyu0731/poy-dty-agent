from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.retrieval_metrics import RelevanceJudgement, retrieval_ranking_metrics
from app.semantic_embedding import EmbeddingConfig, EmbeddingService
from app.semantic_index import IndexDocument, SemanticIndexBuilder, retrieve_semantic_chunks
from app.settings import settings


@pytest.fixture()
def isolated_eval_db(tmp_path: Path):
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "rag-eval.db"))
    yield
    object.__setattr__(settings, "sqlite_path", original)


class ConceptBackend:
    def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        del kind
        return [_concept_vector(text) for text in texts]


class OfflineBackend:
    def embed(self, texts: list[str], *, kind: str) -> list[list[float]]:
        del texts, kind
        raise RuntimeError("forced lexical ablation")


def _concept_vector(text: str) -> list[float]:
    lowered = text.lower()
    if any(term in lowered for term in ("成本压力", "涨价", "crude", "原油")):
        return [1.0, 0.0, 0.0, 0.0]
    if any(term in lowered for term in ("库存", "inventory")):
        return [0.0, 1.0, 0.0, 0.0]
    if any(term in lowered for term in ("开工", "operating")):
        return [0.0, 0.0, 1.0, 0.0]
    return [0.0, 0.0, 0.0, 1.0]


def _config(*, fallback_policy: str = "error") -> EmbeddingConfig:
    return EmbeddingConfig(
        provider="test",
        model="fixed-multilingual-concepts",
        model_version="1",
        dimensions=4,
        normalization=True,
        batch_size=8,
        device="cpu",
        timeout_seconds=2.0,
        fallback_policy=fallback_policy,
        query_prefix="query: ",
        document_prefix="passage: ",
    )


def _doc(doc_id: str, text: str, source_id: str) -> IndexDocument:
    return IndexDocument(
        document_id=doc_id,
        source_kind="market_observation",
        source_id=source_id,
        title=text,
        body=text,
        observed_at="2026-07-01T00:00:00+00:00",
        visible_at="2026-07-01T00:00:00+00:00",
        evidence_level="B",
        review_status="approved",
    )


def test_hybrid_semantic_improves_synonym_recall_over_lexical_only(
    isolated_eval_db: None,
) -> None:
    semantic = EmbeddingService(_config(), ConceptBackend())
    SemanticIndexBuilder(semantic).rebuild(
        [
            _doc("relevant", "WTI涨价推高石脑油及聚酯原料负担", "eia"),
            _doc("inventory", "港口库存回落", "ccf"),
            _doc("operating", "聚酯装置开工下滑", "industry"),
            _doc("noise", "企业发布年度报告", "company"),
        ]
    )
    hybrid = retrieve_semantic_chunks(
        "上游成本压力",
        limit=3,
        embedding_service=semantic,
    )
    lexical = retrieve_semantic_chunks(
        "上游成本压力",
        limit=3,
        embedding_service=EmbeddingService(
            replace(_config(), fallback_policy="lexical_only"),
            OfflineBackend(),
        ),
    )
    judgement = [RelevanceJudgement("relevant", 3)]
    hybrid_metrics = retrieval_ranking_metrics(
        [item["document_id"] for item in hybrid["items"]],
        judgement,
        k=3,
    )
    lexical_metrics = retrieval_ranking_metrics(
        [item["document_id"] for item in lexical["items"]],
        judgement,
        k=3,
    )

    assert hybrid["metadata"]["retrieval_mode"] == "hybrid_semantic"
    assert hybrid_metrics["recall@3"] == 1.0
    assert hybrid_metrics["mrr"] > lexical_metrics["mrr"]

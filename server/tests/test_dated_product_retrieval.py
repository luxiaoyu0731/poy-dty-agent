import pytest

from app import rag, unified_retriever
from app.models import RagEvidence


def article(doc_id, title, observed, visible="2026-09-12T00:00:00Z"):
    return RagEvidence(
        doc_id=doc_id,
        doc_type="news_article",
        source_id="texnet",
        tier="B",
        title=title,
        summary=title,
        observed_at=observed,
        visible_at=visible,
    )


@pytest.mark.parametrize(
    "date", ["2026-09-11", "2026年9月11日", "September 11 2026", "September 11, 2026", "Sep. 11, 2026"]
)
def test_explicit_date_products_beat_incidental_words_and_preserve_visibility(monkeypatch, date):
    poy = article("article:poy", "生意社9月11日涤纶POY为9242.50元/吨", "2026-09-11T08:00:00Z")
    dty = article("article:dty", "生意社9月11日涤纶DTY为10365.00元/吨", "2026-09-11T08:00:00Z")
    wrong_year = article("article:old", poy.title, "2025-09-11T08:00:00Z")
    unrelated = article(
        "article:oil",
        "Verify historical prices for current oil give each original article citation",
        "2026-09-11T08:00:00Z",
    )
    future = article("article:future", poy.title, "2026-09-11T08:00:00Z", "2026-09-15T00:00:00Z")
    monkeypatch.setattr(rag, "_collect_documents", lambda **_: [unrelated, wrong_year, future, poy, dty])
    query = (
        f"Verify historical SunSirs / Texnet POY and DTY prices for {date}. "
        "Give each price unit and original article citation."
    )
    result = rag.retrieve_evidence(query, limit=2, as_of_time="2026-09-14T08:00:00Z")
    assert {d.doc_id for d in result.documents} == {poy.doc_id, dty.doc_id}
    # The date of the fact must never override when its evidence became visible.
    early = rag.retrieve_evidence(query, limit=20, as_of_time="2026-09-11T23:59:00Z")
    assert not early.documents


def test_chinese_and_iso_query_dates_normalize():
    assert "2026-09-11" in rag._query_terms("POY 2026年9月11日")
    assert "2026-09-11" in rag._query_terms("POY 2026-09-11")
    assert "2026-99-11" not in rag._query_terms("POY 2026-99-11")


@pytest.mark.parametrize("date", ["2026-09-11", "2026年9月11日", "September 11 2026", "September 11, 2026"])
def test_dated_query_reads_live_corpus_without_losing_cutoff(monkeypatch, date):
    called = {}

    def retrieve(query, **kwargs):
        called.update(kwargs)
        from app.models import RagSearchResponse

        return RagSearchResponse(query=query, documents=[], confidence=0, evidence_level="C", coverage={}, warnings=[])

    monkeypatch.setattr(rag, "retrieve_evidence", retrieve)
    monkeypatch.setattr(
        unified_retriever,
        "retrieve_semantic_chunks",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("stale index must not supply dated facts")),
    )
    result = unified_retriever.retrieve_chunks(f"POY {date}", as_of_time="2026-09-12T00:00:00Z", persist_run=False)
    assert called["as_of_time"] == "2026-09-12T00:00:00Z"
    assert result["metadata"]["retrieval_mode"] == "live_dated_corpus"


def test_historically_phrased_dated_query_reads_time_filtered_index_not_live_corpus(monkeypatch):
    def live(query, **kwargs):
        raise AssertionError("historical reconstruction must not read the live corpus")

    monkeypatch.setattr(rag, "retrieve_evidence", live)
    monkeypatch.setattr(
        unified_retriever,
        "retrieve_semantic_chunks",
        lambda query, **kwargs: {
            "status": "ready",
            "items": [{"document_id": "kg:node:PTA", "text": "PTA库存和开工率"}],
            "metadata": {"retrieval_mode": "semantic_index", "index_version": "fixture"},
        },
    )
    result = unified_retriever.retrieve_chunks(
        "截至2026-07-01，当时PTA库存和开工率支持什么判断？",
        as_of_time="2026-07-01T23:59:59+08:00",
        persist_run=False,
    )
    assert result["metadata"]["retrieval_mode"] == "semantic_index"
    assert [item["document_id"] for item in result["items"]] == ["kg:node:PTA"]


def test_explicit_dates_do_not_invent_year_or_accept_invalid_day():
    from app.evidence_time_policy import explicit_query_dates

    assert explicit_query_dates("POY Sep 11") == set()
    assert explicit_query_dates("POY February 30 2026") == set()
    assert explicit_query_dates("POY September 11 2025") == {"2025-09-11"}

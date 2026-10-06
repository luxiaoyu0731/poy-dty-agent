"""Reconstruction tests; synthetic vectors are never experiment evidence."""

from copy import deepcopy
from pathlib import Path

import pytest
from scripts.experiments.alfred_price_snapshot import AlfredPriceArchive
from scripts.experiments.cached_replay_port import digest
from scripts.experiments.frozen_chain_replay import validate_input
from scripts.experiments.reconstructed_corpus import POLICY, candidates_for_day, index_chunks, recall_for
from test_strict_replay_admission import bind, strict_bundle


def reconstructed_bundle(tmp_path):
    result = strict_bundle(tmp_path)
    proof = result["source_admission"]
    doc = proof["archive_source_pack"]["documents"][0]
    source = {
        "kind": "archive",
        "receipt": doc,
        "source_quote": "Production stopped.",
        "reviewed_mechanism": True,
        "review_reason": "Test fixture: stopped production.",
        "category": "supply",
        "origin_group_id": "publisher",
    }
    source["source_id"] = "source-" + digest({"kind": source["kind"], "receipt": doc})[:24]
    sources = [source]
    candidates = candidates_for_day(sources, as_of_time=result["as_of_time"], artifact_root=tmp_path)
    result["signal_report"]["candidates"] = candidates
    result["candidate_visibility"] = {e["event_id"]: e["event_time"] for e in candidates}
    result["case_cards_by_event"] = {e["event_id"]: [] for e in candidates}
    result["empirical_by_event"] = {e["event_id"]: {} for e in candidates}
    vector = [1.0] + [0.0] * 383
    index = {
        "policy": POLICY,
        "built_at": "2026-10-04T00:00:00Z",
        "embedding_mode": "semantic_embedding",
        "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "chunks": index_chunks(sources, artifact_root=tmp_path),
        "vectors": [vector],
    }
    proof.update(
        corpus_policy=POLICY,
        reconstructed_sources=sources,
        reconstructed_index=index,
        query_vectors_by_event={e["event_id"]: vector for e in candidates},
        original_revision_disposition=[
            {"event_revision_id": r["event_revision_id"], "reason": "replaced_by_source_bound_reconstruction"}
            for r in proof["archive_source_pack"]["inventory"]
        ],
    )
    result["memory_recalls"] = [
        recall_for(
            e,
            as_of_time=result["as_of_time"],
            index=index,
            query_vector=vector,
            sources=sources,
            archive=AlfredPriceArchive(proof["price_archive"]),
            artifact_root=tmp_path,
        )
        for e in candidates
    ]
    bind(result)
    return result


def test_proved_reconstruction_does_not_invent_recall_or_cause(tmp_path):
    source = reconstructed_bundle(tmp_path)
    assert len(validate_input(source)) == 64
    assert all(not r["eligible_groups"] for r in source["memory_recalls"])
    assert all(r["retrieval"]["historical_index_existence_claimed"] is False for r in source["memory_recalls"])


@pytest.mark.parametrize(
    "mutation", ["quote", "candidate", "omitted_revision", "index_text", "recall_posterior", "raw"]
)
def test_resealed_unproved_reconstruction_is_rejected(tmp_path, mutation):
    source = deepcopy(reconstructed_bundle(tmp_path))
    p = source["source_admission"]
    if mutation == "quote":
        p["reconstructed_sources"][0]["source_quote"] = "Invented production outage."
    elif mutation == "candidate":
        source["signal_report"]["candidates"] = []
    elif mutation == "omitted_revision":
        p["original_revision_disposition"].pop()
    elif mutation == "index_text":
        p["reconstructed_index"]["chunks"][0]["text"] = "future article"
    elif mutation == "recall_posterior":
        source["memory_recalls"][0]["eligible_groups"] = [{"support_count": 99}]
    else:
        Path(p["reconstructed_sources"][0]["receipt"]["raw_body_path"]).write_text("changed")
    bind(source)
    with pytest.raises(ValueError):
        validate_input(source)


def test_quiet_date_keeps_frozen_roster_and_uses_no_candidate(tmp_path):
    source = reconstructed_bundle(tmp_path)
    p = source["source_admission"]
    source["business_date"] = p["archive_source_pack"]["coverage"][-1]["business_date"]
    source["as_of_time"] = source["business_date"] + "T08:00:00+08:00"
    source["signal_report"]["candidates"] = []
    source["candidate_visibility"] = {}
    source["case_cards_by_event"] = {}
    source["empirical_by_event"] = {}
    source["memory_recalls"] = []
    archive = AlfredPriceArchive(p["price_archive"])
    snapshot = archive.snapshot(as_of_time=source["as_of_time"])
    from scripts.experiments.alfred_price_snapshot import baseline_context

    source["baseline_by_product"] = baseline_context(snapshot)
    p.update(
        price_snapshot_sha256=snapshot["snapshot_sha256"],
        baseline_input_prices=snapshot["prices"],
        baseline_payload_sha256=digest(source["baseline_by_product"]),
    )
    bind(source)
    assert len(validate_input(source)) == 64

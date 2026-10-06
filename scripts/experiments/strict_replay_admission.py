"""Strict raw-source admission for isolated experiments, before any model port.

Recompute source/price checks from evidence, not a caller's success flag. This
is not mechanism or effect acceptance. Historical contexts must separately
carry independently reviewed, source-bound certificates; legacy date anchors
and merely asserted known_at fields do not qualify.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from scripts.experiments.alfred_price_snapshot import (
    AlfredPriceArchive,
    baseline_context,
)
from scripts.experiments.archive_source_audit import audit_cohort_sources
from scripts.experiments.cached_replay_port import digest
from scripts.experiments.historical_case_context import (
    case_context_for_event,
    reconstruct_wikipedia_case,
)


def _clock(value):
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("strict_source_clock_requires_timezone")
    return at


def validate_source_admission(bundle: dict) -> dict:
    proof = bundle.get("source_admission")
    if (
        not isinstance(proof, dict)
        or proof.get("schema_version") != "strict-replay-source-admission.v1"
    ):
        raise ValueError("independent_strict_source_admission_required")
    payload = {key: value for key, value in bundle.items() if key != "source_admission"}
    if proof.get("input_payload_sha256") != digest(payload):
        raise ValueError("strict_source_input_binding_mismatch")
    source_pack = proof.get("archive_source_pack")
    if not isinstance(source_pack, dict):
        raise ValueError("strict_archive_source_pack_required")
    audited = audit_cohort_sources(
        documents=source_pack["documents"],
        inventory=source_pack["inventory"],
        coverage=source_pack["coverage"],
        artifact_root=Path(source_pack["artifact_root"]),
        cohort_receipt=source_pack["cohort_receipt"],
    )
    # Require the complete frozen campaign; a complete local day cannot certify
    # the remaining days or justify purchasing a purported full strict replay.
    reconstructed = (
        proof.get("corpus_policy") == "source-proved-reconstructed-corpus.v1"
    )
    if not reconstructed and not audited["source_availability_complete"]:
        raise ValueError("strict_frozen_cohort_source_coverage_incomplete")
    day = next(
        (
            row
            for row in audited["rows"]
            if row["business_date"] == bundle["business_date"]
        ),
        None,
    )
    if day is None:
        raise ValueError("strict_issuance_not_in_frozen_cohort")
    candidates = bundle["signal_report"].get("candidates", [])
    bindings = proof.get("candidate_revision_bindings") or {}
    if not reconstructed and set(bindings) != {
        event["event_id"] for event in candidates
    }:
        raise ValueError("strict_candidate_binding_mismatch")
    assigned = [revision for revisions in bindings.values() for revision in revisions]
    if not reconstructed and (
        len(set(assigned)) != len(assigned)
        or set(assigned) != set(day["body_proved_revision_ids"])
    ):
        raise ValueError("strict_candidate_revision_set_mismatch")
    for event in [] if reconstructed else candidates:
        sources = [
            doc
            for doc in source_pack["documents"]
            if set(doc["event_revision_ids"]) & set(bindings[event["event_id"]])
        ]
        if not sources or any(
            _clock(doc["available_at_upper_bound"])
            > _clock(bundle["candidate_visibility"][event["event_id"]])
            for doc in sources
        ):
            raise ValueError("strict_candidate_visibility_precedes_source_proof")
        quote = event.get("source_quote")
        if (
            not isinstance(quote, str)
            or not quote.strip()
            or not any(quote in doc["body_text"] for doc in sources)
        ):
            raise ValueError("strict_candidate_original_quote_required")
    archive = AlfredPriceArchive(proof["price_archive"])
    price_snapshot = archive.snapshot(as_of_time=bundle["as_of_time"])
    if (
        not price_snapshot["prices"]
        or proof.get("price_snapshot_sha256") != price_snapshot["snapshot_sha256"]
    ):
        raise ValueError("strict_price_snapshot_binding_mismatch")
    if (
        proof.get("baseline_input_prices") != price_snapshot["prices"]
        or bundle["baseline_by_product"] != baseline_context(price_snapshot)
        or proof.get("baseline_payload_sha256") != digest(bundle["baseline_by_product"])
    ):
        raise ValueError("strict_baseline_source_binding_mismatch")
    cases = []
    case_proofs = []
    for source in proof.get("historical_case_sources", []):
        rebuilt_case = reconstruct_wikipedia_case(
            source["document"],
            source["annotation"],
            archive=archive,
            as_of_time=bundle["as_of_time"],
            artifact_root=Path(source_pack["artifact_root"]),
        )
        cases.append(rebuilt_case["card"])
        case_proofs.append(rebuilt_case["proof"]["proof_sha256"])
    if len({case["case_id"] for case in cases}) != len(cases):
        raise ValueError("duplicate_certified_historical_case")
    for event in candidates:
        cards, empirical = case_context_for_event(cases, event)
        identity = event["event_id"]
        if (
            bundle["case_cards_by_event"][identity] != cards
            or bundle["empirical_by_event"][identity] != empirical
        ):
            raise ValueError("strict_historical_context_source_binding_mismatch")
    # Case cards are backgrounds, not a source-qualified semantic index. The
    # recall certificate path is a separate requirement and cannot be replaced
    # with a caller's eligibility booleans or a current, backdated index.
    if reconstructed:
        from scripts.experiments.reconstructed_corpus import (
            candidates_for_day,
            recall_for,
        )

        sources = proof["reconstructed_sources"]
        root = Path(source_pack["artifact_root"])
        if len({s["source_id"] for s in sources}) != len(sources):
            raise ValueError("duplicate_reconstructed_source")
        # The old revision roster remains intact even when no body was found.
        excluded = proof.get("original_revision_disposition") or []
        inventory = source_pack["inventory"]
        if (
            len(excluded) != len(inventory)
            or {r["event_revision_id"] for r in excluded}
            != {r["event_revision_id"] for r in inventory}
            or any(
                r.get("reason")
                not in {
                    "no_original_body_proof",
                    "replaced_by_source_bound_reconstruction",
                }
                for r in excluded
            )
        ):
            raise ValueError("complete_original_revision_disposition_required")
        expected = candidates_for_day(
            sources, as_of_time=bundle["as_of_time"], artifact_root=root
        )
        if candidates != expected or bundle["candidate_visibility"] != {
            r["event_id"]: r["event_time"] for r in expected
        }:
            raise ValueError("reconstructed_candidate_source_binding_mismatch")
        if bundle.get("memory_recalls"):
            queries = proof["query_vectors_by_event"]
            expected_recalls = [
                recall_for(
                    event,
                    as_of_time=bundle["as_of_time"],
                    index=proof["reconstructed_index"],
                    query_vector=queries[event["event_id"]],
                    sources=sources,
                    archive=archive,
                    artifact_root=root,
                )
                for event in expected
            ]
            if bundle["memory_recalls"] != expected_recalls:
                raise ValueError("reconstructed_recall_source_or_posterior_mismatch")
    elif bundle.get("memory_recalls"):
        raise ValueError("strict_recall_index_and_source_certificates_required")
    return {
        "source_audit_sha256": audited["audit_sha256"],
        "price_snapshot_sha256": price_snapshot["snapshot_sha256"],
        "scope": "candidate_price_and_certified_case_backgrounds_only",
        "historical_case_proofs": case_proofs,
        "historical_context_certified": bool(cases),
        "semantic_recall_certified": reconstructed
        and bool(bundle.get("memory_recalls")),
        "corpus_policy": proof.get("corpus_policy", "original-frozen-candidates.v1"),
        "effect_acceptance_complete": False,
    }

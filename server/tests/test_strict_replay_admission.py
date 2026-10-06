"""Raw-source admission checks; fixtures are deliberately not effect evidence."""

import asyncio
from copy import deepcopy
from datetime import date, timedelta

import pytest
from scripts.experiments.alfred_price_snapshot import AlfredPriceArchive, baseline_context
from scripts.experiments.cached_replay_port import digest
from scripts.experiments.frozen_chain_replay import run_rag_pair, validate_input
from test_alfred_price_snapshot import receipt, row
from test_archive_source_audit import document, seal
from test_frozen_chain_replay import bundle

from app.replay_cohort import build_cohort


def strict_bundle(tmp_path):
    start = date(2024, 10, 1)
    prices = [
        {
            "observation_id": f"p-{i}",
            "observed_day": (start + timedelta(days=i)).isoformat(),
            "value": 100 + i % 5,
            "series_id": "crude",
            "source_id": "official",
            "unit": "USD/bbl",
            "contract_version": "v1",
        }
        for i in range(120)
    ]
    revisions = [
        {
            "event_revision_id": f"r-{i}-{j}",
            "created_day": (start + timedelta(days=i + 1)).isoformat(),
        }
        for i in range(60)
        for j in range(2)
    ]
    cohort = build_cohort(
        observations=prices,
        events=revisions,
        data_sha256="a" * 64,
        window_start="2001-01-01",
        window_end="2025-12-31",
    )
    pool = {r["business_date"]: r for r in cohort["eligible_pool"]}
    coverage = [
        {
            "business_date": day,
            "as_of_time": day + "T08:00:00+08:00",
            "event_revision_ids": [r["event_revision_id"] for r in pool[day]["events"]],
        }
        for day in cohort["selected_dates"]
    ]
    doc = document(tmp_path)
    doc.update(
        snapshot_timestamp="20240930010000",
        archived_url="https://web.archive.org/web/20240930010000/https://publisher.test/oil",
        available_at_upper_bound="2024-09-30T01:00:00Z",
        event_revision_ids=[r["event_revision_id"] for r in revisions],
    )
    seal(doc)
    source = bundle()
    source.update(
        business_date=coverage[0]["business_date"],
        as_of_time=coverage[0]["as_of_time"],
        evidence_basis="verified-original-availability",
        memory_recalls=[],
    )
    source["candidate_visibility"]["event-1"] = doc["available_at_upper_bound"]
    event = source["signal_report"]["candidates"][0]
    event.update(event_time="2024-09-30T01:00:00Z", source_quote="Production stopped.")
    price_archive = receipt(
        [
            row(str(75 + i / 10), start="2024-09-27", observed=(date(2024, 8, 1) + timedelta(days=i)).isoformat())
            for i in range(50)
        ]
    )
    snapshot = AlfredPriceArchive(price_archive).snapshot(as_of_time=source["as_of_time"])
    source["baseline_by_product"] = baseline_context(snapshot)
    source["source_admission"] = {
        "schema_version": "strict-replay-source-admission.v1",
        "archive_source_pack": {
            "documents": [doc],
            "inventory": [{**r, "canonical_url": doc["original_url"]} for r in revisions],
            "coverage": coverage,
            "artifact_root": str(tmp_path),
            "cohort_receipt": cohort,
        },
        "candidate_revision_bindings": {"event-1": coverage[0]["event_revision_ids"]},
        "price_archive": price_archive,
        "price_snapshot_sha256": snapshot["snapshot_sha256"],
        "baseline_input_prices": snapshot["prices"],
        "baseline_payload_sha256": digest(source["baseline_by_product"]),
    }
    bind(source)
    return source


def bind(source):
    source["source_admission"]["input_payload_sha256"] = digest(
        {k: v for k, v in source.items() if k != "source_admission"}
    )


def forbidden(*args):
    pytest.fail("unproved inputs must be rejected before buying model calls")


def test_verified_label_or_success_flag_cannot_admit_unproved_inputs():
    source = bundle()
    source["evidence_basis"] = "verified-original-availability"
    for proof in (None, {"source_availability_complete": True}):
        source["source_admission"] = proof
        with pytest.raises(ValueError, match="independent_strict_source"):
            asyncio.run(run_rag_pair(source, port_factory=forbidden))


def test_actual_raw_proofs_can_pass_but_do_not_assert_rag_exposure(tmp_path):
    source = strict_bundle(tmp_path)
    assert len(validate_input(source)) == 64
    with pytest.raises(ValueError, match="no_qualified_recall"):
        asyncio.run(run_rag_pair(source, port_factory=forbidden))


@pytest.mark.parametrize(
    "mutation", ["payload", "quote", "clock", "raw", "price", "baseline", "binding", "cohort", "context"]
)
def test_changed_evidence_and_legacy_context_cannot_spend(tmp_path, mutation):
    source = deepcopy(strict_bundle(tmp_path))
    if mutation == "payload":
        source["signal_report"]["candidates"][0]["title"] = "Modified after sealing"
    elif mutation == "quote":
        source["signal_report"]["candidates"][0]["source_quote"] = "Invented mechanism"
        bind(source)
    elif mutation == "clock":
        source["candidate_visibility"]["event-1"] = "2001-01-01T00:00:00Z"
        bind(source)
    elif mutation == "raw":
        (tmp_path / "body.html").write_text("Changed cached source")
    elif mutation == "price":
        source["source_admission"]["baseline_input_prices"][0]["value"] = 999
    elif mutation == "baseline":
        source["baseline_by_product"]["crude"]["d7"]["direction"] = "down"
        source["source_admission"]["baseline_payload_sha256"] = digest(source["baseline_by_product"])
        bind(source)
    elif mutation == "binding":
        source["source_admission"]["candidate_revision_bindings"]["event-1"].pop()
    elif mutation == "cohort":
        doc = source["source_admission"]["archive_source_pack"]["documents"][0]
        doc["event_revision_ids"].pop()
        seal(doc)
    else:
        source["case_cards_by_event"]["event-1"] = [{"case_id": "legacy", "known_at": "2001-01-01T00:00:00Z"}]
        bind(source)
    with pytest.raises(ValueError):
        asyncio.run(run_rag_pair(source, port_factory=forbidden))

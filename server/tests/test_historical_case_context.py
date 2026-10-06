import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from scripts.experiments.alfred_price_snapshot import AlfredPriceArchive
from scripts.experiments.frozen_chain_replay import validate_input
from scripts.experiments.historical_case_context import (
    case_context_for_event,
    reconstruct_wikipedia_case,
)
from test_strict_replay_admission import bind, strict_bundle


def historical_source(tmp_path):
    text = "An oil terminal stopped on September 1, 2024."
    path = tmp_path / "old-edition.wiki"
    path.write_text(text)
    packet = tmp_path / "wiki-packet.json"
    packet.write_text(
        json.dumps(
            {
                "case_id": "historical-case",
                "endpoint": "https://en.wikipedia.org/w/api.php",
                "http_status": 200,
                "public_parameters": {
                    "action": "query",
                    "prop": "revisions",
                    "rvdir": "older",
                    "rvlimit": "1",
                    "rvslots": "main",
                    "rvstart": "2024-09-30T23:59:59Z",
                },
                "body": {
                    "query": {
                        "pages": [
                            {
                                "title": "Current title disclosing later events in 2026",
                                "revisions": [
                                    {
                                        "revid": 123,
                                        "timestamp": "2024-09-30T01:00:00Z",
                                        "sha1": hashlib.sha1(text.encode()).hexdigest(),
                                        "slots": {"main": {"contentmodel": "wikitext", "content": text}},
                                    }
                                ],
                            }
                        ]
                    }
                },
            }
        )
    )
    document = {
        "case_id": "historical-case",
        "revision_id": 123,
        "revision_timestamp": "2024-09-30T01:00:00Z",
        "raw_wikitext_path": str(path),
        "wikitext_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "request_packet_path": str(packet),
        "event_date": "2024-09-01",
    }
    annotation = {
        "case_id": "historical-case",
        "event_date": "2024-09-01",
        "event_type": "oil_policy",
        "anchor_quotes": [text],
        "source_date_token": "September 1, 2024",
        "reviewed_event_anchor": True,
    }
    return {"document": document, "annotation": annotation}


def reconstruct(source, bundle, tmp_path):
    return reconstruct_wikipedia_case(
        **source,
        archive=AlfredPriceArchive(bundle["source_admission"]["price_archive"]),
        as_of_time=bundle["as_of_time"],
        artifact_root=tmp_path,
    )


def test_old_edition_and_already_known_returns_create_a_background_not_a_vote(tmp_path):
    bundle = strict_bundle(tmp_path)
    source = historical_source(tmp_path)
    result = reconstruct(source, bundle, tmp_path)
    assert "2026" not in json.dumps(result)
    assert result["card"]["source_revision_url"].endswith("oldid=123")
    assert result["card"]["known_at"] == "2024-09-30T01:00:00+00:00"
    assert set(result["card"]["posterior_result"]["brent"]) == {"1", "7"}
    assert result["proof"]["rag_vote_eligible"] is False
    assert result["proof"]["causal_effect_certified"] is False
    bundle["source_admission"]["historical_case_sources"] = [source]
    cards, stats = case_context_for_event([result["card"]], bundle["signal_report"]["candidates"][0])
    bundle["case_cards_by_event"]["event-1"] = cards
    bundle["empirical_by_event"]["event-1"] = stats
    bind(bundle)
    assert len(validate_input(bundle)) == 64
    # Neither a manually supplied posterior nor a resealed input can alter the
    # outcomes reproduced from the actual dated source and admitted prices.
    bundle["case_cards_by_event"]["event-1"][0]["posterior_result"]["brent"]["7"] = -99
    bind(bundle)
    with pytest.raises(ValueError, match="historical_context_source_binding"):
        validate_input(bundle)


@pytest.mark.parametrize("mutation", ["future", "raw", "quote", "date", "provider", "identity", "path"])
def test_later_versions_fabricated_dates_and_current_source_substitutions_rejected(tmp_path, mutation):
    bundle = strict_bundle(tmp_path)
    source = deepcopy(historical_source(tmp_path))
    doc, annotation = source["document"], source["annotation"]
    packet_path = Path(doc["request_packet_path"])
    packet = json.loads(packet_path.read_text())
    if mutation == "future":
        doc["revision_timestamp"] = "2026-01-01T00:00:00Z"
        packet["body"]["query"]["pages"][0]["revisions"][0]["timestamp"] = doc["revision_timestamp"]
    elif mutation == "raw":
        Path(doc["raw_wikitext_path"]).write_text("Current revised facts")
    elif mutation == "quote":
        annotation["anchor_quotes"] = ["Fabricated supply effect"]
    elif mutation == "date":
        doc["event_date"] = annotation["event_date"] = "2024-09-02"
    elif mutation == "provider":
        packet["endpoint"] = "https://other.test/api.php"
    elif mutation == "identity":
        doc["revision_id"] = 124
    else:
        link = tmp_path / "linked.wiki"
        link.symlink_to(doc["raw_wikitext_path"])
        doc["raw_wikitext_path"] = str(link)
    packet_path.write_text(json.dumps(packet))
    with pytest.raises(ValueError):
        reconstruct(source, bundle, tmp_path)

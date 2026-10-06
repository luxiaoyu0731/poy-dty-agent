import copy

import pytest
from scripts.experiments.cached_replay_port import digest
from scripts.experiments.recall_mechanism_audit import comparable_members, signature, validate_label


def label(identity, *, change="increase", stage="realised", scale="near_term"):
    source = {"source_id": identity, "source_quote": "Company A increased crude oil production."}
    return validate_label(source, {
        "driver":"supply_availability", "change":change, "stage":stage, "scale":scale,
        "quote":source["source_quote"], "subject":"Company A", "action":"increased",
        "reason":"原文报告了产量动作；价格后验不用于分类。",
    })


def test_same_price_sign_is_not_same_physical_mechanism_or_stage():
    labels = {key:label(key, **updates) for key, updates in [
        ("event", {}), ("production", {}), ("attack", {"change":"decrease"}),
        ("permit", {"stage":"announced", "scale":"long_term"}),
    ]}
    receipt = {"event_id":"event", "fragments":[{"chunk_id":k, "doc_id":k} for k in labels if k != "event"]}
    group = {"chunk_ids":["production", "attack", "permit"], "horizon":"d7", "direction":"up"}
    result = comparable_members(receipt, group, labels)
    assert result["accepted_members"] == ["production"]
    assert not result["at_least_three_comparable_members"] and not result["voting_enabled"]
    group["direction"] = "down"
    assert comparable_members(receipt, group, labels) == result


def test_literal_and_scope_failure_cannot_be_relabelled_as_verification():
    original = label("source")
    assert not original["counts_as_evidence"]
    tampered = copy.deepcopy(original)
    tampered["stage"] = "announced"
    with pytest.raises(ValueError, match="integrity"):
        signature(tampered)
    output = {k:v for k,v in original.items() if k not in {"label_sha256"}}
    output["quote"] = "Invented crude oil outage."
    with pytest.raises(ValueError, match="binding"):
        validate_label({"source_id":"source", "source_quote":"Company A increased crude oil production."}, output)


def test_unknown_never_forms_comparable_group_and_three_labels_do_not_authorize_votes():
    labels = {k:label(k) for k in ("event", "a", "b", "c")}
    receipt = {"event_id":"event", "fragments":[{"chunk_id":k, "doc_id":k} for k in "abc"]}
    group = {"chunk_ids":list("abc"), "horizon":"d1"}
    result = comparable_members(receipt, group, labels)
    assert result["at_least_three_comparable_members"] and not result["voting_enabled"]
    labels["event"]["scale"] = "unknown"
    labels["event"]["label_sha256"] = digest({k:v for k,v in labels["event"].items() if k != "label_sha256"})
    assert comparable_members(receipt, group, labels)["accepted_members"] == []

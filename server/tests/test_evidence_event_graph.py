from __future__ import annotations

from copy import deepcopy

from app.evidence_event_graph import project_event_chains


def claim(**changes):
    return {
        "claim_id": "source-1",
        "target": "crude",
        "mechanism": "logistics",
        "subject": "油港",
        "event_date": "2026-10-02",
        "event_date_source": "raw_sentence",
        "state": "actual",
        "semantic_status": "needs_review",
        "expected_direction": None,
        "quote": "2026年10月2日油港停止装运原油，受影响流量尚待核实。",
        "source_url": "https://example.com/report",
        "source_title": "港口停止装运",
        "published_at": "2026-10-02T12:00:00+00:00",
        "known_at": "2026-10-02T12:05:00+00:00",
        "gaps": ["系统未实现物流方向核验"],
        "inference_boundary": "条件性解释",
        **changes,
    }


def dossier(claims, target="poy", accepted=()):
    return {
        "as_of_time": "2026-10-03T08:00:00+08:00",
        "claims": claims,
        "cells": {f"{target}:7": {"current_support": list(accepted), "current_counter": []}},
    }


def test_upstream_hypothesis_does_not_promote_direction_or_change_dossier():
    body = dossier([claim(semantic_status="rule_checked", expected_direction="up")])
    before = deepcopy(body)
    chains = project_event_chains(body, "poy", 7)
    assert len(chains) == 1
    chain = chains[0]
    assert [p["target"] for p in chain["path"]] == ["crude", "naphtha", "px", "pta", "poy"]
    assert chain["relation"] == "upstream_context"
    assert chain["direction"] is None and chain["counts_as_evidence"] is False
    assert chain["quote"] == body["claims"][0]["quote"]
    assert any("时滞" in c for c in chain["conditions"])
    assert body == before


def test_price_background_and_unrelated_upstream_demand_do_not_create_event_chains():
    body = dossier([claim(mechanism="price"), claim(claim_id="demand-1", mechanism="demand")])
    assert project_event_chains(body, "poy", 7) == []


def test_newly_published_old_event_and_future_visibility_are_not_current_materials():
    body = dossier(
        [
            claim(event_date="2025-10-02"),
            claim(claim_id="future", known_at="2026-10-04T12:00:00+00:00"),
            claim(claim_id="badclock", published_at="not a date"),
        ]
    )
    assert project_event_chains(body, "poy", 7) == []


def test_unknown_occurrence_stays_visible_as_unverified_material_not_a_vote():
    body = dossier([claim(event_date=None, event_date_source=None)])
    chain = project_event_chains(body, "poy", 7)[0]
    assert chain["event_date"] is None
    assert not chain["counts_as_evidence"]
    assert any("发生日期未核验" in c for c in chain["conditions"])
    assert chain["proof_kind"] == "unreviewed_material"
    assert chain["path"] == [] and chain["state"] == "unknown"


def test_direct_accepted_fact_reuses_existing_relation_without_new_vote():
    c = claim(semantic_status="rule_checked", expected_direction="down")
    body = dossier([c], target="crude", accepted=[c["claim_id"]])
    chain = project_event_chains(body, "crude", 7)[0]
    assert chain["relation"] == "direct"
    assert chain["counts_as_evidence"] and chain["direction"] == "down"
    assert chain["path"] == [{"target": "crude", "label": "原油"}]


def test_same_source_quote_across_multiple_targets_does_not_multiply_chains():
    body = dossier([claim(), claim(claim_id="naphtha-copy", target="naphtha")])
    assert len(project_event_chains(body, "poy", 7)) == 1


def test_oil_route_meg_is_explicitly_conditional():
    body = dossier([claim(semantic_status="rule_checked")], target="meg")
    chain = project_event_chains(body, "meg", 7)[0]
    assert any("煤制" in c and "气制" in c for c in chain["conditions"])
    assert chain["direction"] is None


def test_scoped_context_does_not_reintroduce_historical_or_unmatched_materials():
    body = dossier([claim(), claim(claim_id="historical", source_url="https://example.com/other")])
    chains = project_event_chains(body, "poy", 7, eligible_ids={"source-1"})
    assert [c["claim_id"] for c in chains] == ["source-1"]
    assert project_event_chains(body, "poy", 7, eligible_ids=set()) == []


def test_direct_claim_has_priority_over_duplicate_upstream_quote():
    direct = claim(claim_id="a-direct", target="poy", semantic_status="rule_checked", expected_direction="up")
    body = dossier([claim(claim_id="z-upstream"), direct], accepted=["a-direct"])
    chains = project_event_chains(body, "poy", 7)
    assert len(chains) == 1 and chains[0]["claim_id"] == "a-direct"
    assert chains[0]["counts_as_evidence"]


def test_old_snapshot_missing_occurrence_cannot_count_even_if_accepted():
    c = claim(event_date=None, semantic_status="rule_checked", expected_direction="up")
    body = dossier([c], target="crude", accepted=[c["claim_id"]])
    chain = project_event_chains(body, "crude", 7)[0]
    assert not chain["counts_as_evidence"] and chain["direction"] is None


def test_event_label_uses_source_subject_and_mechanism_not_price_article_title():
    c = claim(source_title="WTI price today", subject="油港", semantic_status="rule_checked")
    chain = project_event_chains(dossier([c]), "poy", 7)[0]
    assert chain["event_label"] == "运输与交付变化 · 油港"
    assert chain["source_title"] == "WTI price today"


def test_regex_forecast_hits_retain_one_original_without_inventing_subject_or_path():
    first = claim(
        subject="Analysts", state="planned", quote="Analysts raised crude price forecasts after shipping resumed."
    )
    second = claim(**{**first, "claim_id": "policy-copy", "mechanism": "policy"})
    chains = project_event_chains(dossier([first, second]), "poy", 7)
    assert len(chains) == 1
    assert chains[0]["quote"] == first["quote"]
    assert chains[0]["state"] == "unknown" and chains[0]["path"] == []
    assert "Analysts" not in chains[0]["event_label"]
    assert not chains[0]["counts_as_evidence"] and chains[0]["direction"] is None


def test_semantic_paths_reuse_exact_support_atom_and_exclude_future_reviews():
    from test_evidence_semantic_review import fixture

    from app.evidence_semantic_review import validate_review

    article, row = fixture()
    review = validate_review(article, row, reviewed_at="2026-10-02T10:00:00Z", model="test")
    projected = review.model_copy(update={"target": "poy", "relation": "upstream_context"})
    duplicate = projected.model_copy(update={"review_id": "duplicate-mechanism", "mechanism": "policy"})
    future = projected.model_copy(update={"review_id": "future", "reviewed_at": "2026-10-04T00:00:00Z"})
    raw = claim(quote=review.quote, source_url=review.source_url, state="planned", subject="wrong")
    chains = project_event_chains(dossier([raw]), "poy", 7, semantic_reviews=[projected, duplicate, future])
    assert len(chains) == 1
    chain = chains[0]
    assert chain["proof_kind"] == "semantic_review"
    assert chain["semantic_review"] == projected.model_dump()
    assert chain["conditions"] == projected.conditions
    assert chain["quote"] == projected.quote
    assert chain["event_label"] == "运输与交付变化 · 甲公司"
    assert [p["target"] for p in chain["path"]] == ["crude", "naphtha", "px", "pta", "poy"]
    assert not chain["counts_as_evidence"] and chain["direction"] is None


def test_current_state_review_preserves_missing_action_day_and_scope():
    from test_evidence_semantic_review import fixture

    from app.evidence_semantic_review import validate_review

    article, row = fixture("甲公司原油运输管道目前已经关闭。")
    row.update(time_kind="current_state", time_anchor="", start=None, end=None)
    review = validate_review(article, row, reviewed_at="2026-10-02T10:00:00Z", model="test")
    body = dossier([], target="crude")
    chain = project_event_chains(body, "crude", 7, semantic_reviews=[review])[0]
    assert chain["event_date"] is None
    assert chain["semantic_review"]["time_kind"] == "current_state"
    assert not chain["counts_as_evidence"]
    assert project_event_chains(dossier([]), "poy", 7, semantic_reviews=[review]) == []


def test_dossier_cards_and_paths_share_one_review_projection(monkeypatch):
    from test_evidence_dossier import dossier as build
    from test_evidence_dossier import sample
    from test_evidence_semantic_review import fixture

    from app import evidence_dossier_service as service
    from app.evidence_semantic_review import validate_review

    article, row = fixture()
    review = validate_review(article, row, reviewed_at="2026-10-02T10:00:00Z", model="test")
    review = review.model_copy(update={"target": "poy", "relation": "upstream_context"})
    body = build(sample("原油"))
    body["as_of_time"] = "2026-10-03T09:00:00Z"
    calls = []

    def projected(**kwargs):
        calls.append(kwargs)
        return [review]

    monkeypatch.setattr(service, "project_reviews", projected)
    result = service.project_dossier(
        body,
        base={
            "view": "current",
            "target": "poy",
            "horizon_days": 7,
            "scope": "product",
            "as_of_time": body["as_of_time"],
        },
        target="poy",
        horizon=7,
        offset=0,
        limit=1,
    )
    assert calls == [{"target": "poy", "cutoff": body["as_of_time"]}]
    paths = [c for c in result.event_chains if c.proof_kind == "semantic_review"]
    assert len(paths) == 1
    assert paths[0].semantic_review == result.semantic_reviews[0] == review
    assert paths[0].conditions == result.semantic_reviews[0].conditions
    assert paths[0].counts_as_evidence is False


def test_event_projection_includes_scoped_upstream_current_not_historical_claims():
    from test_evidence_dossier import dossier as build
    from test_evidence_dossier import sample

    from app.evidence_dossier_service import project_dossier

    body = build(sample("原油"))
    current = next(c for c in body["claims"] if c["expected_direction"] == "up")
    historical = deepcopy(current)
    historical.update(claim_id="historical-with-recent-date", source_url="https://example.test/history")
    body["claims"].append(historical)
    body["cells"]["poy:7"]["historical_support"] = [
        {
            "claim_id": historical["claim_id"],
            "relation": "support",
            "outcome": {"state": "scored"},
            "use": "retrospective_context_not_historical_forecast_input",
            "causality_proven": False,
        }
    ]
    result = project_dossier(
        body,
        base={"view": "current", "target": "poy", "horizon_days": 7, "scope": "event"},
        target="poy",
        horizon=7,
        offset=0,
        limit=1,
    )
    chain_ids = {c.claim_id for c in result.event_chains}
    assert current["claim_id"] in chain_ids and historical["claim_id"] not in chain_ids
    assert all(c.relation == "upstream_context" and not c.counts_as_evidence for c in result.event_chains)

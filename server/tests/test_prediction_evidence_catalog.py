"""Source and case integrity, direct product grounding and no outcome selection."""

import sqlite3
from contextlib import closing

import pytest
import test_prediction_evidence_inputs as fixture_inputs
from test_prediction_evidence_inputs import EARLY, END, LATE, exported, insert, item

from app.prediction_evidence_catalog import attach_outcomes, build_catalog, reviewed_case, select_analogues
from app.prediction_evidence_inputs import EvidenceVintageBook, digest, timestamp


@pytest.fixture
def database(tmp_path):
    return fixture_inputs.database.__wrapped__(tmp_path)


def catalog(path, *, at=END):
    return build_catalog(EvidenceVintageBook(exported(path)), source_cutoff=timestamp(at), prepared_at=timestamp(END))


def test_source_catalog_recovers_excerpt_without_event_claims(database):
    with closing(sqlite3.connect(database)) as c:
        insert(
            c,
            "items",
            item(revision=2, at=LATE, excerpt="Factory A PTA chemical plant restarted production on 2026-09-21."),
        )
    result = catalog(database)
    assert result["summary"]["units_by_product"] == {"pta": 1}
    assert result["summary"]["reviewed_facts"] == 0
    u = result["units"][0]
    assert u["text"] == "Factory A PTA chemical plant restarted production on 2026-09-21."
    assert u["state"] == "needs_semantic_review" and not u["forecast_feature_approved"]
    assert u["source_spans"][0]["item_revision_id"] == "item-v2"


def test_same_original_collection_paths_do_not_duplicate_units(database):
    text = "Crude oil production at A was reduced on 2026-09-21."
    with closing(sqlite3.connect(database)) as c:
        insert(c, "items", item(revision=2, excerpt=text))
        insert(c, "items", item(item_id="second", item_revision_id="second-v1", excerpt=text))
    result = catalog(database)
    assert len(result["units"]) == 1
    assert result["summary"]["duplicate_collection_spans_removed"] == 1
    assert len(result["units"][0]["source_spans"]) == 2


@pytest.mark.parametrize("text,target", [("尼龙DTY纺织报价为100元/吨", "dty"), ("Phone PTA prices in Pakistan", "pta")])
def test_wrong_material_or_phone_acronym_not_forecast_product(database, text, target):
    with closing(sqlite3.connect(database)) as c:
        insert(c, "items", item(revision=2, excerpt=text))
    unit = catalog(database)["units"][0]
    assert target not in unit["products"] and unit["gaps"]


@pytest.mark.parametrize(
    "text,target",
    [
        ("原油供应减少。", "crude"),
        ("石脑油装置停产。", "naphtha"),
        ("对二甲苯装置复产。", "px"),
        ("PTA化工装置检修。", "pta"),
        ("乙二醇装置恢复。", "meg"),
        ("涤纶POY开工下降。", "poy"),
        ("涤纶DTY需求下降。", "dty"),
    ],
)
def test_all_formal_products_have_direct_span_mapping(database, text, target):
    with closing(sqlite3.connect(database)) as c:
        insert(c, "items", item(revision=2, excerpt=text))
    unit = catalog(database)["units"][0]
    assert unit["products"] == [target] and not unit["forecast_feature_approved"]


@pytest.mark.parametrize("action", ["重启", "停车", "降负", "投产", "产能为30万吨/年"])
def test_chinese_plant_cues_are_recalled_for_review_without_becoming_verified_facts(database, action):
    with closing(sqlite3.connect(database)) as c:
        insert(c, "items", item(revision=2, excerpt=f"乙二醇装置{action}。"))
    result = catalog(database)
    unit = result["units"][0]
    assert unit["mechanism_cue"] and unit["products"] == ["meg"]
    assert unit["state"] == "needs_semantic_review" and result["summary"]["reviewed_facts"] == 0
    assert unit["grounding_scope"] == "stored_intelligence_excerpt"
    assert not unit["raw_publisher_text_verified"] and not unit["forecast_feature_approved"]


def test_catalog_never_backdates_preparation_or_uses_future_sources(database):
    with closing(sqlite3.connect(database)) as c:
        insert(c, "items", item(revision=2, at=LATE, excerpt="Crude oil supply fell."))
    assert catalog(database, at=EARLY)["units"] == []
    assert catalog(database)["units"][0]["prepared_at"] == timestamp(END).isoformat()
    with pytest.raises(ValueError, match="preparation_precedes"):
        build_catalog(
            EvidenceVintageBook(exported(database)), source_cutoff=timestamp(END), prepared_at=timestamp(EARLY)
        )


def test_source_invalidation_never_falls_back(database):
    with closing(sqlite3.connect(database)) as c:
        insert(c, "items", item(revision=2, excerpt="Crude oil supply fell."))
        insert(c, "items", item(revision=3, at=LATE, revision_kind="invalidate"))
    assert catalog(database)["units"] == []


def review_unit():
    text = "Factory A reduced PTA production on 2026-09-10 with high inventory."
    body = {
        "state": "needs_semantic_review",
        "text": text,
        "exact_text_sha256": digest(text),
        "unit_id": "unit-1",
        "source_family": "family-1",
        "source_spans": [
            {
                "item_revision_id": "i-1",
                "payload_sha256": "b" * 64,
                "excerpt_sha256": digest(text),
                "start": 0,
                "end": len(text),
                "source_available_at": EARLY,
            }
        ],
        "products": ["pta"],
        "prepared_at": LATE,
    }
    return {**body, "unit_sha256": digest(body)}


def a_case(*, episode="episode-1", family="family-1", state="actual", review_time=END):
    unit = review_unit()
    unit["source_family"] = family
    unit["unit_sha256"] = digest({k: v for k, v in unit.items() if k != "unit_sha256"})
    return reviewed_case(
        unit,
        target="pta",
        mechanism="supply_loss",
        state=state,
        entity_quote="Factory A",
        action_quote="reduced PTA production",
        effective_date_quote="2026-09-10",
        event_date="2026-09-10",
        conditions={"inventory_regime": "high"},
        condition_quotes={"inventory_regime": "high inventory"},
        episode_id=episode,
        reviewer="fixture-reviewer",
        reviewed_at=timestamp(review_time),
    )


def select(cases, *, at=END):
    return select_analogues(
        cases,
        target="pta",
        mechanism="supply_loss",
        conditions={"inventory_regime": "high"},
        query_event_date="2026-09-24",
        query_episode_id="query",
        query_source_family="query-source",
        as_of=timestamp(at),
    )


def test_review_integrity_and_no_retroactive_availability():
    unit = review_unit()
    unit["text"] = "Changed text"
    with pytest.raises(ValueError, match="untrusted_review_unit"):
        reviewed_case(
            unit,
            target="pta",
            mechanism="supply_loss",
            state="actual",
            entity_quote="Factory A",
            action_quote="reduced",
            effective_date_quote="2026-09-10",
            event_date="2026-09-10",
            conditions={"inventory_regime": "high"},
            condition_quotes={"inventory_regime": "high inventory"},
            episode_id="e",
            reviewer="r",
            reviewed_at=timestamp(END),
        )
    case = a_case()
    assert case["known_at"] == timestamp(END).isoformat()
    assert select([case], at=LATE) == [] and not case["forecast_feature_approved"]


def test_analogue_selection_excludes_plans_self_duplicates_wrong_conditions():
    case = a_case()
    other = a_case(episode="episode-1", family="another")
    wrong = a_case(episode="wrong", family="third")
    wrong["conditions"] = {"inventory_regime": "low"}
    wrong["case_sha256"] = digest({k: v for k, v in wrong.items() if k != "case_sha256"})
    selected = select([case, other, a_case(episode="planned", state="planned"), a_case(episode="query"), wrong])
    assert len(selected) == 1 and selected[0]["episode_id"] == "episode-1"


def outcome(value=110, *, available=END, horizon=1):
    return {
        "target": "pta",
        "series_id": "pta-series",
        "horizon_days": horizon,
        "available_at": available,
        "base_day": "2026-09-10",
        "actual_day": "2026-09-11",
        "base_value": 100,
        "actual_value": value,
        "base_revision_id": "base",
        "actual_revision_id": "actual",
        "price_input_sha256": "a" * 64,
    }


def attach(cases, outcomes):
    return attach_outcomes(
        cases,
        outcomes,
        as_of=timestamp(END),
        horizon_days=1,
        series_id="pta-series",
        hypothesis_direction="up",
        neutral_band=0.01,
    )


def test_historical_relations_are_attached_after_selection():
    cases = [a_case(episode=f"e{i}", family=f"f{i}") for i in range(4)]
    selected = select(cases)
    answers = {case["case_sha256"]: outcome(price) for case, price in zip(cases, (110, 90, 100), strict=False)}
    result = attach(selected, answers)
    assert {x["relation"] for x in result} == {
        "historical_support",
        "historical_counter",
        "neutral_or_incomparable",
        "missing_outcome",
    }
    assert select(cases) == selected


def test_late_wrong_horizon_results_never_become_support():
    a, b = a_case(), a_case(episode="second", family="second")
    results = attach(
        [a, b], {a["case_sha256"]: outcome(available="2026-09-26T10:00:00Z"), b["case_sha256"]: outcome(horizon=7)}
    )
    assert [x["relation"] for x in results] == ["outcome_not_yet_available", "incompatible_outcome"]


def test_price_endpoints_require_future_target_day():
    case = a_case()
    bad = outcome()
    bad["actual_day"] = "2026-09-10"
    with pytest.raises(ValueError, match="invalid_historical_price_endpoints"):
        attach([case], {case["case_sha256"]: bad})


def test_plan_cannot_supply_realized_counter_even_when_outcome_attachment_called_directly():
    case = a_case(state="planned")
    assert attach([case], {case["case_sha256"]: outcome(90)})[0]["relation"] == "not_realized_case"


@pytest.mark.parametrize("horizon", [1, 7, 30])
def test_all_horizon_endpoints_respect_same_contract(horizon):
    from datetime import date, timedelta

    case = a_case()
    actual_day = date(2026, 9, 10) + timedelta(days=horizon)
    row = outcome(110, horizon=horizon, available=actual_day.isoformat() + "T12:00:00Z")
    row["actual_day"] = actual_day.isoformat()
    result = attach_outcomes(
        [case],
        {case["case_sha256"]: row},
        as_of=timestamp("2026-11-01T00:00:00Z"),
        horizon_days=horizon,
        series_id="pta-series",
        hypothesis_direction="up",
        neutral_band=0.01,
    )
    assert result[0]["relation"] == "historical_support"


def test_repetition_inside_one_excerpt_does_not_count_as_multiple_collection_paths(database):
    with closing(sqlite3.connect(database)) as c:
        insert(c, "items", item(revision=2, excerpt="原油供应减少。原油供应减少。"))
    summary = catalog(database)["summary"]
    assert summary["duplicate_collection_spans_removed"] == 0
    assert summary["repeated_spans_within_source_revision"] == 1

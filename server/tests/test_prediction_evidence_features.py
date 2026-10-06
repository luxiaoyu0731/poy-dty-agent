"""Offline four-family input behavior; synthetic fixtures are not market evidence."""

import copy

import pytest

from app.prediction_evidence_catalog import build_catalog, reviewed_case
from app.prediction_evidence_features import (
    bind_research_inputs,
    build_evidence_features,
    make_judgment,
    review_current_relation,
)
from app.prediction_evidence_inputs import (
    AVAILABILITY_POLICY,
    SCHEMA_VERSION,
    EvidenceVintageBook,
    digest,
    timestamp,
)

EARLY = "2026-09-24T08:00:00+00:00"
REVIEW = "2026-09-24T09:00:00+00:00"
END = "2026-09-25T08:00:00+00:00"
FUTURE = "2026-09-26T08:00:00+00:00"


def seal(value, key="content_sha256"):
    body = {k: v for k, v in value.items() if k != key}
    return {**body, key: digest(body)}


def source_book(*, tier="A", invalidated=False, invalidated_at=END):
    records = []
    for n, day in enumerate(("10", "12", "24")):
        source = {
            "item_id": f"item-{n}", "item_revision_id": f"item-{n}-v1", "revision_no": 1,
            "revision_kind": "upsert", "first_seen_at": EARLY, "retrieved_at": EARLY,
            "visible_at": EARLY, "created_at": EARLY, "canonical_url": f"https://example.test/{n}",
            "source_tier": tier,
            "excerpt": f"Factory {n} reduced PTA chemical production on 2026-09-{day} with high inventory.",
            "title": "Actual PTA plant production", "content_status": "present",
        }
        records.append({"append_seq": n + 1, "payload": source, "payload_sha256": digest(source)})
    if invalidated:
        body = dict(records[-1]["payload"], item_revision_id="item-2-v2", revision_no=2,
                    revision_kind="invalidate", visible_at=invalidated_at, retrieved_at=invalidated_at)
        records.append({"append_seq": 4, "payload": body, "payload_sha256": digest(body)})
    tables = {}
    for kind in ("items", "events", "links"):
        rows = records if kind == "items" else []
        tables[kind] = {"records": rows, "rows_exported": len(rows), "truncated": False}
    return EvidenceVintageBook(seal({
        "schema_version": SCHEMA_VERSION, "availability_policy": AVAILABILITY_POLICY,
        "as_of_time": FUTURE, "complete": True, "tables": tables,
    }))


def prices(*, first_actual=102, cutoff=END):
    records = []
    for n, (day, value) in enumerate((("10", 100), ("11", first_actual), ("12", 102), ("13", 99), ("24", 100))):
        visible = f"2026-09-{day}T08:00:00+00:00"
        records.append({
            "revision_id": f"price-{n}", "observed_at": f"2026-09-{day}", "value": value,
            "visible_at": visible, "created_at": visible, "captured_at": visible,
            "source_id": "fixture", "series_id": "pta.test", "unit": "CNY/mt",
            "source_url": "https://example.test/prices", "evidence_sha256": "a" * 64,
            "payload_hash_verified": True, "instrument_matches": True, "contract_version": "test.v1",
        })
    return seal({
        "schema_version": "prediction-vintages.v1", "as_of_time": cutoff,
        "series": {"pta": {"source_id": "fixture", "series_id": "pta.test", "unit": "CNY/mt",
                           "records": records, "truncated": False}},
    })


def judgment(**changes):
    values = dict(
        target="pta", series_id="pta.test", horizon_days=1, as_of=timestamp(END),
        statement="PTA supply losses may support next-day prices, conditional on inventory.",
        mechanism="supply_loss", expected_direction="up", neutral_band=0.005,
        conditions={"inventory": "high"}, event_date="2026-09-24", episode_id="current",
        source_family="current-hypothesis",
    )
    return make_judgment(**(values | changes))


@pytest.fixture
def inputs():
    book = source_book()
    catalog = build_catalog(book, source_cutoff=timestamp(EARLY), prepared_at=timestamp(REVIEW))
    units = sorted(catalog["units"], key=lambda u: u["text"])
    return dict(judgment=judgment(), book=book, units=units, current_relations=[], cases=[], outcomes={},
                price_export=prices())


def relation(inputs, *, unit_index=2, kind="support", role=None, group="origin-A", at=REVIEW, **changes):
    unit = inputs["units"][unit_index]
    return review_current_relation(
        unit, inputs["judgment"], relation=kind,
        role=role or ("fact_confirmation" if kind == "support" else "fact_conflict"),
        evidence_quote=unit["text"], explanation="Fixture reviewer attests the relation, not automated truth.",
        origin_group=group, reviewer="fixture-reviewer", reviewed_at=timestamp(at),
        valid_from=timestamp(EARLY), valid_until=timestamp(changes.get("valid_until", FUTURE)),
    )


def historical_case(inputs, n=0, *, episode=None):
    unit = inputs["units"][n]
    day = "10" if n == 0 else "12"
    return reviewed_case(
        unit, target="pta", mechanism="supply_loss", state="actual", entity_quote=f"Factory {n}",
        action_quote="reduced PTA chemical production", effective_date_quote=f"2026-09-{day}",
        event_date=f"2026-09-{day}", conditions={"inventory": "high"},
        condition_quotes={"inventory": "high inventory"}, episode_id=episode or f"past-{n}",
        reviewer="fixture-reviewer", reviewed_at=timestamp(REVIEW),
    )


def outcome(inputs, n=0):
    rows = inputs["price_export"]["series"]["pta"]["records"]
    base, actual = rows[n * 2:n * 2 + 2]
    return {
        "target": "pta", "series_id": "pta.test", "horizon_days": 1, "available_at": EARLY,
        "base_day": base["observed_at"], "actual_day": actual["observed_at"],
        "base_value": base["value"], "actual_value": actual["value"],
        "base_revision_id": base["revision_id"], "actual_revision_id": actual["revision_id"],
        "price_input_sha256": inputs["price_export"]["content_sha256"],
    }


def test_four_families_share_one_frozen_identity_without_model_promotion(inputs):
    inputs["current_relations"] = [relation(inputs), relation(inputs, unit_index=1, kind="counter", group="origin-B")]
    inputs["cases"] = [historical_case(inputs, 0), historical_case(inputs, 1)]
    inputs["outcomes"] = {case["case_sha256"]: outcome(inputs, n) for n, case in enumerate(inputs["cases"])}
    frame = build_evidence_features(**inputs)
    assert frame["current"]["current_support_groups"] == frame["current"]["current_counter_groups"] == 1
    assert frame["historical"]["historical_support_cases"] == frame["historical"]["historical_counter_cases"] == 1
    assert frame["historical"]["historical_mature_cases"] == 2
    assert not frame["forecast_feature_approved"] and frame["calibrated_probability"] is None
    joint = bind_research_inputs(inputs["price_export"], frame)
    assert not joint["production_approved"]
    assert len(joint["ablation_columns"]) == 4
    assert joint["price_input"]["content_sha256"] == inputs["price_export"]["content_sha256"]
    assert joint["content_sha256"] == bind_research_inputs(inputs["price_export"], frame)["content_sha256"]


def test_unreviewed_or_missing_is_null_not_counterevidence_or_zero(inputs):
    frame = build_evidence_features(**inputs)
    assert set(frame["current"].values()) == {None}
    assert set(frame["historical"].values()) == {None}
    assert frame["current_status"] == "no_eligible_reviewed_relations"


@pytest.mark.parametrize("role", ["fact_conflict", "premise_failure", "transmission_block", "opposing_driver"])
def test_current_counter_roles_remain_distinct(inputs, role):
    inputs["current_relations"] = [relation(inputs, kind="counter", role=role)]
    values = build_evidence_features(**inputs)["current"]
    assert values["current_counter_groups"] == values[f"current_{role}_groups"] == 1


def test_duplicate_origins_and_aliases_are_transitively_grouped_and_mixed_not_voted(inputs):
    a = relation(inputs, unit_index=0, group="group-A")
    b = relation(inputs, unit_index=1, group="group-B", kind="counter")
    bridge = relation(inputs, unit_index=0, group="group-B")
    inputs["current_relations"] = [a, b, bridge, copy.deepcopy(a)]
    frame = build_evidence_features(**inputs)
    assert frame["current"]["current_mixed_groups"] == 1
    assert frame["current"]["current_support_groups"] == frame["current"]["current_counter_groups"] == 0


@pytest.mark.parametrize("change", [{"horizon_days": 7}, {"target": "poy"}, {"series_id": "pta.other"}])
def test_relation_cannot_be_reused_for_other_target_horizon_or_series(inputs, change):
    original = relation(inputs)
    original["judgment_sha256"] = judgment(**change)["judgment_sha256"]
    inputs["current_relations"] = [seal(original, "relation_sha256")]
    result = build_evidence_features(**inputs)
    assert result["current"]["current_support_groups"] is None
    assert result["gaps"]["relation_for_other_judgment"] == 1


def test_future_review_and_expired_applicability_are_not_current(inputs):
    inputs["current_relations"] = [relation(inputs, at=FUTURE), relation(inputs, valid_until=REVIEW)]
    result = build_evidence_features(**inputs)
    assert result["gaps"]["relation_review_not_yet_available"] == 1
    assert result["gaps"]["relation_outside_applicability_window"] == 1


def test_source_invalidation_excludes_review_but_future_invalidation_does_not(inputs):
    inputs["current_relations"] = [relation(inputs)]
    inputs["book"] = source_book(invalidated=True)
    assert build_evidence_features(**inputs)["current"]["current_support_groups"] is None
    inputs["book"] = source_book(invalidated=True, invalidated_at=FUTURE)
    assert build_evidence_features(**inputs)["current"]["current_support_groups"] == 1


def test_low_source_grade_is_a_quality_gap_not_counterevidence(inputs):
    inputs["book"] = source_book(tier="C")
    catalog = build_catalog(inputs["book"], source_cutoff=timestamp(EARLY), prepared_at=timestamp(REVIEW))
    inputs["units"] = sorted(catalog["units"], key=lambda x: x["text"])
    inputs["current_relations"] = [relation(inputs)]
    frame = build_evidence_features(**inputs)
    assert frame["current"]["current_counter_groups"] is None
    assert frame["gaps"]["relation_source_not_current_verified_AB_span"] == 1


def test_resealed_quote_not_in_source_is_rejected(inputs):
    row = relation(inputs)
    row["quote"] = "Factory never stopped, fabricated text."
    inputs["current_relations"] = [seal(row, "relation_sha256")]
    with pytest.raises(ValueError, match="grounded_relation"):
        build_evidence_features(**inputs)


def test_historical_selection_is_outcome_blind_and_missing_not_counter(inputs):
    case = historical_case(inputs)
    inputs["cases"] = [case]
    no_outcome = build_evidence_features(**inputs)
    assert no_outcome["historical"]["historical_counter_cases"] is None
    inputs["outcomes"] = {case["case_sha256"]: outcome(inputs)}
    with_outcome = build_evidence_features(**inputs)
    assert no_outcome["historical_selection"] == with_outcome["historical_selection"]
    assert with_outcome["historical"]["historical_support_cases"] == 1
    inputs["outcomes"][case["case_sha256"]]["available_at"] = FUTURE
    future = build_evidence_features(**inputs)
    assert future["historical"]["historical_counter_cases"] is None
    assert future["gaps"]["outcome_not_yet_available"] == 1


@pytest.mark.parametrize("field,value", [("actual_value", 500), ("price_input_sha256", "b" * 64),
                                        ("actual_revision_id", "missing")])
def test_price_outcomes_must_match_actual_visible_vintages(inputs, field, value):
    case = historical_case(inputs)
    inputs["cases"] = [case]
    inputs["outcomes"] = {case["case_sha256"]: outcome(inputs) | {field: value}}
    with pytest.raises(ValueError, match="outcome_"):
        build_evidence_features(**inputs)


def test_neutral_outcome_is_not_negative_evidence(inputs):
    inputs["price_export"] = prices(first_actual=100)
    case = historical_case(inputs)
    inputs["cases"] = [case]
    inputs["outcomes"] = {case["case_sha256"]: outcome(inputs)}
    values = build_evidence_features(**inputs)["historical"]
    assert values["historical_neutral_cases"] == 1
    assert values["historical_counter_cases"] == values["historical_support_cases"] == 0


def test_neutral_hypothesis_does_not_relabel_a_rising_case_as_neutral(inputs):
    inputs["judgment"] = judgment(expected_direction="neutral")
    case = historical_case(inputs)
    inputs["cases"] = [case]
    inputs["outcomes"] = {case["case_sha256"]: outcome(inputs)}
    values = build_evidence_features(**inputs)["historical"]
    assert values["historical_incomparable_cases"] == 1
    assert values["historical_neutral_cases"] == 0


def test_overlapping_outcome_intervals_do_not_inflate_historical_sample_count(inputs):
    inputs["judgment"] = judgment(horizon_days=7)
    rows = inputs["price_export"]["series"]["pta"]["records"]
    for n, day, value in ((5, "17", 101), (6, "19", 100)):
        row = dict(rows[0], revision_id=f"price-{n}", observed_at=f"2026-09-{day}", value=value)
        row.update({k: f"2026-09-{day}T08:00:00+00:00" for k in ("created_at", "visible_at", "captured_at")})
        rows.append(row)
    inputs["price_export"] = seal(inputs["price_export"])
    inputs["cases"] = [historical_case(inputs, 0), historical_case(inputs, 1)]
    for n, case in enumerate(inputs["cases"]):
        attached = outcome(inputs, n)
        actual = rows[5 + n]
        attached.update(horizon_days=7, actual_day=actual["observed_at"], actual_value=actual["value"],
                        actual_revision_id=actual["revision_id"])
        inputs["outcomes"][case["case_sha256"]] = attached
    result = build_evidence_features(**inputs)
    assert len(result["historical_selection"]["cases"]) == 2
    assert result["historical"]["historical_mature_cases"] == 1
    assert result["gaps"]["overlapping_price_interval"] == 1


def test_incompatible_horizon_remains_a_gap_not_a_negative_case(inputs):
    case = historical_case(inputs)
    inputs["cases"] = [case]
    inputs["outcomes"] = {case["case_sha256"]: outcome(inputs) | {"horizon_days": 7}}
    result = build_evidence_features(**inputs)
    assert result["historical"]["historical_counter_cases"] is None
    assert result["gaps"]["incompatible_outcome"] == 1


def test_new_body_or_summary_review_cannot_be_backdated(inputs):
    unit = inputs["units"][2]
    with pytest.raises(ValueError, match="review_times"):
        review_current_relation(
            unit, inputs["judgment"], relation="support", role="fact_confirmation",
            evidence_quote=unit["text"], explanation="fixture", origin_group="fixture", reviewer="fixture",
            reviewed_at=timestamp(EARLY), valid_from=timestamp(EARLY), valid_until=timestamp(END),
        )


def test_joint_bundle_detects_mutation_and_mismatched_price_snapshot(inputs):
    frame = build_evidence_features(**inputs)
    with pytest.raises(ValueError, match="joint_research_input_mismatch"):
        bind_research_inputs(prices(first_actual=101), frame)
    frame["current"]["current_support_groups"] = 20
    with pytest.raises(ValueError, match="integrity_failed"):
        bind_research_inputs(inputs["price_export"], frame)


@pytest.mark.parametrize("kind,role", [("background", "context"), ("unresolved", "insufficient_evidence")])
def test_background_review_alone_does_not_turn_missing_direction_into_zero(inputs, kind, role):
    inputs["current_relations"] = [relation(inputs, kind=kind, role=role)]
    frame = build_evidence_features(**inputs)
    assert frame["current_status"] == "context_or_unresolved_only"
    assert set(frame["current"].values()) == {None}
    assert frame["reviewed_origin_groups"] == 1 and frame["directional_origin_groups"] == 0


def test_packet_retains_review_quotes_and_selected_outcome_provenance(inputs):
    inputs["current_relations"] = [relation(inputs)]
    case = historical_case(inputs)
    inputs["cases"] = [case]
    inputs["outcomes"] = {case["case_sha256"]: outcome(inputs)}
    provenance = build_evidence_features(**inputs)["review_provenance"]
    assert provenance["current_relations"] == inputs["current_relations"]
    assert provenance["historical_cases"] == inputs["cases"]
    assert provenance["outcome_records"] == inputs["outcomes"]
    assert {u["unit_id"] for u in provenance["units"]} == {
        case["unit_id"], inputs["current_relations"][0]["unit_id"],
    }


def test_resealed_review_cannot_substitute_an_unrelated_original_link(inputs):
    unit = inputs["units"][2]
    unit["canonical_url"] = "https://unrelated.test/incorrect-citation"
    inputs["units"][2] = seal(unit, "unit_sha256")
    inputs["current_relations"] = [relation(inputs)]
    frame = build_evidence_features(**inputs)
    assert frame["current"]["current_support_groups"] is None
    assert frame["gaps"]["relation_source_not_current_verified_AB_span"] == 1


@pytest.mark.parametrize("change", [{"horizon_days": True}, {"neutral_band": False}, {"conditions": {"x": 1}}])
def test_invalid_judgment_types_fail_closed(change):
    with pytest.raises(ValueError, match="invalid_judgment"):
        judgment(**change)

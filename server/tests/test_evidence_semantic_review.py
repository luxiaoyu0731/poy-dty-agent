import hashlib
import json

import pytest

from app.evidence_semantic_review import POLICY, project_reviews, validate_review
from app.prediction_inputs import digest


def fixture(quote="2026年10月2日，甲公司原油运输管道已关闭。", mechanism="logistics", direction="up"):
    article = {
        "article_id": "x",
        "title": "原油运输公告",
        "raw_text": quote + "\n" + "完整的原始公告正文，材料仅作来源报道，不证明价格结果。" * 10,
        "tier": "A",
        "canonical_url": "https://example.com/notice",
        "published_at": "2026-10-02T08:00:00+00:00",
        "first_seen_at": "2026-10-02T08:01:00+00:00",
        "created_at": "2026-10-02T08:01:00+00:00",
        "raw": {},
    }
    article["content_hash"] = hashlib.sha256((article["title"] + "\n" + article["raw_text"]).encode()).hexdigest()
    row = {
        "quote": quote,
        "subject": "甲公司",
        "action": "关闭",
        "source_target": "crude",
        "mechanism": mechanism,
        "direction": direction,
        "driver": "supply_availability",
        "driver_change": "decrease",
        "state": "actual",
        "time_kind": "explicit_day",
        "time_anchor": "2026年10月2日",
        "start": "2026-10-02",
        "end": "2026-10-02",
        "rationale": "运输受阻可能减少可交付供给，其他条件相同下支撑原油压力。",
        "conditions": ["需要核验实际受影响流量和替代运输"],
    }
    return article, row


def test_exact_source_review_is_conditional_not_vote():
    a, r = fixture()
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")
    assert out.counts_as_evidence is False
    assert out.period_start == "2026-10-02"
    assert out.assessment == "ai_semantic_review_not_verified_outcome"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a, r: r.update(quote="甲公司原油运输管道已关闭（伪造）"),
        lambda a, r: r.update(subject="乙公司"),
        lambda a, r: r.update(action="恢复"),
        lambda a, r: r.update(start="2026-10-01"),
        lambda a, r: r.update(time_anchor="昨天"),
        lambda a, r: r.update(source_target="poy"),
        lambda a, r: r.update(mechanism="price"),
        lambda a, r: r.update(state="planned"),
        lambda a, r: a.update(tier="C"),
        lambda a, r: a.update(content_hash="0" * 64),
        lambda a, r: a.update(first_seen_at="2027-01-01T00:00:00+00:00"),
    ],
)
def test_fabrication_scope_and_time_rejected(mutate):
    a, r = fixture()
    mutate(a, r)
    with pytest.raises(ValueError):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


@pytest.mark.parametrize("prefix", ["据传闻，", "如果", "预计", "The company may ", "The company denied "])
def test_dropped_qualifier_never_admitted(prefix):
    a, r = fixture()
    a["raw_text"] = prefix + a["raw_text"]
    a["content_hash"] = hashlib.sha256((a["title"] + "\n" + a["raw_text"]).encode()).hexdigest()
    with pytest.raises(ValueError):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_current_state_not_fake_event_date():
    a, r = fixture("甲公司原油运输管道目前已经关闭。")
    r.update(time_kind="current_state", time_anchor="", start=None, end=None)
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")
    assert out.period_start is None
    r["start"] = "2026-10-02"
    with pytest.raises(ValueError):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_cached_projection_is_scoped_fresh_and_no_provider(tmp_path, monkeypatch):
    a, r = fixture()
    packet = {
        "policy": POLICY,
        "articles": [{"article": a, "reviews": [r, r], "reviewed_at": "2026-10-03T08:00:00+00:00", "model": "test"}],
    }
    packet["content_sha256"] = digest(packet)
    (tmp_path / "latest.json").write_text(json.dumps(packet))
    monkeypatch.setenv("EVIDENCE_SEMANTIC_REVIEW_DIR", str(tmp_path))
    assert len(project_reviews(target="crude", cutoff="2026-10-03T09:00:00+00:00")) == 1
    poy = project_reviews(target="poy", cutoff="2026-10-03T09:00:00+00:00")
    assert len(poy) == 1 and poy[0].relation == "upstream_context" and not poy[0].counts_as_evidence
    assert project_reviews(target="poy", cutoff="2026-10-03T07:00:00+00:00") == []
    assert project_reviews(target="poy", cutoff="2026-10-12T09:00:00+00:00") == []
    assert project_reviews(target="poy", cutoff="2026-10-03T09:00:00+00:00", scope="event") == []
    packet["articles"][0]["reviews"][0]["quote"] = "tampered"
    (tmp_path / "latest.json").write_text(json.dumps(packet))
    assert project_reviews(target="poy", cutoff="2026-10-03T09:00:00+00:00") == []


def test_different_product_contrast_cannot_inherit_crude_direction():
    quote = (
        "Even as crude oil cargoes have been pushing through the Strait, "
        "LNG cargoes have been blocked at the chokepoint."
    )
    a, r = fixture(quote)
    r.update(subject="LNG cargoes", action="blocked", time_kind="current_state", time_anchor="", start=None, end=None)
    with pytest.raises(ValueError, match="clause_mismatch"):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_budget_reservation_counts_failed_attempts_and_survives_restart(tmp_path):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "review_worker", Path(__file__).parents[1] / "scripts/review_evidence_semantics.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    budget = module.ReviewBudget(tmp_path, 1)
    budget.reserve(0.6)
    another = module.ReviewBudget(tmp_path, 350)
    with pytest.raises(RuntimeError, match="budget_exhausted"):
        another.reserve(0.5)
    state = json.loads((tmp_path / "budget.json").read_text())
    assert state == {"cap_cny": 1, "reserved_cny": 0.6, "attempts": 1}


def test_source_anchor_v3_frozen_rebuild_preserved():
    from test_evidence_dossier import sample
    from test_prediction_evidence_runtime import prices

    from app.evidence_dossier import SOURCE_ANCHOR_POLICY
    from app.prediction_evidence_runtime import joint_input
    from app.prediction_main import MainInputSnapshot

    exported = joint_input(prices(), sample(), include_dossier=True, dossier_policy=SOURCE_ANCHOR_POLICY)
    assert MainInputSnapshot(exported).exported == exported


@pytest.mark.parametrize("verb,pressure", [("increase", "down"), ("decrease", "up")])
def test_english_inventory_real_report_both_sides(verb, pressure):
    from app.evidence_mechanism_checks import check_mechanism

    quote = (
        f"Crude oil inventories in the United States saw an {verb} of 900,000 barrels "
        "during the week ending September 25, according to EIA."
    )
    # Comma thousands are an explicitly tested format, not a guessed value.
    result = check_mechanism(quote, quote, "inventory", "2026-09-25")
    assert result and result[0] == pressure
    assert (
        check_mechanism("Analysts forecast that " + quote, "Analysts forecast that " + quote, "inventory", "2026-09-25")
        is None
    )


@pytest.mark.parametrize(
    "driver,change,direction",
    [
        ("supply_availability", "increase", "up"),
        ("supply_availability", "decrease", "down"),
        ("inventory_level", "increase", "up"),
        ("demand_quantity", "decrease", "up"),
    ],
)
def test_physical_quantity_is_not_price_pressure(driver, change, direction):
    a, r = fixture()
    r.update(driver=driver, driver_change=change, direction=direction)
    with pytest.raises(ValueError, match="pressure_inverted"):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_physical_driver_cannot_contradict_literal_recovery():
    a, r = fixture("甲公司原油运输管道目前已经恢复。")
    r.update(action="恢复", time_kind="current_state", time_anchor="", start=None, end=None)
    with pytest.raises(ValueError, match="driver_inconsistent"):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_announced_policy_is_not_executed_supply_or_invented_day():
    a, r = fixture(
        "G7 has agreed to release crude oil stocks over the next four months.", mechanism="policy", direction="down"
    )
    r.update(
        subject="G7",
        action="agreed to release",
        driver_change="increase",
        state="planned",
        fact_stage="announced",
        time_kind="reported_announcement",
        time_anchor="",
        start=None,
        end=None,
    )
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")
    assert out.fact_stage == "announced" and not out.counts_as_evidence and out.period_start is None
    r["action"] = "release"
    with pytest.raises(ValueError, match="not_decision"):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_event_requires_literal_exposure_anchor_and_not_other_commodity():
    quote = "Iran has announced restrictions in the Strait of Hormuz."
    scope = "Crude oil cargoes use the Strait of Hormuz."
    a, r = fixture(quote + scope, mechanism="policy")
    r.update(
        quote=quote,
        subject="Iran",
        action="announced restrictions",
        fact_stage="announced",
        scope_quote=scope,
        scope_entity="Strait of Hormuz",
        time_kind="reported_announcement",
        time_anchor="",
        start=None,
        end=None,
    )
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")
    assert out.scope_quote == scope and not out.counts_as_evidence
    r["scope_entity"] = "Saudi Arabia"
    with pytest.raises(ValueError):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_date_only_publication_uses_first_seen_availability():
    a, r = fixture()
    a["published_at"] = "2026-10-02"
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")
    assert out.published_at == a["first_seen_at"]


@pytest.mark.parametrize(
    "subject,action,quote,mechanism",
    [
        ("Resumption", "increase", "Resumption of crude shipments is set to increase global oil supply", "supply"),
        (
            "OPEC+",
            "has been raising nominal production quotas",
            "OPEC+ has been raising nominal production quotas for crude",
            "policy",
        ),
        (
            "Disruptions",
            "have sent diesel crack spreads",
            "Disruptions to refining have sent diesel crack spreads—the premium over crude—higher",
            "supply",
        ),
    ],
)
def test_crude_supply_cannot_inherit_future_quotas_or_diesel(subject, action, quote, mechanism):
    a, r = fixture(quote, mechanism=mechanism, direction="down")
    r.update(
        subject=subject,
        action=action,
        driver_change="increase",
        time_kind="current_state",
        time_anchor="",
        start=None,
        end=None,
    )
    with pytest.raises(ValueError):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_late_ingestion_does_not_make_source_month_complete():
    a, r = fixture("September crude oil exports have decreased.", mechanism="supply")
    a.update(published_at="2026-09-29", first_seen_at="2026-10-02T08:00:00+00:00")
    r.update(
        subject="crude oil exports",
        action="decreased",
        time_kind="report_period",
        time_anchor="September",
        start="2026-09-01",
        end="2026-09-30",
    )
    with pytest.raises(ValueError):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


@pytest.mark.parametrize(
    "action,direction,change", [("have climbed", "down", "increase"), ("have declined", "up", "decrease")]
)
def test_reported_export_quantity_motion_preserves_price_pressure(action, direction, change):
    a, r = fixture(f"Crude oil exports {action}.", mechanism="supply", direction=direction)
    r.update(
        subject="Crude oil exports",
        action=action,
        driver_change=change,
        time_kind="current_state",
        time_anchor="",
        start=None,
        end=None,
    )
    result = validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")
    assert result.direction == direction
    assert result.counts_as_evidence is False


def association_fixture():
    from app.evidence_semantic_bindings import POLICY as BINDING_POLICY

    quote = "The vessels have been attacked this week."
    scope = "Among these vessels is the crude oil tanker Atlas."
    article, row = fixture(quote + " " + scope)
    row.update(
        quote=quote,
        subject="vessels",
        action="have been attacked",
        scope_quote=scope,
        scope_entity="Atlas",
        time_kind="report_period",
        time_anchor="this week",
        start="2026-09-28",
        end="2026-10-02",
    )
    proof = {
        "policy": BINDING_POLICY,
        "source_content_hash": article["content_hash"],
        "proposal_sha256": digest(row),
        "binding_quote": quote + " " + scope,
        "model": "independent-review",
        "reviewed_at": "2026-10-03T07:00:00+00:00",
        "verdict": {
            "associated": True,
            "current_in_source": True,
            "conditional_reasoning": True,
            "reason": "原文明确把原油运输船列为这些遇袭船只的成员，交付影响尚需核验。",
        },
    }
    proof["content_sha256"] = digest(proof)
    row["binding_proof"] = proof
    return article, row


def test_cross_sentence_association_is_labelled_and_has_no_vote():
    a, r = association_fixture()
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")
    assert out.binding_method == "ai_coreference"
    assert out.binding_model == "independent-review"
    assert out.binding_quote in a["raw_text"]
    assert out.period_start == "2026-09-28"
    assert out.period_end == "2026-10-02"
    assert out.counts_as_evidence is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(direction="down"),
        lambda r: r["binding_proof"].update(source_content_hash="0" * 64),
        lambda r: r["binding_proof"]["verdict"].update(associated=False),
        lambda r: r["binding_proof"]["verdict"].update(current_in_source=False),
        lambda r: r["binding_proof"].update(binding_quote="invented source"),
        lambda r: r["binding_proof"].update(reviewed_at="2026-10-04T00:00:00+00:00"),
    ],
)
def test_association_proof_tampering_or_negative_critic_never_admits(mutate):
    a, r = association_fixture()
    mutate(r)
    # Even a re-sealed receipt cannot bypass provenance or a negative critic.
    proof = r["binding_proof"]
    proof.pop("content_sha256")
    proof["content_sha256"] = digest(proof)
    with pytest.raises(ValueError):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_week_interval_does_not_invent_an_event_day_or_complete_future_week():
    a, r = association_fixture()
    r.update(end="2026-10-04")
    proof = r["binding_proof"]
    proof["proposal_sha256"] = digest({k: v for k, v in r.items() if k != "binding_proof"})
    proof.pop("content_sha256")
    proof["content_sha256"] = digest(proof)
    with pytest.raises(ValueError, match="semantic_period_not_completed"):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_numeric_commas_do_not_split_subject_from_inventory_action():
    a, r = fixture(
        "2026年10月2日，Crude oil inventories rose by 1,019,000 barrels.", mechanism="inventory", direction="down"
    )
    r.update(
        subject="Crude oil inventories",
        action="rose by 1,019,000 barrels",
        driver="inventory_level",
        driver_change="increase",
    )
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")
    assert out.direction == "down" and not out.counts_as_evidence


def test_week_interval_uses_publisher_calendar_not_ingestion_or_shanghai_next_day():
    a, r = association_fixture()
    a.update(published_at="2026-10-04T23:30:00-05:00", first_seen_at="2026-10-05T04:31:00+00:00")
    r.update(end="2026-10-04")
    proof = r["binding_proof"]
    proof.update(
        reviewed_at="2026-10-05T05:00:00+00:00",
        proposal_sha256=digest({k: v for k, v in r.items() if k != "binding_proof"}),
    )
    proof.pop("content_sha256")
    proof["content_sha256"] = digest(proof)
    out = validate_review(a, r, reviewed_at="2026-10-05T06:00:00+00:00", model="test")
    assert (out.period_start, out.period_end) == ("2026-09-28", "2026-10-04")


def test_corrupt_optional_association_file_cannot_erase_valid_primary_material(tmp_path, monkeypatch):
    a, r = fixture()
    packet = {
        "policy": POLICY,
        "articles": [{"article": a, "reviews": [r], "reviewed_at": "2026-10-03T08:00:00+00:00", "model": "test"}],
    }
    packet["content_sha256"] = digest(packet)
    (tmp_path / "latest.json").write_text(json.dumps(packet))
    (tmp_path / "associations.json").write_text("corrupt json")
    monkeypatch.setenv("EVIDENCE_SEMANTIC_REVIEW_DIR", str(tmp_path))
    assert len(project_reviews(target="crude", cutoff="2026-10-03T09:00:00+00:00")) == 1


def test_later_association_review_stays_out_of_earlier_cutoff(tmp_path, monkeypatch):
    from app.evidence_semantic_bindings import POLICY as BINDING_POLICY

    a, r = association_fixture()
    packet = {"policy": POLICY, "articles": []}
    packet["content_sha256"] = digest(packet)
    sidecar = {
        "policy": BINDING_POLICY,
        "articles": [{"article": a, "reviews": [r], "reviewed_at": "2026-10-03T08:00:00+00:00", "model": "test"}],
    }
    sidecar["content_sha256"] = digest(sidecar)
    (tmp_path / "latest.json").write_text(json.dumps(packet))
    (tmp_path / "associations.json").write_text(json.dumps(sidecar))
    monkeypatch.setenv("EVIDENCE_SEMANTIC_REVIEW_DIR", str(tmp_path))
    assert project_reviews(target="crude", cutoff="2026-10-03T06:00:00+00:00") == []
    out = project_reviews(target="crude", cutoff="2026-10-03T09:00:00+00:00")
    assert len(out) == 1 and out[0].binding_method == "ai_coreference"


def test_announced_mixed_release_can_bind_explicit_crude_without_treating_total_as_crude():
    a, r = fixture(
        "The G7 have agreed to release emergency diesel and crude stocks over four months.",
        mechanism="policy",
        direction="down",
    )
    r.update(
        subject="G7",
        action="have agreed to release",
        state="planned",
        fact_stage="announced",
        driver_change="increase",
        time_kind="reported_announcement",
        time_anchor="",
        start=None,
        end=None,
    )
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")
    assert out.fact_stage == "announced" and not out.counts_as_evidence
    assert any("混合品种" in x for x in out.conditions)


def test_source_span_alignment_preserves_original_and_rejects_word_edits():
    from app.evidence_semantic_bindings import literal_span

    raw = 'The “Atlas”\n\tcarrier was hit on October 2.'
    assert literal_span(raw, 'The "Atlas" carrier was hit on October 2.') == raw
    assert literal_span(raw, 'The "Atlas" carrier was hit on October 3.') is None
    assert literal_span('A\nB / A\tB', 'A B') is None


def test_english_action_requires_complete_word_not_substring():
    from app.evidence_semantic_bindings import complete_anchor

    assert complete_anchor('The G7 agreed to release crude.', 'agreed')
    assert not complete_anchor('The G7 disagreed to release crude.', 'agreed')
    assert not complete_anchor('The port is unblocked.', 'blocked')
    assert complete_anchor('港口关闭。', '关闭')


@pytest.mark.parametrize('negation', ["hasn't", 'hasn’t', "didn't", "won't"])
def test_english_contracted_negation_cannot_become_supply_fact(negation):
    a, r = fixture(f'2026年10月2日，Crude oil pipeline {negation} closed.')
    r.update(subject='Crude oil pipeline', action='closed')
    with pytest.raises(ValueError):
        validate_review(a, r, reviewed_at='2026-10-03T08:00:00+00:00', model='test')


def test_until_now_background_is_not_a_current_transport_constraint():
    a, r = fixture("Until now, refiners steered clear of using tankers to bring in crude oil because of attacks.")
    r.update(
        subject="refiners", action="steered clear", time_kind="current_state", time_anchor="", start=None, end=None
    )
    with pytest.raises(ValueError, match="semantic_past_background_not_current"):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


@pytest.mark.parametrize("qualifier", ["if the permit is granted", "but officials denied the closure"])
def test_qualifier_after_truncated_quote_is_retained(qualifier):
    quote = "On October 2, 2026, Atlas crude oil pipeline closed"
    a, r = fixture(quote)
    a["raw_text"] = quote + ", " + qualifier + ".\n" + "Original source background. " * 20
    a["content_hash"] = hashlib.sha256((a["title"] + "\n" + a["raw_text"]).encode()).hexdigest()
    r.update(subject="Atlas", action="closed", time_anchor="October 2, 2026")
    with pytest.raises(ValueError, match="semantic_action"):
        validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")


def test_separate_price_forecast_does_not_pollute_current_source_fact():
    quote = "U.S. crude oil inventories are currently lower"
    a, r = fixture(quote, mechanism="inventory")
    a["raw_text"] = quote + ". Prices may rise next month.\n" + "Original source background. " * 20
    a["content_hash"] = hashlib.sha256((a["title"] + "\n" + a["raw_text"]).encode()).hexdigest()
    r.update(subject="U.S. crude oil inventories", action="lower", driver="inventory_level",
             time_kind="current_state", time_anchor="", start=None, end=None)
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00+00:00", model="test")
    assert out.quote == quote and not out.counts_as_evidence


def test_multi_product_capture_validates_once_and_observes_manifest_replacement(tmp_path, monkeypatch):
    from app import evidence_semantic_review as semantic

    article, row = fixture()
    packet = {
        "policy": POLICY,
        "articles": [{"article": article, "reviews": [row], "reviewed_at": "2026-10-03T08:00:00Z", "model": "test"}],
    }
    packet["content_sha256"] = digest(packet)
    manifest = tmp_path / "latest.json"
    manifest.write_text(json.dumps(packet))
    monkeypatch.setenv("EVIDENCE_SEMANTIC_REVIEW_DIR", str(tmp_path))
    calls = []
    original = semantic.validate_review

    def counted(*args, **kwargs):
        calls.append(args[0]["content_hash"])
        return original(*args, **kwargs)

    monkeypatch.setattr(semantic, "validate_review", counted)
    projected = semantic.project_reviews_many(targets=["crude", "poy", "dty"], cutoff="2026-10-03T09:00:00Z")
    assert len(calls) == 1
    assert projected["crude"][0].relation == "direct"
    assert projected["poy"][0].relation == "upstream_context"
    assert all(not row.counts_as_evidence for rows in projected.values() for row in rows)
    assert len(projected["poy"][0].conditions) == len(projected["crude"][0].conditions) + 1
    manifest.write_text(json.dumps({**packet, "content_sha256": "corrupt"}))
    assert semantic.project_reviews_many(targets=["crude", "poy"], cutoff="2026-10-03T09:00:00Z") == {
        "crude": [],
        "poy": [],
    }


def test_week_ending_report_is_a_source_anchored_interval_not_publication_day():
    quote = "Atlas crude oil inventories rose in the week ending September 25, 2026."
    a, r = fixture(quote, mechanism="inventory", direction="down")
    r.update(subject="Atlas crude oil inventories", action="rose", driver="inventory_level",
             driver_change="increase", time_kind="report_period", time_anchor="week ending September 25, 2026",
             start="2026-09-19", end="2026-09-25")
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00Z", model="test")
    assert (out.period_start, out.period_end) == ("2026-09-19", "2026-09-25")
    assert out.published_at.startswith("2026-10-02") and not out.counts_as_evidence
    for wrong in ({"start": "2026-09-25"}, {"end": "2026-10-02"}, {"time_anchor": "week ending September 26, 2026"}):
        with pytest.raises(ValueError, match="semantic_period"):
            validate_review(a, {**r, **wrong}, reviewed_at="2026-10-03T08:00:00Z", model="test")


def test_ambiguous_or_future_week_ending_date_is_not_admitted():
    for date_text in ("September 25-27, 2026", "October 5, 2026", "last Friday"):
        quote = f"Atlas crude oil inventories rose in the week ending {date_text}."
        a, r = fixture(quote, mechanism="inventory", direction="down")
        r.update(subject="Atlas crude oil inventories", action="rose", driver="inventory_level",
                 driver_change="increase", time_kind="report_period", time_anchor=f"week ending {date_text}",
                 start="2026-09-19", end="2026-09-25")
        with pytest.raises(ValueError, match="semantic_period"):
            validate_review(a, r, reviewed_at="2026-10-03T08:00:00Z", model="test")



def test_current_month_to_date_export_change_is_not_a_fake_occurrence_day():
    quote = "Atlas crude oil exports have risen to 12.8 million barrels daily this month."
    a, r = fixture(quote, mechanism="supply", direction="down")
    a.update(
        published_at="2026-09-28T08:00:00Z", first_seen_at="2026-09-28T08:01:00Z", created_at="2026-09-28T08:01:00Z"
    )
    r.update(subject="Atlas crude oil exports", action="have risen", driver_change="increase",
             time_kind="report_period", time_anchor="this month", start="2026-09-01", end="2026-09-28")
    out = validate_review(a, r, reviewed_at="2026-10-03T08:00:00Z", model="test")
    assert out.period_start == "2026-09-01" and out.period_end == "2026-09-28"
    assert out.direction == "down" and not out.counts_as_evidence
    with pytest.raises(ValueError, match="semantic_period_not_completed"):
        validate_review(a, {**r, "end":"2026-09-30"}, reviewed_at="2026-10-03T08:00:00Z", model="test")


def test_recent_release_keeps_reporting_period_separate_from_event_recency(tmp_path, monkeypatch):
    quote="Atlas crude oil inventories rose in the week ending September 25, 2026."
    a, r=fixture(quote, mechanism="inventory", direction="down")
    r.update(
        subject="Atlas crude oil inventories",
        action="rose",
        driver="inventory_level",
        driver_change="increase",
        time_kind="report_period",
        time_anchor="week ending September 25, 2026",
        start="2026-09-19",
        end="2026-09-25",
    )
    packet={"policy":POLICY,"articles":[{"article":a,"reviews":[r],"reviewed_at":"2026-10-03T08:00:00Z","model":"test"}]}
    packet["content_sha256"]=digest(packet)
    (tmp_path/"latest.json").write_text(json.dumps(packet))
    monkeypatch.setenv("EVIDENCE_SEMANTIC_REVIEW_DIR",str(tmp_path))
    assert len(project_reviews(target="crude",cutoff="2026-10-04T08:00:00Z")) == 1
    assert project_reviews(target="crude",cutoff="2026-10-10T08:00:00Z") == []
    # A delayed release about a months-old weekly period remains inadmissible.
    a.update(published_at="2026-10-20T08:00:00Z",first_seen_at="2026-10-20T08:01:00Z",created_at="2026-10-20T08:01:00Z")
    with pytest.raises(ValueError,match="semantic_reporting_period_too_old"):
        validate_review(a,r,reviewed_at="2026-10-21T08:00:00Z",model="test")



def test_strategic_reserve_drawdown_cannot_use_commercial_inventory_sign():
    quote="Atlas crude stocks in the Strategic Petroleum Reserve are currently lower."
    a,r=fixture(quote,mechanism="inventory")
    r.update(
        subject="Atlas crude stocks",
        action="lower",
        driver="inventory_level",
        time_kind="current_state",
        time_anchor="",
        start=None,
        end=None,
    )
    with pytest.raises(ValueError,match="semantic_strategic_stock_not_commercial_inventory"):
        validate_review(a,r,reviewed_at="2026-10-03T08:00:00Z",model="test")


def test_import_proxy_explains_unverified_consumption_and_stock_channel():
    quote="Atlas crude oil imports rose in September."
    a,r=fixture(quote,mechanism="demand")
    r.update(subject="Atlas crude oil imports",action="rose",driver="demand_quantity",driver_change="increase",
             time_kind="report_period",time_anchor="September",start="2026-09-01",end="2026-09-30")
    out=validate_review(a,r,reviewed_at="2026-10-03T08:00:00Z",model="test")
    assert any("加工消费或补库" in x for x in out.conditions) and not out.counts_as_evidence

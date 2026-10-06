"""Offline, version-bound evidence features for the single forecast contract.

Reviews are semantic attestations, not truth established by a hash. Counts are
diagnostics/features, never votes, probabilities or permission for live use.
No database, provider, scheduler or production forecast caller lives here.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime

from .industrial_intelligence.identity import detrack_url
from .prediction_evidence_catalog import MECHANISMS, PRODUCTS, attach_outcomes, reviewed_case, select_analogues
from .prediction_evidence_inputs import EvidenceVintageBook, availability, digest, timestamp
from .prediction_replay import load_export

FEATURE_POLICY = "reviewed-four-family-features.v1"
ROLES = {
    "support": {"fact_confirmation", "mechanism_support"},
    "counter": {"fact_conflict", "premise_failure", "transmission_block", "opposing_driver"},
    "background": {"context"},
    "unresolved": {"insufficient_evidence"},
}
CURRENT_COLUMNS = (
    "current_support_groups", "current_counter_groups", "current_mixed_groups",
    "current_fact_conflict_groups", "current_premise_failure_groups",
    "current_transmission_block_groups", "current_opposing_driver_groups",
)
HISTORY_COLUMNS = (
    "historical_support_cases", "historical_counter_cases", "historical_neutral_cases",
    "historical_incomparable_cases", "historical_mature_cases", "historical_mean_return",
)


def _sealed(body: dict, key: str) -> dict:
    return {**body, key: digest(body)}


def _check(value: dict, key: str) -> None:
    if value.get(key) != digest({k: v for k, v in value.items() if k != key}):
        raise ValueError(f"{key}:integrity_failed")


def make_judgment(
    *, target: str, series_id: str, horizon_days: int, as_of: datetime,
    statement: str, mechanism: str, expected_direction: str, neutral_band: float,
    conditions: dict[str, str], event_date: str, episode_id: str, source_family: str,
) -> dict:
    if (target not in PRODUCTS or type(horizon_days) is not int or horizon_days not in (1, 7, 30)
            or mechanism not in MECHANISMS
            or expected_direction not in ("up", "down", "neutral")
            or isinstance(neutral_band, bool) or not isinstance(neutral_band, (int, float))
            or not math.isfinite(neutral_band) or not 0 <= neutral_band < 1
            or not all(isinstance(x, str) and x.strip() for x in (series_id, statement, episode_id, source_family))
            or not conditions or any(not isinstance(k, str) or not k.strip()
                                     or not isinstance(v, str) or not v.strip() for k, v in conditions.items())):
        raise ValueError("invalid_judgment_contract")
    return _sealed({
        "schema_version": "evidence-judgment.v1", "forecast_contract": "issue-calendar.v1",
        "target": target, "series_id": series_id, "horizon_days": horizon_days,
        "as_of_time": timestamp(as_of.isoformat()).isoformat(), "statement": statement,
        "mechanism": mechanism, "expected_direction": expected_direction, "neutral_band": neutral_band,
        "conditions": dict(sorted(conditions.items())), "event_date": date.fromisoformat(event_date).isoformat(),
        "episode_id": episode_id, "source_family": source_family,
    }, "judgment_sha256")


def review_current_relation(
    unit: dict, judgment: dict, *, relation: str, role: str, evidence_quote: str,
    explanation: str, origin_group: str, reviewer: str, reviewed_at: datetime,
    valid_from: datetime, valid_until: datetime,
) -> dict:
    """Record an explicit semantic review; exact quoting alone is insufficient."""
    _check(unit, "unit_sha256")
    _check(judgment, "judgment_sha256")
    if (unit.get("state") != "needs_semantic_review" or unit.get("exact_text_sha256") != digest(unit["text"])
            or relation not in ROLES or role not in ROLES[relation]
            or not evidence_quote.strip() or evidence_quote not in unit["text"]
            or not all(x.strip() for x in (explanation, origin_group, reviewer))):
        raise ValueError("explicit_grounded_relation_review_required")
    known = timestamp(reviewed_at.isoformat())
    start, end = timestamp(valid_from.isoformat()), timestamp(valid_until.isoformat())
    if known < timestamp(unit["prepared_at"]) or start > end:
        raise ValueError("invalid_relation_review_times")
    return _sealed({
        "schema_version": "reviewed-current-relation.v1", "judgment_sha256": judgment["judgment_sha256"],
        "unit_id": unit["unit_id"], "unit_sha256": unit["unit_sha256"],
        "source_family": unit["source_family"], "origin_group": origin_group,
        "relation": relation, "role": role, "quote": evidence_quote, "explanation": explanation,
        "reviewer": reviewer, "known_at": known.isoformat(), "valid_from": start.isoformat(),
        "valid_until": end.isoformat(), "semantic_status": "reviewer_attested",
        "forecast_feature_approved": False,
    }, "relation_sha256")


def _source_bound(unit: dict, heads: dict) -> bool:
    _check(unit, "unit_sha256")
    if unit.get("exact_text_sha256") != digest(unit["text"]):
        raise ValueError("source_span_hash_mismatch")
    for span in unit["source_spans"]:
        record = heads.get(span["item_revision_id"])
        if not record or record["payload_sha256"] != span["payload_sha256"]:
            continue
        source = record["payload"]
        text = str(source.get("excerpt") or "")
        family = digest({"original_url": detrack_url(source["canonical_url"])})
        if (span["excerpt_sha256"] == digest(text) and family == unit["source_family"]
                and detrack_url(unit["canonical_url"]) == detrack_url(source["canonical_url"])
                and span["source_tier"] == source.get("source_tier")
                and span.get("published_at") == source.get("published_at")
                and 0 <= span["start"] < span["end"] <= len(text)
                and text[span["start"]:span["end"]] == unit["text"]
                and timestamp(span["source_available_at"]) == availability("items", source)
                and timestamp(unit["prepared_at"]) >= availability("items", source)
                and source.get("source_tier") in {"A", "B"}):
            return True
    return False


def _current_groups(rows: list[dict]) -> list[list[dict]]:
    """Transitive origin/family dedup: aliases cannot inflate independent counts."""
    groups: list[list[dict]] = []
    for row in sorted(rows, key=lambda r: r["relation_sha256"]):
        merged, remaining = [row], []
        for group in groups:
            if any(x["origin_group"] == row["origin_group"] or x["source_family"] == row["source_family"]
                   for x in group):
                merged.extend(group)
            else:
                remaining.append(group)
        groups = [*remaining, merged]
    return groups


def _check_price_outcome(outcome: dict, price_export: dict, points: dict, cutoff: datetime) -> None:
    # A future outcome is retained as unavailable, without reading its value as a feature.
    if timestamp(outcome["available_at"]) > cutoff:
        return
    if outcome.get("price_input_sha256") != price_export["content_sha256"]:
        raise ValueError("outcome_price_input_mismatch")
    for prefix in ("base", "actual"):
        point = points.get(outcome.get(f"{prefix}_revision_id"))
        if (point is None or point.value != outcome.get(f"{prefix}_value")
                or point.observed_at[:10] != outcome.get(f"{prefix}_day")
                or timestamp(point.visible_at) > timestamp(outcome["available_at"])):
            raise ValueError("outcome_not_bound_to_visible_price_revision")


def build_evidence_features(
    *, judgment: dict, book: EvidenceVintageBook, units: list[dict],
    current_relations: list[dict], cases: list[dict], outcomes: dict[str, dict], price_export: dict,
) -> dict:
    """Freeze four separate feature families; insufficient data remains missing."""
    _check(judgment, "judgment_sha256")
    cutoff = timestamp(judgment["as_of_time"])
    rebuilt_judgment = make_judgment(
        **{k: judgment[k] for k in (
            "target", "series_id", "horizon_days", "statement", "mechanism", "expected_direction",
            "neutral_band", "conditions", "event_date", "episode_id", "source_family",
        )}, as_of=cutoff,
    )
    if judgment != rebuilt_judgment:
        raise ValueError("judgment_contract_mismatch")
    series = load_export(price_export)
    target = series.get(judgment["target"])
    if (timestamp(price_export["as_of_time"]) != cutoff or target is None
            or target.identity["series_id"] != judgment["series_id"]):
        raise ValueError("price_evidence_cutoff_or_series_mismatch")
    source_view = book.source_view(cutoff)
    heads = {r["payload"]["item_revision_id"]: r for r in source_view["items"]}
    by_unit = {u["unit_id"]: u for u in units}
    if len(by_unit) != len(units):
        raise ValueError("duplicate_source_unit")
    bound = {key: _source_bound(u, heads) for key, u in by_unit.items()}
    gaps = Counter()
    admitted = []
    for row in current_relations:
        _check(row, "relation_sha256")
        unit = by_unit.get(row["unit_id"])
        if (row.get("schema_version") != "reviewed-current-relation.v1"
                or row["judgment_sha256"] != judgment["judgment_sha256"]):
            gaps["relation_for_other_judgment"] += 1
        elif timestamp(row["known_at"]) > cutoff:
            gaps["relation_review_not_yet_available"] += 1
        elif not timestamp(row["valid_from"]) <= cutoff <= timestamp(row["valid_until"]):
            gaps["relation_outside_applicability_window"] += 1
        elif (not unit or row["unit_sha256"] != unit["unit_sha256"] or not bound[row["unit_id"]]):
            gaps["relation_source_not_current_verified_AB_span"] += 1
        else:
            rebuilt = review_current_relation(
                unit, judgment, relation=row["relation"], role=row["role"], evidence_quote=row["quote"],
                explanation=row["explanation"], origin_group=row["origin_group"], reviewer=row["reviewer"],
                reviewed_at=timestamp(row["known_at"]), valid_from=timestamp(row["valid_from"]),
                valid_until=timestamp(row["valid_until"]),
            )
            if rebuilt != row:
                raise ValueError("relation_contract_mismatch")
            admitted.append(row)
    groups = _current_groups(admitted)
    directional_groups = [g for g in groups if any(r["relation"] in {"support", "counter"} for r in g)]
    current = dict.fromkeys(CURRENT_COLUMNS, 0 if directional_groups else None)
    for group in groups:
        relations = {r["relation"] for r in group} & {"support", "counter"}
        if len(relations) == 2:
            current["current_mixed_groups"] += 1
        elif relations:
            kind = next(iter(relations))
            current[f"current_{kind}_groups"] += 1
            if kind == "counter":
                for role in {r["role"] for r in group if r["relation"] == "counter"}:
                    current[f"current_{role}_groups"] += 1
    verified_cases = []
    for case in cases:
        _check(case, "case_sha256")
        unit = by_unit.get(case["unit_id"])
        if (not unit or not bound[case["unit_id"]] or case["source_spans"] != unit["source_spans"]
                or case["source_family"] != unit["source_family"]):
            gaps["historical_source_not_current_verified_AB_span"] += 1
        else:
            rebuilt = reviewed_case(
                unit, **{k: case[k] for k in (
                    "target", "mechanism", "state", "entity_quote", "action_quote", "effective_date_quote",
                    "event_date", "conditions", "condition_quotes", "episode_id", "reviewer",
                )}, reviewed_at=timestamp(case["known_at"]),
            )
            if rebuilt != case:
                raise ValueError("historical_case_contract_mismatch")
            verified_cases.append(case)
    selected = select_analogues(
        verified_cases, target=judgment["target"], mechanism=judgment["mechanism"],
        conditions=judgment["conditions"], query_event_date=judgment["event_date"],
        query_episode_id=judgment["episode_id"], query_source_family=judgment["source_family"], as_of=cutoff,
    )
    # This digest depends only on pre-outcome attributes and selected case IDs.
    selection = _sealed({
        "judgment_sha256": judgment["judgment_sha256"],
        "cases": [c["case_sha256"] for c in selected], "policy": "catalog-outcome-blind.v1",
    }, "selection_sha256")
    points = {p.observation_id: p for p in target.view(cutoff).points}
    for case in selected:
        outcome = outcomes.get(case["case_sha256"])
        if (outcome and outcome.get("target") == judgment["target"]
                and outcome.get("series_id") == judgment["series_id"]
                and outcome.get("horizon_days") == judgment["horizon_days"]):
            _check_price_outcome(outcome, price_export, points, cutoff)
    attached = attach_outcomes(
        selected, outcomes, as_of=cutoff, horizon_days=judgment["horizon_days"],
        series_id=judgment["series_id"], hypothesis_direction=judgment["expected_direction"],
        neutral_band=judgment["neutral_band"],
    )
    intervals, mature = [], []
    for row in attached:
        if row["relation"] not in {"historical_support", "historical_counter", "neutral_or_incomparable"}:
            gaps[row["relation"]] += 1
            continue
        outcome = outcomes[row["case_sha256"]]
        start, end = date.fromisoformat(outcome["base_day"]), date.fromisoformat(outcome["actual_day"])
        if any(start <= b and a <= end for a, b in intervals):
            row["feature_exclusion"] = "overlapping_price_interval"
            gaps["overlapping_price_interval"] += 1
            continue
        intervals.append((start, end))
        mature.append(row)
    history = dict.fromkeys(HISTORY_COLUMNS, None)
    if mature:
        counts = Counter(r["relation"] for r in mature)
        history.update(
            historical_support_cases=counts["historical_support"],
            historical_counter_cases=counts["historical_counter"],
            historical_neutral_cases=sum(r["direction"] == "neutral" for r in mature),
            historical_incomparable_cases=sum(
                r["relation"] == "neutral_or_incomparable" and r["direction"] != "neutral" for r in mature
            ),
            historical_mature_cases=len(mature),
            historical_mean_return=sum(r["change"] for r in mature) / len(mature),
        )
    return _sealed({
        "schema_version": FEATURE_POLICY, "forecast_contract": "issue-calendar.v1",
        "as_of_time": cutoff.isoformat(), "judgment": judgment,
        "price_input_sha256": price_export["content_sha256"], "evidence_input_sha256": book.sha256,
        "source_view_sha256": source_view["input_sha256"], "current": current, "historical": history,
        "current_status": ("reviewed_relations_present" if directional_groups else
                           "context_or_unresolved_only" if groups else "no_eligible_reviewed_relations"),
        "historical_status": "mature_cases_present" if mature else "no_eligible_mature_cases",
        "reviewed_origin_groups": len(groups), "directional_origin_groups": len(directional_groups),
        "current_relations": sorted({r["relation_sha256"] for r in admitted}),
        "historical_selection": selection, "historical_outcomes": attached,
        # Preserve actual annotations and spans, not only opaque hashes. The
        # immutable source book remains the separately retained raw dependency.
        "review_provenance": {
            "current_relations": sorted(admitted, key=lambda r: r["relation_sha256"]),
            "historical_cases": selected,
            "units": sorted(
                [by_unit[key] for key in {r["unit_id"] for r in [*admitted, *selected]}],
                key=lambda u: u["unit_id"],
            ),
            "outcome_records": {c["case_sha256"]: outcomes[c["case_sha256"]]
                                for c in selected if c["case_sha256"] in outcomes},
            "submitted_reviews_sha256": digest({
                "units": units, "current_relations": current_relations, "cases": cases, "outcomes": outcomes,
            }),
        },
        "gaps": dict(sorted(gaps.items())), "calibrated_probability": None,
        "grounding_scope": "stored_intelligence_excerpt", "raw_publisher_text_verified": False,
        "forecast_feature_approved": False, "data_mode": "offline_reviewed_reconstruction",
    }, "content_sha256")


def bind_research_inputs(price_export: dict, features: dict) -> dict:
    """Freeze one joint input identity; no second forecast authority or live hook."""
    load_export(price_export)
    _check(features, "content_sha256")
    if (features.get("schema_version") != FEATURE_POLICY
            or price_export["content_sha256"] != features["price_input_sha256"]
            or timestamp(price_export["as_of_time"]) != timestamp(features["as_of_time"])):
        raise ValueError("joint_research_input_mismatch")
    return _sealed({
        "schema_version": "price-evidence-research-input.v1", "forecast_contract": "issue-calendar.v1",
        "as_of_time": features["as_of_time"], "price_input": price_export, "evidence_features": features,
        "ablation_columns": {
            "price_only": [], "price_current": list(CURRENT_COLUMNS),
            "price_history": list(HISTORY_COLUMNS), "price_current_history": [*CURRENT_COLUMNS, *HISTORY_COLUMNS],
        },
        "missing_value_policy": "explicit_null_requires_training_fold_policy",
        "production_approved": False,
    }, "content_sha256")

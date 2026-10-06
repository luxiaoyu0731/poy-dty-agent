"""Source-grounded preparation for seven-product current and historical evidence.

No model, database, web or production callers. Sentence spans are review units,
NOT automatically verified atomic facts. Never derive truth or price direction
from a headline, source tier, keyword, or an existing model inference.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .industrial_intelligence import analysis, identity
from .prediction_evidence_inputs import EvidenceVintageBook, availability, digest, timestamp

CATALOG_POLICY = "source-span-candidates.v3-mechanism-recall"
CASE_POLICY = "reviewed-mechanism-cases.v1"
PRODUCTS = frozenset(identity.PRODUCT_IDS)
MECHANISMS = frozenset(
    (
        "supply_loss",
        "supply_gain",
        "inventory_draw",
        "inventory_build",
        "demand_gain",
        "demand_loss",
        "logistics_disruption",
        "logistics_recovery",
        "policy_tightening",
        "policy_easing",
    )
)
STATES = frozenset(("actual", "planned", "denied", "static_background", "unknown"))
SHANGHAI = ZoneInfo("Asia/Shanghai")
# Delimit spans without breaking decimal prices or losing original offsets.
BOUNDARY = re.compile(r"[。；;\n]+|(?<=[.!?])\s+(?=[A-Z])")
NYLON = re.compile(r"锦纶|尼龙|\b(?:nylon|polyamide)\b", re.I)
POLYESTER = re.compile(r"涤纶|聚酯|\bpolyester\b", re.I)
NONCHEMICAL = re.compile(
    r"\b(?:phone|smartphone|telecom|pakistan telecommunication|parent.teacher)\b|手机|通讯管理局", re.I
)
CHEMICAL = re.compile(
    r"聚酯|化工|石化|苯二甲酸|乙二醇|\b(?:polyester|petrochemical|terephthalic|glycol|paraxylene)\b", re.I
)
MECHANISM_CUES = re.compile(
    r"停产|复产|重启|停车|停工|复工|投产|产能|检修|扩产|开工|降负|提负|减产|增产|"
    r"供应|库存|需求|运费|封锁|制裁|取消|恢复|"
    r"\b(?:shutdown|restart\w*|outage|production|capacity|supply|inventor\w*|demand|"
    r"export\w*|import\w*|blockade|sanction\w*|reopen\w*)\b",
    re.I,
)


def _spans(text: str):
    start = 0
    for boundary in (*BOUNDARY.finditer(text), None):
        end = len(text) if boundary is None else boundary.start()
        a, b = start, end
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if b > a:
            yield a, b, text[a:b]
        if boundary is not None:
            start = boundary.end()


def _products(text: str) -> tuple[list[str], list[str]]:
    products = analysis.detect_products(text)
    gaps = []
    if NYLON.search(text) and set(products) & {"poy", "dty"}:
        products = [p for p in products if p not in {"poy", "dty"}]
        gaps.append("mixed_materials_require_review" if POLYESTER.search(text) else "nylon_is_not_polyester_target")
    if NONCHEMICAL.search(text) and not CHEMICAL.search(text) and set(products) & {"px", "pta", "meg", "poy", "dty"}:
        products = [p for p in products if p not in {"px", "pta", "meg", "poy", "dty"}]
        gaps.append("ambiguous_nonchemical_acronym")
    return products, gaps


def build_catalog(book: EvidenceVintageBook, *, source_cutoff: datetime, prepared_at: datetime) -> dict:
    """A current preparation artifact; it cannot masquerade as a past forecast input."""
    prepared = timestamp(prepared_at.isoformat())
    cutoff = timestamp(source_cutoff.isoformat())
    if prepared < cutoff:
        raise ValueError("preparation_precedes_source_cutoff")
    view = book.source_view(cutoff)
    units = {}
    gaps = Counter()
    documents = 0
    for row in view["items"]:
        item = row["payload"]
        excerpt = str(item.get("excerpt") or "")
        url = identity.detrack_url(str(item.get("canonical_url") or ""))
        if not excerpt.strip() or not url.startswith(("https://", "http://")):
            gaps["no_excerpt_or_source_url"] += 1
            continue
        documents += 1
        title = str(item.get("title") or "")
        family = digest({"original_url": url})
        for start, end, text in _spans(excerpt):
            # Exact source URL + text dedupes collection paths; different text
            # versions are retained and cannot be presumed independent.
            key = digest({"source_family": family, "text": text})
            products, flags = _products(text)
            title_products, _ = _products(title)
            unit = units.setdefault(
                key,
                {
                    "unit_id": key,
                    "source_family": family,
                    "canonical_url": url,
                    "text": text,
                    "exact_text_sha256": digest(text),
                    "products": products,
                    "title_only_products": sorted(set(title_products) - set(products)),
                    "mechanism_cue": bool(MECHANISM_CUES.search(text)),
                    "gaps": flags,
                    "state": "needs_semantic_review",
                    "forecast_feature_approved": False,
                    # Canonical excerpts may be validated model summaries;
                    # this exact-span check is not a raw-publisher quotation.
                    "grounding_scope": "stored_intelligence_excerpt",
                    "raw_publisher_text_verified": False,
                    "prepared_at": prepared.isoformat(),
                    "source_spans": [],
                },
            )
            unit["source_spans"].append(
                {
                    "item_revision_id": item["item_revision_id"],
                    "payload_sha256": row["payload_sha256"],
                    "source_available_at": availability("items", item).isoformat(),
                    "published_at": item.get("published_at"),
                    "source_tier": item.get("source_tier"),
                    "excerpt_sha256": digest(excerpt),
                    "start": start,
                    "end": end,
                }
            )
    entries = sorted(units.values(), key=lambda x: x["unit_id"])
    for unit in entries:
        unit["source_spans"].sort(key=lambda x: x["item_revision_id"])
        unit["unit_sha256"] = digest(unit)
    body = {
        "schema_version": CATALOG_POLICY,
        "source_input_sha256": view["input_sha256"],
        "source_cutoff": cutoff.isoformat(),
        "prepared_at": prepared.isoformat(),
        "historical_insert_receipts_verified": False,
        "forecast_feature_approved": False,
        "units": entries,
        "summary": {
            "eligible_source_heads": len(view["items"]),
            "source_documents_with_excerpt": documents,
            "unique_units": len(entries),
            "duplicate_collection_spans_removed": sum(
                len({s["item_revision_id"] for s in x["source_spans"]}) - 1 for x in entries
            ),
            "repeated_spans_within_source_revision": sum(
                len(x["source_spans"]) - len({s["item_revision_id"] for s in x["source_spans"]}) for x in entries
            ),
            "units_by_product": dict(Counter(p for x in entries for p in x["products"])),
            "mechanism_candidates_by_product": dict(
                Counter(p for x in entries if x["mechanism_cue"] for p in x["products"])
            ),
            "excluded_sources": dict(gaps),
            "reviewed_facts": 0,
        },
    }
    return {**body, "content_sha256": digest(body)}


def reviewed_case(
    unit: dict,
    *,
    target: str,
    mechanism: str,
    state: str,
    entity_quote: str,
    action_quote: str,
    effective_date_quote: str,
    event_date: str,
    conditions: dict[str, str],
    condition_quotes: dict[str, str],
    episode_id: str,
    reviewer: str,
    reviewed_at: datetime,
) -> dict:
    """Validate explicit local annotations; lexical grounding is not semantic proof.

    Reviewer attests the mechanism/state/date interpretation. Non-actual states
    remain visible but cannot supply a realized historical mechanism case.
    """
    if (
        unit.get("state") != "needs_semantic_review"
        or unit["exact_text_sha256"] != digest(unit["text"])
        or unit.get("unit_sha256") != digest({k: v for k, v in unit.items() if k != "unit_sha256"})
    ):
        raise ValueError("untrusted_review_unit")
    if target not in PRODUCTS or target not in unit["products"]:
        raise ValueError("product_not_grounded_in_span")
    if mechanism not in MECHANISMS or state not in STATES:
        raise ValueError("unsupported_mechanism_or_state")
    if not all(
        isinstance(q, str) and q.strip() and q in unit["text"]
        for q in (entity_quote, action_quote, effective_date_quote)
    ):
        raise ValueError("annotation_not_in_exact_source_span")
    if set(condition_quotes) != set(conditions) or any(
        not isinstance(q, str) or not q.strip() or q not in unit["text"] for q in condition_quotes.values()
    ):
        raise ValueError("condition_not_grounded_in_source_span")
    if not unit.get("source_spans") or any(
        not span.get("item_revision_id")
        or not re.fullmatch(r"[0-9a-f]{64}", str(span.get("payload_sha256") or ""))
        or not re.fullmatch(r"[0-9a-f]{64}", str(span.get("excerpt_sha256") or ""))
        or not isinstance(span.get("start"), int)
        or not isinstance(span.get("end"), int)
        or span["start"] < 0
        or span["end"] - span["start"] != len(unit["text"])
        or timestamp(span["source_available_at"]) > timestamp(unit["prepared_at"])
        for span in unit["source_spans"]
    ):
        raise ValueError("review_unit_provenance_required")
    if (
        not episode_id.strip()
        or not reviewer.strip()
        or not conditions
        or any(
            not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
            for k, v in conditions.items()
        )
    ):
        raise ValueError("review_identity_and_ex_ante_conditions_required")
    day = date.fromisoformat(event_date)
    reviewed = timestamp(reviewed_at.isoformat())
    if reviewed < timestamp(unit["prepared_at"]):
        raise ValueError("review_cannot_precede_preparation")
    if state == "actual" and day > reviewed.astimezone(SHANGHAI).date():
        raise ValueError("future_event_cannot_be_actual")
    body = {
        "schema_version": CASE_POLICY,
        "unit_id": unit["unit_id"],
        "source_family": unit["source_family"],
        "source_spans": unit["source_spans"],
        "target": target,
        "mechanism": mechanism,
        "state": state,
        "entity_quote": entity_quote,
        "action_quote": action_quote,
        "effective_date_quote": effective_date_quote,
        "event_date": day.isoformat(),
        "conditions": dict(sorted(conditions.items())),
        "condition_quotes": dict(sorted(condition_quotes.items())),
        "episode_id": episode_id,
        "reviewer": reviewer,
        "known_at": reviewed.isoformat(),
        "data_mode": "reviewed_historical_reconstruction",
        "forecast_feature_approved": False,
    }
    return {**body, "case_sha256": digest(body)}


def select_analogues(
    cases: list[dict],
    *,
    target: str,
    mechanism: str,
    conditions: dict[str, str],
    query_event_date: str,
    query_episode_id: str,
    query_source_family: str,
    as_of: datetime,
    limit: int = 20,
) -> list[dict]:
    """Freeze cases by known ex-ante attributes BEFORE reading any outcomes.

    Exact condition matching is deliberately conservative. Missing conditions,
    planned/denied events and the query's own episode are not realized analogues.
    """
    cutoff = timestamp(as_of.isoformat())
    if target not in PRODUCTS or mechanism not in MECHANISMS or not conditions or not 1 <= limit <= 100:
        raise ValueError("invalid_analogue_query")
    query_day = date.fromisoformat(query_event_date)
    eligible = []
    for case in cases:
        body = {k: v for k, v in case.items() if k != "case_sha256"}
        if case.get("schema_version") != CASE_POLICY or case.get("case_sha256") != digest(body):
            raise ValueError("case_integrity_failed")
        if (
            case["target"] == target
            and case["mechanism"] == mechanism
            and case["state"] == "actual"
            and timestamp(case["known_at"]) <= cutoff
            and date.fromisoformat(case["event_date"]) < query_day
            and case["episode_id"] != query_episode_id
            and case["source_family"] != query_source_family
            and all(case["conditions"].get(k) == v for k, v in conditions.items())
        ):
            eligible.append(case)
    selected, episodes, families = [], set(), set()
    for case in sorted(eligible, key=lambda x: (x["event_date"], x["case_sha256"]), reverse=True):
        if case["episode_id"] in episodes or case["source_family"] in families:
            continue
        selected.append(case)
        episodes.add(case["episode_id"])
        families.add(case["source_family"])
        if len(selected) == limit:
            break
    return selected


def attach_outcomes(
    selected: list[dict],
    outcomes: dict[str, dict],
    *,
    as_of: datetime,
    horizon_days: int,
    series_id: str,
    hypothesis_direction: str,
    neutral_band: float,
) -> list[dict]:
    """Attach reviewed, same-series price endpoints only after case selection.

    Historical opposite price response challenges transferability; it does not
    refute the occurrence of the original event. Neutral/missing are separate.
    """
    if (
        horizon_days not in (1, 7, 30)
        or hypothesis_direction not in ("up", "down", "neutral")
        or not series_id
        or not math.isfinite(neutral_band)
        or neutral_band < 0
    ):
        raise ValueError("invalid_outcome_contract")
    cutoff = timestamp(as_of.isoformat())
    result = []
    for case in selected:
        if (
            case.get("case_sha256") != digest({k: v for k, v in case.items() if k != "case_sha256"})
            or timestamp(case["known_at"]) > cutoff
        ):
            raise ValueError("case_not_available_at_outcome_cutoff")
        row = {
            "case_sha256": case["case_sha256"],
            "episode_id": case["episode_id"],
            "relation": "missing_outcome",
            "direction": None,
            "change": None,
        }
        if case["state"] != "actual":
            row["relation"] = "not_realized_case"
            result.append(row)
            continue
        outcome = outcomes.get(case["case_sha256"])
        if not outcome:
            result.append(row)
            continue
        if (
            outcome.get("target") != case["target"]
            or outcome.get("series_id") != series_id
            or outcome.get("horizon_days") != horizon_days
        ):
            row["relation"] = "incompatible_outcome"
            result.append(row)
            continue
        if timestamp(outcome["available_at"]) > cutoff:
            row["relation"] = "outcome_not_yet_available"
            result.append(row)
            continue
        event_day = date.fromisoformat(case["event_date"])
        actual_day = date.fromisoformat(outcome["actual_day"])
        base_day = date.fromisoformat(outcome["base_day"])
        values = (outcome["base_value"], outcome["actual_value"])
        if (
            not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v > 0 for v in values
            )
            or not outcome.get("base_revision_id")
            or not outcome.get("actual_revision_id")
            or not re.fullmatch(r"[0-9a-f]{64}", str(outcome.get("price_input_sha256") or ""))
            or base_day > event_day
            or (event_day - base_day).days > 4
            or not event_day + timedelta(days=horizon_days)
            <= actual_day
            <= event_day + timedelta(days=horizon_days + 4)
            or actual_day > timestamp(outcome["available_at"]).astimezone(SHANGHAI).date()
        ):
            raise ValueError("invalid_historical_price_endpoints")
        change = values[1] / values[0] - 1
        direction = "up" if change > neutral_band else "down" if change < -neutral_band else "neutral"
        if direction == "neutral" or hypothesis_direction == "neutral":
            relation = "neutral_or_incomparable"
        else:
            relation = "historical_support" if direction == hypothesis_direction else "historical_counter"
        row.update(
            relation=relation,
            direction=direction,
            change=change,
            outcome_sha256=digest(outcome),
            neutral_band=neutral_band,
        )
        result.append(row)
    return result

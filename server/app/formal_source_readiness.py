from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import Any

from .phase_a_contracts import load_contract, load_source_registry
from .source_registry import REMOVED_SOURCE_IDS

READINESS_SCHEMA_VERSION = "formal-source-readiness.v1"
OWNER_ROLE = "Data Ingestion Agent"

EXACT_MARKET_SERIES_IDS = (
    "crude.brent.ice.front_month.settlement.usd_bbl",
    "crude.wti.cme.front_month.settlement.usd_bbl",
    "px.czce.main.settlement.cny_mt",
    "methanol.czce.main.settlement.cny_mt",
    "pta.czce.main.settlement.cny_mt",
    "meg.dce.main.settlement.cny_mt",
)
DOMESTIC_ASSESSMENT_SERIES_IDS = (
    "coal.benchmark.unresolved.assessment.cny_mt",
    "naphtha.ccf.domestic.daily_assessment.cny_mt",
    "mx.domestic.spot_assessment.cny_mt",
    "px.ccf.domestic.daily_assessment.cny_mt",
    "ethylene.domestic.spot_assessment.cny_mt",
    "eo.domestic.spot_assessment.cny_mt",
    "polyester_melt.domestic.assessment.cny_mt",
    "polyester_chip.domestic.assessment.cny_mt",
)

_BLOCKED_REASONS = {
    **{
        series_id: (
            "capture_missing",
            "instrument_mapping_unproven",
            "settlement_field_unproven",
            "roll_evidence_missing",
            "visibility_unproven",
            "calendar_evidence_missing",
        )
        for series_id in EXACT_MARKET_SERIES_IDS
    },
    "coal.benchmark.unresolved.assessment.cny_mt": (
        "capture_missing",
        "specification_unproven",
        "visibility_unproven",
        "calendar_evidence_missing",
    ),
    "naphtha.ccf.domestic.daily_assessment.cny_mt": (
        "no_exact_source",
        "market_mismatch",
        "unit_currency_mismatch",
        "visibility_unproven",
    ),
    "mx.domestic.spot_assessment.cny_mt": (
        "capture_missing",
        "range_assessment_evidence_missing",
        "visibility_unproven",
        "calendar_evidence_missing",
    ),
    "px.ccf.domestic.daily_assessment.cny_mt": (
        "no_exact_source",
        "market_mismatch",
        "unit_currency_mismatch",
        "visibility_unproven",
    ),
    "ethylene.domestic.spot_assessment.cny_mt": (
        "no_exact_source",
        "specification_unproven",
        "visibility_unproven",
        "calendar_evidence_missing",
    ),
    "eo.domestic.spot_assessment.cny_mt": (
        "no_exact_source",
        "specification_unproven",
        "visibility_unproven",
        "calendar_evidence_missing",
    ),
    "polyester_melt.domestic.assessment.cny_mt": (
        "no_exact_source",
        "specification_unproven",
        "visibility_unproven",
        "calendar_evidence_missing",
    ),
    "polyester_chip.domestic.assessment.cny_mt": (
        "no_exact_source",
        "specification_unproven",
        "visibility_unproven",
        "calendar_evidence_missing",
    ),
}
_BLOCKED_REASONS["meg.dce.main.settlement.cny_mt"] = (
    "source_soft_removed",
    "replacement_source_not_selected",
    "capture_missing",
)


class FormalSourceReadinessError(ValueError):
    """Raised when a frozen source contract cannot be represented safely."""


def build_unresolved_source_readiness() -> dict[str, Any]:
    """Describe the current fourteen unresolved source contracts without I/O to data stores.

    The records intentionally remain blocked. Candidate registration is useful
    lineage, but is not capture evidence and cannot create an eligibility bundle.
    """

    contract = load_contract()
    registry = load_source_registry()
    by_id = {str(item["series_id"]): item for item in contract["series"]}
    target_ids = (*EXACT_MARKET_SERIES_IDS, *DOMESTIC_ASSESSMENT_SERIES_IDS)
    if not set(target_ids) <= set(by_id):
        raise FormalSourceReadinessError("formal_source_contract_missing")

    records = []
    for series_id in target_ids:
        series = by_id[series_id]
        source_ids = [
            *series.get("candidate_source_ids", []),
            *([series["authoritative_source_id"]] if series.get("authoritative_source_id") else []),
        ]
        source_contracts = []
        retired = []
        for source_id in source_ids:
            source = registry.get(str(source_id))
            if source is None:
                if source_id in REMOVED_SOURCE_IDS:
                    retired.append(source_id)
                    continue
                raise FormalSourceReadinessError("candidate_source_not_registered")
            source_contracts.append(
                {
                    "source_id": str(source_id),
                    "endpoint": str(source["url"]),
                    "auth_type": str(source["auth_type"]),
                    "frequency": str(source["frequency"]),
                    "crawl_type": str(source["crawl_type"]),
                    "registry_digest": _digest(source),
                }
            )
        record = {
            "series_id": series_id,
            "issue_group": (
                "exact_exchange_or_benchmark"
                if series_id in EXACT_MARKET_SERIES_IDS
                else "domestic_industrial_assessment"
            ),
            "readiness_status": "blocked",
            "blocked_reasons": ["source_removed"] if retired else list(_BLOCKED_REASONS[series_id]),
            "historical_blocked_reasons": list(_BLOCKED_REASONS[series_id]) if retired else [],
            "historical_only": bool(retired),
            "removed_source_ids": retired,
            "owner_role": OWNER_ROLE,
            "market": series.get("market"),
            "instrument_or_grade": series.get("instrument_or_grade"),
            "quote_type": series.get("quote_type"),
            "raw_unit": series.get("raw_unit"),
            "standard_unit": series.get("standard_unit"),
            "currency": series.get("standard_currency"),
            "freshness_sla_minutes": series.get("freshness_sla_minutes"),
            "timezone": series.get("timezone"),
            "calendar_id": series.get("calendar_id"),
            "price_time_semantics": series.get("price_time_semantics"),
            "continuous_contract_policy_id": series.get("continuous_contract_policy_id"),
            "source_contracts": source_contracts,
            "lineage_policy": {
                "capture_table": "source_capture_revisions",
                "point_binding": "capture_revision_id",
                "required_hashes": ["raw_sha256", "canonical_payload_hash"],
            },
            "retention_and_revision_policy": {
                "retention": "append_only_indefinite_governance_evidence",
                "revision_semantics": "new_capture_revision_never_overwrite",
                "retroactive_revision": "conflict_until_separately_reviewed",
            },
            "observation_only": True,
            "evidence_bundle_emitted": False,
            "contract_digest": _digest(series),
        }
        record["readiness_digest"] = _digest(record)
        records.append(record)

    result = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "contract_version": str(contract["contract_version"]),
        "production_manifest_changed": False,
        "summary": {
            "record_count": len(records),
            "ready_count": 0,
            "blocked_count": len(records),
        },
        "records": records,
    }
    result["readiness_set_digest"] = _digest(result)
    return result


def normalize_range_assessment(
    *,
    low: int | float,
    high: int | float,
    unit: str,
    quote_type: str,
) -> dict[str, Any]:
    """Preserve assessment bounds and derive a deterministic midpoint.

    The helper never labels the result as a transaction and rejects invalid or
    reversed ranges rather than silently swapping their meaning.
    """

    if type(low) not in {int, float} or type(high) not in {int, float}:
        raise FormalSourceReadinessError("range_bounds_must_be_numeric")
    try:
        numeric_low = float(low)
        numeric_high = float(high)
    except OverflowError as exc:
        raise FormalSourceReadinessError("range_bounds_must_be_finite") from exc
    if not math.isfinite(numeric_low) or not math.isfinite(numeric_high):
        raise FormalSourceReadinessError("range_bounds_must_be_finite")
    if numeric_low > numeric_high:
        raise FormalSourceReadinessError("range_bounds_reversed")
    if type(unit) is not str or not unit or unit != unit.strip():
        raise FormalSourceReadinessError("range_unit_missing")
    if len(unit) > 128:
        raise FormalSourceReadinessError("range_unit_too_long")
    if quote_type not in {"public_spot_assessment", "licensed_spot_assessment"}:
        raise FormalSourceReadinessError("range_quote_type_not_assessment")
    midpoint = numeric_low / 2 + numeric_high / 2
    if not math.isfinite(midpoint):
        raise FormalSourceReadinessError("range_midpoint_not_finite")
    result = {
        "price_low": numeric_low,
        "price_high": numeric_high,
        "derived_midpoint": midpoint,
        "midpoint_rule": "arithmetic_midpoint.v1",
        "unit": unit,
        "quote_type": quote_type,
        "is_transaction_price": False,
    }
    result["range_digest"] = _digest(result)
    return result


def canonical_readiness_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_readiness_json(value).encode()).hexdigest()

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
from json import dumps
from typing import Any

from .seven_product_contract import CURRENT_FORMAL_LABEL_SERIES_IDS

CURRENT_FORMAL_SERIES_IDS = CURRENT_FORMAL_LABEL_SERIES_IDS

RESULT_SCHEMA_VERSION = "formal-series-eligibility-result.v1"
ELIGIBILITY_POLICY_VERSION = "formal-series-eligibility.v1"
EVIDENCE_BUNDLE_VERSION = "formal-series-evidence.v1"
APPROVED_MANIFEST_VERSION = "formal-series-approved-evidence.v1"
GATE_APPROVAL_VERSION = "formal-series-gate-approval.v1"
FORMAL_HORIZONS = (1, 7, 30)
LEGACY_READ_ONLY_HORIZON = 14
MAX_EVIDENCE_NUMERIC_MAGNITUDE = 10**12
MAX_EVIDENCE_COUNT = 1_000_000
MAX_HORIZON_DAYS = 10_000
# Aggregate budgets are derived from the current 19-series, three-horizon and
# six-gate contracts. They are intentionally explicit so oversized requests
# fail before normalization or canonical JSON work.
MAX_RECORDS = 19 * 3
MAX_MANIFEST_APPROVALS = 19 * 6
MAX_CANONICAL_DEPTH = 8
MAX_CANONICAL_NODES = 4_096
MAX_CANONICAL_STRING_LENGTH = 512
MAX_CANONICAL_TOTAL_STRING_LENGTH = 65_536
# Canonicalization must also be able to read immutable v4 assessments, whose
# approved manifest contains 20 × 6 entries. Current v5 writes remain bounded
# independently by ``MAX_MANIFEST_APPROVALS`` below.
MAX_CANONICAL_COLLECTION_ITEMS = 20 * 6

# Manifest authorization was removed with the personal-workbench de-scope
# (2026-08-28): every structurally valid approved-evidence manifest is accepted
# and its canonical digest is bound into the assessment identity as provenance.
# What still blocks a series is data quality — missing or mismatched gate
# approvals, freshness, visibility, calendar and unit rules.

# Historical Phase A v7 evidence surface. It remains immutable for replay only;
# current seven-product qualification is ``CURRENT_FORMAL_LABEL_SERIES_IDS``
# and must never use the CCF rows retained below.
FORMAL_SERIES_IDS = (
    "crude.brent.ice.front_month.settlement.usd_bbl",
    "crude.wti.cme.front_month.settlement.usd_bbl",
    "coal.benchmark.unresolved.assessment.cny_mt",
    "naphtha.ccf.domestic.daily_assessment.cny_mt",
    "mx.domestic.spot_assessment.cny_mt",
    "px.czce.main.settlement.cny_mt",
    "px.ccf.domestic.daily_assessment.cny_mt",
    "ethylene.domestic.spot_assessment.cny_mt",
    "eo.domestic.spot_assessment.cny_mt",
    "methanol.czce.main.settlement.cny_mt",
    "pta.czce.main.settlement.cny_mt",
    "pta.ccf.domestic.daily_assessment.cny_mt",
    "meg.dce.main.settlement.cny_mt",
    "meg.ccf.domestic.daily_assessment.cny_mt",
    "polyester_melt.domestic.assessment.cny_mt",
    "polyester_chip.domestic.assessment.cny_mt",
    "poy.upstream_cost_pressure.index",
    "dty.upstream_cost_pressure.index",
    "fx.usd_cny.cfets.central_parity.cny_per_usd",
)
PHASE_A_V7_HISTORICAL_SERIES_IDS = FORMAL_SERIES_IDS
V4_FORMAL_SERIES_IDS = (
    "crude.brent.ice.front_month.settlement.usd_bbl",
    "crude.wti.cme.front_month.settlement.usd_bbl",
    "crude.sc.ine.main.settlement.cny_bbl",
    *FORMAL_SERIES_IDS[2:],
)
FORMAL_SERIES_IDS_BY_CONTRACT_VERSION = {
    "phase-a.v4": V4_FORMAL_SERIES_IDS,
    "phase-a.v5": FORMAL_SERIES_IDS,
    "phase-a.v6": FORMAL_SERIES_IDS,
    "phase-a.v7": FORMAL_SERIES_IDS,
}

GATE_ASSERTIONS = {
    "source_authorization": (
        "source_registered",
        "authorization_valid",
        "license_valid",
    ),
    "series_specification": (
        "market_frozen",
        "instrument_frozen",
        "quote_type_frozen",
    ),
    "unit_currency_conversion": (
        "raw_unit_frozen",
        "standard_unit_frozen",
        "currency_frozen",
        "conversion_version_frozen",
    ),
    "freshness": (
        "freshness_rule_frozen",
        "within_freshness_threshold",
    ),
    "visibility": (
        "visibility_rule_frozen",
        "visible_by_assessment_as_of",
    ),
    "trading_calendar": (
        "calendar_id_frozen",
        "calendar_version_frozen",
        "trade_date_attribution_valid",
    ),
}
GATE_NAMES = tuple(GATE_ASSERTIONS)
GATE_NUMERIC_CHECKS = {
    "source_authorization": ("evidence_count",),
    "series_specification": ("evidence_count",),
    "unit_currency_conversion": ("evidence_count",),
    "freshness": ("age_seconds", "max_age_seconds"),
    "visibility": ("visible_age_seconds",),
    "trading_calendar": ("evidence_count",),
}

_RECORD_FIELDS = {"series_id", "horizon_days", "evidence_bundle"}
_BUNDLE_FIELDS = {"bundle_version", "series_id", "gates"}
_GATE_FIELDS = {
    "status",
    "policy_version",
    "series_id",
    "evidence_id",
    "valid_from",
    "valid_through",
    "applicable_horizons",
    "assertions",
    "numeric_checks",
}
_GATE_STATUSES = {"passed", "failed", "conflict", "unknown"}
_MANIFEST_FIELDS = {"manifest_version", "policy_version", "approvals"}
_APPROVAL_FIELDS = {
    "series_id",
    "gate_name",
    "evidence_id",
    "gate_facts_digest",
    "approval_version",
    "decision",
    "valid_from",
    "valid_through",
    "applicable_horizons",
}
_SERIES_ORDER = {series_id: index for index, series_id in enumerate(FORMAL_SERIES_IDS)}
_RFC3339_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")


class FormalSeriesEligibilityInputError(ValueError):
    """Raised when the evaluator's typed outer input contract is violated."""


def evaluate_formal_series_eligibility(
    records: Sequence[Mapping[str, Any]],
    *,
    assessment_as_of: str,
    policy_version: str = ELIGIBILITY_POLICY_VERSION,
    approved_evidence_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate versioned evidence without performing any I/O.

    Semantic defects are returned as stable blocked reasons. Outer-container and
    scalar type defects raise :class:`FormalSeriesEligibilityInputError`. Neither
    path includes caller-supplied evidence values in its messages.
    """

    try:
        return _evaluate_formal_series_eligibility(
            records,
            assessment_as_of=assessment_as_of,
            policy_version=policy_version,
            approved_evidence_manifest=approved_evidence_manifest,
            series_ids=FORMAL_SERIES_IDS,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except FormalSeriesEligibilityInputError:
        raise
    except BaseException:
        raise FormalSeriesEligibilityInputError("input_container_traversal_failed") from None


def _evaluate_formal_series_eligibility(
    records: Sequence[Mapping[str, Any]],
    *,
    assessment_as_of: str,
    policy_version: str,
    approved_evidence_manifest: Mapping[str, Any] | None,
    series_ids: tuple[str, ...],
) -> dict[str, Any]:
    series_order = {series_id: index for index, series_id in enumerate(series_ids)}
    max_records = len(series_ids) * len(FORMAL_HORIZONS)
    max_approvals = len(series_ids) * len(GATE_NAMES)
    if not _is_sequence(records):
        raise FormalSeriesEligibilityInputError("records_must_be_a_sequence")
    if len(records) > max_records:
        raise FormalSeriesEligibilityInputError("records_limit_exceeded")
    if type(policy_version) is not str:
        raise FormalSeriesEligibilityInputError("policy_version_must_be_a_string")
    assessment = _parse_timestamp(assessment_as_of)
    if assessment is None:
        raise FormalSeriesEligibilityInputError("assessment_as_of_must_be_rfc3339")
    approvals, manifest_reason, approved_manifest_digest = _approved_manifest_lookup(
        approved_evidence_manifest,
        series_ids=series_ids,
        max_approvals=max_approvals,
    )

    normalized: list[tuple[int, str, str, int | None, Mapping[str, Any] | None, list[str]]] = []
    known_keys: list[tuple[str, int]] = []
    for index, record in enumerate(records):
        if index >= max_records:
            raise FormalSeriesEligibilityInputError("records_limit_exceeded")
        if type(record) is not dict:
            raise FormalSeriesEligibilityInputError("record_must_be_an_object")
        record_is_bounded_plain_json = _plain_object_within_shallow_limits(record)
        if not record_is_bounded_plain_json:
            raise FormalSeriesEligibilityInputError("invalid_record_data")
        series_id = record.get("series_id")
        horizon = record.get("horizon_days")
        if type(series_id) is not str:
            raise FormalSeriesEligibilityInputError("series_id_must_be_a_string")
        if type(horizon) is not int:
            raise FormalSeriesEligibilityInputError("horizon_days_must_be_an_integer")
        if abs(horizon) > MAX_HORIZON_DAYS:
            raise FormalSeriesEligibilityInputError("horizon_days_out_of_range")

        safe_series_id = series_id if series_id in series_order else "__unknown__"
        reasons: list[str] = []
        if set(record) != _RECORD_FIELDS:
            reasons.append("invalid_record_schema")
        if series_id not in series_order:
            reasons.append("unknown_series")
        else:
            known_keys.append((series_id, horizon))
        if horizon == LEGACY_READ_ONLY_HORIZON:
            reasons.append("legacy_horizon_read_only")
        elif horizon not in FORMAL_HORIZONS:
            reasons.append("unsupported_horizon")

        bundle = record.get("evidence_bundle")
        if record_is_bounded_plain_json and not _is_plain_json_type(bundle):
            reasons.append("invalid_record_data")
        normalized.append(
            (
                index,
                series_id,
                safe_series_id,
                horizon,
                bundle if record_is_bounded_plain_json and type(bundle) is dict else None,
                reasons,
            )
        )

    duplicate_keys = {key for key, count in Counter(known_keys).items() if count > 1}
    results: list[dict[str, Any]] = []
    for index, requested_series_id, series_id, horizon, bundle, initial_reasons in normalized:
        reasons = list(initial_reasons)
        gate_statuses = {gate_name: "blocked" for gate_name in GATE_NAMES}
        if series_id != "__unknown__" and horizon is not None and (series_id, horizon) in duplicate_keys:
            reasons.append("duplicate_series_horizon")
        if policy_version != ELIGIBILITY_POLICY_VERSION:
            reasons.append("policy_version_mismatch")
        if manifest_reason is not None:
            reasons.append(manifest_reason)

        if "invalid_record_data" in reasons:
            pass
        elif bundle is None:
            reasons.append("missing_evidence_bundle")
        else:
            _evaluate_bundle(
                bundle,
                series_id=requested_series_id,
                assessment=assessment,
                horizon=horizon,
                approvals=approvals,
                reasons=reasons,
                gate_statuses=gate_statuses,
            )

        stable_reasons = _stable_unique(reasons)
        results.append(
            {
                "series_id": series_id,
                "horizon_days": horizon,
                "eligibility_status": "eligible" if not stable_reasons else "blocked",
                "blocked_reasons": stable_reasons,
                "gate_statuses": gate_statuses,
                "_input_index": index,
            }
        )

    results.sort(key=lambda item: _result_sort_key(item, series_order))
    for result in results:
        result.pop("_input_index")
    eligible_count = sum(result["eligibility_status"] == "eligible" for result in results)
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "policy_version": ELIGIBILITY_POLICY_VERSION,
        "approved_manifest_digest": approved_manifest_digest,
        "assessment_as_of": assessment.isoformat(),
        "summary": {
            "record_count": len(results),
            "eligible_count": eligible_count,
            "blocked_count": len(results) - eligible_count,
        },
        "results": results,
    }


def _evaluate_bundle(
    bundle: Mapping[str, Any],
    *,
    series_id: str,
    assessment: datetime,
    horizon: int,
    approvals: Mapping[tuple[str, str], Mapping[str, Any]],
    reasons: list[str],
    gate_statuses: dict[str, str],
) -> None:
    if not _plain_object_within_shallow_limits(bundle):
        reasons.append("invalid_evidence_bundle_schema")
        return
    if set(bundle) != _BUNDLE_FIELDS:
        reasons.append("invalid_evidence_bundle_schema")
    if bundle.get("bundle_version") != EVIDENCE_BUNDLE_VERSION:
        reasons.append("evidence_bundle_version_mismatch")
    if bundle.get("series_id") != series_id:
        reasons.append("evidence_bundle_series_mismatch")
    gates = bundle.get("gates")
    if type(gates) is not dict or not _plain_object_within_shallow_limits(gates):
        reasons.append("invalid_evidence_gates")
        return
    if set(gates) - set(GATE_NAMES):
        reasons.append("unknown_evidence_gate")

    for gate_name in GATE_NAMES:
        gate = gates.get(gate_name)
        if gate is None:
            reasons.append(f"missing_{gate_name}_evidence")
            continue
        if type(gate) is not dict:
            reasons.append(f"invalid_{gate_name}_evidence")
            continue
        gate_reasons = _gate_reasons(
            gate_name,
            gate,
            series_id=series_id,
            assessment=assessment,
            horizon=horizon,
        )
        gate_reasons.extend(
            _approval_reasons(
                gate_name,
                gate,
                series_id=series_id,
                assessment=assessment,
                horizon=horizon,
                approvals=approvals,
            )
        )
        gate_reasons = _stable_unique(gate_reasons)
        reasons.extend(gate_reasons)
        if not gate_reasons:
            gate_statuses[gate_name] = "passed"


def _gate_reasons(
    gate_name: str,
    gate: Mapping[str, Any],
    *,
    series_id: str,
    assessment: datetime,
    horizon: int,
) -> list[str]:
    reasons: list[str] = []
    if not _json_within_resource_limits(gate, validate_numeric_values=False):
        return [f"invalid_{gate_name}_evidence"]
    if set(gate) != _GATE_FIELDS:
        reasons.append(f"invalid_{gate_name}_evidence")

    status = gate.get("status")
    if status not in _GATE_STATUSES:
        reasons.append(f"invalid_{gate_name}_status")
    elif status != "passed":
        reasons.append(f"{gate_name}_{status}")
    if gate.get("policy_version") != ELIGIBILITY_POLICY_VERSION:
        reasons.append(f"{gate_name}_version_mismatch")
    if gate.get("series_id") != series_id:
        reasons.append(f"{gate_name}_series_mismatch")
    evidence_id = gate.get("evidence_id")
    if type(evidence_id) is not str or not evidence_id.strip() or len(evidence_id) > 256:
        reasons.append(f"invalid_{gate_name}_evidence_id")

    valid_from = _parse_timestamp(gate.get("valid_from"))
    valid_through = _parse_timestamp(gate.get("valid_through"))
    if valid_from is None or valid_through is None or valid_from > valid_through:
        reasons.append(f"{gate_name}_validity_invalid")
    elif not valid_from <= assessment <= valid_through:
        reasons.append(f"{gate_name}_not_valid_at_assessment")

    applicable_horizons = gate.get("applicable_horizons")
    if not _valid_horizon_sequence(applicable_horizons):
        reasons.append(f"{gate_name}_horizons_invalid")
    elif horizon not in applicable_horizons:
        reasons.append(f"{gate_name}_horizon_not_covered")

    assertions = gate.get("assertions")
    required_assertions = set(GATE_ASSERTIONS[gate_name])
    if type(assertions) is not dict or set(assertions) != required_assertions:
        reasons.append(f"{gate_name}_assertions_invalid")
    elif not all(value is True for value in assertions.values()):
        reasons.append(f"{gate_name}_assertion_failed")

    numeric_checks = gate.get("numeric_checks")
    if not _valid_numeric_checks(gate_name, numeric_checks):
        reasons.append(f"{gate_name}_numeric_checks_invalid")
    return _stable_unique(reasons)


def _valid_horizon_sequence(value: Any) -> bool:
    if not _is_sequence(value) or not value:
        return False
    if any(type(item) is not int for item in value):
        return False
    return len(set(value)) == len(value) and set(value) <= set(FORMAL_HORIZONS)


def _parse_timestamp(value: Any) -> datetime | None:
    if type(value) is not str or not value or value != value.strip() or _RFC3339_PATTERN.fullmatch(value) is None:
        return None
    if value.endswith("-00:00"):
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _valid_numeric_checks(gate_name: str, value: Any) -> bool:
    required = set(GATE_NUMERIC_CHECKS[gate_name])
    if type(value) is not dict or set(value) != required:
        return False
    if not all(_is_bounded_numeric(item) for item in value.values()):
        return False
    if gate_name in {
        "source_authorization",
        "series_specification",
        "unit_currency_conversion",
        "trading_calendar",
    }:
        evidence_count = value["evidence_count"]
        return type(evidence_count) is int and 0 < evidence_count <= MAX_EVIDENCE_COUNT
    if gate_name == "freshness":
        age_seconds = float(value["age_seconds"])
        max_age_seconds = float(value["max_age_seconds"])
        return 0 <= age_seconds <= max_age_seconds and max_age_seconds > 0
    return float(value["visible_age_seconds"]) >= 0


def _is_bounded_numeric(value: Any) -> bool:
    if type(value) not in {int, float}:
        return False
    if type(value) is float and not math.isfinite(value):
        return False
    return abs(value) <= MAX_EVIDENCE_NUMERIC_MAGNITUDE


def _canonical_manifest_digest(manifest: Mapping[str, Any]) -> str:
    if type(manifest) is not dict:
        raise FormalSeriesEligibilityInputError("approved_manifest_must_be_an_object")
    if not _json_within_resource_limits(manifest):
        raise FormalSeriesEligibilityInputError("approved_manifest_not_canonicalizable")
    try:
        encoded = dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, OverflowError, RecursionError, MemoryError):
        raise FormalSeriesEligibilityInputError("approved_manifest_not_canonicalizable") from None
    return sha256(encoded).hexdigest()


def _approved_manifest_lookup(
    manifest: Mapping[str, Any] | None,
    *,
    series_ids: tuple[str, ...] = FORMAL_SERIES_IDS,
    max_approvals: int = MAX_MANIFEST_APPROVALS,
) -> tuple[dict[tuple[str, str], Mapping[str, Any]], str | None, str | None]:
    if manifest is None:
        return {}, "approved_evidence_manifest_missing", None
    if type(manifest) is not dict:
        return {}, "approved_evidence_manifest_invalid", None
    if not _json_within_resource_limits(manifest):
        return {}, "approved_evidence_manifest_invalid", None
    if set(manifest) != _MANIFEST_FIELDS:
        return {}, "approved_evidence_manifest_invalid", None
    if (
        manifest.get("manifest_version") != APPROVED_MANIFEST_VERSION
        or manifest.get("policy_version") != ELIGIBILITY_POLICY_VERSION
    ):
        return {}, "approved_evidence_manifest_invalid", None
    approvals = manifest.get("approvals")
    if not _is_sequence(approvals) or len(approvals) > max_approvals:
        return {}, "approved_evidence_manifest_invalid", None

    lookup: dict[tuple[str, str], Mapping[str, Any]] = {}
    for approval in approvals:
        if not _valid_approval_shape(approval, series_ids=series_ids):
            return {}, "approved_evidence_manifest_invalid", None
        key = (approval["series_id"], approval["gate_name"])
        if key in lookup:
            return {}, "approved_evidence_manifest_invalid", None
        lookup[key] = approval
    try:
        actual_digest = _canonical_manifest_digest(manifest)
    except FormalSeriesEligibilityInputError:
        return {}, "approved_evidence_manifest_invalid", None
    # Manifest authorization is removed: the digest is bound into the result as
    # provenance only. Gate-approval facts still drive every blocked reason.
    return lookup, None, actual_digest


def _replay_formal_series_eligibility(
    records: Sequence[Mapping[str, Any]],
    *,
    assessment_as_of: str,
    policy_version: str,
    approved_evidence_manifest: Mapping[str, Any],
    contract_version: str = "phase-a.v7",
) -> dict[str, Any]:
    """Private pure historical replay for bounded audits of immutable history.

    Evaluation is deterministic given records and manifest, so a captured
    manifest digest is no longer needed to reproduce historical results.
    """

    series_ids = formal_series_ids_for_contract_version(contract_version)
    if series_ids is None:
        raise FormalSeriesEligibilityInputError("historical_contract_version_unsupported")
    return _evaluate_formal_series_eligibility(
        records,
        assessment_as_of=assessment_as_of,
        policy_version=policy_version,
        approved_evidence_manifest=approved_evidence_manifest,
        series_ids=series_ids,
    )


def formal_series_ids_for_contract_version(contract_version: object) -> tuple[str, ...] | None:
    """Return the frozen series order needed to replay an immutable contract."""

    return FORMAL_SERIES_IDS_BY_CONTRACT_VERSION.get(str(contract_version))


def _valid_approval_shape(value: Any, *, series_ids: tuple[str, ...]) -> bool:
    if type(value) is not dict or set(value) != _APPROVAL_FIELDS:
        return False
    if value.get("series_id") not in set(series_ids) or value.get("gate_name") not in GATE_NAMES:
        return False
    if value.get("approval_version") != GATE_APPROVAL_VERSION or value.get("decision") != "approved":
        return False
    if not _valid_digest(value.get("gate_facts_digest")):
        return False
    evidence_id = value.get("evidence_id")
    if type(evidence_id) is not str or not evidence_id.strip() or len(evidence_id) > 256:
        return False
    valid_from = _parse_timestamp(value.get("valid_from"))
    valid_through = _parse_timestamp(value.get("valid_through"))
    return (
        valid_from is not None
        and valid_through is not None
        and valid_from <= valid_through
        and _valid_horizon_sequence(value.get("applicable_horizons"))
    )


def _approval_reasons(
    gate_name: str,
    gate: Mapping[str, Any],
    *,
    series_id: str,
    assessment: datetime,
    horizon: int,
    approvals: Mapping[tuple[str, str], Mapping[str, Any]],
) -> list[str]:
    approval = approvals.get((series_id, gate_name))
    if approval is None:
        return [f"{gate_name}_approval_missing"]
    reasons: list[str] = []
    if approval.get("evidence_id") != gate.get("evidence_id"):
        reasons.append(f"{gate_name}_approval_evidence_mismatch")
    gate_digest = _canonical_gate_digest(gate)
    if gate_digest is None or approval.get("gate_facts_digest") != gate_digest:
        reasons.append(f"{gate_name}_approval_facts_mismatch")
    approval_from = _parse_timestamp(approval.get("valid_from"))
    approval_through = _parse_timestamp(approval.get("valid_through"))
    if approval_from is None or approval_through is None or not approval_from <= assessment <= approval_through:
        reasons.append(f"{gate_name}_approval_not_valid_at_assessment")
    approved_horizons = approval.get("applicable_horizons")
    if approved_horizons != gate.get("applicable_horizons"):
        reasons.append(f"{gate_name}_approval_horizons_mismatch")
    if not _valid_horizon_sequence(approved_horizons) or horizon not in approved_horizons:
        reasons.append(f"{gate_name}_approval_horizon_not_covered")
    return reasons


def _canonical_gate_digest(gate: Mapping[str, Any]) -> str | None:
    if not _json_within_resource_limits(gate):
        return None
    try:
        encoded = dumps(
            gate,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    except (TypeError, ValueError, OverflowError, RecursionError, MemoryError):
        return None
    return sha256(encoded).hexdigest()


def _json_within_resource_limits(value: Any, *, validate_numeric_values: bool = True) -> bool:
    """Bound canonical-JSON work before invoking the recursive encoder."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    seen_containers: set[int] = set()
    node_count = 0
    total_string_length = 0
    try:
        while stack:
            current, depth = stack.pop()
            node_count += 1
            if node_count > MAX_CANONICAL_NODES or depth > MAX_CANONICAL_DEPTH:
                return False
            if type(current) is str:
                if len(current) > MAX_CANONICAL_STRING_LENGTH:
                    return False
                total_string_length += len(current)
                if total_string_length > MAX_CANONICAL_TOTAL_STRING_LENGTH:
                    return False
                continue
            if current is None or type(current) is bool:
                continue
            if type(current) in {int, float}:
                if validate_numeric_values and not _is_bounded_numeric(current):
                    return False
                continue
            if type(current) is dict:
                container_id = id(current)
                if container_id in seen_containers or len(current) > MAX_CANONICAL_COLLECTION_ITEMS:
                    return False
                seen_containers.add(container_id)
                for child_count, (key, item) in enumerate(current.items(), start=1):
                    if child_count > MAX_CANONICAL_COLLECTION_ITEMS or type(key) is not str:
                        return False
                    if len(key) > MAX_CANONICAL_STRING_LENGTH:
                        return False
                    total_string_length += len(key)
                    if total_string_length > MAX_CANONICAL_TOTAL_STRING_LENGTH:
                        return False
                    stack.append((item, depth + 1))
                continue
            if _is_sequence(current):
                container_id = id(current)
                if container_id in seen_containers or len(current) > MAX_CANONICAL_COLLECTION_ITEMS:
                    return False
                seen_containers.add(container_id)
                for child_count, item in enumerate(current, start=1):
                    if child_count > MAX_CANONICAL_COLLECTION_ITEMS:
                        return False
                    stack.append((item, depth + 1))
                continue
            return False
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return False
    return True


def _plain_object_within_shallow_limits(value: Any) -> bool:
    """Validate one built-in JSON object before any business key traversal."""

    if type(value) is not dict:
        return False
    try:
        if len(value) > MAX_CANONICAL_COLLECTION_ITEMS:
            return False
        total_key_length = 0
        for key_count, key in enumerate(value, start=1):
            if key_count > MAX_CANONICAL_COLLECTION_ITEMS or type(key) is not str:
                return False
            if len(key) > MAX_CANONICAL_STRING_LENGTH:
                return False
            total_key_length += len(key)
            if total_key_length > MAX_CANONICAL_TOTAL_STRING_LENGTH:
                return False
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:
        return False
    return True


def _is_plain_json_type(value: Any) -> bool:
    return value is None or type(value) in {dict, list, str, bool, int, float}


def _valid_digest(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _stable_unique(values: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _result_sort_key(
    result: Mapping[str, Any], series_order: Mapping[str, int]
) -> tuple[int, int, tuple[str, ...], int]:
    series_id = str(result["series_id"])
    horizon = int(result["horizon_days"])
    return (
        series_order.get(series_id, len(series_order)),
        horizon,
        tuple(result["blocked_reasons"]),
        int(result["_input_index"]),
    )


def _is_sequence(value: Any) -> bool:
    return type(value) is list

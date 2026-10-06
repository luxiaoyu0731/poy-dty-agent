"""Pure, content-addressed candidates for Shadow lifecycle scoring.

The production trust root is deliberately empty.  A caller can select a known
candidate by digest, but cannot declare that candidate approved or provide a
replacement contract body.  The module performs no I/O and owns no promotion
thresholds.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from types import MappingProxyType
from typing import Any

CONTRACT_SCHEMA_VERSION = "shadow-metric-contract.v1"
EVALUATION_SCHEMA_VERSION = "shadow-metric-evaluation.v1"
PRODUCTION_TRUST_ROOT_VERSION = "shadow-metric-production-trust-root.empty.v1"
MAX_CONTRACT_BYTES = 8_192
MAX_PROBABILITY_CHARACTERS = 20
_CLASSES = ("down", "neutral", "up")
_REASON_CODE = re.compile(r"\A[a-z][a-z0-9_.:-]{0,127}\Z")
_DECIMAL_PROBABILITY = re.compile(r"\A(?:0(?:\.\d{1,18})?|1(?:\.0{1,18})?)\Z")


class ShadowMetricContractError(ValueError):
    """Stable bounded failure from contract selection or metric calculation."""

    def __init__(self, code: str) -> None:
        if not _REASON_CODE.fullmatch(code):
            code = "shadow_metric_contract_failure"
        super().__init__(code)
        self.code = code


_DIRECTIONAL_THREE_CLASS_BRIER_V1 = {
    "schema_version": CONTRACT_SCHEMA_VERSION,
    "contract_id": "directional-three-class-brier.v1",
    "classes": list(_CLASSES),
    "factor_projection": {
        "field": "direction_probabilities",
        "representation": "canonical_decimal_strings",
        "sum": "exactly_one",
    },
    "baseline": {
        "baseline_id": "uniform-three-class.v1",
        "algorithm": "equal_rational_weight",
        "requires_input_data": False,
        "weights": {"down": 1, "neutral": 1, "up": 1},
        "lifecycle_gate_compatibility": "paired_gate_requires_confidence_lte_one_third",
    },
    "error": {
        "metric": "multiclass_brier",
        "formula": "sum_squared_probability_minus_one_hot",
        "normalization": "unscaled",
        "lower_is_better": True,
    },
    "confidence": {"metric": "maximum_class_probability"},
    "incorrect": {
        "metric": "outcome_not_in_complete_argmax_set",
        "tie_policy": "retain_all_maximizers_without_tiebreak",
        "neutral_is_distinct_class": True,
    },
}


def _canonical_json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ShadowMetricContractError("shadow_metric_contract_invalid") from None
    if len(encoded.encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise ShadowMetricContractError("shadow_metric_contract_resource_limit")
    return encoded


_DIRECTIONAL_THREE_CLASS_BRIER_V1_JSON = _canonical_json(_DIRECTIONAL_THREE_CLASS_BRIER_V1)
DIRECTIONAL_THREE_CLASS_BRIER_V1_DIGEST = hashlib.sha256(
    _DIRECTIONAL_THREE_CLASS_BRIER_V1_JSON.encode("utf-8")
).hexdigest()

# Candidate content and production approval are separate roots.  Altering a
# candidate body without changing its key is detected by digest verification.
_CANDIDATE_CONTRACT_JSON_BY_DIGEST = MappingProxyType(
    {DIRECTIONAL_THREE_CLASS_BRIER_V1_DIGEST: _DIRECTIONAL_THREE_CLASS_BRIER_V1_JSON}
)
_PRODUCTION_APPROVED_CONTRACT_DIGESTS: frozenset[str] = frozenset()


def list_shadow_metric_contract_candidates() -> tuple[dict[str, Any], ...]:
    """Return canonical candidate metadata without granting approval."""

    return tuple(
        {
            "contract_digest": digest,
            "contract": _decode_verified_contract(digest),
            "approval_status": (
                "approved" if digest in _PRODUCTION_APPROVED_CONTRACT_DIGESTS else "candidate"
            ),
        }
        for digest in sorted(_CANDIDATE_CONTRACT_JSON_BY_DIGEST)
    )


def evaluate_shadow_metric_contract(
    *,
    contract_digest: str,
    factor_probabilities: dict[str, str],
    outcome_direction: str,
) -> dict[str, Any]:
    """Calculate lifecycle fields only for a code-approved contract digest.

    ``factor_probabilities`` uses canonical decimal strings so sum-to-one is
    exact and independent of platform float rounding.  The candidate's uniform
    baseline is generated internally, but the production trust root remains
    empty because this baseline enters the paired-high-confidence cohort only
    when the policy threshold is at most one third, and no compatible policy
    binding has been approved for production.
    """

    contract = _decode_verified_contract(contract_digest)
    if contract_digest not in _PRODUCTION_APPROVED_CONTRACT_DIGESTS:
        return {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "calculation_status": "blocked",
            "reason_codes": ["shadow_metric_contract_unapproved"],
            "contract_digest": contract_digest,
            "contract_id": contract["contract_id"],
            "trust_root_version": PRODUCTION_TRUST_ROOT_VERSION,
            "lifecycle_metrics": None,
        }

    factor = _probabilities(factor_probabilities)
    if outcome_direction not in _CLASSES:
        raise ShadowMetricContractError("shadow_metric_outcome_invalid")
    baseline = {label: Fraction(1, len(_CLASSES)) for label in _CLASSES}
    factor_error = _brier(factor, outcome_direction)
    baseline_error = _brier(baseline, outcome_direction)
    metrics = {
        "factor_error": float(factor_error),
        "baseline_error": float(baseline_error),
        "factor_confidence": float(max(factor.values())),
        "baseline_confidence": float(max(baseline.values())),
        "factor_incorrect": _incorrect(factor, outcome_direction),
        "baseline_incorrect": _incorrect(baseline, outcome_direction),
    }
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "calculation_status": "computed",
        "reason_codes": [],
        "contract_digest": contract_digest,
        "contract_id": contract["contract_id"],
        "trust_root_version": PRODUCTION_TRUST_ROOT_VERSION,
        "lifecycle_metrics": metrics,
        "calculation_audit": {
            "factor_probabilities": dict(sorted(factor_probabilities.items())),
            "baseline_probability_weights": {label: 1 for label in _CLASSES},
            "outcome_direction": outcome_direction,
            "factor_error_fraction": f"{factor_error.numerator}/{factor_error.denominator}",
            "baseline_error_fraction": f"{baseline_error.numerator}/{baseline_error.denominator}",
        },
    }


def _decode_verified_contract(contract_digest: Any) -> dict[str, Any]:
    if (
        type(contract_digest) is not str
        or len(contract_digest) != 64
        or any(character not in "0123456789abcdef" for character in contract_digest)
    ):
        raise ShadowMetricContractError("shadow_metric_contract_digest_invalid")
    encoded = _CANDIDATE_CONTRACT_JSON_BY_DIGEST.get(contract_digest)
    if encoded is None:
        raise ShadowMetricContractError("shadow_metric_contract_missing")
    if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != contract_digest:
        raise ShadowMetricContractError("shadow_metric_contract_digest_mismatch")
    try:
        contract = json.loads(encoded)
    except (TypeError, json.JSONDecodeError):
        raise ShadowMetricContractError("shadow_metric_contract_invalid") from None
    if _canonical_json(contract) != encoded or contract != _DIRECTIONAL_THREE_CLASS_BRIER_V1:
        raise ShadowMetricContractError("shadow_metric_contract_invalid")
    return contract


def _probabilities(raw: Any) -> dict[str, Fraction]:
    if type(raw) is not dict or set(raw) != set(_CLASSES):
        raise ShadowMetricContractError("shadow_metric_probabilities_invalid")
    result: dict[str, Fraction] = {}
    for label in _CLASSES:
        value = raw[label]
        if (
            type(value) is not str
            or len(value) > MAX_PROBABILITY_CHARACTERS
            or not _DECIMAL_PROBABILITY.fullmatch(value)
        ):
            raise ShadowMetricContractError("shadow_metric_probabilities_invalid")
        try:
            decimal = Decimal(value)
        except InvalidOperation:
            raise ShadowMetricContractError("shadow_metric_probabilities_invalid") from None
        canonical = "0" if decimal == 0 else "1" if decimal == 1 else format(decimal.normalize(), "f")
        if value != canonical:
            raise ShadowMetricContractError("shadow_metric_probabilities_noncanonical")
        result[label] = Fraction(decimal)
    if sum(result.values(), start=Fraction(0)) != 1:
        raise ShadowMetricContractError("shadow_metric_probability_sum_invalid")
    return result


def _brier(probabilities: dict[str, Fraction], outcome: str) -> Fraction:
    return sum(
        (
            probabilities[label]
            - (Fraction(1) if label == outcome else Fraction(0))
        )
        ** 2
        for label in _CLASSES
    )


def _incorrect(probabilities: dict[str, Fraction], outcome: str) -> bool:
    maximum = max(probabilities.values())
    maximizers = {label for label, value in probabilities.items() if value == maximum}
    return outcome not in maximizers

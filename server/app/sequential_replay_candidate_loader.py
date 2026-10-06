"""Read-only, point-in-time candidates for the pure sequential replay planner."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from . import formal_prediction_batches, storage
from .phase_a_contracts import EXPECTED_FORMAL_NODES
from .sequential_replay import MAX_ABS_METRIC_VALUE

MAX_SELECTORS = 512
MAX_SELECTOR_BYTES = 4_096
MAX_SELECTOR_ID_LENGTH = 128
MAX_DIAGNOSTICS = 128
TERMINAL_NODE = "poy_dty_upstream_cost_pressure"
SUPPORTED_KINDS = frozenset({"formal_prediction_revision", "experience_revision"})
_CANONICAL_RFC3339 = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?"
    r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)\Z"
)
_REASON_CODE = re.compile(r"\A[a-z][a-z0-9_.:-]{0,127}\Z")


class SequentialReplayCandidateLoaderError(ValueError):
    """Stable, non-secret failure from explicit replay candidate loading."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def load_sequential_replay_checkpoint(
    *,
    as_of: str,
    selectors: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Load an exact replay checkpoint from explicit persisted selectors.

    Supported records are fully re-audited by their owning readers.  This
    adapter never chooses a revision, product, node, horizon, snapshot,
    calendar or policy and never performs persistence.
    """

    checkpoint_time = _canonical_time(as_of, "replay_checkpoint_as_of_invalid")
    if isinstance(selectors, (str, bytes, bytearray)) or not isinstance(selectors, Sequence):
        raise SequentialReplayCandidateLoaderError("replay_selectors_invalid")
    if len(selectors) > MAX_SELECTORS:
        raise SequentialReplayCandidateLoaderError("replay_selector_limit_exceeded")

    normalized: list[tuple[str, dict[str, Any]]] = []
    seen_selectors: set[str] = set()
    for raw in selectors:
        selector, encoded = _selector(raw)
        if encoded in seen_selectors:
            raise SequentialReplayCandidateLoaderError("replay_selector_duplicate")
        seen_selectors.add(encoded)
        normalized.append((encoded, selector))

    items: list[dict[str, Any]] = []
    diagnostics: list[dict[str, str]] = []
    for encoded, selector in sorted(normalized, key=lambda item: item[0]):
        kind = selector["kind"]
        if kind == "formal_prediction_revision":
            items.append(_formal_prediction_item(selector, checkpoint_time, as_of))
        elif kind == "experience_revision":
            items.append(_experience_item(selector, checkpoint_time, as_of))
        else:
            if len(diagnostics) >= MAX_DIAGNOSTICS:
                raise SequentialReplayCandidateLoaderError("replay_diagnostic_limit_exceeded")
            diagnostics.append(
                {
                    "selector_sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                    "kind": kind,
                    "status": "unsupported",
                    "reason_code": "unsupported_persisted_kind",
                }
            )

    identities = [(item["product"], item["node_id"], item["kind"], item["item_id"]) for item in items]
    if len(identities) != len(set(identities)):
        raise SequentialReplayCandidateLoaderError("replay_candidate_identity_duplicate")
    items.sort(key=lambda item: (item["product"], item["node_id"], item["kind"], item["item_id"]))
    diagnostics.sort(key=lambda item: (item["kind"], item["selector_sha256"]))
    return {
        "schema_version": "sequential-replay-candidate-loader.v1",
        "checkpoint": {"as_of": as_of, "items": items},
        "diagnostics": diagnostics,
    }


def _selector(raw: Any) -> tuple[dict[str, Any], str]:
    if type(raw) is not dict:
        raise SequentialReplayCandidateLoaderError("replay_selector_invalid")
    if len(raw) > 12 or type(raw.get("kind")) is not str:
        raise SequentialReplayCandidateLoaderError("replay_selector_invalid")
    kind = raw["kind"]
    if not kind or len(kind) > MAX_SELECTOR_ID_LENGTH:
        raise SequentialReplayCandidateLoaderError("replay_selector_kind_invalid")
    required = {
        "formal_prediction_revision": {
            "kind",
            "revision_id",
            "data_snapshot_id",
            "product",
            "node_id",
            "horizon",
        },
        "experience_revision": {
            "kind",
            "revision_id",
            "prediction_revision_id",
            "data_snapshot_id",
            "product",
            "node_id",
            "horizon",
            "calendar_id",
            "calendar_version",
        },
    }.get(kind)
    if required is not None and set(raw) != required:
        raise SequentialReplayCandidateLoaderError("replay_selector_fields_invalid")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise SequentialReplayCandidateLoaderError("replay_selector_invalid") from None
    if len(encoded.encode("utf-8")) > MAX_SELECTOR_BYTES:
        raise SequentialReplayCandidateLoaderError("replay_selector_size_exceeded")
    return dict(raw), encoded


def _formal_prediction_item(
    selector: dict[str, Any],
    checkpoint_time: datetime,
    checkpoint_text: str,
) -> dict[str, Any]:
    _validate_explicit_selector(selector)
    try:
        revision = formal_prediction_batches.load_verified_formal_prediction_revision(
            revision_id=selector["revision_id"],
            as_of_time=checkpoint_text,
        )
    except formal_prediction_batches.FormalPredictionBatchError as exc:
        raise SequentialReplayCandidateLoaderError(exc.code) from None
    payload = revision["payload"]
    if payload["data_snapshot_id"] != selector["data_snapshot_id"]:
        raise SequentialReplayCandidateLoaderError("replay_selector_snapshot_mismatch")
    cell = _exact_cell(payload["cells"], selector["node_id"], selector["horizon"])
    projected = _formal_projection(cell, selector["node_id"], selector["product"])
    observed_at = _visible_time(payload["as_of_time"], checkpoint_time, "replay_observed_after_as_of")
    available_at = _visible_time(
        revision["authorization_at"],
        checkpoint_time,
        "replay_available_after_as_of",
    )
    if available_at < observed_at:
        raise SequentialReplayCandidateLoaderError("replay_available_precedes_observed")
    evidence_times = _evidence_times(
        [
            payload["data_frozen_at"],
            payload["created_at"],
            payload["published_at"],
            revision["persisted_at"],
            revision["authorization_at"],
        ],
        checkpoint_time,
    )
    scoreability, reasons = _formal_scoreability(projected)
    return {
        "item_id": _item_id("formal", selector),
        "kind": "prediction",
        "product": selector["product"],
        "node_id": selector["node_id"],
        "observed_at": observed_at.isoformat(),
        "available_at": available_at.isoformat(),
        "evidence_times": evidence_times,
        "evidence_status": "point_in_time",
        "scoreability": scoreability,
        "exclusion_reasons": reasons,
        "metric_values": _numeric_metrics(
            projected,
            (
                "direction_probability",
                "magnitude",
                "confidence",
                "remaining_effective_probability",
                "data_completeness",
            ),
        ),
        "horizon": selector["horizon"],
    }


def _experience_item(
    selector: dict[str, Any],
    checkpoint_time: datetime,
    checkpoint_text: str,
) -> dict[str, Any]:
    _validate_explicit_selector(selector)
    try:
        experience = storage.load_verified_experience_card_revision(
            revision_id=selector["revision_id"],
            as_of_time=checkpoint_text,
        )
    except storage.ExperienceRevisionReadError as exc:
        raise SequentialReplayCandidateLoaderError(exc.code) from None
    card = experience["card"]
    for field in (
        "prediction_revision_id",
        "data_snapshot_id",
        "node_id",
        "horizon_days",
        "calendar_id",
        "calendar_version",
    ):
        selector_field = "horizon" if field == "horizon_days" else field
        if card[field] != selector[selector_field]:
            raise SequentialReplayCandidateLoaderError(f"replay_selector_{selector_field}_mismatch")
    _validate_product_binding(selector["node_id"], selector["product"], card.get("subtarget"))
    try:
        prediction = formal_prediction_batches.load_verified_formal_prediction_revision(
            revision_id=selector["prediction_revision_id"],
            as_of_time=checkpoint_text,
        )
    except formal_prediction_batches.FormalPredictionBatchError as exc:
        raise SequentialReplayCandidateLoaderError(exc.code) from None
    if prediction["data_snapshot_id"] != selector["data_snapshot_id"]:
        raise SequentialReplayCandidateLoaderError("replay_selector_snapshot_mismatch")
    if prediction["payload"]["prediction_batch_id"] != card["prediction_batch_id"]:
        raise SequentialReplayCandidateLoaderError("replay_experience_prediction_mismatch")

    observed_at = _visible_time(card["as_of_time"], checkpoint_time, "replay_observed_after_as_of")
    matured_at = _visible_time(card["evaluation_as_of"], checkpoint_time, "replay_maturity_after_as_of")
    persisted_at = _visible_time(
        experience["persisted_at"], checkpoint_time, "replay_available_after_as_of"
    )
    prediction_available = _visible_time(
        prediction["authorization_at"], checkpoint_time, "replay_available_after_as_of"
    )
    available_at = max(persisted_at, prediction_available)
    evidence_values = [
        card["as_of_time"],
        card["evaluation_as_of"],
        experience["persisted_at"],
        prediction["authorization_at"],
    ]
    eligible_at = card.get("eligible_for_retrieval_at")
    if eligible_at is not None:
        eligible_time = _visible_time(eligible_at, checkpoint_time, "replay_available_after_as_of")
        available_at = max(available_at, eligible_time)
        evidence_values.append(eligible_at)
    if card.get("created_at") is not None:
        evidence_values.append(card["created_at"])
    if available_at < observed_at:
        raise SequentialReplayCandidateLoaderError("replay_available_precedes_observed")
    visibility = card["visibility_mode"]
    scoreability = card["scoreability"]
    reasons = _experience_reasons(card["exclusion_reasons"])
    if visibility == "reconstructed":
        scoreability = "unscorable"
        reasons = sorted(set([*reasons, "reconstructed_evidence"]))
    return {
        "item_id": _item_id("experience", selector),
        "kind": "settlement",
        "product": selector["product"],
        "node_id": selector["node_id"],
        "observed_at": observed_at.isoformat(),
        "available_at": available_at.isoformat(),
        "evidence_times": _evidence_times(evidence_values, checkpoint_time),
        "evidence_status": "reconstructed" if visibility == "reconstructed" else "point_in_time",
        "scoreability": scoreability,
        "exclusion_reasons": reasons,
        "metric_values": _experience_metrics(card),
        "matured_horizon": selector["horizon"],
        "matured_at": matured_at.isoformat(),
        "completed_horizons": experience["completed_horizons"],
    }


def _validate_explicit_selector(selector: dict[str, Any]) -> None:
    for field in ("revision_id", "data_snapshot_id", "product", "node_id"):
        value = selector[field]
        if type(value) is not str or not value or len(value) > MAX_SELECTOR_ID_LENGTH:
            raise SequentialReplayCandidateLoaderError(f"replay_selector_{field}_invalid")
    if selector["product"] not in {"poy", "dty"}:
        raise SequentialReplayCandidateLoaderError("replay_selector_product_invalid")
    if selector["node_id"] not in EXPECTED_FORMAL_NODES:
        raise SequentialReplayCandidateLoaderError("replay_selector_node_id_invalid")
    if type(selector["horizon"]) is not int or selector["horizon"] not in {1, 7, 30}:
        raise SequentialReplayCandidateLoaderError("replay_selector_horizon_invalid")
    for field in ("prediction_revision_id", "calendar_id", "calendar_version"):
        if field in selector:
            value = selector[field]
            if type(value) is not str or not value or len(value) > MAX_SELECTOR_ID_LENGTH:
                raise SequentialReplayCandidateLoaderError(f"replay_selector_{field}_invalid")


def _exact_cell(cells: Any, node_id: str, horizon: int) -> dict[str, Any]:
    matches = [cell for cell in cells if cell["node_id"] == node_id and cell["horizon_days"] == horizon]
    if not matches:
        raise SequentialReplayCandidateLoaderError("replay_selector_node_horizon_mismatch")
    if len(matches) != 1:
        raise SequentialReplayCandidateLoaderError("replay_formal_cell_ambiguous")
    return matches[0]


def _formal_projection(cell: dict[str, Any], node_id: str, product: str) -> dict[str, Any]:
    if node_id != TERMINAL_NODE:
        if cell["subtarget_results"]:
            raise SequentialReplayCandidateLoaderError("replay_formal_subtarget_ambiguous")
        return cell
    matches = [item for item in cell["subtarget_results"] if item["target"] == product]
    if len(matches) != 1:
        raise SequentialReplayCandidateLoaderError("replay_selector_product_mismatch")
    return matches[0]


def _validate_product_binding(node_id: str, product: str, subtarget: Any) -> None:
    if node_id == TERMINAL_NODE:
        if subtarget != product:
            raise SequentialReplayCandidateLoaderError("replay_selector_product_mismatch")
    elif subtarget is not None:
        raise SequentialReplayCandidateLoaderError("replay_selector_product_mismatch")


def _formal_scoreability(projected: dict[str, Any]) -> tuple[str, list[str]]:
    scoreability = projected["scoreability"]
    if scoreability == "scorable":
        return scoreability, []
    if scoreability not in {"blocked", "unscorable"}:
        raise SequentialReplayCandidateLoaderError("replay_formal_scoreability_invalid")
    reasons = ["formal_prediction_unscorable"]
    if projected["missing_series_ids"]:
        reasons.append("missing_series_evidence")
    return scoreability, reasons


def _experience_reasons(raw: Any) -> list[str]:
    if type(raw) is not list or len(raw) > 15:
        raise SequentialReplayCandidateLoaderError("replay_experience_exclusion_reasons_invalid")
    reasons: list[str] = []
    for reason in raw:
        if type(reason) is not str or not _REASON_CODE.fullmatch(reason):
            raise SequentialReplayCandidateLoaderError("replay_experience_exclusion_reason_invalid")
        reasons.append(reason)
    return sorted(set(reasons))


def _experience_metrics(card: dict[str, Any]) -> dict[str, float | int]:
    metrics = _numeric_metrics(
        card,
        (
            "start_price",
            "end_price",
            "raw_change_abs",
            "raw_change_pct",
            "relative_change",
            "mfe",
            "mae",
            "days_to_peak",
        ),
    )
    completeness = card.get("data_completeness")
    if type(completeness) is dict and type(completeness.get("overall")) in {int, float}:
        value = completeness["overall"]
        if type(value) is not bool and math.isfinite(float(value)):
            if abs(value) > MAX_ABS_METRIC_VALUE:
                raise SequentialReplayCandidateLoaderError("replay_metric_value_out_of_range")
            metrics["data_completeness"] = value
    return dict(sorted(metrics.items()))


def _numeric_metrics(source: dict[str, Any], fields: Sequence[str]) -> dict[str, float | int]:
    result: dict[str, float | int] = {}
    for field in fields:
        value = source.get(field)
        if type(value) in {int, float} and (type(value) is int or math.isfinite(value)):
            if abs(value) > MAX_ABS_METRIC_VALUE:
                raise SequentialReplayCandidateLoaderError("replay_metric_value_out_of_range")
            result[field] = value
    return dict(sorted(result.items()))


def _item_id(prefix: str, selector: dict[str, Any]) -> str:
    encoded = json.dumps(selector, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return f"{prefix}:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


def _evidence_times(values: Sequence[Any], checkpoint: datetime) -> list[str]:
    normalized = {_visible_time(value, checkpoint, "replay_evidence_after_as_of").isoformat() for value in values}
    return sorted(normalized, key=datetime.fromisoformat)


def _canonical_time(value: Any, error: str) -> datetime:
    if type(value) is not str or not _CANONICAL_RFC3339.fullmatch(value) or value.endswith("-00:00"):
        raise SequentialReplayCandidateLoaderError(error)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SequentialReplayCandidateLoaderError(error) from None
    if parsed.utcoffset() is None:
        raise SequentialReplayCandidateLoaderError(error)
    return parsed


def _visible_time(value: Any, checkpoint: datetime, error: str) -> datetime:
    parsed = _canonical_time(value, error)
    if parsed > checkpoint:
        raise SequentialReplayCandidateLoaderError(error)
    return parsed

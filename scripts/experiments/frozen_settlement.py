"""Isolated issue-calendar labels; never resettle a production ledger.

Declared availability is bounded by observation completion. Historical source
availability must still be established separately in the experiment manifest.
"""

from __future__ import annotations

import math
from datetime import timedelta

from scripts.experiments.cached_replay_port import digest
from scripts.experiments.reflection_protocol import (
    POLICY,
    clock,
    effective_available_at,
    observation_clock,
    observation_day,
    settlement_band,
)

LEGACY_POLICY = "replay-fixed-neutral-band.v1"
IDENTITY_KEYS = ("series_id", "source_id", "unit", "contract_version")


def settle_frozen_cell(
    issued: dict, prices: list[dict], *, known_at: str, policy: str
) -> dict:
    if policy not in {POLICY, LEGACY_POLICY}:
        raise ValueError("unregistered_settlement_policy")
    horizon = issued["horizon_days"]
    if type(horizon) is not int or horizon not in {1, 7, 30}:
        raise ValueError("unsupported_horizon")
    issuance, cutoff = clock(issued["as_of_time"]), clock(known_at)
    if cutoff < issuance:
        raise ValueError("settlement_before_issuance")
    identity = dict(issued["label_identity"])
    if any(
        not isinstance(identity.get(key), str) or not identity[key]
        for key in IDENTITY_KEYS
    ):
        raise ValueError("frozen_label_identity_required")
    points = sorted(
        prices, key=lambda row: (observation_clock(row), row["observation_id"])
    )
    if len({row["observation_id"] for row in points}) != len(points) or len(
        {observation_day(row) for row in points}
    ) != len(points):
        raise ValueError("ambiguous_price_revisions")
    for row in points:
        if any(row.get(key) != identity[key] for key in IDENTITY_KEYS):
            raise ValueError("matched_label_identity_required")
        if (
            type(row.get("value")) not in {int, float}
            or not math.isfinite(row["value"])
            or row["value"] <= 0
        ):
            raise ValueError("invalid_price_value")
    history = [row for row in points if effective_available_at(row) <= issuance]
    if not history or history[-1]["observation_id"] != issued["origin_observation_id"]:
        raise ValueError("frozen_origin_not_latest_known_price")
    origin = history[-1]
    if policy == POLICY:
        normalized_history = [
            {**row, "visible_at": effective_available_at(row).isoformat()}
            for row in history
        ]
        band = settlement_band(
            normalized_history, as_of=issued["as_of_time"], horizon=horizon
        )
    else:
        fixed = issued["neutral_band"]
        if (
            type(fixed) not in {int, float}
            or not math.isfinite(fixed)
            or not 0 < fixed < 1
        ):
            raise ValueError("invalid_frozen_neutral_band")
        band = {
            "policy": policy,
            "neutral_band": fixed,
            "price_input_sha256": digest(history),
        }
    from zoneinfo import ZoneInfo

    business_day = issuance.astimezone(ZoneInfo("Asia/Shanghai")).date()
    if issued["business_date"] != business_day.isoformat():
        raise ValueError("issued_business_date_mismatch")
    due = (business_day + timedelta(days=horizon)).isoformat()
    candidates = [row for row in points if observation_day(row) >= due]
    result = {
        "business_date": issued["business_date"],
        "target": issued["target"],
        "horizon_days": horizon,
        "settlement_policy": policy,
        "due_date": due,
        "origin_observation_id": origin["observation_id"],
        "baseline_direction": issued["baseline_direction"],
        "event_adjusted_direction": issued["event_adjusted_direction"],
        "band": band,
        "label_identity": identity,
        "issued_input_sha256": digest(issued),
    }
    if any(
        result[field] not in {"up", "down", "neutral"}
        for field in ("baseline_direction", "event_adjusted_direction")
    ):
        raise ValueError("invalid_frozen_direction")
    if not candidates or effective_available_at(candidates[0]) > cutoff:
        return {**result, "status": "pending_maturity"}
    actual = candidates[0]
    change = actual["value"] / origin["value"] - 1
    direction = (
        "up"
        if change > band["neutral_band"]
        else "down"
        if change < -band["neutral_band"]
        else "neutral"
    )
    return {
        **result,
        "status": "settled",
        "actual_observation_id": actual["observation_id"],
        "settled_available_at": effective_available_at(actual).isoformat(),
        "actual_change_pct": change * 100,
        "actual_direction": direction,
        "outcome_baseline": "hit"
        if issued["baseline_direction"] == direction
        else "miss",
        "outcome_adjusted": "hit"
        if issued["event_adjusted_direction"] == direction
        else "miss",
    }

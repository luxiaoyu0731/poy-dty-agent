"""Preregistered reflection experiment helpers; no production activation.

Settlement bands use only timestamped pre-issuance prices. Distillation corpus
contains matured switched cells only. Both A/B arms must share this policy.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import date, datetime, timezone

POLICY = "reflection-experiment.v2-switched-pit-band"
DIRECTIONS = {"up", "down", "neutral"}


def clock(value: str) -> datetime:
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("reflection_clock_must_be_aware")
    return at


def observation_clock(row: dict) -> datetime:
    """Normalize intraday clocks; day precision denotes a UTC calendar day."""
    observed = row["observed_at"]
    if len(observed) == 10:
        return datetime.combine(
            date.fromisoformat(observed), datetime.min.time(), tzinfo=timezone.utc
        )
    parsed = clock(observed)
    if row.get("time_precision") == "day":
        return datetime.combine(parsed.date(), datetime.min.time(), tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def observation_day(row: dict) -> str:
    return observation_clock(row).date().isoformat()


def effective_available_at(row: dict) -> datetime:
    from datetime import timedelta

    observed = observation_clock(row)
    completed = (
        observed + timedelta(days=1)
        if len(row["observed_at"]) == 10 or row.get("time_precision") == "day"
        else observed
    )
    return max(clock(row["visible_at"]), completed)


def settlement_band(observations: list[dict], *, as_of: str, horizon: int) -> dict:
    if horizon not in {1, 7, 30}:
        raise ValueError("unsupported_horizon")
    cutoff = clock(as_of)
    # Visibility alone cannot expose a later intraday value or that day's close.
    declared = [row for row in observations if clock(row["visible_at"]) <= cutoff]
    if any(observation_clock(row) > cutoff for row in declared):
        raise ValueError("future_price_observation")
    eligible = [row for row in declared if effective_available_at(row) <= cutoff]
    if len({observation_day(row) for row in eligible}) != len(eligible):
        raise ValueError("duplicate_price_day")
    visible = sorted(
        eligible, key=lambda row: (observation_clock(row), row["observation_id"])
    )[-120:]
    keys = [row["observation_id"] for row in visible]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate_price_observation")
    values = [float(row["value"]) for row in visible]
    if len(values) < 30 or any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("insufficient_or_invalid_price_history")
    returns = [math.log(right / left) for left, right in zip(values, values[1:])]
    median = statistics.median(returns)
    mad = statistics.median(abs(r - median) for r in returns)
    sigma = max(1.4826 * mad, statistics.pstdev(returns), 1e-6)
    band = 0.005 if horizon == 1 else max(0.005, 0.5 * sigma * math.sqrt(horizon))
    return {
        "policy": POLICY,
        "as_of": as_of,
        "horizon_days": horizon,
        "neutral_band": band,
        "history_points": len(values),
        "price_input_sha256": hashlib.sha256(
            json.dumps(visible, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def reflection_corpus(cells: list[dict], *, known_at: str) -> dict:
    cutoff = clock(known_at)
    accepted, excluded, seen = [], {}, set()
    for cell in cells:
        reason = None
        key = (cell.get("business_date"), cell.get("target"), cell.get("horizon_days"))
        band = cell.get("band") or {}
        change = cell.get("actual_change_pct")
        neutral = band.get("neutral_band")
        if cell.get("settlement_policy") != POLICY:
            reason = "different_settlement_policy"
        elif (
            not cell.get("settled_available_at")
            or clock(cell["settled_available_at"]) > cutoff
        ):
            reason = "not_known_settled"
        elif (
            cell.get("baseline_direction") not in DIRECTIONS
            or cell.get("event_adjusted_direction") not in DIRECTIONS
            or cell.get("actual_direction") not in DIRECTIONS
        ):
            reason = "invalid_direction"
        elif (
            type(change) not in {int, float}
            or not math.isfinite(change)
            or type(neutral) not in {int, float}
            or not math.isfinite(neutral)
            or not 0 < neutral < 1
            or band.get("policy") != POLICY
        ):
            reason = "missing_frozen_label_band"
        elif cell["actual_direction"] != (
            "up"
            if change / 100 > neutral
            else "down"
            if change / 100 < -neutral
            else "neutral"
        ):
            reason = "actual_label_mismatch"
        elif cell["baseline_direction"] == cell["event_adjusted_direction"]:
            reason = "not_switched"
        elif cell.get("outcome_baseline") != (
            "hit" if cell["baseline_direction"] == cell["actual_direction"] else "miss"
        ) or cell.get("outcome_adjusted") != (
            "hit"
            if cell["event_adjusted_direction"] == cell["actual_direction"]
            else "miss"
        ):
            reason = "outcome_mismatch"
        elif key in seen:
            reason = "duplicate_cell"
        if reason:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue
        seen.add(key)
        accepted.append(dict(cell))
    accepted.sort(
        key=lambda row: (
            clock(row["settled_available_at"]),
            row["business_date"],
            row["target"],
            row["horizon_days"],
        )
    )
    return {
        "policy": POLICY,
        "known_at": known_at,
        "cells": accepted[-120:],
        "excluded": excluded,
    }


BACKGROUND_LESSON_INSTRUCTION = (
    "教训仅作为背景假设，不是门槛、置信上限或方向命令。"
    "必须用本次事件的原文和执行证据独立判断；教训与本次证据矛盾时以本次证据为准。"
    "引用 lesson_id 说明适用条件，不得仅因历史成败将本次判断机械降级。"
)

"""Prospective calibration labels, separate from immutable issuance/OOS labels."""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import UTC, datetime, timedelta

POLICY = "reflection-live.v2-switched-pit-band"


def clock(value: str) -> datetime:
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("timestamp_requires_timezone")
    return at


def freeze_bands(loaded, as_of: str) -> dict:
    """Price history available at issuance only; no proxy/source mixing."""
    cutoff = clock(as_of)
    if not loaded.source_matches_label:
        raise ValueError("label_source_unavailable")
    rows = []
    for point in loaded.points:
        observed = point.observed_at
        completed = (
            datetime.fromisoformat(observed).replace(tzinfo=UTC) + timedelta(days=1)
            if len(observed) == 10
            else clock(observed)
        )
        if max(completed, clock(point.visible_at)) <= cutoff:
            rows.append(point)
    rows.sort(key=lambda p: (p.observed_at, p.observation_id))
    rows = rows[-120:]
    if len(rows) < 30 or len({p.observed_at[:10] for p in rows}) != len(rows):
        raise ValueError("insufficient_or_duplicate_history")
    identities = {(p.semantic_series_id, p.source_id, p.unit, p.contract_version) for p in rows}
    if len(identities) != 1 or not rows[0].semantic_series_id or not rows[0].contract_version:
        raise ValueError("mixed_or_unknown_label_contract")
    values = [float(p.value) for p in rows]
    if any(not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError("invalid_price_history")
    returns = [math.log(b / a) for a, b in zip(values, values[1:], strict=False)]
    median = statistics.median(returns)
    sigma = max(1.4826 * statistics.median(abs(r - median) for r in returns), statistics.pstdev(returns), 1e-6)
    digest = hashlib.sha256(
        json.dumps(
            [
                [
                    p.observation_id,
                    p.observed_at,
                    p.visible_at,
                    p.value,
                    p.source_id,
                    p.unit,
                    p.semantic_series_id,
                    p.contract_version,
                ]
                for p in rows
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return {
        "policy": POLICY,
        "as_of_time": as_of,
        "history_points": len(rows),
        "price_input_sha256": digest,
        "identity": list(next(iter(identities))),
        "bands": {str(h): 0.005 if h == 1 else max(0.005, 0.5 * sigma * math.sqrt(h)) for h in (1, 7, 30)},
    }


def calibration_sample(item: dict, *, known_at: str) -> dict | None:
    """Only matured switched cells with an issuance-time frozen band can teach."""
    try:
        meta = item.get("metadata") or {}
        frozen = meta.get("reflection_band") or {}
        horizon = int(item["horizon_days"])
        issued = clock(item["as_of_time"])
        settled = clock(item["settled_at"])
        visible = clock(item["actual_visible_at"])
        if (
            frozen.get("policy") != POLICY
            or clock(frozen["as_of_time"]) != issued
            or max(settled, visible) > clock(known_at)
            or min(settled, visible) <= issued
            or item["baseline_direction"] == item["event_adjusted_direction"]
            or item["baseline_direction"] not in {"up", "down", "neutral"}
            or item["event_adjusted_direction"] not in {"up", "down", "neutral"}
            or len(frozen.get("price_input_sha256", "")) != 64
        ):
            return None
        due = datetime.fromisoformat(item["business_date"]).date() + timedelta(days=horizon)
        if datetime.fromisoformat(item["actual_observed_at"][:10]).date() < due:
            return None
        band = float(frozen["bands"][str(horizon)])
        payload = item["cell_payload"]
        base = float(payload["latest_value"])
        actual = float(item["actual_value"])
        identity = frozen["identity"]
        if (
            horizon not in {1, 7, 30}
            or not 0 < band < 1
            or not math.isfinite(actual)
            or not math.isfinite(base)
            or base <= 0
            or actual <= 0
            or identity[0] != item["label_series_id"]
            or identity[1] != item["actual_source_id"]
            or identity[2] != item["actual_unit"]
            or item["unit"] != item["actual_unit"]
        ):
            return None
        change = actual / base - 1
        direction = "up" if change > band else "down" if change < -band else "neutral"
        return {
            "sample_id": item["factor_id"],
            "business_date": item["business_date"],
            "batch_id": item["batch_id"],
            "target": item["target"],
            "horizon_days": horizon,
            "baseline_direction": item["baseline_direction"],
            "event_adjusted_direction": item["event_adjusted_direction"],
            "outcome_baseline": "hit" if direction == item["baseline_direction"] else "miss",
            "outcome_adjusted": "hit" if direction == item["event_adjusted_direction"] else "miss",
            "actual_direction": direction,
            "neutral_band": band,
            "policy": POLICY,
            "chain_run_id": meta.get("chain_run_id"),
            "input_sha256": meta.get("input_sha256"),
            "settled_available_at": max(settled, visible).isoformat(),
        }
    except (KeyError, ValueError, TypeError, IndexError, OverflowError):
        return None

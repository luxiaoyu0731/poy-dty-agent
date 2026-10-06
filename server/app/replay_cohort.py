"""Frozen ranked-day receipt; pool window is distinct from selected coverage.

This validates reconstruction and pairing, not historical source availability.
The historical evidence certificate must be reviewed independently.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from copy import deepcopy
from datetime import date, timedelta

SCHEMA = "frozen-ranked-cohort.v1"
POLICY = "absolute-return-ranked.v1"
IDENTITY_FIELDS = ("series_id", "source_id", "unit", "contract_version")


def fingerprint(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def build_cohort(
    *,
    observations: list[dict],
    events: list[dict],
    data_sha256: str,
    window_start: str,
    window_end: str,
    limit: int = 60,
) -> dict:
    start, end = date.fromisoformat(window_start), date.fromisoformat(window_end)
    if start > end or type(limit) is not int or limit <= 0 or not re.fullmatch(r"[0-9a-f]{64}", data_sha256):
        raise ValueError("invalid_cohort_configuration")
    points = sorted(deepcopy(observations), key=lambda point: point["observed_day"])
    if (
        not points
        or len({p["observed_day"] for p in points}) != len(points)
        or len({p["observation_id"] for p in points}) != len(points)
    ):
        raise ValueError("ambiguous_cohort_price_curve")
    identity = {key: points[0].get(key) for key in IDENTITY_FIELDS}
    if any(not isinstance(value, str) or not value for value in identity.values()):
        raise ValueError("cohort_price_identity_required")
    for point in points:
        date.fromisoformat(point["observed_day"])
        if (
            any(point.get(key) != value for key, value in identity.items())
            or type(point.get("value")) not in {int, float}
            or not math.isfinite(point["value"])
            or point["value"] <= 0
            or not isinstance(point.get("observation_id"), str)
            or not point["observation_id"]
        ):
            raise ValueError("invalid_cohort_price_point")
    by_day = {}
    seen = set()
    for event in events:
        anchor = date.fromisoformat(event["created_day"])
        ref = event["event_revision_id"]
        if not isinstance(ref, str) or not ref or ref in seen:
            raise ValueError("duplicate_or_invalid_cohort_event_revision")
        seen.add(ref)
        morning = anchor + timedelta(days=1)
        if start <= morning <= end:
            by_day.setdefault(anchor.isoformat(), []).append(deepcopy(event))
    # Keep the legacy 35-calendar-day closing margin explicit and frozen.
    maturity_cutoff = date.fromisoformat(points[-1]["observed_day"]) - timedelta(days=35)
    pool = []
    for previous, current in zip(points, points[1:], strict=False):
        anchor = date.fromisoformat(current["observed_day"])
        refs = by_day.get(anchor.isoformat())
        absolute_return = abs(current["value"] / previous["value"] - 1)
        if refs and anchor <= maturity_cutoff and absolute_return > 0:
            pool.append(
                {
                    "business_date": (anchor + timedelta(days=1)).isoformat(),
                    "anchor_day": anchor.isoformat(),
                    "events": sorted(refs, key=lambda row: row["event_revision_id"]),
                    "price_pair": [previous, current],
                    "absolute_return": absolute_return,
                }
            )
    selected = sorted(pool, key=lambda row: (-row["absolute_return"], row["business_date"]))[:limit]
    body = {
        "schema_version": SCHEMA,
        "selection_policy": POLICY,
        "data_sha256": data_sha256,
        "window_start": window_start,
        "window_end": window_end,
        "limit": limit,
        "maturity_margin_days": 35,
        "latest_price_day": points[-1]["observed_day"],
        "price_identity": identity,
        "eligible_pool": pool,
        "pool_sha256": fingerprint(pool),
        "selected_dates": [row["business_date"] for row in selected],
        "sample_sha256": fingerprint(sorted(row["business_date"] for row in selected)),
        "source_availability_certified": False,
    }
    return {**body, "receipt_sha256": fingerprint(body)}


def validate_cohort(
    receipt: dict,
    *,
    data_sha256: str,
    sample_sha256: str,
    expected_window: tuple[str, str] = ("2001-01-01", "2025-12-31"),
    limit: int = 60,
) -> set[str]:
    if not isinstance(receipt, dict):
        raise ValueError("frozen_cohort_receipt_required")
    body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    if receipt.get("receipt_sha256") != fingerprint(body):
        raise ValueError("cohort_receipt_changed")
    if (
        receipt.get("schema_version") != SCHEMA
        or receipt.get("selection_policy") != POLICY
        or (receipt.get("window_start"), receipt.get("window_end")) != expected_window
        or receipt.get("limit") != limit
        or receipt.get("data_sha256") != data_sha256
        or receipt.get("maturity_margin_days") != 35
    ):
        raise ValueError("cohort_protocol_mismatch")
    identity = receipt.get("price_identity")
    if not isinstance(identity, dict) or any(
        not isinstance(identity.get(key), str) or not identity[key] for key in IDENTITY_FIELDS
    ):
        raise ValueError("cohort_price_identity_required")
    pool = receipt["eligible_pool"]
    if not isinstance(pool, list) or len(pool) < limit or receipt["pool_sha256"] != fingerprint(pool):
        raise ValueError("incomplete_or_changed_cohort_pool")
    days = set()
    revisions = set()
    prices = {}
    observed_ids = {}
    start, end = map(date.fromisoformat, expected_window)
    cutoff = date.fromisoformat(receipt["latest_price_day"]) - timedelta(days=35)
    for row in pool:
        day, anchor = date.fromisoformat(row["business_date"]), date.fromisoformat(row["anchor_day"])
        if not start <= day <= end or day != anchor + timedelta(days=1) or anchor > cutoff or day in days:
            raise ValueError("invalid_or_duplicate_cohort_day")
        days.add(day)
        pair = row["price_pair"]
        if not isinstance(pair, list) or len(pair) != 2 or not row.get("events"):
            raise ValueError("cohort_price_and_event_witness_required")
        for point in pair:
            observed = date.fromisoformat(point["observed_day"])
            if (
                any(point.get(key) != receipt["price_identity"].get(key) for key in IDENTITY_FIELDS)
                or type(point.get("value")) not in {int, float}
                or not math.isfinite(point["value"])
                or point["value"] <= 0
            ):
                raise ValueError("invalid_cohort_price_witness")
            identity = point.get("observation_id")
            if (
                not isinstance(identity, str)
                or not identity
                or (identity in observed_ids and observed_ids[identity] != point)
            ):
                raise ValueError("invalid_or_duplicate_cohort_price_identity")
            observed_ids[identity] = point
            if observed in prices and prices[observed] != point:
                raise ValueError("conflicting_cohort_price_revision")
            prices[observed] = point
        if date.fromisoformat(pair[0]["observed_day"]) >= anchor or pair[1]["observed_day"] != row["anchor_day"]:
            raise ValueError("cohort_anchor_price_mismatch")
        if (
            type(row.get("absolute_return")) not in {int, float}
            or not math.isfinite(row["absolute_return"])
            or row["absolute_return"] != abs(pair[1]["value"] / pair[0]["value"] - 1)
            or not row["absolute_return"] > 0
        ):
            raise ValueError("cohort_return_mismatch")
        for event in row["events"]:
            ref = event["event_revision_id"]
            if not isinstance(ref, str) or not ref or ref in revisions or event["created_day"] != row["anchor_day"]:
                raise ValueError("invalid_cohort_event_witness")
            revisions.add(ref)
    ranked = sorted(pool, key=lambda row: (-row["absolute_return"], row["business_date"]))[:limit]
    selected = [row["business_date"] for row in ranked]
    if (
        receipt["selected_dates"] != selected
        or receipt["sample_sha256"] != fingerprint(sorted(selected))
        or sample_sha256 != receipt["sample_sha256"]
    ):
        raise ValueError("cohort_selected_sample_mismatch")
    return set(selected)

"""Offline, date-precision ALFRED snapshots for isolated replay preparation.

An ALFRED vintage is a calendar date, not an intraday publication timestamp.
Use the latest calendar day completed even in UTC-12. This deliberately lags
issuance, and does NOT certify the latest price known at an intraday cutoff.
No network, database, production contract replacement, or model calls.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from scripts.experiments.cached_replay_port import digest

POLICY = "alfred-completed-vintage-utc-minus12.v1"
IDENTITY = {
    "series_id": "crude.brent.fred.history",
    "source_id": "fred_spot_history",
    "unit": "USD/bbl",
    "contract_version": POLICY,
}
ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"


def baseline_context(snapshot: dict) -> dict:
    """Recompute the champion prompt formula using admitted prices only.

    Keep the 120-observation window and 0.5% floor. A source vintage changes
    which prices were known; it does not certify the six absent products.
    """
    from app.seven_product_forecast import robust_drift_projection

    points = snapshot["prices"]
    if len(points) < 30:
        raise ValueError("strict_baseline_needs_30_known_prices")
    if any(
        {key: point[key] for key in IDENTITY} != IDENTITY
        or _clock(point["visible_at"]) > _clock(snapshot["as_of_time"])
        for point in points
    ):
        raise ValueError("strict_baseline_source_or_clock_mismatch")
    context = {}
    for horizon in (1, 7, 30):
        result = robust_drift_projection(
            [p["value"] for p in points[-120:]],
            horizon_days=horizon,
            neutral_floor_pct=0.005,
            observation_days=[p["observed_at"] for p in points[-120:]],
        )
        context[f"d{horizon}"] = {
            "direction": result.direction,
            "predicted_change_pct": round(result.predicted_change_pct * 100, 3),
            "neutral_band_pct": round(result.neutral_band_pct * 100, 3),
        }
    return {"crude": context}


def _day(value: str) -> date:
    if not isinstance(value, str):
        raise ValueError("invalid_alfred_date")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("invalid_alfred_date")
    return parsed


def _clock(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("aware_issuance_required")
    return parsed.astimezone(timezone.utc)


def completed_day_upper_bound(day: date) -> datetime:
    """Conservative boundary, never an invented original publication hour."""
    return datetime.combine(
        day + timedelta(days=1), datetime.min.time(), timezone.utc
    ) + timedelta(hours=12)


class AlfredPriceArchive:
    def __init__(self, receipt: dict):
        params, body = receipt.get("public_parameters", {}), receipt.get("body", {})
        if (
            receipt.get("endpoint") != ENDPOINT
            or receipt.get("http_status") != 200
            or receipt.get("key_value_recorded") is not False
            or "api_key" in params
            or receipt.get("complete") is not True
            or params.get("series_id") != "DCOILBRENTEU"
            or params.get("output_type") != 1
            or body.get("output_type") != 1
            or body.get("units") != "lin"
            or params.get("realtime_start") != "1776-07-04"
            or params.get("realtime_end") != "9999-12-31"
            or body.get("offset") != 0
        ):
            raise ValueError("complete_original_units_alfred_archive_required")
        rows = body.get("observations")
        if (
            not isinstance(rows, list)
            or type(body.get("count")) is not int
            or len(rows) != body["count"]
        ):
            raise ValueError("incomplete_alfred_archive")
        self.start = _day(params["observation_start"])
        self.end = _day(params["observation_end"])
        if (
            self.start > self.end
            or body.get("observation_start") != self.start.isoformat()
            or body.get("observation_end") != self.end.isoformat()
        ):
            raise ValueError("alfred_observation_range_mismatch")
        self.archive_sha256 = digest(body)
        self.versions = defaultdict(list)
        for raw in rows:
            observed, start, end = (
                _day(raw[key]) for key in ("date", "realtime_start", "realtime_end")
            )
            if start > end or not self.start <= observed <= self.end:
                raise ValueError("invalid_alfred_interval")
            value = raw.get("value")
            if value == ".":
                number = None
            else:
                if not isinstance(value, str):
                    raise ValueError("invalid_alfred_price")
                number = float(value)
                if not math.isfinite(number) or number <= 0:
                    raise ValueError("invalid_alfred_price")
            self.versions[observed].append(
                {
                    "observed": observed,
                    "start": start,
                    "end": end,
                    "value": number,
                    "raw": {
                        key: raw[key]
                        for key in ("date", "realtime_start", "realtime_end", "value")
                    },
                }
            )
        for versions in self.versions.values():
            versions.sort(key=lambda version: version["start"])
            for previous, current in zip(versions, versions[1:]):
                if current["start"] <= previous["end"]:
                    raise ValueError("ambiguous_alfred_intervals")

    def snapshot(self, *, as_of_time: str) -> dict:
        cutoff = _clock(as_of_time)
        # End of vintage day in UTC-12 must have passed by issuance.
        vintage = (cutoff - timedelta(hours=12)).date() - timedelta(days=1)
        if vintage < self.start:
            raise ValueError("issuance_before_requested_observation_range")
        if vintage > self.end:
            raise ValueError("observation_archive_does_not_cover_cutoff")
        prices, missing = [], []
        for observed, versions in sorted(self.versions.items()):
            if observed > vintage:
                continue
            selected = next(
                (row for row in versions if row["start"] <= vintage <= row["end"]), None
            )
            if selected is None:
                continue
            if selected["value"] is None:
                # Missing/deleted state at this vintage must not revive an old value.
                missing.append(observed.isoformat())
                continue
            available = max(
                completed_day_upper_bound(observed),
                completed_day_upper_bound(selected["start"]),
            )
            assert available <= cutoff
            version = {
                "observed_date": observed.isoformat(),
                "vintage_start": selected["start"].isoformat(),
                "value": selected["value"],
            }
            prices.append(
                {
                    **IDENTITY,
                    "observation_id": "alfred:" + digest(version),
                    "observed_at": observed.isoformat(),
                    "visible_at": available.isoformat(),
                    "value": selected["value"],
                    "publication_precision": "date",
                    "publication_date": selected["start"].isoformat(),
                    "availability_basis": POLICY,
                }
            )
        projection = {
            "policy": POLICY,
            "as_of_time": cutoff.isoformat(),
            "information_vintage_date": vintage.isoformat(),
            "vintage_available_at_upper_bound": completed_day_upper_bound(
                vintage
            ).isoformat(),
            "label_identity": dict(IDENTITY),
            "prices": prices,
            "missing_observation_dates": missing,
            "status": "known_source_subset" if prices else "no_known_price_history",
            "latest_intraday_price_certified": False,
        }
        return {
            **projection,
            # Future archive contents belong to the receipt, never input identity.
            "snapshot_sha256": digest(projection),
            "archive_body_sha256": self.archive_sha256,
        }

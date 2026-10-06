"""Audit immutable public prediction exports; no database or model calls.

Scores retain the original frozen direction labels. A separate strictly later
observation-day cohort distinguishes forward forecasts from delayed-price
nowcasts. Neither cohort silently relabels or overwrites historical predictions.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo

THRESHOLDS = (0.0, 0.45, 0.50, 0.55, 0.60, 0.65)


def _time(value: str) -> datetime:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("issue_and_visibility_times_must_be_timezone_aware")
    return stamp


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "accuracy": None, "balanced_accuracy": None,
                "neutral_baseline_accuracy": None, "non_neutral_calls": 0,
                "non_neutral_accuracy": None, "selective": []}
    truth = Counter(row["actual"] for row in rows)
    prediction = Counter(row["predicted"] for row in rows)
    hits = [row["predicted"] == row["actual"] for row in rows]
    directional = [row for row in rows if row["predicted"] in {"up", "down"}]
    recalls = {
        label: sum(row["predicted"] == label and row["actual"] == label for row in rows) / count
        for label, count in truth.items()
    }
    selective = []
    for threshold in THRESHOLDS:
        chosen = [row for row in rows if row["confidence"] >= threshold]
        selective.append({
            "threshold": threshold, "selected": len(chosen),
            "coverage_of_scored_cohort": len(chosen) / len(rows),
            "accuracy": mean(row["predicted"] == row["actual"] for row in chosen) if chosen else None,
        })
    return {
        "n": len(rows), "accuracy": mean(hits),
        "predicted_counts": dict(prediction), "actual_counts": dict(truth),
        "class_recalls": recalls,
        "balanced_accuracy": mean(recalls.values()) if len(truth) >= 2 else None,
        "balanced_accuracy_supported_classes": sorted(truth),
        "neutral_baseline_accuracy": truth["neutral"] / len(rows),
        "non_neutral_calls": len(directional),
        "non_neutral_accuracy": mean(row["predicted"] == row["actual"] for row in directional)
        if directional else None,
        "unique_actual_observations": len({row["actual_id"] for row in rows}),
        "selective": selective,
        "confidence_bins": [
            {"lower": lo, "upper": hi, "n": len(selected),
             "mean_displayed_confidence": mean(row["confidence"] for row in selected),
             "observed_accuracy": mean(row["predicted"] == row["actual"] for row in selected)}
            for lo, hi in [(0.0, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 1.01)]
            if (selected := [row for row in rows if lo <= row["confidence"] < hi])
        ],
    }


def audit_history(batches: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, int, str], dict[str, Any]] = {}
    statuses: Counter[str] = Counter()
    latest = max(batches, key=lambda batch: batch["as_of_time"]) if batches else None
    latest_due = []
    for batch in batches:
        for cell in batch["cells"]:
            forecast = cell["forecast"]
            key = (forecast["target"], forecast["horizon_days"], forecast["label_registry_version"])
            group = grouped.setdefault(key, {"issued": 0, "rows": [], "statuses": Counter(),
                "nominal_due_on_or_before_issue_day": 0, "visible_before_or_at_issue": 0,
                "neutral_bands": []})
            status = cell["settlement_status"]
            statuses[status] += 1
            group["statuses"][status] += 1
            group["issued"] += 1
            issue = _time(forecast["as_of_time"])
            issue_day = issue.astimezone(ZoneInfo("Asia/Shanghai")).date()
            origin_day = forecast.get("latest_observation_at")
            if origin_day:
                due = datetime.fromisoformat(origin_day.replace("Z", "+00:00")).date() + timedelta(days=key[1])
                group["nominal_due_on_or_before_issue_day"] += due <= issue_day
                if batch is latest:
                    latest_due.append({"target": key[0], "horizon_days": key[1],
                        "issue_day": str(issue_day), "origin_day": origin_day,
                        "nominal_due_day": str(due), "remaining_calendar_days": (due - issue_day).days})
            group["neutral_bands"].append(float(forecast["neutral_band_pct"]))
            outcome = cell.get("outcome")
            if status != "scored" or not outcome or cell.get("invalidation"):
                continue
            actual_day = datetime.fromisoformat(outcome["actual_observed_at"].replace("Z", "+00:00")).date()
            actual_visible = _time(outcome["actual_visible_at"])
            group["visible_before_or_at_issue"] += actual_visible <= issue
            group["rows"].append({
                "predicted": forecast["direction"], "actual": outcome["actual_direction"],
                "confidence": float(forecast["confidence"]),
                "actual_id": outcome["actual_observation_id"],
                "strict_forward": actual_day > issue_day and actual_visible > issue,
            })
    cells = []
    for (target, horizon, version), group in sorted(grouped.items()):
        rows = group.pop("rows")
        bands = group.pop("neutral_bands")
        cells.append({"target": target, "horizon_days": horizon,
            "label_registry_version": version, **group,
            "neutral_band_min": min(bands), "neutral_band_max": max(bands),
            "frozen_contract_scores": _metrics(rows),
            "strictly_later_observation_day": _metrics([row for row in rows if row["strict_forward"]]),
        })
    return {"schema_version": "prediction-direction-audit.v1", "batches": len(batches),
        "settlement_statuses": dict(statuses), "cells": cells, "latest_horizon_calendar": latest_due,
        "notes": [
            "Historical frozen direction bands are retained, not silently corrected.",
            "Neutral is a market-state prediction, not abstention; thresholds are hypothetical cohort filters.",
            "Coverage denominators contain scored records only; pending/invalidated counts are separate.",
            "Strict future cohort requires a later Shanghai business date and later actual visibility.",
            "Same-day or earlier observation is a timing/nowcast distinction, not by itself evidence leakage.",
            "Repeated labels and overlapping horizons are dependent, not independent trials.",
            "Confidence is an uncalibrated runtime heuristic, not a verified probability.",
        ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.history.resolve() == args.output.resolve():
        parser.error("output must not overwrite the input evidence")
    raw = args.history.read_bytes()
    report = audit_history(json.loads(raw))
    report["input"] = {"path": str(args.history.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw)}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()

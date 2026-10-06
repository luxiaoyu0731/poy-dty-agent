"""Four fixed ablations on prospective joint receipts; no live model promotion."""

from __future__ import annotations

import math
from collections import Counter
from datetime import date

import numpy as np

from .prediction_benchmark import calibrate, classification_metrics, nonoverlapping_matured, probability_metrics
from .prediction_evidence_runtime import ABLATIONS, DOSSIER_SCHEMA, JOINT_SCHEMA, joint_input, seal, verify
from .prediction_features import MIN_TRAIN_OUTCOMES, RIDGE_PENALTY, feature_vector
from .prediction_replay import SHANGHAI, direction, load_export, timestamp
from .seven_product_forecast import FRESHNESS_DAYS
from .seven_product_multivariate_experiment import fit_predict_ridge_return


def evaluate_archives(
    archives: list[dict], final_prices: dict, *, target: str, horizon: int, issued_inputs: dict[str, dict]
) -> dict:
    """Time-ordered fitting, purged overlapping labels, identical complete-case folds.

    The terminal export supplies outcomes only. It cannot supply old feature values.
    A fresh extraction or a retrospective reconstruction is not an issued archive.
    """
    final_series = load_export(final_prices)[target]
    cutoff = timestamp(final_prices["as_of_time"])
    rows, skipped, seen = [], Counter(), set()
    for archive in sorted(archives, key=lambda a: a["as_of_time"]):
        verify(archive)
        if archive.get("schema_version") not in {JOINT_SCHEMA, DOSSIER_SCHEMA}:
            raise ValueError("prospective_joint_archive_required")
        if (
            joint_input(
                archive["price_input"],
                archive["evidence_receipt"],
                include_dossier=archive["schema_version"] == DOSSIER_SCHEMA,
            )
            != archive
        ):
            raise ValueError("archive_feature_reconstruction_mismatch")
        issue = timestamp(archive["as_of_time"])
        binding = issued_inputs.get(archive["content_sha256"])
        if not isinstance(binding, dict) or binding.get("as_of_time") != archive["as_of_time"]:
            skipped["not_bound_to_issued_main_batch"] += 1
            continue
        cell = binding.get("cells", {}).get(f"{target}:{horizon}")
        if not cell or cell.get("invalidation"):
            skipped["issued_cell_missing_or_invalidated"] += 1
            continue
        if issue > cutoff or archive["content_sha256"] in seen:
            skipped["future_or_duplicate_archive"] += 1
            continue
        seen.add(archive["content_sha256"])
        # Receipt must actually have been taken during issuance, not days later.
        delay = (timestamp(archive["evidence_receipt"]["captured_at"]) - issue).total_seconds()
        if not 0 <= delay <= 300 or not archive["evidence_receipt"]["complete_requested_scope"]:
            skipped["not_prospective_complete_receipt"] += 1
            continue
        series = load_export(archive["price_input"])[target]
        if series.identity != final_series.identity:
            raise ValueError("evaluation_label_series_changed")
        view = series.view(issue)
        day = issue.astimezone(SHANGHAI).date()
        if not view.points or view.latest_blocked:
            skipped["base_unavailable"] += 1
            continue
        if (day - date.fromisoformat(view.points[-1].observed_at[:10])).days > FRESHNESS_DAYS[target]:
            skipped["stale_base"] += 1
            continue
        price_features = feature_vector(view.points, {}, day)
        packet = archive["evidence_features"][f"{target}:{horizon}"]
        if price_features is None:
            skipped["price_features_unavailable"] += 1
            continue
        if any(packet["values"][key] is None for key in ABLATIONS["price-current-history"]):
            skipped["four_arm_complete_case_unavailable"] += 1
            continue
        vectors = {
            name: [*price_features[0].tolist(), *(packet["values"][key] for key in fields)]
            for name, fields in ABLATIONS.items()
        }
        base = view.points[-1]
        if (
            cell["label_series_id"] != series.identity["series_id"]
            or cell["latest_value"] != base.value
            or cell["latest_observation_id"] != base.observation_id
        ):
            raise ValueError("issued_cell_input_identity_mismatch")
        band = float(cell["neutral_band_pct"])
        if not math.isfinite(band) or not 0 <= band < 1:
            raise ValueError("invalid_issued_neutral_band")
        actual = cell.get("outcome")
        outcome = {"state": "pending"}
        if actual and timestamp(actual["settled_at"]) <= cutoff:
            record = next((r for r in final_series.records if r.revision_id == actual["actual_observation_id"]), None)
            if (
                record is None
                or record.problems
                or record.record["value"] != actual["actual_value"]
                or record.available_at != timestamp(actual["actual_visible_at"])
                or not issue < record.available_at <= timestamp(actual["settled_at"])
                or not horizon <= (record.day - day).days <= horizon + 4
            ):
                raise ValueError("official_outcome_price_identity_mismatch")
            if record.revision_id not in {p.observation_id for p in final_series.view(record.available_at).points}:
                raise ValueError("official_outcome_quality_blocked")
            change = actual["actual_value"] / base.value - 1
            if direction(change, band) != actual["actual_direction"]:
                raise ValueError("official_outcome_neutral_band_mismatch")
            outcome = {
                "state": "scored",
                "revision_id": actual["actual_observation_id"],
                "settled_at": actual["settled_at"],
                "value": actual["actual_value"],
                "direction": actual["actual_direction"],
                "change": change,
            }
        rows.append(
            {
                "issue_at": issue.isoformat(),
                "input_sha256": archive["content_sha256"],
                "base": base.value,
                "features": vectors,
                "neutral_band": band,
                "outcome": outcome,
            }
        )
    issued, failures = [], Counter()
    for row in rows:
        issue = timestamp(row["issue_at"])
        train = nonoverlapping_matured(rows, before=issue)
        if len(train) < MIN_TRAIN_OUTCOMES:
            failures["fewer_than_40_nonoverlapping_matured_training_episodes"] += 1
            continue
        predictions = {}
        for name in ABLATIONS:
            x = np.asarray([r["features"][name] for r in train], dtype=float)
            y = np.asarray([math.log(r["outcome"]["value"] / r["base"]) for r in train])
            vector = np.asarray(row["features"][name], dtype=float)
            value = fit_predict_ridge_return(
                training_records=[{"features": a, "actual_log_return": b} for a, b in zip(x, y, strict=True)],
                feature_vector=vector,
                penalty=RIDGE_PENALTY,
            )
            raw_direction = direction(math.expm1(value), row["neutral_band"])
            previous = nonoverlapping_matured(issued, before=issue)
            calibration = calibrate(previous, model_id=name, raw_direction=raw_direction)
            predictions[name] = {"raw_direction": raw_direction, "log_return": value, "calibration": calibration}
        issued.append({**row, "models": predictions, "training_input_hashes": [r["input_sha256"] for r in train]})
    scored = nonoverlapping_matured(issued, before=cutoff)
    metrics = {}
    for name in ABLATIONS:
        calibrated = [r for r in scored if r["models"][name]["calibration"]["probabilities"] is not None]
        metrics[name] = {
            "direction": classification_metrics(
                [r["outcome"]["direction"] for r in scored], [r["models"][name]["raw_direction"] for r in scored]
            ),
            "probability": probability_metrics(
                [r["outcome"]["direction"] for r in calibrated],
                [r["models"][name]["calibration"]["probabilities"] for r in calibrated],
            ),
        }
    return seal(
        {
            "policy": "prospective-evidence-ablations.v1",
            "target": target,
            "horizon_days": horizon,
            "as_of_time": cutoff.isoformat(),
            "archive_count": len(archives),
            "eligible_rows": len(rows),
            "skipped": dict(skipped),
            "training_blocks": dict(failures),
            "scored_count": len(scored),
            "metrics": metrics,
            "issued": issued,
            "minimum_training_episodes": MIN_TRAIN_OUTCOMES,
            "status": "insufficient_data" if len(scored) < 30 else "requires_effect_review",
            "effect_validated": False,
            "automatic_promotion": False,
        }
    )

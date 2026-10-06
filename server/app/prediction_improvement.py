"""Second-round ablation and delayed-feedback ensemble. No live promotion path."""

from __future__ import annotations

import copy
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import numpy as np

from .prediction_benchmark import (
    CLASSES,
    MODEL_IDS,
    class_prior,
    classification_metrics,
    nonoverlapping_matured,
    probability_direction,
)
from .prediction_features import (
    CLASSIFIER_CANDIDATES,
    CLASSIFIER_MAX_STEPS,
    CLASSIFIER_TOLERANCE,
    DRIVERS,
    FEATURE_CANDIDATES,
    FEATURE_VERSION,
    MIN_CLASS_TRAIN,
    MIN_TRAIN_OUTCOMES,
    RIDGE_PENALTY,
    TARGET_CONTROL,
    candidates_at,
)
from .prediction_replay import digest, load_export, timestamp

IMPROVEMENT_VERSION = "prequential-feature-ablation.v1"
PRIOR = "past-class-prior.v1"
ENSEMBLE = "delayed-regime-ensemble.v1"
SELECTIVE = "delayed-regime-selective065.v1"
MIN_ENSEMBLE_EPISODES = 5
CONSENSUS_THRESHOLD = 0.65
FAMILIES = {
    "persistence.v1": "baseline",
    PRIOR: "baseline",
    "robust-drift-reference.v1": "trend",
    "calendar-median-drift.v1": "trend",
    "damped-theil-sen.w20.d050.v1": "trend",
    FEATURE_CANDIDATES[0]: "trend",
    FEATURE_CANDIDATES[1]: "target_learned",
    FEATURE_CANDIDATES[2]: "upstream",
    FEATURE_CANDIDATES[3]: "upstream",
}
EXPERTS = (*MODEL_IDS, *FEATURE_CANDIDATES, PRIOR)
ALL_MODELS = (*EXPERTS, ENSEMBLE, SELECTIVE, TARGET_CONTROL, *CLASSIFIER_CANDIDATES)


def ensemble_prediction(past: list[dict], current: dict) -> tuple[dict, dict]:
    past = nonoverlapping_matured(past, before=timestamp(current["issue_at"]))
    recent = past[-20:]
    same_regime = [r for r in recent if r["regime"] == current["regime"]]
    pool = same_regime if len(same_regime) >= MIN_ENSEMBLE_EPISODES else recent
    weights: dict[str, float] = {}
    counts = {}
    for model in EXPERTS:
        if current["models"][model]["raw_direction"] is None:
            continue
        samples = [r for r in pool if r["models"][model]["raw_direction"] is not None]
        counts[model] = len(samples)
        if len(samples) < MIN_ENSEMBLE_EPISODES:
            score = 0.5
        else:
            correct = sum(r["models"][model]["raw_direction"] == r["outcome"]["direction"] for r in samples)
            score = (correct + 2) / (len(samples) + 4)
        weights[model] = float(np.exp(4 * (score - 0.5)))
    families = defaultdict(list)
    for model in weights:
        families[FAMILIES[model]].append(model)
    # Correlated trend variants do not get four times the family voting power.
    for models in families.values():
        total = sum(weights[m] for m in models)
        for model in models:
            weights[model] /= total * len(families)
    votes = {k: 0.0 for k in CLASSES}
    for model, weight in weights.items():
        votes[current["models"][model]["raw_direction"]] += weight
    chosen = probability_direction([votes[k] for k in CLASSES])
    consensus = votes[chosen]
    result = {
        "raw_direction": chosen,
        "point_forecast": None,
        "reason": None,
        "consensus": consensus,
        "consensus_is_calibrated_probability": False,
        "past_episode_count": len(pool),
        "same_regime": pool is same_regime,
        "weights": weights,
        "candidate_fit_counts": counts,
        "learning_sha256": digest(
            [
                [
                    r["issue_at"],
                    r["outcome"]["revision_id"],
                    r["outcome"]["settled_at"],
                    r["outcome"]["direction"],
                    [r["models"][m]["raw_direction"] for m in EXPERTS],
                ]
                for r in pool
            ]
        ),
    }
    selective = dict(result)
    if consensus < CONSENSUS_THRESHOLD or len(pool) < MIN_ENSEMBLE_EPISODES or len(weights) < 3 or len(families) < 2:
        selective["raw_direction"] = None
        selective["reason"] = "consensus_or_history_insufficient"
    return result, selective


def summarize_calls(rows: list[dict], model: str, *, cutoff: datetime) -> dict:
    scored = [r for r in rows if r.get("outcome", {}).get("state") == "scored"]
    called = [r for r in scored if r["models"][model]["raw_direction"] is not None]
    truth = [r["outcome"]["direction"] for r in called]
    metrics = classification_metrics(truth, [r["models"][model]["raw_direction"] for r in called])
    metrics["coverage"] = len(called) / len(scored) if scored else None
    metrics["unique_actuals"] = len({r["outcome"]["revision_id"] for r in called})
    paired = {}
    for name in ("robust-drift-reference.v1", "persistence.v1", PRIOR, TARGET_CONTROL, "always_up", "always_down"):
        pairs = [
            (r, y)
            for r, y in zip(called, truth, strict=True)
            if name in {"always_up", "always_down"} or r["models"][name]["raw_direction"] is not None
        ]
        paired_truth = [y for _, y in pairs]
        preds = [
            "up" if name == "always_up" else "down" if name == "always_down" else r["models"][name]["raw_direction"]
            for r, _ in pairs
        ]
        baseline = classification_metrics(paired_truth, preds)
        candidate = classification_metrics(paired_truth, [r["models"][model]["raw_direction"] for r, _ in pairs])
        delta = None if not pairs else candidate["accuracy"] - baseline["accuracy"]
        paired[name] = {
            "count": len(pairs),
            "accuracy": baseline["accuracy"],
            "accuracy_delta": delta,
            "wins": sum(
                r["models"][model]["raw_direction"] == y and b != y for (r, y), b in zip(pairs, preds, strict=True)
            ),
            "losses": sum(
                r["models"][model]["raw_direction"] != y and b == y for (r, y), b in zip(pairs, preds, strict=True)
            ),
        }
    disjoint = nonoverlapping_matured(called, before=cutoff + timedelta(microseconds=1))
    mature_without_calls = sum(r["models"][model]["raw_direction"] is None for r in scored)
    return {
        "model_id": model,
        "scored_opportunities": len(scored),
        "metrics": metrics,
        "paired_baselines": paired,
        "nonoverlapping": classification_metrics(
            [r["outcome"]["direction"] for r in disjoint], [r["models"][model]["raw_direction"] for r in disjoint]
        ),
        "scheduled_coverage": sum(r.get("models", {}).get(model, {}).get("raw_direction") is not None for r in rows)
        / len(rows)
        if rows
        else None,
        "abstentions_on_scored": mature_without_calls,
        "abstention_reasons": dict(
            Counter(r["models"][model]["reason"] for r in scored if r["models"][model]["raw_direction"] is None)
        ),
        "effect_validated": False,
        "production_promotion_allowed": False,
    }


def run_improvement(export: dict, baseline: dict) -> dict:
    series = load_export(export)
    if baseline.get("result_sha256") != digest({k: v for k, v in baseline.items() if k != "result_sha256"}):
        raise ValueError("baseline_report_integrity_failed")
    if baseline["input_sha256"] != export["content_sha256"]:
        raise ValueError("different_input_exports")
    cutoff = timestamp(export["as_of_time"])
    config = {
        "version": IMPROVEMENT_VERSION,
        "feature_version": FEATURE_VERSION,
        "models": ALL_MODELS,
        "drivers": DRIVERS,
        "ridge_penalty": RIDGE_PENALTY,
        "minimum_training_outcomes": MIN_TRAIN_OUTCOMES,
        "minimum_ensemble_episodes": MIN_ENSEMBLE_EPISODES,
        "recent_ensemble_episodes": 20,
        "weight_shrinkage_successes": 2,
        "weight_shrinkage_total": 4,
        "weight_temperature": 4,
        "family_balance": FAMILIES,
        "consensus_threshold": CONSENSUS_THRESHOLD,
        "minimum_selective_experts": 3,
        "minimum_selective_families": 2,
        "probability_calibrated": False,
        "automatic_promotion": False,
        "evaluation": "already_inspected_dates_exploratory_prequential",
        "diagnostic_control_added_after_initial_run": TARGET_CONTROL,
        "directional_loss_added_after_initial_run": CLASSIFIER_CANDIDATES,
        "classifier_max_steps": CLASSIFIER_MAX_STEPS,
        "classifier_gradient_tolerance": CLASSIFIER_TOLERANCE,
        "classifier_minimum_per_class": MIN_CLASS_TRAIN,
    }
    result = {
        "configuration": config,
        "configuration_sha256": digest(config),
        "baseline_result_sha256": baseline["result_sha256"],
        "input_sha256": export["content_sha256"],
        "as_of_time": export["as_of_time"],
        "cells": [],
        "records": [],
    }
    groups = defaultdict(list)
    for row in baseline["records"]:
        groups[(row["target"], row["contract_id"])].append(row)
    for (target, contract_id), group in groups.items():
        processed = []
        for original in sorted(group, key=lambda r: timestamp(r["issue_at"])):
            row = copy.deepcopy(original)
            if row["state"] == "issued":
                candidates, regime = candidates_at(series, row)
                row["models"].update(candidates)
                row["regime"] = regime
                past = nonoverlapping_matured(processed, before=timestamp(row["issue_at"]))
                row["models"][PRIOR] = {
                    "raw_direction": probability_direction(class_prior(past)),
                    "reason": None,
                    "point_forecast": None,
                }
                row["models"][ENSEMBLE], row["models"][SELECTIVE] = ensemble_prediction(past, row)
            processed.append(row)
        # Recent-date slices are stability diagnostics, NOT unobserved holdouts.
        slices = {
            "through_2026_09_12": [r for r in processed if r["issue_at"][:10] <= "2026-09-12"],
            "since_2026_09_13": [r for r in processed if r["issue_at"][:10] >= "2026-09-13"],
        }
        result["cells"].append(
            {
                "target": target,
                "contract_id": contract_id,
                "models": [summarize_calls(processed, m, cutoff=cutoff) for m in ALL_MODELS],
                "date_diagnostics": {
                    k: [summarize_calls(v, m, cutoff=cutoff) for m in ALL_MODELS] for k, v in slices.items()
                },
            }
        )
        result["records"].extend(processed)
    result["summary"] = {m: summarize_calls(result["records"], m, cutoff=cutoff) for m in ALL_MODELS}
    # Global repeated targets/horizons cannot be combined into an independence claim.
    for summary in result["summary"].values():
        summary.pop("nonoverlapping")
    result["result_sha256"] = digest(result)
    return result

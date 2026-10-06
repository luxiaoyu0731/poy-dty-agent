"""As-of price features and fit-time-only supervised candidates. Offline research."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import numpy as np

from .prediction_benchmark import CLASSES, probability_direction
from .prediction_replay import CONTRACTS, SHANGHAI, VintageSeries, digest, direction, timestamp
from .seven_product_forecast import FRESHNESS_DAYS, MAX_ABS_DAILY_LOG_RETURN, NEUTRAL_FLOOR_PCT, PricePoint
from .seven_product_multivariate_experiment import fit_predict_ridge_return

FEATURE_VERSION = "asof-price-features.v2"
LAGS = (1, 5, 20)
RIDGE_PENALTY = 10.0
MIN_TRAIN_OUTCOMES = 40
DRIVERS = {
    "crude": (),
    "naphtha": ("crude",),
    "px": ("crude", "naphtha"),
    "pta": ("px",),
    "meg": ("crude",),
    "poy": ("pta", "meg"),
    "dty": ("poy",),
}
FEATURE_CANDIDATES = (
    "momentum-calendar20.v1",
    "direct-target-ridge.v1",
    "upstream-momentum7.v1",
    "direct-upstream-ridge.v1",
)
TARGET_CONTROL = "target-momentum7-control.v1"
CLASSIFIER_CANDIDATES = ("direct-target-logistic.v1", "direct-upstream-logistic.v1")
CLASSIFIER_MAX_STEPS = 2000
CLASSIFIER_TOLERANCE = 1e-5
MIN_CLASS_TRAIN = 5


def at_or_before(points: tuple[PricePoint, ...], day: date) -> PricePoint | None:
    return next((p for p in reversed(points) if date.fromisoformat(p.observed_at[:10]) <= day), None)


def calendar_return(points: tuple[PricePoint, ...], anchor: date, lag: int) -> tuple[float, list[str]] | None:
    current = at_or_before(points, anchor)
    old = at_or_before(points, anchor - timedelta(days=lag))
    if current is None or old is None:
        return None
    if (anchor - date.fromisoformat(current.observed_at[:10])).days > 4:
        return None
    if (anchor - timedelta(days=lag) - date.fromisoformat(old.observed_at[:10])).days > 4:
        return None
    return math.log(current.value / old.value), [old.observation_id, current.observation_id]


def feature_vector(
    points: tuple[PricePoint, ...], drivers: dict[str, tuple[PricePoint, ...]], anchor: date
) -> tuple[np.ndarray, list[str]] | None:
    """Economic-date alignment inside data already available at the fitting cutoff."""
    values: list[float] = []
    lineage: list[str] = []
    for series in (points, *drivers.values()):
        for lag in LAGS:
            value = calendar_return(series, anchor, lag)
            if value is None:
                return None
            values.append(value[0])
            lineage.extend(value[1])
    prefix = [p for p in points if date.fromisoformat(p.observed_at[:10]) <= anchor][-20:]
    if len(prefix) < 10:
        return None
    levels = np.log([p.value for p in prefix])
    returns = np.diff(levels)
    values += [float(np.std(returns)), float(levels[-1] - np.mean(levels))]
    lineage.extend(p.observation_id for p in prefix)
    return np.asarray(values, dtype=np.float64), sorted(set(lineage))


def supervised_pairs(
    points: tuple[PricePoint, ...],
    drivers: dict[str, tuple[PricePoint, ...]],
    *,
    horizon_days: int,
    fitting_cutoff: datetime,
    publication_steps: int | None = None,
    issue_offset_days: int = 0,
) -> list[dict]:
    """Historical pairs available NOW for training, never historical-issued OOS evidence.

    Backfilled/revised values are legitimate training data after arrival. Their
    old observation dates do not move their first availability or create past
    forecast/calibration records. Predictor normalization is fit on these rows only.
    """
    fitting_day = fitting_cutoff.astimezone(SHANGHAI).date()

    def visible(series):
        return tuple(
            p
            for p in series
            if timestamp(p.visible_at) <= fitting_cutoff and date.fromisoformat(p.observed_at[:10]) <= fitting_day
        )

    points = visible(points)
    drivers = {k: visible(v) for k, v in drivers.items()}
    rows = []
    used_actuals: set[str] = set()
    for i, origin in enumerate(points):
        day = date.fromisoformat(origin.observed_at[:10])
        if publication_steps is not None:
            # Quote-count targets must not be approximated with calendar days.
            future = [
                p
                for p in points[i + 1 :]
                if date.fromisoformat(p.observed_at[:10]) > day + timedelta(days=issue_offset_days)
            ]
            actual = future[publication_steps - 1] if len(future) >= publication_steps else None
            maximum_days = issue_offset_days + 4 * publication_steps + 7
        else:
            actual = next(
                (
                    p
                    for p in points[i + 1 :]
                    if date.fromisoformat(p.observed_at[:10]) >= day + timedelta(days=horizon_days)
                ),
                None,
            )
            maximum_days = horizon_days + 4
        if actual is None or actual.observation_id in used_actuals:
            continue
        if (date.fromisoformat(actual.observed_at[:10]) - day).days > maximum_days:
            continue
        vector = feature_vector(points, drivers, day)
        if vector is None:
            continue
        used_actuals.add(actual.observation_id)
        rows.append(
            {
                "features": vector[0],
                "feature_ids": vector[1],
                "origin_id": origin.observation_id,
                "actual_id": actual.observation_id,
                "actual_visible_at": actual.visible_at,
                "actual_log_return": math.log(actual.value / origin.value),
            }
        )
    return rows[-250:]


def unavailable(reason: str, **details) -> dict:
    return {"raw_direction": None, "point_forecast": None, "reason": reason, **details}


def from_log_return(target: str, base: float, log_return: float, horizon: int, **details) -> dict:
    bounded = float(np.clip(log_return, -MAX_ABS_DAILY_LOG_RETURN * horizon, MAX_ABS_DAILY_LOG_RETURN * horizon))
    value = base * math.exp(bounded)
    return {
        "raw_direction": direction(value / base - 1, NEUTRAL_FLOOR_PCT[target]),
        "point_forecast": value,
        "reason": None,
        **details,
    }


def fit_direction_classifier(pairs: list[dict], vector: np.ndarray, target: str) -> dict:
    """Fixed L2 multinomial objective; returned scores are not calibrated confidence.

    Loss = mean cross entropy + penalty / (2*n) * squared non-intercept weights.
    The multinomial Hessian has class-covariance norm <= 1/2; this gives a
    conservative spectral step bound. Scaling is estimated only from training.
    """
    labels = np.asarray(
        [CLASSES.index(direction(math.expm1(p["actual_log_return"]), NEUTRAL_FLOOR_PCT[target])) for p in pairs],
        dtype=np.int64,
    )
    counts = np.bincount(labels, minlength=len(CLASSES))
    details = {"training_class_counts": dict(zip(CLASSES, counts.tolist(), strict=True)), "scores_calibrated": False}
    if len(pairs) < MIN_TRAIN_OUTCOMES or np.min(counts) < MIN_CLASS_TRAIN:
        return unavailable("classifier_class_history_insufficient", **details)
    x = np.asarray([p["features"] for p in pairs], dtype=np.float64)
    if x.ndim != 2 or vector.shape != (x.shape[1],) or not np.isfinite(x).all() or not np.isfinite(vector).all():
        raise ValueError("invalid_classifier_features")
    center, scale = np.mean(x, axis=0), np.std(x, axis=0)
    scale = np.where(scale > 1e-12, scale, 1.0)
    design = np.column_stack((np.ones(len(x)), (x - center) / scale))
    current = np.concatenate(([1.0], (vector - center) / scale))
    weights = np.zeros((design.shape[1], len(CLASSES)))
    weights[0] = np.log((counts + 1) / (len(pairs) + len(CLASSES)))
    targets = np.eye(len(CLASSES))[labels]
    regularization = RIDGE_PENALTY / len(pairs)
    step = 1.0 / (0.5 * np.linalg.eigvalsh(design.T @ design / len(pairs))[-1] + regularization)

    def loss_gradient():
        logits = design @ weights
        logits -= np.max(logits, axis=1, keepdims=True)
        log_probs = logits - np.log(np.exp(logits).sum(axis=1, keepdims=True))
        penalty = weights.copy()
        penalty[0] = 0
        gradient = design.T @ (np.exp(log_probs) - targets) / len(pairs) + regularization * penalty
        loss = -np.mean(log_probs[np.arange(len(pairs)), labels]) + regularization * np.sum(penalty**2) / 2
        return float(loss), gradient

    initial_loss, _ = loss_gradient()
    for _iteration in range(CLASSIFIER_MAX_STEPS):
        _, gradient = loss_gradient()
        if np.max(np.abs(gradient)) <= CLASSIFIER_TOLERANCE:
            break
        weights -= step * gradient
    final_loss, gradient = loss_gradient()
    details.update(
        iterations=_iteration + 1,
        initial_loss=initial_loss,
        final_loss=final_loss,
        gradient_max=float(np.max(np.abs(gradient))),
    )
    if details["gradient_max"] > CLASSIFIER_TOLERANCE or not np.isfinite(final_loss):
        return unavailable("classifier_not_converged", **details)
    logits = current @ weights
    logits -= logits.max()
    scores = np.exp(logits) / np.exp(logits).sum()
    return {
        "raw_direction": probability_direction(scores.tolist()),
        "point_forecast": None,
        "reason": None,
        "class_scores": dict(zip(CLASSES, scores.tolist(), strict=True)),
        **details,
    }


def candidates_at(series: dict[str, VintageSeries], row: dict) -> tuple[dict, str]:
    target = row["target"]
    issue = timestamp(row["issue_at"])
    issue_day = issue.astimezone(SHANGHAI).date()
    view = series[target].view(issue)
    if view.input_sha256 != row["input_sha256"]:
        raise ValueError("replay_input_changed")
    points = view.points
    if not points or view.latest_blocked:
        raise ValueError("benchmark_issued_without_valid_input")
    base = points[-1]
    if base.observation_id != row["base"]["observation_id"] or base.value != row["base"]["value"]:
        raise ValueError("benchmark_base_mismatch")
    anchor = date.fromisoformat(base.observed_at[:10])
    horizon = row["projection_days"]
    contract = next(c for c in CONTRACTS if c.contract_id == row["contract_id"])
    publication_steps = contract.steps if contract.kind == "publication" else None
    issue_offset = (issue_day - anchor).days
    models = {}
    own_move = calendar_return(points, issue_day, 7)
    models[TARGET_CONTROL] = (
        from_log_return(target, base.value, own_move[0] * horizon / 7, horizon, feature_ids=own_move[1])
        if own_move
        else unavailable("target_lag_missing")
    )
    momentum = calendar_return(points, anchor, 20)
    regime = direction(math.expm1(momentum[0]), NEUTRAL_FLOOR_PCT[target]) if momentum else "unknown"
    models[FEATURE_CANDIDATES[0]] = (
        from_log_return(target, base.value, momentum[0] * horizon / 20, horizon, feature_ids=momentum[1])
        if momentum
        else unavailable("momentum_history_missing")
    )

    driver_points = {}
    driver_inputs = {}
    unavailable_drivers = []
    for name in DRIVERS[target]:
        if name not in series:
            unavailable_drivers.append(name)
            continue
        driver_view = series[name].view(issue)
        if not driver_view.points or driver_view.latest_blocked:
            unavailable_drivers.append(name)
            continue
        latest = driver_view.points[-1]
        age = (issue_day - date.fromisoformat(latest.observed_at[:10])).days
        if age > FRESHNESS_DAYS[name]:
            unavailable_drivers.append(name)
            continue
        driver_points[name] = driver_view.points
        driver_inputs[name] = driver_view.input_sha256
    if not DRIVERS[target] or unavailable_drivers:
        models[FEATURE_CANDIDATES[2]] = unavailable("upstream_unavailable", missing=unavailable_drivers)
    else:
        moves = [calendar_return(p, issue_day, 7) for p in driver_points.values()]
        if any(m is None for m in moves):
            models[FEATURE_CANDIDATES[2]] = unavailable("upstream_lag_missing")
        else:
            # Equal normalized returns are predictive signals, not a mixed-unit material-cost basket.
            move = float(np.mean([m[0] for m in moves]))
            models[FEATURE_CANDIDATES[2]] = from_log_return(
                target, base.value, move * horizon / 7, horizon, driver_input_hashes=driver_inputs
            )
    for candidate, classifier, drivers in (
        (FEATURE_CANDIDATES[1], CLASSIFIER_CANDIDATES[0], {}),
        (FEATURE_CANDIDATES[3], CLASSIFIER_CANDIDATES[1], driver_points),
    ):
        if candidate == FEATURE_CANDIDATES[3] and (not DRIVERS[target] or unavailable_drivers):
            models[candidate] = unavailable("upstream_unavailable", missing=unavailable_drivers)
            models[classifier] = unavailable("upstream_unavailable", missing=unavailable_drivers)
            continue
        vector = feature_vector(points, drivers, anchor)
        if vector is None:
            models[candidate] = unavailable("aligned_feature_history_missing")
            models[classifier] = unavailable("aligned_feature_history_missing")
            continue
        pairs = supervised_pairs(
            points,
            drivers,
            horizon_days=horizon,
            fitting_cutoff=issue,
            publication_steps=publication_steps,
            issue_offset_days=issue_offset,
        )
        details = {
            "training_count": len(pairs),
            "training_mode": "fit_time_available_retrospective_pairs",
            "calibration_evidence": False,
            "training_target_contract": contract.contract_id,
            "training_issue_offset_days": issue_offset,
            "driver_input_hashes": driver_inputs if drivers else {},
            "feature_ids": vector[1],
            "training_sha256": digest(
                [
                    [
                        r["origin_id"],
                        r["actual_id"],
                        r["actual_visible_at"],
                        r["feature_ids"],
                        r["features"].tolist(),
                        r["actual_log_return"],
                    ]
                    for r in pairs
                ]
            ),
        }
        if len(pairs) < MIN_TRAIN_OUTCOMES:
            models[candidate] = unavailable("training_history_below_40", **details)
            models[classifier] = unavailable("training_history_below_40", **details)
            continue
        prediction = fit_predict_ridge_return(training_records=pairs, feature_vector=vector[0], penalty=RIDGE_PENALTY)
        models[candidate] = from_log_return(target, base.value, prediction, horizon, **details)
        models[classifier] = {**fit_direction_classifier(pairs, vector[0], target), **details}
    return models, regime

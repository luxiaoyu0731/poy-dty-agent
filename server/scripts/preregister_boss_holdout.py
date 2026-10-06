from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

FORBIDDEN_PREDICTION_KEYS = {
    "actual",
    "actual_direction",
    "answer",
    "future",
    "future_price",
    "future_date",
    "outcome",
    "posterior",
    "score",
    "verdict",
}
ALLOWED_DIRECTIONS = {"偏强", "震荡", "偏弱"}


def canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_manifest(registry: Path) -> dict[str, Any]:
    manifest = _read_json(registry / "manifest.json")
    if canonical_hash(manifest["strategy"]) != manifest.get("strategy_hash"):
        raise ValueError("immutable strategy hash verification failed")
    return manifest


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    previous = "GENESIS"
    for index, row in enumerate(rows, start=1):
        entry_hash = row.get("entry_hash")
        unsigned = {key: value for key, value in row.items() if key != "entry_hash"}
        if (
            row.get("sequence") != index
            or row.get("previous_hash") != previous
            or canonical_hash(unsigned) != entry_hash
        ):
            raise ValueError(f"append-only chain verification failed at sequence {index}")
        previous = str(entry_hash)
    return rows


def _contains_forbidden(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            str(key).casefold() in FORBIDDEN_PREDICTION_KEYS or _contains_forbidden(item) for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden(item) for item in value)
    return False


def freeze(source: Path, registry: Path, *, start_date: date, min_scored: int, max_days: int) -> dict[str, Any]:
    artifact = _read_json(source)
    candidate = artifact["best_accepted_candidate"]
    frozen_strategy = {
        "name": candidate["name"],
        "family": candidate["family"],
        "features": artifact["guardrails"]["prediction_features"],
        "weights": candidate["params"]["weights"],
        "prediction_threshold": candidate["params"]["prediction_threshold"],
        "actual_threshold": candidate["params"]["actual_threshold"],
        "gate_policy": candidate["params"]["gate_policy"],
        "rag_visible_at_required": artifact["guardrails"]["rag_visible_at_required"],
    }
    manifest = {
        "schema_version": "boss_holdout_preregistration.v1",
        "registry_id": "boss-7541-unseen-holdout-v1",
        "registered_at": datetime.now(UTC).isoformat(),
        "source_artifact": str(source),
        "source_artifact_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "strategy": frozen_strategy,
        "strategy_hash": canonical_hash(frozen_strategy),
        "holdout": {
            "start_date": start_date.isoformat(),
            "end_date": (start_date + timedelta(days=max_days - 1)).isoformat(),
            "minimum_scored_samples": min_scored,
            "maximum_calendar_days": max_days,
            "historical_initialization_allowed": False,
            "prediction_must_precede_outcome": True,
            "append_only": True,
        },
        "evaluation": {
            "primary_metric": "accuracy_scored_non_neutral",
            "target": 0.75,
            "coverage_floor": 0.4453,
            "required_metrics": [
                "total",
                "scored",
                "coverage",
                "hit",
                "miss",
                "accuracy",
                "neutral_baseline",
                "action_samples",
                "action_coverage",
                "action_hit_rate",
                "non_neutral_recall",
                "precision",
                "recall",
                "confusion_matrix",
                "future_price_leaks",
                "future_event_leaks",
            ],
        },
        "stopping_rule": {
            "early_stopping": False,
            "first_and_only_primary_evaluation": "when minimum_scored_samples is reached or end_date passes",
            "insufficient_sample_result": "inconclusive",
            "parameter_changes": "require a new registry_id and new future holdout window",
        },
    }
    manifest_path = registry / "manifest.json"
    registry.mkdir(parents=True, exist_ok=True)
    if manifest_path.exists():
        existing = _read_json(manifest_path)
        comparable = {key: value for key, value in manifest.items() if key != "registered_at"}
        existing_comparable = {key: value for key, value in existing.items() if key != "registered_at"}
        if existing_comparable != comparable:
            raise ValueError("registry already exists with different immutable preregistration")
        return existing
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path.chmod(0o444)
    return manifest


def _append_chained(path: Path, record: dict[str, Any]) -> dict[str, Any]:
    rows = _jsonl(path)
    record = {**record, "sequence": len(rows) + 1, "previous_hash": rows[-1]["entry_hash"] if rows else "GENESIS"}
    record["entry_hash"] = canonical_hash(record)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def append_prediction(registry: Path, payload: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_manifest(registry)
    if _contains_forbidden(payload):
        raise ValueError("prediction payload contains posterior/future answer fields")
    decision_date = date.fromisoformat(str(payload["decision_date"]))
    as_of_time = datetime.fromisoformat(str(payload["as_of_time"]).replace("Z", "+00:00"))
    if decision_date < date.fromisoformat(manifest["holdout"]["start_date"]):
        raise ValueError("historical initialization before registered holdout is forbidden")
    if as_of_time.date() > decision_date:
        raise ValueError("prediction feature snapshot is not visible by decision date")
    if payload.get("direction") not in ALLOWED_DIRECTIONS:
        raise ValueError("invalid direction")
    prediction_id = str(payload.get("prediction_id") or decision_date.isoformat())
    if any(row["prediction_id"] == prediction_id for row in _jsonl(registry / "predictions.jsonl")):
        raise ValueError("prediction is append-only and already exists")
    return _append_chained(
        registry / "predictions.jsonl",
        {
            **payload,
            "prediction_id": prediction_id,
            "strategy_hash": manifest["strategy_hash"],
            "recorded_at": datetime.now(UTC).isoformat(),
        },
    )


def append_score(registry: Path, payload: dict[str, Any]) -> dict[str, Any]:
    predictions = {row["prediction_id"]: row for row in _jsonl(registry / "predictions.jsonl")}
    prediction_id = str(payload["prediction_id"])
    if prediction_id not in predictions:
        raise ValueError("score requires an existing pre-outcome prediction")
    if any(row["prediction_id"] == prediction_id for row in _jsonl(registry / "scores.jsonl")):
        raise ValueError("score is append-only and already exists")
    observed_at = datetime.fromisoformat(str(payload["observed_at"]).replace("Z", "+00:00"))
    decision_date = date.fromisoformat(predictions[prediction_id]["decision_date"])
    if observed_at.date() <= decision_date:
        raise ValueError("outcome must become visible after prediction decision date")
    if payload.get("actual_direction") not in ALLOWED_DIRECTIONS:
        raise ValueError("invalid actual direction")
    return _append_chained(
        registry / "scores.jsonl",
        {
            **payload,
            "prediction_direction": predictions[prediction_id]["direction"],
            "hit": predictions[prediction_id]["direction"] == payload["actual_direction"],
            "recorded_at": datetime.now(UTC).isoformat(),
        },
    )


def status(registry: Path, *, today: date | None = None) -> dict[str, Any]:
    manifest = _load_manifest(registry)
    predictions = _jsonl(registry / "predictions.jsonl")
    scores = _jsonl(registry / "scores.jsonl")
    minimum = int(manifest["holdout"]["minimum_scored_samples"])
    end_date = date.fromisoformat(manifest["holdout"]["end_date"])
    current = today or datetime.now(UTC).date()
    if len(scores) >= minimum:
        state = "ready_for_single_evaluation"
    elif current > end_date:
        state = "inconclusive_insufficient_sample"
    else:
        state = "pending"
    return {
        "registry_id": manifest["registry_id"],
        "strategy_hash": manifest["strategy_hash"],
        "status": state,
        "predictions": len(predictions),
        "scored": len(scores),
        "minimum_scored_samples": minimum,
        "remaining_to_evaluate": max(0, minimum - len(scores)),
        "holdout_start_date": manifest["holdout"]["start_date"],
        "holdout_end_date": manifest["holdout"]["end_date"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Immutable preregistered holdout ledger for the frozen 75.41% candidate."
    )
    parser.add_argument("--registry", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    freeze_parser = sub.add_parser("freeze")
    freeze_parser.add_argument("--source", type=Path, required=True)
    freeze_parser.add_argument("--start-date", type=date.fromisoformat, required=True)
    freeze_parser.add_argument("--min-scored", type=int, default=60)
    freeze_parser.add_argument("--max-days", type=int, default=180)
    for command in ("predict", "score"):
        item = sub.add_parser(command)
        item.add_argument("--payload", type=Path, required=True)
    sub.add_parser("status")
    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze(
            args.source, args.registry, start_date=args.start_date, min_scored=args.min_scored, max_days=args.max_days
        )
    elif args.command == "predict":
        result = append_prediction(args.registry, _read_json(args.payload))
    elif args.command == "score":
        result = append_score(args.registry, _read_json(args.payload))
    else:
        result = status(args.registry)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

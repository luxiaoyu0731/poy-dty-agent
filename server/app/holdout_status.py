from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

DEFAULT_REGISTRY = (
    Path(__file__).resolve().parents[2] / ".codex-run" / "preregistered-holdouts" / "boss-7541-unseen-holdout-v1"
)
SAFE_METRICS = {
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
}


def _line_count(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def customer_holdout_status(*, today: date | None = None) -> dict[str, Any]:
    registry = Path(os.getenv("BOSS_HOLDOUT_REGISTRY", str(DEFAULT_REGISTRY)))
    manifest_path = registry / "manifest.json"
    if not manifest_path.exists():
        return {
            "status": "not_registered",
            "window_start": None,
            "window_end": None,
            "minimum_scored_samples": 0,
            "prediction_count": 0,
            "scored_count": 0,
            "remaining_to_evaluate": 0,
            "primary_metric": "",
            "target": None,
            "coverage_floor": None,
            "required_metrics": [],
            "boundaries": ["预注册尚未完成，不展示历史实验结果作为未来样本表现。"],
            "updated_at": datetime.now(UTC).isoformat(),
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        holdout = manifest["holdout"]
        evaluation = manifest["evaluation"]
        predictions = _line_count(registry / "predictions.jsonl")
        scored = _line_count(registry / "scores.jsonl")
        minimum = int(holdout["minimum_scored_samples"])
        end_date = date.fromisoformat(str(holdout["end_date"]))
        current = today or datetime.now(UTC).date()
        if scored >= minimum:
            status = "ready_for_single_evaluation"
        elif current > end_date:
            status = "inconclusive_insufficient_sample"
        else:
            status = "pending"
        required = [str(item) for item in evaluation.get("required_metrics", []) if str(item) in SAFE_METRICS]
        return {
            "status": status,
            "window_start": str(holdout["start_date"]),
            "window_end": str(holdout["end_date"]),
            "minimum_scored_samples": minimum,
            "prediction_count": predictions,
            "scored_count": scored,
            "remaining_to_evaluate": max(0, minimum - scored),
            "primary_metric": str(evaluation["primary_metric"]),
            "target": float(evaluation["target"]),
            "coverage_floor": float(evaluation["coverage_floor"]),
            "required_metrics": required,
            "boundaries": [
                "仅统计预注册窗口开始后、先预测后到期的未见样本。",
                "达到最少评分样本或窗口结束前，不形成生产准确率结论。",
                "不提前停止；参数变化必须使用新的未来窗口重新注册。",
                "历史回测与当前holdout分开披露，不能合并抬高样本或命中率。",
            ],
            "updated_at": datetime.now(UTC).isoformat(),
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {
            "status": "unavailable",
            "window_start": None,
            "window_end": None,
            "minimum_scored_samples": 0,
            "prediction_count": 0,
            "scored_count": 0,
            "remaining_to_evaluate": 0,
            "primary_metric": "",
            "target": None,
            "coverage_floor": None,
            "required_metrics": [],
            "boundaries": ["预注册状态暂时不可读取；系统不会回退到历史回测结果。"],
            "updated_at": datetime.now(UTC).isoformat(),
        }

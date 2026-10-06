from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_llm_v3_experiment_pack import estimate_cost  # noqa: E402
from llm_event_direction_judge import call_deepseek  # noqa: E402
from run_llm_v3_small_experiment import normalize_v3_payload, validate_v3_payload  # noqa: E402

DEFAULT_PROMPT_PACK = Path(".codex-run/full-test-prep-20260629/04-v4pro-prompt-pack.jsonl")
DEFAULT_CANDIDATE_POOL = Path(".codex-run/full-test-prep-20260629/02-full-candidate-pool.json")
DEFAULT_OUTPUT_DIR = Path(".codex-run/full-test-v4pro-20260629")
DIRECTIONS = {"利多": 1, "利空": -1, "中性": 0}
WINDOWS = {"h1", "h3", "h7"}


def run_full_test(
    *,
    prompt_pack: Path = DEFAULT_PROMPT_PACK,
    candidate_pool: Path = DEFAULT_CANDIDATE_POOL,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    limit: int | None = None,
    offset: int = 0,
    resume: bool = True,
    concurrency: int = 1,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    prompts = read_jsonl(prompt_pack)
    candidates = {str(item["event_id"]): item for item in read_json(candidate_pool)["candidates"]}
    selected = prompts[max(offset, 0) :]
    if limit is not None:
        selected = selected[:limit]

    live_path = output_dir / "01-live-judgments.json"
    existing = load_existing_judgments(live_path) if resume else {}
    judgments: list[dict[str, Any]] = list(existing.values())
    selected_ids = {str(item["event_id"]) for item in selected}
    judgments = [item for item in judgments if str(item.get("event_id")) in selected_ids]
    completed_ids = {str(item.get("event_id")) for item in judgments}

    pending = [
        (index, item)
        for index, item in enumerate(selected, start=1 + max(offset, 0))
        if str(item["event_id"]) not in completed_ids
    ]
    if concurrency <= 1:
        for index, item in pending:
            judgment = call_and_parse(index, item, candidates)
            judgments.append(judgment)
            completed_ids.add(str(judgment.get("event_id")))
            persist_partial(judgments, candidates, output_dir=output_dir, live_path=live_path)
    else:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
            futures = {
                executor.submit(call_and_parse, index, item, candidates): (index, item) for index, item in pending
            }
            for future in as_completed(futures):
                judgment = future.result()
                judgments.append(judgment)
                completed_ids.add(str(judgment.get("event_id")))
                judgments.sort(key=lambda row: int(row.get("index") or 0))
                persist_partial(judgments, candidates, output_dir=output_dir, live_path=live_path)

    output = build_output(judgments, candidates, output_dir=output_dir)
    write_json(live_path, output["live"])
    write_json(output_dir / "02-layered-score.json", output["layered_score"])
    write_json(output_dir / "run-report.json", output)
    write_summary(output_dir / "03-summary.md", output)
    print(json.dumps(summarize_for_stdout(output, output_dir), ensure_ascii=False, indent=2))
    return output


def call_and_parse(index: int, item: dict[str, Any], candidates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    provider = call_deepseek(str(item["prompt"]))
    parsed = normalize_v3_payload(provider.content if not provider.fallback else "")
    validation_errors = validate_v3_payload(parsed)
    candidate = candidates.get(str(item["event_id"]), {})
    return {
        "index": index,
        "event_id": item.get("event_id"),
        "title": item.get("title"),
        "as_of_time": item.get("as_of_time"),
        "candidate_type": item.get("candidate_type"),
        "product": item.get("product"),
        "provider": provider.provider,
        "model": provider.model,
        "provider_call_attempted": provider.provider == "deepseek",
        "provider_call_succeeded": provider.provider == "deepseek" and not provider.fallback,
        "fallback": provider.fallback,
        "error": provider.error,
        "latency_ms": provider.latency_ms,
        "prompt_tokens_est": provider.prompt_tokens_est,
        "completion_tokens_est": provider.completion_tokens_est,
        "attempts": provider.attempts,
        "schema_v3": parsed,
        "validation_errors": validation_errors,
        "raw_model_content": provider.content,
        "candidate": compact_candidate_for_trace(candidate),
    }


def persist_partial(
    judgments: list[dict[str, Any]],
    candidates: dict[str, dict[str, Any]],
    *,
    output_dir: Path,
    live_path: Path,
) -> None:
    write_json(live_path, build_live_report(judgments))
    write_json(output_dir / "run-report.partial.json", build_output(judgments, candidates, output_dir=output_dir))


def build_output(
    judgments: list[dict[str, Any]], candidates: dict[str, dict[str, Any]], *, output_dir: Path
) -> dict[str, Any]:
    live = build_live_report(judgments)
    score = score_judgments(judgments, candidates)
    return {
        "schema_version": "full_test_v4pro_run.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "guardrails": {
            "db_write": False,
            "posterior_used_only_for_scoring": True,
            "normal_entry_requires_provider_success": True,
        },
        "live": live,
        "layered_score": score,
        "files": {
            "live": str(output_dir / "01-live-judgments.json"),
            "score": str(output_dir / "02-layered-score.json"),
            "summary": str(output_dir / "03-summary.md"),
        },
    }


def build_live_report(judgments: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_tokens = sum(int(item.get("prompt_tokens_est") or 0) for item in judgments)
    completion_tokens = sum(int(item.get("completion_tokens_est") or 0) for item in judgments)
    return {
        "schema_version": "full_test_v4pro_live_judgments.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "summary": {
            "event_count": len(judgments),
            "provider_attempts": sum(1 for item in judgments if item.get("provider_call_attempted")),
            "provider_success": sum(1 for item in judgments if item.get("provider_call_succeeded")),
            "fallbacks": sum(1 for item in judgments if item.get("fallback")),
            "validation_error_events": sum(1 for item in judgments if item.get("validation_errors")),
            "prompt_tokens_est": prompt_tokens,
            "completion_tokens_est": completion_tokens,
            **estimate_cost(prompt_tokens, completion_tokens),
        },
        "judgments": judgments,
    }


def score_judgments(judgments: list[dict[str, Any]], candidates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for judgment in judgments:
        event_id = str(judgment.get("event_id"))
        candidate = candidates.get(event_id, {})
        v3 = judgment.get("schema_v3") if isinstance(judgment.get("schema_v3"), dict) else {}
        actionability = str(v3.get("actionability") or "disabled")
        should_enter = bool(v3.get("should_enter_backtest"))
        window = str(v3.get("recommended_scoring_window") or "explain_only")
        direction = str(v3.get("poy_dty_direction") or "中性")
        score = score_candidate_window(
            candidate=candidate,
            predicted_direction=direction,
            window=window,
            invalid_or_fallback=bool(judgment.get("fallback") or judgment.get("validation_errors")),
        )
        action_score = score_action(
            actionability=actionability,
            should_enter=should_enter,
            window=window,
            layer_score=score,
            invalid_or_fallback=bool(judgment.get("fallback") or judgment.get("validation_errors")),
        )
        watch_score = score_watch(
            actionability=actionability,
            should_enter=should_enter,
            window=window,
            layer_score=score,
            invalid_or_fallback=bool(judgment.get("fallback") or judgment.get("validation_errors")),
        )
        rows.append(
            {
                "event_id": event_id,
                "title": judgment.get("title"),
                "as_of_time": judgment.get("as_of_time"),
                "candidate_type": judgment.get("candidate_type"),
                "product": judgment.get("product"),
                "actionability": actionability,
                "should_enter_backtest": should_enter,
                "recommended_scoring_window": window,
                "poy_dty_direction": direction,
                "confidence": v3.get("confidence"),
                "evidence_strength": v3.get("evidence_strength"),
                "offset_risk": v3.get("offset_risk"),
                "priced_in_risk": v3.get("priced_in_risk"),
                "window_score": score,
                "action_score": action_score,
                "watch_score": watch_score,
                "provider_call_succeeded": judgment.get("provider_call_succeeded"),
                "validation_errors": judgment.get("validation_errors"),
            }
        )
    action_counted = [row for row in rows if row["action_score"]["counts"]]
    watch_counted = [row for row in rows if row["watch_score"]["counts"]]
    return {
        "schema_version": "full_test_v4pro_layered_score.v1",
        "scoring_policy": {
            "action": "Only actionable + should_enter_backtest + matching h1/h3/h7 non-neutral posterior counts.",
            "watch": "watch_only is scored separately and never enters action hit_rate.",
            "explain": "explain_only and disabled do not count as accuracy.",
        },
        "summary": {
            "total_events": len(rows),
            "actionability_distribution": dict(Counter(row["actionability"] for row in rows)),
            "window_distribution": dict(Counter(row["recommended_scoring_window"] for row in rows)),
            "action_scored": len(action_counted),
            "action_hit": sum(1 for row in action_counted if row["action_score"]["verdict"] == "hit"),
            "action_miss": sum(1 for row in action_counted if row["action_score"]["verdict"] == "miss"),
            "action_hit_rate": hit_rate(action_counted, "action_score"),
            "watch_scored": len(watch_counted),
            "watch_hit": sum(1 for row in watch_counted if row["watch_score"]["verdict"] == "hit"),
            "watch_miss": sum(1 for row in watch_counted if row["watch_score"]["verdict"] == "miss"),
            "watch_hit_rate": hit_rate(watch_counted, "watch_score"),
            "action_by_window": summarize_by_window(action_counted, "action_score"),
            "watch_by_window": summarize_by_window(watch_counted, "watch_score"),
            "invalid_or_fallback": sum(
                1 for row in rows if row["action_score"]["verdict"] == "invalid_or_fallback_not_scored"
            ),
            "sample_too_small": len(action_counted) < 30,
        },
        "events": rows,
    }


def score_candidate_window(
    *,
    candidate: dict[str, Any],
    predicted_direction: str,
    window: str,
    invalid_or_fallback: bool,
) -> dict[str, Any]:
    if invalid_or_fallback:
        return {"verdict": "invalid_or_fallback_not_scored", "counts": False}
    if window not in WINDOWS:
        return {"verdict": "not_scored_non_horizon_window", "counts": False, "window": window}
    posterior = candidate.get("posterior_coverage") if isinstance(candidate.get("posterior_coverage"), dict) else {}
    window_payload = posterior.get(window) if isinstance(posterior.get(window), dict) else {}
    product_results = (
        window_payload.get("product_results") if isinstance(window_payload.get("product_results"), dict) else {}
    )
    predicted_score = DIRECTIONS.get(predicted_direction, 0)
    if predicted_score == 0:
        return {"verdict": "not_scored_neutral_prediction", "counts": False, "window": window}
    actual_scores = []
    products = {}
    for product, result in product_results.items():
        if not isinstance(result, dict):
            continue
        actual_direction = str(result.get("actual_direction") or "中性")
        actual_score = DIRECTIONS.get(actual_direction, 0)
        products[product] = {
            "actual_direction": actual_direction,
            "scoring_eligible": bool(result.get("scoring_eligible")),
            "change_pct": result.get("change_pct"),
            "posterior_status": result.get("posterior_status") or result.get("status"),
            "effective_horizon_end": result.get("effective_horizon_end"),
            "horizon_end_policy": result.get("horizon_end_policy"),
        }
        if result.get("scoring_eligible") and actual_score != 0:
            actual_scores.append(actual_score)
    if not actual_scores:
        return {"verdict": "not_scored_missing_posterior", "counts": False, "window": window, "products": products}
    total = sum(actual_scores)
    actual_direction = "利多" if total > 0 else "利空" if total < 0 else "中性"
    if actual_direction == "中性":
        return {"verdict": "neutral_or_unscored", "counts": False, "window": window, "products": products}
    verdict = "hit" if predicted_score == DIRECTIONS[actual_direction] else "miss"
    return {
        "verdict": verdict,
        "counts": True,
        "window": window,
        "predicted_direction": predicted_direction,
        "actual_direction": actual_direction,
        "products": products,
    }


def score_action(
    *,
    actionability: str,
    should_enter: bool,
    window: str,
    layer_score: dict[str, Any],
    invalid_or_fallback: bool,
) -> dict[str, Any]:
    if invalid_or_fallback:
        return {"verdict": "invalid_or_fallback_not_scored", "counts": False}
    if actionability != "actionable":
        return {"verdict": f"{actionability}_not_action_scored", "counts": False}
    if not should_enter:
        return {"verdict": "disabled_by_model_not_entering_backtest", "counts": False}
    if window not in WINDOWS:
        return {"verdict": f"non_action_window_{window}", "counts": False}
    if layer_score.get("verdict") in {"hit", "miss"}:
        return {"verdict": layer_score["verdict"], "counts": True, "window": window}
    return {"verdict": str(layer_score.get("verdict") or "not_scored"), "counts": False, "window": window}


def score_watch(
    *,
    actionability: str,
    should_enter: bool,
    window: str,
    layer_score: dict[str, Any],
    invalid_or_fallback: bool,
) -> dict[str, Any]:
    if invalid_or_fallback:
        return {"verdict": "invalid_or_fallback_not_scored", "counts": False}
    if actionability != "watch_only":
        return {"verdict": "not_watch_layer", "counts": False}
    if not should_enter:
        return {"verdict": "watch_only_not_entering_observation_score", "counts": False}
    if window not in WINDOWS:
        return {"verdict": f"non_watch_window_{window}", "counts": False}
    if layer_score.get("verdict") in {"hit", "miss"}:
        return {"verdict": layer_score["verdict"], "counts": True, "window": window}
    return {"verdict": str(layer_score.get("verdict") or "not_scored"), "counts": False, "window": window}


def summarize_by_window(rows: list[dict[str, Any]], score_key: str) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for window in ("h1", "h3", "h7"):
        subset = [row for row in rows if row[score_key].get("window") == window]
        output[window] = {
            "scored": len(subset),
            "hit": sum(1 for row in subset if row[score_key]["verdict"] == "hit"),
            "miss": sum(1 for row in subset if row[score_key]["verdict"] == "miss"),
            "hit_rate": hit_rate(subset, score_key),
        }
    return output


def hit_rate(rows: list[dict[str, Any]], score_key: str) -> float | None:
    if not rows:
        return None
    hits = sum(1 for row in rows if row[score_key]["verdict"] == "hit")
    return round(hits / len(rows), 4)


def compact_candidate_for_trace(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_type": candidate.get("candidate_type"),
        "source_table": candidate.get("source_table"),
        "direction_hint": candidate.get("direction_hint"),
        "affected_products": candidate.get("affected_products"),
        "primary_spec": candidate.get("primary_spec"),
        "spec_hints": candidate.get("spec_hints"),
        "posterior_coverage_status": candidate.get("posterior_coverage_status"),
    }


def summarize_for_stdout(output: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    return {
        "output_dir": str(output_dir),
        "live_summary": output["live"]["summary"],
        "layered_score": output["layered_score"]["summary"],
    }


def write_summary(path: Path, output: dict[str, Any]) -> None:
    live = output["live"]["summary"]
    score = output["layered_score"]["summary"]
    lines = [
        "# Full Test V4-pro Run",
        "",
        "## Provider",
        f"- Events: {live['event_count']}",
        f"- Provider success: {live['provider_success']}",
        f"- Fallbacks: {live['fallbacks']}",
        f"- Validation errors: {live['validation_error_events']}",
        f"- Estimated cost: ¥{live['cny_est']}",
        "",
        "## Score",
        f"- Action scored: {score['action_scored']}",
        f"- Action hit/miss: {score['action_hit']} / {score['action_miss']}",
        f"- Action hit_rate: {score['action_hit_rate']}",
        f"- Watch scored: {score['watch_scored']}",
        f"- Watch hit/miss: {score['watch_hit']} / {score['watch_miss']}",
        f"- Watch hit_rate: {score['watch_hit_rate']}",
        f"- Action by window: {json.dumps(score['action_by_window'], ensure_ascii=False)}",
        "",
        "## Interpretation",
        "- hit_rate = hit / (hit + miss) on scored rows only; it is not a guarantee of future correctness.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_existing_judgments(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = read_json(path)
    rows = payload.get("judgments") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    return {str(item.get("event_id")): item for item in rows if isinstance(item, dict) and item.get("event_id")}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run full v4-pro prompt pack and score against prepared posterior coverage."
    )
    parser.add_argument("--prompt-pack", type=Path, default=DEFAULT_PROMPT_PACK)
    parser.add_argument("--candidate-pool", type=Path, default=DEFAULT_CANDIDATE_POOL)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--no-resume", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_full_test(
        prompt_pack=args.prompt_pack,
        candidate_pool=args.candidate_pool,
        output_dir=args.output_dir,
        limit=args.limit,
        offset=args.offset,
        resume=not args.no_resume,
        concurrency=args.concurrency,
    )


if __name__ == "__main__":
    main()

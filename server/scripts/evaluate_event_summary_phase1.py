#!/usr/bin/env python3
"""Privacy-safe Phase 1 provider evaluation with hard cost and request gates."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

DEFAULT_INPUT_USD_PER_MILLION = 0.435
DEFAULT_OUTPUT_USD_PER_MILLION = 0.87
DEFAULT_REQUIRED_YIELD = 0.51861
CURRENT_MODEL = "deepseek-v4-pro"
CURRENT_PROMPT_VERSION = "event-grounded-v9-core-fact-http-budget"
DEFAULT_MAX_INPUT_TOKENS = 10_000
DEFAULT_MAX_OUTPUT_TOKENS = 512
MAX_LOGICAL_CALLS_PER_ARTICLE = 3
PROMPT_OVERHEAD_CHARACTERS = 2_500
MAX_TOKENS_PER_CHARACTER = 0.6
WILSON_ONE_SIDED_95_Z = 1.6448536269514722


def connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _json_object(value: object) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _input_grade(row: dict[str, Any]) -> str:
    raw = _json_object(row.get("raw"))
    source_content = raw.get("source_content", {}) if isinstance(raw, dict) else {}
    summary_input = raw.get("summary_input_quality", {}) if isinstance(raw, dict) else {}
    return str(
        (source_content.get("status") if isinstance(source_content, dict) else "")
        or (summary_input.get("level") if isinstance(summary_input, dict) else "")
        or row.get("input_quality")
        or "unknown"
    )


def anonymous_id(article_id: str) -> str:
    return "sample-" + hashlib.sha256(f"phase1-eval:{article_id}".encode()).hexdigest()[:16]


def load_population(database: Path) -> list[dict[str, Any]]:
    with closing(connect_read_only(database)) as connection, connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """SELECT a.article_id,a.source_id,a.language,a.title,a.raw_text,a.published_at,
                          a.first_seen_at,a.created_at,a.raw,a.content_hash,
                          COALESCE(s.input_quality,'') AS input_quality,
                          COALESCE(s.summary_status,'') AS summary_status,
                          COALESCE(s.source_hash,'') AS source_hash,
                          COALESCE(s.model,'') AS model,
                          COALESCE(s.prompt_version,'') AS prompt_version
                   FROM news_articles a LEFT JOIN event_ai_summaries s USING(article_id)
                   ORDER BY a.article_id"""
            )
        ]
    from scripts.backfill_event_deepseek_summaries import select_candidates

    full_text = [row for row in rows if _input_grade(row) == "full_text"]
    return select_candidates(
        full_text,
        now=datetime.now(UTC),
        days=365,
        model=CURRENT_MODEL,
        prompt_version=CURRENT_PROMPT_VERSION,
    )


def stratified_sample(
    rows: list[dict[str, Any]],
    *,
    sample_size: int,
    seed: str,
) -> list[dict[str, Any]]:
    strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (str(row.get("source_id") or "unknown"), str(row.get("language") or "unknown"))
        strata[key].append(row)
    ordered: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for key, values in strata.items():
        ordered[key] = sorted(
            values,
            key=lambda row: hashlib.sha256(f"{seed}:{row['article_id']}".encode()).hexdigest(),
        )
    selected: list[dict[str, Any]] = []
    index = 0
    keys = sorted(ordered)
    limit = min(max(0, sample_size), len(rows))
    while len(selected) < limit:
        progressed = False
        for key in keys:
            if index < len(ordered[key]) and len(selected) < limit:
                selected.append(ordered[key][index])
                progressed = True
        if not progressed:
            break
        index += 1
    return selected


def conservative_input_tokens_per_attempt(
    rows: list[dict[str, Any]],
    *,
    max_input_tokens: int,
) -> int:
    constructed_characters = max(
        (
            min(12_000, len(str(row.get("raw_text") or "")))
            + len(str(row.get("title") or ""))
            + len(str(row.get("source_id") or ""))
            + len(str(row.get("published_at") or ""))
            + len(str(row.get("language") or ""))
            + PROMPT_OVERHEAD_CHARACTERS
            for row in rows
        ),
        default=PROMPT_OVERHEAD_CHARACTERS,
    )
    estimate = math.ceil(constructed_characters * MAX_TOKENS_PER_CHARACTER)
    if estimate > max_input_tokens:
        raise ValueError("constructed_input_exceeds_max_input_tokens")
    return estimate


def conservative_cost_usd(
    max_http_attempts: int,
    *,
    input_tokens_per_attempt: int,
    max_output_tokens: int,
    input_usd_per_million: float,
    output_usd_per_million: float,
) -> float:
    per_call = (
        input_tokens_per_attempt * input_usd_per_million + max_output_tokens * output_usd_per_million
    ) / 1_000_000
    return max_http_attempts * per_call


def wilson_one_sided_lower(successes: int, trials: int, *, z: float = WILSON_ONE_SIDED_95_Z) -> float:
    if trials <= 0:
        return 0.0
    n = float(trials)
    proportion = min(max(successes, 0), trials) / n
    denominator = 1 + z * z / n
    centre = proportion + z * z / (2 * n)
    margin = z * math.sqrt(proportion * (1 - proportion) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denominator)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("invalid_checkpoint_json") from exc
    return payload if isinstance(payload, dict) else {}


def _fingerprint(sample: list[dict[str, Any]], seed: str) -> str:
    ids = sorted(anonymous_id(str(row["article_id"])) for row in sample)
    return hashlib.sha256(json.dumps([seed, ids], separators=(",", ":")).encode()).hexdigest()


def build_dry_run(
    database: Path,
    *,
    sample_size: int,
    seed: str,
    max_http_attempts: int,
    max_input_tokens: int,
    max_output_tokens: int,
    input_usd_per_million: float,
    output_usd_per_million: float,
    max_cost_usd: float,
    required_yield: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    population = load_population(database)
    sample = stratified_sample(population, sample_size=sample_size, seed=seed)
    input_tokens_per_attempt = conservative_input_tokens_per_attempt(
        sample,
        max_input_tokens=max_input_tokens,
    )
    estimate = conservative_cost_usd(
        max_http_attempts,
        input_tokens_per_attempt=input_tokens_per_attempt,
        max_output_tokens=max_output_tokens,
        input_usd_per_million=input_usd_per_million,
        output_usd_per_million=output_usd_per_million,
    )
    if estimate > max_cost_usd:
        raise ValueError("conservative_cost_exceeds_max_cost_usd")
    strata = Counter(
        (str(row.get("source_id") or "unknown"), str(row.get("language") or "unknown"))
        for row in sample
    )
    report = {
        "mode": "dry_run",
        "writes_database": False,
        "provider_calls": False,
        "population_v9_candidates": len(population),
        "sample_size": len(sample),
        "sample_ids": [anonymous_id(str(row["article_id"])) for row in sample],
        "strata": [
            {"source_id": source, "language": language, "count": count}
            for (source, language), count in sorted(strata.items())
        ],
        "request_gate": {
            "max_http_attempts": max_http_attempts,
            "provider_max_retries": 0,
        },
        "cost_gate": {
            "currency": "USD",
            "conservative_cost_usd": round(estimate, 6),
            "max_cost_usd": max_cost_usd,
            "input_usd_per_million": input_usd_per_million,
            "output_usd_per_million": output_usd_per_million,
            "max_input_tokens": max_input_tokens,
            "estimated_input_tokens_per_attempt": input_tokens_per_attempt,
            "max_output_tokens": max_output_tokens,
            "reserved_http_attempts": max_http_attempts,
            "prompt_overhead_characters": PROMPT_OVERHEAD_CHARACTERS,
            "max_tokens_per_character": MAX_TOKENS_PER_CHARACTER,
        },
        "required_yield": required_yield,
        "wilson_95_one_sided_lower": None,
        "wilson_lower_meets_required_yield": False,
        "sample_fingerprint": _fingerprint(sample, seed),
        "privacy": {
            "contains_titles": False,
            "contains_source_text": False,
            "contains_model_output": False,
            "contains_credentials": False,
        },
    }
    return report, sample


async def execute_provider(
    dry_run: dict[str, Any],
    sample: list[dict[str, Any]],
    *,
    checkpoint_path: Path,
    required_yield: float,
    max_http_attempts: int,
    client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    if max_http_attempts != int(dry_run["request_gate"]["max_http_attempts"]):
        raise ValueError("execution_request_cap_mismatch")
    if client_factory is None:
        from app.deepseek_client import DeepSeekClient

        client_factory = DeepSeekClient
    client = client_factory()
    client.max_retries = 0
    client.max_output_tokens = int(dry_run["cost_gate"]["max_output_tokens"])
    checkpoint = _read_checkpoint(checkpoint_path)
    fingerprint = str(dry_run["sample_fingerprint"])
    if checkpoint and checkpoint.get("sample_fingerprint") != fingerprint:
        raise ValueError("checkpoint_sample_mismatch")
    outcomes = checkpoint.get("outcomes", {}) if isinstance(checkpoint.get("outcomes", {}), dict) else {}
    prior_http_attempts = int(checkpoint.get("http_attempts_used", 0) or 0)
    if not 0 <= prior_http_attempts <= max_http_attempts:
        raise ValueError("checkpoint_http_attempts_invalid")

    def persist_checkpoint(current_run_attempts: int) -> None:
        _atomic_write_json(
            checkpoint_path,
            {
                "schema_version": 1,
                "sample_fingerprint": fingerprint,
                "http_attempts_used": prior_http_attempts + current_run_attempts,
                "outcomes": outcomes,
            },
        )

    client.set_http_attempt_budget(
        max_http_attempts - prior_http_attempts,
        on_attempt=persist_checkpoint,
    )
    for row in sample:
        sample_id = anonymous_id(str(row["article_id"]))
        if sample_id in outcomes:
            continue
        attempts_used = int(getattr(client, "http_attempts_used", 0))
        if max_http_attempts - prior_http_attempts - attempts_used < MAX_LOGICAL_CALLS_PER_ARTICLE:
            break
        try:
            result = await client.summarize_event_grounded(
                title=str(row.get("title") or ""),
                raw_text=str(row.get("raw_text") or ""),
                source_name=str(row.get("source_id") or ""),
                published_at=str(row.get("published_at") or ""),
                language=str(row.get("language") or "unknown"),
                input_quality="full_text",
            )
            passed = bool(result.usable and result.fact_summary_status == "completed")
            reasons = list(result.rejection_reasons) + list(result.impact_quality_reasons)
            outcomes[sample_id] = {
                "passed_fact_gate": passed,
                "fact_status": str(result.fact_summary_status),
                "impact_status": str(result.impact_analysis_status),
                "reason_codes": sorted({str(reason).split(":", 1)[0] for reason in reasons}),
            }
        except Exception as exc:
            safe_message = str(exc).strip()
            safe_error_code = (
                safe_message
                if re.fullmatch(r"[a-z0-9_]{1,80}", safe_message)
                else exc.__class__.__name__
            )
            outcomes[sample_id] = {
                "passed_fact_gate": False,
                "fact_status": "failed",
                "impact_status": "not_requested",
                "reason_codes": [safe_error_code],
            }
        persist_checkpoint(int(getattr(client, "http_attempts_used", 0)))
    trials = len(outcomes)
    successes = sum(bool(item.get("passed_fact_gate")) for item in outcomes.values())
    lower = wilson_one_sided_lower(successes, trials)
    return {
        **{key: value for key, value in dry_run.items() if key not in {"mode", "provider_calls"}},
        "mode": "execute_provider",
        "provider_calls": True,
        "checkpoint_path": str(checkpoint_path.resolve()),
        "completed_samples": trials,
        "remaining_samples": len(sample) - trials,
        "passed_fact_gate": successes,
        "observed_pass_rate": round(successes / trials, 6) if trials else 0.0,
        "wilson_95_one_sided_lower": round(lower, 6),
        "required_yield": required_yield,
        "wilson_lower_meets_required_yield": lower >= required_yield,
        "provider_http_attempts_this_run": int(getattr(client, "http_attempts_used", 0)),
        "provider_http_attempts_cumulative": (
            prior_http_attempts + int(getattr(client, "http_attempts_used", 0))
        ),
        "outcome_counts": dict(
            sorted(Counter(str(item.get("fact_status") or "unknown") for item in outcomes.values()).items())
        ),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--sample-size", type=int, default=20)
    parser.add_argument("--seed", default="phase1-v1")
    parser.add_argument("--execute-provider", action="store_true")
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=Path(".codex-run/evals/event-summary-phase1-checkpoint.json"),
    )
    parser.add_argument("--max-http-attempts", type=int, default=50)
    parser.add_argument("--max-input-tokens", type=int, default=DEFAULT_MAX_INPUT_TOKENS)
    parser.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
    parser.add_argument("--input-usd-per-million", type=float, default=DEFAULT_INPUT_USD_PER_MILLION)
    parser.add_argument("--output-usd-per-million", type=float, default=DEFAULT_OUTPUT_USD_PER_MILLION)
    parser.add_argument("--max-cost-usd", type=float, default=5.0)
    parser.add_argument("--required-yield", type=float, default=DEFAULT_REQUIRED_YIELD)
    args = parser.parse_args(argv)
    if min(args.sample_size, args.max_http_attempts, args.max_input_tokens, args.max_output_tokens) < 1:
        parser.error("sample, request and token limits must be positive")
    if min(args.input_usd_per_million, args.output_usd_per_million, args.max_cost_usd) < 0:
        parser.error("prices and cost cap must be non-negative")
    if not 0 <= args.required_yield <= 1:
        parser.error("required yield must be in [0, 1]")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report, sample = build_dry_run(
            args.db,
            sample_size=args.sample_size,
            seed=args.seed,
            max_http_attempts=args.max_http_attempts,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            input_usd_per_million=args.input_usd_per_million,
            output_usd_per_million=args.output_usd_per_million,
            max_cost_usd=args.max_cost_usd,
            required_yield=args.required_yield,
        )
        if args.execute_provider:
            report = asyncio.run(
                execute_provider(
                    report,
                    sample,
                    checkpoint_path=args.checkpoint,
                    required_yield=args.required_yield,
                    max_http_attempts=args.max_http_attempts,
                )
            )
    except ValueError as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

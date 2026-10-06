#!/usr/bin/env python3
"""Read-only Phase 1 backlog, request-budget, cost, and reachability audit."""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

CURRENT_PROMPT_VERSION = "event-grounded-v9-core-fact-http-budget"
CURRENT_MODEL = "deepseek-v4-pro"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_DAILY_HTTP_REQUEST_LIMIT = 50

# DeepSeek's public USD prices observed on 2026-07-27. Pricing is deliberately
# overridable because it is external state and may change.
DEFAULT_INPUT_USD_PER_MILLION = 0.435
DEFAULT_OUTPUT_USD_PER_MILLION = 0.87


def connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.create_function("is_customer_chinese_summary", 1, is_customer_chinese_summary)
    connection.execute("PRAGMA query_only=ON")
    return connection


def is_customer_chinese_summary(value: object) -> int:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    han_count = len(re.findall(r"[\u3400-\u9fff]", text))
    latin_count = len(re.findall(r"[A-Za-z]", text))
    return int(han_count >= 8 and han_count / max(han_count + latin_count, 1) >= 0.5)


def input_grade(row: dict[str, Any]) -> str:
    try:
        raw = json.loads(str(row.get("raw") or "{}"))
    except (TypeError, json.JSONDecodeError):
        raw = {}
    source_content = raw.get("source_content", {}) if isinstance(raw, dict) else {}
    summary_input = raw.get("summary_input_quality", {}) if isinstance(raw, dict) else {}
    grade = (
        (source_content.get("status") if isinstance(source_content, dict) else "")
        or (summary_input.get("level") if isinstance(summary_input, dict) else "")
        or row.get("input_quality")
        or "unknown"
    )
    return str(grade)


def is_current_revision(row: dict[str, Any], *, model: str, prompt_version: str) -> bool:
    return (
        str(row.get("source_hash") or "") == str(row.get("content_hash") or "")
        and str(row.get("model") or "") == model
        and str(row.get("prompt_version") or "") == prompt_version
    )


def is_within_worker_window(row: dict[str, Any], *, now: datetime, days: int = 365) -> bool:
    value = str(row.get("published_at") or row.get("first_seen_at") or row.get("created_at") or "")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        try:
            timestamp = parsedate_to_datetime(value).astimezone(UTC)
        except (TypeError, ValueError):
            return False
    return now.astimezone(UTC) - timedelta(days=max(1, min(days, 365))) <= timestamp <= now.astimezone(UTC)


def request_attempt_bounds(
    article_count: int,
    *,
    max_logical_calls_per_article: int = 3,
    provider_retries: int = 2,
) -> dict[str, int]:
    """Bound calls made by grounded fact -> optional repair -> impact.

    One full-text article always needs a fact call. A fact that passes the local
    gate needs an impact call, and invalid fact JSON can add one repair call.
    Each logical call can make ``provider_retries + 1`` HTTP attempts.
    """
    articles = max(0, int(article_count))
    logical_max = articles * max(1, int(max_logical_calls_per_article))
    return {
        "articles": articles,
        "logical_calls_min": articles,
        "logical_calls_max": logical_max,
        "http_attempts_min": articles,
        "http_attempts_max": logical_max * max(1, int(provider_retries) + 1),
    }


def cost_range_usd(
    rows: list[dict[str, Any]],
    *,
    input_price: float,
    output_price: float,
) -> dict[str, float]:
    """Return an explicit planning range, not a provider invoice prediction."""
    capped_chars = sum(min(12_000, len(str(row.get("raw_text") or ""))) for row in rows)
    count = len(rows)
    # Lower: one fact response, 0.3 token/input char, 250 output tokens.
    low_input_tokens = capped_chars * 0.3
    low_output_tokens = count * 250
    # Upper: fact + repair + impact, each with prompt overhead and conservative
    # 0.6 token/character conversion, with 1,000 output tokens per completion.
    high_input_tokens = 3 * ((capped_chars + count * 2_000) * 0.6)
    high_output_tokens = count * 3 * 1_000
    low = (low_input_tokens * input_price + low_output_tokens * output_price) / 1_000_000
    high = (high_input_tokens * input_price + high_output_tokens * output_price) / 1_000_000
    return {
        "input_tokens_low": round(low_input_tokens),
        "input_tokens_high": round(high_input_tokens),
        "output_tokens_low": round(low_output_tokens),
        "output_tokens_high": round(high_output_tokens),
        "cost_usd_low": round(low, 4),
        "cost_usd_high": round(high, 4),
    }


def build_report(
    database: Path,
    *,
    model: str = CURRENT_MODEL,
    prompt_version: str = CURRENT_PROMPT_VERSION,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    target_ratio: float = 0.08,
    daily_http_request_limit: int = DEFAULT_DAILY_HTTP_REQUEST_LIMIT,
    provider_retries: int = 2,
    input_price: float = DEFAULT_INPUT_USD_PER_MILLION,
    output_price: float = DEFAULT_OUTPUT_USD_PER_MILLION,
) -> dict[str, Any]:
    with closing(connect_read_only(database)) as connection, connection:
        rows = [
            dict(row)
            for row in connection.execute(
                """SELECT a.article_id,a.content_hash,a.raw,a.raw_text,
                          a.published_at,a.first_seen_at,a.created_at,
                          s.summary_status,s.fact_summary_status,s.impact_analysis_status,
                          s.input_quality,s.source_hash,s.model,s.prompt_version,s.attempts
                   FROM news_articles a LEFT JOIN event_ai_summaries s USING(article_id)
                   ORDER BY a.article_id"""
            )
        ]
        complete_events = int(
            connection.execute(
                """SELECT COUNT(DISTINCT c.cluster_id)
                   FROM event_ai_summaries s
                   JOIN news_articles a USING(article_id)
                   JOIN news_event_clusters c
                     ON EXISTS (
                       SELECT 1 FROM json_each(c.article_ids)
                       WHERE json_each.value=s.article_id
                     )
                   JOIN event_observations o ON o.event_record_id=c.event_record_id
                   WHERE s.input_quality='full_text'
                     AND s.summary_status='completed'
                     AND s.quality_status='completed'
                     AND s.fact_summary_status='completed'
                     AND s.impact_analysis_status='completed'
                     AND json_extract(s.business_impact_payload,'$.relevant')=1
                     AND TRIM(s.factual_summary)!=''
                     AND is_customer_chinese_summary(s.factual_summary)=1
                     AND c.status='featured'
                     AND COALESCE(c.event_record_id,'')!=''
                     AND json_valid(c.affected_products)=1
                     AND json_array_length(c.affected_products)>0
                     AND json_valid(o.affected_products)=1
                     AND json_array_length(o.affected_products)>0
                     AND o.event_record_id=c.event_record_id"""
            ).fetchone()[0]
        )
        structurally_eligible_clusters = [
            dict(row)
            for row in connection.execute(
                """SELECT c.cluster_id,c.article_ids
                   FROM news_event_clusters c
                   WHERE c.status='featured'
                     AND COALESCE(c.event_record_id,'')!=''
                     AND json_valid(c.affected_products)=1
                     AND json_array_length(c.affected_products)>0
                     AND EXISTS (
                       SELECT 1 FROM event_observations o
                       WHERE o.event_record_id=c.event_record_id
                         AND json_valid(o.affected_products)=1
                         AND json_array_length(o.affected_products)>0
                     )"""
            )
        ]

    full_text = [row for row in rows if input_grade(row) == "full_text"]
    now = datetime.now(UTC)
    worker_eligible = [row for row in full_text if is_within_worker_window(row, now=now)]
    window_excluded = [row for row in full_text if row not in worker_eligible]
    current = [
        row for row in worker_eligible if is_current_revision(row, model=model, prompt_version=prompt_version)
    ]
    stale = [row for row in worker_eligible if row not in current]
    pending = [row for row in current if row.get("summary_status") == "pending"]
    processing = [row for row in current if row.get("summary_status") == "processing"]
    retryable_failed = [
        row
        for row in current
        if row.get("summary_status") == "failed" and int(row.get("attempts") or 0) < max_attempts
    ]
    dead_letter = [
        row
        for row in current
        if row.get("summary_status") == "failed" and int(row.get("attempts") or 0) >= max_attempts
    ]
    rejected = [row for row in current if row.get("summary_status") == "rejected"]
    completed = [row for row in current if row.get("summary_status") == "completed"]
    default_candidates = stale + pending + retryable_failed
    explicit_reopen_candidates = default_candidates + rejected
    default_candidate_ids = {str(row["article_id"]) for row in default_candidates}
    structurally_eligible_candidate_clusters = 0
    for cluster in structurally_eligible_clusters:
        try:
            members = {str(value) for value in json.loads(str(cluster.get("article_ids") or "[]"))}
        except (TypeError, json.JSONDecodeError):
            members = set()
        structurally_eligible_candidate_clusters += bool(members & default_candidate_ids)

    article_count = len(rows)
    target_count = math.ceil(article_count * target_ratio)
    additional_needed = max(0, target_count - complete_events)
    historical_decisions = sum(
        row.get("summary_status") in {"completed", "rejected"}
        and str(row.get("prompt_version") or "").startswith("event-grounded-")
        for row in full_text
    )
    historical_complete_yield = complete_events / historical_decisions if historical_decisions else 0.0
    default_required_yield = additional_needed / len(default_candidates) if default_candidates else math.inf
    reopened_required_yield = (
        additional_needed / len(explicit_reopen_candidates) if explicit_reopen_candidates else math.inf
    )
    expected_default_additions = math.floor(len(default_candidates) * historical_complete_yield)
    structural_upper_bound = complete_events + structurally_eligible_candidate_clusters

    request_bounds = request_attempt_bounds(
        len(default_candidates),
        provider_retries=provider_retries,
    )
    safe_articles_per_day = daily_http_request_limit // (3 * max(1, provider_retries + 1))
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "read_only_no_provider_calls",
        "database": str(database.resolve()),
        "revision": {"model": model, "prompt_version": prompt_version},
        "population": {
            "articles": article_count,
            "full_text": len(full_text),
            "complete_events": complete_events,
        },
        "backlog": {
            "not_queued": sum(row.get("summary_status") is None for row in full_text),
            "pending": len(pending),
            "processing": len(processing),
            "completed": len(completed),
            "rejected": len(rejected),
            "retryable_failed": len(retryable_failed),
            "dead_letter": len(dead_letter),
            "stale_revision": len(stale),
            "excluded_by_worker_365_day_or_invalid_date_window": len(window_excluded),
            "default_dry_run_candidates": len(default_candidates),
            "explicit_reopen_candidates": len(explicit_reopen_candidates),
            "attempt_histogram": dict(
                sorted(Counter(int(row.get("attempts") or 0) for row in full_text).items())
            ),
        },
        "request_budget": {
            "configured_daily_http_request_limit": daily_http_request_limit,
            "candidate_bounds_without_daily_guard": request_bounds,
            "selected_article_count_is_http_request_measure": False,
            "hard_http_attempt_guard_present": True,
            "daily_http_attempts_max_with_guard": daily_http_request_limit,
            "whole_articles_per_day_worst_case_planning_floor": safe_articles_per_day,
            "days_to_drain_range_under_guard": [
                math.ceil(request_bounds["http_attempts_min"] / daily_http_request_limit),
                math.ceil(request_bounds["http_attempts_max"] / daily_http_request_limit),
            ],
            "reservation_durability": "atomically persisted immediately before each provider HTTP attempt",
        },
        "cost": {
            "currency": "USD",
            "input_usd_per_million_tokens": input_price,
            "output_usd_per_million_tokens": output_price,
            "assumptions": {
                "input_token_per_character_range": [0.3, 0.6],
                "output_tokens_per_logical_call_range": [250, 1_000],
                "logical_calls_per_article_range": [1, 3],
                "source_character_cap": 12_000,
                "prompt_overhead_characters_upper_per_call": 2_000,
                "cache_pricing": "cache_miss",
            },
            **cost_range_usd(
                default_candidates,
                input_price=input_price,
                output_price=output_price,
            ),
        },
        "target": {
            "ratio": target_ratio,
            "count": target_count,
            "additional_complete_events_needed": additional_needed,
            "historical_grounded_decisions": historical_decisions,
            "historical_end_to_end_complete_yield": round(historical_complete_yield, 6),
            "required_default_candidate_yield": (
                round(default_required_yield, 6) if math.isfinite(default_required_yield) else None
            ),
            "required_with_explicit_reopen_yield": (
                round(reopened_required_yield, 6) if math.isfinite(reopened_required_yield) else None
            ),
            "expected_complete_events_after_default_at_historical_yield": (
                complete_events + expected_default_additions
            ),
            "mathematically_possible_default_perfect_yield": (
                complete_events + len(default_candidates) >= target_count
            ),
            "structurally_eligible_default_candidate_clusters": (
                structurally_eligible_candidate_clusters
            ),
            "complete_event_upper_bound_without_new_promotions": structural_upper_bound,
            "complete_event_upper_bound_ratio_without_new_promotions": round(
                structural_upper_bound / article_count,
                6,
            )
            if article_count
            else 0.0,
            "structurally_possible_without_new_promotions": structural_upper_bound >= target_count,
            "mathematically_possible_with_reopen_perfect_yield": (
                complete_events + len(explicit_reopen_candidates) >= target_count
            ),
            "supported_at_observed_yield": (
                complete_events + expected_default_additions >= target_count
            ),
        },
        "recovery_contract": {
            "idempotency_key": ["article_id", "source_hash", "model", "prompt_version"],
            "lease_recovery_minutes": 30,
            "retry_cap": max_attempts,
            "dead_letter_definition": f"summary_status=failed AND attempts>={max_attempts}",
            "rejected_reopen_requires_explicit_flag": True,
        },
        "writes_database": False,
        "provider_calls": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=CURRENT_MODEL)
    parser.add_argument("--prompt-version", default=CURRENT_PROMPT_VERSION)
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--target-ratio", type=float, default=0.08)
    parser.add_argument("--daily-http-request-limit", type=int, default=DEFAULT_DAILY_HTTP_REQUEST_LIMIT)
    parser.add_argument("--provider-retries", type=int, default=2)
    parser.add_argument("--input-usd-per-million", type=float, default=DEFAULT_INPUT_USD_PER_MILLION)
    parser.add_argument("--output-usd-per-million", type=float, default=DEFAULT_OUTPUT_USD_PER_MILLION)
    args = parser.parse_args()
    if args.max_attempts < 1 or args.daily_http_request_limit < 1 or args.provider_retries < 0:
        parser.error("attempt and request limits must be positive; retries must be non-negative")
    if not 0 < args.target_ratio <= 1:
        parser.error("target ratio must be in (0, 1]")
    report = build_report(
        args.db,
        model=args.model,
        prompt_version=args.prompt_version,
        max_attempts=args.max_attempts,
        target_ratio=args.target_ratio,
        daily_http_request_limit=args.daily_http_request_limit,
        provider_retries=args.provider_retries,
        input_price=args.input_usd_per_million,
        output_price=args.output_usd_per_million,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

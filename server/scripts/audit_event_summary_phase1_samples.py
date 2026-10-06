from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app.event_summary_quality import is_customer_chinese_summary  # noqa: E402


def _json_object(value: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _json_list(value: str) -> list[Any]:
    try:
        decoded = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _anonymous_id(article_id: str) -> str:
    return hashlib.sha256(article_id.encode("utf-8")).hexdigest()[:12]


def _repeated_fragment(summary: str) -> bool:
    fragments = [part.strip() for part in summary.replace("！", "。").replace("；", "。").split("。") if part.strip()]
    return len(fragments) != len(set(fragments))


def audit_record(row: sqlite3.Row) -> dict[str, Any]:
    summary = str(row["factual_summary"] or "").strip()
    facts = _json_object(str(row["fact_payload"] or "{}"))
    quotes = facts.get("evidence_quotes")
    if not isinstance(quotes, list):
        quotes = []
    source = str(row["raw_text"] or "")
    normalized_source = " ".join(source.split()).casefold()
    supported_quotes = [
        quote
        for quote in quotes
        if isinstance(quote, str) and quote.strip() and " ".join(quote.split()).casefold() in normalized_source
    ]
    flags: list[str] = []
    if row["summary_status"] == "completed":
        if row["input_quality"] != "full_text":
            flags.append("completed_without_full_text")
        if not summary:
            flags.append("completed_empty")
        elif not is_customer_chinese_summary(summary):
            flags.append("non_chinese_formal_summary")
        if not str(facts.get("subject") or "").strip():
            flags.append("missing_subject")
        if not str(facts.get("action") or "").strip():
            flags.append("missing_action")
        if len(supported_quotes) < 2 or len(supported_quotes) != len(quotes):
            flags.append("unsupported_evidence_quote")
        if _repeated_fragment(summary):
            flags.append("repeated_fragment")
    return {
        "sample_id": _anonymous_id(str(row["article_id"])),
        "summary_status": str(row["summary_status"]),
        "quality_status": str(row["quality_status"]),
        "fact_summary_status": str(row["fact_summary_status"]),
        "impact_analysis_status": str(row["impact_analysis_status"]),
        "input_quality": str(row["input_quality"]),
        "source_id": str(row["source_id"]),
        "prompt_version": str(row["prompt_version"]),
        "language": str(row["language"]),
        "flags": flags,
    }


def build_report(db_path: Path, sample_per_stratum: int = 5) -> dict[str, Any]:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection, connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        rows = connection.execute(
            """
            SELECT s.article_id, s.factual_summary, s.summary_status, s.quality_status,
                   s.fact_summary_status, s.impact_analysis_status, s.input_quality,
                   s.prompt_version, s.fact_payload, s.quality_reasons,
                   a.source_id, a.language, a.raw_text
            FROM event_ai_summaries AS s
            JOIN news_articles AS a USING(article_id)
            WHERE s.summary_status IN ('completed', 'rejected')
            ORDER BY s.article_id
            """
        ).fetchall()

    audited = [audit_record(row) for row in rows]
    strata: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in audited:
        strata[(item["summary_status"], item["source_id"])].append(item)
    samples: list[dict[str, Any]] = []
    for key in sorted(strata):
        ordered = sorted(strata[key], key=lambda item: item["sample_id"])
        samples.extend(ordered[:sample_per_stratum])

    rejection_reasons = Counter(
        str(reason)
        for row in rows
        if row["summary_status"] == "rejected"
        for reason in _json_list(str(row["quality_reasons"] or "[]"))
        if isinstance(reason, str)
    )
    completed = [item for item in audited if item["summary_status"] == "completed"]
    completed_flags = Counter(flag for item in completed for flag in item["flags"])
    return {
        "mode": "sqlite_read_only_no_provider_calls",
        "privacy": "aggregate_and_anonymous_ids_only",
        "population": len(audited),
        "status_counts": dict(Counter(item["summary_status"] for item in audited)),
        "completed_quality": {
            "count": len(completed),
            "clean": sum(not item["flags"] for item in completed),
            "flagged": sum(bool(item["flags"]) for item in completed),
            "flag_counts": dict(completed_flags),
            "customer_chinese_pass_rate": round(
                sum("non_chinese_formal_summary" not in item["flags"] for item in completed)
                / max(1, len(completed)),
                4,
            ),
        },
        "flagged_completed_samples": [item for item in completed if item["flags"]],
        "rejection_reason_counts": dict(rejection_reasons),
        "sample_rule": f"sha256_order_first_{sample_per_stratum}_per_status_and_source",
        "anonymous_sample": samples,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only, anonymous Phase 1 event-summary sample audit.")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--sample-per-stratum", type=int, default=5)
    args = parser.parse_args()
    report = build_report(args.db, max(1, min(args.sample_per_stratum, 20)))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

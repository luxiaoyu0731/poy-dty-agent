from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_REPORT_DIR = SERVER_ROOT / "data" / "backfill_reports"
DEFAULT_START = date(2025, 6, 16)
DEFAULT_END = date(2026, 6, 15)

POSTERIOR_TERMS = (
    "后验",
    "回测",
    "错因",
    "错判",
    "命中率",
    "hit_rate",
    "verdict",
    "miss_reason",
    "miss_reasons",
    "error_reason",
    "posterior_status",
    "actual_direction",
    "weight_adjustment",
    "weight_adjustments",
    "price_curve_explanation",
    "ex_post",
    "backtest",
)
POSTERIOR_KEYS = {
    "actual_direction",
    "error_cause",
    "error_reason",
    "ex_post_explanation",
    "hit_rate",
    "miss_reason",
    "miss_reasons",
    "posterior_status",
    "price_curve_explanation",
    "targets",
    "verdict",
    "weight_adjustment",
    "weight_adjustments",
}


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            try:
                parsed = datetime.fromisoformat(text[:10])
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_date(value: Any) -> date | None:
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def parse_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, dict | list):
        return value
    if value in {None, ""}:
        return fallback
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return fallback


def parse_json_list(value: Any) -> list[str]:
    parsed = parse_json(value, [])
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    if isinstance(parsed, str) and parsed.strip():
        return [parsed.strip()]
    return []


def parse_fact_sentence_citations(raw_value: Any, reasoning: str = "") -> list[dict[str, Any]]:
    raw = parse_json(raw_value, {})
    entries = raw.get("fact_sentence_citations") if isinstance(raw, dict) else None
    if isinstance(entries, list):
        parsed: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            sentence = str(entry.get("sentence") or "").strip()
            if not sentence:
                continue
            cited_doc_ids = parse_json_list(entry.get("cited_doc_ids"))
            parsed.append(
                {
                    "sentence": sentence,
                    "cited_doc_ids": cited_doc_ids,
                    "covered": bool(cited_doc_ids),
                }
            )
        return parsed
    return [
        {"sentence": sentence, "cited_doc_ids": [], "covered": False} for sentence in split_fact_sentences(reasoning)
    ]


def split_fact_sentences(text: str) -> list[str]:
    parts = [part.strip() for part in str(text or "").replace("；", "。").replace("。", ".").split(".")]
    return [part for part in parts if len(part) >= 8]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit temporal RAG evidence used by cached LLM judgments.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=DEFAULT_END.isoformat())
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_REPORT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def audit_temporal_rag(db_path: Path, *, start: date, end: date) -> dict[str, Any]:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection, connection:
        connection.row_factory = sqlite3.Row
        doc_index = build_doc_index(connection)
        judgments = [dict(row) for row in connection.execute("SELECT * FROM llm_event_directions").fetchall()]

    scoped = [row for row in judgments if (parsed := parse_date(row.get("as_of_time"))) and start <= parsed <= end]
    leaks: list[dict[str, Any]] = []
    missing_docs: list[dict[str, Any]] = []
    prediction_doc_refs: list[dict[str, Any]] = []
    posterior_refs: list[dict[str, Any]] = []
    with_citations = 0
    fact_sentence_covered = 0
    fact_sentence_total = 0
    fact_sentence_citation_covered = 0
    missing_fact_sentence_citations: list[dict[str, Any]] = []

    for row in scoped:
        as_of = parse_datetime(row.get("as_of_time"))
        doc_ids = parse_json_list(row.get("cited_doc_ids"))
        if doc_ids:
            with_citations += 1
        reasoning = str(row.get("reasoning") or "")
        if doc_ids and sentence_count(reasoning) > 0:
            fact_sentence_covered += 1
        fact_sentence_entries = parse_fact_sentence_citations(row.get("raw"), reasoning)
        fact_sentence_total += len(fact_sentence_entries)
        for entry in fact_sentence_entries:
            cited_for_sentence = [doc_id for doc_id in entry["cited_doc_ids"] if doc_id in doc_ids]
            if cited_for_sentence:
                fact_sentence_citation_covered += 1
                continue
            missing_fact_sentence_citations.append(
                {
                    "judgment_id": row.get("judgment_id"),
                    "event_id": row.get("event_id"),
                    "as_of_time": row.get("as_of_time"),
                    "sentence": entry["sentence"],
                    "sentence_cited_doc_ids": entry["cited_doc_ids"],
                }
            )
        for doc_id in doc_ids:
            doc = doc_index.get(doc_id)
            if doc is None:
                missing_docs.append(
                    {"judgment_id": row.get("judgment_id"), "event_id": row.get("event_id"), "doc_id": doc_id}
                )
                continue
            if doc["doc_type"] == "prediction_record":
                prediction_doc_refs.append(
                    {"judgment_id": row.get("judgment_id"), "event_id": row.get("event_id"), "doc_id": doc_id}
                )
            if doc["doc_type"] in {"backtest_result", "error_cause", "weight_adjustment"}:
                posterior_refs.append(
                    {
                        "judgment_id": row.get("judgment_id"),
                        "event_id": row.get("event_id"),
                        "doc_id": doc_id,
                        "reason": "posterior_doc_type",
                    }
                )
            visible_at = parse_datetime(doc.get("visible_at"))
            if as_of and visible_at and visible_at > as_of:
                leaks.append(
                    {
                        "judgment_id": row.get("judgment_id"),
                        "event_id": row.get("event_id"),
                        "as_of_time": row.get("as_of_time"),
                        "doc_id": doc_id,
                        "doc_type": doc.get("doc_type"),
                        "visible_at": doc.get("visible_at"),
                    }
                )
        posterior_keys = sorted(find_posterior_keys(parse_json(row.get("raw"), {})))
        posterior_text = "\n".join(str(row.get(key) or "") for key in ("title", "reasoning", "counter_evidence", "raw"))
        if posterior_keys or contains_posterior_term(posterior_text):
            posterior_refs.append(
                {
                    "judgment_id": row.get("judgment_id"),
                    "event_id": row.get("event_id"),
                    "as_of_time": row.get("as_of_time"),
                    "posterior_keys": posterior_keys,
                    "reason": "posterior_or_backtest_payload",
                }
            )

    entering = [row for row in scoped if int(row.get("should_enter_backtest") or 0) == 1]
    entering_ids = {str(row.get("judgment_id")) for row in entering}
    entering_leaks = [leak for leak in leaks if str(leak.get("judgment_id")) in entering_ids]
    return {
        "suite": "eval-v2-rag-leak-audit",
        "scope": {"start": start.isoformat(), "end": end.isoformat()},
        "metrics": {
            "total_judgments": len(scoped),
            "judgments_with_citations": with_citations,
            "citation_coverage_rate": round(with_citations / len(scoped), 6) if scoped else 0.0,
            "fact_sentence_doc_coverage_rate": round(fact_sentence_covered / len(scoped), 6) if scoped else 0.0,
            "fact_sentence_citations": fact_sentence_total,
            "fact_sentence_citations_covered": fact_sentence_citation_covered,
            "fact_sentence_citation_coverage_rate": (
                round(fact_sentence_citation_covered / fact_sentence_total, 6) if fact_sentence_total else 1.0
            ),
            "missing_fact_sentence_citation_count": len(missing_fact_sentence_citations),
            "missing_doc_refs": len(missing_docs),
            "future_leak_count": len(leaks),
            "visible_at_lte_as_of_time_violation_count": len(leaks),
            "future_leak_count_entering_backtest": len(entering_leaks),
            "prediction_doc_refs": len(prediction_doc_refs),
            "backtest_or_error_refs": len(posterior_refs),
            "entering_backtest_judgments": len(entering),
        },
        "guardrails": {
            "visible_at_lte_as_of_time": len(leaks) == 0,
            "prediction_phase_blocks_posterior_price_docs": len(prediction_doc_refs) == 0,
            "prediction_phase_blocks_backtest_docs": len(posterior_refs) == 0,
            "posterior_backtest_error_context_blocked": len(posterior_refs) == 0,
            "formal_scoring_future_leak_count_must_be_zero": len(entering_leaks) == 0,
            "cited_doc_ids_required": with_citations == len(scoped),
            "fact_sentence_citations_required": len(missing_fact_sentence_citations) == 0,
            "provider_calls": 0,
            "llm_called": False,
        },
        "samples": {
            "future_leaks": leaks[:25],
            "missing_docs": missing_docs[:25],
            "prediction_doc_refs": prediction_doc_refs[:25],
            "posterior_or_backtest_refs": posterior_refs[:25],
            "missing_fact_sentence_citations": missing_fact_sentence_citations[:25],
        },
    }


def build_doc_index(connection: sqlite3.Connection) -> dict[str, dict[str, str]]:
    docs: dict[str, dict[str, str]] = {}
    article_dates: dict[str, str] = {}
    for row in connection.execute("SELECT article_id, published_at, first_seen_at FROM news_articles").fetchall():
        article_id = str(row["article_id"])
        article_dates[article_id] = str(row["published_at"] or row["first_seen_at"] or "")
        docs[f"news_article:{article_id}"] = {
            "doc_type": "news_article",
            "visible_at": str(row["published_at"] or row["first_seen_at"] or ""),
        }
    for row in connection.execute("SELECT event_record_id, occurred_at FROM event_observations").fetchall():
        event_id = str(row["event_record_id"])
        doc = {"doc_type": "event_observation", "visible_at": str(row["occurred_at"] or "")}
        docs[f"event:{event_id}"] = doc
        docs[f"event_candidate:{event_id}"] = doc
    for row in connection.execute(
        "SELECT cluster_id, created_at, updated_at, article_ids FROM news_event_clusters"
    ).fetchall():
        cluster_id = str(row["cluster_id"])
        dates = sorted(
            article_dates[article_id]
            for article_id in parse_json_list(row["article_ids"])
            if article_dates.get(article_id)
        )
        doc = {
            "doc_type": "news_event_cluster",
            "visible_at": dates[0] if dates else str(row["created_at"] or row["updated_at"] or ""),
        }
        docs[f"news_event:{cluster_id}"] = doc
        docs[f"event_candidate:{cluster_id}"] = doc
    for row in connection.execute("SELECT observation_id, observed_at FROM market_observations").fetchall():
        docs[f"market:{row['observation_id']}"] = {
            "doc_type": "market_observation",
            "visible_at": str(row["observed_at"] or ""),
        }
    for row in connection.execute("SELECT observation_id, observed_at FROM industry_observations").fetchall():
        docs[f"industry:{row['observation_id']}"] = {
            "doc_type": "industry_observation",
            "visible_at": str(row["observed_at"] or ""),
        }
    for row in connection.execute("SELECT prediction_id, created_at FROM prediction_ledger").fetchall():
        docs[f"prediction:{row['prediction_id']}"] = {
            "doc_type": "prediction_record",
            "visible_at": str(row["created_at"] or ""),
        }
    return docs


def find_posterior_keys(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            if key_text in POSTERIOR_KEYS:
                found.add(key_text)
            found.update(find_posterior_keys(child))
    elif isinstance(value, list):
        for item in value:
            found.update(find_posterior_keys(item))
    return found


def contains_posterior_term(value: str) -> bool:
    text = value.lower()
    return any(term.lower() in text for term in POSTERIOR_TERMS)


def sentence_count(text: str) -> int:
    return len(split_fact_sentences(text))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    report = audit_temporal_rag(args.db, start=start, end=end)
    output = args.output_dir / f"eval-v2-rag-leak-audit-{args.start}-to-{args.end}.json"
    if not args.dry_run:
        write_json(output, report)
    print(
        {
            "future_leak_count_entering_backtest": report["metrics"]["future_leak_count_entering_backtest"],
            "citation_coverage_rate": report["metrics"]["citation_coverage_rate"],
            "output": str(output),
            "dry_run": args.dry_run,
        }
    )
    return 0 if report["metrics"]["future_leak_count_entering_backtest"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
REPORT_DIR = SERVER_ROOT / "data" / "backfill_reports"
MIN_BODY_CHARS = 200
MAX_SAMPLES = 40
MAX_FACT_SENTENCE_SAMPLES = 40


@dataclass(frozen=True)
class EvidenceDoc:
    doc_id: str
    doc_type: str
    source_id: str
    title: str
    observed_at: str
    url: str = ""
    content_length: int = 0
    title_only_evidence: bool = False


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_doc_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if value in {None, ""}:
        return []
    text = str(value).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return [item.strip() for item in text.split(",") if item.strip()]
    if isinstance(parsed, list):
        return [str(item) for item in parsed if str(item).strip()]
    if isinstance(parsed, str):
        return [parsed] if parsed.strip() else []
    return []


def parse_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, dict | list):
        return value
    if value in {None, ""}:
        return fallback
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return fallback


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
            cited_doc_ids = parse_doc_ids(entry.get("cited_doc_ids"))
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
    parts = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?；;])\s*|\n+", str(text or "").replace("；", "。"))
        if part.strip()
    ]
    return [part for part in parts if len(part) >= 8]


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text[:10])
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def latest_visibility_time(*values: Any) -> str:
    parsed = [item for item in (parse_datetime(value) for value in values) if item is not None]
    return max(parsed).isoformat() if parsed else ""


def first_visibility_time(*values: Any) -> str:
    for value in values:
        parsed = parse_datetime(value)
        if parsed is not None:
            return parsed.isoformat()
    return ""


def parse_as_of_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    parsed = parse_datetime(text)
    if parsed is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed


def compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).lower()


def is_title_only_evidence(title: str, raw_text: str, *, min_body_chars: int = MIN_BODY_CHARS) -> bool:
    body = compact_text(raw_text)
    headline = compact_text(title)
    if not body:
        return True
    if len(body) < min_body_chars:
        return True
    if not headline:
        return False
    if body == headline:
        return True
    if len(body) > max(min_body_chars, int(len(headline) * 1.5) + 40):
        return False
    return headline in body or body in headline


def load_doc_index(connection: sqlite3.Connection, *, min_body_chars: int = MIN_BODY_CHARS) -> dict[str, EvidenceDoc]:
    docs: dict[str, EvidenceDoc] = {}
    article_dates: dict[str, str] = {}
    for row in connection.execute("""
        SELECT article_id, source_id, tier, url, title, published_at, first_seen_at, raw_text
        FROM news_articles
        """).fetchall():
        observed_at = first_visibility_time(row["published_at"], row["first_seen_at"])
        article_id = str(row["article_id"])
        article_dates[article_id] = observed_at
        raw_text = str(row["raw_text"] or "")
        docs[f"news_article:{article_id}"] = EvidenceDoc(
            doc_id=f"news_article:{article_id}",
            doc_type="news_article",
            source_id=str(row["source_id"] or ""),
            title=str(row["title"] or ""),
            observed_at=observed_at,
            url=str(row["url"] or ""),
            content_length=len(compact_text(raw_text)),
            title_only_evidence=is_title_only_evidence(
                str(row["title"] or ""),
                raw_text,
                min_body_chars=min_body_chars,
            ),
        )

    for row in connection.execute("""
        SELECT event_record_id, source_id, occurred_at, created_at, title, evidence_url
        FROM event_observations
        """).fetchall():
        event_id = str(row["event_record_id"])
        doc = EvidenceDoc(
            doc_id=f"event:{event_id}",
            doc_type="event_observation",
            source_id=str(row["source_id"] or ""),
            title=str(row["title"] or ""),
            observed_at=first_visibility_time(row["occurred_at"], row["created_at"]),
            url=str(row["evidence_url"] or ""),
        )
        docs[doc.doc_id] = doc
        docs[f"event_candidate:{event_id}"] = doc

    for row in connection.execute("""
        SELECT cluster_id, updated_at, created_at, title, source_ids, article_ids
        FROM news_event_clusters
        """).fetchall():
        cluster_id = str(row["cluster_id"])
        article_ids = [str(item) for item in parse_json(row["article_ids"], [])]
        dates = [article_dates[article_id] for article_id in article_ids if article_dates.get(article_id)]
        observed_at = first_visibility_time(*dates, row["created_at"], row["updated_at"])
        source_ids = [str(item) for item in parse_json(row["source_ids"], [])]
        doc = EvidenceDoc(
            doc_id=f"news_event:{cluster_id}",
            doc_type="news_event_cluster",
            source_id=",".join(source_ids) or "news_cluster",
            title=str(row["title"] or ""),
            observed_at=observed_at,
        )
        docs[doc.doc_id] = doc
        docs[f"event_candidate:{cluster_id}"] = doc

    for row in connection.execute("""
        SELECT observation_id, source_id, observed_at, created_at, product, indicator, evidence_url
        FROM market_observations
        """).fetchall():
        observation_id = str(row["observation_id"])
        docs[f"market:{observation_id}"] = EvidenceDoc(
            doc_id=f"market:{observation_id}",
            doc_type="market_observation",
            source_id=str(row["source_id"] or ""),
            title=f"{row['product']} {row['indicator']}",
            observed_at=first_visibility_time(row["observed_at"], row["created_at"]),
            url=str(row["evidence_url"] or ""),
        )

    for row in connection.execute("""
        SELECT observation_id, source_id, observed_at, created_at, product, metric, evidence_url
        FROM industry_observations
        """).fetchall():
        observation_id = str(row["observation_id"])
        docs[f"industry:{observation_id}"] = EvidenceDoc(
            doc_id=f"industry:{observation_id}",
            doc_type="industry_observation",
            source_id=str(row["source_id"] or ""),
            title=f"{row['product']} {row['metric']}",
            observed_at=first_visibility_time(row["observed_at"], row["created_at"]),
            url=str(row["evidence_url"] or ""),
        )

    for row in connection.execute("""
        SELECT prediction_id, created_at, target, horizon
        FROM prediction_ledger
        """).fetchall():
        prediction_id = str(row["prediction_id"])
        docs[f"prediction:{prediction_id}"] = EvidenceDoc(
            doc_id=f"prediction:{prediction_id}",
            doc_type="prediction_record",
            source_id="prediction_ledger",
            title=f"{row['target']} {row['horizon']}",
            observed_at=str(row["created_at"] or ""),
        )

    return docs


def audit_citation_coverage(
    db_path: str | Path,
    *,
    start: str | None = None,
    end: str | None = None,
    min_body_chars: int = MIN_BODY_CHARS,
) -> dict[str, Any]:
    start_dt = parse_datetime(start) if start else None
    end_dt = parse_datetime(end) if end else None
    with closing(sqlite3.connect(db_path, timeout=30)) as connection, connection:
        connection.row_factory = sqlite3.Row
        docs = load_doc_index(connection, min_body_chars=min_body_chars)
        rows = connection.execute("""
            SELECT judgment_id, event_id, as_of_time, title, reasoning, cited_doc_ids, fallback,
                   should_enter_backtest, raw
            FROM llm_event_directions
            ORDER BY as_of_time, event_id
            """).fetchall()

    judgments: list[dict[str, Any]] = []
    future_leaks: list[dict[str, Any]] = []
    future_leaks_entering_backtest: list[dict[str, Any]] = []
    title_only_evidence: list[dict[str, Any]] = []
    title_only_entering_backtest: list[dict[str, Any]] = []
    unknown_citations: list[dict[str, Any]] = []
    total_docs = 0
    backtest_docs = 0
    judgments_with_citations = 0
    backtest_with_citations = 0
    fallback_count = 0
    backtest_count = 0
    fact_sentence_total = 0
    fact_sentence_covered = 0
    missing_fact_sentence_citations: list[dict[str, Any]] = []

    for row in rows:
        as_of = parse_as_of_datetime(row["as_of_time"])
        if start_dt and as_of and as_of < start_dt:
            continue
        if end_dt and as_of and as_of > end_dt:
            continue
        doc_ids = parse_doc_ids(row["cited_doc_ids"])
        enters_backtest = bool(row["should_enter_backtest"])
        total_docs += len(doc_ids)
        fallback_count += int(bool(row["fallback"]))
        backtest_count += int(enters_backtest)
        if enters_backtest:
            backtest_docs += len(doc_ids)
        if doc_ids:
            judgments_with_citations += 1
            if enters_backtest:
                backtest_with_citations += 1
        judgment_summary = {
            "judgment_id": str(row["judgment_id"]),
            "event_id": str(row["event_id"]),
            "as_of_time": str(row["as_of_time"]),
            "title": str(row["title"] or ""),
            "doc_count": len(doc_ids),
            "cited_doc_ids": doc_ids,
            "should_enter_backtest": enters_backtest,
        }
        judgments.append(judgment_summary)
        fact_sentence_entries = parse_fact_sentence_citations(row["raw"], str(row["reasoning"] or ""))
        fact_sentence_total += len(fact_sentence_entries)
        for entry in fact_sentence_entries:
            cited_for_sentence = [doc_id for doc_id in entry["cited_doc_ids"] if doc_id in doc_ids]
            if cited_for_sentence:
                fact_sentence_covered += 1
                continue
            missing_fact_sentence_citations.append(
                {
                    **judgment_summary,
                    "sentence": entry["sentence"],
                    "sentence_cited_doc_ids": entry["cited_doc_ids"],
                }
            )
        for doc_id in doc_ids:
            doc = docs.get(doc_id)
            if doc is None and is_always_visible_doc(doc_id):
                continue
            if doc is None:
                unknown_citations.append({**judgment_summary, "doc_id": doc_id})
                continue
            observed_at = parse_datetime(doc.observed_at)
            if is_future_leak(doc_id=doc_id, as_of=as_of, observed_at=observed_at):
                item = {
                    **judgment_summary,
                    "doc_id": doc_id,
                    "doc_type": doc.doc_type,
                    "doc_observed_at": doc.observed_at,
                    "doc_title": doc.title,
                }
                future_leaks.append(item)
                if enters_backtest:
                    future_leaks_entering_backtest.append(item)
            if doc.title_only_evidence:
                item = {
                    **judgment_summary,
                    "doc_id": doc_id,
                    "doc_type": doc.doc_type,
                    "content_length": doc.content_length,
                    "doc_title": doc.title,
                }
                title_only_evidence.append(item)
                if enters_backtest:
                    title_only_entering_backtest.append(item)

    total = len(judgments)
    metrics = {
        "total_llm_judgments": total,
        "judgments_with_citations": judgments_with_citations,
        "citation_coverage_rate": round(judgments_with_citations / total, 3) if total else 1.0,
        "avg_docs_per_judgment": round(total_docs / total, 3) if total else 0.0,
        "future_leak_count": len(future_leaks),
        "future_leak_count_entering_backtest": len(future_leaks_entering_backtest),
        "title_only_evidence_count": len(title_only_evidence),
        "title_only_evidence_count_entering_backtest": len(title_only_entering_backtest),
        "fallback_judgments": fallback_count,
        "should_enter_backtest_count": backtest_count,
        "judgments_with_citations_entering_backtest": backtest_with_citations,
        "citation_coverage_rate_entering_backtest": (
            round(backtest_with_citations / backtest_count, 3) if backtest_count else 1.0
        ),
        "avg_docs_per_backtest_judgment": round(backtest_docs / backtest_count, 3) if backtest_count else 0.0,
        "unknown_citation_count": len(unknown_citations),
        "fact_sentence_citations": fact_sentence_total,
        "fact_sentence_citations_covered": fact_sentence_covered,
        "fact_sentence_citation_coverage_rate": (
            round(fact_sentence_covered / fact_sentence_total, 3) if fact_sentence_total else 1.0
        ),
        "missing_fact_sentence_citation_count": len(missing_fact_sentence_citations),
    }
    return {
        "generated_at": now_iso(),
        "scope": {
            "db_path": str(db_path),
            "start": start,
            "end": end,
            "min_body_chars": min_body_chars,
        },
        "metrics": metrics,
        "guardrails": {
            "future_leak_count_must_be_zero": metrics["future_leak_count"] == 0,
            "future_leak_count_entering_backtest_must_be_zero": metrics["future_leak_count_entering_backtest"] == 0,
            "citation_required_for_llm_judgments": (
                metrics["judgments_with_citations"] == metrics["total_llm_judgments"]
            ),
            "citation_required_for_backtest_judgments": (
                metrics["judgments_with_citations_entering_backtest"] == metrics["should_enter_backtest_count"]
            ),
            "fact_sentence_citations_required": metrics["missing_fact_sentence_citation_count"] == 0,
        },
        "samples": {
            "missing_citation_judgments": [item for item in judgments if item["doc_count"] == 0][:MAX_SAMPLES],
            "future_leaks": future_leaks[:MAX_SAMPLES],
            "future_leaks_entering_backtest": future_leaks_entering_backtest[:MAX_SAMPLES],
            "title_only_evidence": title_only_evidence[:MAX_SAMPLES],
            "title_only_evidence_entering_backtest": title_only_entering_backtest[:MAX_SAMPLES],
            "unknown_citations": unknown_citations[:MAX_SAMPLES],
            "missing_fact_sentence_citations": missing_fact_sentence_citations[:MAX_FACT_SENTENCE_SAMPLES],
        },
    }


def is_always_visible_doc(doc_id: str) -> bool:
    return doc_id.startswith(("kg:", "source:", "news_source:"))


def is_future_leak(*, doc_id: str, as_of: datetime | None, observed_at: datetime | None) -> bool:
    if as_of is None or observed_at is None:
        return False
    return observed_at > as_of


def write_report(report: dict[str, Any], output: Path | None) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = output
    if output_path is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_path = REPORT_DIR / f"rag-citation-coverage-{stamp}.json"
    if not output_path.is_absolute():
        output_path = REPO_ROOT / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = REPORT_DIR / "rag-citation-coverage-latest.json"
    latest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit RAG citation coverage and as-of safety for cached LLM event direction judgments."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite DB path")
    parser.add_argument("--output", type=Path, default=None, help="JSON report path")
    parser.add_argument("--start", default=None, help="Optional inclusive as_of_time lower bound")
    parser.add_argument("--end", default=None, help="Optional inclusive as_of_time upper bound")
    parser.add_argument("--min-body-chars", type=int, default=MIN_BODY_CHARS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = audit_citation_coverage(
        args.db,
        start=args.start,
        end=args.end,
        min_body_chars=args.min_body_chars,
    )
    output_path = write_report(report, args.output)
    print(
        json.dumps(
            {
                "output_path": str(output_path),
                "metrics": report["metrics"],
                "guardrails": report["guardrails"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report["metrics"]["future_leak_count"] == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

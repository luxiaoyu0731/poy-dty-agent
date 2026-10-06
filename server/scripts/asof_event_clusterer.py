from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import event_candidate_generator  # noqa: E402

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_OUTPUT_DIR = Path("server/data/backfill_reports")
DEFAULT_START = date(2025, 6, 16)
DEFAULT_END = date(2026, 6, 15)
DEFAULT_WINDOW = "half_month"

EVIDENCE_ORDER = {"A": 4, "B": 3, "C": 2, "D": 1}
STOPWORDS = {
    "and",
    "for",
    "from",
    "into",
    "the",
    "with",
    "after",
    "before",
    "signal",
    "observation",
    "price",
    "move",
}

TOPIC_KEYWORDS = {
    "sanctions": ("ofac", "sanction", "treasury"),
    "shipping": ("shipping", "tanker", "maritime", "vessel", "hormuz", "red sea"),
    "oil_policy": ("opec", "eia", "iea", "quota", "production", "output"),
    "macro_finance": ("dollar", "treasury", "yield", "fed", "rate", "inflation", "macro"),
    "polyester_chain": ("poy", "dty", "pta", "px", "meg", "polyester"),
    "capacity": ("outage", "restart", "maintenance", "capacity", "operating"),
}


def build_asof_event_clusters(
    db_path: str | Path,
    *,
    start: date = DEFAULT_START,
    end: date = DEFAULT_END,
    window: str = DEFAULT_WINDOW,
    limit: int | None = None,
    candidates_path: str | Path | None = None,
) -> dict[str, Any]:
    if candidates_path:
        candidates = load_candidates_from_json(candidates_path)
        candidate_source = str(candidates_path)
    else:
        candidate_report = event_candidate_generator.generate_event_candidates(
            db_path,
            start=start,
            end=end,
            window=window,
            include_preclustered_news=False,
        )
        candidates = list(candidate_report["candidates"])
        candidate_source = "event_candidate_generator(raw_inputs_only)"
    return build_cluster_report_from_candidates(
        candidates,
        start=start,
        end=end,
        window=window,
        limit=limit,
        candidate_source=candidate_source,
    )


def build_cluster_report_from_candidates(
    candidates: list[dict[str, Any]],
    *,
    start: date,
    end: date,
    window: str,
    limit: int | None = None,
    candidate_source: str = "provided_candidates",
) -> dict[str, Any]:
    usable_candidates, excluded_precomputed = reject_precomputed_year_clusters(candidates)
    records = [
        {"candidate": item, "as_of_time": parsed}
        for item in usable_candidates
        if (parsed := event_candidate_generator.parse_datetime(item.get("as_of_time"))) is not None
    ]
    windows = event_candidate_generator.build_windows(start, end, window)
    window_reports = []
    for item in windows:
        as_of_time = end_of_day(item.end)
        window_records = [
            record
            for record in records
            if item.start <= record["as_of_time"].date() <= item.end and record["as_of_time"] <= as_of_time
        ]
        window_records = sorted(
            window_records,
            key=lambda record: (
                -float(record["candidate"].get("heat_score") or 0),
                str(record["candidate"].get("as_of_time") or ""),
                str(record["candidate"].get("candidate_id") or ""),
            ),
        )
        if limit is not None:
            window_records = window_records[: max(limit, 0)]
        visible_candidates = [record["candidate"] for record in window_records]
        included_future_leak_count = sum(1 for record in window_records if record["as_of_time"] > as_of_time)
        future_candidates_after_as_of = sum(1 for record in records if record["as_of_time"] > as_of_time)
        clusters = cluster_visible_candidates(visible_candidates, as_of_time=as_of_time)
        window_reports.append(
            {
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "as_of_time": as_of_time.isoformat(),
                "candidate_count": len(visible_candidates),
                "cluster_count": len(clusters),
                "future_leak_count": included_future_leak_count,
                "future_candidates_after_as_of": future_candidates_after_as_of,
                "clusters": clusters,
            }
        )

    all_clusters = [cluster for item in window_reports for cluster in item["clusters"]]
    return {
        "scope": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "window": window,
            "limit": limit,
            "candidate_source": candidate_source,
        },
        "guardrails": {
            "uses_precomputed_year_clusters": False,
            "excluded_precomputed_news_cluster_candidates": excluded_precomputed,
            "provider_calls": 0,
            "remote_fetches": 0,
            "cluster_policy": "cluster each window from candidates visible by that window as_of_time",
        },
        "summary": {
            "window_count": len(window_reports),
            "cluster_count": len(all_clusters),
            "candidate_count": sum(item["candidate_count"] for item in window_reports),
            "total_window_future_leak_count": sum(item["future_leak_count"] for item in window_reports),
            "max_window_future_leak_count": max((item["future_leak_count"] for item in window_reports), default=0),
            "future_candidates_after_as_of": sum(item["future_candidates_after_as_of"] for item in window_reports),
            "clusters_by_category": dict(Counter(cluster["category"] for cluster in all_clusters)),
        },
        "windows": window_reports,
    }


def cluster_visible_candidates(candidates: list[dict[str, Any]], *, as_of_time: datetime) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        grouped[cluster_key(candidate)].append(candidate)

    clusters = []
    for (category, topic), items in sorted(grouped.items()):
        ordered = sorted(
            items,
            key=lambda item: (-float(item.get("heat_score") or 0), str(item.get("candidate_id") or "")),
        )
        representative = ordered[0]
        candidate_ids = [str(item.get("candidate_id")) for item in ordered]
        cluster_hash = hashlib.sha1("|".join(sorted(candidate_ids)).encode("utf-8")).hexdigest()[:12]
        times = [
            parsed
            for item in ordered
            if (parsed := event_candidate_generator.parse_datetime(item.get("as_of_time"))) is not None
        ]
        heat_scores = [float(item.get("heat_score") or 0) for item in ordered]
        cluster_id = f"asof_cluster_{as_of_time.strftime('%Y%m%d')}_{safe_id(category)}_{safe_id(topic)}_{cluster_hash}"
        clusters.append(
            {
                "cluster_id": cluster_id,
                "as_of_time": as_of_time.isoformat(),
                "category": category,
                "topic": topic,
                "title": representative.get("title") or candidate_ids[0],
                "summary": representative.get("summary") or "",
                "candidate_ids": candidate_ids,
                "candidate_types": sorted({str(item.get("candidate_type") or "") for item in ordered}),
                "source_kinds": sorted({str(item.get("source_kind") or "") for item in ordered}),
                "source_record_ids": sorted({str(item.get("source_record_id") or "") for item in ordered}),
                "source_ids": sorted(
                    {
                        str(source_id)
                        for item in ordered
                        for source_id in item.get("source_ids", [])
                        if str(source_id).strip()
                    }
                ),
                "cited_doc_ids": sorted(
                    {str(doc_id) for item in ordered for doc_id in item.get("cited_doc_ids", []) if str(doc_id).strip()}
                ),
                "affected_products": sorted(
                    {
                        str(product)
                        for item in ordered
                        for product in item.get("affected_products", [])
                        if str(product).strip()
                    }
                ),
                "evidence_level": strongest_evidence_level(str(item.get("evidence_level") or "D") for item in ordered),
                "first_seen_at": min(times).isoformat() if times else None,
                "last_seen_at": max(times).isoformat() if times else None,
                "heat_score": round(max(heat_scores) if heat_scores else 0.0, 4),
                "average_heat_score": round(sum(heat_scores) / len(heat_scores), 4) if heat_scores else 0.0,
                "requires_human_review": any(bool(item.get("requires_human_review")) for item in ordered),
            }
        )
    return sorted(clusters, key=lambda item: (-float(item["heat_score"]), item["cluster_id"]))


def reject_precomputed_year_clusters(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    usable = []
    excluded = 0
    for item in candidates:
        if str(item.get("source_kind") or "") == "news_event_cluster":
            excluded += 1
            continue
        usable.append(item)
    return usable, excluded


def cluster_key(candidate: dict[str, Any]) -> tuple[str, str]:
    category = str(candidate.get("category") or "general")
    text = " ".join(
        [
            str(candidate.get("title") or ""),
            str(candidate.get("summary") or ""),
            " ".join(str(item) for item in candidate.get("affected_products", [])),
        ]
    ).lower()
    for topic, keywords in TOPIC_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category, topic
    tokens = [token for token in re.findall(r"[a-zA-Z0-9]+", text) if len(token) > 2 and token not in STOPWORDS]
    return category, "_".join(tokens[:3]) if tokens else "general"


def strongest_evidence_level(values: Any) -> str:
    levels = [str(value) for value in values if str(value)]
    if not levels:
        return "D"
    return max(levels, key=lambda value: EVIDENCE_ORDER.get(value, 0))


def load_candidates_from_json(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("candidates", "items", "events"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def end_of_day(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=UTC)


def safe_id(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value).strip()).strip("_").lower()
    return text[:48] or "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cluster event candidates as-of each local window.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=DEFAULT_END.isoformat())
    parser.add_argument("--window", default=DEFAULT_WINDOW)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--candidates", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
        report = build_asof_event_clusters(
            args.db,
            start=start,
            end=end,
            window=args.window,
            limit=args.limit,
            candidates_path=args.candidates,
        )
        output = args.output_dir / f"asof-event-clusters-{start.isoformat()}-to-{end.isoformat()}.json"
        summary = {
            "dry_run": args.dry_run,
            "output": str(output),
            "cluster_count": report["summary"]["cluster_count"],
            "candidate_count": report["summary"]["candidate_count"],
            "excluded_precomputed_news_cluster_candidates": report["guardrails"][
                "excluded_precomputed_news_cluster_candidates"
            ],
            "uses_precomputed_year_clusters": report["guardrails"]["uses_precomputed_year_clusters"],
        }
        if args.dry_run:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        args.output_dir.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, sqlite3.Error, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

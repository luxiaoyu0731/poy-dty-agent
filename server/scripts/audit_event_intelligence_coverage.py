from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SERVER_ROOT.parent
DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT = PROJECT_ROOT / ".codex-run" / "event-intelligence-coverage-latest.json"
OIL_GEOPOLITICS_CATEGORIES = {"oil_policy", "sanctions_geopolitics", "shipping_security"}
CHAIN_CATEGORIES = {"company_capacity", "market_signal", "macro_finance", "china_policy"}


def parse_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, dict | list):
        return value
    if value in {None, ""}:
        return fallback
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return fallback


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def load_events(connection: sqlite3.Connection, *, start: str | None, end: str | None) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if table_exists(connection, "event_observations"):
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append("occurred_at >= ?")
            params.append(start)
        if end:
            clauses.append("occurred_at <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"""
            SELECT event_record_id AS event_id, occurred_at AS as_of_time, event_type AS category,
                   title, source_id, evidence_level, affected_products, direction
            FROM event_observations
            {where}
            """,
            params,
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["record_type"] = "event_observation"
            item["affected_products"] = parse_json(item.get("affected_products"), [])
            events.append(item)
    if table_exists(connection, "news_event_clusters"):
        clauses = []
        params = []
        if start:
            clauses.append("COALESCE(updated_at, created_at) >= ?")
            params.append(start)
        if end:
            clauses.append("COALESCE(updated_at, created_at) <= ?")
            params.append(end)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = connection.execute(
            f"""
            SELECT cluster_id AS event_id, COALESCE(updated_at, created_at) AS as_of_time, category,
                   title, source_ids AS source_id, evidence_level, affected_products, direction
            FROM news_event_clusters
            {where}
            """,
            params,
        ).fetchall()
        for row in rows:
            item = dict(row)
            item["record_type"] = "news_event_cluster"
            item["affected_products"] = parse_json(item.get("affected_products"), [])
            source_ids = parse_json(item.get("source_id"), [])
            item["source_id"] = (
                ",".join(source_ids) if isinstance(source_ids, list) else str(item.get("source_id") or "")
            )
            events.append(item)
    return events


def load_snapshots(connection: sqlite3.Connection, *, start: str | None, end: str | None) -> list[dict[str, Any]]:
    if not table_exists(connection, "event_intelligence_snapshots"):
        return []
    clauses: list[str] = []
    params: list[Any] = []
    if start:
        clauses.append("as_of_time >= ?")
        params.append(start)
    if end:
        clauses.append("as_of_time <= ?")
        params.append(end)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = connection.execute(f"SELECT * FROM event_intelligence_snapshots {where}", params).fetchall()
    snapshots: list[dict[str, Any]] = []
    json_fields = {
        "facts",
        "inferences",
        "hypotheses",
        "key_actors",
        "stakeholders",
        "beneficiaries",
        "losers",
        "likely_motives",
        "hidden_implications",
        "supply_chain_paths",
        "affected_products",
        "expected_direction_by_product",
        "horizon_impact",
        "evidence_quality",
        "speculation_flags",
        "disconfirming_signals",
        "cited_doc_ids",
        "raw",
    }
    for row in rows:
        item = dict(row)
        for field in json_fields:
            item[field] = parse_json(item.get(field), [] if field != "raw" else {})
        item["should_enter_backtest"] = bool(item.get("should_enter_backtest"))
        snapshots.append(item)
    return snapshots


def audit(db_path: Path, *, start: str | None, end: str | None, include_raw: bool) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        events = load_events(connection, start=start, end=end)
        snapshots = load_snapshots(connection, start=start, end=end)
    snapshots_by_event = {(row["event_id"], row["as_of_time"]): row for row in snapshots}
    event_ids_with_snapshot = {row["event_id"] for row in snapshots}
    by_category = Counter(str(row.get("category") or "general") for row in events)
    snapshot_by_category = Counter(str(row.get("category") or "general") for row in snapshots)
    c_d_sources = [
        row for row in snapshots if str((row.get("evidence_quality") or {}).get("tier", "C")).upper() in {"C", "D"}
    ]
    high_conf_c_d = [
        row
        for row in c_d_sources
        if float((row.get("evidence_quality") or {}).get("confidence") or 0) > 0.55 and row.get("should_enter_backtest")
    ]
    coverage_fields = {
        "actors": "key_actors",
        "stakeholders": "stakeholders",
        "motives": "likely_motives",
        "supply_chain_paths": "supply_chain_paths",
        "disconfirming_signals": "disconfirming_signals",
    }
    coverage = {name: coverage_rate(snapshots, field) for name, field in coverage_fields.items()}
    poy_dty_events = [row for row in events if has_any(row.get("affected_products"), {"POY", "DTY"})]
    poy_dty_snapshots = [row for row in snapshots if has_any(row.get("affected_products"), {"POY", "DTY"})]
    low_evidence = [
        row
        for row in snapshots
        if str((row.get("evidence_quality") or {}).get("tier", "C")).upper() in {"C", "D"}
        or "poy_dty_transmission_unstable" in row.get("speculation_flags", [])
    ]
    mismatch_risk = [row for row in snapshots if target_mismatch_risk(row)]
    oil_events = sum(count for category, count in by_category.items() if category in OIL_GEOPOLITICS_CATEGORIES)
    chain_events = sum(count for category, count in by_category.items() if category in CHAIN_CATEGORIES)
    report = {
        "generated_at": date.today().isoformat(),
        "db": str(db_path),
        "scope": {"start": start, "end": end},
        "read_only": True,
        "total_events": len(events),
        "snapshot_count": len(snapshots),
        "snapshot_event_id_coverage": ratio(len(event_ids_with_snapshot), len({row["event_id"] for row in events})),
        "snapshot_exact_event_time_coverage": ratio(len(snapshots_by_event), len(events)),
        "field_coverage": coverage,
        "source_quality": {
            "c_d_snapshot_count": len(c_d_sources),
            "c_d_snapshot_share": ratio(len(c_d_sources), len(snapshots)),
            "c_d_single_source_high_confidence_count": len(high_conf_c_d),
        },
        "by_category": category_rows(by_category, snapshot_by_category),
        "poy_dty": {
            "target_event_count": len(poy_dty_events),
            "snapshot_count": len(poy_dty_snapshots),
            "coverage": ratio(
                len({row["event_id"] for row in poy_dty_snapshots}), len({row["event_id"] for row in poy_dty_events})
            ),
        },
        "risk_samples": {
            "low_evidence_count": len(low_evidence),
            "target_mismatch_risk_count": len(mismatch_risk),
            "oil_geopolitics_event_share": ratio(oil_events, len(events)),
            "industrial_chain_event_share": ratio(chain_events, len(events)),
        },
    }
    if include_raw:
        report["raw_samples"] = {
            "low_evidence": compact_samples(low_evidence),
            "target_mismatch_risk": compact_samples(mismatch_risk),
        }
    return report


def coverage_rate(rows: list[dict[str, Any]], field: str) -> dict[str, Any]:
    covered = sum(1 for row in rows if bool(row.get(field)))
    return {"covered": covered, "total": len(rows), "rate": ratio(covered, len(rows))}


def category_rows(events: Counter[str], snapshots: Counter[str]) -> list[dict[str, Any]]:
    rows = []
    for category in sorted(set(events) | set(snapshots)):
        rows.append(
            {
                "category": category,
                "events": events.get(category, 0),
                "snapshots": snapshots.get(category, 0),
                "coverage_proxy": ratio(snapshots.get(category, 0), events.get(category, 0)),
            }
        )
    return rows


def target_mismatch_risk(row: dict[str, Any]) -> bool:
    products = set(str(item) for item in row.get("affected_products", []))
    directions = row.get("expected_direction_by_product")
    if not isinstance(directions, dict):
        return False
    directed_products = {
        str(product)
        for product, direction in directions.items()
        if str(direction) not in {"", "中性", "neutral", "None"}
    }
    return bool(products and directed_products and products.isdisjoint(directed_products))


def has_any(value: object, targets: set[str]) -> bool:
    if not isinstance(value, list):
        return False
    return bool({str(item) for item in value} & targets)


def compact_samples(rows: list[dict[str, Any]], *, limit: int = 10) -> list[dict[str, Any]]:
    return [
        {
            "snapshot_id": row.get("snapshot_id"),
            "event_id": row.get("event_id"),
            "as_of_time": row.get("as_of_time"),
            "category": row.get("category"),
            "title": row.get("title"),
            "evidence_quality": row.get("evidence_quality"),
            "speculation_flags": row.get("speculation_flags"),
        }
        for row in rows[:limit]
    ]


def ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit event intelligence snapshot coverage.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--include-raw", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = audit(args.db, start=args.start, end=args.end, include_raw=args.include_raw)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "total_events": report["total_events"],
                "snapshot_count": report["snapshot_count"],
                "snapshot_event_id_coverage": report["snapshot_event_id_coverage"],
                "read_only": True,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

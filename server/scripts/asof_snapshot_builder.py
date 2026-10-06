from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_OUTPUT_DIR = Path("server/data/backfill_reports")
DEFAULT_START = date(2025, 6, 16)
DEFAULT_END = date(2026, 6, 15)
DEFAULT_WINDOW = "half_month"

TEMPORAL_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("market_observations", ("observed_at",)),
    ("industry_observations", ("observed_at",)),
    ("intraday_price_observations", ("observed_at",)),
    ("event_observations", ("occurred_at",)),
    ("news_articles", ("published_at", "first_seen_at", "created_at")),
    ("llm_event_directions", ("as_of_time", "created_at")),
)

JSON_COLUMNS = {
    "affected_products",
    "article_ids",
    "cited_doc_ids",
    "industry_observation_ids",
    "market_observation_ids",
    "event_record_ids",
    "raw",
    "source_ids",
    "tags",
}

BOOL_COLUMNS = {
    "fallback",
    "requires_human_review",
    "risk_premium_decay",
    "demand_weakness_offset",
    "supply_recovery_offset",
    "should_enter_backtest",
}


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date


def build_asof_snapshot(
    db_path: str | Path,
    *,
    as_of_time: datetime,
    limit: int | None = None,
) -> dict[str, Any]:
    as_of = normalize_datetime(as_of_time)
    payload: dict[str, list[dict[str, Any]]] = {}
    future_leak_by_table: dict[str, int] = {}
    excluded_future_by_table: dict[str, int] = {}
    missing_time_by_table: dict[str, int] = {}
    trimmed_future_cluster_article_refs = 0

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        for table_name, temporal_columns in TEMPORAL_TABLES:
            rows, future_count, missing_count = select_asof_rows(
                connection,
                table_name=table_name,
                temporal_columns=temporal_columns,
                as_of_time=as_of,
                limit=limit,
            )
            payload[table_name] = rows
            future_leak_by_table[table_name] = validate_no_future_rows(rows, as_of)
            excluded_future_by_table[table_name] = future_count
            missing_time_by_table[table_name] = missing_count

        cluster_rows, cluster_future_count, cluster_missing_count, cluster_trimmed_future_refs = (
            select_asof_news_event_clusters(
                connection,
                as_of_time=as_of,
                limit=limit,
            )
        )
        payload["news_event_clusters"] = cluster_rows
        future_leak_by_table["news_event_clusters"] = validate_no_future_rows(cluster_rows, as_of)
        excluded_future_by_table["news_event_clusters"] = cluster_future_count
        missing_time_by_table["news_event_clusters"] = cluster_missing_count
        trimmed_future_cluster_article_refs = cluster_trimmed_future_refs

    table_counts = {table: len(rows) for table, rows in payload.items()}
    future_leak_count = sum(future_leak_by_table.values())
    return {
        "snapshot_id": f"asof_{as_of.strftime('%Y%m%dT%H%M%SZ')}",
        "as_of_time": as_of.isoformat(),
        "future_leak_count": future_leak_count,
        "future_leak_by_table": future_leak_by_table,
        "excluded_future_rows_by_table": excluded_future_by_table,
        "trimmed_future_cluster_article_refs": trimmed_future_cluster_article_refs,
        "missing_time_by_table": missing_time_by_table,
        "table_counts": table_counts,
        "source_ids": source_ids_from_payload(payload),
        "payload": payload,
        "guardrails": {
            "as_of_filter": "all included temporal rows have observed/published/as_of time <= as_of_time",
            "news_cluster_policy": "cluster article_ids are trimmed to article timestamps <= as_of_time",
            "provider_calls": 0,
            "remote_fetches": 0,
        },
    }


def build_asof_snapshots(
    db_path: str | Path,
    *,
    start: date = DEFAULT_START,
    end: date = DEFAULT_END,
    window: str = DEFAULT_WINDOW,
    limit: int | None = None,
) -> dict[str, Any]:
    windows = build_windows(start, end, window)
    snapshots = [
        build_asof_snapshot(db_path, as_of_time=end_of_day(item.end), limit=limit)
        | {"window_start": item.start.isoformat(), "window_end": item.end.isoformat()}
        for item in windows
    ]
    return {
        "scope": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "window": window,
            "limit": limit,
        },
        "summary": {
            "snapshot_count": len(snapshots),
            "future_leak_count": sum(item["future_leak_count"] for item in snapshots),
            "future_leak_by_table": sum_counter(item["future_leak_by_table"] for item in snapshots),
            "excluded_future_rows_by_table": sum_counter(item["excluded_future_rows_by_table"] for item in snapshots),
            "trimmed_future_cluster_article_refs": sum(
                item["trimmed_future_cluster_article_refs"] for item in snapshots
            ),
        },
        "snapshots": snapshots,
    }


def select_asof_rows(
    connection: sqlite3.Connection,
    *,
    table_name: str,
    temporal_columns: tuple[str, ...],
    as_of_time: datetime,
    limit: int | None,
) -> tuple[list[dict[str, Any]], int, int]:
    if not table_exists(connection, table_name):
        return [], 0, 0
    rows = connection.execute(f"SELECT * FROM {table_name}").fetchall()
    included: list[tuple[datetime, dict[str, Any]]] = []
    future_count = 0
    missing_count = 0
    for row in rows:
        row_keys = set(row.keys())
        row_time = first_datetime(*(row[column] for column in temporal_columns if column in row_keys))
        if row_time is None:
            missing_count += 1
            continue
        if row_time > as_of_time:
            future_count += 1
            continue
        item = serialize_row(row)
        item["_asof_observed_at"] = row_time.isoformat()
        included.append((row_time, item))
    ordered = [item for _row_time, item in sorted(included, key=lambda pair: pair[0], reverse=True)]
    if limit is not None:
        ordered = ordered[: max(limit, 0)]
    return ordered, future_count, missing_count


def select_asof_news_event_clusters(
    connection: sqlite3.Connection,
    *,
    as_of_time: datetime,
    limit: int | None,
) -> tuple[list[dict[str, Any]], int, int, int]:
    if not table_exists(connection, "news_event_clusters"):
        return [], 0, 0, 0
    article_times = load_article_times(connection)
    rows = connection.execute("SELECT * FROM news_event_clusters").fetchall()
    included: list[tuple[datetime, dict[str, Any]]] = []
    future_count = 0
    missing_count = 0
    trimmed_future_article_refs = 0
    for row in rows:
        article_ids = [str(item) for item in parse_json_list(row["article_ids"])]
        visible_article_ids = []
        future_article_ids = []
        visible_times = []
        for article_id in article_ids:
            article_time = article_times.get(article_id)
            if article_time is None:
                continue
            if article_time <= as_of_time:
                visible_article_ids.append(article_id)
                visible_times.append(article_time)
            else:
                future_article_ids.append(article_id)
        cluster_time = min(visible_times) if visible_times else first_datetime(row["created_at"], row["updated_at"])
        if cluster_time is None:
            missing_count += 1
            continue
        if cluster_time > as_of_time:
            future_count += 1 + len(future_article_ids)
            continue
        future_count += len(future_article_ids)
        trimmed_future_article_refs += len(future_article_ids)
        item = serialize_row(row)
        if visible_article_ids:
            item["article_ids"] = visible_article_ids
        elif future_article_ids:
            item["article_ids"] = []
        else:
            item["article_ids"] = parse_json_list(row["article_ids"])
        item["_asof_observed_at"] = cluster_time.isoformat()
        item["_asof_trimmed_future_article_ids"] = future_article_ids
        included.append((cluster_time, item))
    ordered = [item for _row_time, item in sorted(included, key=lambda pair: pair[0], reverse=True)]
    if limit is not None:
        ordered = ordered[: max(limit, 0)]
    return ordered, future_count, missing_count, trimmed_future_article_refs


def validate_no_future_rows(rows: list[dict[str, Any]], as_of_time: datetime) -> int:
    leaks = 0
    for row in rows:
        row_time = first_datetime(row.get("_asof_observed_at"))
        if row_time is not None and row_time > as_of_time:
            leaks += 1
    return leaks


def load_article_times(connection: sqlite3.Connection) -> dict[str, datetime]:
    if not table_exists(connection, "news_articles"):
        return {}
    rows = connection.execute(
        "SELECT article_id, published_at, first_seen_at, created_at FROM news_articles"
    ).fetchall()
    result = {}
    for row in rows:
        observed_at = first_datetime(row["published_at"], row["first_seen_at"], row["created_at"])
        if observed_at is not None:
            result[str(row["article_id"])] = observed_at
    return result


def source_ids_from_payload(payload: dict[str, list[dict[str, Any]]]) -> list[str]:
    source_ids: set[str] = set()
    for rows in payload.values():
        for row in rows:
            source_id = row.get("source_id")
            if isinstance(source_id, str) and source_id:
                source_ids.add(source_id)
            for item in row.get("source_ids", []) if isinstance(row.get("source_ids"), list) else []:
                if str(item).strip():
                    source_ids.add(str(item))
    return sorted(source_ids)


def serialize_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    for key, value in list(item.items()):
        if key in JSON_COLUMNS:
            item[key] = parse_json_object(value) if key == "raw" else parse_json_list(value)
        elif key in BOOL_COLUMNS:
            item[key] = bool(value)
    return item


def build_windows(start: date, end: date, window: str) -> list[DateWindow]:
    normalized = window.strip().lower().replace("-", "_")
    windows: list[DateWindow] = []
    cursor = start
    while cursor <= end:
        if normalized in {"day", "daily", "1d"}:
            window_end = cursor
        elif normalized in {"week", "weekly", "7d"}:
            window_end = min(cursor + timedelta(days=6), end)
        elif normalized in {"month", "monthly"}:
            next_month = date(cursor.year + int(cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
            window_end = min(next_month - timedelta(days=1), end)
        elif normalized in {"half_month", "halfmonth", "semi_month"}:
            if cursor.day <= 15:
                window_end = min(date(cursor.year, cursor.month, 15), end)
            else:
                next_month = date(
                    cursor.year + int(cursor.month == 12),
                    1 if cursor.month == 12 else cursor.month + 1,
                    1,
                )
                window_end = min(next_month - timedelta(days=1), end)
        elif normalized.isdigit() and int(normalized) > 0:
            window_end = min(cursor + timedelta(days=int(normalized) - 1), end)
        else:
            raise ValueError("window must be day, week, half_month, month, or a positive day count")
        windows.append(DateWindow(cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def sum_counter(items: Any) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for item in items:
        counter.update({str(key): int(value) for key, value in item.items()})
    return dict(counter)


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def first_datetime(*values: Any) -> datetime | None:
    for value in values:
        parsed = parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.combine(date.fromisoformat(text), time.min, tzinfo=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return normalize_datetime(parsed)


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def end_of_day(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=UTC)


def parse_json_object(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in {None, ""}:
        return {}
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return {}


def parse_json_list(value: Any) -> list[Any]:
    parsed = parse_json_object(value)
    if isinstance(parsed, list):
        return parsed
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build as-of-safe local evidence snapshots.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=DEFAULT_END.isoformat())
    parser.add_argument("--window", default=DEFAULT_WINDOW)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
        report = build_asof_snapshots(
            args.db,
            start=start,
            end=end,
            window=args.window,
            limit=args.limit,
        )
        output = args.output_dir / f"asof-snapshots-{start.isoformat()}-to-{end.isoformat()}.json"
        summary = {
            "dry_run": args.dry_run,
            "output": str(output),
            "snapshot_count": report["summary"]["snapshot_count"],
            "future_leak_count": report["summary"]["future_leak_count"],
            "future_leak_by_table": report["summary"]["future_leak_by_table"],
        }
        if args.dry_run:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        args.output_dir.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - CLI should return concise automation errors.
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

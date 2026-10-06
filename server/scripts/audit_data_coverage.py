from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_OUTPUT = Path("server/data/backfill_reports/data-coverage-latest.json")
DEFAULT_START = date(2025, 6, 16)
DEFAULT_END = date(2026, 6, 15)
DEFAULT_HORIZON_DAYS = 14
TARGETS = ("Brent", "WTI", "POY", "DTY")


@dataclass(frozen=True)
class Observation:
    instrument: str
    observed_at: date
    value: float
    source_id: str
    evidence_url: str


@dataclass(frozen=True)
class Window:
    start: date
    end: date


def audit_coverage(
    db_path: str | Path,
    *,
    start: date = DEFAULT_START,
    end: date = DEFAULT_END,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> dict[str, Any]:
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        observations = load_observations(connection, start=start, end=end + timedelta(days=horizon_days))

    by_instrument: dict[str, list[Observation]] = defaultdict(list)
    for observation in observations:
        by_instrument[observation.instrument].append(observation)

    windows = half_month_windows(start, end)
    window_rows = [
        summarize_window(window, by_instrument=by_instrument, horizon_days=horizon_days) for window in windows
    ]
    return {
        "scope": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "horizon_days": horizon_days,
            "policy": "真实后验价格不足时标记 pending_future_prices，不计入 hit/miss。",
        },
        "instruments": {
            target: summarize_instrument(
                by_instrument.get(target, []),
                start=start,
                end=end + timedelta(days=horizon_days),
            )
            for target in TARGETS
        },
        "windows": window_rows,
        "window_status_counts": count_by_status(window_rows),
        "pending_windows": [item for item in window_rows if item["overall_status"] == "pending_future_prices"],
    }


def load_observations(connection: sqlite3.Connection, *, start: date, end: date) -> list[Observation]:
    observations: list[Observation] = []
    if table_exists(connection, "market_observations"):
        rows = connection.execute("""
            SELECT source_id, observed_at, indicator, product, value, evidence_url
            FROM market_observations
            WHERE value IS NOT NULL
            """).fetchall()
        for row in rows:
            observed_at = parse_date(row["observed_at"])
            value = to_float(row["value"])
            instrument = market_instrument(row)
            if observed_at is None or value is None or instrument not in {"Brent", "WTI"}:
                continue
            if start <= observed_at <= end:
                observations.append(
                    Observation(
                        instrument=instrument,
                        observed_at=observed_at,
                        value=value,
                        source_id=str(row["source_id"] or ""),
                        evidence_url=str(row["evidence_url"] or ""),
                    )
                )
    if table_exists(connection, "industry_observations"):
        rows = connection.execute("""
            SELECT source_id, observed_at, product, metric, value, evidence_url
            FROM industry_observations
            WHERE value IS NOT NULL
            """).fetchall()
        for row in rows:
            observed_at = parse_date(row["observed_at"])
            value = to_float(row["value"])
            product = str(row["product"] or "").upper()
            if (
                observed_at is None
                or value is None
                or product not in {"POY", "DTY"}
                or str(row["metric"] or "") != "spot_quote"
            ):
                continue
            if start <= observed_at <= end:
                observations.append(
                    Observation(
                        instrument=product,
                        observed_at=observed_at,
                        value=value,
                        source_id=str(row["source_id"] or ""),
                        evidence_url=str(row["evidence_url"] or ""),
                    )
                )
    return sorted(observations, key=lambda item: (item.instrument, item.observed_at, item.source_id))


def summarize_instrument(observations: list[Observation], *, start: date, end: date) -> dict[str, Any]:
    if not observations:
        return {
            "earliest_date": None,
            "latest_date": None,
            "point_count": 0,
            "unique_dates": 0,
            "missing_dates": date_range(start, end),
            "source_ids": [],
        }
    dates = sorted({item.observed_at for item in observations})
    expected = set(date_range_as_dates(start, end))
    present = set(dates)
    return {
        "earliest_date": dates[0].isoformat(),
        "latest_date": dates[-1].isoformat(),
        "point_count": len(observations),
        "unique_dates": len(dates),
        "missing_dates": [item.isoformat() for item in sorted(expected - present)],
        "source_ids": sorted({item.source_id for item in observations if item.source_id}),
    }


def summarize_window(
    window: Window,
    *,
    by_instrument: dict[str, list[Observation]],
    horizon_days: int,
) -> dict[str, Any]:
    horizon_start = window.end + timedelta(days=1)
    horizon_end = window.end + timedelta(days=horizon_days)
    targets = {
        target: summarize_horizon(
            by_instrument.get(target, []),
            horizon_start=horizon_start,
            horizon_end=horizon_end,
        )
        for target in TARGETS
    }
    full_targets = [target for target, item in targets.items() if item["status"] == "full_horizon"]
    partial_targets = [target for target, item in targets.items() if item["status"] == "partial_observed"]
    pending_targets = [target for target, item in targets.items() if item["status"] == "pending_future_prices"]
    if pending_targets:
        overall_status = "pending_future_prices"
    elif partial_targets:
        overall_status = "partial_observed"
    else:
        overall_status = "full_horizon"
    return {
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
        "horizon_start": horizon_start.isoformat(),
        "horizon_end": horizon_end.isoformat(),
        "overall_status": overall_status,
        "full_targets": full_targets,
        "partial_targets": partial_targets,
        "pending_targets": pending_targets,
        "targets": targets,
    }


def summarize_horizon(
    observations: list[Observation],
    *,
    horizon_start: date,
    horizon_end: date,
) -> dict[str, Any]:
    series = [item for item in observations if horizon_start <= item.observed_at <= horizon_end]
    dates = sorted({item.observed_at for item in series})
    if not dates:
        status = "pending_future_prices"
    elif dates[-1] < horizon_end:
        status = "partial_observed"
    else:
        status = "full_horizon"
    return {
        "status": status,
        "points": len(series),
        "unique_dates": len(dates),
        "first_date": dates[0].isoformat() if dates else None,
        "latest_date": dates[-1].isoformat() if dates else None,
        "missing_horizon_dates": [
            item.isoformat() for item in date_range_as_dates(horizon_start, horizon_end) if item not in set(dates)
        ],
    }


def half_month_windows(start: date, end: date) -> list[Window]:
    windows = []
    cursor = start
    while cursor <= end:
        if cursor.day <= 15:
            window_end = min(date(cursor.year, cursor.month, 15), end)
        else:
            next_month = date(cursor.year + int(cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
            window_end = min(next_month - timedelta(days=1), end)
        windows.append(Window(cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def count_by_status(windows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"full_horizon": 0, "partial_observed": 0, "pending_future_prices": 0}
    for window in windows:
        status = str(window.get("overall_status") or "pending_future_prices")
        counts[status] = counts.get(status, 0) + 1
    return counts


def market_instrument(row: sqlite3.Row) -> str:
    text = f"{row['indicator']} {row['product']}".lower()
    if "brent" in text:
        return "Brent"
    if "wti" in text or "cushing" in text:
        return "WTI"
    return ""


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def date_range(start: date, end: date) -> list[str]:
    return [item.isoformat() for item in date_range_as_dates(start, end)]


def date_range_as_dates(start: date, end: date) -> list[date]:
    days = (end - start).days
    if days < 0:
        return []
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit Brent/WTI/POY/DTY coverage and future-horizon readiness.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=DEFAULT_END.isoformat())
    parser.add_argument("--horizon-days", type=int, default=DEFAULT_HORIZON_DAYS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = audit_coverage(
            args.db,
            start=date.fromisoformat(args.start),
            end=date.fromisoformat(args.end),
            horizon_days=args.horizon_days,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - CLI should return a concise automation error.
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(args.output),
                "window_status_counts": report["window_status_counts"],
                "pending_windows": len(report["pending_windows"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

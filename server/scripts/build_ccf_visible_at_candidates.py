#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_OUTPUT_DIR = Path(".codex-run/ccf-authorized-capture")
CCF_SOURCES = {"ccf_dom_daily", "ccf_manual_export"}


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build dry-run visible_at candidates for authorized CCF rows.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--decision-cutoff-time", default="15:00:00")
    args = parser.parse_args(argv)

    db_path = args.db.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    decision_cutoff = parse_time(args.decision_cutoff_time)

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = build_candidates(connection, decision_cutoff=decision_cutoff)

    summary = summarize(rows)
    report = {
        "schema_version": "ccf_visible_at_candidates.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "db_path": str(db_path),
        "scope": {
            "source_tables": ["forecast_price_points", "industry_observations"],
            "sources": sorted(CCF_SOURCES),
            "mode": "dry_run_simulation_only",
            "decision_cutoff_time": args.decision_cutoff_time,
            "promotion_rule": (
                "Rows require CCF page/export/download/capture timestamp evidence before action-grade migration."
            ),
        },
        "guardrails": {
            "db_writes": 0,
            "provider_calls": 0,
            "no_auth_bypass": True,
            "no_synthetic_prices": True,
            "no_license_promotion": True,
        },
        "summary": summary,
        "rows": rows,
        "artifacts": {
            "json": str(output_dir / "ccf-visible-at-candidates-latest.json"),
            "csv": str(output_dir / "ccf-visible-at-candidates-latest.csv"),
            "report": str(output_dir / "16-ccf-visible-at-candidates-report.md"),
        },
    }
    write_json(output_dir / "ccf-visible-at-candidates-latest.json", report)
    write_csv(output_dir / "ccf-visible-at-candidates-latest.csv", rows)
    (output_dir / "16-ccf-visible-at-candidates-report.md").write_text(render_report(report), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": summary}, ensure_ascii=False, indent=2))
    return 0


def build_candidates(connection: sqlite3.Connection, *, decision_cutoff: time) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if table_exists(connection, "forecast_price_points"):
        rows.extend(build_forecast_candidates(connection, decision_cutoff=decision_cutoff))
    if table_exists(connection, "industry_observations"):
        rows.extend(build_industry_candidates(connection, decision_cutoff=decision_cutoff))
    return sorted(
        rows,
        key=lambda row: (
            row["source_table"],
            row["source_id"],
            row["observed_at"],
            row["product"],
            row["series_or_metric"],
        ),
    )


def build_forecast_candidates(connection: sqlite3.Connection, *, decision_cutoff: time) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in CCF_SOURCES)
    records = connection.execute(
        f"""
        SELECT point_id, created_at, source_id, dataset_type, observed_at, product,
               COALESCE(NULLIF(spec, ''), NULLIF(series, ''), NULLIF(feature, ''), dataset_type) AS series_or_metric,
               price, unit
        FROM forecast_price_points
        WHERE source_id IN ({placeholders})
          AND observed_at IS NOT NULL
        ORDER BY observed_at, product, series_or_metric, point_id
        """,
        tuple(sorted(CCF_SOURCES)),
    ).fetchall()
    out = []
    for record in records:
        observed = parse_day(record["observed_at"])
        if observed is None:
            continue
        policy = ccf_policy(record["source_id"], table="forecast_price_points", frequency="daily", observed=observed)
        visible_at = reconstructed_visible_at(observed, policy)
        out.append(
            candidate_row(
                record,
                "point_id",
                "forecast_price_points",
                policy,
                visible_at,
                decision_cutoff,
                value_present=record["price"] is not None,
            )
        )
    return out


def build_industry_candidates(connection: sqlite3.Connection, *, decision_cutoff: time) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in CCF_SOURCES)
    records = connection.execute(
        f"""
        SELECT observation_id, created_at, source_id, observed_at, product, metric AS series_or_metric,
               frequency, value, unit
        FROM industry_observations
        WHERE source_id IN ({placeholders})
          AND observed_at IS NOT NULL
        ORDER BY observed_at, product, metric, observation_id
        """,
        tuple(sorted(CCF_SOURCES)),
    ).fetchall()
    out = []
    for record in records:
        observed = parse_day(record["observed_at"])
        if observed is None:
            continue
        policy = ccf_policy(
            record["source_id"],
            table="industry_observations",
            frequency=str(record["frequency"] or ""),
            observed=observed,
        )
        visible_at = reconstructed_visible_at(observed, policy)
        out.append(
            candidate_row(
                record,
                "observation_id",
                "industry_observations",
                policy,
                visible_at,
                decision_cutoff,
                value_present=record["value"] is not None,
            )
        )
    return out


def candidate_row(
    record: sqlite3.Row,
    id_column: str,
    source_table: str,
    policy: dict[str, Any],
    visible_at: datetime,
    decision_cutoff: time,
    *,
    value_present: bool,
) -> dict[str, Any]:
    observed = parse_day(record["observed_at"])
    assert observed is not None
    created_at = str(record["created_at"] or "")
    return {
        "row_id": record[id_column],
        "source_table": source_table,
        "source_id": record["source_id"],
        "product": record["product"],
        "series_or_metric": record["series_or_metric"],
        "frequency": record.get("frequency", "daily"),
        "observed_at": record["observed_at"],
        "created_at": created_at,
        "reconstructed_visible_at": visible_at.isoformat(),
        "release_policy_id": policy["policy_id"],
        "release_lag_days": policy["lag_days"],
        "release_time_utc": policy["release_time_utc"],
        "policy_confidence": policy["confidence"],
        "promotion_status": "simulation_only_needs_ccf_capture_evidence",
        "visible_before_local_import": compare_dt(visible_at, created_at) <= 0 if created_at else None,
        "visible_by_same_day_cutoff": visible_by_cutoff(visible_at, observed, decision_cutoff),
        "visible_by_next_day_cutoff": visible_by_cutoff(visible_at, observed + timedelta(days=1), decision_cutoff),
        "value_present": value_present,
        "unit": record["unit"],
        "risk_note": risk_note(source_table, str(record["source_id"]), policy),
    }


def ccf_policy(source_id: str, *, table: str, frequency: str, observed: date) -> dict[str, Any]:
    if source_id == "ccf_manual_export":
        return {
            "policy_id": "ccf_manual_export_capture_timestamp_required",
            "lag_days": 9999,
            "release_time_utc": "23:59:00",
            "confidence": "blocked_without_capture_log",
        }
    if "weekly" in frequency.lower():
        return {
            "policy_id": "ccf_weekly_observed_plus_1d_0830utc_simulation",
            "lag_days": 1,
            "release_time_utc": "08:30:00",
            "confidence": "simulation_only",
        }
    return {
        "policy_id": "ccf_daily_observed_plus_1d_0830utc_simulation",
        "lag_days": 1,
        "release_time_utc": "08:30:00",
        "confidence": "simulation_only",
    }


def reconstructed_visible_at(observed: date, policy: dict[str, Any]) -> datetime:
    hh, mm, ss = (int(part) for part in str(policy["release_time_utc"]).split(":"))
    return datetime.combine(observed + timedelta(days=int(policy["lag_days"])), time(hh, mm, ss), tzinfo=UTC)


def risk_note(source_table: str, source_id: str, policy: dict[str, Any]) -> str:
    if source_id == "ccf_manual_export":
        return "Manual export rows need the original download/capture timestamp before any action-grade use."
    return (
        f"{source_table} CCF row uses a conservative simulation timestamp only; "
        "must be replaced by CCF page/export/download evidence before migration."
    )


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "candidate_rows": len(rows),
        "by_source_table": dict(Counter(row["source_table"] for row in rows)),
        "by_source": dict(Counter(row["source_id"] for row in rows)),
        "by_product": dict(Counter(row["product"] for row in rows)),
        "by_policy": dict(Counter(row["release_policy_id"] for row in rows)),
        "policy_confidence": dict(Counter(row["policy_confidence"] for row in rows)),
        "visible_by_next_day_cutoff_rows": sum(1 for row in rows if row["visible_by_next_day_cutoff"]),
        "visible_before_local_import_rows": sum(1 for row in rows if row["visible_before_local_import"]),
        "promotion_ready_rows": 0,
        "promotion_blocker": (
            "CCF candidates are simulation-only until authorized page/export/download timestamp evidence is captured."
        ),
        "sample_rows": rows[:20],
    }


def render_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# CCF Visible-at Candidates",
        "",
        f"Generated at: `{report['generated_at']}`",
        f"DB: `{report['db_path']}`",
        "",
        "## 结论",
        "",
        "本报告把 CCF 授权源价格与产业行展开为逐行 `reconstructed_visible_at` 候选。",
        (
            "这些候选只用于研究级 simulation，不是行动级证据；真正晋升前必须拿到 CCF"
            " 页面/导出/下载/截图或自动采集日志中的可见时间。"
        ),
        "",
        "## 汇总",
        "",
        f"- 候选行数：{summary['candidate_rows']}",
        f"- 可直接晋升行数：{summary['promotion_ready_rows']}",
        f"- 阻塞：{summary['promotion_blocker']}",
        f"- 本地批量导入前理论已公开行：{summary['visible_before_local_import_rows']}",
        f"- 次日决策截点可见行：{summary['visible_by_next_day_cutoff_rows']}",
        "",
        "## Source Table Breakdown",
        "",
        "| Table | Rows |",
        "| --- | ---: |",
    ]
    for table, count in summary["by_source_table"].items():
        lines.append(f"| {table} | {count} |")
    lines.extend(["", "## Product Breakdown", "", "| Product | Rows |", "| --- | ---: |"])
    for product, count in summary["by_product"].items():
        lines.append(f"| {product} | {count} |")
    lines.extend(
        [
            "",
            "## 对 75% Goal 的影响",
            "",
            "- CCF 是 POY/DTY、PX/PTA/MEG、石脑油与利润库存开工的最高收益源。",
            "- 但当前没有行级 CCF capture timestamp，不能把这些候选计入行动级验收。",
            (
                "- 下一步可把这些候选作为 research overlay 接入 as-of feature builder，量化如果 CCF"
                " 可见性补齐后最多能解锁多少特征与 accuracy。"
            ),
            "",
            "## 写库说明",
            "",
            (
                "本次写库行数为 `0`，未触发主库备份。任何将候选写入主库的迁移前，都必须先备份到"
                " `.codex-run/full-chain-delivery/db-backups/`。"
            ),
            "",
            "## Artifacts",
            "",
            f"- JSON: `{report['artifacts']['json']}`",
            f"- CSV: `{report['artifacts']['csv']}`",
            f"- Report: `{report['artifacts']['report']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def visible_by_cutoff(visible_at: datetime, day: date, cutoff: time) -> bool:
    return visible_at <= datetime.combine(day, cutoff, tzinfo=UTC)


def compare_dt(left: datetime, right: str) -> int:
    parsed = parse_dt(right)
    if parsed is None:
        return 1
    if left < parsed:
        return -1
    if left > parsed:
        return 1
    return 0


def parse_day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_time(value: str) -> time:
    hh, mm, ss = (int(part) for part in value.split(":"))
    return time(hh, mm, ss)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    raise SystemExit(main())

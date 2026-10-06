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
DEFAULT_OUTPUT_DIR = Path(".codex-run/source-visibility")
OFFICIAL_SOURCES = {"eia_petroleum_api", "fred_macro_api", "cftc_cot_petroleum"}


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build dry-run visible_at candidates for official/public source rows.")
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
        "schema_version": "official_source_visible_at_candidates.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "db_path": str(db_path),
        "scope": {
            "source_table": "market_observations",
            "sources": sorted(OFFICIAL_SOURCES),
            "mode": "dry_run_only",
            "decision_cutoff_time": args.decision_cutoff_time,
            "promotion_rule": (
                "Candidates must be reviewed before DB migration; they are conservative policy timestamps, not"
                " fabricated market values."
            ),
        },
        "guardrails": {
            "db_writes": 0,
            "provider_calls": 0,
            "no_auth_bypass": True,
            "no_synthetic_prices": True,
        },
        "summary": summary,
        "rows": rows,
        "artifacts": {
            "json": str(output_dir / "official-source-visible-at-candidates-latest.json"),
            "csv": str(output_dir / "official-source-visible-at-candidates-latest.csv"),
            "report": str(output_dir / "15-official-source-visible-at-candidates-report.md"),
        },
    }
    write_json(output_dir / "official-source-visible-at-candidates-latest.json", report)
    write_csv(output_dir / "official-source-visible-at-candidates-latest.csv", rows)
    (output_dir / "15-official-source-visible-at-candidates-report.md").write_text(
        render_report(report), encoding="utf-8"
    )
    print(json.dumps({"status": "success", "summary": summary}, ensure_ascii=False, indent=2))
    return 0


def build_candidates(connection: sqlite3.Connection, *, decision_cutoff: time) -> list[dict[str, Any]]:
    if not table_exists(connection, "market_observations"):
        return []
    placeholders = ",".join("?" for _ in OFFICIAL_SOURCES)
    records = connection.execute(
        f"""
        SELECT observation_id, created_at, source_id, observed_at, indicator,
               product, value, unit, frequency, region, evidence_url
        FROM market_observations
        WHERE source_id IN ({placeholders})
          AND observed_at IS NOT NULL
        ORDER BY source_id, observed_at, indicator, observation_id
        """,
        tuple(sorted(OFFICIAL_SOURCES)),
    ).fetchall()
    rows = []
    for record in records:
        observed = parse_day(record["observed_at"])
        if observed is None:
            continue
        policy = release_policy(record)
        visible_at = reconstructed_visible_at(observed, policy)
        created_at = str(record["created_at"] or "")
        rows.append(
            {
                "observation_id": record["observation_id"],
                "source_table": "market_observations",
                "source_id": record["source_id"],
                "product": record["product"],
                "indicator": record["indicator"],
                "frequency": record["frequency"],
                "observed_at": record["observed_at"],
                "created_at": created_at,
                "reconstructed_visible_at": visible_at.isoformat(),
                "release_policy_id": policy["policy_id"],
                "release_lag_days": policy["lag_days"],
                "release_time_utc": policy["release_time_utc"],
                "policy_confidence": policy["confidence"],
                "promotion_status": "candidate_review_required",
                "visible_before_local_import": compare_dt(visible_at, created_at) <= 0 if created_at else None,
                "visible_by_same_day_cutoff": visible_by_cutoff(visible_at, observed, decision_cutoff),
                "visible_by_next_day_cutoff": visible_by_cutoff(
                    visible_at, observed + timedelta(days=1), decision_cutoff
                ),
                "value_present": record["value"] is not None,
                "unit": record["unit"],
                "region": record["region"],
                "evidence_url": record["evidence_url"],
                "risk_note": risk_note(record, policy),
            }
        )
    return rows


def release_policy(record: sqlite3.Row) -> dict[str, Any]:
    source_id = str(record["source_id"])
    frequency = str(record["frequency"] or "").lower()
    product = str(record["product"] or "")
    indicator = str(record["indicator"] or "")
    if source_id == "eia_petroleum_api":
        if frequency == "weekly":
            return {
                "policy_id": "eia_weekly_petroleum_observed_plus_7d_2359utc",
                "lag_days": 7,
                "release_time_utc": "23:59:00",
                "confidence": "medium_conservative",
            }
        return {
            "policy_id": "eia_daily_price_observed_plus_2d_2359utc",
            "lag_days": 2,
            "release_time_utc": "23:59:00",
            "confidence": "medium_conservative",
        }
    if source_id == "cftc_cot_petroleum":
        return {
            "policy_id": "cftc_cot_tuesday_position_plus_4d_2359utc",
            "lag_days": 4,
            "release_time_utc": "23:59:00",
            "confidence": "medium_conservative",
        }
    if source_id == "fred_macro_api":
        if product in {"crude_oil", "fx"}:
            return {
                "policy_id": "fred_daily_market_series_observed_plus_2d_2359utc",
                "lag_days": 2,
                "release_time_utc": "23:59:00",
                "confidence": "medium_conservative",
            }
        if any(token in indicator for token in ["DGS10", "SOFR", "Exchange Rate", "Dollar Index"]):
            return {
                "policy_id": "fred_daily_rate_or_fx_observed_plus_2d_2359utc",
                "lag_days": 2,
                "release_time_utc": "23:59:00",
                "confidence": "medium_conservative",
            }
        return {
            "policy_id": "fred_monthly_macro_observed_plus_45d_2359utc",
            "lag_days": 45,
            "release_time_utc": "23:59:00",
            "confidence": "low_conservative",
        }
    return {
        "policy_id": "unknown_source_review_required",
        "lag_days": 9999,
        "release_time_utc": "23:59:00",
        "confidence": "blocked",
    }


def reconstructed_visible_at(observed: date, policy: dict[str, Any]) -> datetime:
    hh, mm, ss = (int(part) for part in str(policy["release_time_utc"]).split(":"))
    return datetime.combine(observed + timedelta(days=int(policy["lag_days"])), time(hh, mm, ss), tzinfo=UTC)


def risk_note(record: sqlite3.Row, policy: dict[str, Any]) -> str:
    source_id = str(record["source_id"])
    if source_id == "fred_macro_api" and policy["confidence"] == "low_conservative":
        return (
            "FRED macro monthly releases vary by series; use this only as a conservative candidate until"
            " realtime/release metadata is captured."
        )
    if source_id == "cftc_cot_petroleum":
        return (
            "CFTC COT positions are Tuesday observations; candidate visible_at is conservatively after the usual Friday"
            " release."
        )
    if source_id == "eia_petroleum_api":
        return (
            "EIA candidate uses conservative lag; replace with exact API/release calendar timestamp when materializing."
        )
    return "Review required before promotion."


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_source = Counter(row["source_id"] for row in rows)
    by_policy = Counter(row["release_policy_id"] for row in rows)
    by_product = Counter(row["product"] for row in rows)
    confidence = Counter(row["policy_confidence"] for row in rows)
    visible_next_day = sum(1 for row in rows if row["visible_by_next_day_cutoff"])
    visible_before_import = sum(1 for row in rows if row["visible_before_local_import"])
    return {
        "candidate_rows": len(rows),
        "by_source": dict(by_source),
        "by_product": dict(by_product),
        "by_policy": dict(by_policy),
        "policy_confidence": dict(confidence),
        "visible_by_next_day_cutoff_rows": visible_next_day,
        "visible_before_local_import_rows": visible_before_import,
        "promotion_ready_rows": 0,
        "promotion_blocker": (
            "dry-run candidates are not DB-visible until reviewed, backed up, migrated, and consumed by the as-of"
            " feature builder"
        ),
        "sample_rows": rows[:20],
    }


def render_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Official Source Visible-at Candidates",
        "",
        f"Generated at: `{report['generated_at']}`",
        f"DB: `{report['db_path']}`",
        "",
        "## 结论",
        "",
        "本报告把 EIA/FRED/CFTC 的公开官方数据展开成逐行 `reconstructed_visible_at` 候选。",
        (
            "它解决的是原油/crack/宏观公开源的可见性重建入口，但本轮仍是 dry-run："
            "没有写库、没有把候选直接晋升为行动级证据。"
        ),
        "",
        "## 汇总",
        "",
        f"- 候选行数：{summary['candidate_rows']}",
        f"- 可直接晋升行数：{summary['promotion_ready_rows']}",
        f"- 阻塞：{summary['promotion_blocker']}",
        f"- 在本地批量导入前理论已公开的行：{summary['visible_before_local_import_rows']}",
        f"- 次日决策截点可见行：{summary['visible_by_next_day_cutoff_rows']}",
        "",
        "## Source Breakdown",
        "",
        "| Source | Rows |",
        "| --- | ---: |",
    ]
    for source_id, count in summary["by_source"].items():
        lines.append(f"| {source_id} | {count} |")
    lines.extend(["", "## Policy Breakdown", "", "| Policy | Rows |", "| --- | ---: |"])
    for policy, count in summary["by_policy"].items():
        lines.append(f"| {policy} | {count} |")
    lines.extend(
        [
            "",
            "## 对 75% Goal 的影响",
            "",
            "- 这批候选能优先解除原油、WTI/Brent、EIA 库存/炼厂、CFTC 持仓和 FRED 宏观源的 `visible_at` 阻塞。",
            (
                "- 但当前 full-chain as-of 特征消费的原油/crack 仍偏 Yahoo proxy；下一步需要把这些官方源候选接入"
                " feature builder，并重新跑 strict as-of/ablation。"
            ),
            (
                "- 即使官方源可见性补齐，也不能替代 CCF 产业端和 DTY 竞品价盘的行级可见性证据；"
                "POY/DTY/PX/PTA/MEG 仍是最高收益层。"
            ),
            "",
            "## 写库说明",
            "",
            (
                "本次写库行数为 `0`，未触发主库备份。若下一步执行迁移写入，必须先备份主库到"
                " `.codex-run/full-chain-delivery/db-backups/`，并记录回滚方式。"
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
    cutoff_dt = datetime.combine(day, cutoff, tzinfo=UTC)
    return visible_at <= cutoff_dt


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
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_time(value: str) -> time:
    try:
        hh, mm, ss = (int(part) for part in value.split(":"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid HH:MM:SS time: {value}") from exc
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

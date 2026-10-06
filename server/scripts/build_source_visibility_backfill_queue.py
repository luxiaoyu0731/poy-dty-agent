#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_OUTPUT_DIR = Path(".codex-run/source-visibility")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a read-only source visible_at backfill queue.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    db_path = args.db.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC)

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = build_queue(connection)

    rows = sorted(rows, key=lambda row: (row["priority_rank"], -row["rows"], row["source_table"], row["source_id"]))
    summary = summarize(rows)
    report = {
        "schema_version": "source_visibility_backfill_queue.v1",
        "generated_at": generated_at.isoformat(),
        "db_path": str(db_path),
        "guardrails": {
            "db_writes": 0,
            "provider_calls": 0,
            "purpose": "read-only queue for restoring action-grade as-of visibility; no data is fabricated or promoted",
        },
        "summary": summary,
        "rows": rows,
        "artifacts": {
            "json": str(output_dir / "source-visibility-backfill-queue-latest.json"),
            "csv": str(output_dir / "source-visibility-backfill-queue-latest.csv"),
            "report": str(output_dir / "14-source-visibility-backfill-queue-report.md"),
        },
    }
    write_json(output_dir / "source-visibility-backfill-queue-latest.json", report)
    write_csv(output_dir / "source-visibility-backfill-queue-latest.csv", rows)
    (output_dir / "14-source-visibility-backfill-queue-report.md").write_text(render_report(report), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": summary}, ensure_ascii=False, indent=2))
    return 0


def build_queue(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if table_exists(connection, "futures_daily_bars"):
        rows.extend(build_futures_queue(connection))
    if table_exists(connection, "forecast_price_points"):
        rows.extend(build_forecast_price_queue(connection))
    if table_exists(connection, "industry_observations"):
        rows.extend(build_industry_queue(connection))
    if table_exists(connection, "market_observations"):
        rows.extend(build_market_queue(connection))
    return [enrich_priority(row) for row in rows]


def build_futures_queue(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    query = """
        SELECT
            source_id,
            product,
            'daily_bars' AS series_or_metric,
            COUNT(*) AS rows,
            MIN(trade_date) AS first_observed_at,
            MAX(trade_date) AS last_observed_at,
            MIN(created_at) AS min_created_at,
            MAX(created_at) AS max_created_at,
            COUNT(DISTINCT visible_at) AS visible_at_versions,
            MIN(visible_at) AS min_visible_at,
            MAX(visible_at) AS max_visible_at,
            COUNT(DISTINCT source_publish_time) AS publish_time_versions,
            SUM(CASE WHEN date(created_at) > date(trade_date, '+7 day') THEN 1 ELSE 0 END) AS created_lag_rows,
            SUM(CASE WHEN date(visible_at) > date(trade_date, '+2 day') THEN 1 ELSE 0 END) AS visible_lag_rows
        FROM futures_daily_bars
        GROUP BY source_id, product
    """
    out = []
    for item in connection.execute(query):
        source_id = item["source_id"]
        out.append(
            base_row(
                source_table="futures_daily_bars",
                source_id=source_id,
                product=item["product"],
                series_or_metric=item["series_or_metric"],
                rows=item["rows"],
                first_observed_at=item["first_observed_at"],
                last_observed_at=item["last_observed_at"],
                min_created_at=item["min_created_at"],
                max_created_at=item["max_created_at"],
                has_visible_at=True,
                visible_at_status=visible_status(item),
                created_lag_rows=item["created_lag_rows"] or 0,
                visible_lag_rows=item["visible_lag_rows"] or 0,
                recommended_policy=policy_for_futures(source_id),
                action_grade_blocker=blocker_for_futures(source_id, item),
                needed_user_or_source_action=action_for_futures(source_id),
                can_auto_reconstruct=can_auto_reconstruct(source_id),
                risk_note=risk_for_source(source_id),
            )
        )
    return out


def build_forecast_price_queue(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    query = """
        SELECT
            source_id,
            dataset_type,
            product,
            COALESCE(NULLIF(spec, ''), NULLIF(series, ''), NULLIF(feature, ''), dataset_type) AS series_or_metric,
            COUNT(*) AS rows,
            MIN(observed_at) AS first_observed_at,
            MAX(observed_at) AS last_observed_at,
            MIN(created_at) AS min_created_at,
            MAX(created_at) AS max_created_at,
            SUM(CASE WHEN date(created_at) > date(observed_at, '+7 day') THEN 1 ELSE 0 END) AS created_lag_rows
        FROM forecast_price_points
        GROUP BY source_id, dataset_type, product, series_or_metric
    """
    out = []
    for item in connection.execute(query):
        source_id = item["source_id"]
        out.append(
            base_row(
                source_table="forecast_price_points",
                source_id=source_id,
                product=item["product"],
                series_or_metric=item["series_or_metric"],
                rows=item["rows"],
                first_observed_at=item["first_observed_at"],
                last_observed_at=item["last_observed_at"],
                min_created_at=item["min_created_at"],
                max_created_at=item["max_created_at"],
                has_visible_at=False,
                visible_at_status="missing_column",
                created_lag_rows=item["created_lag_rows"] or 0,
                visible_lag_rows=None,
                recommended_policy=policy_for_source(source_id, table="forecast_price_points"),
                action_grade_blocker=blocker_for_source(source_id, table="forecast_price_points"),
                needed_user_or_source_action=action_for_source(source_id, table="forecast_price_points"),
                can_auto_reconstruct=can_auto_reconstruct(source_id),
                risk_note=risk_for_source(source_id),
            )
        )
    return out


def build_industry_queue(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    query = """
        SELECT
            source_id,
            product,
            metric AS series_or_metric,
            frequency,
            COUNT(*) AS rows,
            MIN(observed_at) AS first_observed_at,
            MAX(observed_at) AS last_observed_at,
            MIN(created_at) AS min_created_at,
            MAX(created_at) AS max_created_at,
            SUM(CASE WHEN date(created_at) > date(observed_at, '+7 day') THEN 1 ELSE 0 END) AS created_lag_rows
        FROM industry_observations
        GROUP BY source_id, product, metric, frequency
    """
    out = []
    for item in connection.execute(query):
        source_id = item["source_id"]
        out.append(
            base_row(
                source_table="industry_observations",
                source_id=source_id,
                product=item["product"],
                series_or_metric=f"{item['series_or_metric']} ({item['frequency']})",
                rows=item["rows"],
                first_observed_at=item["first_observed_at"],
                last_observed_at=item["last_observed_at"],
                min_created_at=item["min_created_at"],
                max_created_at=item["max_created_at"],
                has_visible_at=False,
                visible_at_status="missing_column",
                created_lag_rows=item["created_lag_rows"] or 0,
                visible_lag_rows=None,
                recommended_policy=policy_for_source(source_id, table="industry_observations"),
                action_grade_blocker=blocker_for_source(source_id, table="industry_observations"),
                needed_user_or_source_action=action_for_source(source_id, table="industry_observations"),
                can_auto_reconstruct=can_auto_reconstruct(source_id),
                risk_note=risk_for_source(source_id),
            )
        )
    return out


def build_market_queue(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    query = """
        SELECT
            source_id,
            product,
            indicator AS series_or_metric,
            frequency,
            COUNT(*) AS rows,
            MIN(observed_at) AS first_observed_at,
            MAX(observed_at) AS last_observed_at,
            MIN(created_at) AS min_created_at,
            MAX(created_at) AS max_created_at,
            SUM(CASE WHEN date(created_at) > date(observed_at, '+7 day') THEN 1 ELSE 0 END) AS created_lag_rows
        FROM market_observations
        GROUP BY source_id, product, indicator, frequency
    """
    out = []
    for item in connection.execute(query):
        source_id = item["source_id"]
        out.append(
            base_row(
                source_table="market_observations",
                source_id=source_id,
                product=item["product"],
                series_or_metric=f"{item['series_or_metric']} ({item['frequency']})",
                rows=item["rows"],
                first_observed_at=item["first_observed_at"],
                last_observed_at=item["last_observed_at"],
                min_created_at=item["min_created_at"],
                max_created_at=item["max_created_at"],
                has_visible_at=False,
                visible_at_status="missing_column",
                created_lag_rows=item["created_lag_rows"] or 0,
                visible_lag_rows=None,
                recommended_policy=policy_for_source(source_id, table="market_observations"),
                action_grade_blocker=blocker_for_source(source_id, table="market_observations"),
                needed_user_or_source_action=action_for_source(source_id, table="market_observations"),
                can_auto_reconstruct=can_auto_reconstruct(source_id),
                risk_note=risk_for_source(source_id),
            )
        )
    return out


def base_row(**kwargs: Any) -> dict[str, Any]:
    return {
        "priority": "",
        "priority_rank": 99,
        "source_table": kwargs["source_table"],
        "source_id": kwargs["source_id"],
        "product": kwargs["product"],
        "series_or_metric": kwargs["series_or_metric"],
        "rows": int(kwargs["rows"] or 0),
        "first_observed_at": kwargs["first_observed_at"],
        "last_observed_at": kwargs["last_observed_at"],
        "min_created_at": kwargs["min_created_at"],
        "max_created_at": kwargs["max_created_at"],
        "has_visible_at": bool(kwargs["has_visible_at"]),
        "visible_at_status": kwargs["visible_at_status"],
        "created_lag_rows": int(kwargs["created_lag_rows"] or 0),
        "visible_lag_rows": None if kwargs["visible_lag_rows"] is None else int(kwargs["visible_lag_rows"] or 0),
        "recommended_policy": kwargs["recommended_policy"],
        "action_grade_blocker": kwargs["action_grade_blocker"],
        "needed_user_or_source_action": kwargs["needed_user_or_source_action"],
        "can_auto_reconstruct": bool(kwargs["can_auto_reconstruct"]),
        "risk_note": kwargs["risk_note"],
    }


def enrich_priority(row: dict[str, Any]) -> dict[str, Any]:
    source_id = row["source_id"]
    table = row["source_table"]
    product = row["product"]
    rank = 80
    if source_id in {"ccf_dom_daily", "ccf_manual_export"} and product in {"POY", "DTY", "PX", "PTA", "MEG", "NAPHTHA"}:
        rank = 10
    elif source_id == "peer_dty_xlsx":
        rank = 15
    elif table == "futures_daily_bars" and product in {"SC", "PTA", "PX", "MEG"}:
        rank = 20
    elif source_id in {"eia_petroleum_api", "cftc_cot_petroleum", "fred_macro_api"}:
        rank = 30
    elif source_id == "yahoo_futures_daily_proxy":
        rank = 45
    elif product in {"POY", "DTY"}:
        rank = 50
    row["priority_rank"] = rank
    row["priority"] = priority_label(rank)
    return row


def priority_label(rank: int) -> str:
    if rank <= 10:
        return "P0_action_grade_blocker"
    if rank <= 20:
        return "P1_high_yield"
    if rank <= 35:
        return "P2_official_release_time"
    if rank <= 50:
        return "P3_cross_check_or_proxy"
    return "P4_low_direct_lift"


def visible_status(row: sqlite3.Row) -> str:
    versions = int(row["visible_at_versions"] or 0)
    lag = int(row["visible_lag_rows"] or 0)
    rows = int(row["rows"] or 0)
    if versions == 0:
        return "missing_values"
    if versions == 1 and lag >= rows * 0.9:
        return "single_batch_after_history"
    if lag:
        return "partially_late_visible_at"
    return "has_visible_at"


def policy_for_futures(source_id: str) -> str:
    if source_id == "akshare_prototype":
        return (
            "research proxy only; action-grade requires official exchange/commercial visible_at or audited daily"
            " capture log"
        )
    return policy_for_source(source_id, table="futures_daily_bars")


def blocker_for_futures(source_id: str, row: sqlite3.Row) -> str:
    if source_id == "akshare_prototype":
        return (
            "AkShare is allowed only as internal prototype/cross-check and current visible_at is one 2026-07-04 batch"
            " timestamp for 2025-2026 history"
        )
    if int(row["visible_lag_rows"] or 0):
        return "visible_at is later than the decision day for part of history"
    return "none"


def action_for_futures(source_id: str) -> str:
    if source_id == "akshare_prototype":
        return (
            "Keep for prototype; add official exchange/Choice/Wind/iFinD/authorized source or daily immutable capture"
            " logs before action-grade promotion"
        )
    return action_for_source(source_id, table="futures_daily_bars")


def policy_for_source(source_id: str, *, table: str) -> str:
    if source_id in {"ccf_dom_daily", "ccf_manual_export"}:
        return "authorized source, but action-grade needs CCF export timestamp/capture timestamp per observed_at row"
    if source_id == "peer_dty_xlsx":
        return "user file can be used after provenance, file timestamp, and publish/capture timestamp are attached"
    if source_id in {"eia_petroleum_api", "fred_macro_api", "cftc_cot_petroleum"}:
        return "official/public API; reconstruct source release time and capture time, then promote after audit"
    if source_id == "yahoo_futures_daily_proxy":
        return "public proxy/cross-check only unless vendor timestamp and licensing are proven"
    if "sunsirs" in source_id or "business_society" in source_id or "texnet" in source_id:
        return "public/manual source; keep as reviewed cross-check unless article/page publication time is captured"
    if source_id.startswith("internal"):
        return "internal note only; not action-grade without external source evidence"
    return f"needs source-specific visible_at policy for {table}"


def blocker_for_source(source_id: str, *, table: str) -> str:
    if source_id in {"ccf_dom_daily", "ccf_manual_export"}:
        return (
            "table has no visible_at column; historical CCF rows were loaded in 2026 batches, so decision-day"
            " visibility is not proven"
        )
    if source_id == "peer_dty_xlsx":
        return "competitor DTY file lacks row-level publish/capture timestamps and ends before late-2026H1 dates"
    if source_id in {"eia_petroleum_api", "fred_macro_api", "cftc_cot_petroleum"}:
        return "official source but table lacks visible_at/source_release_at; release calendars must be materialized"
    if source_id == "yahoo_futures_daily_proxy":
        return "proxy source lacks action-grade visible_at/licensing proof"
    if "sunsirs" in source_id or "business_society" in source_id or "texnet" in source_id:
        return "manual/public reviewed rows need source page publish time and capture log"
    if source_id.startswith("internal"):
        return "internal notes cannot independently prove market visibility"
    return "visible_at missing or unproven"


def action_for_source(source_id: str, *, table: str) -> str:
    if source_id in {"ccf_dom_daily", "ccf_manual_export"}:
        return (
            "Use current Safari CCF login to export/capture 2026H1 target series with download timestamp and page"
            " timestamp; stop on CAPTCHA/2FA/export limits"
        )
    if source_id == "peer_dty_xlsx":
        return (
            "Provide or locate original competitor DTY files for 2026H1, file modification timestamps, source owner,"
            " and missing dates after 2026-05-20"
        )
    if source_id == "eia_petroleum_api":
        return "Automate EIA release calendar/API metadata capture for weekly petroleum and daily price series"
    if source_id == "fred_macro_api":
        return "Capture FRED realtime_start/realtime_end or release metadata for macro/oil spot series"
    if source_id == "cftc_cot_petroleum":
        return "Capture CFTC report publication dates and map Tuesday positions to Friday release visibility"
    if source_id == "yahoo_futures_daily_proxy":
        return (
            "Keep as cross-check; replace with authorized futures vendor or exchange settlement source for action-grade"
        )
    if "sunsirs" in source_id or "business_society" in source_id or "texnet" in source_id:
        return "Capture article/page URL, page publication time, and archived fetch time before promotion"
    if source_id.startswith("internal"):
        return "Keep out of action-grade gate unless backed by external source evidence"
    return "Define visible_at policy, capture mechanism, and license status"


def can_auto_reconstruct(source_id: str) -> bool:
    return source_id in {"eia_petroleum_api", "fred_macro_api", "cftc_cot_petroleum"}


def risk_for_source(source_id: str) -> str:
    if source_id == "akshare_prototype":
        return "allowed as internal prototype/public proxy/cross-check; cannot be represented as formal authorized data"
    if source_id in {"ccf_dom_daily", "ccf_manual_export"}:
        return (
            "authorized CCF data is high value, but rows need auditable capture timestamps before action-grade backtest"
        )
    if source_id == "peer_dty_xlsx":
        return "useful for DTY competitor structure, but provenance and late-2026H1 coverage are weak"
    if source_id in {"eia_petroleum_api", "fred_macro_api", "cftc_cot_petroleum"}:
        return "best candidate for automated visible_at reconstruction because release schedules are public"
    if source_id == "yahoo_futures_daily_proxy":
        return "proxy data can reduce research gaps but carries licensing and timestamp risk"
    return "promotion risk depends on timestamp, license, and source provenance evidence"


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total_rows = sum(row["rows"] for row in rows)
    action_blocked_rows = sum(row["rows"] for row in rows if row["action_grade_blocker"] != "none")
    auto_rows = sum(row["rows"] for row in rows if row["can_auto_reconstruct"])
    priority_counts = Counter(row["priority"] for row in rows)
    source_counts = Counter(row["source_id"] for row in rows)
    return {
        "queue_items": len(rows),
        "underlying_data_rows": total_rows,
        "action_grade_blocked_rows": action_blocked_rows,
        "auto_reconstructable_rows": auto_rows,
        "manual_or_authorized_capture_rows": total_rows - auto_rows,
        "priority_counts": dict(priority_counts),
        "top_sources_by_items": dict(source_counts.most_common(12)),
        "highest_priority_items": rows[:15],
    }


def render_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Source Visibility Backfill Queue",
        "",
        f"Generated at: `{report['generated_at']}`",
        f"DB: `{report['db_path']}`",
        "",
        "## 结论",
        "",
        "当前瓶颈不是单纯缺价格行，而是缺少可审计的 `visible_at/source_release_at/capture_at`。",
        "这会导致 2026H1 回测即使特征覆盖接近完整，也不能证明这些特征在当时决策时可见，因此不能进入行动预测级验收。",
        "",
        "## 汇总",
        "",
        f"- 回填队列项：{summary['queue_items']}",
        f"- 覆盖底层数据行：{summary['underlying_data_rows']}",
        f"- 行动级可见性阻塞行：{summary['action_grade_blocked_rows']}",
        f"- 可自动重建发布时间的行：{summary['auto_reconstructable_rows']}",
        f"- 需要授权源/人工证明的行：{summary['manual_or_authorized_capture_rows']}",
        "",
        "## 优先级分布",
        "",
        "| Priority | Items |",
        "| --- | ---: |",
    ]
    for priority, count in summary["priority_counts"].items():
        lines.append(f"| {priority} | {count} |")
    lines.extend(
        [
            "",
            "## P0/P1 高收益队列",
            "",
            "| Priority | Table | Source | Product | Series | Rows | Observed Range | Auto? | Blocker | Action |",
            "| --- | --- | --- | --- | --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for row in [item for item in report["rows"] if item["priority_rank"] <= 20][:40]:
        observed_range = f"{row['first_observed_at']}..{row['last_observed_at']}"
        lines.append(
            "| {priority} | {table} | {source} | {product} | {series} | {rows} | {range} | {auto} | {blocker} |"
            " {action} |".format(
                priority=row["priority"],
                table=row["source_table"],
                source=row["source_id"],
                product=row["product"],
                series=escape_cell(row["series_or_metric"]),
                rows=row["rows"],
                range=observed_range,
                auto="yes" if row["can_auto_reconstruct"] else "no",
                blocker=escape_cell(row["action_grade_blocker"]),
                action=escape_cell(row["needed_user_or_source_action"]),
            )
        )
    lines.extend(
        [
            "",
            "## 对 75% Goal 的影响",
            "",
            (
                "- AkShare 期货端已经补齐研究覆盖，但当前 `akshare_prototype` 只能作为 internal prototype / public"
                " proxy / cross-check，不能作为行动级验收主证据。"
            ),
            (
                "- CCF 产业端是最高收益数据，但 `forecast_price_points` 与 `industry_observations` 当前无行级"
                " `visible_at`，需要 CCF 导出/截图/下载日志或页面发布时间证据。"
            ),
            (
                "- EIA/FRED/CFTC 是最适合自动补 `source_release_at` 的公开源，应该优先自动化，因为它能把原油/crack"
                " 传导误判从研究态推向可审计态。"
            ),
            "- DTY 竞品价盘仍需要用户或源文件层面的 provenance，尤其是 2026-05-20 之后缺口。",
            "",
            "## 写库说明",
            "",
            (
                "本次脚本只读数据库，数据库写入行数为 `0`，未触发主库备份要求。真正回填 `visible_at`"
                " 前仍必须先备份主库到 `.codex-run/full-chain-delivery/db-backups/`。"
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


def escape_cell(value: Any) -> str:
    return str(value).replace("|", "/").replace("\n", " ")


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


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone()
    return row is not None


if __name__ == "__main__":
    raise SystemExit(main())

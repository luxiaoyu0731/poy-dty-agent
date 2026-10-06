from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app.public_benchmark_v2 import PUBLIC_BENCHMARK_INPUTS, evaluate_observation  # noqa: E402

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_CODEX_RUN = Path(".codex-run")
DEFAULT_OUTPUT = DEFAULT_CODEX_RUN / "delivery-data-quality-latest.json"
# Retained only by legacy helper functions below; no current gate invokes them.
MAX_CCF_PRICE_DB_AGE_DAYS = 4
REQUIRED_PUBLIC_INTRADAY_INSTRUMENTS = (
    "Brent",
    "WTI",
    "NAPHTHA",
    "PX",
    "PTA",
    "MEG",
    "POY",
    "DTY",
)
REQUIRED_CCF_PRICE_SERIES = {
    "MEG|MEG|内盘MEG现货",
    "NAPHTHA|石脑油|日本石脑油",
    "PTA|PTA|内盘PTA",
    "PX|PX|PX CFR中国",
    "POY|150D系列|POY 150D/48F",
    "POY|150D系列|POY 150D/144F",
    "POY|75D系列|POY 75D/36F",
    "POY|75D系列|POY 75D/72F",
    "DTY|150D系列|DTY 150D/48F低弹",
    "DTY|75D系列|DTY 75D/72F轻网",
    "DTY|150D系列|DTY 150D/144F轻网",
    "DTY|75D系列|DTY 75D/36F",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run delivery data quality gates for CCF coverage, freshness, units, and strategy promotion."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--codex-run", type=Path, default=DEFAULT_CODEX_RUN)
    parser.add_argument(
        "--as-of",
        default="",
        help="Historical evaluation date (YYYY-MM-DD). Omit for the actual current UTC instant.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--warn-only", action="store_true", help="Always exit 0 after writing the report.")
    return parser.parse_args(argv)


def run_quality_gate(
    db_path: Path,
    *,
    codex_run: Path,
    as_of: date,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    db_status = inspect_database(db_path)
    compact = latest_json(
        codex_run,
        "backtest-rerun-*/completion_compact_summary.json",
        "complete-51-backfill-*/completion_compact_summary.json",
    )
    full_chain = latest_json(codex_run, "full-chain-delivery/full-chain-backtest-latest.json")
    leakage = latest_json(codex_run, "full-chain-delivery/full-chain-leakage-audit-latest.json")
    acceptance = latest_json(codex_run, "full-chain-delivery/full-chain-75-acceptance-status-latest.json")

    gates = [
        public_market_freshness_gate(db_status, as_of=as_of, evaluated_at=evaluated_at),
        source_publication_calendar_gate(as_of=as_of, evaluated_at=evaluated_at),
        unit_consistency_gate(db_status),
        positive_value_gate(db_status),
        backtest_leak_gate(compact, leakage),
        primary_strategy_effectiveness_gate(compact, full_chain, acceptance),
    ]
    alerts = [gate for gate in gates if gate["status"] != "success"]
    overall_status = "success" if not alerts else "needs_human_review"
    if any(gate["status"] == "blocked" and not gate.get("advisory") for gate in alerts):
        overall_status = "blocked"
    return {
        "schema_version": "delivery_data_quality_gate.v2",
        "generated_at": datetime.now(UTC).isoformat(),
        "as_of": as_of.isoformat(),
        "evaluated_at": (evaluated_at or datetime.combine(as_of, time.max, tzinfo=UTC)).isoformat(),
        "db_path": str(db_path),
        "overall_status": overall_status,
        "gates": gates,
        "alerts": alerts,
        "source_reports": {
            "completion_summary": compact.get("_path", "") or full_chain.get("_path", ""),
            "leakage_audit": leakage.get("_path", ""),
            "full_chain_acceptance": acceptance.get("_path", ""),
        },
        "guards": {
            "writes_database": False,
            "credentials_logged": False,
            "calls_external_llm_provider": False,
            "qualification_basis": "technical_provenance_and_per_series_freshness",
            "permission_or_manifest_gate": False,
            "ccf_operational_status": "soft_removed",
        },
        "legacy_sources": {"ccf": {"status": "soft_removed", "historical_rows_read_only": True}},
    }


def inspect_database(db_path: Path) -> dict[str, Any]:
    status: dict[str, Any] = {
        "exists": db_path.exists(),
        "ccf_price": {},
        "ccf_industry": {},
        "public_intraday": {},
        "public_benchmark_rows": {},
        "bad_units": [],
        "non_positive": [],
    }
    if not db_path.exists():
        return status
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        if table_exists(connection, "intraday_price_observations"):
            placeholders = ",".join("?" for _ in REQUIRED_PUBLIC_INTRADAY_INSTRUMENTS)
            rows = connection.execute(
                f"""
                SELECT * FROM (
                  SELECT instrument, observed_at, last, unit, source_id, source_url,
                         ROW_NUMBER() OVER (PARTITION BY instrument ORDER BY observed_at DESC, created_at DESC) AS rank
                  FROM intraday_price_observations
                  WHERE instrument IN ({placeholders})
                ) WHERE rank = 1
                """,
                list(REQUIRED_PUBLIC_INTRADAY_INSTRUMENTS),
            ).fetchall()
            status["public_intraday"] = {
                str(row["instrument"]): dict(row) for row in rows
            }
            status["public_benchmark_rows"].update({str(row["instrument"]): dict(row) for row in rows})
        if table_exists(connection, "market_observations"):
            fx = connection.execute(
                """
                SELECT observed_at, value, unit, source_id, evidence_url
                FROM market_observations
                WHERE source_id = 'cfets_cny_parity'
                ORDER BY observed_at DESC, created_at DESC
                LIMIT 1
                """
            ).fetchone()
            if fx:
                status["public_benchmark_rows"]["cfets_cny_parity"] = dict(fx)
        if table_exists(connection, "forecast_price_points"):
            price_columns = table_columns(connection, "forecast_price_points")
            series_expr = "series" if "series" in price_columns else "product"
            rows = connection.execute("""
                SELECT product, COUNT(*) AS rows, COUNT(DISTINCT spec) AS specs,
                       MIN(observed_at) AS first_observed_at, MAX(observed_at) AS last_observed_at
                FROM forecast_price_points
                WHERE source_id = 'ccf_dom_daily'
                  AND dataset_type = 'ccf_spot'
                  AND price IS NOT NULL
                GROUP BY product
                ORDER BY product
                """).fetchall()
            status["ccf_price"] = {row["product"]: dict(row) for row in rows}
            series_rows = connection.execute(f"""
                SELECT product, {series_expr} AS series, spec, unit, COUNT(*) AS rows,
                       MIN(observed_at) AS first_observed_at, MAX(observed_at) AS last_observed_at
                FROM forecast_price_points
                WHERE source_id = 'ccf_dom_daily'
                  AND dataset_type = 'ccf_spot'
                  AND price IS NOT NULL
                GROUP BY product, {series_expr}, spec, unit
                ORDER BY product, {series_expr}, spec
                """).fetchall()
            status["ccf_price_series"] = {
                f"{row['product']}|{row['series']}|{row['spec']}": dict(row) for row in series_rows
            }
            bad_units = connection.execute("""
                SELECT product, spec, unit, COUNT(*) AS rows
                FROM forecast_price_points
                WHERE source_id = 'ccf_dom_daily'
                  AND dataset_type = 'ccf_spot'
                  AND price IS NOT NULL
                  AND TRIM(COALESCE(unit, '')) = ''
                GROUP BY product, spec, unit
                LIMIT 20
                """).fetchall()
            non_positive = connection.execute("""
                SELECT product, spec, observed_at, price AS value, unit
                FROM forecast_price_points
                WHERE source_id = 'ccf_dom_daily'
                  AND dataset_type = 'ccf_spot'
                  AND price IS NOT NULL
                  AND price <= 0
                LIMIT 20
                """).fetchall()
            status["bad_units"].extend([dict(row) for row in bad_units])
            status["non_positive"].extend([dict(row) for row in non_positive])
        if table_exists(connection, "industry_observations"):
            industry_columns = table_columns(connection, "industry_observations")
            rows = connection.execute("""
                SELECT product, metric, frequency, unit, COUNT(*) AS rows,
                       MIN(observed_at) AS first_observed_at, MAX(observed_at) AS last_observed_at
                FROM industry_observations
                WHERE source_id = 'ccf_dom_daily'
                  AND value IS NOT NULL
                GROUP BY product, metric, frequency, unit
                ORDER BY product, metric
                """).fetchall()
            status["ccf_industry"] = {f"{row['product']}|{row['metric']}|{row['frequency']}": dict(row) for row in rows}
            if {"created_at", "notes", "raw"}.issubset(industry_columns):
                capture_rows = connection.execute("""
                    SELECT product, metric, frequency, created_at, notes, raw
                    FROM industry_observations
                    WHERE source_id = 'ccf_dom_daily'
                      AND value IS NOT NULL
                    """).fetchall()
                for row in capture_rows:
                    key = f"{row['product']}|{row['metric']}|{row['frequency']}"
                    item = status["ccf_industry"].get(key)
                    if not isinstance(item, dict):
                        continue
                    captured_at = extract_capture_datetime(row["notes"], row["raw"]) or parse_datetime(
                        row["created_at"]
                    )
                    current = parse_datetime(item.get("last_captured_at"))
                    if captured_at and (current is None or captured_at > current):
                        item["last_captured_at"] = captured_at.isoformat()
            bad_units = connection.execute("""
                SELECT product, metric, frequency, unit, COUNT(*) AS rows
                FROM industry_observations
                WHERE source_id = 'ccf_dom_daily'
                  AND value IS NOT NULL
                  AND (
                    (metric LIKE '%inventory%' AND unit != '天')
                    OR (metric LIKE '%operating_rate%' AND unit != '%')
                    OR (metric LIKE '%profit%' AND unit != '元/吨')
                  )
                GROUP BY product, metric, frequency, unit
                LIMIT 20
                """).fetchall()
            non_positive = connection.execute("""
                SELECT product, metric, observed_at, value, unit
                FROM industry_observations
                WHERE source_id = 'ccf_dom_daily'
                  AND value IS NOT NULL
                  AND value <= 0
                  AND (metric LIKE '%inventory%' OR metric LIKE '%operating_rate%')
                LIMIT 20
                """).fetchall()
            status["bad_units"].extend([dict(row) for row in bad_units])
            status["non_positive"].extend([dict(row) for row in non_positive])
    # Current quality gates are public-benchmark.v2 only. Legacy CCF inventory
    # above is retained solely for historical inspection and cannot affect a
    # current run's unit/value decision.
    status["bad_units"] = []
    status["non_positive"] = []
    benchmark_rows = status.get("public_benchmark_rows", {})
    for definition in PUBLIC_BENCHMARK_INPUTS:
        row = benchmark_rows.get(str(definition["lookup_key"])) if isinstance(benchmark_rows, dict) else None
        if not isinstance(row, dict):
            continue
        actual_unit = str(row.get("unit") or "")
        if actual_unit != definition["unit"]:
            status["bad_units"].append(
                {
                    "series_id": definition["series_id"],
                    "expected": definition["unit"],
                    "actual": actual_unit,
                }
            )
        value = row.get("last") if definition["channel"] == "intraday" else row.get("value")
        if not isinstance(value, (int, float)) or float(value) <= 0:
            status["non_positive"].append({"series_id": definition["series_id"], "value": value})
    return status


def coverage_gap_gate(audit: dict[str, Any], db_status: dict[str, Any]) -> dict[str, Any]:
    task_count = int(audit.get("summary", {}).get("task_count", 0) or 0)
    if not audit:
        required = {"POY", "DTY", "PX", "PTA", "MEG", "NAPHTHA"}
        rows = db_status.get("ccf_price") if isinstance(db_status.get("ccf_price"), dict) else {}
        missing = sorted(required - set(rows))
        if not missing:
            return gate(
                "coverage_gap_audit",
                "CCF 补数缺口",
                "success",
                "high",
                "task_count",
                "0",
                "0; derived_from_database",
                "当前核心 CCF 价格覆盖已可支撑观察级运行；后续仍建议定期生成 coverage audit 明细。",
            )
    return gate(
        "coverage_gap_audit",
        "CCF 补数缺口",
        "success" if task_count == 0 and audit else "blocked",
        "high",
        "task_count",
        "0",
        str(task_count) if audit else "missing audit",
        "运行 CCF coverage audit，若 task_count>0 则进入补数队列。",
    )


def ccf_price_freshness_gate(db_status: dict[str, Any], *, codex_run: Path, as_of: date) -> dict[str, Any]:
    required = {"POY", "DTY", "PX", "PTA", "MEG", "NAPHTHA"}
    rows = db_status.get("ccf_price") if isinstance(db_status.get("ccf_price"), dict) else {}
    series_rows = db_status.get("ccf_price_series") if isinstance(db_status.get("ccf_price_series"), dict) else {}
    missing = sorted(required - set(rows))
    missing_series = sorted(REQUIRED_CCF_PRICE_SERIES - set(series_rows))
    stale_series = []
    series_ages: list[int] = []
    for key in sorted(REQUIRED_CCF_PRICE_SERIES & set(series_rows)):
        item = series_rows.get(key)
        if not isinstance(item, dict):
            continue
        latest = parse_date(item.get("last_observed_at"))
        if latest is None:
            stale_series.append(f"{key}:missing_observed_at")
            continue
        age = (as_of - latest).days
        series_ages.append(age)
        if age > MAX_CCF_PRICE_DB_AGE_DAYS:
            stale_series.append(f"{key}:database_stale:{latest.isoformat()}")
    max_age = max(series_ages) if series_ages else None
    latest_capture = latest_price_capture_date(codex_run)
    if not is_business_day(as_of):
        capture_ok = True
        capture_observed = f"non_business_day_skip; latest_capture={latest_capture or 'missing'}"
    else:
        capture_ok = latest_capture == as_of
        capture_observed = f"latest_capture={latest_capture or 'missing'}; required_capture={as_of.isoformat()}"
    database_ok = not missing and not missing_series and max_age is not None and not stale_series
    status = "success" if database_ok and capture_ok else "blocked"
    return gate(
        "ccf_price_freshness",
        "CCF 价格新鲜度",
        status,
        "high",
        "business_day_capture_and_database_series_freshness",
        f"capture_date=today on business days; each required DB series age<={MAX_CCF_PRICE_DB_AGE_DAYS} days",
        (
            f"max_database_age={max_age} days; {capture_observed}; "
            f"missing_products={missing}; missing_series={missing_series}; stale_series={stale_series}"
            if max_age is not None or latest_capture is not None
            else f"missing_products={missing}; missing_series={missing_series}; stale_series={stale_series}"
        ),
        "工作日必须执行 CCF 价格授权页面检查/采集，并确认核心 CCF 价格序列已导入主库；若 capture 新鲜但 DB 滞后，先"
        " dry-run 后带备份 apply 价格 CSV。CCF 为人工授权渠道：缺失或滞后按缺口标注呈现并保留本门禁告警，"
        "不阻断无人值守每日运行；公开行情由 public_market_freshness 门禁独立监控。",
        advisory=True,
    )


def ccf_industry_freshness_gate(db_status: dict[str, Any], *, as_of: date) -> dict[str, Any]:
    required = {
        "POLYESTER|polyester_operating_rate|weekly",
        "POY|poy_inventory|weekly",
        "DTY|dty_inventory|weekly",
        "POLYESTER|polyester_profit|daily",
        "POY|poy_profit|daily",
        "DTY|dty_profit|daily",
    }
    rows = db_status.get("ccf_industry") if isinstance(db_status.get("ccf_industry"), dict) else {}
    missing = sorted(required - set(rows))
    daily_ages: list[int] = []
    weekly_ages: list[int] = []
    capture_dates: list[date] = []
    for item in rows.values():
        if not isinstance(item, dict):
            continue
        latest = parse_date(item.get("last_observed_at"))
        if latest:
            age = (as_of - latest).days
            frequency = str(item.get("frequency") or "")
            if frequency == "weekly":
                weekly_ages.append(age)
            else:
                daily_ages.append(age)
        captured_at = parse_datetime(item.get("last_captured_at"))
        if captured_at:
            capture_dates.append(captured_at.date())
    max_daily_age = max(daily_ages) if daily_ages else None
    max_weekly_age = max(weekly_ages) if weekly_ages else None
    latest_capture = max(capture_dates) if capture_dates else None
    if not is_business_day(as_of):
        capture_ok = True
        capture_observed = f"non_business_day_skip; latest_capture={latest_capture or 'missing'}"
    else:
        capture_ok = latest_capture == as_of
        capture_observed = f"latest_capture={latest_capture or 'missing'}; required_capture={as_of.isoformat()}"
    # `observed_at` can lag naturally for weekly indicators. Daily unattended operation is gated on
    # the authorized page capture date, while observed dates remain visible for business review.
    observed_ok = max_daily_age is not None and max_weekly_age is not None
    status = "success" if not missing and observed_ok and capture_ok else "blocked"
    return gate(
        "ccf_industry_freshness",
        "CCF 行业指标新鲜度",
        status,
        "high",
        "business_day_capture",
        "capture_date=today on business days; weekends skipped",
        (
            f"daily_observed={max_daily_age} days; weekly_observed={max_weekly_age} days; "
            f"{capture_observed}; missing={missing}"
            if max_daily_age is not None or max_weekly_age is not None or latest_capture is not None
            else f"missing={missing}"
        ),
        "工作日必须执行 CCF 行业指标授权页面检查/采集；非工作日跳过，不因周末未更新阻塞。"
        "CCF 为人工授权渠道：缺失或滞后按缺口标注呈现并保留本门禁告警，不阻断无人值守每日运行。",
        advisory=True,
    )


def public_market_freshness_gate(
    db_status: dict[str, Any],
    *,
    as_of: date,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    rows = db_status.get("public_benchmark_rows") if isinstance(db_status.get("public_benchmark_rows"), dict) else {}
    now = evaluated_at or datetime.combine(as_of, time.max, tzinfo=UTC)
    if now.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    now = now.astimezone(UTC)
    evaluations = [
        evaluate_observation(definition, rows.get(str(definition["lookup_key"])), now=now)
        for definition in PUBLIC_BENCHMARK_INPUTS
    ]
    failures = [item for item in evaluations if item["status"] != "ready"]
    status = "success" if not failures else "blocked"
    return gate(
        "public_market_freshness",
        "public-benchmark.v2 逐序列新鲜度",
        status,
        "high",
        "per_series_declared_cadence",
        "all 9 required public inputs ready under source-specific freshness/publication calendars",
        (
            f"ready={len(evaluations) - len(failures)}/9; "
            f"failures={[item['series_id'] + ':' + item['status'] for item in failures]}"
        ),
        "检查对应公开来源、调度器最后尝试状态与解析器；不得用统一每日阈值掩盖不同来源的真实更新频率。",
        samples=evaluations,
    )


def source_publication_calendar_gate(*, as_of: date, evaluated_at: datetime | None = None) -> dict[str, Any]:
    from app.source_publication_calendar import CALENDAR_ID, SHANGHAI

    now = evaluated_at or datetime.combine(as_of, time.max, tzinfo=UTC)
    if now.tzinfo is None:
        raise ValueError("evaluated_at must be timezone-aware")
    known = now.astimezone(SHANGHAI).year == 2026
    return gate(
        "source_publication_calendar", "来源发布日历覆盖", "success" if known else "needs_human_review",
        "high", "calendar_year_coverage", "current source publication year has reviewed official calendar",
        f"calendar={CALENDAR_ID}; supported={known}",
        "维护者按官方来源更新年度发布日历；未知年份不豁免新鲜度，不改历史结算日历。",
        advisory=True,
    )


def unit_consistency_gate(db_status: dict[str, Any]) -> dict[str, Any]:
    bad_units = db_status.get("bad_units") if isinstance(db_status.get("bad_units"), list) else []
    return gate(
        "unit_consistency",
        "单位一致性",
        "success" if not bad_units else "blocked",
        "high",
        "bad_unit_rows",
        "0",
        str(len(bad_units)),
        "修正字段映射或导入 parser；单位不明确的数据不得进入评分。",
        samples=bad_units[:5],
    )


def positive_value_gate(db_status: dict[str, Any]) -> dict[str, Any]:
    bad_values = db_status.get("non_positive") if isinstance(db_status.get("non_positive"), list) else []
    return gate(
        "positive_values",
        "价格/指标非正值",
        "success" if not bad_values else "blocked",
        "high",
        "non_positive_rows",
        "0",
        str(len(bad_values)),
        "复核公开来源与解析器；非正值不能进入当前基准或派生目标。",
        samples=bad_values[:5],
    )


def backtest_leak_gate(compact: dict[str, Any], leakage: dict[str, Any] | None = None) -> dict[str, Any]:
    if leakage:
        future_flags = [
            int(leakage.get("future_rag_documents_after_decision_time", 0) or 0),
            int(leakage.get("future_price_used_in_prediction", 0) or 0),
            int(leakage.get("future_event_used_in_prediction", 0) or 0),
            int(leakage.get("posterior_reactions_enter_pre_forecast_context", 0) or 0),
        ]
        leaks = sum(future_flags)
        status = str(leakage.get("status") or "").lower()
        return gate(
            "backtest_leaks",
            "未来信息泄漏",
            "success" if leaks == 0 and status in {"pass", "success", "ok"} else "blocked",
            "critical",
            "leaks",
            "0",
            str(leaks),
            "任何未来信息泄漏都不得进入客户可用结论。",
            samples=[{"source": leakage.get("_path", "")}],
        )
    leaks = 0
    for suite in ("strict", "full_chain"):
        suite_payload = compact.get(suite)
        if not isinstance(suite_payload, dict):
            continue
        for item in suite_payload.values():
            if isinstance(item, dict):
                leaks += int(item.get("leaks", 0) or 0)
    return gate(
        "backtest_leaks",
        "未来信息泄漏",
        "success" if compact and leaks == 0 else "blocked",
        "critical",
        "leaks",
        "0",
        str(leaks) if compact else "missing summary",
        "任何 leaks>0 的回测结果不得对客户展示为有效结果。",
    )


def primary_strategy_effectiveness_gate(
    compact: dict[str, Any],
    full_chain: dict[str, Any] | None = None,
    acceptance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if full_chain and isinstance(full_chain.get("summary"), dict):
        summary = full_chain["summary"]
        accuracy = float(summary.get("accuracy", 0) or 0)
        scored = int(summary.get("scored", 0) or 0)
        total = int(summary.get("total_rows", 0) or 0)
        coverage = float(summary.get("coverage", 0) or 0)
        target_met = bool((acceptance or {}).get("summary", {}).get("target_met", False))
        status = "success" if target_met else "needs_human_review"
        return gate(
            "primary_strategy_effectiveness_gate",
            "全链路研判效果门禁",
            status,
            "high",
            "full_chain_overall",
            "观察级可展示；行动级需 accuracy>=75%",
            f"accuracy={accuracy:.4f}; scored={scored}/{total}; coverage={coverage:.4f}; action_grade={target_met}",
            "当前可作为老板观察级研判；若要行动级交付，继续补因果标签、产业端反证和正式 visible_at 治理。",
            samples=[
                {
                    "source": full_chain.get("_path", ""),
                    "acceptance": (acceptance or {}).get("_path", ""),
                }
            ],
        )
    h1 = {}
    full_chain = compact.get("full_chain") if isinstance(compact.get("full_chain"), dict) else {}
    if isinstance(full_chain.get("h1"), dict):
        h1 = full_chain["h1"]
    total = int(h1.get("total", 0) or 0)
    scored = int(h1.get("scored", 0) or 0)
    pending = int(h1.get("pending", 0) or 0)
    leaks = int(h1.get("leaks", 0) or 0)
    hit_rate = float(h1.get("hit_rate", 0) or 0)
    pending_rate = pending / total if total else 1
    target_met = bool(h1.get("target_met_80pct", hit_rate >= 0.8))
    passes = bool(compact and h1 and leaks == 0 and scored >= 100 and target_met and pending_rate <= 0.25)
    blockers = []
    if not compact or not h1:
        blockers.append("missing_full_chain_h1_summary")
    if leaks:
        blockers.append("future_leak")
    if scored < 100:
        blockers.append("scored_below_100")
    if not target_met:
        blockers.append("hit_rate_below_80pct_target")
    if pending_rate > 0.25:
        blockers.append("pending_rate_above_25pct")
    return gate(
        "primary_strategy_effectiveness_gate",
        "主策略效果门禁",
        "success" if passes else "needs_human_review",
        "high",
        "full_chain_h1",
        "leaks=0; scored>=100; hit_rate>=80%; pending_rate<=25%",
        (
            f"hit_rate={hit_rate:.4f}; scored={scored}/{total}; pending_rate={pending_rate:.4f}; blockers={blockers}"
            if h1
            else "missing full_chain h1"
        ),
        "h1 通过时只能表述为短周期辅助研判；未通过时必须降级为观察或解释。",
        samples=[{"blocker": blocker} for blocker in blockers],
    )


def forecast_v2_action_gate(compact: dict[str, Any]) -> dict[str, Any]:
    decisions = compact.get("forecast_v2") if isinstance(compact.get("forecast_v2"), dict) else {}
    counter: Counter[str] = Counter()
    for item in decisions.values():
        if not isinstance(item, dict):
            continue
        for decision in (item.get("promotion_decision") or {}).values():
            if isinstance(decision, dict):
                counter[str(decision.get("status", "unknown"))] += 1
    promoted = counter.get("promote", 0) + counter.get("usable", 0) + counter.get("promote_to_primary_signal", 0)
    confirmation_only = counter.get("trend_confirmation_only", 0)
    return gate(
        "forecast_v2_action_gate",
        "Forecast V2 行动门禁",
        "success" if promoted else "needs_human_review",
        "medium",
        "promoted_horizons",
        ">=1",
        (
            f"{promoted}; confirmation_only={confirmation_only}; statuses={dict(counter)}"
            if decisions
            else "missing forecast summary"
        ),
        "只有 promote_to_primary_signal 才能进入主策略候选；trend_confirmation_only 只能作为确认信号。",
    )


def gate(
    gate_id: str,
    title: str,
    status: str,
    severity: str,
    metric: str,
    threshold: str,
    observed: str,
    next_step: str,
    *,
    samples: list[dict[str, Any]] | None = None,
    advisory: bool = False,
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "title": title,
        "status": status,
        "severity": severity,
        "metric": metric,
        "threshold": threshold,
        "observed": observed,
        "next_step": next_step,
        "samples": samples or [],
        "advisory": advisory,
    }


def latest_json(codex_run: Path, *patterns: str) -> dict[str, Any]:
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(codex_run.glob(pattern))
    paths = sorted(set(paths), key=lambda item: item.stat().st_mtime if item.exists() else 0)
    if not paths:
        return {}
    path = paths[-1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {**payload, "_path": str(path)} if isinstance(payload, dict) else {}


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        is not None
    )


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def parse_date(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def extract_capture_datetime(notes: Any, raw: Any) -> datetime | None:
    candidates: list[Any] = []
    text = str(notes or "")
    match = re.search(r"captured_at=([^;,\s]+)", text)
    if match:
        candidates.append(match.group(1))
    try:
        payload = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        payload = {}
    if isinstance(payload, dict):
        candidates.extend([payload.get("captured_at"), payload.get("canonicalized_at")])
        raw_notes = payload.get("notes")
        if raw_notes:
            match = re.search(r"captured_at=([^;,\s]+)", str(raw_notes))
            if match:
                candidates.append(match.group(1))
    for candidate in candidates:
        parsed = parse_datetime(candidate)
        if parsed:
            return parsed
    return None


def latest_price_capture_date(codex_run: Path) -> date | None:
    capture_dir = codex_run / "ccf-authorized-capture"
    candidates: list[date] = []
    for path in capture_dir.glob("ccf_dom_daily_*.csv"):
        captured = latest_capture_date_from_csv(path)
        if captured:
            candidates.append(captured)
        else:
            try:
                candidates.append(datetime.fromtimestamp(path.stat().st_mtime).date())
            except OSError:
                continue
    return max(candidates, default=None)


def latest_capture_date_from_csv(path: Path) -> date | None:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            dates = [parsed.date() for row in reader if (parsed := parse_datetime(row.get("captured_at") or ""))]
    except (OSError, UnicodeDecodeError, csv.Error):
        return None
    return max(dates, default=None)


def is_business_day(value: date) -> bool:
    return value.weekday() < 5


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.as_of:
        as_of = date.fromisoformat(args.as_of)
        evaluated_at = datetime.combine(as_of, time.max, tzinfo=UTC)
    else:
        evaluated_at = datetime.now(UTC)
        as_of = date.today()
    report = run_quality_gate(args.db, codex_run=args.codex_run, as_of=as_of, evaluated_at=evaluated_at)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"output": str(args.output), "overall_status": report["overall_status"], "alerts": len(report["alerts"])},
            ensure_ascii=False,
        )
    )
    if args.warn_only:
        return 0
    return 0 if report["overall_status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

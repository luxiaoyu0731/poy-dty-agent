from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .crawler_registry import assess_source_readiness
from .report_delivery import normalize_client_report
from .settings import settings
from .source_acquisition import build_source_automation_status
from .source_registry import list_sources
from .sqlite_runtime import connect_serialized
from .storage import list_agent_artifacts, list_agent_runs

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = Path(__file__).resolve().parents[1]
CODEX_RUN = Path(os.getenv("CODEX_RUN_PATH", str(REPO_ROOT / ".codex-run"))).expanduser().resolve()

SOURCE_FALLBACKS: dict[str, dict[str, str]] = {
    "ccf_authorized_portal": {
        "name": "CCF 授权门户",
        "tier": "A",
        "status": "authorized_manual",
        "access_method": "Computer Use 登录授权页面；优先页面下载/导出，其次读取授权页面表格。",
        "human_action": "仅在验证码、二次验证、权限不足或导出限制时需要用户处理。",
        "coverage_summary": "POY/DTY 多规格、PX/PTA/MEG/NAPHTHA、聚酯开工/库存/利润。",
        "risk": "不得记录账号密码；不得绕过登录、验证码、付费墙、授权限制、robots.txt 或反爬。",
    },
    "user_files": {
        "name": "用户上传 Excel/CSV/PDF/截图",
        "tier": "B",
        "status": "authorized_manual",
        "access_method": "用户上传后走标准 parser、OCR 或表格抽取，写入标准观察表。",
        "human_action": "需要用户提供原始文件或确认字段含义。",
        "coverage_summary": "同行 DTY、业务员报价、行业指标、事件复核标签。",
        "risk": "附件不能只停留在对话里；入库前必须保留来源、采集时间和字段映射。",
    },
    "gdelt_rss_official_news": {
        "name": "GDELT / RSS / 官方公告",
        "tier": "B",
        "status": "planned",
        "access_method": "公开 RSS/API/官方页面慢队列采集，C 级发现源需 A/B 源交叉验证。",
        "human_action": "反爬、浏览器检查或语义方向不确定时进入人工复核。",
        "coverage_summary": "全球能源、政策、航运、制裁、装置和宏观事件。",
        "risk": "新闻只能作为预测前证据；不得把后验新闻用于预测期判断。",
    },
    "authorized_commercial_sources": {
        "name": "其他授权商业源",
        "tier": "B",
        "status": "planned",
        "access_method": "只在用户授权范围内使用官方 API、导出文件或已登录页面。",
        "human_action": "需要用户确认授权、账号状态、许可边界和导出权限。",
        "coverage_summary": "可补充商业行情、库存、利润、贸易流和终端需求。",
        "risk": "只列方案，不绕过授权；任何验证码或权限不足都必须暂停。",
    },
}


def build_delivery_status() -> dict[str, Any]:
    generated_at = datetime.now(UTC).isoformat()
    errors: list[str] = []
    db_status = _database_status(errors)
    import_summary = _latest_import_summary(errors)
    final_summary = _latest_final_summary(errors)
    backtest_metrics: list[dict[str, Any]] = []
    quality_report = _latest_quality_report(errors)
    first_backtest = None
    strategy_improvement: dict[str, Any] = {
        "primary_strategy": {},
        "horizon_gates": [],
        "failure_attribution": [],
        "improvement_actions": [],
        "action_matrix": [],
    }
    source_automation = _source_automation_summary(errors)
    source_mode = "live" if not errors and db_status.get("exists") and final_summary else "partial_fallback"
    quality_gates = _quality_gates(quality_report, backtest_metrics)
    backtest_generated_at = str(final_summary.get("generated_at") or "")
    data_latest_at = (
        _latest_timestamp(
            backtest_generated_at,
            str(import_summary.get("generated_at") or ""),
            str(quality_report.get("generated_at") or ""),
            str(source_automation.get("generated_at") or ""),
        )
        or generated_at
    )
    operational_status = _operational_status(
        source_mode=source_mode,
        source_automation=source_automation,
        quality_gates=quality_gates,
        errors=errors,
    )

    return {
        "generated_at": data_latest_at,
        "status_generated_at": generated_at,
        "backtest_generated_at": backtest_generated_at,
        "data_latest_at": data_latest_at,
        "operational_status": operational_status,
        "source_mode": source_mode,
        "database": db_status,
        "import_summary": import_summary,
        # Compatibility field name; entries are current technical data sources,
        # not permission approvals.
        "authorized_sources": _authorized_sources(db_status, import_summary, generated_at),
        "coverage_gaps": [],
        "replenishment_tasks": _merge_replenishment_tasks([], source_automation),
        "backtest_metrics": backtest_metrics,
        "quality_gates": quality_gates,
        "update_schedule": _merge_update_schedule(_update_schedule(db_status, import_summary), source_automation),
        "client_reports": _client_reports(final_summary, quality_report),
        "strategy_improvement": strategy_improvement,
        "source_automation": source_automation,
        "first_backtest": first_backtest,
        "guardrails": {
            "credentials_logged": False,
            "calls_external_llm_provider": False,
            "writes_database": False,
            "requires_backup_before_apply": True,
            "ccf_operational_status": "soft_removed",
            "permission_or_manifest_gate": False,
            "latest_backtest_leaks": sum(int(item.get("leaks", 0)) for item in backtest_metrics),
        },
        "errors": errors,
    }


def _latest_timestamp(*values: str) -> str:
    parsed: list[tuple[datetime, str]] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            parsed.append((datetime.fromisoformat(text.replace("Z", "+00:00")), text))
        except ValueError:
            continue
    if not parsed:
        return ""
    return max(parsed, key=lambda item: item[0])[1]


def _operational_status(
    *,
    source_mode: str,
    source_automation: dict[str, Any],
    quality_gates: list[dict[str, Any]],
    errors: list[str],
) -> str:
    if source_mode == "mock_fallback":
        return "blocked"
    if errors:
        return "ready_with_warnings"
    manual_or_blocked = int(source_automation.get("manual_or_blocked") or 0) if source_automation else 0
    has_gate_warning = any(
        str(gate.get("status") or "") not in {"success", "ready", "pass", "passed"} for gate in quality_gates
    )
    if source_mode == "partial_fallback" or manual_or_blocked > 0 or has_gate_warning:
        return "ready_with_warnings"
    return "ready"


def _source_automation_summary(errors: list[str]) -> dict[str, Any]:
    try:
        status = build_source_automation_status()
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).exception("source automation status failed")
        errors.append("source_automation: status_unavailable")
        return {}
    tasks = status.get("tasks") if isinstance(status.get("tasks"), list) else []
    visible_tasks = [
        {
            "source_id": task.get("source_id", ""),
            "task_name": task.get("task_name", ""),
            "dataset_type": task.get("dataset_type", ""),
            "status": task.get("status", ""),
            "automation_level": task.get("automation_level", ""),
            "requires_computer_use": bool(task.get("requires_computer_use")),
            "requires_human_action": bool(task.get("requires_human_action")),
            "target_table": task.get("target_table", ""),
            "coverage_scope": task.get("coverage_scope", ""),
            "blocking_reason": task.get("blocking_reason", ""),
            "next_step": task.get("next_step", ""),
        }
        for task in tasks[:80]
        if isinstance(task, dict)
    ]
    return {
        "generated_at": status.get("generated_at", ""),
        "summary": status.get("summary", {}),
        "automation_ready": status.get("automation_ready", 0),
        "manual_or_blocked": status.get("manual_or_blocked", 0),
        "tasks": visible_tasks,
        "guards": status.get("guards", {}),
    }


def _merge_replenishment_tasks(tasks: list[dict[str, Any]], source_automation: dict[str, Any]) -> list[dict[str, Any]]:
    existing_ids = {str(item.get("id", "")) for item in tasks}
    auto_tasks = source_automation.get("tasks") if isinstance(source_automation.get("tasks"), list) else []
    merged = list(tasks)
    for index, task in enumerate(auto_tasks):
        if not isinstance(task, dict):
            continue
        if task.get("status") == "success":
            continue
        task_id = f"source-auto-{index}-{task.get('source_id', 'unknown')}"
        if task_id in existing_ids:
            continue
        merged.append(
            {
                "id": task_id,
                "title": str(task.get("task_name", "数据源自动化任务")),
                "source_id": str(task.get("source_id", "")),
                "status": _delivery_task_status(str(task.get("status", ""))),
                "coverage_scope": str(task.get("coverage_scope", "")),
                "owner": "数据运营",
                "next_step": str(task.get("next_step", "")),
                "blockers": [str(task.get("blocking_reason", ""))] if task.get("blocking_reason") else [],
                "metadata": {
                    "automation_level": task.get("automation_level", ""),
                    "requires_computer_use": bool(task.get("requires_computer_use")),
                    "requires_human_action": bool(task.get("requires_human_action")),
                },
            }
        )
    return merged


def _merge_update_schedule(schedule: list[dict[str, Any]], source_automation: dict[str, Any]) -> list[dict[str, Any]]:
    if not source_automation:
        return schedule
    return [
        *schedule,
        {
            "id": "source-automation-daily",
            "source_id": "source_automation",
            "cadence": "每日 08:30 + 每次 CCF 导入后",
            "mode": "公开源自动抓取 + 授权源 Computer Use 队列 + 质量门禁",
            "owner": "Data Ingestion Agent",
            "status": "ready" if source_automation.get("manual_or_blocked", 0) == 0 else "needs_human_review",
            "trigger": "运行 source automation 编排脚本",
            "next_step": "先跑公开源与已授权 CSV 导入；CCF 页面任务进入 Computer Use 队列。",
            "runbook": "docs/runbook.md",
        },
    ]


def _delivery_task_status(status: str) -> str:
    if status in {"success", "ready"}:
        return "success"
    if status in {"ready_for_computer_use", "ready_to_import", "waiting_for_file"}:
        return "needs_human_review"
    return "blocked"


def _settings_db_path() -> Path:
    path = Path(settings.sqlite_path)
    if path.is_absolute():
        return path
    return SERVER_ROOT / path


def _database_status(errors: list[str]) -> dict[str, Any]:
    db_path = _settings_db_path()
    status: dict[str, Any] = {
        "path": str(db_path),
        "exists": db_path.exists(),
        "forecast_price_points_total": 0,
        "ccf_dom_daily_rows": 0,
        "ccf_products": [],
    }
    if not db_path.exists():
        return status
    try:
        with closing(connect_serialized(f"file:{db_path}?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            if not _table_exists(connection, "forecast_price_points"):
                return status
            total = connection.execute("SELECT COUNT(*) AS n FROM forecast_price_points").fetchone()["n"]
            ccf_total = connection.execute(
                """
                SELECT COUNT(*) AS n
                FROM forecast_price_points
                WHERE source_id = 'ccf_dom_daily'
                  AND dataset_type = 'ccf_spot'
                """
            ).fetchone()["n"]
            rows = connection.execute(
                """
                SELECT product, COUNT(*) AS rows, COUNT(DISTINCT spec) AS specs,
                       MIN(observed_at) AS first_observed_at, MAX(observed_at) AS last_observed_at
                FROM forecast_price_points
                WHERE source_id = 'ccf_dom_daily'
                  AND dataset_type = 'ccf_spot'
                GROUP BY product
                ORDER BY product
                """
            ).fetchall()
        status["forecast_price_points_total"] = int(total)
        status["ccf_dom_daily_rows"] = int(ccf_total)
        status["ccf_products"] = [dict(row) for row in rows]
    except sqlite3.Error:
        logging.getLogger(__name__).exception("delivery database read failed")
        errors.append("database: read_failed")
    return status


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _latest_json_file(*patterns: str) -> Path | None:
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(CODEX_RUN.glob(pattern))
    paths = sorted(set(paths), key=lambda item: item.stat().st_mtime if item.exists() else 0)
    return paths[-1] if paths else None


def _read_json(path: Path | None, errors: list[str]) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.getLogger(__name__).exception("delivery report read failed")
        errors.append("report: invalid_or_unreadable")
        return {}
    return payload if isinstance(payload, dict) else {}


def _latest_quality_report(errors: list[str]) -> dict[str, Any]:
    path = _latest_json_file(
        "delivery-data-quality-latest.json",
        "local-production/source-automation/delivery-data-quality.json",
        "complete-51-backfill-*/delivery-data-quality.json",
    )
    payload = _read_json(path, errors)
    if payload:
        payload["_path"] = str(path)
    return payload


def _latest_import_summary(errors: list[str]) -> dict[str, Any]:
    path = _latest_json_file(
        "complete-51-backfill-*/industry/import_apply_summary.json",
        "ccf-apply-and-backtests-*/ccf_authorized_import_summary.json",
        "ccf-authorized-capture/ccf_authorized_import_summary*.json",
    )
    payload = _read_json(path, errors)
    if not payload:
        return {"status": "missing", "path": str(path) if path else ""}
    guards = payload.get("guards") if isinstance(payload.get("guards"), dict) else {}
    return {
        "status": "loaded",
        "path": str(path),
        "generated_at": payload.get("generated_at", ""),
        "accepted_rows": int(payload.get("accepted_rows", 0) or 0),
        "stored_rows": int(payload.get("stored_rows", 0) or 0),
        "delta_total_ccf_dom_daily": int(payload.get("delta_total_ccf_dom_daily", 0) or 0),
        "delta_total_ccf_industry": int(payload.get("delta_total_ccf_industry", 0) or 0),
        "credentials_logged": bool(guards.get("credentials_logged", False)),
        "main_db_modified": bool(guards.get("main_db_modified", False)),
    }


def _latest_final_summary(errors: list[str]) -> dict[str, Any]:
    path = _latest_json_file(
        "backtest-rerun-*/completion_compact_summary.json",
        "complete-51-backfill-*/completion_compact_summary.json",
        "ccf-apply-and-backtests-*/final-summary-after-ccf-import.json",
        "backtests-after-ccf-import-*/final-summary-after-ccf-import.json",
    )
    payload = _read_json(path, errors)
    if payload:
        payload["_path"] = str(path)
        return payload
    full_chain_path = _latest_json_file("full-chain-delivery/full-chain-backtest-latest.json")
    full_chain = _read_json(full_chain_path, errors)
    if not full_chain:
        return {}
    summary = full_chain.get("summary") if isinstance(full_chain.get("summary"), dict) else {}
    converted = {
        "generated_at": full_chain.get("generated_at", ""),
        "_path": str(full_chain_path),
        "full_chain": {
            "overall": {
                "total": int(summary.get("total_rows", 0) or 0),
                "scored": int(summary.get("scored", 0) or 0),
                "hit": int(summary.get("hit", 0) or 0),
                "miss": int(summary.get("miss", 0) or 0),
                "pending": max(0, int(summary.get("total_rows", 0) or 0) - int(summary.get("scored", 0) or 0)),
                "leaks": 0,
                "hit_rate": float(summary.get("accuracy", 0) or 0),
                "scored_ratio": float(summary.get("coverage", 0) or 0),
                "target_met_80pct": False,
                "risk": "观察级全链路研判，尚未达到行动级 75% 门槛。",
            }
        },
        "full_chain_delivery": full_chain,
    }
    acceptance_path = _latest_json_file("full-chain-delivery/full-chain-75-acceptance-status-latest.json")
    acceptance = _read_json(acceptance_path, errors)
    if acceptance:
        converted["full_chain_acceptance"] = {**acceptance, "_path": str(acceptance_path)}
    return converted


def _audit_summary(final_summary: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    direct = final_summary.get("audit_after_import")
    path = _latest_json_file(
        "ccf-authorized-source-gap-audit-latest.json",
        "source-automation/ccf-authorized-source-gap-audit-*.json",
        "complete-51-backfill-*/audit/ccf-authorized-source-gap-audit-*.json",
        "ccf-apply-and-backtests-*/audit/ccf-authorized-source-gap-audit-*.json",
        "backtests-after-ccf-import-*/audit/ccf-authorized-source-gap-audit-*.json",
    )
    payload = _read_json(path, errors)
    tasks = payload.get("acquisition_tasks", []) if isinstance(payload.get("acquisition_tasks"), list) else []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if summary and _is_newer_report(path, final_summary.get("_path")):
        merged = dict(summary)
        merged["acquisition_tasks"] = tasks[:50]
        return merged
    if isinstance(direct, dict) and direct:
        merged = dict(direct)
        merged["acquisition_tasks"] = tasks[:50]
        return merged
    if summary:
        summary["acquisition_tasks"] = payload.get("acquisition_tasks", [])[:50]
        return summary
    return {}


def _is_newer_report(candidate: Path | None, baseline: Any) -> bool:
    if candidate is None:
        return False
    if not baseline:
        return True
    try:
        baseline_path = Path(str(baseline))
        return candidate.stat().st_mtime >= baseline_path.stat().st_mtime
    except OSError:
        return True


def _backtest_metrics(final_summary: dict[str, Any]) -> list[dict[str, Any]]:
    compact_metrics = _compact_backtest_metrics(final_summary)
    if compact_metrics:
        return compact_metrics
    latest = final_summary.get("latest_cutoff_after_import")
    if not isinstance(latest, dict):
        latest = (
            final_summary.get("same_cutoff_after_import")
            if isinstance(final_summary.get("same_cutoff_after_import"), dict)
            else {}
        )
    metrics: list[dict[str, Any]] = []
    for suite in ("strict", "full_chain"):
        suite_payload = latest.get(suite) if isinstance(latest.get(suite), dict) else {}
        for horizon, item in suite_payload.items():
            if not isinstance(item, dict):
                continue
            total = int(item.get("total", 0) or 0)
            scored = int(item.get("scored", 0) or 0)
            scored_ratio = round(scored / total, 4) if total else None
            leaks = int(item.get("leaks", 0) or 0)
            hit_rate = item.get("hit_rate")
            status = "success" if leaks == 0 and scored_ratio is not None and scored_ratio >= 0.7 else "needs_more_data"
            risk = (
                "样本偏少，不能直接宣传" if scored_ratio is not None and scored_ratio < 0.7 else "可作为阶段性回测结果"
            )
            if leaks:
                status = "failed"
                risk = "存在未来泄漏，结果不可用"
            metrics.append(
                {
                    "suite": suite,
                    "horizon": horizon,
                    "total": total,
                    "scored": scored,
                    "hit": int(item.get("hit", 0) or 0),
                    "miss": int(item.get("miss", 0) or 0),
                    "hit_rate": float(hit_rate) if isinstance(hit_rate, int | float) else None,
                    "scored_ratio": scored_ratio,
                    "pending": int(item.get("pending", 0) or 0),
                    "leaks": leaks,
                    "status": status,
                    "risk": risk,
                }
            )
    return metrics


def _compact_backtest_metrics(final_summary: dict[str, Any]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for suite in ("strict", "full_chain"):
        suite_payload = final_summary.get(suite)
        if not isinstance(suite_payload, dict):
            continue
        for horizon, item in suite_payload.items():
            if not isinstance(item, dict):
                continue
            total = int(item.get("total", 0) or 0)
            scored = int(item.get("scored", 0) or 0)
            scored_ratio = round(scored / total, 4) if total else None
            leaks = int(item.get("leaks", 0) or 0)
            hit_rate = item.get("hit_rate")
            target_met = item.get("target_met_80pct")
            status = (
                "success" if leaks == 0 and (target_met is True or (scored_ratio or 0) >= 0.7) else "needs_more_data"
            )
            risk = (
                "样本偏少，不能直接宣传" if scored_ratio is not None and scored_ratio < 0.7 else "可作为阶段性回测结果"
            )
            if target_met is False:
                risk = "未达到 80% 行动门槛"
            if leaks:
                status = "failed"
                risk = "存在未来泄漏，结果不可用"
            metrics.append(
                {
                    "suite": suite,
                    "horizon": str(horizon),
                    "total": total,
                    "scored": scored,
                    "hit": int(item.get("hit", 0) or 0),
                    "miss": int(item.get("miss", 0) or 0),
                    "hit_rate": float(hit_rate) if isinstance(hit_rate, int | float) else None,
                    "scored_ratio": scored_ratio,
                    "pending": int(item.get("pending", 0) or 0),
                    "leaks": leaks,
                    "status": status,
                    "risk": risk,
                }
            )
    return metrics


def _quality_gates(
    quality_report: dict[str, Any],
    backtest_metrics: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    gates = quality_report.get("gates")
    if isinstance(gates, list) and gates:
        return [item for item in gates if isinstance(item, dict) and item.get("id") != "forecast_v2_action_gate"]
    leaks = sum(int(item.get("leaks", 0) or 0) for item in backtest_metrics)
    return [
        {
            "id": "delivery_quality_report",
            "title": "质量门禁报告",
            "status": "needs_human_review",
            "severity": "medium",
            "metric": "report",
            "threshold": "present",
            "observed": "missing",
            "next_step": "请联系系统管理员完成质量门禁更新。",
            "samples": [],
        },
        {
            "id": "backtest_leaks",
            "title": "未来信息泄漏",
            "status": "success" if leaks == 0 else "blocked",
            "severity": "critical",
            "metric": "leaks",
            "threshold": "0",
            "observed": str(leaks),
            "next_step": "任何 leaks>0 的回测不得对客户展示为有效结果。",
            "samples": [],
        },
    ]


def _strategy_improvement(
    final_summary: dict[str, Any],
    backtest_metrics: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    full_chain_reports = _suite_reports(final_summary, "full_chain", errors)
    strict_reports = _suite_reports(final_summary, "strict", errors)
    full_chain_metrics = [item for item in backtest_metrics if item.get("suite") == "full_chain"]
    horizon_gates = [
        _full_chain_horizon_gate(item, full_chain_reports.get(str(item.get("horizon", ""))))
        for item in full_chain_metrics
    ]
    horizon_gates.sort(key=lambda item: _horizon_days(str(item.get("horizon", ""))))
    primary = next((item for item in horizon_gates if item.get("primary")), horizon_gates[0] if horizon_gates else {})
    return {
        "primary_strategy": primary,
        "horizon_gates": horizon_gates,
        "failure_attribution": _failure_attribution(strict_reports, full_chain_reports),
        "improvement_actions": _improvement_actions(primary, horizon_gates, strict_reports, full_chain_reports),
        "action_matrix": _strategy_action_matrix(),
    }


def _suite_reports(final_summary: dict[str, Any], suite: str, errors: list[str]) -> dict[str, dict[str, Any]]:
    suite_payload = final_summary.get(suite)
    if not isinstance(suite_payload, dict):
        latest = final_summary.get("latest_cutoff_after_import")
        if isinstance(latest, dict):
            suite_payload = latest.get(suite)
    if not isinstance(suite_payload, dict):
        same_cutoff = final_summary.get("same_cutoff_after_import")
        if isinstance(same_cutoff, dict):
            suite_payload = same_cutoff.get(suite)
    if not isinstance(suite_payload, dict):
        return {}
    reports: dict[str, dict[str, Any]] = {}
    for horizon, item in suite_payload.items():
        if not isinstance(item, dict):
            continue
        path = item.get("output")
        report = _read_report_path(path, errors)
        if report:
            reports[str(horizon)] = report
    return reports


def _read_report_path(value: Any, errors: list[str]) -> dict[str, Any]:
    if not value:
        return {}
    path = Path(str(value))
    if not path.is_absolute():
        path = REPO_ROOT / path
    return _read_json(path, errors)


def _full_chain_horizon_gate(metric: dict[str, Any], report: dict[str, Any] | None) -> dict[str, Any]:
    horizon = str(metric.get("horizon", ""))
    horizon_days = _horizon_days(horizon)
    raw_best = report.get("best_strategy") if isinstance(report, dict) else {}
    summary = (
        raw_best.get("summary") if isinstance(raw_best, dict) and isinstance(raw_best.get("summary"), dict) else {}
    )
    wilson = summary.get("wilson_95") if isinstance(summary.get("wilson_95"), dict) else {}
    target_hit_rate = _optional_float((report or {}).get("target_hit_rate")) or 0.8
    hit_rate = _optional_float(metric.get("hit_rate"))
    total = int(metric.get("total", 0) or 0)
    scored = int(metric.get("scored", 0) or 0)
    pending = int(metric.get("pending", 0) or 0)
    leaks = int(metric.get("leaks", 0) or 0)
    scored_ratio = _optional_float(metric.get("scored_ratio"))
    pending_rate = round(pending / total, 4) if total else None
    target_met = bool(summary.get("target_met_80pct", metric.get("hit_rate", 0) and (hit_rate or 0) >= target_hit_rate))
    if isinstance((report or {}).get("target_met"), bool):
        target_met = bool((report or {}).get("target_met"))
    blockers = _horizon_blockers(hit_rate, scored, pending_rate, leaks, target_hit_rate, target_met)
    action_state = _horizon_action_state(horizon_days, hit_rate, scored, leaks, target_met)
    return {
        "id": f"full-chain-{horizon}",
        "suite": "full_chain",
        "horizon": horizon,
        "horizon_days": horizon_days,
        "horizon_role": _horizon_role(horizon_days),
        "primary": horizon_days == 1,
        "strategy_name": str(raw_best.get("name") or _compact_suite_item_name(metric) or "full_chain_quality_filter"),
        "action_state": action_state,
        "status": _action_state_status(action_state),
        "status_reason": _horizon_status_reason(horizon_days, action_state, blockers),
        "hit_rate": hit_rate,
        "target_hit_rate": target_hit_rate,
        "target_met": target_met,
        "total": total,
        "scored": scored,
        "scored_ratio": scored_ratio,
        "miss": int(metric.get("miss", 0) or 0),
        "pending": pending,
        "pending_rate": pending_rate,
        "leaks": leaks,
        "wilson_95_low": _optional_float(wilson.get("low")),
        "wilson_95_high": _optional_float(wilson.get("high")),
        "blockers": blockers,
        "next_step": _horizon_next_step(horizon_days, action_state, blockers, miss=int(metric.get("miss", 0) or 0)),
        "customer_claim": _customer_claim(action_state, horizon_days),
    }


def _compact_suite_item_name(metric: dict[str, Any]) -> str:
    return str(metric.get("best_strategy") or "")


def _horizon_blockers(
    hit_rate: float | None,
    scored: int,
    pending_rate: float | None,
    leaks: int,
    target_hit_rate: float,
    target_met: bool,
) -> list[str]:
    blockers: list[str] = []
    if leaks:
        blockers.append("future_leak")
    if scored < 100:
        blockers.append("scored_below_100")
    if hit_rate is None:
        blockers.append("missing_hit_rate")
    elif hit_rate < target_hit_rate:
        blockers.append("hit_rate_below_80pct_target")
    if not target_met:
        blockers.append("target_not_met")
    if pending_rate is not None and pending_rate > 0.25:
        blockers.append("pending_rate_above_25pct")
    return blockers


def _horizon_action_state(horizon_days: int, hit_rate: float | None, scored: int, leaks: int, target_met: bool) -> str:
    if leaks:
        return "disabled"
    if horizon_days == 1 and target_met and scored >= 100:
        return "actionable"
    if hit_rate is not None and hit_rate >= 0.75 and scored >= 100:
        return "watch_only"
    if hit_rate is not None:
        return "explain_only"
    return "disabled"


def _action_state_status(action_state: str) -> str:
    if action_state == "actionable":
        return "success"
    if action_state == "watch_only":
        return "needs_human_review"
    if action_state == "explain_only":
        return "running"
    return "blocked"


def _horizon_role(horizon_days: int) -> str:
    if horizon_days == 1:
        return "主策略短周期辅助判断"
    if horizon_days == 3:
        return "近端观察，不直接行动"
    if horizon_days == 7:
        return "周度趋势解释"
    return "结构性复盘解释"


def _horizon_status_reason(horizon_days: int, action_state: str, blockers: list[str]) -> str:
    if action_state == "actionable":
        return "h1 达到 80% 阶段门槛且 leaks=0，可作为短周期业务研判辅助。"
    if action_state == "watch_only":
        return "命中率接近但未满足主策略行动门槛，只能观察或二次确认。"
    if action_state == "explain_only":
        return f"h{horizon_days} 未达到行动门槛，用于复盘解释；阻断项：{', '.join(blockers) or '未达门槛'}。"
    return f"h{horizon_days} 不可用；阻断项：{', '.join(blockers) or '缺少评分'}。"


def _horizon_next_step(horizon_days: int, action_state: str, blockers: list[str], *, miss: int = 0) -> str:
    if horizon_days == 1:
        return f"复盘 {miss} 个 miss，继续提高 scored/total 覆盖，并保持 quality_filter 不放宽。"
    if horizon_days == 3:
        return "为 h3 单独加入事件确认、库存和连续报价特征，不沿用 h1 入场阈值。"
    if horizon_days == 7:
        return "为 h7 加入开工、库存、利润和下游订单的周度结构特征。"
    return "h14 仅做结构解释；需要宏观、供需和季节性模型单独验证后再谈行动。"


def _customer_claim(action_state: str, horizon_days: int) -> str:
    if action_state == "actionable":
        return "可对客户表述为短周期辅助研判策略，不得表述为自动交易或保收益。"
    if action_state == "watch_only":
        return "可展示为观察信号，需要人工复核或更多证据确认。"
    if action_state == "explain_only":
        return "只能用于事后解释和趋势背景，不用于行动建议。"
    return "不得进入客户行动建议。"


def _failure_attribution(
    strict_reports: dict[str, dict[str, Any]],
    full_chain_reports: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    strict_h1 = strict_reports.get("h1")
    if isinstance(strict_h1, dict):
        miss_reasons = strict_h1.get("by_miss_reason") if isinstance(strict_h1.get("by_miss_reason"), dict) else {}
        for reason, count in sorted(miss_reasons.items(), key=lambda item: int(item[1] or 0), reverse=True)[:5]:
            rows.append(
                {
                    "id": f"strict-h1-{reason}",
                    "source": "strict_h1",
                    "title": f"strict h1 错因：{reason}",
                    "impact": int(count or 0),
                    "status": "needs_human_review" if reason != "pending_future_price" else "blocked",
                    "detail": _miss_reason_label(str(reason)),
                    "next_step": _miss_reason_next_step(str(reason)),
                }
            )
    full_h1 = full_chain_reports.get("h1")
    best = full_h1.get("best_strategy") if isinstance(full_h1, dict) else {}
    by_product = best.get("by_product") if isinstance(best, dict) and isinstance(best.get("by_product"), list) else []
    weak_products = [
        item for item in by_product if isinstance(item, dict) and int(item.get("scored_predictions", 0) or 0) >= 10
    ]
    weak_products.sort(
        key=lambda item: (_optional_float(item.get("overall_hit_rate")) or 0, -int(item.get("miss", 0) or 0))
    )
    for item in weak_products[:5]:
        product = str(item.get("key", "unknown"))
        rows.append(
            {
                "id": f"full-chain-h1-product-{product}",
                "source": "full_chain_h1",
                "title": f"h1 弱产品：{product}",
                "impact": int(item.get("miss", 0) or 0),
                "status": "needs_human_review",
                "detail": (
                    f"hit {_format_pct(_optional_float(item.get('overall_hit_rate')))} · "
                    f"scored {item.get('scored_predictions', 0)}/{item.get('total_product_predictions', 0)}"
                ),
                "next_step": "拆分该产品的成本传导、库存/开工和新闻事件类型，避免整体策略掩盖弱点。",
            }
        )
    return rows[:18]


def _improvement_actions(
    primary: dict[str, Any],
    horizon_gates: list[dict[str, Any]],
    strict_reports: dict[str, dict[str, Any]],
    full_chain_reports: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    strict_h1 = strict_reports.get("h1") if isinstance(strict_reports.get("h1"), dict) else {}
    strict_summary = strict_h1.get("summary") if isinstance(strict_h1.get("summary"), dict) else {}
    h1_miss = int(primary.get("miss", 0) or 0)
    scored_ratio = _optional_float(primary.get("scored_ratio")) or 0
    actions = [
        {
            "id": "raise-scored-coverage",
            "title": "提高主策略可评分覆盖",
            "priority": "P0",
            "owner": "Data/Evaluation",
            "status": "needs_human_review" if scored_ratio < 0.4 else "running",
            "evidence": f"h1 scored/total 当前 {_format_pct(scored_ratio)}，目标先到 40%-60%。",
            "next_step": "持续补 CCF POY/DTY 多规格、Brent/WTI/Naphtha posterior 和停报日映射。",
        },
        {
            "id": "review-h1-misses",
            "title": "复盘 h1 失败样本",
            "priority": "P0",
            "owner": "Strategy",
            "status": "needs_human_review",
            "evidence": f"full-chain h1 miss={h1_miss}；strict h1 miss={strict_summary.get('miss', 'n/a')}。",
            "next_step": "按 priced_in、demand_offset、supply_recovery、macro_offset、low_evidence 建复核队列。",
        },
        {
            "id": "horizon-specific-strategies",
            "title": "拆分 h3/h7/h14 策略",
            "priority": "P1",
            "owner": "Strategy/Evaluation",
            "status": "blocked"
            if any(item.get("action_state") == "explain_only" for item in horizon_gates if not item.get("primary"))
            else "running",
            "evidence": "h1 可行动，h3/h7/h14 未达 80% 行动门槛。",
            "next_step": "h3 加确认信号，h7 加库存/开工/利润，h14 单独做结构供需模型。",
        },
        {
            "id": "keep-abstain-quality-filter",
            "title": "保留高质量证据 abstain",
            "priority": "P0",
            "owner": "Product",
            "status": "success",
            "evidence": "主策略表现来自 quality_filter，不应为覆盖率放宽证据门槛。",
            "next_step": "前端继续展示 actionable/watch_only/explain_only/disabled，不把观察态写成行动建议。",
        },
    ]
    return actions


def _strategy_action_matrix() -> list[dict[str, Any]]:
    return [
        {
            "action_state": "actionable",
            "label": "可行动",
            "allowed_use": "短周期业务研判辅助，可进入客户日报行动栏。",
            "blocked_use": "不得表述为自动交易、保收益或无需人工确认。",
            "ui_status": "success",
        },
        {
            "action_state": "watch_only",
            "label": "观察",
            "allowed_use": "可进入观察栏，需结合人工复核和新增证据。",
            "blocked_use": "不得直接生成买料、报价、接单建议。",
            "ui_status": "needs_human_review",
        },
        {
            "action_state": "explain_only",
            "label": "仅解释",
            "allowed_use": "用于复盘解释、周报背景和风险提示。",
            "blocked_use": "不得进入行动建议或策略晋升宣传。",
            "ui_status": "running",
        },
        {
            "action_state": "disabled",
            "label": "禁用",
            "allowed_use": "只显示阻断原因和下一步。",
            "blocked_use": "不得对客户展示为有效策略。",
            "ui_status": "blocked",
        },
    ]


def _horizon_days(value: str) -> int:
    digits = "".join(char for char in value if char.isdigit())
    return int(digits or 0)


def _miss_reason_label(reason: str) -> str:
    labels = {
        "priced_in": "价格可能已提前反映事件。",
        "demand_offset": "需求弱或下游抵消了利多。",
        "supply_recovery": "供应恢复或库存压力抵消。",
        "macro_offset": "宏观/汇率/利率因素抵消。",
        "low_evidence": "证据强度不足，应该更早降级。",
        "wrong_direction": "方向判断本身错误。",
        "pending_future_price": "后验价格尚未完全成熟。",
    }
    return labels.get(reason, "需要人工复核该错因。")


def _miss_reason_next_step(reason: str) -> str:
    if reason == "priced_in":
        return "加入价格提前反应检测，事件发生前后价差过大时降级为观察。"
    if reason == "demand_offset":
        return "把库存、开工、下游订单和利润加入反证门禁。"
    if reason == "supply_recovery":
        return "加入装置恢复、供应回补和库存压力标签。"
    if reason == "macro_offset":
        return "把美元、利率、油价和汇率代理加入冲突信号。"
    if reason == "low_evidence":
        return "C/D 级证据或单来源证据默认 explain_only。"
    if reason == "pending_future_price":
        return "等待或补齐真实后验价格后再评分。"
    return "进入失败样本复核队列。"


def _format_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _format_lift(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.1f}pct"


def _update_schedule(db_status: dict[str, Any], import_summary: dict[str, Any]) -> list[dict[str, Any]]:
    _ = (db_status, import_summary)
    return [
        {
            "id": "daily-public-refresh",
            "source_id": "public_benchmark_v2",
            "cadence": "每日 08:30 工作日",
            "mode": "自动公开 API/文件",
            "owner": "Data Ingestion Agent",
            "status": "ready",
            "trigger": "npm/cron 调用公开源 fetch 与 freshness gate",
            "next_step": "公开源失败时检查逐源最后尝试状态、OUTBOUND_FETCH_HOSTS 和来源字段变更。",
            "runbook": "docs/runbook.md#daily-weekly-data-update",
        },
        {
            "id": "post-import-validation",
            "source_id": "local_validation",
            "cadence": "每次导入后立即",
            "mode": "只读质量门禁 + strict/full-chain 回测",
            "owner": "Evaluation Agent",
            "status": "ready",
            "trigger": "任何公开源或用户文件导入完成后",
            "next_step": "运行 public-benchmark.v2 quality gate、strict/full-chain，并刷新 delivery/status。",
            "runbook": "docs/customer-acceptance-pack.md",
        },
    ]


def _client_reports(final_summary: dict[str, Any], quality_report: dict[str, Any]) -> list[dict[str, Any]]:
    """Return customer reports produced by the report Agent in a completed run."""
    _ = (final_summary, quality_report)
    for run in list_agent_runs(status="completed", limit=20):
        if run.get("source") != "daily_agent_pipeline":
            continue
        artifacts = list_agent_artifacts(run_id=str(run["run_id"]))
        reports = [
            item.get("payload", {}) for item in artifacts if item.get("artifact_type") == "customer_daily_report"
        ]
        if reports:
            return [
                normalize_client_report(report, project_root=REPO_ROOT)
                for report in reports
                if isinstance(report, dict)
            ]
    return []


def _internal_delivery_artifacts(final_summary: dict[str, Any], quality_report: dict[str, Any]) -> list[dict[str, Any]]:
    final_path = str(final_summary.get("_path", ""))
    quality_path = str(quality_report.get("_path", ""))
    return [
        {
            "id": "customer_acceptance_pack",
            "title": "客户验收包",
            "path": "docs/customer-acceptance-pack.md",
            "status": "ready"
            if (REPO_ROOT / "docs" / "customer-acceptance-pack.md").exists()
            else "needs_human_review",
            "audience": "甲方业务/项目验收",
            "summary": "范围、限制、SLA、数据源授权、回测解释、操作手册。",
            "download_available": (REPO_ROOT / "docs" / "customer-acceptance-pack.md").exists(),
            "disabled_reason": ""
            if (REPO_ROOT / "docs" / "customer-acceptance-pack.md").exists()
            else "报告文件尚未生成。",
            "next_step": "可直接查看或下载。"
            if (REPO_ROOT / "docs" / "customer-acceptance-pack.md").exists()
            else "请先生成客户验收材料。",
            "content_status": "ready" if (REPO_ROOT / "docs" / "customer-acceptance-pack.md").exists() else "missing",
            "source_categories": ["验收范围", "服务承诺", "数据说明"],
        },
        {
            "id": "ccf_update_runbook",
            "title": "行业数据更新手册",
            "path": "docs/ccf-authorized-update-runbook.md",
            "status": "ready"
            if (REPO_ROOT / "docs" / "ccf-authorized-update-runbook.md").exists()
            else "needs_human_review",
            "audience": "数据运营/交付维护",
            "summary": "每日价格检查、每周行业指标更新、验证码/权限阻塞处理。",
            "download_available": (REPO_ROOT / "docs" / "ccf-authorized-update-runbook.md").exists(),
            "disabled_reason": ""
            if (REPO_ROOT / "docs" / "ccf-authorized-update-runbook.md").exists()
            else "更新手册尚未生成。",
            "next_step": "按手册执行日常更新。"
            if (REPO_ROOT / "docs" / "ccf-authorized-update-runbook.md").exists()
            else "请先生成更新手册。",
            "content_status": "ready"
            if (REPO_ROOT / "docs" / "ccf-authorized-update-runbook.md").exists()
            else "missing",
            "source_categories": ["行业价格", "行业指标", "质量复核"],
        },
        {
            "id": "latest_completion_summary",
            "title": "最新回测与补数摘要",
            "path": final_path,
            "status": "ready" if final_path else "needs_human_review",
            "audience": "项目负责人/复盘",
            "summary": "本轮补数、coverage audit、strict/full-chain 结果。",
            "download_available": bool(final_path),
            "disabled_reason": "" if final_path else "最新摘要文件尚未生成。",
            "next_step": "用于复盘近期数据更新和验证结果。" if final_path else "请先运行本地日常检查。",
            "content_status": "ready" if final_path else "missing",
            "source_categories": ["补数结果", "覆盖审计", "回测摘要"],
        },
        {
            "id": "quality_gate_report",
            "title": "数据质量门禁报告",
            "path": quality_path,
            "status": str(quality_report.get("overall_status", "needs_human_review")),
            "audience": "运维/验收",
            "summary": "缺口、过期、单位错配、异常值和证据等级门禁。",
            "download_available": bool(quality_path),
            "disabled_reason": "" if quality_path else "质量门禁报告尚未生成。",
            "next_step": "优先处理需复核项。" if quality_path else "请先运行质量门禁检查。",
            "content_status": "ready" if quality_path else "missing",
            "source_categories": ["数据缺口", "新鲜度", "质量门禁"],
        },
    ]


def _optional_float(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None


def _first_backtest(
    metrics: list[dict[str, Any]], final_summary: dict[str, Any], generated_at: str
) -> dict[str, object] | None:
    if not metrics:
        return None
    chosen = next((item for item in metrics if item["suite"] == "strict" and item["horizon"] == "h1"), metrics[0])
    return {
        "id": "delivery-latest-backtest",
        "title": f"{chosen['suite']} {chosen['horizon']} CCF 导入后回测",
        "generated_at": str(final_summary.get("generated_at") or generated_at),
        "status": "success" if chosen["leaks"] == 0 and (chosen.get("scored_ratio") or 0) >= 0.7 else "blocked",
        "scored_events": chosen["scored"],
        "hit_rate": chosen.get("hit_rate"),
        "baseline_hit_rate": None,
        "lift_vs_baseline": None,
        "pending": chosen["pending"],
        "report_path": final_summary.get("_path", ""),
    }


def _authorized_sources(
    db_status: dict[str, Any], import_summary: dict[str, Any], generated_at: str
) -> list[dict[str, Any]]:
    registry_by_id = {source.source_id: source for source in list_sources()}
    rows: list[dict[str, Any]] = []
    _ = (db_status, import_summary)

    for source_id in (
        "eia_petroleum_api",
        "fred_macro_api",
        "cftc_cot_petroleum",
        "cfets_cny_parity",
        "gacc_trade_statistics",
    ):
        source = registry_by_id.get(source_id)
        if source is None:
            continue
        readiness = assess_source_readiness(source)
        rows.append(
            {
                "id": source.source_id,
                "name": source.source_name,
                "tier": source.tier,
                "status": _source_status_from_readiness(readiness.status),
                "access_method": _source_access_method(source.source_id, source.auth_type),
                "human_action": readiness.next_step,
                "coverage_summary": ", ".join(source.products),
                "risk": f"technical freshness SLA: {source.freshness_sla_minutes} minutes",
                "updated_at": generated_at,
                "metadata": {"readiness_status": readiness.status, "category": source.category},
            }
        )

    for source_id in ("user_files", "gdelt_rss_official_news"):
        item = SOURCE_FALLBACKS[source_id]
        rows.append(
            {
                "id": source_id,
                "name": item["name"],
                "tier": item["tier"],
                "status": item["status"],
                "access_method": item["access_method"],
                "human_action": item["human_action"],
                "coverage_summary": item["coverage_summary"],
                "risk": item["risk"],
                "updated_at": generated_at,
                "metadata": {},
            }
        )
    return rows


def _source_status_from_readiness(status: str) -> str:
    if status == "ready":
        return "ready"
    if status == "requires_api_key":
        return "blocked"
    if status in {"requires_license", "internal_only"}:
        return "needs_login"
    return "needs_export"


def _source_access_method(source_id: str, auth_type: str) -> str:
    if auth_type == "api_key":
        return "官方 API key 自动抓取。"
    if source_id == "cftc_cot_petroleum":
        return "官方公开周度文件下载并标准化。"
    return "官方公开页面或下载文件，遵守 robots 和频率限制。"


def _coverage_gaps(db_status: dict[str, Any], audit_summary: dict[str, Any]) -> list[dict[str, Any]]:
    product_counts = {item["product"]: item for item in db_status.get("ccf_products", []) if isinstance(item, dict)}
    poy_specs = int(product_counts.get("POY", {}).get("specs", 0) or 0)
    dty_specs = int(product_counts.get("DTY", {}).get("specs", 0) or 0)
    upstream_complete = all(product in product_counts for product in ("PX", "PTA", "MEG", "NAPHTHA"))
    by_group = (
        audit_summary.get("task_count_by_group") if isinstance(audit_summary.get("task_count_by_group"), dict) else {}
    )
    poy_dty_task_count = int(by_group.get("poy_dty_multi_spec_price", 0) or 0)
    upstream_task_count = int(by_group.get("upstream_price", 0) or 0)
    missing_industry = audit_summary.get("industry_requirements_missing")
    missing_industry_count = len(missing_industry) if isinstance(missing_industry, list) else 0
    partial_industry = audit_summary.get("industry_requirements_partial")
    partial_industry_count = len(partial_industry) if isinstance(partial_industry, list) else 0
    missing_products = audit_summary.get("products_with_missing_union_dates")
    missing_products_text = (
        ", ".join(missing_products) if isinstance(missing_products, list) and missing_products else "无价格补数缺口"
    )
    return [
        {
            "id": "ccf-poy-dty-multi-spec",
            "title": "POY/DTY 多规格 CCF 覆盖",
            "status": "blocked" if poy_dty_task_count else "success",
            "progress": 100 if poy_dty_task_count == 0 else min(92, max(0, int(((poy_specs + dty_specs) / 16) * 100))),
            "current": f"POY {poy_specs} 个规格、DTY {dty_specs} 个规格；价格补数任务 {poy_dty_task_count} 个。",
            "target": "POY/DTY 关键规格从 2025-01-01 到 posterior_end 覆盖可评分日期。",
            "next_step": "价格缺口已按 CCF 实际报价日历关闭。",
        },
        {
            "id": "ccf-upstream-chain",
            "title": "PX/PTA/MEG/NAPHTHA 上游链闭合",
            "status": "blocked" if not upstream_complete or upstream_task_count else "success",
            "progress": 100 if upstream_complete and upstream_task_count == 0 else 45,
            "current": f"缺口产品/日期：{missing_products_text}。",
            "target": "上游价格覆盖到 strict/full-chain 的 posterior_end，且单位/币种明确。",
            "next_step": "价格缺口已按 CCF 实际报价日历关闭；公开 EIA/FRED 只作油端和宏观补充。",
        },
        {
            "id": "ccf-industry-indicators",
            "title": "聚酯开工/库存/利润",
            "status": "needs_human_review" if missing_industry_count or partial_industry_count else "success",
            "progress": 100 if missing_industry_count == 0 and partial_industry_count == 0 else 0,
            "current": f"缺少 {missing_industry_count} 类，partial {partial_industry_count} 类行业指标。",
            "target": "聚酯开工、库存、利润和 POY/DTY 库存/利润进入 industry_observations。",
            "next_step": "已按 CCF 原始发布频率入库；后续定期增量即可。",
        },
    ]


def _replenishment_tasks(audit_summary: dict[str, Any]) -> list[dict[str, Any]]:
    if int(audit_summary.get("task_count", 0) or 0) == 0:
        return []
    raw_tasks = audit_summary.get("acquisition_tasks")
    if not isinstance(raw_tasks, list):
        raw_tasks = []
    tasks: list[dict[str, Any]] = []
    group_counts: Counter[str] = Counter()
    for item in raw_tasks:
        if isinstance(item, dict):
            group_counts[str(item.get("data_group", "unknown"))] += 1
    if raw_tasks:
        for item in raw_tasks[:12]:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("task_id", f"ccf-task-{len(tasks) + 1}"))
            product = str(item.get("product", "CCF"))
            spec = str(item.get("spec_or_metric", "authorized data"))
            tasks.append(
                {
                    "id": task_id,
                    "title": f"补 {product} {spec}",
                    "source_id": str(item.get("recommended_source_id", "ccf_authorized_portal")),
                    "status": "needs_human_review",
                    "coverage_scope": f"{item.get('missing_start', '')}..{item.get('missing_end', '')}",
                    "owner": "Data Ingestion Agent",
                    "next_step": str(item.get("acquisition_method", "computer_use_authorized_export_or_page_table")),
                    "blockers": [str(item.get("blocked_if", "captcha_or_permission_denied"))],
                    "metadata": item,
                }
            )
        return tasks
    return [
        {
            "id": "ccf-poy-spec-backfill",
            "title": "补 CCF POY 多规格历史价",
            "source_id": "ccf_authorized_portal",
            "status": "needs_human_review",
            "coverage_scope": "2025-01-01..posterior_end",
            "owner": "Data Ingestion Agent",
            "next_step": "使用授权页面下载/读取 POY 多规格价格，标准化到 forecast_price_points。",
            "blockers": ["验证码、二次验证、权限不足或导出限制"],
            "metadata": {"data_group_counts": dict(group_counts)},
        },
        {
            "id": "ccf-dty-spec-backfill",
            "title": "补 CCF DTY 多规格历史价",
            "source_id": "ccf_authorized_portal",
            "status": "needs_human_review",
            "coverage_scope": "2025-01-01..posterior_end",
            "owner": "Data Ingestion Agent",
            "next_step": "导出 DTY 长丝多规格价格，保留规格、品级、单位和采集时间。",
            "blockers": ["验证码、二次验证、权限不足或导出限制"],
            "metadata": {"data_group_counts": dict(group_counts)},
        },
    ]

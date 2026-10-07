from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .settings import settings
from .source_automation_policy import assess_readiness, get_source_policy, source_run_succeeded
from .source_registry import list_sources
from .sqlite_runtime import connect_serialized

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = Path(__file__).resolve().parents[1]
CODEX_RUN = REPO_ROOT / ".codex-run"

ACQUISITION_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS source_acquisition_runs (
  acquisition_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  task_name TEXT NOT NULL,
  dataset_type TEXT NOT NULL,
  status TEXT NOT NULL,
  automation_level TEXT NOT NULL,
  requires_computer_use INTEGER NOT NULL,
  requires_human_action INTEGER NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  target_table TEXT NOT NULL,
  expected_fields TEXT NOT NULL,
  blocking_reason TEXT NOT NULL,
  next_step TEXT NOT NULL,
  evidence_path TEXT NOT NULL,
  summary TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_source_acquisition_runs_latest
ON source_acquisition_runs(source_id, dataset_type, created_at DESC);
"""

REQUIRED_CCF_PRICE_PRODUCTS = {
    "POY": {"min_specs": 4, "target_table": "forecast_price_points"},
    "DTY": {"min_specs": 3, "target_table": "forecast_price_points"},
    "PX": {"min_specs": 1, "target_table": "forecast_price_points"},
    "PTA": {"min_specs": 1, "target_table": "forecast_price_points"},
    "MEG": {"min_specs": 1, "target_table": "forecast_price_points"},
    "NAPHTHA": {"min_specs": 1, "target_table": "forecast_price_points"},
}

REQUIRED_CCF_INDUSTRY_KEYS = {
    "POLYESTER|polyester_operating_rate|weekly",
    "POY|poy_inventory|weekly",
    "DTY|dty_inventory|weekly",
    "POLYESTER|polyester_profit|daily",
    "POY|poy_profit|daily",
    "DTY|dty_profit|daily",
}

PUBLIC_SOURCE_IDS = {
    "eia_petroleum_api",
    "fred_macro_api",
    "cftc_cot_petroleum",
    "czce_pta_px",
    "ofac_sanctions",
    "opec_press",
    "gacc_trade_statistics",
    "un_comtrade_api",
    "tnc_polyester_history",
}


def build_source_automation_status(*, db_path: Path | None = None, codex_run: Path | None = None) -> dict[str, Any]:
    db = db_path or _settings_db_path()
    run_root = codex_run or CODEX_RUN
    errors: list[str] = []
    inventory = inspect_source_inventory(db, errors)
    plan = build_acquisition_plan(db_path=db, codex_run=run_root, inventory=inventory)
    latest_runs = list_acquisition_runs(db_path=db, limit=50)
    summary = Counter(item["status"] for item in plan)
    not_ready = [item for item in plan if item["status"] not in {"success", "ready", "ready_to_import"}]
    manual_or_blocked = [item for item in plan if _requires_manual_action(item)]
    return {
        "generated_at": _now(),
        "database": {"path": str(db), "exists": db.exists()},
        "summary": dict(summary),
        "automation_ready": sum(1 for item in plan if item["automation_level"] in {"full", "semi"}),
        "manual_or_blocked": len(manual_or_blocked),
        "not_ready": len(not_ready),
        "tasks": plan,
        "latest_runs": latest_runs,
        "inventory": inventory,
        "legacy_sources": {
            "ccf": {
                "status": "soft_removed",
                "historical_rows_read_only": True,
                "scheduled": False,
                "writes_database": False,
            },
            "dce_meg": {
                "status": "soft_removed",
                "historical_rows_read_only": True,
                "scheduled": False,
                "writes_database": False,
                "current_target_gap": None,
                "current_target_replacement": "sunsirs_public_commodity_assessment",
            },
        },
        "guards": {
            "credentials_logged": False,
            "calls_external_llm_provider": False,
            "ccf_operational_status": "soft_removed",
            "dce_operational_status": "soft_removed",
            "meg_current_source_gap": False,
            "meg_current_label_source": "sunsirs_public_commodity_assessment",
            "writes_database": False,
            "requires_backup_before_db_write": True,
        },
        "errors": errors,
    }


def build_acquisition_plan(
    *,
    db_path: Path | None = None,
    codex_run: Path | None = None,
    inventory: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    db = db_path or _settings_db_path()
    run_root = codex_run or CODEX_RUN
    data = inventory or inspect_source_inventory(db, [])
    tasks: list[dict[str, Any]] = []
    tasks.extend(_public_source_tasks(data, db))
    tasks.extend(_user_file_tasks(run_root))
    tasks.extend(_news_source_tasks())
    return tasks


def inspect_source_inventory(db_path: Path, errors: list[str] | None = None) -> dict[str, Any]:
    issues = errors if errors is not None else []
    inventory: dict[str, Any] = {
        "ccf_price": {},
        "ccf_industry": {},
        "market_observations": {},
        "news": {},
        "events": {},
        "event_observations": {},
        "futures_daily_bars": {},
    }
    if not db_path.exists():
        return inventory
    try:
        with closing(connect_serialized(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=1000")
            if _table_exists(connection, "forecast_price_points"):
                rows = connection.execute(
                    """
                    SELECT product, COUNT(*) AS rows, COUNT(DISTINCT spec) AS specs,
                           MIN(observed_at) AS first_observed_at,
                           MAX(observed_at) AS last_observed_at
                    FROM forecast_price_points
                    WHERE source_id = 'ccf_dom_daily'
                      AND dataset_type = 'ccf_spot'
                    GROUP BY product
                    ORDER BY product
                    """
                ).fetchall()
                inventory["ccf_price"] = {row["product"]: dict(row) for row in rows}
            if _table_exists(connection, "industry_observations"):
                industry_columns = _table_columns(connection, "industry_observations")
                rows = connection.execute(
                    """
                    SELECT product, metric, frequency, COUNT(*) AS rows,
                           MIN(observed_at) AS first_observed_at,
                           MAX(observed_at) AS last_observed_at
                    FROM industry_observations
                    WHERE source_id = 'ccf_dom_daily'
                    GROUP BY product, metric, frequency
                    """
                ).fetchall()
                inventory["ccf_industry"] = {
                    f"{row['product']}|{row['metric']}|{row['frequency']}": dict(row) for row in rows
                }
                if {"created_at", "notes", "raw"}.issubset(industry_columns):
                    capture_rows = connection.execute(
                        """
                        SELECT product, metric, frequency, created_at, notes, raw
                        FROM industry_observations
                        WHERE source_id = 'ccf_dom_daily'
                        """
                    ).fetchall()
                    for row in capture_rows:
                        key = f"{row['product']}|{row['metric']}|{row['frequency']}"
                        item = inventory["ccf_industry"].get(key)
                        if not isinstance(item, dict):
                            continue
                        captured_at = _extract_capture_datetime(row["notes"], row["raw"]) or _parse_datetime(
                            row["created_at"]
                        )
                        current = _parse_datetime(item.get("last_captured_at"))
                        if captured_at and (current is None or captured_at > current):
                            item["last_captured_at"] = captured_at.isoformat()
            if _table_exists(connection, "market_observations"):
                market_rows = connection.execute(
                    """
                    SELECT source_id, COUNT(*) AS rows, MAX(observed_at) AS last_observed_at
                    FROM market_observations
                    GROUP BY source_id
                    """
                ).fetchall()
                inventory["market_observations"] = {row["source_id"]: dict(row) for row in market_rows}
            if _table_exists(connection, "futures_daily_bars"):
                futures_rows = connection.execute(
                    """
                    SELECT source_id, COUNT(*) AS rows, MAX(trade_date) AS last_trade_date
                    FROM futures_daily_bars
                    GROUP BY source_id
                    """
                ).fetchall()
                inventory["futures_daily_bars"] = {row["source_id"]: dict(row) for row in futures_rows}
            if _table_exists(connection, "news_articles"):
                news_rows = connection.execute(
                    """
                    SELECT source_id, COUNT(*) AS rows, MAX(published_at) AS last_published_at
                    FROM news_articles
                    GROUP BY source_id
                    """
                ).fetchall()
                inventory["news"] = {row["source_id"]: dict(row) for row in news_rows}
            if _table_exists(connection, "event_observations"):
                event_count = connection.execute(
                    "SELECT COUNT(*) AS n, MAX(occurred_at) AS latest FROM event_observations"
                ).fetchone()
                inventory["events"] = dict(event_count) if event_count else {}
                event_rows = connection.execute(
                    """
                    SELECT source_id, COUNT(*) AS rows, MAX(occurred_at) AS last_occurred_at
                    FROM event_observations
                    GROUP BY source_id
                    """
                ).fetchall()
                inventory["event_observations"] = {row["source_id"]: dict(row) for row in event_rows}
    except sqlite3.Error:
        logging.getLogger(__name__).exception("source inventory read failed")
        issues.append("source inventory: read_failed")
    return inventory


def record_acquisition_run(
    *,
    db_path: Path | None = None,
    source_id: str,
    task_name: str,
    dataset_type: str,
    status: str,
    automation_level: str,
    requires_computer_use: bool,
    requires_human_action: bool,
    start_date: str = "",
    end_date: str = "",
    target_table: str = "",
    expected_fields: list[str] | None = None,
    blocking_reason: str = "",
    next_step: str = "",
    evidence_path: str = "",
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    db = db_path or _settings_db_path()
    now = _now()
    payload = {
        "acquisition_id": str(uuid4()),
        "created_at": now,
        "updated_at": now,
        "source_id": source_id,
        "task_name": task_name,
        "dataset_type": dataset_type,
        "status": status,
        "automation_level": automation_level,
        "requires_computer_use": int(requires_computer_use),
        "requires_human_action": int(requires_human_action),
        "start_date": start_date,
        "end_date": end_date,
        "target_table": target_table,
        "expected_fields": json.dumps(expected_fields or [], ensure_ascii=False),
        "blocking_reason": blocking_reason,
        "next_step": next_step,
        "evidence_path": evidence_path,
        "summary": json.dumps(summary or {}, ensure_ascii=False),
    }
    with closing(connect_serialized(db, timeout=30.0)) as connection, connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.executescript(ACQUISITION_SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO source_acquisition_runs (
              acquisition_id, created_at, updated_at, source_id, task_name, dataset_type, status,
              automation_level, requires_computer_use, requires_human_action, start_date, end_date,
              target_table, expected_fields, blocking_reason, next_step, evidence_path, summary
            ) VALUES (
              :acquisition_id, :created_at, :updated_at, :source_id, :task_name, :dataset_type, :status,
              :automation_level, :requires_computer_use, :requires_human_action, :start_date, :end_date,
              :target_table, :expected_fields, :blocking_reason, :next_step, :evidence_path, :summary
            )
            """,
            payload,
        )
    return _decode_run_row(payload)


def list_acquisition_runs(*, db_path: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    db = db_path or _settings_db_path()
    if not db.exists():
        return []
    try:
        with closing(connect_serialized(f"file:{db}?mode=ro", uri=True, timeout=1.0)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=1000")
            if not _table_exists(connection, "source_acquisition_runs"):
                return []
            rows = connection.execute(
                "SELECT * FROM source_acquisition_runs ORDER BY created_at DESC LIMIT ?",
                (min(max(limit, 1), 200),),
            ).fetchall()
    except sqlite3.Error:
        return []
    return [_decode_run_row(dict(row)) for row in rows]


def _ccf_price_tasks(inventory: dict[str, Any], codex_run: Path) -> list[dict[str, Any]]:
    rows = inventory.get("ccf_price") if isinstance(inventory.get("ccf_price"), dict) else {}
    pending_csv = sorted((codex_run / "ccf-authorized-capture").glob("ccf_dom_daily_*.csv"))
    today = date.today()
    is_workday = today.weekday() < 5
    fresh_pending_csv = [path for path in pending_csv if datetime.fromtimestamp(path.stat().st_mtime).date() == today]
    tasks = []
    for product, requirement in REQUIRED_CCF_PRICE_PRODUCTS.items():
        item = rows.get(product) if isinstance(rows, dict) else None
        specs = int(item.get("specs", 0) or 0) if isinstance(item, dict) else 0
        latest = str(item.get("last_observed_at", "")) if isinstance(item, dict) else ""
        min_specs = int(requirement["min_specs"])
        has_coverage = specs >= min_specs and bool(latest)
        needs_capture = is_workday and not fresh_pending_csv
        status = "success" if has_coverage and not needs_capture else "ready_for_computer_use"
        next_step = "工作日已有今日授权价格 CSV；继续每日自动检查。"
        blocking = ""
        if fresh_pending_csv and status != "success":
            status = "ready_to_import"
            next_step = "已有授权 CSV，运行导入和质量门禁。"
        elif latest and not is_workday:
            next_step = "非工作日跳过；下个工作日再执行授权价格采集。"
        elif status != "success":
            next_step = "用 Computer Use 打开 CCF 授权页面，选择品种、滑块验证、查询后导出或读取表格。"
            blocking = (
                "工作日要求每日执行 CCF 价格授权页面检查/采集。"
                if has_coverage and needs_capture
                else "后台脚本不能自行处理授权页面滑块；需要 Computer Use 会话执行。"
            )
        tasks.append(
            _task(
                source_id="ccf_dom_daily",
                task_name=f"CCF {product} 价格补数",
                dataset_type="ccf_spot",
                status=status,
                automation_level="semi",
                requires_computer_use=status in {"ready_for_computer_use", "ready_to_import"},
                requires_human_action=False,
                target_table=str(requirement["target_table"]),
                expected_fields=["产品", "日期", "均价", "source_url", "captured_at"],
                coverage_scope=(
                    f"{product}: specs={specs}/{min_specs}; latest={latest or 'missing'}; "
                    f"fresh_pending_csv={len(fresh_pending_csv)}"
                ),
                blocking_reason=blocking,
                next_step=next_step,
            )
        )
    return tasks


def _ccf_industry_tasks(inventory: dict[str, Any], codex_run: Path) -> list[dict[str, Any]]:
    rows = inventory.get("ccf_industry") if isinstance(inventory.get("ccf_industry"), dict) else {}
    pending_csv = sorted((codex_run / "ccf-authorized-capture").glob("ccf_industry_*.csv"))
    today = date.today()
    is_workday = today.weekday() < 5
    fresh_pending_csv = [path for path in pending_csv if datetime.fromtimestamp(path.stat().st_mtime).date() == today]
    tasks = []
    for key in sorted(REQUIRED_CCF_INDUSTRY_KEYS):
        item = rows.get(key) if isinstance(rows, dict) else None
        latest = str(item.get("last_observed_at", "")) if isinstance(item, dict) else ""
        captured = str(item.get("last_captured_at", "")) if isinstance(item, dict) else ""
        captured_dt = _parse_datetime(captured)
        captured_date = captured_dt.date() if captured_dt else None
        needs_capture = is_workday and captured_date != today
        status = "success" if latest and not needs_capture else "ready_for_computer_use"
        if fresh_pending_csv and status != "success":
            status = "ready_to_import"
        blocking_reason = ""
        if not latest:
            blocking_reason = "需要在授权页面导出或读取开工、库存、利润表。"
        elif needs_capture:
            blocking_reason = "工作日要求每日执行 CCF 行业指标授权页面检查/采集。"
        tasks.append(
            _task(
                source_id="ccf_dom_daily",
                task_name=f"CCF 行业指标补数：{key}",
                dataset_type="ccf_industry",
                status=status,
                automation_level="semi",
                requires_computer_use=status in {"ready_for_computer_use", "ready_to_import"},
                requires_human_action=False,
                target_table="industry_observations",
                expected_fields=["observed_at", "product", "metric", "value", "unit", "frequency"],
                coverage_scope=(
                    f"{key}: latest={latest or 'missing'}; captured={captured or 'missing'}; "
                    f"fresh_pending_csv={len(fresh_pending_csv)}"
                ),
                blocking_reason=blocking_reason,
                next_step=(
                    "非工作日跳过；下个工作日再执行授权页面检查。"
                    if latest and not is_workday
                    else "已有今日采集文件则自动导入；否则用 Computer Use 采集授权页面表格。"
                ),
            )
        )
    return tasks


def _public_source_tasks(inventory: dict[str, Any], db_path: Path) -> list[dict[str, Any]]:
    tasks = []
    run_states = _public_source_run_states(db_path)
    observations = inventory.get("market_observations", {})
    futures_bars = inventory.get("futures_daily_bars", {})
    event_sources = inventory.get("event_observations", {})
    news_sources = inventory.get("news", {})
    for source in list_sources():
        if source.source_id not in PUBLIC_SOURCE_IDS:
            continue
        policy = get_source_policy(source.source_id)
        requires_key = (
            source.auth_type == "api_key"
            and source.source_id != "un_comtrade_api"
            and not settings.source_credentials_configured(source.source_id)
        )
        observation = observations.get(source.source_id, {}) if isinstance(observations, dict) else {}
        state = dict(run_states.get(source.source_id, {}))
        state["adapter"] = policy.production_schedulable
        if source.source_id == "opec_press":
            news = news_sources.get(source.source_id, {}) if isinstance(news_sources, dict) else {}
            state["latest_observed_at"] = state.get("last_success_at") or news.get("last_published_at", "")
        elif source.source_id == "ofac_sanctions":
            event = event_sources.get(source.source_id, {}) if isinstance(event_sources, dict) else {}
            state["latest_observed_at"] = state.get("last_success_at") or event.get("last_occurred_at", "")
        elif getattr(source, "data_role", "") == "historical_context" and not getattr(
            source, "current_formal_eligible", True
        ):
            state["latest_observed_at"] = state.get("last_success_at", "")
        elif source.source_id == "czce_pta_px":
            futures = futures_bars.get(source.source_id, {}) if isinstance(futures_bars, dict) else {}
            state["latest_observed_at"] = futures.get("last_trade_date", "")
        else:
            state["latest_observed_at"] = observation.get("last_observed_at", "")
        readiness = assess_readiness(policy, state)
        if requires_key:
            status = "blocked"
        elif not policy.production_schedulable:
            status = "not_automated"
        elif readiness.ready:
            status = "success"
        elif state.get("last_run_status") and not source_run_succeeded(state.get("last_run_status")):
            status = "failed"
        else:
            status = "due"
        reasons = ", ".join(readiness.reasons)
        task = _task(
            source_id=source.source_id,
            task_name=f"{source.source_name} 自动更新",
            dataset_type=source.category,
            status=status,
            automation_level="full" if policy.production_schedulable and not requires_key else "manual_config",
            requires_computer_use=False,
            requires_human_action=requires_key or not policy.production_schedulable,
            target_table=(
                "news_articles / event_observations"
                if source.source_id == "opec_press"
                else "event_observations / source-state"
                if source.source_id == "ofac_sanctions"
                else "futures_daily_bars"
                if source.source_id == "czce_pta_px"
                else "market_observations"
            ),
            expected_fields=(
                ["published_at", "title", "source_url", "affected_products"]
                if source.source_id == "opec_press"
                else ["snapshot_sha256", "record_count", "delta_action", "evidence_url"]
                if source.source_id == "ofac_sanctions"
                else ["trade_date", "exchange", "product", "contract_code", "close", "evidence_url"]
                if source.source_id == "czce_pta_px"
                else ["observed_at", "indicator", "product", "value", "unit", "evidence_url"]
            ),
            coverage_scope=(
                f"{' / '.join(source.products)}; latest={state.get('latest_observed_at') or 'missing'}; "
                f"last_success={state.get('last_success_at') or 'missing'}"
            ),
            blocking_reason=(
                "缺少 API key"
                if requires_key
                else "当前没有可调度适配器"
                if not policy.production_schedulable
                else reasons
            ),
            next_step=(
                "配置该来源 API key 后再自动更新。"
                if requires_key
                else "补充合规适配器后纳入生产调度。"
                if not policy.production_schedulable
                else "由逐源调度器在到期时抓取、校验并幂等入库。"
            ),
        )
        task.update(
            {
                "last_run_status": str(state.get("last_run_status") or ""),
                "last_run_succeeded": source_run_succeeded(state.get("last_run_status")),
                "refresh_due": readiness.due,
                "data_role": getattr(source, "data_role", "current_candidate"),
                "current_formal_eligible": getattr(source, "current_formal_eligible", True),
                "product_roles": getattr(source, "product_roles", {}),
            }
        )
        tasks.append(task)
    return tasks


def _requires_manual_action(task: dict[str, Any]) -> bool:
    automation_level = str(task.get("automation_level") or "")
    return bool(task.get("requires_human_action")) or automation_level.startswith("manual") or str(
        task.get("status") or ""
    ) == "blocked"


def _public_source_run_states(db_path: Path) -> dict[str, dict[str, Any]]:
    if not db_path.exists():
        return {}
    try:
        with closing(connect_serialized(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=1000")
            states: dict[str, dict[str, Any]] = {}
            if _table_exists(connection, "source_fetch_audit"):
                rows = connection.execute(
                    """
                    SELECT audit.source_id,
                           audit.status AS last_run_status,
                           audit.created_at AS last_run_started_at,
                           success.last_success_at
                    FROM source_fetch_audit AS audit
                    JOIN (
                      SELECT source_id, MAX(created_at) AS latest_at
                      FROM source_fetch_audit
                      GROUP BY source_id
                    ) AS latest
                      ON latest.source_id = audit.source_id AND latest.latest_at = audit.created_at
                    LEFT JOIN (
                      SELECT source_id, MAX(created_at) AS last_success_at
                      FROM source_fetch_audit
                      WHERE status IN (
                        'ok', 'success', 'completed', 'succeeded', 'no_new_data',
                        'no-new-data', 'no_relevant_items', 'up_to_date', 'unchanged'
                      )
                      GROUP BY source_id
                    ) AS success ON success.source_id = audit.source_id
                    """
                ).fetchall()
                states.update({str(row["source_id"]): dict(row) for row in rows})
            if _table_exists(connection, "news_fetch_runs"):
                row = connection.execute(
                    """
                    SELECT run.source_id, run.status AS last_run_status,
                           run.created_at AS last_run_started_at, success.last_success_at
                    FROM news_fetch_runs AS run
                    JOIN (
                      SELECT source_id, MAX(created_at) AS latest_at
                      FROM news_fetch_runs WHERE source_id = 'opec_press' GROUP BY source_id
                    ) AS latest
                      ON latest.source_id = run.source_id AND latest.latest_at = run.created_at
                    LEFT JOIN (
                      SELECT source_id, MAX(COALESCE(finished_at, created_at)) AS last_success_at
                      FROM news_fetch_runs
                      WHERE source_id = 'opec_press' AND status IN ('ok', 'no_relevant_items')
                      GROUP BY source_id
                    ) AS success ON success.source_id = run.source_id
                    """
                ).fetchone()
                if row is not None:
                    states["opec_press"] = dict(row)
    except sqlite3.Error:
        return {}
    return states


def _user_file_tasks(codex_run: Path) -> list[dict[str, Any]]:
    files_dir = codex_run / "user-files"
    candidates = [path for path in files_dir.glob("*") if path.is_file()] if files_dir.exists() else []
    return [
        _task(
            source_id="user_files",
            task_name="用户文件标准导入",
            dataset_type="user_supplied_file",
            status="ready_to_import" if candidates else "ready",
            automation_level="full",
            requires_computer_use=False,
            requires_human_action=False,
            target_table="forecast_price_points / industry_observations / event_observations",
            expected_fields=["source_file", "observed_at", "product", "metric_or_spec", "value", "unit"],
            coverage_scope=f"pending_files={len(candidates)}",
            blocking_reason="",
            next_step=(
                "按标准模板自动校验并导入；PDF/图片移入 review 并生成报告。"
                if candidates
                else "收件箱健康空闲；每日闭环继续扫描。"
            ),
        )
    ]


def _news_source_tasks() -> list[dict[str, Any]]:
    return [
        _task(
            source_id="gdelt_rss_official_news",
            task_name="新闻与官方公告增量更新",
            dataset_type="event_news",
            status="ready",
            automation_level="full",
            requires_computer_use=False,
            requires_human_action=False,
            target_table="news_articles / news_event_clusters / event_observations",
            expected_fields=["title", "published_at", "source_url", "summary", "category", "affected_products"],
            coverage_scope="全球能源、政策、航运、制裁、装置、宏观事件",
            blocking_reason="",
            next_step="运行 news fetch-runs；低置信事件进入人工复核。",
        )
    ]


def _task(
    *,
    source_id: str,
    task_name: str,
    dataset_type: str,
    status: str,
    automation_level: str,
    requires_computer_use: bool,
    requires_human_action: bool,
    target_table: str,
    expected_fields: list[str],
    coverage_scope: str,
    blocking_reason: str,
    next_step: str,
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "task_name": task_name,
        "dataset_type": dataset_type,
        "status": status,
        "automation_level": automation_level,
        "requires_computer_use": requires_computer_use,
        "requires_human_action": requires_human_action,
        "target_table": target_table,
        "expected_fields": expected_fields,
        "coverage_scope": coverage_scope,
        "blocking_reason": blocking_reason,
        "next_step": next_step,
    }


def _decode_run_row(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    for key, fallback in (("expected_fields", []), ("summary", {})):
        value = decoded.get(key)
        if isinstance(value, str):
            try:
                decoded[key] = json.loads(value)
            except json.JSONDecodeError:
                decoded[key] = fallback
    decoded["requires_computer_use"] = bool(decoded.get("requires_computer_use"))
    decoded["requires_human_action"] = bool(decoded.get("requires_human_action"))
    return decoded


def _settings_db_path() -> Path:
    path = Path(settings.sqlite_path)
    if path.is_absolute():
        return path
    return SERVER_ROOT / path


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
        is not None
    )


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _extract_capture_datetime(notes: Any, raw: Any) -> datetime | None:
    candidates: list[Any] = []
    match = re.search(r"captured_at=([^;,\s]+)", str(notes or ""))
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
        parsed = _parse_datetime(candidate)
        if parsed:
            return parsed
    return None


def today_iso() -> str:
    return date.today().isoformat()

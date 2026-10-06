"""管线混合图读层 (pipeline graph) — DESIGN §2.7 批次2/3 的服务端聚合器。

一次 ``GET /api/v1/pipeline/graph`` 聚合首屏所需的 11 节点状态（10 主链 + 研判
助手），抽屉懒加载由 ``GET /api/v1/pipeline/nodes/{node_id}`` 提供四块全量。

设计约束（DESIGN §2.3 层3）：
* 确定性管道不进 agent 表 —— 状态来自现有状态文件/表
  （news-automation、event-summary-worker、output-freshness、daily
  latest-status、快照/批次表、semantic_index_state、artifact 文件）；
* 全部只读：文件读取 + ``connect_readonly()`` 受限聚合查询，任何失败 fail-open
  映射到 waiting/degraded 徽章，绝不让图接口 5xx；
* 徽章三态映射到 API 契约枚举 ``ok|degraded|waiting|idle``（idle 仅助手空闲）。
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .assistant_run_status import (
    DERIVED_FROM_LEGACY,
    LEGACY_REVIEW_STATUS,
)
from .counter_scan import load_counter_scan_artifact
from .daily_interpretation import load_daily_interpretation_artifact
from .pipeline_state_paths import local_production_directory, shared_state_root
from .semantic_index import semantic_index_status
from .settings import settings
from .storage import connect_readonly

BUSINESS_TIMEZONE = ZoneInfo("Asia/Shanghai")
GRAPH_SCHEMA_VERSION = "pipeline_graph.v1"

NODE_ORDER = (
    "collect",
    "clean",
    "index",
    "event_summary",
    "event_overview",
    "factor_score",
    "event_signal",
    "political_analysis",
    "historical_analog",
    "product_synthesis",
    "skeptic_review",
    "event_fusion",
    "seven_product",
    "shadow_eval",
    "counter_scan",
    "daily_interpretation",
    "report_assembly",
    "assistant",
)
NODE_KINDS = {
    "collect": ("code", "采集", "data"),
    "clean": ("code", "清洗去重门禁", "data"),
    "index": ("code", "语义索引", "data"),
    "event_summary": ("agent", "事件摘要", "judgement"),
    "event_overview": ("agent", "事件总览", "judgement"),
    "factor_score": ("code", "因子打分", "judgement"),
    "event_signal": ("code", "当日事件精选", "prediction"),
    "political_analysis": ("agent", "政局解读", "prediction"),
    "historical_analog": ("agent", "历史经验", "prediction"),
    "product_synthesis": ("agent", "品种研判", "prediction"),
    "skeptic_review": ("agent", "交叉质证", "prediction"),
    "event_fusion": ("code", "预测定案", "prediction"),
    "seven_product": ("code", "七产品预测", "prediction"),
    "shadow_eval": ("code", "复盘校准", "prediction"),
    "counter_scan": ("agent", "反证扫描", "judgement"),
    "daily_interpretation": ("agent", "日报解读", "judgement"),
    "report_assembly": ("code", "研报组装", "judgement"),
    "assistant": ("agent", "研判助手", "assistant"),
}
FLOW_EDGES = (
    ("collect", "clean"),
    ("clean", "index"),
    ("index", "event_summary"),
    ("event_summary", "event_overview"),
    ("event_overview", "factor_score"),
    ("factor_score", "event_signal"),
    ("event_signal", "political_analysis"),
    ("political_analysis", "historical_analog"),
    ("historical_analog", "product_synthesis"),
    ("product_synthesis", "skeptic_review"),
    ("skeptic_review", "event_fusion"),
    ("event_fusion", "seven_product"),
    ("seven_product", "shadow_eval"),
    ("shadow_eval", "counter_scan"),
    ("counter_scan", "daily_interpretation"),
    ("daily_interpretation", "report_assembly"),
)
# 虚线交互边=「随时可问」语义（2026-09-16 共识，BATCH-FRONTEND OPEN-1 收口）：
# 研判助手可基于事件摘要与日报解读随时提问，故 dashed=事件摘要⇢研判助手、日报解读⇢研判助手。
# 2026-10-01 多 Agent 链（docs/multi-agent-prediction-plan.md §6）：交叉质证⇢研判助手。
DASHED_EDGES = (
    ("event_summary", "assistant"),
    ("daily_interpretation", "assistant"),
    ("skeptic_review", "assistant"),
)
# 经验回灌（ADR-9 单主线后的学习闭环）：复盘校准⇢政局解读/历史经验。
# 周蒸馏任务（poydty-distill.timer）把已结算样本提炼为 agent_lessons，
# 局势解读与历史经验检索在下一轮直接消费这些教训。
FEEDBACK_EDGES = (
    ("shadow_eval", "political_analysis"),
    ("shadow_eval", "historical_analog"),
)
LLM_STAGES = (
    "event_summary", "event_overview", "direction_review", "assistant", "counter_scan",
    "daily_interpretation",
    "political_analysis", "historical_analog", "product_synthesis", "skeptic_review",
    "event_adjudication",
)


# ---------------------------------------------------------------------------
# Path resolution (env-overridable for tests; defaults mirror production layout).
# ---------------------------------------------------------------------------


def _shared_root() -> Path:
    return shared_state_root(settings.sqlite_path)


def _data_dir() -> Path:
    return Path(settings.sqlite_path).expanduser().resolve().parent


def _local_production_dir() -> Path:
    return local_production_directory(_shared_root())


def _read_json_with_state(path: Path) -> tuple[dict[str, Any], str]:
    """Read a handoff JSON, reporting why it is empty.

    State values: "" (parsed ok), "missing" (no file), "corrupt" (file exists
    but is unreadable or not a JSON object). Corrupt and missing used to be
    indistinguishable, so the canvas could not tell "not run" from "broken".
    """

    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, json.JSONDecodeError, ValueError):
        return {}, "corrupt"
    if not isinstance(payload, dict):
        return {}, "corrupt"
    return payload, ""


def _read_json(path: Path) -> dict[str, Any]:
    return _read_json_with_state(path)[0]


def _now() -> datetime:
    return datetime.now(UTC)


def default_business_date(now: datetime | None = None) -> str:
    return (now or _now()).astimezone(BUSINESS_TIMEZONE).date().isoformat()


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _age_seconds(value: str, now: datetime | None = None) -> float | None:
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    return max(0.0, ((now or _now()) - parsed).total_seconds())


def _business_day_window(business_date: str) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(f"{business_date}T00:00:00").replace(tzinfo=BUSINESS_TIMEZONE).astimezone(UTC)
    return start, start + timedelta(days=1)


def _in_business_day(value: str, business_date: str) -> bool:
    parsed = _parse_ts(value)
    if parsed is None:
        return False
    start, end = _business_day_window(business_date)
    return start <= parsed < end


# ---------------------------------------------------------------------------
# Read-only DB aggregations.
# ---------------------------------------------------------------------------


def _fetch_run_counts(business_date: str) -> dict[str, Any]:
    counts: dict[str, int] = {"ok": 0, "no_relevant": 0, "error": 0, "timeout": 0, "other": 0}
    per_source: dict[str, dict[str, int]] = {}
    last_run_at = ""
    try:
        with closing(connect_readonly()) as connection:
            rows = connection.execute(
                """
                SELECT created_at, source_id, status FROM news_fetch_runs
                WHERE date(created_at) IN (date(?), date(?, '-1 day'))
                """,
                (business_date, business_date),
            ).fetchall()
    except Exception:  # noqa: BLE001 - graph must fail open, never 5xx.
        return {**counts, "total": 0, "last_run_at": "", "per_source": per_source}
    for row in rows:
        if not _in_business_day(str(row["created_at"] or ""), business_date):
            continue
        status = str(row["status"] or "other")
        counts[status if status in counts else "other"] += 1
        source_id = str(row["source_id"] or "")
        if source_id:
            bucket = per_source.setdefault(source_id, {"ok": 0, "no_relevant": 0, "error": 0, "timeout": 0, "other": 0})
            bucket[status if status in bucket else "other"] += 1
        last_run_at = max(last_run_at, str(row["created_at"] or ""))
    total = sum(counts.values())
    return {**counts, "total": total, "last_run_at": last_run_at, "per_source": per_source}


def _persistently_blocked_source_ids(business_date: str, *, window_days: int = 14, min_runs: int = 6) -> list[str]:
    """Sources whose runs almost never succeed over a trailing window.

    A source blocked at the network layer (geo-block / WAF) fails every retry
    cycle; counting those retries as collection failures keeps the node
    permanently red. Classification is self-healing: one recovered source
    drops out of the set as soon as its window success rate rises.
    """
    try:
        with closing(connect_readonly()) as connection:
            rows = connection.execute(
                """
                SELECT source_id, status, COUNT(*) AS n FROM news_fetch_runs
                WHERE date(created_at) BETWEEN date(?, ?) AND date(?)
                GROUP BY source_id, status
                """,
                (business_date, f"-{window_days} day", business_date),
            ).fetchall()
    except Exception:  # noqa: BLE001 - graph must fail open.
        return []
    totals: dict[str, dict[str, int]] = {}
    for row in rows:
        source_id = str(row["source_id"] or "")
        if not source_id:
            continue
        bucket = totals.setdefault(source_id, {"good": 0, "total": 0})
        status = str(row["status"] or "")
        bucket["total"] += int(row["n"] or 0)
        if status in {"ok", "no_relevant"}:
            bucket["good"] += int(row["n"] or 0)
    return sorted(
        source_id
        for source_id, bucket in totals.items()
        if bucket["total"] >= min_runs and bucket["good"] / bucket["total"] < 0.2
    )


def _today_table_counts(business_date: str) -> dict[str, int]:
    result = {"articles": 0, "clusters": 0, "quality_gate_rejected": 0, "summaries_completed": 0, "summaries_failed": 0}
    try:
        with closing(connect_readonly()) as connection:
            for key, table, column in (
                ("articles", "news_articles", "created_at"),
                ("clusters", "news_event_clusters", "created_at"),
            ):
                rows = connection.execute(
                    f"SELECT {column} AS ts FROM {table} WHERE date({column}) IN (date(?), date(?, '-1 day'))",
                    (business_date, business_date),
                ).fetchall()  # noqa: S608 - static table/column names only.
                result[key] = sum(
                    1 for row in rows if _in_business_day(str(row["ts"] or ""), business_date)
                )
            summary_rows = connection.execute(
                """
                SELECT generated_at, provider, summary_status FROM event_ai_summaries
                WHERE date(generated_at) IN (date(?), date(?, '-1 day'))
                   OR summary_status IN ('pending','failed','processing')
                """,
                (business_date, business_date),
            ).fetchall()
            for row in summary_rows:
                status = str(row["summary_status"] or "")
                provider = str(row["provider"] or "")
                if status in {"pending", "failed", "processing"}:
                    continue
                if not _in_business_day(str(row["generated_at"] or ""), business_date):
                    continue
                if provider == "quality_gate":
                    result["quality_gate_rejected"] += 1
                elif status == "completed":
                    result["summaries_completed"] += 1
                elif status == "failed":
                    result["summaries_failed"] += 1
    except Exception:  # noqa: BLE001
        return result
    return result


def _queue_counts() -> dict[str, int]:
    result = {"pending": 0, "processing": 0, "failed": 0, "completed": 0, "rejected": 0}
    try:
        with closing(connect_readonly()) as connection:
            rows = connection.execute(
                "SELECT summary_status, COUNT(*) AS n FROM event_ai_summaries GROUP BY summary_status"
            ).fetchall()
        for row in rows:
            key = str(row["summary_status"] or "")
            if key in result:
                result[key] = int(row["n"])
    except Exception:  # noqa: BLE001
        pass
    return result


def _llm_stage_costs(business_date: str) -> dict[str, dict[str, Any]]:
    costs: dict[str, dict[str, Any]] = {}
    try:
        with closing(connect_readonly()) as connection:
            rows = connection.execute(
                """
                SELECT stage, COUNT(*) AS calls, SUM(cost_micros) AS cost_micros
                FROM llm_traces WHERE business_date = ? GROUP BY stage
                """,
                (business_date,),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return costs
    for row in rows:
        stage = str(row["stage"] or "")
        if stage:
            costs[stage] = {
                "calls": int(row["calls"] or 0),
                "cost_micros": int(row["cost_micros"] or 0),
            }
    return costs


def _stage_traces(stage: str, business_date: str, limit: int = 5) -> list[dict[str, Any]]:
    try:
        with closing(connect_readonly()) as connection:
            rows = connection.execute(
                """
                SELECT trace_id, created_at, latency_ms, prompt_tokens_est, completion_tokens_est,
                       fallback, error, cost_micros
                FROM llm_traces WHERE stage = ? AND business_date = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (stage, business_date, max(1, min(limit, 20))),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "run_id": str(row["trace_id"]),
            "started_at": str(row["created_at"] or ""),
            "latency_ms": int(row["latency_ms"] or 0),
            "status": "ok" if not row["error"] else "degraded",
            "gate_result": str(row["error"] or ""),
            "cost_micros": int(row["cost_micros"] or 0),
            "fallback": bool(row["fallback"]),
        }
        for row in rows
    ]


def _recent_assistant_runs(limit: int = 5) -> list[dict[str, Any]]:
    try:
        with closing(connect_readonly()) as connection:
            rows = connection.execute(
                """
                SELECT run_id, goal, status, created_at, updated_at FROM agent_runs
                WHERE source = 'assistant_pipeline'
                ORDER BY created_at DESC LIMIT ?
                """,
                (max(1, min(limit, 20)),),
            ).fetchall()
            flags_by_run: dict[str, list[str]] = {}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                flag_rows = connection.execute(
                    f"""
                    SELECT run_id, risk_flags FROM agent_turns
                    WHERE agent_name = '质量复核' AND run_id IN ({placeholders})
                    """,  # noqa: S608 - placeholders only, static names.
                    tuple(str(row["run_id"]) for row in rows),
                ).fetchall()
                for flag_row in flag_rows:
                    try:
                        values = json.loads(str(flag_row["risk_flags"] or "[]"))
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(values, list):
                        flags_by_run.setdefault(str(flag_row["run_id"]), []).extend(
                            str(item) for item in values if item
                        )
    except Exception:  # noqa: BLE001 - graph must fail open, never 5xx.
        return []
    runs: list[dict[str, Any]] = []
    for row in rows:
        run_id = str(row["run_id"])
        status = str(row["status"] or "")
        derived = ""
        # assistant-status.v2 read-layer projection: legacy review runs were
        # delivered answers, so they present as completed with a marker.
        if status == LEGACY_REVIEW_STATUS:
            status, derived = "completed", DERIVED_FROM_LEGACY
        runs.append(
            {
                "run_id": run_id,
                "question": str(row["goal"] or "")[:160],
                "started_at": str(row["created_at"] or ""),
                "latency_ms": 0,
                "status": status,
                "derived_status": derived,
                "gate_result": ",".join(sorted(set(flags_by_run.get(run_id, [])))),
                "finished_at": str(row["updated_at"] or ""),
            }
        )
    return runs


def _assistant_cited_doc_ids(limit: int = 6) -> list[str]:
    try:
        with closing(connect_readonly()) as connection:
            row = connection.execute(
                """
                SELECT cited_source_ids FROM llm_traces
                WHERE stage = 'assistant' ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
    except Exception:  # noqa: BLE001
        return []
    if row is None:
        return []
    try:
        values = json.loads(str(row["cited_source_ids"] or "[]"))
    except (json.JSONDecodeError, TypeError):
        return []
    return [str(value) for value in values if value][:limit]


def _latest_seven_product_batch() -> dict[str, Any] | None:
    try:
        with closing(connect_readonly()) as connection:
            row = connection.execute(
                """
                SELECT batch_id, business_date, generated_at, formal_count, reference_count, unavailable_count
                FROM seven_product_forecast_batches ORDER BY business_date DESC LIMIT 1
                """
            ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return {
        "batch_id": str(row["batch_id"]),
        "business_date": str(row["business_date"]),
        "generated_at": str(row["generated_at"] or ""),
        "formal_count": int(row["formal_count"] or 0),
        "reference_count": int(row["reference_count"] or 0),
        "unavailable_count": int(row["unavailable_count"] or 0),
    }


def _latest_snapshot(business_date: str) -> dict[str, Any] | None:
    try:
        with closing(connect_readonly()) as connection:
            row = connection.execute(
                """
                SELECT business_date, snapshot_id, generated_at, as_of_time, payload_sha256, payload
                FROM daily_judgement_snapshots WHERE business_date = ?
                """,
                (business_date,),
            ).fetchone()
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    try:
        payload = json.loads(str(row["payload"] or "{}"))
    except json.JSONDecodeError:
        payload = {}
    return {
        "business_date": str(row["business_date"]),
        "snapshot_id": str(row["snapshot_id"]),
        "generated_at": str(row["generated_at"] or ""),
        "as_of_time": str(row["as_of_time"] or ""),
        "payload_sha256": str(row["payload_sha256"] or ""),
        "payload": payload if isinstance(payload, dict) else {},
    }


def _snapshot_factors(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not snapshot:
        return []
    judgement = snapshot.get("payload", {}).get("judgement")
    if not isinstance(judgement, dict):
        return []
    return [item for item in (judgement.get("factors") or []) if isinstance(item, dict)]


# ---------------------------------------------------------------------------
# Per-node state (shared by graph summary and drawer detail).
# ---------------------------------------------------------------------------


NodeState = dict[str, Any]


def _collect_state(business_date: str, now: datetime | None = None) -> dict[str, NodeState]:
    now = now or _now()
    states: dict[str, NodeState] = {}
    for node_id in NODE_ORDER:
        builder = _NODE_BUILDERS[node_id]
        try:
            states[node_id] = builder(business_date, now)
        except Exception as exc:  # noqa: BLE001 - one broken node never breaks the graph.
            states[node_id] = {
                "status": "waiting",
                "status_detail": f"state_unavailable:{exc.__class__.__name__}",
                "timestamp": "",
                "metrics": {},
                "sources": [],
            }
    return states


def _collect_node(business_date: str, now: datetime) -> NodeState:
    fetch = _fetch_run_counts(business_date)
    automation = _read_json(_local_production_dir() / "source-automation" / "source-automation-latest.json") or \
        _read_json(_shared_root() / "news-automation" / "source-automation-latest.json")
    freshness = _read_json(_local_production_dir() / "output-freshness" / "latest.json")
    critical = [str(item) for item in (automation.get("critical_failures") or []) if item]
    automation_status = str(automation.get("status") or "missing")
    blocked = set(_persistently_blocked_source_ids(business_date))
    reachable_total = 0
    reachable_good = 0
    reachable_bad = 0
    reachable_bad_by_source: dict[str, int] = {}
    for source_id, bucket in fetch["per_source"].items():
        runs = sum(bucket.values())
        if source_id in blocked:
            continue
        reachable_total += runs
        reachable_good += bucket["ok"] + bucket["no_relevant"]
        bad = bucket["error"] + bucket["timeout"]
        reachable_bad += bad
        if bad:
            reachable_bad_by_source[source_id] = bad
    reachable_rate = (reachable_good / reachable_total) if reachable_total else None
    ok_rate = (fetch["ok"] / fetch["total"]) if fetch["total"] else None
    worst_sources = [
        f"{source_id} {bad_runs}"
        for source_id, bad_runs in sorted(reachable_bad_by_source.items(), key=lambda item: -item[1])[:4]
    ]
    if fetch["total"] == 0:
        status, detail = "waiting", "当日无任何采集运行（调度未到或停摆→告警）"
    elif reachable_rate is not None and reachable_rate >= 0.8 and not critical and automation_status != "blocked":
        status = "ok"
        detail = f"可达源 {reachable_total} 次抓取 ok 率 {reachable_rate:.0%}"
        if blocked:
            detail += f"；{len(blocked)} 源外部受限（14 天窗口持续失败，自动重试不视为采集故障）"
    else:
        problems = []
        if reachable_bad > 0:
            note = f"可达源 error+timeout {reachable_bad}/{reachable_total}"
            if worst_sources:
                note += "（" + "、".join(worst_sources) + "）"
            problems.append(note)
        elif reachable_rate is not None:
            problems.append(f"可达源 ok 率 {reachable_rate:.0%}")
        if critical:
            problems.append("critical: " + ",".join(critical[:3]))
        if automation_status in {"blocked", "degraded", "missing"}:
            problems.append(f"automation={automation_status}")
        status, detail = ("degraded", "；".join(problems) or "automation degraded")
    return {
        "status": status,
        "status_detail": detail,
        "timestamp": fetch["last_run_at"] or str(automation.get("finished_at") or ""),
        "metrics": {
            "runs": {key: fetch[key] for key in ("ok", "no_relevant", "error", "timeout", "total")},
            "ok_rate": round(ok_rate, 3) if ok_rate is not None else None,
            "reachable_runs": reachable_total,
            "reachable_ok_rate": round(reachable_rate, 3) if reachable_rate is not None else None,
            "blocked_source_count": len(blocked),
            "blocked_sources": sorted(blocked),
            "automation_status": automation_status,
            "critical_failures": critical,
            "freshness_overall": str(freshness.get("overall") or ""),
        },
        "sources": [
            "news_fetch_runs",
            "local-production/source-automation/source-automation-latest.json",
            "local-production/output-freshness/latest.json",
        ],
    }


def _clean_node(business_date: str, now: datetime) -> NodeState:
    quality = _read_json(_local_production_dir() / "source-automation" / "delivery-data-quality.json")
    daily = _read_json(_local_production_dir() / "latest-status.json")
    refresh = daily.get("quality_refresh") if isinstance(daily.get("quality_refresh"), dict) else {}
    counts = _today_table_counts(business_date)
    overall = str(quality.get("overall_status") or "missing")
    exit_code = refresh.get("exit_code")
    if overall == "missing":
        status, detail = "waiting", "质量门未跑（链未到）"
    elif overall in {"ok", "success"} and exit_code in {0, None}:
        status, detail = "ok", f"质量门 exit={exit_code}，overall={overall}"
    elif overall == "needs_human_review":
        status, detail = "degraded", "质量门 needs_human_review（观察级放行）"
    else:
        status, detail = "degraded", f"质量门 overall={overall}"
    return {
        "status": status,
        "status_detail": detail,
        "timestamp": str(quality.get("generated_at") or daily.get("finished_at") or ""),
        "metrics": {
            "overall_status": overall,
            "exit_code": exit_code,
            "articles_today": counts["articles"],
            "clusters_today": counts["clusters"],
            "quality_gate_rejected_today": counts["quality_gate_rejected"],
            "alerts": len(quality.get("alerts") or []) if isinstance(quality.get("alerts"), list) else 0,
        },
        "sources": ["local-production/source-automation/delivery-data-quality.json", "event_ai_summaries"],
    }


def _index_node(business_date: str, now: datetime) -> NodeState:
    status_payload = semantic_index_status()
    active = status_payload.get("active_index") if isinstance(status_payload.get("active_index"), dict) else {}
    building = status_payload.get("building_index") if isinstance(status_payload.get("building_index"), dict) else {}
    status_value = str(status_payload.get("status") or "missing")
    embedding_mode = str(active.get("embedding_mode") or "")
    if status_value == "ready" and embedding_mode == "semantic_embedding":
        status, detail = "ok", f"索引 ready，{active.get('document_count', 0)} 文档（semantic_embedding）"
    elif status_value == "ready":
        status, detail = "degraded", f"索引 ready 但 embedding_mode={embedding_mode or 'unknown'}（降级检索）"
    elif status_value == "stale":
        status, detail = "degraded", f"索引过期：{status_payload.get('stale_reason')}"
    elif building:
        status, detail = "waiting", "索引 building 中"
    else:
        status, detail = "waiting", "索引缺失（未构建）"
    return {
        "status": status,
        "status_detail": detail,
        "timestamp": str(active.get("completed_at") or status_payload.get("last_successful_build") or ""),
        "metrics": {
            "index_status": status_value,
            "embedding_mode": embedding_mode,
            "documents": int(active.get("document_count") or 0),
            "chunks": int(active.get("chunk_count") or 0),
            "vectors": int(active.get("vector_count") or 0),
            "stale_reason": str(status_payload.get("stale_reason") or ""),
        },
        "sources": ["semantic_index_state", "event-summary-worker/index-maintenance.json"],
    }


def _event_summary_node(business_date: str, now: datetime) -> NodeState:
    worker = _read_json(_shared_root() / "event-summary-worker" / "latest.json")
    heartbeat_age = _age_seconds(str(worker.get("generated_at") or ""), now)
    after = worker.get("after") if isinstance(worker.get("after"), dict) else {}
    pending = int(after.get("pending") or 0)
    failed = int(after.get("failed") or 0) + int(after.get("retryable_failed") or 0)
    if not worker:
        status, detail = "waiting", "worker 心跳缺失（可能停摆→触发告警）"
    elif heartbeat_age is None or heartbeat_age > 600:
        age_note = f"{int(heartbeat_age)}s" if heartbeat_age is not None else "?"
        status, detail = "waiting", f"worker 心跳缺失（{age_note} 前最后心跳，可能停摆）"
    elif pending == 0 and failed == 0:
        status, detail = "ok", f"心跳 {int(heartbeat_age)}s，队列清空"
    else:
        severity = "（积压超 2h，加重）" if heartbeat_age is not None and heartbeat_age > 7200 else ""
        status, detail = "degraded", f"pending={pending}，failed={failed}{severity}"
    # Cumulative counts prefer the worker heartbeat's own totals; the queue
    # table aggregation is the fallback when the heartbeat is missing.
    worker_counts = {
        key: int(after.get(key) or 0)
        for key in ("pending", "processing", "failed", "completed", "rejected")
        if after.get(key) is not None
    }
    counts = worker_counts or _queue_counts()
    return {
        "status": status,
        "status_detail": detail,
        "timestamp": str(worker.get("generated_at") or ""),
        "metrics": {
            "heartbeat_age_seconds": int(heartbeat_age) if heartbeat_age is not None else None,
            "queue": counts,
            "daily_reserved_requests": worker.get("daily_reserved_requests"),
            "processed_last_cycle": worker.get("processed") if isinstance(worker.get("processed"), dict) else {},
        },
        "sources": ["event-summary-worker/latest.json", "event_ai_summaries"],
    }


def _event_overview_node(business_date: str, now: datetime) -> NodeState:
    root = _data_dir() / "event-overviews"
    budget = _read_json(root / "budget.json")
    limit = int(budget.get("limit_microusd") or 0)
    used = int(budget.get("used_microusd") or 0)
    reservations = budget.get("reservations") if isinstance(budget.get("reservations"), dict) else {}
    settled = sum(1 for item in reservations.values() if isinstance(item, dict) and item.get("status") == "settled")
    failures_today = 0
    failure_dir = root / "failures"
    if failure_dir.is_dir():
        start, end = _business_day_window(business_date)
        for path in failure_dir.glob("*.json"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError:
                continue
            if start <= mtime < end:
                failures_today += 1
    budget_path = root / "budget.json"
    if limit == 0:
        status, detail = "waiting", "预算停用（limit=0，fail-closed）"
    elif failures_today:
        status, detail = "degraded", f"今日付费失败 {failures_today} 次（failures/）"
    elif used < limit:
        status, detail = "ok", f"预算 {used}/{limit} micro-USD，已结算 {settled} 条"
    else:
        status, detail = "waiting", "预算封盘（used>=limit）"
    timestamp = ""
    if budget_path.is_file():
        try:
            timestamp = datetime.fromtimestamp(budget_path.stat().st_mtime, tz=UTC).isoformat()
        except OSError:
            timestamp = ""
    return {
        "status": status,
        "status_detail": detail,
        "timestamp": timestamp,
        "metrics": {
            "limit_microusd": limit,
            "used_microusd": used,
            "settled_count": settled,
            "failures_today": failures_today,
        },
        "sources": ["data/event-overviews/budget.json", "data/event-overviews/failures/"],
    }


def _factor_score_node(business_date: str, now: datetime) -> NodeState:
    snapshot = _latest_snapshot(business_date)
    factors = _snapshot_factors(snapshot)
    ready = sum(1 for item in factors if str(item.get("data_status") or "") == "ready")
    if snapshot is None:
        status, detail = "waiting", "当日快照未物化（按需计算无输入锚点）"
    elif factors:
        degraded_note = "" if ready == len(factors) else f"，{len(factors) - ready} 项输入缺失"
        status = "ok" if ready == len(factors) else "degraded"
        detail = f"{len(factors)} 项因子（{ready} 项 ready）{degraded_note}"
    else:
        status, detail = "waiting", "快照无因子数据"
    return {
        "status": status,
        "status_detail": detail,
        "timestamp": str((snapshot or {}).get("generated_at") or ""),
        "metrics": {
            "factors_total": len(factors),
            "factors_ready": ready,
            "factor_names": [str(item.get("name") or "") for item in factors][:8],
            "factor_details": [
                {key: item.get(key) for key in ("name", "data_status", "reason", "observed_at", "source_id")}
                for item in factors
            ],
        },
        "sources": ["daily_judgement_snapshots"],
    }


def _seven_product_node(business_date: str, now: datetime) -> NodeState:
    batch = _latest_seven_product_batch()
    daily = _read_json(_local_production_dir() / "latest-status.json")
    evaluation = daily.get("seven_product_oos_evaluation") if isinstance(
        daily.get("seven_product_oos_evaluation"), dict
    ) else {}
    eval_payload = _read_json(
        _local_production_dir() / "seven-product-evaluation" / "seven-product-evaluation-latest.json"
    )
    cells = (eval_payload.get("evaluation") or {}).get("cells") if isinstance(eval_payload, dict) else None
    if not isinstance(cells, list):
        cells = []
    minimum_samples = int((eval_payload.get("evaluation") or {}).get("minimum_effective_samples") or 20) \
        if isinstance(eval_payload, dict) else 20
    maturing = 0
    max_effective = 0
    failing: list[str] = []
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        effective = int(cell.get("effective_sample_count") or 0)
        max_effective = max(max_effective, effective)
        direction = cell.get("direction_accuracy")
        improvement = cell.get("error_improvement")
        if direction is None and effective == 0:
            maturing += 1  # 尚无任何可评估结果。
            continue
        if effective >= 10 and (
            (direction is not None and direction < 0.55)
            or (improvement is not None and improvement < 0.05)
        ):
            # 样本量足以给出方向性结论时仍未达标，视为真失败而非成熟期。
            failing.append(f"{cell.get('target')} h{cell.get('horizon_days')}")
        else:
            maturing += 1
    if batch is None:
        status, detail = "waiting", "无七产品批次（链未跑到）"
    elif batch["business_date"] != business_date:
        status, detail = "waiting", f"最新批次为 {batch['business_date']}（当日链未跑到）"
    else:
        notes = []
        if batch["formal_count"] > 0:
            status = "ok"
        elif failing:
            status = "degraded"
            notes.append("方向/误差未达标：" + "、".join(failing[:4]))
        else:
            status = "degraded"
            notes.append("formal=0（现状：正式晋级未开）")
        if cells:
            notes.append(f"样本积累中 {maturing} 格（max {max_effective}/{minimum_samples}）")
        if str(evaluation.get("status") or "") == "blocked" and not failing:
            notes.append("OOS 门禁未过（样本不足为主）")
        detail = (
            f"formal={batch['formal_count']} reference={batch['reference_count']}"
            f" unavailable={batch['unavailable_count']}"
        )
        if notes:
            detail = detail + "；" + "；".join(notes)
    return {
        "status": status,
        "status_detail": detail,
        "timestamp": str((batch or {}).get("generated_at") or ""),
        "metrics": {
            "batch": batch or {},
            "evaluation_status": str(evaluation.get("status") or ""),
            "cells": len(cells),
            "maturing_cells": maturing,
            "max_effective_samples": max_effective,
            "minimum_effective_samples": minimum_samples,
            "failing_cells": failing,
        },
        "sources": [
            "seven_product_forecast_batches",
            "local-production/latest-status.json",
            "local-production/seven-product-evaluation/seven-product-evaluation-latest.json",
        ],
    }


def _artifact_node(
    business_date: str,
    loader,
    *,
    present_ok_detail: str,
    missing_waiting_chain_ok: str,
    missing_waiting_chain_blocked: str,
) -> NodeState:
    artifact = loader(business_date)
    chain_succeeded = (_shared_root() / "state" / "local-daily" / f"{business_date}.success").is_file()
    if artifact is None:
        status = "waiting"
        detail = missing_waiting_chain_ok if chain_succeeded else missing_waiting_chain_blocked
        return {
            "status": status,
            "status_detail": detail,
            "timestamp": "",
            "metrics": {"artifact": None, "chain_succeeded": chain_succeeded},
            "sources": [],
        }
    artifact_status = str(artifact.get("status") or "")
    if artifact_status == "completed":
        status, detail = "ok", present_ok_detail
    elif artifact_status == "skipped":
        status, detail = "waiting", f"skipped（{artifact.get('failure_reason') or '未运行'}）"
    else:
        status, detail = "degraded", f"degraded（{artifact.get('failure_reason') or 'unknown'}）"
    return {
        "status": status,
        "status_detail": detail,
        "timestamp": str(artifact.get("generated_at") or ""),
        "metrics": {"artifact": artifact, "chain_succeeded": chain_succeeded},
        "sources": [str(artifact.get("artifact_path") or "")] if artifact.get("artifact_path") else [],
    }


def _counter_scan_node(business_date: str, now: datetime) -> NodeState:
    state = _artifact_node(
        business_date,
        load_counter_scan_artifact,
        present_ok_detail="扫描完成（observation_only）",
        missing_waiting_chain_ok="无 artifact：当日链已成功，跳过/等待重跑",
        missing_waiting_chain_blocked="无 artifact：当日链未成功（skipped）",
    )
    artifact = state["metrics"].get("artifact")
    if isinstance(artifact, dict):
        state["metrics"]["scan_outcome"] = str(artifact.get("scan_outcome") or "")
        state["metrics"]["findings"] = len(artifact.get("findings") or [])
        state["metrics"]["stripped"] = int((artifact.get("sanitized") or {}).get("findings_stripped") or 0)
    return state


def _daily_interpretation_node(business_date: str, now: datetime) -> NodeState:
    state = _artifact_node(
        business_date,
        load_daily_interpretation_artifact,
        present_ok_detail="解读完成（observation_only）",
        missing_waiting_chain_ok="无 artifact：当日链已成功，跳过/等待重跑",
        missing_waiting_chain_blocked="无 artifact：当日链未成功（skipped）",
    )
    artifact = state["metrics"].get("artifact")
    if isinstance(artifact, dict):
        state["metrics"]["sections"] = len(artifact.get("sections") or [])
        state["metrics"]["notes"] = artifact.get("notes") or []
    return state


def _report_assembly_node(business_date: str, now: datetime) -> NodeState:
    daily = _read_json(_local_production_dir() / "latest-status.json")
    brief_path = _shared_root() / "morning-brief" / f"{business_date}.json"
    overall = str(daily.get("overall_status") or "")
    brief_exists = brief_path.is_file()
    warnings_count = len(daily.get("warnings") or []) if isinstance(daily.get("warnings"), list) else 0
    if not daily:
        status, detail = "waiting", "链运行中（latest-status 未生成）"
    elif overall in {"ready", "ready_with_warnings"} and brief_exists:
        status = "ok"
        detail = f"overall={overall}，morning-brief 当日文件存在（warnings={warnings_count}）"
    elif overall in {"blocked", "failed"}:
        status, detail = "degraded", f"overall={overall}"
    else:
        brief_note = "缺失" if not brief_exists else "存在"
        status, detail = "degraded", f"overall={overall or 'unknown'}；morning-brief 当日文件{brief_note}"
    return {
        "status": status,
        "status_detail": detail,
        "timestamp": str(daily.get("finished_at") or ""),
        "metrics": {
            "overall_status": overall,
            "warnings_count": warnings_count,
            "morning_brief_file": str(brief_path) if brief_exists else "",
        },
        "sources": ["local-production/latest-status.json", "morning-brief/"],
    }


def _chain_report_state(business_date: str) -> tuple[dict[str, Any], str]:
    """当日 event-agent-chain-latest.json（节点 B 落盘）。

    State values: "" (当日报告可用), "missing" (无文件), "corrupt" (文件损坏),
    "stale" (文件存在但非当日).
    """

    path = _local_production_dir() / "event-agent-chain-latest.json"
    report, file_state = _read_json_with_state(path)
    if file_state:
        return {}, file_state
    if not report:
        return {}, "missing"
    report_date = str(report.get("business_date") or "")
    as_of = _parse_ts(str(report.get("as_of_time") or ""))
    if report_date == business_date or (as_of and _in_business_day(as_of.isoformat(), business_date)):
        return report, ""
    return {}, "stale"


def _chain_report(business_date: str) -> dict[str, Any]:
    return _chain_report_state(business_date)[0]


def _event_signal_node(business_date: str, now: datetime) -> NodeState:
    signal_path = _local_production_dir() / "event-signal-latest.json"
    signal, signal_file_state = _read_json_with_state(signal_path)
    if not signal:
        empty_detail = (
            "信号报告文件损坏无法解析：请重跑链节点 A"
            if signal_file_state == "corrupt"
            else "无信号报告：等待当日链节点 A 运行"
        )
        return {
            "status": "waiting",
            "status_detail": empty_detail,
            "timestamp": "",
            "metrics": {},
            "sources": ["local-production/event-signal-latest.json"],
        }
    status = str(signal.get("status") or "")
    if status == "ok":
        state_status, detail = (
            "ok",
            f"候选 {signal.get('selected_count', 0)}/16 · 覆盖 {len(signal.get('covered_products') or [])} 品种",
        )
    elif status == "empty":
        state_status, detail = "degraded", "当日 0 候选：Agent 链跳过，预测走纯价格"
    else:
        state_status, detail = "degraded", f"status={status or 'unknown'}"
    return {
        "status": state_status,
        "status_detail": detail,
        "timestamp": str(signal.get("as_of_time") or ""),
        "metrics": {
            "selected_count": signal.get("selected_count", 0),
            "covered_products": signal.get("covered_products") or [],
            "input_sha256": str(signal.get("input_sha256") or "")[:8],
            "business_date_match": str(signal.get("business_date") or "") == business_date,
        },
        "sources": ["local-production/event-signal-latest.json"],
    }


def _chain_stage_node(
    business_date: str,
    *,
    stage: str,
    label: str,
    build_detail,
) -> NodeState:
    report, report_state = _chain_report_state(business_date)
    if not report:
        if report_state == "corrupt":
            empty_detail = f"链报告文件损坏无法解析：{label} 状态未知，请重跑节点 B"
        elif report_state == "stale":
            empty_detail = f"链报告非当日：{label} 等待节点 B 今日运行"
        else:
            empty_detail = f"无链报告：{label} 等待节点 B 运行"
        return {
            "status": "waiting",
            "status_detail": empty_detail,
            "timestamp": "",
            "metrics": {},
            "sources": ["local-production/event-agent-chain-latest.json"],
        }
    counters = (report.get("counters") or {}).get(stage) or {}
    ok_count = int(counters.get("ok") or 0)
    fallback_count = int(counters.get("fallback") or 0)
    rejected_count = int(counters.get("rejected") or 0)
    status = "degraded" if fallback_count or rejected_count else "ok"
    budget = report.get("budget")
    caps = (budget.get("stage_caps") or budget.get("cap")) if isinstance(budget, dict) else None
    call_cap = caps.get(stage) if isinstance(caps, dict) else None
    return {
        "status": status,
        "status_detail": build_detail(report, ok_count, fallback_count, rejected_count),
        "timestamp": str(report.get("as_of_time") or ""),
        "metrics": {
            "calls": ok_count,
            "call_cap": call_cap if type(call_cap) is int and call_cap > 0 else None,
            "fallback": fallback_count,
            "rejected": rejected_count,
            "run_id": str(report.get("run_id") or ""),
        },
        "sources": ["local-production/event-agent-chain-latest.json", "llm_traces"],
    }


def _political_analysis_node(business_date: str, now: datetime) -> NodeState:
    def detail(report, ok_count, fallback_count, rejected_count):
        return f"调用 {ok_count} · 模板回退 {fallback_count} · 存活 {len(report.get('surviving_event_ids') or [])}"

    return _chain_stage_node(business_date, stage="political_analysis", label="政局解读", build_detail=detail)


def _historical_analog_node(business_date: str, now: datetime) -> NodeState:
    def detail(report, ok_count, fallback_count, rejected_count):
        no_prior = sum(
            1
            for envelope in report.get("artifacts") or []
            if envelope.get("stage") == "historical_analog"
            and (envelope.get("output") or {}).get("analog_validity") == "no_prior"
        )
        return f"调用 {ok_count} · 无先例 {no_prior} · 回退 {fallback_count}"

    return _chain_stage_node(business_date, stage="historical_analog", label="历史经验", build_detail=detail)


def _product_synthesis_node(business_date: str, now: datetime) -> NodeState:
    def detail(report, ok_count, fallback_count, rejected_count):
        factors = report.get("product_factors") or {}
        products_with_direction = sum(
            1
            for item in factors.values()
            if any(
                str((factor or {}).get("direction") or "neutral") != "neutral"
                for factor in (item.get("factor_by_horizon") or {}).values()
            )
        )
        return f"{len(factors)}/7 品种因子 · 有方向 {products_with_direction} · 回退 {fallback_count}"

    return _chain_stage_node(business_date, stage="product_synthesis", label="品种研判", build_detail=detail)


def _skeptic_review_node(business_date: str, now: datetime) -> NodeState:
    def detail(report, ok_count, fallback_count, rejected_count):
        factors = report.get("product_factors") or {}
        verdicts = [str(item.get("skeptic_verdict") or "维持") for item in factors.values()]
        return (
            f"维持 {verdicts.count('维持')} · 降级 {verdicts.count('降级')}"
            f" · 推翻 {verdicts.count('推翻')} · 回退 {fallback_count}"
        )

    return _chain_stage_node(business_date, stage="skeptic_review", label="交叉质证", build_detail=detail)


def _event_fusion_node(business_date: str, now: datetime) -> NodeState:
    daily = _read_json(_local_production_dir() / "latest-status.json")
    fusion = (
        ((daily.get("seven_product_lifecycle") or {}).get("event_fusion") or {})
        if isinstance(daily.get("seven_product_lifecycle"), dict)
        else {}
    )
    if not fusion:
        return {
            "status": "waiting",
            "status_detail": "无定案报告：等待发牌后的定案步骤",
            "timestamp": "",
            "metrics": {},
            "sources": ["local-production/latest-status.json"],
        }
    if str(fusion.get("status") or "") == "degraded":
        return {
            "status": "degraded",
            "status_detail": f"degraded（{fusion.get('reason') or 'unknown'}）",
            "timestamp": str(daily.get("finished_at") or ""),
            "metrics": {"fusion": fusion},
            "sources": ["local-production/latest-status.json"],
        }
    confirm_count = int(fusion.get("confirm_count") or 0)
    switch_count = int(fusion.get("switch_count") or 0)
    return {
        "status": "ok",
        "status_detail": (
            f"同向 {confirm_count} · 切换 {switch_count}"
            f" · 裁决 {len(fusion.get('adjudicated_products') or [])}/2"
        ),
        "timestamp": str(daily.get("finished_at") or ""),
        "metrics": {"fusion": fusion},
        "sources": ["local-production/latest-status.json"],
    }


def _shadow_eval_node(business_date: str, now: datetime) -> NodeState:
    audit = None
    settled = None
    lesson_count = 0
    try:
        with closing(connect_readonly()) as connection:
            audit = connection.execute(
                """
                SELECT COUNT(*) AS cells,
                       SUM(CASE WHEN event_adjusted_direction != baseline_direction
                                THEN 1 ELSE 0 END) AS rewritten,
                       MAX(business_date) AS latest_date
                FROM forecast_event_factors
                """
            ).fetchone()
            settled = connection.execute(
                """
                SELECT COUNT(*) AS samples,
                       SUM(CASE WHEN outcome_adjusted='hit' THEN 1 ELSE 0 END) AS adjusted_hits,
                       SUM(CASE WHEN outcome_baseline='hit' THEN 1 ELSE 0 END) AS baseline_hits
                FROM forecast_event_factors
                WHERE outcome_baseline IS NOT NULL
                """
            ).fetchone()
            try:
                row = connection.execute("SELECT COUNT(*) AS n FROM agent_lessons").fetchone()
                lesson_count = int(row["n"] or 0) if row is not None else 0
            except Exception:  # noqa: BLE001 - agent_lessons may not exist yet (fail-open).
                lesson_count = 0
    except Exception:  # noqa: BLE001 - fail-open to waiting, the graph never 5xx.
        audit = None
    cells = int(audit["cells"] or 0) if audit is not None else 0
    rewritten = int(audit["rewritten"] or 0) if audit is not None else 0
    if not cells:
        return {
            "status": "waiting",
            "status_detail": "暂无定案审计：等待主线发牌积累",
            "timestamp": "",
            "metrics": {"cells": 0, "rewritten": 0, "lessons": lesson_count},
            "sources": ["forecast_event_factors"],
        }
    samples = int(settled["samples"] or 0) if settled is not None else 0
    adjusted_hits = int(settled["adjusted_hits"] or 0) if settled is not None else 0
    baseline_hits = int(settled["baseline_hits"] or 0) if settled is not None else 0
    settlement_note = (
        f" · 已结算 {samples} 格命中 {adjusted_hits / samples:.0%}"
        if samples
        else " · 等待结算积累"
    )
    return {
        "status": "ok",
        "status_detail": (
            f"审计 {cells} 格 · 事件改写 {rewritten} · 教训 {lesson_count} 条{settlement_note}"
        ),
        "timestamp": "",
        "metrics": {
            "cells": cells,
            "rewritten": rewritten,
            "lessons": lesson_count,
            "samples": samples,
            "adjusted_hits": adjusted_hits,
            "baseline_hits": baseline_hits,
        },
        "sources": ["forecast_event_factors", "agent_lessons"],
    }


def _assistant_node(business_date: str, now: datetime) -> NodeState:
    runs = _recent_assistant_runs(limit=1)
    if not runs:
        status, detail = "idle", "空闲（无最近 run，正常）"
        timestamp = ""
    else:
        latest = runs[0]
        timestamp = latest["started_at"]
        gate_result = str(latest.get("gate_result") or "")
        flag_note = f"，质量旗标 {len([item for item in gate_result.split(',') if item])} 项" if gate_result else ""
        # assistant-status.v2: a delivered answer is completed regardless of
        # gate outcomes (legacy runs are projected at read time); only a
        # failed generation is degraded.
        if latest["status"] == "completed":
            status, detail = "ok", f"最近 run completed{flag_note}"
        elif latest["status"] == "failed":
            status, detail = "degraded", "最近 run failed（无回答交付）"
        elif latest["status"] == "running":
            status, detail = "waiting", "最近 run 进行中"
        else:
            status, detail = "degraded", f"最近 run {latest['status']}"
    return {
        "status": status,
        "status_detail": detail,
        "timestamp": timestamp,
        "metrics": {},
        "sources": ["agent_runs(source=assistant_pipeline)", "llm_traces(stage=assistant)"],
    }


_NODE_BUILDERS = {
    "collect": _collect_node,
    "clean": _clean_node,
    "index": _index_node,
    "event_summary": _event_summary_node,
    "event_overview": _event_overview_node,
    "factor_score": _factor_score_node,
    "event_signal": _event_signal_node,
    "political_analysis": _political_analysis_node,
    "historical_analog": _historical_analog_node,
    "product_synthesis": _product_synthesis_node,
    "skeptic_review": _skeptic_review_node,
    "event_fusion": _event_fusion_node,
    "seven_product": _seven_product_node,
    "shadow_eval": _shadow_eval_node,
    "counter_scan": _counter_scan_node,
    "daily_interpretation": _daily_interpretation_node,
    "report_assembly": _report_assembly_node,
    "assistant": _assistant_node,
}


# ---------------------------------------------------------------------------
# Public builders.
# ---------------------------------------------------------------------------


def build_pipeline_graph(business_date: str | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    business_date = business_date or default_business_date(now)
    states = _collect_state(business_date, now)
    nodes = []
    for node_id in NODE_ORDER:
        kind, name, edge_group = NODE_KINDS[node_id]
        state = states[node_id]
        nodes.append(
            {
                "id": node_id,
                "kind": kind,
                "name": name,
                "status": state["status"],
                "status_detail": state["status_detail"],
                "timestamp": state["timestamp"],
                "edge_group": edge_group,
                "metrics": state.get("metrics", {}),
            }
        )
    edges = [{"from": source, "to": target, "kind": "flow"} for source, target in FLOW_EDGES]
    edges.extend({"from": source, "to": target, "kind": "dashed"} for source, target in DASHED_EDGES)
    edges.extend({"from": source, "to": target, "kind": "feedback"} for source, target in FEEDBACK_EDGES)
    from .pipeline_resources import resource_blocks

    report, report_state = _chain_report_state(business_date)
    resources = resource_blocks(
        business_date=business_date, now=now, report=report, report_state=report_state,
        index_documents=states["index"].get("metrics", {}).get("documents"),
    )
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "business_date": business_date,
        "generated_at": (now or _now()).isoformat(),
        "nodes": nodes,
        "edges": edges,
        **resources,
    }


def _display_cost_block(calls: int, native_amount_micros: int, native_currency: str,
                        note: str = "") -> dict[str, Any]:
    """Normalize one cost block to a CNY display value (read layer only).

    Storage keeps precise native-currency micros; USD amounts are folded to
    CNY with the versioned static anchor from settings (no live FX). Budget
    enforcement for the event-overview worker still settles natively in
    micro-USD and must not read the display fields.
    """

    if native_currency == "USD":
        fx_rate = float(settings.usd_cny_display_rate)
        fx_version = settings.usd_cny_display_fx_version
    else:
        fx_rate, fx_version = 1.0, "cny-native@1"
    return {
        "calls": calls,
        # Native values (authoritative, untouched).
        "native_amount_micros": int(native_amount_micros),
        "native_currency": native_currency,
        # Display projection (always CNY).
        "display_amount_micros": int(round(int(native_amount_micros) * fx_rate)),
        "display_currency": "CNY",
        "fx_rate": fx_rate,
        "fx_version": fx_version,
        "note": note,
    }


def _daily_cost(stage: str, business_date: str, costs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    entry = costs.get(stage) or {"calls": 0, "cost_micros": 0}
    return _display_cost_block(entry["calls"], entry["cost_micros"], "CNY")


def _agent_extra(
    node_id: str, business_date: str, state: NodeState, costs: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    extra: dict[str, Any] = {"recent_runs": [], "daily_cost": _daily_cost(node_id, business_date, costs)}
    metrics = state.get("metrics", {})
    if node_id == "counter_scan" or node_id == "daily_interpretation":
        extra["recent_runs"] = _stage_traces(node_id, business_date)
    elif node_id == "assistant":
        extra["recent_runs"] = _recent_assistant_runs()
    elif node_id == "event_summary":
        worker = _read_json(_shared_root() / "event-summary-worker" / "latest.json")
        extra["recent_runs"] = [
            {
                "run_id": "worker:last-cycle",
                "started_at": str(worker.get("generated_at") or ""),
                "latency_ms": 0,
                "status": str(worker.get("status") or ""),
                "gate_result": "",
            }
        ]
        entry = costs.get("event_summary") or {"calls": 0, "cost_micros": 0}
        extra["daily_cost"] = _display_cost_block(entry["calls"], entry["cost_micros"], "CNY")
    elif node_id == "event_overview":
        budget_metrics = metrics
        extra["recent_runs"] = []
        extra["daily_cost"] = _display_cost_block(
            budget_metrics.get("settled_count", 0),
            budget_metrics.get("used_microusd", 0),
            "USD",
            note="event_overview 原生 micro-USD（budget.json 精确结算），展示层按静态汇率折算 CNY",
        )
    return extra


def _input_summary(node_id: str, business_date: str, state: NodeState) -> dict[str, Any]:
    metrics = state.get("metrics", {})
    if node_id == "collect":
        return {"runs_today": metrics.get("runs", {}), "automation_status": metrics.get("automation_status")}
    if node_id == "clean":
        return {
            "articles_today": metrics.get("articles_today", 0),
            "clusters_today": metrics.get("clusters_today", 0),
            "quality_gate_rejected_today": metrics.get("quality_gate_rejected_today", 0),
        }
    if node_id == "index":
        return {"documents": metrics.get("documents", 0), "chunks": metrics.get("chunks", 0)}
    if node_id == "event_summary":
        return {"queue": metrics.get("queue", {}), "daily_reserved_requests": metrics.get("daily_reserved_requests")}
    if node_id == "event_overview":
        return {
            "limit_microusd": metrics.get("limit_microusd", 0),
            "used_microusd": metrics.get("used_microusd", 0),
            "failures_today": metrics.get("failures_today", 0),
        }
    if node_id == "factor_score":
        return {
            "factors_total": metrics.get("factors_total", 0),
            "factors_ready": metrics.get("factors_ready", 0),
        }
    if node_id == "seven_product":
        return {"cells": 21, "batch": metrics.get("batch")}
    if node_id == "counter_scan":
        artifact = metrics.get("artifact") or {}
        summary = artifact.get("input_summary") or {}
        return {
            "snapshot_id": summary.get("snapshot_id", ""),
            "payload_sha256": summary.get("payload_sha256", ""),
            "events": summary.get("events", 0),
            "evidence_documents": summary.get("evidence_documents", 0),
        }
    if node_id == "daily_interpretation":
        artifact = metrics.get("artifact") or {}
        summary = artifact.get("input_summary") or {}
        return {
            "snapshot_id": summary.get("snapshot_id", ""),
            "payload_sha256": summary.get("payload_sha256", ""),
            "counter_scan_status": summary.get("counter_scan_status", ""),
        }
    if node_id == "report_assembly":
        return {"overall_status": metrics.get("overall_status", ""), "warnings_count": metrics.get("warnings_count", 0)}
    if node_id == "assistant":
        runs = _recent_assistant_runs(limit=1)
        return {
            "question": runs[0]["question"] if runs else "",
            "note": "问题摘要；context_pack 深链见 agent trace",
        }
    return {}


def _output_summary(node_id: str, business_date: str, state: NodeState) -> dict[str, Any]:
    metrics = state.get("metrics", {})
    if node_id == "collect":
        runs = metrics.get("runs", {})
        return {"articles_found_note": "见 clean 节点", "runs": runs}
    if node_id == "clean":
        return metrics
    if node_id == "index":
        return {"documents": metrics.get("documents", 0), "vectors": metrics.get("vectors", 0)}
    if node_id == "event_summary":
        queue = metrics.get("queue", {})
        return {
            "completed": queue.get("completed", 0),
            "rejected": queue.get("rejected", 0),
            "failed": queue.get("failed", 0),
        }
    if node_id == "event_overview":
        return {"settled_count": metrics.get("settled_count", 0), "failures_today": metrics.get("failures_today", 0)}
    if node_id == "factor_score":
        return {"factor_names": metrics.get("factor_names", []), "factor_details": metrics.get("factor_details", [])}
    if node_id == "seven_product":
        batch = metrics.get("batch") or {}
        return {
            "formal": batch.get("formal_count", 0),
            "reference": batch.get("reference_count", 0),
            "unavailable": batch.get("unavailable_count", 0),
        }
    if node_id == "counter_scan":
        artifact = metrics.get("artifact") or {}
        return {
            "scan_outcome": artifact.get("scan_outcome", ""),
            "findings": len(artifact.get("findings") or []),
            "stripped": int((artifact.get("sanitized") or {}).get("findings_stripped") or 0),
        }
    if node_id == "daily_interpretation":
        artifact = metrics.get("artifact") or {}
        sections = artifact.get("sections") or []
        return {
            "sections": len(sections),
            "number_refs": sum(len((section or {}).get("number_refs") or []) for section in sections),
        }
    if node_id == "report_assembly":
        return {"morning_brief_file": metrics.get("morning_brief_file", "")}
    if node_id == "assistant":
        runs = _recent_assistant_runs(limit=1)
        latest = runs[0] if runs else None
        return {
            "recent_run_status": latest["status"] if latest else "idle",
            "recent_run_derived": latest.get("derived_status") or "" if latest else "",
        }
    return {}


def _evidence_entries(node_id: str, business_date: str, state: NodeState) -> list[dict[str, str]]:
    metrics = state.get("metrics", {})
    entries: list[dict[str, str]] = []
    if node_id == "counter_scan":
        artifact = metrics.get("artifact") or {}
        for finding in (artifact.get("findings") or [])[:8]:
            for support in ((finding or {}).get("claim") or {}).get("supports") or []:
                entries.append({
                    "kind": "doc", "id": str(support.get("doc_id") or ""),
                    "note": str(support.get("quote", "")[:80]),
                })
    elif node_id == "daily_interpretation":
        artifact = metrics.get("artifact") or {}
        for section in (artifact.get("sections") or [])[:8]:
            for ref in (section or {}).get("number_refs") or []:
                entries.append({
                    "kind": "snapshot_path", "id": str(ref.get("source_ref") or ""),
                    "note": str(ref.get("value") or ""),
                })
    elif node_id == "assistant":
        for doc_id in _assistant_cited_doc_ids():
            entries.append({"kind": "doc", "id": doc_id, "note": "最近回答引用"})
    elif node_id == "seven_product":
        batch = metrics.get("batch") or {}
        if batch.get("batch_id"):
            entries.append({
                "kind": "batch", "id": str(batch["batch_id"]),
                "note": f"business_date={batch.get('business_date')}",
            })
    for source in state.get("sources", [])[:5]:
        if source:
            entries.append({"kind": "state_source", "id": source, "note": ""})
    return entries[:16]


def build_pipeline_node_detail(
    node_id: str, business_date: str | None = None, *, now: datetime | None = None
) -> dict[str, Any]:
    if node_id not in NODE_KINDS:
        raise ValueError("pipeline_node_unknown")
    business_date = business_date or default_business_date(now)
    state = _collect_state(business_date, now)[node_id]
    kind, name, _ = NODE_KINDS[node_id]
    costs = _llm_stage_costs(business_date)
    detail = {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "business_date": business_date,
        "node_id": node_id,
        "kind": kind,
        "name": name,
        "status_block": {
            "status": state["status"],
            "status_detail": state["status_detail"],
            "timestamp": state["timestamp"],
            "metrics": state.get("metrics", {}),
            "sources": [source for source in state.get("sources", []) if source],
        },
        "input_summary": _input_summary(node_id, business_date, state),
        "output_summary": _output_summary(node_id, business_date, state),
        "evidence_entries": _evidence_entries(node_id, business_date, state),
    }
    if kind == "agent":
        detail["agent_extra"] = _agent_extra(node_id, business_date, state, costs)
        from .agent_inspection import agent_implementation_profile

        profile = agent_implementation_profile(node_id)
        if profile is not None:
            detail["implementation_profile"] = profile
    return detail


__all__ = [
    "build_pipeline_graph",
    "build_pipeline_node_detail",
    "default_business_date",
    "NODE_ORDER",
]

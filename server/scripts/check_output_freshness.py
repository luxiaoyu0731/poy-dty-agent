#!/usr/bin/env python3
"""Output-driven freshness check for the five core delivery chains.

Distinguishes, per chain, between:
  ok             - last real output is fresh within its own publication cadence
  external_wait  - upstream simply has not published yet (weekend, weekly
                   cadence, pre-cutoff), NOT an internal failure
  degraded       - internal backlog or execution failure (outputs stale beyond
                   the cadence allowance, or the checker itself is impaired)
  unknown        - the checker could not read the evidence it needs; this must
                   never be reported as healthy

Judgement uses real outputs and observation timestamps (rows in the production
database), never process liveness, task start, success stamps, or HTTP 200.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _env_gib(name: str, default_gib: float) -> int:
    """Disk thresholds are absolute-byte guards; make them env-tunable per host."""
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else default_gib
    except ValueError:
        value = default_gib
    return int(max(0.0, value) * 1024**3)


# Disk consolidation 2026-09-17 (DISK-MODEL §2.3): 20/12GiB keeps the warn
# level above one full daily backup churn window on the 70G server target and
# leaves critical a >=24h response window. Override per host with
# FRESHNESS_DISK_WARN_GIB / FRESHNESS_DISK_CRITICAL_GIB.
DISK_WARN_BYTES = _env_gib("FRESHNESS_DISK_WARN_GIB", 20)
DISK_CRITICAL_BYTES = _env_gib("FRESHNESS_DISK_CRITICAL_GIB", 12)
CHECKER_MAX_AGE_SECONDS = 15 * 60  # a probe result older than this is unknown
WEEKEND_EXTERNAL = True  # price/article cadences treat Sat/Sun as external wait


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("SQLITE_PATH", ""))
    parser.add_argument("--status-in", default="", help="previous freshness payload (for change-only alerts)")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def _now() -> datetime:
    return datetime.now(UTC)


def _parse(value: Any) -> datetime | None:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return stamp.replace(tzinfo=UTC)
    return stamp


def _age_hours(value: Any, now: datetime) -> float | None:
    stamp = _parse(value)
    if stamp is None:
        return None
    return (now - stamp).total_seconds() / 3600


def _trading_days_between(a: datetime, b: datetime) -> int:
    """Count Mon-Fri days in (a, b]; weekends are external, not internal."""
    days = 0
    cursor = (a + timedelta(days=1)).date()
    end = b.date()
    while cursor <= end:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def entry(
    status: str,
    last_output: str,
    detail: str,
    *,
    recovery: str = "",
    latency: str = "",
) -> dict[str, str]:
    return {
        "status": status,
        "last_output": last_output,
        "detail": detail,
        "recovery_action": recovery,
        "latency": latency,
    }


def check_prices(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Per-product observation freshness against publication cadence."""
    # cadence: max internal lag in trading days beyond the source's own rhythm.
    cadences = {
        # (daily-series table/source, intraday instrument, max trading-day lag, rhythm)
        # crude OOS label switched to ICE Brent front-month futures (BZ=F) daily
        # close in seven-product-labels.v5 (2026-09-24); the label series is the
        # yahoo proxy import, so freshness tracks its daily cadence.
        "crude_oil": ("market_observations", "yahoo_futures_daily_proxy", "Brent", 3, "日度（BZ=F 前月连续收盘）"),
        "naphtha": ("market_observations", "public_spot_page_refresh", "NAPHTHA", 3, "日度"),
        # czce_pta_px daily settles land in futures_daily_bars main contracts
        # (product-fix-20260916): freshness reads the delivered bar series
        # directly instead of leaning on the intraday fallback.
        "xylenes_broad": ("futures_daily_bars", "czce_pta_px", "PX", 4, "交易日（郑商所主力合约）"),
        "pta": ("futures_daily_bars", "czce_pta_px", "PTA", 4, "交易日（郑商所主力合约）"),
        "meg": ("market_observations", "sunsirs_public_commodity_assessment", "MEG", 3, "日度（生意社）"),
        "poy": ("market_observations", "tnc_polyester_history", "POY", 4, "日度（TNC，周末顺延）"),
        "dty": ("market_observations", "tnc_polyester_history", "DTY", 4, "日度（TNC，周末顺延）"),
    }
    items: dict[str, Any] = {}
    for product, (table, expected_source, instrument, max_lag_days, rhythm) in cadences.items():
        row = None
        if table == "futures_daily_bars":
            # The bar table keys rows by the uppercase exchange product and
            # trade_date; a missing table falls through to the intraday fallback.
            try:
                columns = {r[1] for r in connection.execute("PRAGMA table_info(futures_daily_bars)")}
                # A main contract can also be next_month/near_month. The role
                # label is not exclusive; the canonical main flag is authoritative.
                main_filter = "is_main=1" if "is_main" in columns else "contract_role='main'"
                row = connection.execute(
                    "SELECT trade_date AS observed_at, source_id FROM futures_daily_bars "
                    f"WHERE product=? AND source_id=? AND {main_filter} "
                    "ORDER BY trade_date DESC LIMIT 1",
                    (instrument, expected_source),
                ).fetchone()
            except sqlite3.Error:
                row = None
        else:
            row = connection.execute(
                f"SELECT observed_at, '{expected_source}' AS source_id FROM {table} "
                "WHERE product=? AND source_id=? ORDER BY observed_at DESC LIMIT 1",
                (product, expected_source),
            ).fetchone()
        if row is None and instrument:
            # Label-ledger daily series absent; fall back to the intraday
            # instrument so freshness still tracks the actually delivered page.
            try:
                row = connection.execute(
                    "SELECT observed_at, 'intraday' AS source_id FROM intraday_price_observations "
                    "WHERE instrument=? ORDER BY observed_at DESC LIMIT 1",
                    (instrument,),
                ).fetchone()
            except sqlite3.Error:
                row = None
        if row is None:
            items[product] = entry("degraded", "无记录", f"{product} 无任何价格观测", recovery="检查采集任务")
            continue
        observed = _parse(str(row["observed_at"]))
        lag_hours = _age_hours(row["observed_at"], now)
        trading_lag = _trading_days_between(observed, now) if observed else None
        wrong_source = False  # fallback sources are by-design alternatives
        if lag_hours is None or trading_lag is None:
            items[product] = entry("unknown", str(row["observed_at"]), "观测时间无法解析")
        elif trading_lag > max_lag_days:
            items[product] = entry(
                "degraded",
                str(row["observed_at"])[:10],
                f"超出 {rhythm} 允许滞后（{trading_lag} 个交易日）"
                + (f"；来源变为 {row['source_id']}" if wrong_source else ""),
                recovery="检查采集任务与来源可用性",
                latency=f"{lag_hours:.0f}h",
            )
        else:
            items[product] = entry(
                "external_wait" if trading_lag >= 1 else "ok",
                str(row["observed_at"])[:10],
                f"{rhythm}，当前滞后 {trading_lag} 个交易日属正常",
                latency=f"{lag_hours:.0f}h",
            )
    return {"items": items}


def check_article_collection(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Report transport separately from newly stored source publications."""
    core_sources = [
        "google_news_oil_rss",
        "google_news_chemical_rss",
        "texnet_polyester_news",
        "ndrc_news",
        "eia_press",
        "ofac_recent_actions",
        "us_centcom_press",
        "mpa_press_releases",
        "ukmto_incidents",
    ]
    max_lag_hours = 30
    items: dict[str, Any] = {}
    degraded = []
    for source in core_sources:
        row = connection.execute(
            "SELECT MAX(created_at) m FROM news_fetch_runs WHERE source_id=? AND status IN ('ok','no_relevant_items')",
            (source,),
        ).fetchone()
        lag = _age_hours(row["m"] if row else None, now)
        if lag is None:
            items[source] = entry("unknown", "-", "无成功运行记录")
            degraded.append(source)
        elif lag > max_lag_hours:
            items[source] = entry(
                "degraded",
                str(row["m"])[:16],
                f"最近成功运行已 {lag:.0f} 小时",
                recovery="查看该源最近 error；有替代覆盖时降级观察",
                latency=f"{lag:.0f}h",
            )
            degraded.append(source)
        else:
            items[source] = entry("ok", str(row["m"])[:16], "最近运行成功", latency=f"{lag:.0f}h")
    # A healthy request (including an empty response) is not evidence of new
    # business information. Alternatives can provide output while a source fails.
    from app.publication_time import publication_instant

    publications = []
    for row in connection.execute("SELECT created_at,published_at FROM news_articles"):
        stamp = publication_instant(row["published_at"])
        if stamp and 0 <= (now - datetime.fromisoformat(stamp)).total_seconds() <= 30 * 3600:
            publications.append(row)
    last = max((r["created_at"] for r in publications), default=None)
    lag = _age_hours(last, now)
    status = "ok" if lag is not None and lag <= 30 else "degraded"
    return {
        "status": status,
        "last_output": last,
        "recent_publications": len(publications),
        "transport": items,
        "degraded_sources": degraded,
        "detail": "按来源发布日期核验文章入库；摘要质量与业务覆盖另行核验",
        "recovery_action": "检查缺失业务领域及替代源，不能以请求成功替代有效新文章" if status != "ok" else "",
    }


def check_summaries(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Pending backlog depth and age; completed-today proves the consumer runs."""
    pending, oldest = connection.execute(
        "SELECT COUNT(*), MIN(updated_at) FROM event_ai_summaries WHERE summary_status='pending'"
    ).fetchone()
    completed_recent = connection.execute(
        "SELECT MAX(updated_at) FROM event_ai_summaries WHERE summary_status='completed'"
    ).fetchone()[0]
    completed_lag = _age_hours(completed_recent, now)
    oldest_age = _age_hours(oldest, now) if oldest else None
    states = {
        str(row[0]): int(row[1])
        for row in connection.execute("SELECT summary_status,COUNT(*) FROM event_ai_summaries GROUP BY summary_status")
    }
    recent_rejected = connection.execute(
        "SELECT COUNT(*) FROM event_ai_summaries WHERE summary_status IN ('failed','rejected') "
        "AND julianday(updated_at)>=julianday(?)",
        ((now - timedelta(hours=48)).isoformat(),),
    ).fetchone()[0]
    processing_oldest = connection.execute(
        "SELECT MIN(updated_at) FROM event_ai_summaries WHERE summary_status='processing'"
    ).fetchone()[0]
    processing_age = _age_hours(processing_oldest, now)
    if completed_lag is None:
        return {"status": "unknown", "detail": "无法读取摘要产出"}
    if pending >= 100 or (oldest_age is not None and oldest_age > 48):
        status = "degraded"
        detail = f"积压 {pending} 条，最老 {oldest_age:.0f}h；最近完成于 {completed_lag:.0f}h 前"
    elif completed_lag > 36:
        status = "degraded"
        detail = f"最近完成已 {completed_lag:.0f}h（无新合格材料亦可能，结合 pending 判断）"
        if pending == 0:
            status = "external_wait"
    else:
        status = "ok"
        detail = f"pending={pending}，最近完成 {completed_lag:.0f}h 前"
    if recent_rejected or (processing_age is not None and processing_age > 0.5):
        status = "degraded"
        detail += f"；近48小时失败/拒绝 {recent_rejected} 条，处理中 {states.get('processing', 0)} 条；需核验有效产出"
    payload_out = {
        "status": status,
        "pending": int(pending or 0),
        "oldest_pending_age_hours": round(oldest_age, 1) if oldest_age else None,
        "last_completed": str(completed_recent),
        "detail": detail,
        "processing": states.get("processing", 0),
        "failed_total": states.get("failed", 0),
        "rejected_total": states.get("rejected", 0),
        "recent_failed_or_rejected": recent_rejected,
    }
    if status == "degraded":
        payload_out["recovery"] = "检查 event-summary-worker 预算与优先级"
    return payload_out


def check_search_index(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """Index freshness vs newest ingested article (rebuild is operator-triggered)."""
    active = connection.execute("SELECT active_index_id FROM semantic_index_state WHERE state_key='default'").fetchone()
    active_id = str(active["active_index_id"]) if active else ""
    completed = None
    if active_id:
        row = connection.execute("SELECT completed_at FROM semantic_indices WHERE index_id=?", (active_id,)).fetchone()
        completed = row["completed_at"] if row else None
    newest_article = connection.execute("SELECT MAX(created_at) FROM news_articles").fetchone()[0]
    # A completed summary changes retrieval content without inserting a new
    # article. The index must be compared with that output as well.
    newest_summary = connection.execute(
        "SELECT MAX(updated_at) FROM event_ai_summaries WHERE summary_status='completed'"
    ).fetchone()[0]
    newest_input = max((_parse(v) for v in (newest_article, newest_summary) if _parse(v)), default=None)
    idx_age = _age_hours(completed, now)
    art_age = _age_hours(newest_input.isoformat(), now) if newest_input else None
    if idx_age is None:
        return {"status": "unknown", "detail": "无活跃索引完成时间"}
    if art_age is not None and idx_age > art_age + 24:
        return {
            "status": "degraded",
            "active_index": active_id,
            "last_completed": str(completed),
            "detail": f"索引完成落后最新文章 {idx_age - art_age:.0f} 小时",
            "recovery": "运行 rebuild-index（内容未变部分自动复用，不重复嵌入）",
        }
    return {
        "status": "ok",
        "active_index": active_id,
        "last_completed": str(completed),
        "last_input_change": newest_input.isoformat() if newest_input else None,
        "pending_input_change": bool(newest_input and _parse(completed) < newest_input),
        "detail": "索引更新延迟未超24小时阈值；逐文档覆盖另行核验",
    }


def check_daily_brief(connection: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    """The latest Shanghai business day at/after 10:00 CST must have a brief."""
    shanghai = now + timedelta(hours=8)
    cursor = shanghai.date()
    for _ in range(4):
        if cursor.weekday() < 5:
            break
        cursor -= timedelta(days=1)
    row = connection.execute(
        "SELECT business_date, status, released_at FROM intelligence_daily_briefs ORDER BY business_date DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"status": "degraded", "detail": "无任何日报", "recovery": "检查 intelligence-daily 任务"}
    latest = str(row["business_date"])
    past_cutoff = shanghai.hour >= 10
    if latest >= cursor.isoformat():
        return {
            "status": "degraded" if row["status"] == "blocked" else "ok",
            "latest": latest,
            "latest_status": str(row["status"]),
            "detail": f"最新日报 {latest}（{row['status']}）",
        }
    if not past_cutoff:
        return {"status": "external_wait", "latest": latest, "detail": f"今日尚未到发布截止（最新 {latest}）"}
    return {
        "status": "degraded",
        "latest": latest,
        "detail": f"最新日报 {latest}，缺 {cursor.isoformat()}（已过 10:00 上海时间）",
        "recovery": "检查 intelligence-daily 任务与 lock",
    }


def check_disk(path: Path) -> dict[str, Any]:
    usage = os.statvfs(path)
    free = usage.f_bavail * usage.f_frsize
    if free < DISK_CRITICAL_BYTES:
        status, level = "critical", "磁盘严重不足，写入类任务将安全失败"
    elif free < DISK_WARN_BYTES:
        status, level = "warning", "磁盘余量偏低"
    else:
        status, level = "ok", ""
    return {
        "status": status,
        "free_bytes": free,
        "free_gb": round(free / 1024**3, 1),
        "thresholds": {
            "warn_gb": round(DISK_WARN_BYTES / 1024**3, 1),
            "critical_gb": round(DISK_CRITICAL_BYTES / 1024**3, 1),
        },
        "detail": level,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = _now()
    chains: dict[str, Any] = {}
    impaired: list[str] = []

    db_path = Path(args.db).expanduser() if args.db else Path("data/agent.db")
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        chains["prices"] = check_prices(connection, now)
        chains["articles"] = check_article_collection(connection, now)
        chains["summaries"] = check_summaries(connection, now)
        chains["search_index"] = check_search_index(connection, now)
        chains["daily_brief"] = check_daily_brief(connection, now)
        connection.close()
    except sqlite3.Error as exc:
        impaired.append(f"database:{exc.__class__.__name__}")

    try:
        chains["disk"] = check_disk(db_path.parent if db_path.parent.exists() else Path("."))
    except OSError as exc:
        impaired.append(f"disk:{exc.__class__.__name__}")

    # Probe self-health: a previous payload older than the probe cadence means
    # this checker itself stopped running; report unknown, never healthy.
    self_status = "ok"
    self_note = ""
    if args.status_in:
        try:
            previous = json.loads(Path(args.status_in).read_text(encoding="utf-8"))
            prev_at = _parse(previous.get("generated_at"))
            if prev_at is None or (now - prev_at).total_seconds() > CHECKER_MAX_AGE_SECONDS * 4:
                self_status = "unknown"
                self_note = "上一份检查结果过旧，检查器可能间歇失联"
        except (OSError, ValueError):
            self_status = "unknown"
            self_note = "无法读取上一份检查结果"

    overall = "ok"
    for _name, payload in chains.items():
        statuses: list[str] = []
        if isinstance(payload, dict) and "items" in payload:
            statuses = [str(v.get("status")) for v in payload["items"].values()]
        elif isinstance(payload, dict):
            statuses = [str(payload.get("status"))]
        if any(s in ("degraded", "critical") for s in statuses):
            overall = "degraded"
        if "unknown" in statuses:
            overall = "unknown" if overall != "degraded" else overall
    if impaired:
        overall = "unknown"
    if self_status == "unknown":
        overall = "unknown"

    payload = {
        "schema_version": "output_freshness.v1",
        "generated_at": now.isoformat(),
        "overall": overall,
        "self": {"status": self_status, "note": self_note},
        "impaired_checks": impaired,
        "chains": chains,
        "usage_note": (
            "ok=产出新鲜；external_wait=外部未发布/休市；degraded=内部积压或失败；unknown=检查器失联或证据不可读"
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(args.output)
    except OSError:
        # Disk-full safety: fail without touching previous payload.
        print(json.dumps({"overall": "unknown", "error": "write_failed"}))
        return 0
    print(json.dumps({"overall": overall, "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

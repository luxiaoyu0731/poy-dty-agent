#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
sys.path.insert(0, str(SERVER_ROOT))

from app.foundation_utils import stable_hash  # noqa: E402
from app.settings import settings  # noqa: E402
from app.storage import connect, upsert_political_case_memory  # noqa: E402

DEFAULT_DB = SERVER_ROOT / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "production-rag"
POLITICAL_CATEGORIES = {
    "sanctions_geopolitics",
    "oil_policy",
    "china_policy",
    "shipping_security",
    "macro_finance",
    "company_capacity",
    "supply_disruption",
    "demand_policy",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build political event case memory from historical visible events.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--train-start", default="2025-01-01")
    parser.add_argument("--train-end", default="2025-12-31")
    parser.add_argument("--visible-at", default="")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-db", action="store_true")
    args = parser.parse_args(argv)
    db_path = args.db.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()

    original_sqlite_path = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(db_path))
    try:
        report = build_cases(
            db_path=db_path,
            train_start=args.train_start,
            train_end=args.train_end,
            visible_at=args.visible_at or f"{args.train_end}T23:59:59+00:00",
            apply=args.apply,
            backup_db=args.backup_db,
            output_dir=output_dir,
        )
    finally:
        object.__setattr__(settings, "sqlite_path", original_sqlite_path)

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / "political-case-memory-build-latest.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "status": report["status"]}, ensure_ascii=False))
    return 0 if report["status"] in {"dry_run", "success"} else 1


def build_cases(
    *,
    db_path: Path,
    train_start: str,
    train_end: str,
    visible_at: str,
    apply: bool,
    backup_db: bool,
    output_dir: Path,
) -> dict[str, Any]:
    backup_path = ""
    if apply and backup_db:
        backup_dir = output_dir / "db-backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = str(
            backup_dir
            / f"{db_path.name}.pre_political_case_memory_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.sqlite"
        )
        shutil.copy2(db_path, backup_path)

    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)) as read_connection, read_connection:
        read_connection.row_factory = sqlite3.Row
        rows = load_training_events(read_connection, train_start=train_start, train_end=train_end)

    cases = [case_from_row(row, train_start=train_start, train_end=train_end, visible_at=visible_at) for row in rows]
    stored = 0
    if apply:
        with closing(connect()):
            pass
        for case in cases:
            upsert_political_case_memory(case_id=case["case_id"], payload=case)
            stored += 1

    return {
        "schema_version": "political_case_memory_build.1",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "success" if apply else "dry_run",
        "train_start": train_start,
        "train_end": train_end,
        "visible_at": visible_at,
        "candidate_rows": len(rows),
        "case_count": len(cases),
        "stored_rows": stored,
        "backup_path": backup_path,
        "case_type_distribution": distribution(cases, "event_type"),
        "direction_distribution": distribution(cases, "price_direction"),
        "sample_case_ids": [case["case_id"] for case in cases[:10]],
        "writes_database": apply,
    }


def load_training_events(connection: sqlite3.Connection, *, train_start: str, train_end: str) -> list[sqlite3.Row]:
    if not table_exists(connection, "event_observations"):
        return []
    placeholders = ",".join("?" for _ in POLITICAL_CATEGORIES)
    return connection.execute(
        f"""
        SELECT *
        FROM event_observations
        WHERE substr(occurred_at, 1, 10) >= ?
          AND substr(occurred_at, 1, 10) <= ?
          AND event_type IN ({placeholders})
        ORDER BY occurred_at ASC, created_at ASC
        """,
        (train_start, train_end, *sorted(POLITICAL_CATEGORIES)),
    ).fetchall()


def case_from_row(row: sqlite3.Row, *, train_start: str, train_end: str, visible_at: str) -> dict[str, Any]:
    event_type = str(row["event_type"] or "political_event")
    affected_products = parse_json(row["affected_products"], [])
    title = str(row["title"] or "")
    direction = str(row["direction"] or "中性")
    summary = str(row["summary"] or "")
    case_id = f"political_case_{stable_hash({'event': row['event_record_id'], 'train': [train_start, train_end]})}"
    stakeholders = stakeholders_for(event_type, title, summary)
    return {
        "case_id": case_id,
        "event_date": normalize_date(str(row["occurred_at"] or "")),
        "event_type": event_type,
        "title": title,
        "summary": summary,
        "stakeholders": stakeholders,
        "interest_map": interest_map_for(event_type, stakeholders),
        "power_structure": power_structure_for(event_type, stakeholders),
        "stated_position": "以公开事件表述为准，进入预测时需区分表态与可执行行动。",
        "real_action": real_action_for(event_type, summary),
        "action_boundary": action_boundary_for(event_type),
        "timing_window": "短周期先观察价格是否已反映，后续按新增证据滚动复核。",
        "compromise_space": compromise_space_for(event_type),
        "market_reaction": f"历史事件方向记录为{direction}，强度为{row['impact_strength']}。",
        "priced_in_pattern": "若价格已在事件前后快速反应，后续影响按衰减处理，不直接外推。",
        "decay_pattern": "地缘/制裁/政策事件默认设置衰减检查，除非后续出现执行升级或供应实质受限。",
        "transmission_path": transmission_path_for(event_type, affected_products),
        "affected_products": affected_products,
        "price_direction": direction,
        "confidence": confidence_for(str(row["evidence_level"] or "C"), str(row["impact_strength"] or "")),
        "outcome_window": "h1 及后续滚动窗口按真实规格价格评分。",
        "posterior_result": "该案例来自训练期事件，仅作为 2026 前推测试前的历史复盘记忆。",
        "lessons": lessons_for(event_type),
        "reusable_rules": reusable_rules_for(event_type),
        "evidence_refs": [f"event:{row['event_record_id']}"],
        "visible_at": visible_at,
        "train_period": f"{train_start}..{train_end}",
        "metadata": {
            "source_table": "event_observations",
            "source_record_id": row["event_record_id"],
            "evidence_level": row["evidence_level"],
            "requires_human_review": bool(row["requires_human_review"]),
        },
    }


def stakeholders_for(event_type: str, title: str, summary: str) -> list[str]:
    text = f"{title} {summary}".lower()
    stakeholders = ["原料市场", "聚酯产业链"]
    if event_type == "sanctions_geopolitics" or any(
        term in text for term in ("iran", "ofac", "sanction", "伊朗", "制裁")
    ):
        stakeholders.extend(["制裁发布方", "受制裁实体", "能源贸易商"])
    if event_type == "oil_policy" or "opec" in text:
        stakeholders.extend(["产油国", "能源消费方", "期货市场"])
    if event_type == "china_policy":
        stakeholders.extend(["国内政策部门", "下游制造企业"])
    if event_type == "shipping_security" or any(
        term in text for term in ("shipping", "hormuz", "red sea", "航运", "霍尔木兹")
    ):
        stakeholders.extend(["航运公司", "保险机构", "原油进口方"])
    return sorted(set(stakeholders))


def interest_map_for(event_type: str, stakeholders: list[str]) -> dict[str, str]:
    return {stakeholder: "关注成本、供应安全、价格传导或执行边界" for stakeholder in stakeholders}


def power_structure_for(event_type: str, stakeholders: list[str]) -> dict[str, str]:
    if event_type in {"sanctions_geopolitics", "oil_policy", "china_policy"}:
        return {
            "主导方": "政策/监管/产油决策主体",
            "受影响方": "贸易商、生产企业、下游采购方",
            "市场角色": "通过风险溢价和预期调整定价",
        }
    return {"主导方": stakeholders[0] if stakeholders else "事件发起方", "市场角色": "通过供应、物流或需求预期传导"}


def real_action_for(event_type: str, summary: str) -> str:
    if event_type == "sanctions_geopolitics":
        return "检查是否存在制裁名单、执行公告、船运受限或金融结算影响。"
    if event_type == "oil_policy":
        return "检查配额、产量、库存和官方会议决议是否落地。"
    if event_type == "shipping_security":
        return "检查航线绕行、保险费、运价和交付延迟是否实际发生。"
    return "检查公开表态是否转化为可观察的供应、需求或价格行为。"


def action_boundary_for(event_type: str) -> str:
    return {
        "sanctions_geopolitics": "单纯表态不能直接进入行动，需执行主体和影响链条确认。",
        "oil_policy": "口头减产或会议预期需等待产量、库存或官方执行证据。",
        "shipping_security": "安全事件需看到航运成本或供应节奏变化。",
    }.get(event_type, "需区分原则、立场和可执行策略。")


def compromise_space_for(event_type: str) -> str:
    if event_type in {"sanctions_geopolitics", "oil_policy"}:
        return "存在谈判、豁免、替代供应或市场提前定价空间。"
    return "存在执行节奏、市场吸收和需求抵消空间。"


def transmission_path_for(event_type: str, affected_products: list[str]) -> list[str]:
    base = ["事件", "风险溢价/供需预期", "原油/石脑油", "PX/PTA/MEG", "POY/DTY"]
    if affected_products:
        base.append("影响产品：" + "、".join(str(item) for item in affected_products[:6]))
    return base


def confidence_for(tier: str, strength: str) -> float:
    base = {"A": 0.72, "B": 0.62, "C": 0.48, "D": 0.35}.get(tier, 0.45)
    if str(strength).lower() in {"high", "strong", "高"}:
        base += 0.08
    return round(min(base, 0.86), 2)


def lessons_for(event_type: str) -> list[str]:
    return [
        "先判断利益格局和执行主体，再判断价格方向。",
        "价格已经提前反映时，后续影响需要衰减。",
        "没有直接传导到 POY/DTY 的证据时，只能作为观察信号。",
    ]


def reusable_rules_for(event_type: str) -> list[str]:
    return [
        "表态不等于执行，执行不等于传导，传导不等于可行动。",
        "高风险事件必须经过价格、供需和反证三重校验。",
    ]


def normalize_date(value: str) -> str:
    return value[:10] if len(value) >= 10 else value


def parse_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, list | dict):
        return value
    try:
        return json.loads(str(value or ""))
    except json.JSONDecodeError:
        return fallback


def distribution(items: list[dict[str, Any]], field: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for item in items:
        key = str(item.get(field) or "")
        result[key] = result.get(key, 0) + 1
    return result


def table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)).fetchone()
        is not None
    )


if __name__ == "__main__":
    raise SystemExit(main())

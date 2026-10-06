from __future__ import annotations

import argparse
import csv
import json
import shutil
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INPUT = REPO_ROOT / "docs" / "data-templates" / "polyester_chain_observations.csv"
DEFAULT_DB_PATH = REPO_ROOT / "server" / "data" / "agent.db"
DEFAULT_BACKUP_DIR = REPO_ROOT / ".codex-run" / "db-backups"
DEFAULT_SUMMARY_PATH = REPO_ROOT / ".codex-run" / "ccf-industry-import-summary-latest.json"

INDUSTRY_OBSERVATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS industry_observations (
  observation_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  product TEXT NOT NULL,
  metric TEXT NOT NULL,
  market TEXT NOT NULL,
  region TEXT NOT NULL,
  value REAL,
  unit TEXT NOT NULL,
  frequency TEXT NOT NULL,
  evidence_level TEXT NOT NULL,
  evidence_url TEXT NOT NULL,
  notes TEXT NOT NULL,
  raw TEXT NOT NULL
);
"""

FIELD_ALIASES = {
    "observed_at": ("observed_at", "date", "日期", "时间", "观测日期", "数据日期"),
    "source_id": ("source_id", "来源ID", "来源", "数据源"),
    "product": ("product", "产品", "品种", "类别"),
    "metric": ("metric", "指标", "指标名称", "项目", "字段"),
    "value": ("value", "数值", "值", "均值", "均价", "价格"),
    "unit": ("unit", "单位"),
    "frequency": ("frequency", "频率", "周期"),
    "market": ("market", "市场"),
    "region": ("region", "地区", "区域"),
    "evidence_level": ("evidence_level", "tier", "证据等级", "等级"),
    "source_url": ("source_url", "evidence_url", "链接", "来源链接", "页面链接", "url", "URL"),
    "notes": ("notes", "备注", "说明"),
}

PRODUCT_ALIASES = {
    "POLYESTER": "POLYESTER",
    "聚酯": "POLYESTER",
    "聚酯产业链": "POLYESTER",
    "PX": "PX",
    "PTA": "PTA",
    "MEG": "MEG",
    "POY": "POY",
    "涤纶POY": "POY",
    "涤纶 POY": "POY",
    "DTY": "DTY",
    "涤纶DTY": "DTY",
    "涤纶 DTY": "DTY",
}

METRIC_ALIASES = {
    "polyester_operating_rate": "polyester_operating_rate",
    "operating_rate": "polyester_operating_rate",
    "run_rate": "polyester_operating_rate",
    "聚酯开工": "polyester_operating_rate",
    "聚酯负荷": "polyester_operating_rate",
    "开工负荷": "polyester_operating_rate",
    "开工率": "polyester_operating_rate",
    "负荷": "polyester_operating_rate",
    "poy_inventory": "poy_inventory",
    "POY库存": "poy_inventory",
    "POY库存天数": "poy_inventory",
    "涤纶POY库存": "poy_inventory",
    "dty_inventory": "dty_inventory",
    "DTY库存": "dty_inventory",
    "DTY库存天数": "dty_inventory",
    "涤纶DTY库存": "dty_inventory",
    "polyester_profit": "polyester_profit",
    "polyester_margin": "polyester_profit",
    "polyester_cashflow": "polyester_profit",
    "聚酯利润": "polyester_profit",
    "聚酯现金流": "polyester_profit",
    "聚酯加工差": "polyester_profit",
    "poy_profit": "poy_profit",
    "poy_margin": "poy_profit",
    "poy_cashflow": "poy_profit",
    "POY利润": "poy_profit",
    "POY现金流": "poy_profit",
    "POY加工差": "poy_profit",
    "dty_profit": "dty_profit",
    "dty_margin": "dty_profit",
    "dty_cashflow": "dty_profit",
    "DTY利润": "dty_profit",
    "DTY现金流": "dty_profit",
    "DTY加工差": "dty_profit",
}

METRIC_DEFAULTS = {
    "polyester_operating_rate": {"product": "POLYESTER", "unit": "%", "frequency": "weekly"},
    "poy_inventory": {"product": "POY", "unit": "天", "frequency": "weekly"},
    "dty_inventory": {"product": "DTY", "unit": "天", "frequency": "weekly"},
    "polyester_profit": {"product": "POLYESTER", "unit": "元/吨", "frequency": "daily"},
    "poy_profit": {"product": "POY", "unit": "元/吨", "frequency": "daily"},
    "dty_profit": {"product": "DTY", "unit": "元/吨", "frequency": "daily"},
}

PRODUCT_SCOPED_METRICS = {
    "inventory": {"unit": "天", "frequency": "weekly", "products": {"POY", "DTY", "PTA", "MEG"}},
    "profit": {"unit": "元/吨", "frequency": "daily", "products": {"POY", "DTY", "PTA", "MEG"}},
    "operating_rate": {"unit": "%", "frequency": "weekly", "products": {"POY", "DTY", "PTA", "MEG"}},
    "product_operating_rate": {"unit": "%", "frequency": "weekly", "products": {"POY", "DTY", "PTA", "MEG"}},
    "production_sales_ratio": {"unit": "%", "frequency": "daily", "products": {"POY", "DTY"}},
    "spread_or_margin": {"unit": "元/吨", "frequency": "daily", "products": {"PTA", "MEG"}},
    "maintenance_supply_note": {"unit": "text", "frequency": "event", "products": {"PTA", "MEG"}, "text": True},
}

PRODUCT_SCOPED_ALIASES = {
    "inventory": "inventory",
    "库存": "inventory",
    "库存天数": "inventory",
    "profit": "profit",
    "margin": "profit",
    "cashflow": "profit",
    "利润": "profit",
    "现金流": "profit",
    "加工差": "profit",
    "operating_rate": "operating_rate",
    "product_operating_rate": "product_operating_rate",
    "run_rate": "operating_rate",
    "开工": "operating_rate",
    "开工率": "operating_rate",
    "负荷": "operating_rate",
    "production_sales_ratio": "production_sales_ratio",
    "sales_ratio": "production_sales_ratio",
    "产销": "production_sales_ratio",
    "产销率": "production_sales_ratio",
    "spread_or_margin": "spread_or_margin",
    "spread": "spread_or_margin",
    "价差": "spread_or_margin",
    "margin_spread": "spread_or_margin",
    "maintenance_supply_note": "maintenance_supply_note",
    "maintenance": "maintenance_supply_note",
    "supply_note": "maintenance_supply_note",
    "检修": "maintenance_supply_note",
    "装置检修": "maintenance_supply_note",
    "供应备注": "maintenance_supply_note",
}

UNIT_ALIASES = {
    "day": "天",
    "days": "天",
    "天": "天",
    "库存天数": "天",
    "%": "%",
    "％": "%",
    "百分比": "%",
    "元/吨": "元/吨",
    "元／吨": "元/吨",
    "yuan/ton": "元/吨",
    "rmb/ton": "元/吨",
    "RMB/ton": "元/吨",
    "CNY/mt": "元/吨",
    "cny/mt": "元/吨",
    "days_or_tonnes": "天",
    "text": "text",
}

FREQUENCY_ALIASES = {
    "daily": "daily",
    "day": "daily",
    "日": "daily",
    "日频": "daily",
    "每天": "daily",
    "weekly": "weekly",
    "week": "weekly",
    "周": "weekly",
    "周频": "weekly",
    "每周": "weekly",
    "daily_or_weekly": "weekly",
    "event": "event",
}


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    dry_run = not args.apply
    if args.apply and args.dry_run:
        print("Use either --apply or --dry-run, not both.")
        return 2

    db_path = args.db.expanduser().resolve()
    input_path = args.input.expanduser().resolve()
    summary_path = args.summary_output.expanduser().resolve()
    backup_path: Path | None = None

    if args.apply:
        if not args.backup_db:
            print("--apply requires --backup-db so the database is copied before import.")
            return 2
        if not db_path.exists():
            print(f"--apply requires an existing database to back up first: {db_path}")
            return 2
        backup_path = backup_database(db_path, args.backup_dir.expanduser().resolve())

    payloads, by_metric, errors = parse_input(input_path)
    before = load_counts(db_path)
    stored_rows = 0
    after = before

    if errors:
        summary = build_summary(
            input_path=input_path,
            db_path=db_path,
            dry_run=dry_run,
            accepted_rows=len(payloads),
            rejected_rows=len(errors),
            stored_rows=0,
            by_metric=by_metric,
            before=before,
            after=after,
            backup_path=backup_path,
            errors=errors,
        )
        write_summary(summary_path, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1

    if args.apply:
        with closing(sqlite3.connect(db_path, timeout=30.0)) as connection, connection:
            connection.execute("PRAGMA busy_timeout=30000")
            ensure_schema(connection)
            stored_rows = upsert_industry_observations(connection, payloads)
        after = load_counts(db_path)

    summary = build_summary(
        input_path=input_path,
        db_path=db_path,
        dry_run=dry_run,
        accepted_rows=len(payloads),
        rejected_rows=0,
        stored_rows=stored_rows,
        by_metric=by_metric,
        before=before,
        after=after,
        backup_path=backup_path,
        errors=[],
    )
    write_summary(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import user-authorized CCF polyester chain industry observations.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write rows to SQLite. Requires --backup-db.")
    mode.add_argument(
        "--dry-run", action="store_true", help="Parse and summarize without writing. This is the default."
    )
    parser.add_argument("--backup-db", action="store_true", help="Copy the DB to .codex-run/db-backups before --apply.")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_PATH)
    return parser.parse_args(argv)


def parse_input(path: Path) -> tuple[list[dict[str, Any]], Counter[str], list[str]]:
    payloads: list[dict[str, Any]] = []
    by_metric: Counter[str] = Counter()
    errors: list[str] = []
    if not path.exists():
        return payloads, by_metric, [f"input file not found: {path}"]
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for line_no, row in enumerate(reader, start=2):
                if is_blank_or_comment(row):
                    continue
                try:
                    payload = canonical_payload(row)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{path.name}:{line_no}: {exc}")
                    continue
                payloads.append(payload)
                by_metric[f"{payload['product']}|{payload['metric']}"] += 1
    except OSError as exc:
        errors.append(f"{path}: {exc}")
    return payloads, by_metric, errors


def is_blank_or_comment(row: dict[str, str]) -> bool:
    values = [str(value or "").strip() for value in row.values()]
    return not any(values) or values[0].startswith("#")


def canonical_payload(row: dict[str, str]) -> dict[str, Any]:
    cleaned = {key: (value or "").strip() for key, value in row.items()}
    observed_at = required_field(cleaned, "observed_at")
    raw_product = optional_field(cleaned, "product")
    raw_metric = required_field(cleaned, "metric")
    product = canonical_product(raw_product) if raw_product else ""
    metric, defaults, is_text_metric = canonical_metric_with_defaults(raw_metric, product)
    if not product:
        product = defaults["product"]
    if product != defaults["product"]:
        raise ValueError(f"metric {metric} must use product {defaults['product']}, got {product}")
    raw_value = optional_field(cleaned, "value")
    value = None if is_text_metric else optional_float(raw_value)
    unit = canonical_unit(optional_field(cleaned, "unit") or defaults["unit"])
    frequency = canonical_frequency(optional_field(cleaned, "frequency") or defaults["frequency"])
    raw = dict(cleaned)
    raw["canonicalized_at"] = datetime.now(UTC).isoformat()
    raw["canonical_product"] = product
    raw["canonical_metric"] = metric
    raw["canonical_unit"] = unit
    raw["canonical_frequency"] = frequency
    return {
        "source_id": optional_field(cleaned, "source_id") or "ccf_dom_daily",
        "observed_at": observed_at,
        "product": product,
        "metric": metric,
        "market": optional_field(cleaned, "market") or "中国",
        "region": optional_field(cleaned, "region") or "中国",
        "value": value,
        "unit": unit,
        "frequency": frequency,
        "evidence_level": optional_field(cleaned, "evidence_level") or "A",
        "evidence_url": optional_field(cleaned, "source_url") or "",
        "notes": notes_for_payload(cleaned, raw_value=raw_value, is_text_metric=is_text_metric),
        "raw": raw,
    }


def optional_field(row: dict[str, str], canonical_key: str) -> str:
    for key in FIELD_ALIASES[canonical_key]:
        value = row.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def required_field(row: dict[str, str], canonical_key: str) -> str:
    value = optional_field(row, canonical_key)
    if not value:
        raise ValueError(f"{canonical_key} is required")
    return value


def canonical_product(value: str) -> str:
    normalized = value.strip()
    if normalized.upper() in PRODUCT_ALIASES:
        return PRODUCT_ALIASES[normalized.upper()]
    if normalized in PRODUCT_ALIASES:
        return PRODUCT_ALIASES[normalized]
    raise ValueError(f"unsupported product: {value}")


def canonical_metric(value: str) -> str:
    normalized = value.strip()
    if normalized in METRIC_ALIASES:
        return METRIC_ALIASES[normalized]
    lower = normalized.lower().replace(" ", "_")
    if lower in METRIC_ALIASES:
        return METRIC_ALIASES[lower]
    raise ValueError(f"unsupported metric: {value}")


def canonical_metric_with_defaults(value: str, product: str) -> tuple[str, dict[str, str], bool]:
    try:
        metric = canonical_metric(value)
        return metric, METRIC_DEFAULTS[metric], False
    except ValueError:
        pass
    scoped_key = canonical_product_scoped_metric(value)
    if not scoped_key:
        raise ValueError(f"unsupported metric: {value}")
    if not product:
        raise ValueError(f"product is required for scoped metric: {value}")
    scoped = PRODUCT_SCOPED_METRICS[scoped_key]
    if product not in scoped["products"]:
        raise ValueError(f"metric {scoped_key} does not support product {product}")
    canonical = f"{product.lower()}_{scoped_key}"
    if scoped_key == "product_operating_rate":
        canonical = f"{product.lower()}_operating_rate"
    defaults = {"product": product, "unit": scoped["unit"], "frequency": scoped["frequency"]}
    return canonical, defaults, bool(scoped.get("text"))


def canonical_product_scoped_metric(value: str) -> str:
    normalized = value.strip()
    if normalized in PRODUCT_SCOPED_ALIASES:
        return PRODUCT_SCOPED_ALIASES[normalized]
    lower = normalized.lower().replace(" ", "_")
    if lower in PRODUCT_SCOPED_ALIASES:
        return PRODUCT_SCOPED_ALIASES[lower]
    return ""


def canonical_unit(value: str) -> str:
    normalized = value.strip()
    if normalized in UNIT_ALIASES:
        return UNIT_ALIASES[normalized]
    lower = normalized.lower()
    if lower in UNIT_ALIASES:
        return UNIT_ALIASES[lower]
    raise ValueError(f"unsupported unit: {value}")


def canonical_frequency(value: str) -> str:
    normalized = value.strip()
    if normalized in FREQUENCY_ALIASES:
        return FREQUENCY_ALIASES[normalized]
    lower = normalized.lower()
    if lower in FREQUENCY_ALIASES:
        return FREQUENCY_ALIASES[lower]
    raise ValueError(f"unsupported frequency: {value}")


def optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


def notes_for_payload(cleaned: dict[str, str], *, raw_value: str, is_text_metric: bool) -> str:
    notes = optional_field(cleaned, "notes")
    if is_text_metric and raw_value:
        return f"{notes}; text_value={raw_value}" if notes else f"text_value={raw_value}"
    return notes or "authorized CCF industry observation import"


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(INDUSTRY_OBSERVATIONS_SCHEMA)


def upsert_industry_observations(connection: sqlite3.Connection, payloads: list[dict[str, Any]]) -> int:
    created_at = datetime.now(UTC).isoformat()
    stored = 0
    for payload in payloads:
        existing = connection.execute(
            """
            SELECT observation_id FROM industry_observations
            WHERE source_id = ? AND observed_at = ? AND product = ? AND metric = ?
              AND market = ? AND region = ? AND unit = ? AND frequency = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (
                payload["source_id"],
                payload["observed_at"],
                payload["product"],
                payload["metric"],
                payload["market"],
                payload["region"],
                payload["unit"],
                payload["frequency"],
            ),
        ).fetchone()
        values = (
            payload["source_id"],
            payload["observed_at"],
            payload["product"],
            payload["metric"],
            payload["market"],
            payload["region"],
            payload["value"],
            payload["unit"],
            payload["frequency"],
            payload["evidence_level"],
            payload["evidence_url"],
            payload["notes"],
            json.dumps(payload["raw"], ensure_ascii=False),
        )
        if existing is None:
            connection.execute(
                """
                INSERT INTO industry_observations (
                  observation_id, created_at, source_id, observed_at, product, metric, market, region,
                  value, unit, frequency, evidence_level, evidence_url, notes, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), created_at, *values),
            )
        else:
            connection.execute(
                """
                UPDATE industry_observations
                SET source_id = ?, observed_at = ?, product = ?, metric = ?, market = ?, region = ?,
                    value = ?, unit = ?, frequency = ?, evidence_level = ?, evidence_url = ?, notes = ?, raw = ?
                WHERE observation_id = ?
                """,
                (*values, existing[0]),
            )
        stored += 1
    return stored


def load_counts(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"db_exists": False, "total_ccf_industry": 0, "target_metrics": []}
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection, connection:
            connection.row_factory = sqlite3.Row
            if not table_exists(connection, "industry_observations"):
                return {"db_exists": True, "total_ccf_industry": 0, "target_metrics": []}
            total = connection.execute(
                "SELECT COUNT(*) AS n FROM industry_observations WHERE source_id LIKE 'ccf%'"
            ).fetchone()["n"]
            rows = connection.execute("""
                SELECT source_id, product, metric, unit, frequency, COUNT(*) AS n,
                       MIN(observed_at) AS first_observed_at,
                       MAX(observed_at) AS last_observed_at
                FROM industry_observations
                WHERE source_id LIKE 'ccf%'
                GROUP BY source_id, product, metric, unit, frequency
                ORDER BY product, metric
                """).fetchall()
            return {"db_exists": True, "total_ccf_industry": int(total), "target_metrics": [dict(row) for row in rows]}
    except sqlite3.Error:
        return {"db_exists": db_path.exists(), "total_ccf_industry": 0, "target_metrics": []}


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        is not None
    )


def backup_database(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{db_path.name}.pre_ccf_industry_import_{timestamp}.sqlite"
    shutil.copy2(db_path, backup_path)
    return backup_path


def build_summary(
    *,
    input_path: Path,
    db_path: Path,
    dry_run: bool,
    accepted_rows: int,
    rejected_rows: int,
    stored_rows: int,
    by_metric: Counter[str],
    before: dict[str, Any],
    after: dict[str, Any],
    backup_path: Path | None,
    errors: list[str],
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "writes_database": not dry_run,
        "db_path": str(db_path),
        "input": str(input_path),
        "accepted_rows": accepted_rows,
        "rejected_rows": rejected_rows,
        "stored_rows": stored_rows,
        "would_store_rows": accepted_rows if dry_run else 0,
        "metric_rows": dict(sorted(by_metric.items())),
        "before": before,
        "after": after,
        "delta_total_ccf_industry": (
            int(after.get("total_ccf_industry", 0) or 0) - int(before.get("total_ccf_industry", 0) or 0)
        ),
        "backup_path": str(backup_path) if backup_path else "",
        "errors": errors[:50],
        "guards": {
            "backup_required_before_apply": True,
            "backup_created": bool(backup_path),
            "source_id_prefix": "ccf",
            "credentials_logged": False,
            "calls_external_llm_provider": False,
            "main_db_modified": not dry_run,
            "authorization_policy": "use only user-authorized CCF export/page table or uploaded files",
        },
    }


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

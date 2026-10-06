from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app import storage  # noqa: E402

DEFAULT_INPUT_DIR = REPO_ROOT / ".codex-run" / "ccf-authorized-capture"
DEFAULT_DB_PATH = REPO_ROOT / "server" / "data" / "agent.db"
DEFAULT_BACKUP_DIR = REPO_ROOT / ".codex-run" / "db-backups"
DEFAULT_SUMMARY_PATH = REPO_ROOT / ".codex-run" / "ccf-authorized-import-summary-latest.json"
DEFAULT_PATTERN = "ccf_dom_daily_*.csv"
SOURCE_EVIDENCE_CONTRACT_VERSION = "source-evidence-contract.v1"
PARSER_VERSION = "ccf-authorized-import.v2"

FORECAST_PRICE_POINTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_price_points (
  point_id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  dataset_type TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  company TEXT NOT NULL,
  product TEXT NOT NULL,
  series TEXT NOT NULL,
  spec TEXT NOT NULL,
  batch_no TEXT NOT NULL,
  poy_spec TEXT NOT NULL,
  market TEXT NOT NULL,
  grade TEXT NOT NULL,
  feature TEXT NOT NULL,
  price REAL NOT NULL,
  price_low REAL,
  price_high REAL,
  unit TEXT NOT NULL,
  quote_type TEXT NOT NULL,
  notes TEXT NOT NULL,
  raw TEXT NOT NULL,
  capture_revision_id TEXT REFERENCES source_capture_revisions(capture_revision_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_forecast_price_point_unique
ON forecast_price_points (
  source_id, dataset_type, observed_at, company, product, series, spec,
  batch_no, poy_spec, market, grade, feature, unit, quote_type
);

CREATE INDEX IF NOT EXISTS idx_forecast_price_point_lookup
ON forecast_price_points (dataset_type, product, spec, observed_at DESC);

CREATE INDEX IF NOT EXISTS ix_forecast_price_capture_revision
ON forecast_price_points(capture_revision_id);
"""

SPEC_MAPPING: dict[str, tuple[str, str]] = {
    "直纺半光POY 150D/48F": ("150D系列", "POY 150D/48F"),
    "直纺半光POY 150D/144F": ("150D系列", "POY 150D/144F"),
    "直纺半光POY 75D/36F（十公斤125分特）": ("75D系列", "POY 75D/36F"),
    "直纺半光POY 75D/72F（135分特）": ("75D系列", "POY 75D/72F"),
    "涤纶DTY 150D/48F低弹": ("150D系列", "DTY 150D/48F低弹"),
    "涤纶DTY 150D/144F轻网": ("150D系列", "DTY 150D/144F轻网"),
    "涤纶DTY 75D/72F轻网": ("75D系列", "DTY 75D/72F轻网"),
    "DTY75/36": ("75D系列", "DTY 75D/36F"),
    "切片纺POY 75D/36F": ("切片纺", "切片纺POY 75D/36F"),
    "切片纺POY 黑丝 150D/48F": ("切片纺", "切片纺POY 黑丝 150D/48F"),
    "切片纺POY 黑丝 300D/96F": ("切片纺", "切片纺POY 黑丝 300D/96F"),
    "CFR日本石脑油": ("石脑油", "日本石脑油"),
    "日本石脑油": ("石脑油", "日本石脑油"),
    "CFR中国PX": ("PX", "PX CFR中国"),
    "PX CFR中国": ("PX", "PX CFR中国"),
    "内盘PTA": ("PTA", "内盘PTA"),
    "内盘MEG现货": ("MEG", "内盘MEG现货"),
}

PRODUCT_MAPPING = {
    "NAPHTHA": "NAPHTHA",
    "Naphtha": "NAPHTHA",
    "PX": "PX",
    "PTA": "PTA",
    "MEG": "MEG",
    "POY": "POY",
    "DTY": "DTY",
}

SEMANTIC_SERIES_IDS = {
    ("NAPHTHA", "日本石脑油"): "naphtha.ccf.japan_cfr.daily_assessment",
    ("PX", "PX CFR中国"): "px.ccf.china_cfr.daily_assessment",
    ("PTA", "内盘PTA"): "pta.ccf.domestic.daily_assessment",
    ("MEG", "内盘MEG现货"): "meg.ccf.domestic.daily_assessment",
}


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    dry_run = not args.apply
    if args.apply and args.dry_run:
        print("Use either --apply or --dry-run, not both.")
        return 2
    db_path = args.db.expanduser().resolve()
    input_dir = args.input_dir.expanduser().resolve()
    summary_path = args.summary_output.expanduser().resolve()
    backup_path: Path | None = None

    csv_paths = sorted(input_dir.glob(args.pattern))
    payloads, source_files, canonical_changes, by_series, errors = parse_csv_files(csv_paths)
    before = load_counts(db_path)
    stored_rows = 0
    after_normalize = before
    after = before
    if errors:
        summary = build_summary(
            args=args,
            dry_run=dry_run,
            db_path=db_path,
            input_dir=input_dir,
            csv_paths=csv_paths,
            accepted_rows=len(payloads),
            rejected_rows=len(errors),
            stored_rows=0,
            backup_path=backup_path,
            source_files=source_files,
            canonical_changes=canonical_changes,
            by_series=by_series,
            before=before,
            after_normalize=after_normalize,
            after=after,
            errors=errors,
        )
        write_summary(summary_path, summary)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1

    if args.apply:
        if not args.backup_db:
            print("--apply requires --backup-db so the database is copied before import.")
            return 2
        if not db_path.exists():
            print(f"--apply requires an existing database to back up first: {db_path}")
            return 2
        try:
            with closing(sqlite3.connect(db_path)) as connection, connection:
                storage.assert_source_capture_revision_schema(connection)
                storage.assert_forecast_capture_lineage_schema(connection)
        except sqlite3.Error as exc:
            errors = [str(exc)]
            summary = build_summary(
                args=args,
                dry_run=dry_run,
                db_path=db_path,
                input_dir=input_dir,
                csv_paths=csv_paths,
                accepted_rows=len(payloads),
                rejected_rows=1,
                stored_rows=0,
                backup_path=backup_path,
                source_files=source_files,
                canonical_changes=canonical_changes,
                by_series=by_series,
                before=before,
                after_normalize=after_normalize,
                after=after,
                errors=errors,
            )
            write_summary(summary_path, summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 1
        backup_path = backup_database(db_path, args.backup_dir.expanduser().resolve())

    if args.apply:
        with closing(sqlite3.connect(db_path)) as connection, connection:
            ensure_forecast_schema(connection)
            storage.assert_source_capture_revision_schema(connection)
            storage.assert_forecast_capture_lineage_schema(connection)
            normalize_existing_chip_spun_series(connection)
            after_normalize = load_counts_from_connection(connection)
            stored_rows = upsert_forecast_price_points(connection, payloads)
        after = load_counts(db_path)

    summary = build_summary(
        args=args,
        dry_run=dry_run,
        db_path=db_path,
        input_dir=input_dir,
        csv_paths=csv_paths,
        accepted_rows=len(payloads),
        rejected_rows=0,
        stored_rows=stored_rows,
        backup_path=backup_path,
        source_files=source_files,
        canonical_changes=canonical_changes,
        by_series=by_series,
        before=before,
        after_normalize=after_normalize,
        after=after,
        errors=[],
    )
    write_summary(summary_path, summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import user-authorized CCF CSV exports into forecast_price_points.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Write rows to SQLite. Requires --backup-db.")
    mode.add_argument(
        "--dry-run", action="store_true", help="Parse and summarize without writing. This is the default."
    )
    parser.add_argument("--backup-db", action="store_true", help="Copy the DB to .codex-run/db-backups before --apply.")
    parser.add_argument("--backup-dir", type=Path, default=DEFAULT_BACKUP_DIR)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--pattern", default=DEFAULT_PATTERN)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY_PATH)
    return parser.parse_args(argv)


def parse_csv_files(
    csv_paths: list[Path],
) -> tuple[list[dict[str, Any]], Counter[str], Counter[str], Counter[tuple[str, str, str]], list[str]]:
    payloads: list[dict[str, Any]] = []
    source_files: Counter[str] = Counter()
    canonical_changes: Counter[str] = Counter()
    by_series: Counter[tuple[str, str, str]] = Counter()
    errors: list[str] = []
    if not csv_paths:
        return payloads, source_files, canonical_changes, by_series, ["No CCF CSV files matched the input pattern."]
    for path in csv_paths:
        try:
            source_file_sha256 = file_sha256(path)
            with path.open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                for line_no, row in enumerate(reader, start=2):
                    try:
                        payload, changed = canonical_payload(
                            row, source_file=path.name, source_file_sha256=source_file_sha256
                        )
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"{path.name}:{line_no}: {exc}")
                        continue
                    payloads.append(payload)
                    source_files[path.name] += 1
                    by_series[(payload["product"], payload["series"], payload["spec"])] += 1
                    for field in changed:
                        canonical_changes[field] += 1
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return payloads, source_files, canonical_changes, by_series, errors


def canonical_payload(
    row: dict[str, str], *, source_file: str, source_file_sha256: str
) -> tuple[dict[str, Any], list[str]]:
    cleaned = {key: (value or "").strip() for key, value in row.items()}
    source_id = cleaned.get("source_id") or "ccf_dom_daily"
    dataset_type = cleaned.get("dataset_type") or "ccf_spot"
    company = cleaned.get("company") or "CCF"
    raw_product = cleaned.get("product") or cleaned.get("产品") or ""
    product = PRODUCT_MAPPING.get(raw_product.upper(), PRODUCT_MAPPING.get(raw_product, raw_product))
    if not product:
        raise ValueError("product is required")

    original_series = cleaned.get("series") or cleaned.get("产品") or ""
    original_spec = cleaned.get("spec") or cleaned.get("规格") or ""
    mapping_key = original_series or original_spec
    series, spec = SPEC_MAPPING.get(mapping_key, (original_series, original_spec))
    if original_spec in SPEC_MAPPING:
        series, spec = SPEC_MAPPING[original_spec]
    if not spec:
        raise ValueError("spec is required")
    observed_at = cleaned.get("observed_at") or cleaned.get("date") or cleaned.get("日期") or ""
    if not observed_at:
        raise ValueError("observed_at is required")
    price = float(cleaned.get("price") or cleaned.get("均价") or "")
    unit = cleaned.get("unit") or default_unit(product)
    quote_type = cleaned.get("quote_type") or "daily_average"
    captured_at = required_rfc3339(cleaned.get("captured_at"), field="captured_at")
    visible_at = required_rfc3339(cleaned.get("visible_at"), field="visible_at")
    source_url = cleaned.get("source_url") or ""
    if not source_url.startswith(("https://", "http://")):
        raise ValueError("source_url must be http(s)")
    authorization_scope = cleaned.get("authorization_scope") or cleaned.get("license_scope") or ""
    if not authorization_scope:
        raise ValueError("authorization_scope is required")
    published_at = cleaned.get("published_at") or ""
    if published_at:
        published_at = required_rfc3339(published_at, field="published_at")
    semantic_series_id = SEMANTIC_SERIES_IDS.get((product, spec), operational_semantic_series_id(product, spec))
    raw = {
        "source_file": source_file,
        "source_file_sha256": source_file_sha256,
        "source_url": source_url,
        "published_at": published_at,
        "visible_at": visible_at,
        "captured_at": captured_at,
        "authorization_scope": authorization_scope,
        "contract_version": SOURCE_EVIDENCE_CONTRACT_VERSION,
        "parser_version": PARSER_VERSION,
        "semantic_series_id": semantic_series_id,
        "acquisition_method": cleaned.get("acquisition_method") or "authorized_export_or_page_table",
        "original_product": raw_product,
        "original_series": original_series,
        "original_spec": original_spec,
        "canonicalized_at": datetime.now(UTC).isoformat(),
    }
    payload = {
        "source_id": source_id,
        "dataset_type": dataset_type,
        "observed_at": observed_at,
        "company": company,
        "product": product,
        "series": series,
        "spec": spec,
        "batch_no": cleaned.get("batch_no") or "",
        "poy_spec": cleaned.get("poy_spec") or "",
        "market": cleaned.get("market") or "",
        "grade": cleaned.get("grade") or "",
        "feature": cleaned.get("feature") or "",
        "price": price,
        "price_low": optional_float(cleaned.get("price_low")),
        "price_high": optional_float(cleaned.get("price_high")),
        "unit": unit,
        "quote_type": quote_type,
        "notes": "authorized CCF data center DOM table capture; imported from canonical CSV",
        "raw": raw,
    }
    changed = []
    if series != original_series:
        changed.append("series")
    if spec != original_spec:
        changed.append("spec")
    return payload, changed


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def required_rfc3339(value: str | None, *, field: str) -> str:
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError(f"{field} is required")
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be RFC3339") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an offset")
    return candidate


def operational_semantic_series_id(product: str, spec: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", f"{product}-{spec}".lower()).strip("-")
    return f"ccf.operational.{normalized}.daily_assessment"


def optional_float(value: str | None) -> float | None:
    if value is None or not value.strip():
        return None
    return float(value)


def default_unit(product: str) -> str:
    if product in {"PX", "NAPHTHA"}:
        return "USD/mt"
    return "CNY/mt"


def backup_database(db_path: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{db_path.name}.pre_ccf_authorized_import_{timestamp}.sqlite"
    shutil.copy2(db_path, backup_path)
    return backup_path


def ensure_forecast_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(FORECAST_PRICE_POINTS_SCHEMA)


def normalize_existing_chip_spun_series(connection: sqlite3.Connection) -> None:
    specs = (
        "切片纺POY 75D/36F",
        "切片纺POY 黑丝 150D/48F",
        "切片纺POY 黑丝 300D/96F",
    )
    connection.execute(
        """
        UPDATE forecast_price_points
        SET series = '切片纺'
        WHERE source_id = 'ccf_dom_daily'
          AND dataset_type = 'ccf_spot'
          AND product = 'POY'
          AND spec IN (?, ?, ?)
          AND series <> '切片纺'
        """,
        specs,
    )


def upsert_forecast_price_points(connection: sqlite3.Connection, payloads: list[dict[str, Any]]) -> int:
    created_at = datetime.now(UTC).isoformat()
    stored = 0
    for payload in payloads:
        raw = payload["raw"]
        capture_revision_id, appended = storage.append_source_capture_revision_with_connection(
            connection,
            capture_revision_id=str(uuid4()),
            source_id=payload["source_id"],
            semantic_series_id=str(raw["semantic_series_id"]),
            observed_at=payload["observed_at"],
            published_at=str(raw["published_at"]),
            visible_at=str(raw["visible_at"]),
            captured_at=str(raw["captured_at"]),
            source_url=str(raw["source_url"]),
            raw_sha256=str(raw["source_file_sha256"]),
            authorization_scope=str(raw["authorization_scope"]),
            contract_version=str(raw["contract_version"]),
            parser_version=str(raw["parser_version"]),
            canonical_payload=payload,
        )
        raw["capture_revision_id"] = capture_revision_id
        raw["capture_revision_appended"] = appended
        values = forecast_values(payload)
        existing = connection.execute(
            """
            SELECT point_id FROM forecast_price_points
            WHERE source_id = ? AND dataset_type = ? AND observed_at = ? AND company = ?
              AND product = ? AND series = ? AND spec = ? AND batch_no = ? AND poy_spec = ?
              AND market = ? AND grade = ? AND feature = ? AND unit = ? AND quote_type = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (
                payload["source_id"],
                payload["dataset_type"],
                payload["observed_at"],
                payload["company"],
                payload["product"],
                payload["series"],
                payload["spec"],
                payload["batch_no"],
                payload["poy_spec"],
                payload["market"],
                payload["grade"],
                payload["feature"],
                payload["unit"],
                payload["quote_type"],
            ),
        ).fetchone()
        if existing is not None:
            connection.execute(
                """
                UPDATE forecast_price_points
                SET source_id = ?, dataset_type = ?, observed_at = ?, company = ?, product = ?, series = ?,
                    spec = ?, batch_no = ?, poy_spec = ?, market = ?, grade = ?, feature = ?, price = ?,
                    price_low = ?, price_high = ?, unit = ?, quote_type = ?, notes = ?, raw = ?,
                    capture_revision_id = ?
                WHERE point_id = ?
                """,
                (*values, capture_revision_id, existing[0]),
            )
        else:
            connection.execute(
                """
                INSERT INTO forecast_price_points (
                  point_id, created_at, source_id, dataset_type, observed_at, company, product, series, spec,
                  batch_no, poy_spec, market, grade, feature, price, price_low, price_high, unit, quote_type,
                  notes, raw, capture_revision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), created_at, *values, capture_revision_id),
            )
        stored += 1
    return stored


def forecast_values(payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        payload["source_id"],
        payload["dataset_type"],
        payload["observed_at"],
        payload["company"],
        payload["product"],
        payload["series"],
        payload["spec"],
        payload["batch_no"],
        payload["poy_spec"],
        payload["market"],
        payload["grade"],
        payload["feature"],
        payload["price"],
        payload["price_low"],
        payload["price_high"],
        payload["unit"],
        payload["quote_type"],
        payload["notes"],
        json.dumps(payload["raw"], ensure_ascii=False),
    )


def load_counts(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"db_exists": False, "total_ccf_dom_daily": 0, "target_series": []}
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection, connection:
            connection.row_factory = sqlite3.Row
            if not table_exists(connection, "forecast_price_points"):
                return {"db_exists": True, "total_ccf_dom_daily": 0, "target_series": []}
            return load_counts_from_connection(connection)
    except sqlite3.Error:
        return {"db_exists": db_path.exists(), "total_ccf_dom_daily": 0, "target_series": []}


def load_counts_from_connection(connection: sqlite3.Connection) -> dict[str, Any]:
    connection.row_factory = sqlite3.Row
    total = connection.execute("""
        SELECT COUNT(*) AS n
        FROM forecast_price_points
        WHERE source_id = 'ccf_dom_daily'
          AND dataset_type = 'ccf_spot'
        """).fetchone()["n"]
    rows = connection.execute("""
        SELECT product, series, spec, unit, quote_type, COUNT(*) AS n,
               MIN(observed_at) AS first_observed_at,
               MAX(observed_at) AS last_observed_at
        FROM forecast_price_points
        WHERE source_id = 'ccf_dom_daily'
          AND dataset_type = 'ccf_spot'
          AND product IN ('POY', 'DTY', 'PX', 'PTA', 'MEG', 'NAPHTHA')
        GROUP BY product, series, spec, unit, quote_type
        ORDER BY product, series, spec
        """).fetchall()
    return {"db_exists": True, "total_ccf_dom_daily": int(total), "target_series": [dict(row) for row in rows]}


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def build_summary(
    *,
    args: argparse.Namespace,
    dry_run: bool,
    db_path: Path,
    input_dir: Path,
    csv_paths: list[Path],
    accepted_rows: int,
    rejected_rows: int,
    stored_rows: int,
    backup_path: Path | None,
    source_files: Counter[str],
    canonical_changes: Counter[str],
    by_series: Counter[tuple[str, str, str]],
    before: dict[str, Any],
    after_normalize: dict[str, Any],
    after: dict[str, Any],
    errors: list[str],
) -> dict[str, Any]:
    del args
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "dry_run": dry_run,
        "writes_database": not dry_run,
        "db_path": str(db_path),
        "input_dir": str(input_dir),
        "csv_files": [str(path) for path in csv_paths],
        "accepted_rows": accepted_rows,
        "rejected_rows": rejected_rows,
        "stored_rows": stored_rows,
        "would_store_rows": 0 if not dry_run else accepted_rows,
        "source_files": dict(source_files),
        "canonical_changes": dict(canonical_changes),
        "series_rows": {"|".join(key): value for key, value in sorted(by_series.items())},
        "before": before,
        "after_normalize_existing_chip_spun_series": after_normalize,
        "after": after,
        "delta_total_ccf_dom_daily": (
            int(after.get("total_ccf_dom_daily", 0) or 0) - int(before.get("total_ccf_dom_daily", 0) or 0)
        ),
        "backup_path": str(backup_path) if backup_path else "",
        "errors": errors[:50],
        "guards": {
            "backup_required_before_apply": True,
            "backup_created": bool(backup_path),
            "source_id": "ccf_dom_daily",
            "dataset_type": "ccf_spot",
            "credentials_logged": False,
            "calls_external_llm_provider": False,
            "rebuilds_rag_index": False,
            "main_db_modified": not dry_run,
            "authorization_policy": "use only user-authorized CCF export/page table; do not bypass access controls",
        },
    }


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

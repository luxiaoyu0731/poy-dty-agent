from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree

from .models import (
    EventObservationCreate,
    ForecastPricePointCreate,
    IndustryObservationCreate,
    MarketObservationCreate,
)
from .storage import (
    _create_industry_observation_with_connection,
    _upsert_event_observation_with_connection,
    _upsert_forecast_price_point_with_connection,
    _upsert_market_observation_with_connection,
    connect,
)

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_UNCOMPRESSED_XLSX_BYTES = 50 * 1024 * 1024
MAX_ROWS = 10_000
DATA_SUFFIXES = {".csv", ".xlsx"}
REVIEW_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".heic"}
XML_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


@dataclass(slots=True)
class ImportFileResult:
    file: str
    status: str
    kind: str
    rows: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list[str] | None = None
    destination: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "status": self.status,
            "kind": self.kind,
            "rows": self.rows,
            "inserted": self.inserted,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": self.errors or [],
            "destination": self.destination,
        }


def process_user_files(*, inbox: Path, report_dir: Path, apply: bool) -> dict[str, object]:
    inbox.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    candidates = sorted(
        path for path in inbox.iterdir() if path.is_file() and not path.is_symlink() and not path.name.startswith(".")
    )
    results: list[ImportFileResult] = []
    for path in candidates:
        result = process_user_file(path, apply=apply)
        if apply:
            destination_group = (
                "processed"
                if result.status == "imported"
                else "review"
                if result.status == "review_required"
                else "rejected"
            )
            destination = _move_file(path, inbox / destination_group)
            result.destination = str(destination)
        _write_report(report_dir, path.name, result)
        results.append(result)
    errors = [error for result in results for error in (result.errors or [])]
    return {
        "status": "idle" if not results else "degraded" if errors else "completed",
        "writes_database": apply and any(result.status == "imported" for result in results),
        "inbox": str(inbox),
        "pending_files": len(candidates),
        "imported_files": sum(result.status == "imported" for result in results),
        "review_files": sum(result.status == "review_required" for result in results),
        "rejected_files": sum(result.status == "rejected" for result in results),
        "rows": sum(result.rows for result in results),
        "inserted": sum(result.inserted for result in results),
        "updated": sum(result.updated for result in results),
        "unchanged": sum(result.unchanged for result in results),
        "errors": errors[:20],
        "files": [result.as_dict() for result in results],
    }


def process_user_file(path: Path, *, apply: bool) -> ImportFileResult:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError(f"file exceeds {MAX_FILE_BYTES} byte limit")
        suffix = path.suffix.lower()
        if suffix in REVIEW_SUFFIXES:
            return ImportFileResult(
                file=path.name,
                status="review_required",
                kind="document_or_image",
                errors=[],
            )
        if suffix not in DATA_SUFFIXES:
            raise ValueError(f"unsupported file type: {suffix or 'none'}")
        rows = read_tabular_rows(path)
        kind = detect_template_kind(rows)
        if kind == "public_observations":
            return _import_public(path, rows, apply=apply)
        if kind == "industry_observations":
            return _import_industry(path, rows, apply=apply)
        if kind == "events":
            return _import_events(path, rows, apply=apply)
        if kind == "forecast_price_points":
            return _import_forecast(path, rows, apply=apply)
        raise ValueError("unsupported standard-template headers")
    except Exception as exc:  # noqa: BLE001 - one bad inbox file must not stop the daily run.
        return ImportFileResult(
            file=path.name,
            status="rejected",
            kind="unknown",
            errors=[f"{exc.__class__.__name__}: {str(exc)[:500]}"],
        )


def read_tabular_rows(path: Path) -> list[dict[str, str]]:
    if path.suffix.lower() == ".csv":
        text = path.read_text(encoding="utf-8-sig")
        return _dict_rows(csv.reader(io.StringIO(text)))
    return _read_xlsx_rows(path)


def detect_template_kind(rows: list[dict[str, str]]) -> str:
    if not rows:
        raise ValueError("file contains no data rows")
    fields = set(rows[0])
    if {"event_time", "source_id", "title"}.issubset(fields):
        return "events"
    if {"source_id", "dataset_type", "observed_at", "product", "spec", "price"}.issubset(fields):
        return "forecast_price_points"
    if {"observed_at", "source_id", "value"}.issubset(fields) and (
        "series_id" in fields or "dataset" in fields or "indicator" in fields
    ):
        return "public_observations"
    if {"observed_at", "product", "value"}.issubset(fields) and (
        "raw_field_name" in fields or "quote_type" in fields or "metric" in fields
    ):
        return "industry_observations"
    raise ValueError("headers do not match a supported standard template")


def _import_public(path: Path, rows: list[dict[str, str]], *, apply: bool) -> ImportFileResult:
    payloads: list[dict[str, Any]] = []
    source_file_sha256 = sha256(path.read_bytes()).hexdigest()
    for row in rows:
        indicator = row.get("indicator") or row.get("series_id") or row.get("dataset") or row.get("raw_field_name")
        payload = MarketObservationCreate(
            source_id=_required(row, "source_id"),
            observed_at=_required_timestamp(row, "observed_at"),
            indicator=indicator or "public_observation",
            product=row.get("product") or row.get("instrument") or "unknown",
            value=_float_or_none(row.get("value")),
            unit=row.get("unit") or "",
            frequency=row.get("frequency") or "",
            region=row.get("region") or "global",
            evidence_url=row.get("source_url") or "",
            notes=row.get("notes") or "",
            raw=_source_raw(row, path, source_file_sha256),
        ).model_dump()
        payloads.append(payload)
    inserted = updated = unchanged = 0
    if apply:
        created_at = datetime.now(UTC).isoformat()
        with closing(connect()) as connection, connection:
            for payload in payloads:
                existing = connection.execute(
                    """
                    SELECT value, unit, frequency, region, evidence_url, notes, raw
                    FROM market_observations
                    WHERE source_id=? AND observed_at=? AND indicator=? AND product=?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (
                        payload["source_id"],
                        payload["observed_at"],
                        payload["indicator"],
                        payload["product"],
                    ),
                ).fetchone()
                if existing is None:
                    inserted += 1
                else:
                    comparable = dict(existing)
                    comparable["raw"] = json.loads(comparable["raw"])
                    changed = any(
                        comparable.get(field) != payload.get(field)
                        for field in ("value", "unit", "frequency", "region", "evidence_url", "notes")
                    ) or _semantic_raw(comparable["raw"]) != _semantic_raw(payload["raw"])
                    updated += int(changed)
                    unchanged += int(not changed)
                    if not changed:
                        payload["raw"] = comparable["raw"]
                _upsert_market_observation_with_connection(
                    connection,
                    observation_id=str(uuid4()),
                    payload=payload,
                    created_at=created_at,
                )
    else:
        inserted = len(payloads)
    return ImportFileResult(
        file=path.name,
        status="imported",
        kind="public_observations",
        rows=len(rows),
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
    )


def _import_industry(path: Path, rows: list[dict[str, str]], *, apply: bool) -> ImportFileResult:
    payloads: list[dict[str, Any]] = []
    source_file_sha256 = sha256(path.read_bytes()).hexdigest()
    for row in rows:
        metric = row.get("metric") or row.get("raw_field_name") or row.get("quote_type") or "industry_metric"
        payloads.append(
            IndustryObservationCreate(
                source_id=row.get("source_id") or "user_files",
                observed_at=_required_timestamp(row, "observed_at"),
                product=_required(row, "product"),
                metric=metric,
                market=row.get("market") or "全国",
                region=row.get("region") or "全国",
                value=_float_or_none(row.get("value") or row.get("spread_value")),
                unit=row.get("unit") or "",
                frequency=row.get("frequency") or "manual",
                evidence_level=row.get("tier") or "D",
                evidence_url=row.get("source_url") or "",
                notes=row.get("notes") or "",
                raw=_source_raw(row, path, source_file_sha256),
            ).model_dump()
        )
    inserted = unchanged = 0
    if apply:
        created_at = datetime.now(UTC).isoformat()
        with closing(connect()) as connection, connection:
            for payload in payloads:
                observation_id = _stable_id("user-industry", payload)
                existing = connection.execute(
                    "SELECT 1 FROM industry_observations WHERE observation_id = ?",
                    (observation_id,),
                ).fetchone()
                if existing is not None:
                    unchanged += 1
                    continue
                _create_industry_observation_with_connection(
                    connection,
                    observation_id=observation_id,
                    payload=payload,
                    created_at=created_at,
                )
                inserted += 1
    else:
        inserted = len(payloads)
    return ImportFileResult(
        file=path.name,
        status="imported",
        kind="industry_observations",
        rows=len(rows),
        inserted=inserted,
        unchanged=unchanged,
    )


def _import_events(path: Path, rows: list[dict[str, str]], *, apply: bool) -> ImportFileResult:
    payloads: list[dict[str, Any]] = []
    source_file_sha256 = sha256(path.read_bytes()).hexdigest()
    for row in rows:
        payloads.append(
            EventObservationCreate(
                source_id=_required(row, "source_id"),
                occurred_at=_required_timestamp(row, "event_time"),
                title=_required(row, "title"),
                event_type=row.get("event_type") or "general",
                evidence_level=row.get("evidence_level") or row.get("tier") or "C",
                summary=row.get("summary") or "",
                affected_products=_split_list(row.get("affected_products") or ""),
                direction=row.get("direction") or "中性",
                impact_strength=row.get("impact_strength") or "",
                evidence_url=row.get("source_url") or "",
                requires_human_review=_truthy(row.get("requires_human_review"), default=True),
                notes=row.get("notes") or "",
                raw=_source_raw(row, path, source_file_sha256),
            ).model_dump()
        )
    inserted = unchanged = 0
    if apply:
        created_at = datetime.now(UTC).isoformat()
        with closing(connect()) as connection, connection:
            for payload in payloads:
                _, created = _upsert_event_observation_with_connection(
                    connection,
                    event_record_id=_stable_id("user-event", payload),
                    payload=payload,
                    created_at=created_at,
                )
                inserted += int(created)
                unchanged += int(not created)
    else:
        inserted = len(payloads)
    return ImportFileResult(
        file=path.name,
        status="imported",
        kind="events",
        rows=len(rows),
        inserted=inserted,
        unchanged=unchanged,
    )


def _import_forecast(path: Path, rows: list[dict[str, str]], *, apply: bool) -> ImportFileResult:
    payloads: list[dict[str, Any]] = []
    source_file_sha256 = sha256(path.read_bytes()).hexdigest()
    for row in rows:
        payloads.append(
            ForecastPricePointCreate(
                source_id=row.get("source_id") or "user_files",
                dataset_type=row.get("dataset_type") or "upstream_spot",
                observed_at=_required_timestamp(row, "observed_at"),
                company=row.get("company") or "source_reporter",
                product=row.get("product") or "PTA",
                series=row.get("series") or "",
                spec=_required(row, "spec"),
                batch_no=row.get("batch_no") or "",
                poy_spec=row.get("poy_spec") or "",
                market=row.get("market") or "",
                grade=row.get("grade") or "",
                feature=row.get("feature") or "",
                price=float(_required(row, "price")),
                price_low=_float_or_none(row.get("price_low")),
                price_high=_float_or_none(row.get("price_high")),
                unit=row.get("unit") or "CNY/mt",
                quote_type=row.get("quote_type") or "market_observation",
                notes=row.get("notes") or "",
                raw=_source_raw(row, path, source_file_sha256),
            ).model_dump()
        )
    inserted = updated = unchanged = 0
    if apply:
        with closing(connect()) as connection, connection:
            for payload in payloads:
                existing = _existing_forecast_price_point(connection, payload)
                if existing is None:
                    inserted += 1
                else:
                    comparable = dict(existing)
                    comparable["raw"] = json.loads(comparable["raw"])
                    changed = any(
                        comparable.get(field) != payload.get(field)
                        for field in ("price", "price_low", "price_high", "notes")
                    ) or _semantic_raw(comparable["raw"]) != _semantic_raw(payload["raw"])
                    updated += int(changed)
                    unchanged += int(not changed)
                    if not changed:
                        payload["raw"] = comparable["raw"]
                _upsert_forecast_price_point_with_connection(
                    connection,
                    point_id=str(uuid4()),
                    payload=payload,
                )
    else:
        inserted = len(payloads)
    return ImportFileResult(
        file=path.name,
        status="imported",
        kind="forecast_price_points",
        rows=len(rows),
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
    )


def _existing_forecast_price_point(connection: sqlite3.Connection, payload: dict[str, Any]) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT price, price_low, price_high, notes, raw
        FROM forecast_price_points
        WHERE source_id = ? AND dataset_type = ? AND observed_at = ? AND company = ?
          AND product = ? AND series = ? AND spec = ? AND batch_no = ? AND poy_spec = ?
          AND market = ? AND grade = ? AND feature = ? AND unit = ? AND quote_type = ?
        ORDER BY created_at DESC LIMIT 1
        """,
        (
            payload.get("source_id", "authorized_upstream_import"),
            payload.get("dataset_type", "upstream_spot"),
            payload["observed_at"],
            payload.get("company", "source_reporter"),
            payload.get("product", "PTA"),
            payload.get("series", ""),
            payload["spec"],
            payload.get("batch_no", ""),
            payload.get("poy_spec", ""),
            payload.get("market", ""),
            payload.get("grade", ""),
            payload.get("feature", ""),
            payload.get("unit", "CNY/mt"),
            payload.get("quote_type", "market_observation"),
        ),
    ).fetchone()


def _read_xlsx_rows(path: Path) -> list[dict[str, str]]:
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if sum(info.file_size for info in infos) > MAX_UNCOMPRESSED_XLSX_BYTES:
            raise ValueError("XLSX expanded content exceeds safety limit")
        names = {info.filename for info in infos}
        if any(name.startswith("xl/externalLinks/") for name in names) or "xl/vbaProject.bin" in names:
            raise ValueError("XLSX external links or macros are not allowed")
        workbook = _safe_xml(archive.read("xl/workbook.xml"))
        relationships = _safe_xml(archive.read("xl/_rels/workbook.xml.rels"))
        first_sheet = workbook.find(f".//{{{XML_MAIN_NS}}}sheet")
        if first_sheet is None:
            raise ValueError("XLSX contains no worksheet")
        relation_id = first_sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        targets = {
            rel.attrib.get("Id"): rel.attrib.get("Target")
            for rel in relationships.findall(f".//{{{REL_NS}}}Relationship")
        }
        target = str(targets.get(relation_id) or "")
        if not target:
            raise ValueError("XLSX first worksheet relationship is missing")
        sheet_name = target.lstrip("/") if target.startswith("xl/") else f"xl/{target.lstrip('/')}"
        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            shared_root = _safe_xml(archive.read("xl/sharedStrings.xml"))
            shared_strings = [
                "".join(node.text or "" for node in item.findall(f".//{{{XML_MAIN_NS}}}t"))
                for item in shared_root.findall(f".//{{{XML_MAIN_NS}}}si")
            ]
        sheet_bytes = archive.read(sheet_name)
        if b"<f" in sheet_bytes:
            raise ValueError("XLSX formulas are not allowed in automatic imports")
        sheet = _safe_xml(sheet_bytes)
        matrix: list[list[str]] = []
        for row in sheet.findall(f".//{{{XML_MAIN_NS}}}row"):
            values: dict[int, str] = {}
            for cell in row.findall(f"{{{XML_MAIN_NS}}}c"):
                reference = str(cell.attrib.get("r") or "")
                column = _xlsx_column_index(reference)
                cell_type = cell.attrib.get("t")
                value_node = cell.find(f"{{{XML_MAIN_NS}}}v")
                inline = cell.find(f"{{{XML_MAIN_NS}}}is/{{{XML_MAIN_NS}}}t")
                raw = (value_node.text if value_node is not None else inline.text if inline is not None else "") or ""
                if cell_type == "s" and raw:
                    raw = shared_strings[int(raw)]
                values[column] = raw
            if values:
                matrix.append([values.get(index, "") for index in range(max(values) + 1)])
            if len(matrix) > MAX_ROWS + 1:
                raise ValueError(f"file exceeds {MAX_ROWS} data row limit")
    return _dict_rows(matrix)


def _safe_xml(content: bytes) -> ElementTree.Element:
    upper = content[:4096].upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("XML entities are not allowed in XLSX")
    return ElementTree.fromstring(content)


def _dict_rows(matrix: Any) -> list[dict[str, str]]:
    iterator = iter(matrix)
    try:
        headers = [str(value).strip().lower() for value in next(iterator)]
    except StopIteration as exc:
        raise ValueError("file is empty") from exc
    if not headers or any(not header for header in headers) or len(set(headers)) != len(headers):
        raise ValueError("headers must be non-empty and unique")
    rows: list[dict[str, str]] = []
    for values in iterator:
        if len(rows) >= MAX_ROWS:
            raise ValueError(f"file exceeds {MAX_ROWS} data row limit")
        normalized = [str(value).strip() for value in values]
        if not any(normalized):
            continue
        rows.append(
            {header: normalized[index] if index < len(normalized) else "" for index, header in enumerate(headers)}
        )
    return rows


def _xlsx_column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if letters is None:
        raise ValueError(f"invalid XLSX cell reference: {reference}")
    result = 0
    for letter in letters.group(0):
        result = result * 26 + ord(letter) - ord("A") + 1
    return result - 1


def _move_file(path: Path, destination_dir: Path) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    digest = sha256(path.read_bytes()).hexdigest()[:12]
    destination = destination_dir / f"{digest}-{path.name}"
    if destination.exists():
        path.unlink()
    else:
        path.replace(destination)
    return destination


def _write_report(report_dir: Path, original_name: str, result: ImportFileResult) -> None:
    digest = sha256(original_name.encode("utf-8")).hexdigest()[:12]
    path = report_dir / f"{digest}.json"
    path.write_text(json.dumps(result.as_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _stable_id(prefix: str, payload: dict[str, Any]) -> str:
    normalized = dict(payload)
    normalized["raw"] = _semantic_raw(payload.get("raw", {}))
    canonical = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-{sha256(canonical.encode('utf-8')).hexdigest()}"


def _source_raw(row: dict[str, str], path: Path, file_sha256: str) -> dict[str, str]:
    return {**row, "source_file": path.name, "source_file_sha256": file_sha256}


def _semantic_raw(value: object) -> dict[str, object]:
    raw = dict(value) if isinstance(value, dict) else {}
    raw.pop("source_file", None)
    return raw


def _required(row: dict[str, str], field: str) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise ValueError(f"missing required field: {field}")
    return value


def _required_timestamp(row: dict[str, str], field: str) -> str:
    value = _required(row, field)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO date/time field: {field}") from exc
    return value


def _float_or_none(value: object) -> float | None:
    text = str(value or "").strip()
    return float(text) if text else None


def _split_list(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[|,，;；]", value) if item.strip()]


def _truthy(value: object, *, default: bool) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "y", "是"}

from __future__ import annotations

import csv
import io
import json
import math
import re
import sqlite3
import zipfile
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing, suppress
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from .data_governance import quarantine_timestamp_invalid, validate_observed_at_syntax
from .fetchers import FRED_SERIES, cftc_zip_to_observations
from .source_registry import get_source
from .storage import _store_market_observation_with_connection, append_source_capture_revision_with_connection, connect

FRED_SOURCE_ID = "fred_macro_api"
CFTC_SOURCE_ID = "cftc_cot_petroleum"
EIA_SOURCE_ID = "eia_petroleum_api"
CFETS_SOURCE_ID = "cfets_cny_parity"
GACC_SOURCE_ID = "gacc_trade_statistics"
UN_COMTRADE_SOURCE_ID = "un_comtrade_api"
TNC_POLYESTER_SOURCE_ID = "tnc_polyester_history"
TEXNET_PRICE_ARTICLES_SOURCE_ID = "texnet_price_articles"
EXPECTED_SOURCE_IDS = {
    FRED_SOURCE_ID,
    CFTC_SOURCE_ID,
    EIA_SOURCE_ID,
    CFETS_SOURCE_ID,
    GACC_SOURCE_ID,
    UN_COMTRADE_SOURCE_ID,
    TNC_POLYESTER_SOURCE_ID,
    TEXNET_PRICE_ARTICLES_SOURCE_ID,
}
OFFICIAL_HOSTS = {
    FRED_SOURCE_ID: {"fred.stlouisfed.org", "api.stlouisfed.org"},
    CFTC_SOURCE_ID: {"cftc.gov", "www.cftc.gov"},
    EIA_SOURCE_ID: {"eia.gov", "www.eia.gov", "api.eia.gov"},
    CFETS_SOURCE_ID: {"chinamoney.com.cn", "www.chinamoney.com.cn"},
    GACC_SOURCE_ID: {"english.customs.gov.cn"},
    UN_COMTRADE_SOURCE_ID: {"comtradeapi.un.org"},
    TNC_POLYESTER_SOURCE_ID: {"www.tnc.com.cn", "tnc.com.cn"},
    TEXNET_PRICE_ARTICLES_SOURCE_ID: {"info.texnet.com.cn"},
}
FRED_DOWNLOAD_URL = "https://fred.stlouisfed.org/graph/fredgraph.zip"

EIA_SERIES: dict[str, dict[str, str]] = {
    "RWTC": {
        "indicator": "Cushing, OK WTI Spot Price FOB (RWTC)",
        "product": "crude_oil",
        "unit": "dollars_per_barrel",
        "frequency": "daily",
    },
    "RBRTE": {
        "indicator": "Europe Brent Spot Price FOB (RBRTE)",
        "product": "crude_oil",
        "unit": "dollars_per_barrel",
        "frequency": "daily",
    },
    "WCESTUS1": {
        "indicator": "U.S. Ending Stocks of Crude Oil excluding SPR (WCESTUS1)",
        "product": "crude_oil",
        "unit": "thousand_barrels",
        "frequency": "weekly",
    },
    "W_EPC0_SAX_YCUOK_MBBL": {
        "indicator": "U.S. Crude Oil Stocks at Cushing, Oklahoma (W_EPC0_SAX_YCUOK_MBBL)",
        "product": "crude_oil",
        "unit": "thousand_barrels",
        "frequency": "weekly",
    },
    "WGTSTUS1": {
        "indicator": "U.S. Ending Stocks of Total Gasoline (WGTSTUS1)",
        "product": "gasoline",
        "unit": "thousand_barrels",
        "frequency": "weekly",
    },
    "WDISTUS1": {
        "indicator": "U.S. Ending Stocks of Distillate Fuel Oil (WDISTUS1)",
        "product": "distillate",
        "unit": "thousand_barrels",
        "frequency": "weekly",
    },
    "WCRRIUS2": {
        "indicator": "U.S. Refinery Net Input of Crude Oil (WCRRIUS2)",
        "product": "refinery_run",
        "unit": "thousand_barrels_per_day",
        "frequency": "weekly",
    },
    "WCRFPUS2": {
        "indicator": "U.S. Field Production of Crude Oil (WCRFPUS2)",
        "product": "crude_oil",
        "unit": "thousand_barrels_per_day",
        "frequency": "weekly",
    },
    "WCRIMUS2": {
        "indicator": "U.S. Imports of Crude Oil (WCRIMUS2)",
        "product": "crude_oil",
        "unit": "thousand_barrels_per_day",
        "frequency": "weekly",
    },
    "WPULEUS3": {
        "indicator": "U.S. Percent Utilization of Refinery Operable Capacity (WPULEUS3)",
        "product": "refinery_run",
        "unit": "percent",
        "frequency": "weekly",
    },
}

IDENTITY_FIELDS = ("source_id", "observed_at", "indicator", "product")
MUTABLE_FIELDS = ("value", "unit", "frequency", "region", "evidence_url", "notes", "raw")


def parse_fred_zip(path: Path) -> list[dict[str, Any]]:
    _require_filename(path, "fredgraph.zip", FRED_SOURCE_ID)
    source = _required_source(FRED_SOURCE_ID)
    series_by_id = {item["series_id"]: item for item in FRED_SERIES}
    observations: list[dict[str, Any]] = []
    with zipfile.ZipFile(path) as archive:
        members = {Path(name).name.lower(): name for name in archive.namelist() if not name.endswith("/")}
        missing = [name for name in ("daily.csv", "monthly.csv") if name not in members]
        if missing:
            raise ValueError(f"{path.name}: missing FRED members: {', '.join(missing)}")
        for member_name, frequency in (("daily.csv", "daily"), ("monthly.csv", "monthly")):
            member = members[member_name]
            text = io.TextIOWrapper(archive.open(member), encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            if not reader.fieldnames:
                raise ValueError(f"{path.name}/{member}: missing CSV header")
            date_column = next(
                (name for name in reader.fieldnames if name.strip().lower() in {"date", "observation_date"}),
                None,
            )
            supported = [name for name in reader.fieldnames if name.strip() in series_by_id]
            if date_column is None or not supported:
                raise ValueError(f"{path.name}/{member}: expected a date column and at least one supported FRED series")
            for line_number, row in enumerate(reader, start=2):
                raw_date = str(row.get(date_column) or "").strip()
                if not raw_date:
                    continue
                observed_at = _validated_date(raw_date, context=f"{path.name}/{member}:{line_number}")
                for column in supported:
                    value = _to_float(row.get(column))
                    if value is None:
                        continue
                    series = series_by_id[column.strip()]
                    observations.append(
                        {
                            "source_id": source.source_id,
                            "observed_at": observed_at,
                            "indicator": f"{series['label']} ({series['series_id']})",
                            "product": series["product"],
                            "value": value,
                            "unit": series["unit"],
                            "frequency": frequency,
                            "region": "United States",
                            "evidence_url": FRED_DOWNLOAD_URL,
                            "notes": (
                                f"Imported from official FRED download {member_name}, series {series['series_id']}."
                            ),
                            "raw": {
                                "download": path.name,
                                "member": member,
                                "series_id": series["series_id"],
                                "date": raw_date,
                                "value": str(row.get(column) or "").strip(),
                            },
                        }
                    )
    return validate_observations(observations)


def parse_cftc_zip(path: Path) -> list[dict[str, Any]]:
    match = re.fullmatch(r"fut_disagg_txt_(\d{4})\.zip", path.name, flags=re.IGNORECASE)
    if match is None:
        raise ValueError(f"unexpected {CFTC_SOURCE_ID} filename: {path.name}")
    source = _required_source(CFTC_SOURCE_ID)
    evidence_url = f"https://www.cftc.gov/files/dea/history/fut_disagg_txt_{match.group(1)}.zip"
    observations = cftc_zip_to_observations(path.read_bytes(), source=source, evidence_url=evidence_url)
    wrong_year = [
        item["observed_at"] for item in observations if not str(item["observed_at"]).startswith(match.group(1))
    ]
    if wrong_year:
        raise ValueError(f"{path.name}: report date does not match archive year: {wrong_year[0]}")
    return validate_observations(observations)


def parse_eia_xls(path: Path) -> list[dict[str, Any]]:
    series_id = _eia_series_from_filename(path.name)
    config = EIA_SERIES[series_id]
    source = _required_source(EIA_SOURCE_ID)
    rows = _read_eia_xls(path, series_id=series_id)
    frequency_code = "D" if config["frequency"] == "daily" else "W"
    evidence_url = f"https://www.eia.gov/dnav/pet/hist/LeafHandler.ashx?n=PET&s={series_id}&f={frequency_code}"
    observations = [
        {
            "source_id": source.source_id,
            "observed_at": _validated_date(raw_date, context=f"{path.name}:{row_number}"),
            "indicator": config["indicator"],
            "product": config["product"],
            "value": value,
            "unit": config["unit"],
            "frequency": config["frequency"],
            "region": "United States",
            "evidence_url": evidence_url,
            "notes": f"Imported from official EIA historical spreadsheet series {series_id}.",
            "raw": {
                "download": path.name,
                "series_id": series_id,
                "date": raw_date,
                "value": value,
            },
        }
        for row_number, raw_date, value in rows
    ]
    return validate_observations(observations)


def load_official_downloads(input_dir: Path, *, require_complete: bool = True) -> list[dict[str, Any]]:
    input_dir = input_dir.expanduser().resolve()
    fred_path = input_dir / "fredgraph.zip"
    cftc_paths = sorted(input_dir.glob("fut_disagg_txt_*.zip"))
    eia_dir = input_dir / "hist_xls"
    errors: list[str] = []
    if not fred_path.is_file():
        errors.append(f"missing {fred_path}")
    if not cftc_paths:
        errors.append(f"missing {input_dir}/fut_disagg_txt_YYYY.zip")
    eia_paths = sorted(eia_dir.glob("*.xls")) if eia_dir.is_dir() else []
    found_eia = {_eia_series_from_filename(path.name): path for path in eia_paths}
    missing_eia = sorted(set(EIA_SERIES) - set(found_eia))
    if require_complete and missing_eia:
        errors.append(f"missing EIA series: {', '.join(missing_eia)}")
    if errors and require_complete:
        raise ValueError("; ".join(errors))

    payloads: list[dict[str, Any]] = []
    if fred_path.is_file():
        payloads.extend(parse_fred_zip(fred_path))
    for path in cftc_paths:
        payloads.extend(parse_cftc_zip(path))
    for path in found_eia.values():
        payloads.extend(parse_eia_xls(path))
    return deduplicate_observations(payloads)


def validate_observations(observations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    validated: list[dict[str, Any]] = []
    for index, payload in enumerate(observations, start=1):
        validated.append(_validate_observation(payload, index=index))
    return validated


def deduplicate_observations(observations: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    for payload in validate_observations(observations):
        key = tuple(str(payload[field]) for field in IDENTITY_FIELDS)
        previous = unique.get(key)
        if previous is not None and not _payload_equal(previous, payload):
            raise ValueError(f"conflicting duplicate observation: {key}")
        unique[key] = payload
    return list(unique.values())


def import_observations(
    observations: Iterable[dict[str, Any]],
    *,
    capture_revisions: Iterable[Mapping[str, Any]] = (),
    apply: bool = False,
    connection_factory: Callable[[], sqlite3.Connection] = connect,
) -> dict[str, Any]:
    actions: list[tuple[str, dict[str, Any], str | None]] = []
    unique: dict[tuple[str, ...], dict[str, Any]] = {}
    errors: list[str] = []
    for index, original_payload in enumerate(observations, start=1):
        payload = dict(original_payload)
        failure_detail = validate_observed_at_syntax(payload.get("observed_at"))
        if failure_detail is not None:
            actions.append(("rejected", payload, failure_detail))
            errors.append(f"observation {index}: timestamp_invalid")
            continue
        payload = _validate_observation(payload, index=index)
        key = _identity_key(payload)
        previous = unique.get(key)
        if previous is not None:
            if not _payload_equal(previous, payload):
                raise ValueError(f"conflicting duplicate observation: {key}")
            continue
        unique[key] = payload
        actions.append(("accepted", payload, None))
    payloads = list(unique.values())
    capture_payloads = [dict(capture) for capture in capture_revisions]
    observation_identities = {(str(payload["source_id"]), str(payload["observed_at"])) for payload in payloads}
    for capture in capture_payloads:
        required = {
            "source_id",
            "semantic_series_id",
            "observed_at",
            "published_at",
            "visible_at",
            "captured_at",
            "source_url",
            "raw_sha256",
            "authorization_scope",
            "contract_version",
            "parser_version",
            "canonical_payload",
        }
        if required - set(capture):
            raise ValueError("source_capture_revision_fields_missing")
        if (str(capture["source_id"]), str(capture["observed_at"])) not in observation_identities:
            raise ValueError("source_capture_revision_without_observation")
        if not re.fullmatch(r"[0-9a-f]{64}", str(capture["raw_sha256"])):
            raise ValueError("source_capture_revision_raw_sha256_invalid")
        if not isinstance(capture["canonical_payload"], dict):
            raise ValueError("source_capture_revision_payload_invalid")
    capture_inserted = 0
    capture_unchanged = 0
    with closing(connection_factory()) as connection:
        existing_by_key = _load_existing_observations(connection, payloads)
        statuses_by_key = {
            _identity_key(payload): _classify_observation(
                payload,
                existing=existing_by_key.get(_identity_key(payload)),
            )
            for payload in payloads
        }
        if capture_payloads and not apply:
            requested_source_ids = {str(capture["source_id"]) for capture in capture_payloads}
            existing_capture_identities = {
                (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
                for row in connection.execute(
                    """
                    SELECT source_id, semantic_series_id, observed_at, raw_sha256
                    FROM source_capture_revisions
                    """
                ).fetchall()
                if str(row[0]) in requested_source_ids
            }
            for capture in capture_payloads:
                identity = (
                    str(capture["source_id"]),
                    str(capture["semantic_series_id"]),
                    str(capture["observed_at"]),
                    str(capture["raw_sha256"]),
                )
                if identity in existing_capture_identities:
                    capture_unchanged += 1
                else:
                    capture_inserted += 1
        if apply:
            with connection:
                created_at = datetime.now(UTC).isoformat()
                for action, payload, failure_detail in actions:
                    if action == "rejected":
                        quarantine_timestamp_invalid(
                            connection,
                            payload,
                            failure_detail=str(failure_detail),
                            received_at=created_at,
                        )
                        continue
                    _apply_observation_changes(
                        connection,
                        [payload],
                        [statuses_by_key[_identity_key(payload)]],
                    )
                for capture in capture_payloads:
                    _, inserted = append_source_capture_revision_with_connection(
                        connection,
                        capture_revision_id=str(uuid4()),
                        source_id=str(capture["source_id"]),
                        semantic_series_id=str(capture["semantic_series_id"]),
                        observed_at=str(capture["observed_at"]),
                        published_at=str(capture["published_at"]),
                        visible_at=str(capture["visible_at"]),
                        captured_at=str(capture["captured_at"]),
                        source_url=str(capture["source_url"]),
                        raw_sha256=str(capture["raw_sha256"]),
                        authorization_scope=str(capture["authorization_scope"]),
                        contract_version=str(capture["contract_version"]),
                        parser_version=str(capture["parser_version"]),
                        canonical_payload=dict(capture["canonical_payload"]),
                    )
                    if inserted:
                        capture_inserted += 1
                    else:
                        capture_unchanged += 1
    statuses = list(statuses_by_key.values())
    inserted = statuses.count("inserted")
    updated = statuses.count("updated")
    unchanged = statuses.count("unchanged")
    summary = {
        "mode": "apply" if apply else "dry-run",
        "accepted": len(payloads),
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
    }
    if capture_payloads:
        summary.update(
            {
                "capture_revisions": len(capture_payloads),
                "capture_revisions_inserted": capture_inserted,
                "capture_revisions_unchanged": capture_unchanged,
            }
        )
    if errors:
        summary["rejected"] = len(errors)
        summary["errors"] = errors
    return summary


def run_import(input_dir: Path, *, apply: bool = False, require_complete: bool = True) -> dict[str, Any]:
    payloads = load_official_downloads(input_dir, require_complete=require_complete)
    return import_observations(payloads, apply=apply)


def _load_existing_observations(
    connection: sqlite3.Connection,
    payloads: list[dict[str, Any]],
) -> dict[tuple[str, ...], dict[str, Any]]:
    if not payloads:
        return {}
    connection.row_factory = sqlite3.Row
    source_ids = sorted({str(payload["source_id"]) for payload in payloads})
    observed_dates = [str(payload["observed_at"]) for payload in payloads]
    placeholders = ",".join("?" for _ in source_ids)
    try:
        rows = connection.execute(
            f"""
            SELECT source_id, observed_at, indicator, product,
                   value, unit, frequency, region, evidence_url, notes, raw
            FROM market_observations
            WHERE source_id IN ({placeholders})
              AND observed_at BETWEEN ? AND ?
            """,
            (*source_ids, min(observed_dates), max(observed_dates)),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return {}
        raise
    existing: dict[tuple[str, ...], dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        with suppress(TypeError, json.JSONDecodeError):
            item["raw"] = json.loads(item["raw"])
        existing[_identity_key(item)] = item
    return existing


def _classify_observation(payload: dict[str, Any], *, existing: dict[str, Any] | None) -> str:
    if existing is None:
        return "inserted"
    return "unchanged" if _payload_equal(existing, payload) else "updated"


def _apply_observation_changes(
    connection: sqlite3.Connection,
    payloads: list[dict[str, Any]],
    statuses: list[str],
) -> None:
    created_at = datetime.now(UTC).isoformat()
    for payload, status in zip(payloads, statuses, strict=True):
        if status == "unchanged":
            continue
        stored = _store_market_observation_with_connection(
            connection,
            observation_id=str(uuid4()),
            payload=payload,
            created_at=created_at,
        )
        if stored is None:
            raise RuntimeError("validated official observation was quarantined")


def _validate_observation(payload: dict[str, Any], *, index: int) -> dict[str, Any]:
    source_id = str(payload.get("source_id") or "")
    if source_id not in EXPECTED_SOURCE_IDS:
        raise ValueError(f"observation {index}: unsupported source_id {source_id!r}")
    _required_source(source_id)
    payload["observed_at"] = _validated_date(
        str(payload.get("observed_at") or ""), context=f"observation {index} ({source_id})"
    )
    host = (urlparse(str(payload.get("evidence_url") or "")).hostname or "").lower()
    if host not in OFFICIAL_HOSTS[source_id]:
        raise ValueError(f"observation {index}: non-official evidence host {host!r} for {source_id}")
    for field in ("indicator", "product", "unit", "frequency", "region"):
        if not str(payload.get(field) or "").strip():
            raise ValueError(f"observation {index}: missing {field}")
    value = payload.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"observation {index}: value must be a finite number")
    return payload


def _identity_key(payload: dict[str, Any]) -> tuple[str, ...]:
    return tuple(str(payload[field]) for field in IDENTITY_FIELDS)


def _payload_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(field) == right.get(field) for field in MUTABLE_FIELDS)


def _required_source(source_id: str):
    source = get_source(source_id)
    if source is None or source.source_id != source_id:
        raise ValueError(f"official source is not registered: {source_id}")
    host = (urlparse(source.url).hostname or "").lower()
    if host not in OFFICIAL_HOSTS[source_id]:
        raise ValueError(f"registered source URL is not official for {source_id}: {source.url}")
    return source


def _validated_date(value: str, *, context: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{context}: invalid ISO date {value!r}") from exc
    if parsed.isoformat() != value or parsed > datetime.now(UTC).date():
        raise ValueError(f"{context}: date must be an ISO date no later than today: {value!r}")
    return value


def _require_filename(path: Path, expected: str, source_id: str) -> None:
    if path.name.lower() != expected.lower():
        raise ValueError(f"unexpected {source_id} filename: {path.name}; expected {expected}")


def _to_float(value: object) -> float | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {".", "NA", "N/A", "nan"}:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"invalid numeric value: {value!r}") from exc


def _eia_series_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    for series_id in sorted(EIA_SERIES, key=len, reverse=True):
        suffix = "d" if EIA_SERIES[series_id]["frequency"] == "daily" else "w"
        if stem.lower() in {series_id.lower(), f"{series_id}{suffix}".lower()}:
            return series_id
    raise ValueError(f"unsupported EIA historical spreadsheet: {filename}")


def _read_eia_xls(path: Path, *, series_id: str) -> list[tuple[int, str, float]]:
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - provided by the locked runtime through akshare.
        raise RuntimeError("reading EIA .xls files requires the project's xlrd runtime dependency") from exc

    workbook = xlrd.open_workbook(file_contents=path.read_bytes())
    for sheet in workbook.sheets():
        header: tuple[int, int, int] | None = None
        for row_index in range(sheet.nrows):
            cells = [str(sheet.cell_value(row_index, column)).strip() for column in range(sheet.ncols)]
            date_columns = [column for column, value in enumerate(cells) if value.lower() == "date"]
            if not date_columns:
                continue
            date_column = date_columns[0]
            value_columns = [
                column
                for column in range(date_column + 1, sheet.ncols)
                if cells[column] and (cells[column].upper() == series_id.upper() or header is None)
            ]
            if value_columns:
                header = (row_index, date_column, value_columns[0])
                if cells[value_columns[0]].upper() == series_id.upper():
                    break
        if header is None:
            continue
        header_row, date_column, value_column = header
        parsed_rows: list[tuple[int, str, float]] = []
        for row_index in range(header_row + 1, sheet.nrows):
            raw_date = sheet.cell_value(row_index, date_column)
            raw_value = sheet.cell_value(row_index, value_column)
            if raw_date in (None, "") or raw_value in (None, ""):
                continue
            observed_at = _xls_date(raw_date, datemode=workbook.datemode)
            value = _to_float(raw_value)
            if value is not None:
                parsed_rows.append((row_index + 1, observed_at, value))
        if parsed_rows:
            return parsed_rows
    raise ValueError(f"{path.name}: could not find Date/{series_id} data columns")


def _xls_date(value: object, *, datemode: int) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)):
        import xlrd

        return xlrd.xldate_as_datetime(value, datemode).date().isoformat()
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"invalid EIA spreadsheet date: {value!r}")

"""Bounded point-in-time input export shared by production and replay."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlsplit

from .price_intraday import public_spot_quote_matches_instrument
from .seven_product_contract import CURRENT_LABEL_REGISTRY_VERSION, LABEL_REGISTRY


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    if (
        parts.username
        or parts.password
        or any(
            any(word in key.lower() for word in ("key", "token", "secret", "password", "signature"))
            for key, _ in parse_qsl(parts.query)
        )
    ):
        raise ValueError("credential_bearing_evidence_url_export_refused")
    return url


def export_vintages(connection: sqlite3.Connection, *, as_of: str, row_limit: int = 20000) -> dict:
    cutoff = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if cutoff.tzinfo is None or row_limit < 1:
        raise ValueError("explicit_timezone_and_positive_row_limit_required")
    cutoff = cutoff.astimezone(UTC).isoformat()
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    deadline = time.monotonic() + 45
    connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    result = {
        "schema_version": "prediction-vintages.v1",
        "as_of_time": cutoff,
        "label_registry_version": CURRENT_LABEL_REGISTRY_VERSION,
        "series": {},
    }
    connection.execute("BEGIN")
    try:
        for target, definition in LABEL_REGISTRY.items():
            if target == "crude":
                rows = connection.execute(
                    "SELECT observation_id,source_id,product,indicator,observed_at,created_at,value,unit,evidence_url "
                    "FROM market_observations WHERE source_id=? AND product=? AND indicator=? "
                    "AND julianday(created_at)<=julianday(?) ORDER BY observed_at,created_at,observation_id LIMIT ?",
                    (definition.source_id, "crude_oil", "Brent futures daily close", cutoff, row_limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM source_capture_revisions WHERE source_id=? AND semantic_series_id=? "
                    "AND julianday(visible_at)<=julianday(?) "
                    "ORDER BY observed_at,visible_at,created_at,capture_revision_id LIMIT ?",
                    (definition.source_id, definition.series_id, cutoff, row_limit + 1),
                ).fetchall()
            if len(rows) > row_limit:
                raise ValueError(f"{target}:export_truncated_refused")
            records = []
            for raw in rows:
                row = dict(raw)
                if target == "crude":
                    record = {
                        "revision_id": row["observation_id"],
                        "observed_at": row["observed_at"],
                        "visible_at": row["created_at"],
                        "created_at": row["created_at"],
                        "captured_at": row["created_at"],
                        "value": row["value"],
                        "unit": row["unit"],
                        "source_id": row["source_id"],
                        "series_id": definition.series_id,
                        "source_url": safe_url(row["evidence_url"]),
                        "evidence_sha256": digest(row),
                        "hash_kind": "stored_fields_only",
                        "payload_hash_verified": True,
                        "instrument_matches": True,
                        "contract_version": CURRENT_LABEL_REGISTRY_VERSION,
                    }
                else:
                    payload = json.loads(row["canonical_payload"])
                    if digest(payload) != row["canonical_payload_hash"]:
                        raise ValueError(f"{target}:canonical_payload_hash_mismatch")
                    field = "settle" if target in {"px", "pta"} else "value" if target in {"poy", "dty"} else "last"
                    # A close is not silently substituted for a missing settlement.
                    record = {
                        "revision_id": row["capture_revision_id"],
                        "observed_at": row["observed_at"],
                        "visible_at": row["visible_at"],
                        "created_at": row["created_at"],
                        "captured_at": row["captured_at"],
                        "value": payload.get(field),
                        "unit": payload.get("unit", ""),
                        "source_id": row["source_id"],
                        "series_id": row["semantic_series_id"],
                        "source_url": safe_url(row["source_url"]),
                        "evidence_sha256": row["raw_sha256"],
                        "hash_kind": "source_capture",
                        "payload_sha256": row["canonical_payload_hash"],
                        "payload_hash_verified": True,
                        "instrument_matches": target != "naphtha"
                        or public_spot_quote_matches_instrument(
                            "NAPHTHA", {**payload, "source_id": row["source_id"], "observed_at": row["observed_at"]}
                        ),
                        "contract_version": row["contract_version"],
                        "parser_version": row["parser_version"],
                    }
                records.append(record)
            result["series"][target] = {
                "source_id": definition.source_id,
                "series_id": definition.series_id,
                "unit": definition.unit,
                "market": definition.market,
                "records": records,
                "truncated": False,
            }
    finally:
        connection.rollback()
        connection.set_progress_handler(None, 0)
    result["content_sha256"] = digest(result)
    return result

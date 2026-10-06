"""Revalidate retained same-provider quotes and append missing label captures only.

Default is read-only. Never rewrite a legacy observation, infer a missing date,
or give newly qualified evidence an earlier visibility boundary.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app import storage  # noqa: E402
from app.price_intraday import _naphtha_public_quote, _public_spot_page_to_row  # noqa: E402
from app.seven_product_contract import CURRENT_LABEL_REGISTRY_VERSION, LABEL_REGISTRY  # noqa: E402


def validated_capture(row: dict, *, now: str) -> dict:
    instrument = row.get("instrument")
    target = {"MEG": "meg", "NAPHTHA": "naphtha"}.get(instrument)
    if target is None or row.get("source_id") != "public_spot_page_refresh":
        raise ValueError("unexpected legacy source")
    definition = LABEL_REGISTRY[target]
    expected_urls = (
        {"https://www.sunsirs.com/uk/prodetail-222.html"}
        if target == "meg"
        else {"https://zh.tradingeconomics.com/commodity/naphtha", "https://www.tradingeconomics.com/commodity/naphtha"}
    )
    if row.get("source_url") not in expected_urls or row.get("unit") != definition.unit:
        raise ValueError("incompatible quote basis")
    raw = json.loads(row["raw"]) if isinstance(row.get("raw"), str) else row.get("raw", {})
    text = str(raw.get("quote") or "")
    quote = None
    if target == "meg":
        match = re.search(r"\bEthylene glycol\s+Chemical\s+([\d,]+(?:\.\d+)?)\s+(20\d{2}-\d{2}-\d{2})\b", text)
        if match:
            quote = (float(match[1].replace(",", "")), match[2], match[0])
    else:
        quote = _naphtha_public_quote(text)
    if not quote or not math.isfinite(float(row["last"])):
        raise ValueError("dated product quote missing")
    if quote[1] != row.get("observed_at") or abs(quote[0] - float(row["last"])) >= 0.01:
        raise ValueError("stored date/value does not match its own source quote")
    if date.fromisoformat(quote[1]) > datetime.fromisoformat(now).date():
        raise ValueError("future observation")
    payload = _public_spot_page_to_row(
        "",
        instrument=instrument,
        symbol=f"{instrument}_PUBLIC_SPOT",
        labels=(),
        unit=definition.unit,
        source_url=row["source_url"],
        latency=0,
        source_id=definition.source_id,
        quote=quote,
    )
    provenance = {
        "legacy_observation_id": row["observation_id"],
        "legacy_created_at": row["created_at"],
        "retained_quote_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }
    payload["raw"].update({"captured_at": now, "revalidated_at": now, **provenance})
    return {
        "source_id": definition.source_id,
        "semantic_series_id": definition.series_id,
        "observed_at": quote[1],
        "published_at": now,
        "visible_at": now,
        "captured_at": now,
        "source_url": row["source_url"],
        "raw_sha256": payload["raw"]["raw_evidence_sha256"],
        "authorization_scope": "public_personal_reuse",
        "contract_version": CURRENT_LABEL_REGISTRY_VERSION,
        "parser_version": "retained-public-spot-revalidation.v1",
        "canonical_payload": payload,
    }


def prepare(db: Path, *, now: str | None = None) -> dict:
    now = now or datetime.now(UTC).isoformat()
    package = {"schema": "verified-retained-spot.v1", "prepared_at": now, "captures": [], "rejected": [], "existing": 0}
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as con:
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA query_only=ON")
        covered = {
            (r[0], r[1])
            for r in con.execute(
                "SELECT semantic_series_id,observed_at FROM source_capture_revisions WHERE semantic_series_id IN (?,?)",
                (LABEL_REGISTRY["meg"].series_id, LABEL_REGISTRY["naphtha"].series_id),
            )
        }
        rows = con.execute(
            "SELECT * FROM intraday_price_observations WHERE source_id='public_spot_page_refresh' "
            "AND instrument IN ('MEG','NAPHTHA') ORDER BY observed_at,created_at"
        ).fetchall()
    for legacy in rows:
        row = dict(legacy)
        try:
            capture = validated_capture(row, now=now)
        except (ValueError, KeyError, TypeError) as exc:
            package["rejected"].append(
                {
                    "observation_id": row["observation_id"],
                    "instrument": row["instrument"],
                    "observed_at": row["observed_at"],
                    "reason": str(exc),
                }
            )
            continue
        identity = (capture["semantic_series_id"], capture["observed_at"])
        if identity in covered:
            package["existing"] += 1
            continue
        covered.add(identity)
        package["captures"].append(capture)
    return package


def apply(db: Path, package: dict) -> dict:
    if package.get("schema") != "verified-retained-spot.v1":
        raise ValueError("unexpected package schema")
    if Path(storage.settings.sqlite_path).resolve() != db.resolve():
        raise ValueError("write database differs from approved input")
    # Re-read originals and revalidate instead of trusting a mutable candidate file.
    now = datetime.now(UTC).isoformat()
    fresh = prepare(db, now=now)
    allowed = {(c["semantic_series_id"], c["observed_at"], c["raw_sha256"]) for c in fresh["captures"]}
    ids = []
    with closing(storage.connect()) as con, con:
        for cap in package["captures"]:
            key = (cap["semantic_series_id"], cap["observed_at"], cap["raw_sha256"])
            if con.execute(
                "SELECT 1 FROM source_capture_revisions WHERE semantic_series_id=? AND observed_at=?", key[:2]
            ).fetchone():
                continue
            if key not in allowed:
                raise ValueError("candidate no longer matches retained original")
            verified = next(
                c for c in fresh["captures"] if (c["semantic_series_id"], c["observed_at"], c["raw_sha256"]) == key
            )
            rid, inserted = storage.append_source_capture_revision_with_connection(
                con,
                capture_revision_id=str(uuid4()),
                **verified,
            )
            if inserted:
                ids.append(rid)
    return {"inserted": len(ids), "capture_revision_ids": ids, "visible_at": now}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        if args.candidate is None:
            parser.error("--apply requires --candidate and an explicit production approval")
        result = apply(args.db, json.loads(args.candidate.read_text()))
    else:
        result = prepare(args.db)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {
                "applied": args.apply,
                "captures": len(result.get("captures", [])),
                "rejected": len(result.get("rejected", [])),
                "inserted": result.get("inserted", 0),
            }
        )
    )


if __name__ == "__main__":
    main()

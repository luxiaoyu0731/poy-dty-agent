"""Prepare and apply a bounded, missing-day-only official PX/PTA history package.

No writes without --apply. Captures retain the actual acquisition time, never
an observation-day visibility time. Existing days and predictions are untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import re
import sqlite3
import sys
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
from app.official_futures_daily import fetch_czce_pta_px_daily  # noqa: E402
from app.publication_time import publication_instant  # noqa: E402
from app.settings import settings  # noqa: E402
from app.source_registry import get_source  # noqa: E402
from app.storage import bulk_upsert_futures_daily_bars_with_capture_revisions  # noqa: E402


def existing_days(db: Path) -> set[tuple[str, str]]:
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as con:
        con.execute("PRAGMA query_only=ON")
        return set(
            con.execute(
                "SELECT DISTINCT product,trade_date FROM futures_daily_bars "
                "WHERE source_id='czce_pta_px' AND product IN ('PX','PTA') AND contract_role='main'"
            )
        )


def missing_days(existing: set, start: date, end: date) -> list[date]:
    if end < start or (end - start).days > 92:
        raise ValueError("history window must be 0..92 calendar days")
    return [
        start + timedelta(days=i)
        for i in range((end - start).days + 1)
        if (start + timedelta(days=i)).weekday() < 5
        and any((p, (start + timedelta(days=i)).isoformat()) not in existing for p in ("PX", "PTA"))
    ]


async def prepare(db: Path, output: Path, start: date, end: date) -> dict:
    existing = existing_days(db)
    package = {
        "schema": "czce-missing-history.v1",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "bars": [],
        "captures": [],
        "attempts": [],
    }
    if output.exists():
        prior = json.loads(output.read_text())
        if (
            prior.get("schema") != package["schema"]
            or prior.get("start") != package["start"]
            or prior.get("end") != package["end"]
        ):
            raise ValueError("existing candidate belongs to a different window")
        package = prior
        existing |= {(b["product"], b["trade_date"]) for b in package["bars"]}
    for day in missing_days(existing, start, end):
        try:
            result = await fetch_czce_pta_px_daily(get_source("czce_pta_px"), today=day, lookback_days=1)
            bars = [
                r
                for r in result.bars
                if r["product"] in ("PX", "PTA") and (r["product"], r["trade_date"]) not in existing
            ]
            captures = [
                r
                for r in result.capture_revisions
                if str(r["semantic_series_id"]).startswith(("px.", "pta."))
                and any(
                    b["trade_date"] == r["observed_at"]
                    and b["product"].lower() == r["semantic_series_id"].split(".")[0]
                    for b in bars
                )
            ]
            package["bars"].extend(bars)
            package["captures"].extend(captures)
            package["attempts"].append({"date": day.isoformat(), "status": result.status, "bars": len(bars)})
        except Exception as exc:
            package["attempts"].append({"date": day.isoformat(), "status": "failed", "error": type(exc).__name__})
            break  # no repeat after rate limit/access failure; resume only after review
        output.write_text(json.dumps(package, ensure_ascii=False, indent=2))
        await asyncio.sleep(1)
    output.write_text(json.dumps(package, ensure_ascii=False, indent=2))
    return package


def validate_package(package: dict) -> None:
    if package.get("schema") != "czce-missing-history.v1":
        raise ValueError("unexpected package schema")
    start, end = date.fromisoformat(package["start"]), date.fromisoformat(package["end"])
    missing_days(set(), start, end)
    mains = {}
    identities = set()
    for bar in package["bars"]:
        day = date.fromisoformat(bar["trade_date"])
        expected_url = f"https://www.czce.com.cn/cn/DFSStaticFiles/Future/{day.year}/{day:%Y%m%d}/FutureDataDaily.txt"
        identity = (bar["product"], bar["trade_date"], bar["contract_code"])
        if identity in identities or not start <= day <= end:
            raise ValueError("duplicate bar or out-of-window day")
        identities.add(identity)
        if (
            bar.get("source_id") != "czce_pta_px"
            or bar.get("product") not in ("PX", "PTA")
            or bar.get("unit") != "CNY/mt"
            or bar.get("source_url") != expected_url
        ):
            raise ValueError("unexpected bar provenance")
        captured = publication_instant(bar["raw"].get("captured_at"))
        visible = publication_instant(bar.get("visible_at"))
        if not captured or not visible or visible < captured or visible[:10] < day.isoformat():
            raise ValueError("invalid capture visibility")
        if not re.fullmatch(r"[a-f0-9]{64}", bar["raw"].get("raw_sha256", "")):
            raise ValueError("missing raw hash")
        if any(
            not math.isfinite(float(bar[k])) or float(bar[k]) <= 0 for k in ("open", "high", "low", "close", "settle")
        ):
            raise ValueError("invalid price")
        if bar.get("is_main"):
            key = (bar["product"].lower(), bar["trade_date"])
            if key in mains:
                raise ValueError("multiple main contracts")
            mains[key] = bar
    covered = set()
    for cap in package["captures"]:
        key = (cap["semantic_series_id"].split(".")[0], cap["observed_at"])
        main = mains.get(key)
        if not main or key in covered or cap["canonical_payload"] != main:
            raise ValueError("capture does not match main bar")
        if (
            cap["source_id"] != "czce_pta_px"
            or cap["source_url"] != main["source_url"]
            or cap["raw_sha256"] != main["raw"]["raw_sha256"]
            or cap["visible_at"] != main["visible_at"]
            or cap["captured_at"] != main["raw"]["captured_at"]
        ):
            raise ValueError("capture provenance mismatch")
        covered.add(key)
    if covered != set(mains):
        raise ValueError("missing main capture")


def apply(db: Path, package: dict) -> dict:
    validate_package(package)
    existing = existing_days(db)
    bars = [b for b in package["bars"] if (b["product"], b["trade_date"]) not in existing]
    allowed = {(b["product"].lower(), b["trade_date"]) for b in bars}
    caps = [c for c in package["captures"] if (c["semantic_series_id"].split(".")[0], c["observed_at"]) in allowed]
    for bar in bars:
        if bar.get("source_id") != "czce_pta_px" or bar.get("product") not in ("PX", "PTA"):
            raise ValueError("unexpected source or product")
    # Append capture and bar projections through the existing atomic contract.
    object.__setattr__(settings, "sqlite_path", str(db))
    stored = (
        bulk_upsert_futures_daily_bars_with_capture_revisions(
            bars, caps, id_factory=lambda: str(uuid4()), capture_revision_id_factory=lambda: str(uuid4())
        )
        if bars
        else []
    )
    return {"bars_inserted": len(stored), "captures_submitted": len(caps), "existing_days_untouched": len(existing)}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", type=Path, required=True)
    p.add_argument("--package", type=Path, required=True)
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args(argv)
    if args.apply:
        print(json.dumps(apply(args.db, json.loads(args.package.read_text())), ensure_ascii=False))
    else:
        if args.start is None or args.end is None:
            p.error("--start and --end required to prepare")
        args.package.parent.mkdir(parents=True, exist_ok=True)
        result = asyncio.run(prepare(args.db, args.package, args.start, args.end))
        print(
            json.dumps(
                {"bars": len(result["bars"]), "captures": len(result["captures"]), "attempts": result["attempts"]}
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

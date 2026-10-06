"""Prepare and apply a bounded, missing-day-only texnet POY/DTY history package.

Walks the public texnet price channel listing pages, prefers per-product daily
articles ("X月X日涤纶POY为XXXX") and falls back to the daily summary table
(纺织大宗商品价格涨跌榜). No writes without --apply. Captures retain the actual
acquisition time, never an observation-day visibility time; existing days and
observations are untouched (append-only via import_observations).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import ssl
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

import httpx  # noqa: E402

from app.official_downloads import import_observations  # noqa: E402
from app.settings import settings  # noqa: E402
from app.texnet_price_history import (  # noqa: E402
    TEXNET_LIST_URL,
    TEXNET_REFERER,
    TEXNET_USER_AGENT,
    now_iso,
    parse_texnet_listing,
    parse_texnet_zdb_article,
    resolve_article_year,
    sha256_hex,
    texnet_capture_payload,
    texnet_observation_payload,
)

CONTRACT_VERSION = "texnet-history.v1"
PARSER_VERSION = "texnet-price-parse.v1"
MAX_LIST_PAGES = 1300
STOP_AFTER_EMPTY = 12


def existing_days(db: Path) -> set[tuple[str, str]]:
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)) as con:
        con.execute("PRAGMA query_only=ON")
        return set(
            con.execute(
                "SELECT DISTINCT product, observed_at FROM market_observations "
                "WHERE source_id='texnet_price_articles' AND product IN ('poy','dty')"
            )
        )


async def _client() -> httpx.AsyncClient:
    tls_context = ssl.create_default_context()
    tls_context.set_ciphers("HIGH:!aNULL:!eNULL")
    return httpx.AsyncClient(verify=tls_context, timeout=20, follow_redirects=False)


async def build_index(index_path: Path, *, start: str, end: str) -> dict[str, Any]:
    """Walk listing pages forward until pages are older than `start`."""
    if index_path.exists():
        prior = json.loads(index_path.read_text())
        if prior.get("start") == start and prior.get("end") == end:
            return prior
    index: dict[str, Any] = {"start": start, "end": end, "pages": [], "articles": []}
    seen_urls: set[str] = set()
    empty_streak = 0
    async with await _client() as http:
        for page in range(1, MAX_LIST_PAGES + 1):
            url = TEXNET_LIST_URL.format(page=page)
            settings.require_outbound_url_allowed(url)
            try:
                response = await http.get(
                    url, headers={"User-Agent": TEXNET_USER_AGENT, "Referer": TEXNET_REFERER}
                )
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - record and stop for review.
                index["pages"].append({"page": page, "status": "failed", "error": type(exc).__name__})
                break
            refs = parse_texnet_listing(response.text, year_hint=None)
            index["pages"].append({"page": page, "status": "ok", "articles": len(refs)})
            if not refs:
                empty_streak += 1
                if empty_streak >= STOP_AFTER_EMPTY:
                    break
            else:
                empty_streak = 0
            for ref in refs:
                if ref.url not in seen_urls:
                    seen_urls.add(ref.url)
                    index["articles"].append(
                        {
                            "url": ref.url,
                            "title": ref.title,
                            "kind": ref.kind,
                            "product": ref.product,
                            "month": ref.month,
                            "day": ref.day,
                            "year_hint": ref.year_hint,
                            "title_value": ref.title_value,
                        }
                    )
            index_path.write_text(json.dumps(index, ensure_ascii=False))
            await asyncio.sleep(0.4)
    index_path.write_text(json.dumps(index, ensure_ascii=False))
    return index


def _resolve_date(month: int, day: int, year_hint: int | None) -> str | None:
    if not year_hint:
        return None
    try:
        return f"{year_hint:04d}-{month:02d}-{day:02d}"
    except Exception:  # noqa: BLE001 - malformed dates are skipped.
        return None


async def prepare(db: Path, index_path: Path, package_path: Path, *, start: str, end: str) -> dict:
    index = await build_index(index_path, start=start, end=end)
    existing = existing_days(db)
    package: dict[str, Any] = {
        "schema": "texnet-missing-history.v1",
        "start": start,
        "end": end,
        "observations": [],
        "captures": [],
        "attempts": [],
    }
    if package_path.exists():
        prior = json.loads(package_path.read_text())
        if prior.get("schema") == package["schema"] and prior.get("start") == start:
            package = prior
            done_keys = {(o["product"], str(o["observed_at"])) for o in package["observations"]}
        else:
            done_keys = set()
    else:
        done_keys = set()

    articles = index["articles"]
    # dailies first (title carries the value), zdb as per-date fallback
    daily_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    zdb_by_date: dict[str, dict[str, Any]] = {}
    for art in articles:
        if art["kind"] == "daily" and art["year_hint"]:
            date = _resolve_date(art["month"], art["day"], art["year_hint"])
            if date and start <= date <= end:
                daily_by_key.setdefault((art["product"], date), art)
        elif art["kind"] == "zdb" and art["year_hint"]:
            date = _resolve_date(art["month"], art["day"], art["year_hint"])
            if date and start <= date <= end:
                zdb_by_date.setdefault(date, art)

    plan: list[tuple[str, dict[str, Any]]] = []
    for (product, date), art in sorted(daily_by_key.items()):
        if (product, date) not in existing and (product, date) not in done_keys:
            plan.append((date, art))
    needed_zdb_dates = sorted(
        date
        for date in zdb_by_date
        if any((p, date) not in existing and (p, date) not in done_keys for p in ("poy", "dty"))
    )
    for date in needed_zdb_dates:
        plan.append((date, zdb_by_date[date]))
    plan.sort(key=lambda item: item[0])

    captured_at = now_iso()
    async with await _client() as http:
        for date, art in plan:
            try:
                response = await http.get(
                    art["url"], headers={"User-Agent": TEXNET_USER_AGENT, "Referer": TEXNET_REFERER}
                )
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001 - stop for review, resumable.
                package["attempts"].append(
                    {"url": art["url"], "status": "failed", "error": type(exc).__name__}
                )
                break
            raw_sha256 = sha256_hex(response.content)
            added = 0
            if art["kind"] == "daily":
                year = resolve_article_year(response.text, year_hint=art["year_hint"])
                resolved = _resolve_date(art["month"], art["day"], int(year) if year else None)
                if resolved == date and art.get("title_value"):
                    product = str(art["product"])
                    package["observations"].append(
                        texnet_observation_payload(
                            product=product,
                            observed_at=date,
                            value=float(art["title_value"]),
                            evidence_url=art["url"],
                            captured_at=captured_at,
                            raw_sha256=raw_sha256,
                            notes_extra="来源：日度单品行情文章标题值。",
                        )
                    )
                    package["captures"].append(
                        texnet_capture_payload(
                            product=product,
                            observed_at=date,
                            value=float(art["title_value"]),
                            evidence_url=art["url"],
                            captured_at=captured_at,
                            raw_sha256=raw_sha256,
                            contract_version=CONTRACT_VERSION,
                            parser_version=PARSER_VERSION,
                        )
                    )
                    added += 1
            else:
                rows = parse_texnet_zdb_article(response.text)
                for product in ("poy", "dty"):
                    row = rows.get(product)
                    if not row or (product, date) in existing or (product, date) in done_keys:
                        continue
                    package["observations"].append(
                        texnet_observation_payload(
                            product=product,
                            observed_at=date,
                            value=float(row["value"]),
                            evidence_url=art["url"],
                            captured_at=captured_at,
                            raw_sha256=raw_sha256,
                            notes_extra=f"来源：纺织大宗商品价格涨跌榜（前值{row['prev_value']}，日涨跌{row['change_text']}）。",
                        )
                    )
                    package["captures"].append(
                        texnet_capture_payload(
                            product=product,
                            observed_at=date,
                            value=float(row["value"]),
                            evidence_url=art["url"],
                            captured_at=captured_at,
                            raw_sha256=raw_sha256,
                            contract_version=CONTRACT_VERSION,
                            parser_version=PARSER_VERSION,
                        )
                    )
                    added += 1
            package["attempts"].append({"url": art["url"], "status": "ok", "added": added})
            package_path.write_text(json.dumps(package, ensure_ascii=False))
            await asyncio.sleep(1.0)
    package_path.write_text(json.dumps(package, ensure_ascii=False))
    return package


def validate_package(package: dict) -> None:
    if package.get("schema") != "texnet-missing-history.v1":
        raise ValueError("unexpected package schema")
    seen: set[tuple[str, str]] = set()
    for obs in package["observations"]:
        key = (str(obs["product"]), str(obs["observed_at"]))
        if key in seen:
            raise ValueError(f"duplicate observation: {key}")
        seen.add(key)
        if obs["source_id"] != "texnet_price_articles" or obs["unit"] != "CNY/mt":
            raise ValueError("unexpected observation provenance")
        if not isinstance(obs["value"], (int, float)) or obs["value"] <= 0:
            raise ValueError(f"invalid value for {key}")
        date = str(obs["observed_at"])
        if not (package["start"] <= date <= package["end"]):
            raise ValueError(f"out-of-window day: {date}")
    for cap in package["captures"]:
        if len(str(cap.get("raw_sha256", ""))) != 64:
            raise ValueError("capture raw hash invalid")
        if (str(cap["source_id"]), str(cap["observed_at"])) not in {
            (str(o["source_id"]), str(o["observed_at"])) for o in package["observations"]
        }:
            raise ValueError("capture without observation")


def apply(db: Path, package: dict) -> dict:
    validate_package(package)
    existing = existing_days(db)
    observations = [
        o for o in package["observations"] if (str(o["product"]), str(o["observed_at"])) not in existing
    ]
    kept_dates = {str(o["observed_at"]) for o in observations}
    captures = [c for c in package["captures"] if str(c["observed_at"]) in kept_dates]
    object.__setattr__(settings, "sqlite_path", str(db))
    result = import_observations(observations, capture_revisions=captures, apply=True)
    return {
        "observations_imported": result.get("inserted", result),
        "captures_submitted": len(captures),
        "existing_days_untouched": len(existing),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--start", default="2024-12-27")
    parser.add_argument("--end", default="")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    end = args.end or __import__("datetime").date.today().isoformat()
    if args.apply:
        print(json.dumps(apply(args.db, json.loads(args.package.read_text())), ensure_ascii=False))
    else:
        result = asyncio.run(prepare(args.db, args.index, args.package, start=args.start, end=end))
        print(
            json.dumps(
                {
                    "observations": len(result["observations"]),
                    "captures": len(result["captures"]),
                    "attempts": len(result["attempts"]),
                    "failed_attempts": sum(
                        1 for a in result["attempts"] if a.get("status") != "ok"
                    ),
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

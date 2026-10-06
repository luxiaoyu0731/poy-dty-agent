from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from xml.etree import ElementTree

import httpx

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app.models import NewsSource  # noqa: E402
from app.news import RawNewsItem, _canonical_url, _cluster_id, _hash, _id, analyze_news_item  # noqa: E402

DEFAULT_DB = REPO_ROOT / "server" / "data" / "agent.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "server" / "data" / "backfill_reports"
DEFAULT_START = date(2025, 6, 16)
DEFAULT_END = date(2026, 6, 15)
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_SLEEP_SECONDS = 6.8
DEFAULT_MAX_RECORDS = 250
SUMMARY_ONLY_MARKER = (
    "Summary-only public discovery record; GDELT DOC ArtList does not include full article body, "
    "and detail fetching was skipped to avoid login, paywall, CAPTCHA, or license issues."
)
CAPTCHA_MARKERS = (
    "captcha",
    "checking your browser",
    "enable javascript and cookies",
    "unusual traffic",
    "cf-mitigated",
)
TRANSIENT_HTTP_STATUS = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class QueryGroup:
    source_id: str
    source_name: str
    category: str
    query: str


QUERY_GROUPS = (
    QueryGroup(
        source_id="gdelt_v2_oil_policy",
        source_name="GDELT V2 Oil Policy",
        category="oil_policy",
        query='OPEC OR "OPEC+" OR "crude oil" OR Brent OR WTI OR refinery OR "oil inventory"',
    ),
    QueryGroup(
        source_id="gdelt_v2_middle_east_shipping",
        source_name="GDELT V2 Middle East Shipping",
        category="shipping_security",
        query='"Strait of Hormuz" OR Hormuz OR "Red Sea" OR tanker OR maritime OR shipping OR vessel',
    ),
    QueryGroup(
        source_id="gdelt_v2_sanctions",
        source_name="GDELT V2 Sanctions",
        category="sanctions_geopolitics",
        query='OFAC OR sanctions OR "U.S. Treasury" OR "State Department" OR Iran OR Russia',
    ),
    QueryGroup(
        source_id="gdelt_v2_polyester_chain",
        source_name="GDELT V2 Polyester Chain",
        category="company_capacity",
        query=(
            'paraxylene OR PX OR PTA OR "purified terephthalic acid" OR '
            '"monoethylene glycol" OR "ethylene glycol" OR polyester OR POY OR DTY'
        ),
    ),
    QueryGroup(
        source_id="gdelt_v2_china_chemicals",
        source_name="GDELT V2 China Chemicals",
        category="china_policy",
        query='China AND (petrochemical OR polyester OR "chemical fiber" OR PTA OR MEG OR paraxylene OR refinery)',
    ),
    QueryGroup(
        source_id="gdelt_v2_macro_energy",
        source_name="GDELT V2 Macro Energy",
        category="macro_finance",
        query='"US dollar" OR "interest rates" OR Fed OR inflation OR "risk appetite" AND (oil OR commodities)',
    ),
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill public GDELT news for V2 yearly evaluation.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=DEFAULT_END.isoformat())
    parser.add_argument("--target-raw-news", type=int, default=2_000)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    parser.add_argument("--sleep-seconds", type=float, default=DEFAULT_SLEEP_SECONDS)
    parser.add_argument("--window-days", type=int, default=14)
    parser.add_argument("--limit-requests", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--max-backoff-seconds", type=float, default=90.0)
    parser.add_argument("--providers", default="gdelt,google")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    report = run_backfill(
        args.db,
        start=start,
        end=end,
        target_raw_news=args.target_raw_news,
        max_records=args.max_records,
        sleep_seconds=args.sleep_seconds,
        window_days=args.window_days,
        limit_requests=args.limit_requests or None,
        timeout_seconds=args.timeout_seconds,
        attempts=args.attempts,
        max_backoff_seconds=args.max_backoff_seconds,
        providers=parse_providers(args.providers),
        dry_run=args.dry_run,
    )
    suffix = "dry-run" if args.dry_run else "run"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = (
        args.output_dir / f"public-news-backfill-v2-{start.isoformat()}-to-{end.isoformat()}-{suffix}-{stamp}.json"
    )
    write_json(output_path, report)
    print(
        {
            "output": str(output_path),
            "dry_run": args.dry_run,
            "before_raw_news": report["before"]["raw_news"],
            "after_raw_news": report["after"]["raw_news"],
            "new_articles": report["summary"]["new_articles"],
            "requests": report["summary"]["requests_attempted"],
            "errors": report["summary"]["errors"],
        }
    )
    return 0


def run_backfill(
    db_path: Path,
    *,
    start: date,
    end: date,
    target_raw_news: int,
    max_records: int,
    sleep_seconds: float,
    window_days: int,
    limit_requests: int | None,
    timeout_seconds: float,
    attempts: int,
    max_backoff_seconds: float,
    providers: set[str],
    dry_run: bool,
) -> dict[str, Any]:
    started_at = now_iso()
    before = db_counts(db_path, start=start, end=end)
    planned_requests = build_requests(
        start=start,
        end=end,
        window_days=window_days,
        max_records=max_records,
        providers=providers,
    )
    requests_attempted = 0
    errors: list[dict[str, str]] = []
    per_source: Counter[str] = Counter()
    new_articles = 0
    updated_articles = 0
    clusters_upserted = 0
    events_created = 0
    seen_urls, seen_titles = existing_article_keys(db_path)
    batch_items: list[RawNewsItem] = []

    for request_index, (provider, group, window_start, window_end, url) in enumerate(planned_requests, start=1):
        if limit_requests is not None and requests_attempted >= limit_requests:
            break
        current_raw_news = (
            db_counts(db_path, start=start, end=end)["raw_news"]
            if not dry_run
            else before["raw_news"] + len(batch_items)
        )
        if current_raw_news >= target_raw_news and not dry_run:
            break
        if request_index > 1:
            time.sleep(max(sleep_seconds, 0.0))
        requests_attempted += 1
        try:
            payload = fetch_public_payload(
                provider,
                url,
                timeout_seconds=timeout_seconds,
                attempts=attempts,
                max_backoff_seconds=max_backoff_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - report individual source failures.
            errors.append(
                {
                    "provider": provider,
                    "source_id": group.source_id,
                    "window": f"{window_start.isoformat()}..{window_end.isoformat()}",
                    "url": url,
                    "error": f"{exc.__class__.__name__}:{str(exc)[:180]}",
                }
            )
            continue
        items = payload_to_items(provider, payload, group=group, seen_urls=seen_urls, seen_titles=seen_titles)
        per_source[source_id_for_provider(provider, group)] += len(items)
        if dry_run:
            batch_items.extend(items)
            continue
        result = ingest_items_direct(db_path, items, group=group)
        new_articles += int(result["new_articles"])
        updated_articles += int(result["updated_articles"])
        clusters_upserted += int(result["clusters_upserted"])
        events_created += int(result["events_created"])

    after = db_counts(db_path, start=start, end=end)
    return {
        "schema_version": "public_news_backfill_v2.1",
        "started_at": started_at,
        "finished_at": now_iso(),
        "scope": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "target_raw_news": target_raw_news,
            "window_days": window_days,
            "max_records": max_records,
            "providers": sorted(providers),
            "dry_run": dry_run,
        },
        "guardrails": {
            "paid_sources_called": False,
            "login_or_paywall_bypass": False,
            "vpn_or_proxy_changed": False,
            "deepseek_called": False,
            "source": "public news indexes: " + ", ".join(sorted(providers)),
            "rate_limit_sleep_seconds": sleep_seconds,
            "timeout_seconds": timeout_seconds,
            "attempts": attempts,
        },
        "before": before,
        "after": after if not dry_run else {"raw_news": before["raw_news"] + len(batch_items)},
        "summary": {
            "planned_requests": len(planned_requests),
            "requests_attempted": requests_attempted,
            "new_articles": new_articles if not dry_run else len(batch_items),
            "updated_articles": updated_articles,
            "clusters_upserted": clusters_upserted,
            "events_created": events_created,
            "errors": len(errors),
            "per_source_items": dict(per_source),
        },
        "errors": errors[:80],
        "failed_queries": errors[:80],
        "noise_warning": (
            "GDELT is a broad C-tier public news index. It can include duplicate wire copies, headline-only "
            "records, irrelevant broad-query hits, and outlet-specific framing. Treat these rows as raw "
            "discovery evidence and require source review before high-confidence use."
        ),
        "noise_notes": [
            "GDELT C-tier items need RAG/citation filtering before scoring.",
            "This script expands capture coverage only. It does not make prediction directions or call DeepSeek.",
            SUMMARY_ONLY_MARKER,
        ],
    }


def build_requests(
    *,
    start: date,
    end: date,
    window_days: int,
    max_records: int,
    providers: set[str],
) -> list[tuple[str, QueryGroup, date, date, str]]:
    requests: list[tuple[str, QueryGroup, date, date, str]] = []
    current = start
    while current <= end:
        window_end = min(end, current + timedelta(days=max(window_days, 1) - 1))
        for group in QUERY_GROUPS:
            if "gdelt" in providers:
                params = {
                    "query": group.query,
                    "mode": "ArtList",
                    "format": "json",
                    "maxrecords": str(max(1, min(max_records, 250))),
                    "sort": "HybridRel",
                    "startdatetime": f"{compact_date(current)}000000",
                    "enddatetime": f"{compact_date(window_end)}235959",
                }
                requests.append(("gdelt", group, current, window_end, f"{GDELT_URL}?{urlencode(params)}"))
            if "google" in providers:
                before = (window_end + timedelta(days=1)).isoformat()
                params = {
                    "q": f"{group.query} after:{current.isoformat()} before:{before}",
                    "hl": "en-US",
                    "gl": "US",
                    "ceid": "US:en",
                }
                requests.append(
                    ("google", group, current, window_end, f"https://news.google.com/rss/search?{urlencode(params)}")
                )
        current = window_end + timedelta(days=1)
    return requests


def fetch_public_payload(
    provider: str,
    url: str,
    *,
    timeout_seconds: float,
    attempts: int,
    max_backoff_seconds: float,
) -> dict[str, Any] | str:
    if provider == "gdelt":
        return fetch_gdelt_json(
            url,
            timeout_seconds=timeout_seconds,
            attempts=attempts,
            max_backoff_seconds=max_backoff_seconds,
        )
    if provider == "google":
        return fetch_google_rss(
            url,
            timeout_seconds=timeout_seconds,
            attempts=attempts,
            max_backoff_seconds=max_backoff_seconds,
        )
    raise ValueError(f"Unsupported provider: {provider}")


def fetch_google_rss(
    url: str,
    *,
    timeout_seconds: float = 20.0,
    attempts: int = 3,
    max_backoff_seconds: float = 90.0,
) -> str:
    last_error: Exception | None = None
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0))
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for attempt in range(max(1, attempts)):
            if attempt:
                time.sleep(min(max_backoff_seconds, 1.5 * (2**attempt)))
            try:
                response = client.get(url, headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"})
                response.raise_for_status()
                if is_guarded_response(response.text):
                    raise RuntimeError("Google News returned guarded browser/CAPTCHA response")
                return response.text
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code not in TRANSIENT_HTTP_STATUS:
                    break
            except (httpx.TimeoutException, httpx.TransportError, RuntimeError) as exc:
                last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("Google News request failed without exception")


def fetch_gdelt_json(
    url: str,
    *,
    timeout_seconds: float = 20.0,
    attempts: int = 3,
    max_backoff_seconds: float = 90.0,
) -> dict[str, Any]:
    last_error: Exception | None = None
    timeout = httpx.Timeout(timeout_seconds, connect=min(timeout_seconds, 10.0))
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for attempt in range(max(1, attempts)):
            if attempt:
                time.sleep(min(max_backoff_seconds, DEFAULT_SLEEP_SECONDS * (2**attempt)))
            try:
                response = client.get(url, headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"})
                response.raise_for_status()
                if "Please limit requests" in response.text:
                    raise RuntimeError("GDELT rate limited")
                if is_guarded_response(response.text):
                    raise RuntimeError("GDELT returned guarded browser/CAPTCHA response")
                return response.json()
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response.status_code not in TRANSIENT_HTTP_STATUS:
                    break
            except (httpx.TimeoutException, httpx.TransportError, RuntimeError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
    raise RuntimeError("GDELT request failed without exception")


def payload_to_items(
    provider: str,
    payload: dict[str, Any] | str,
    *,
    group: QueryGroup,
    seen_urls: set[str],
    seen_titles: set[str] | None = None,
) -> list[RawNewsItem]:
    if provider == "gdelt" and isinstance(payload, dict):
        return gdelt_articles_to_items(payload, group=group, seen_urls=seen_urls, seen_titles=seen_titles)
    if provider == "google" and isinstance(payload, str):
        return google_rss_to_items(payload, group=group, seen_urls=seen_urls, seen_titles=seen_titles)
    return []


def gdelt_articles_to_items(
    payload: dict[str, Any],
    *,
    group: QueryGroup,
    seen_urls: set[str],
    seen_titles: set[str] | None = None,
) -> list[RawNewsItem]:
    if seen_titles is None:
        seen_titles = set()
    items: list[RawNewsItem] = []
    for article in payload.get("articles", []) or []:
        if not isinstance(article, dict):
            continue
        url = str(article.get("url") or "").strip()
        title = clean_text(str(article.get("title") or ""))
        if not url or not title:
            continue
        canonical = _canonical_url(url)
        title_key = normalize_title(title)
        if canonical in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(canonical)
        seen_titles.add(title_key)
        published_at = normalize_gdelt_date(article.get("seendate"))
        domain = str(article.get("domain") or "")
        language = str(article.get("language") or "unknown") or "unknown"
        raw_text = clean_text(
            " ".join(
                item
                for item in [
                    title,
                    domain,
                    str(article.get("sourcecountry") or ""),
                    str(article.get("socialimage") or ""),
                    SUMMARY_ONLY_MARKER,
                ]
                if item
            )
        )
        items.append(
            RawNewsItem(
                source_id=source_id_for_provider("gdelt", group),
                tier="C",
                url=url,
                title=title,
                published_at=published_at,
                raw_text=raw_text,
                language=language,
            )
        )
    return items


def google_rss_to_items(
    text: str,
    *,
    group: QueryGroup,
    seen_urls: set[str],
    seen_titles: set[str] | None = None,
) -> list[RawNewsItem]:
    if seen_titles is None:
        seen_titles = set()
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except ElementTree.ParseError:
        return []
    items: list[RawNewsItem] = []
    for element in root.findall("./channel/item"):
        title = clean_text(element.findtext("title") or "")
        url = clean_text(element.findtext("link") or "")
        published_at = normalize_rss_date(element.findtext("pubDate") or "")
        description = clean_text(strip_html(element.findtext("description") or ""))
        if not title or not url:
            continue
        canonical = _canonical_url(url)
        title_key = normalize_title(title)
        if canonical in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(canonical)
        seen_titles.add(title_key)
        items.append(
            RawNewsItem(
                source_id=source_id_for_provider("google", group),
                tier="C",
                url=url,
                title=title,
                published_at=published_at,
                raw_text=clean_text(" ".join(item for item in [description or title, SUMMARY_ONLY_MARKER] if item)),
                language="en",
            )
        )
    return items


def ingest_items_direct(db_path: Path, items: list[RawNewsItem], *, group: QueryGroup) -> dict[str, int]:
    if not items:
        return {
            "new_articles": 0,
            "updated_articles": 0,
            "clusters_upserted": 0,
            "events_created": 0,
        }
    source = NewsSource(
        source_id=items[0].source_id,
        source_name=f"{items[0].source_id} public news index",
        tier="C",
        url=GDELT_URL if items[0].source_id.startswith("gdelt") else "https://news.google.com/rss/search",
        category=group.category,
        fetcher="rss",
        cadence="archive",
    )
    now = now_iso()
    new_articles = 0
    updated_articles = 0
    clusters_upserted = 0
    events_created = 0
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        for item in items:
            analysis = analyze_news_item(item, source=source)
            if float(analysis["score"]) < 25:
                continue
            article_id = _id("art", item.url or item.title)
            content_hash = _hash(f"{item.title}\n{item.raw_text}")
            public_index_url = GDELT_URL if item.source_id.startswith("gdelt") else "https://news.google.com/rss/search"
            raw = {
                "source_url": item.url,
                "public_index_url": public_index_url,
                "body_status": "summary_only",
                "body_note": SUMMARY_ONLY_MARKER,
                "analysis": analysis,
                "backfill": "public_news_backfill_v2",
            }
            existing = connection.execute(
                "SELECT article_id FROM news_articles WHERE article_id = ?",
                (article_id,),
            ).fetchone()
            payload = (
                article_id,
                now,
                item.source_id,
                item.tier,
                item.url,
                _canonical_url(item.url),
                item.title[:200],
                item.published_at,
                now,
                content_hash,
                item.language,
                item.raw_text[:5000],
                str(analysis["summary"]),
                float(analysis["score"]),
                str(analysis["category"]),
                json.dumps(raw, ensure_ascii=False),
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO news_articles (
                      article_id, created_at, source_id, tier, url, canonical_url, title, published_at,
                      first_seen_at, content_hash, language, raw_text, summary, score, category, raw
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                new_articles += 1
            else:
                connection.execute(
                    """
                    UPDATE news_articles
                    SET source_id = ?, tier = ?, url = ?, canonical_url = ?, title = ?, published_at = ?,
                        content_hash = ?, language = ?, raw_text = ?, summary = ?, score = ?, category = ?, raw = ?
                    WHERE article_id = ?
                    """,
                    (
                        item.source_id,
                        item.tier,
                        item.url,
                        _canonical_url(item.url),
                        item.title[:200],
                        item.published_at,
                        content_hash,
                        item.language,
                        item.raw_text[:5000],
                        str(analysis["summary"]),
                        float(analysis["score"]),
                        str(analysis["category"]),
                        json.dumps(raw, ensure_ascii=False),
                        article_id,
                    ),
                )
                updated_articles += 1

            cluster_id = _cluster_id(
                str(analysis["category"]),
                item.title,
                published_at=item.published_at,
                affected_products=[str(value) for value in analysis["affected_products"]],
                matched_keywords=[str(value) for value in analysis["matched_keywords"]],
            )
            cluster_payload = {
                "news_article_id": article_id,
                "keywords": analysis["matched_keywords"],
                "promotion_blocked_reason": "gdelt_c_tier_requires_structuring_review",
            }
            connection.execute(
                """
                INSERT INTO news_event_clusters (
                  cluster_id, created_at, updated_at, title, category, source_ids, article_ids, heat_score,
                  evidence_level, affected_products, direction, impact_strength, summary, status, event_record_id, raw
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cluster_id) DO UPDATE SET
                  updated_at = excluded.updated_at,
                  title = excluded.title,
                  category = excluded.category,
                  source_ids = excluded.source_ids,
                  article_ids = excluded.article_ids,
                  heat_score = excluded.heat_score,
                  evidence_level = excluded.evidence_level,
                  affected_products = excluded.affected_products,
                  direction = excluded.direction,
                  impact_strength = excluded.impact_strength,
                  summary = excluded.summary,
                  status = excluded.status,
                  raw = excluded.raw
                """,
                (
                    cluster_id,
                    now,
                    now,
                    item.title[:200],
                    str(analysis["category"]),
                    json.dumps([item.source_id], ensure_ascii=False),
                    json.dumps([article_id], ensure_ascii=False),
                    float(analysis["score"]),
                    item.tier,
                    json.dumps(analysis["affected_products"], ensure_ascii=False),
                    str(analysis["direction"]),
                    f"{float(analysis['impact_strength']):.2f}",
                    str(analysis["summary"]),
                    "candidate",
                    None,
                    json.dumps(cluster_payload, ensure_ascii=False),
                ),
            )
            clusters_upserted += 1
        connection.commit()
    return {
        "new_articles": new_articles,
        "updated_articles": updated_articles,
        "clusters_upserted": clusters_upserted,
        "events_created": events_created,
    }


def db_counts(db_path: Path, *, start: date, end: date) -> dict[str, Any]:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT source_id, tier, published_at, first_seen_at FROM news_articles").fetchall()
    window_rows = [row for row in rows if row_in_window(row, start=start, end=end)]
    by_source = Counter(str(row["source_id"] or "unknown") for row in window_rows)
    by_tier = Counter(str(row["tier"] or "unknown") for row in window_rows)
    all_published = [parse_date(row["published_at"]) for row in rows]
    all_published = [value for value in all_published if value is not None]
    window_published = [parse_date(row["published_at"]) for row in window_rows]
    window_published = [value for value in window_published if value is not None]
    return {
        "raw_news": len(window_rows),
        "total_news_articles": len(rows),
        "by_source": dict(sorted(by_source.items(), key=lambda item: (-item[1], item[0]))),
        "by_tier": dict(sorted(by_tier.items())),
        "time_coverage": {
            "min_published_at": min(all_published).isoformat() if all_published else "",
            "max_published_at": max(all_published).isoformat() if all_published else "",
            "window_min_published_at": min(window_published).isoformat() if window_published else "",
            "window_max_published_at": max(window_published).isoformat() if window_published else "",
        },
        "date_quality": {
            "missing_published_at": sum(1 for row in rows if not str(row["published_at"] or "").strip()),
            "unparseable_published_at": sum(
                1 for row in rows if str(row["published_at"] or "").strip() and parse_date(row["published_at"]) is None
            ),
        },
    }


def row_in_window(row: sqlite3.Row, *, start: date, end: date) -> bool:
    observed = parse_date(row["published_at"]) or parse_date(row["first_seen_at"])
    return bool(observed and start <= observed <= end)


def existing_article_keys(db_path: Path) -> tuple[set[str], set[str]]:
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as connection, connection:
        rows = connection.execute("SELECT canonical_url, url, title FROM news_articles").fetchall()
    urls: set[str] = set()
    titles: set[str] = set()
    for canonical_url, url, title in rows:
        for value in (canonical_url, url):
            key = _canonical_url(str(value or ""))
            if key:
                urls.add(key)
        title_key = normalize_title(str(title or ""))
        if title_key:
            titles.add(title_key)
    return urls, titles


def normalize_gdelt_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC).isoformat()
        except ValueError:
            pass
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return datetime.strptime(text[:8], "%Y%m%d").replace(tzinfo=UTC).isoformat()
        except ValueError:
            return text
    return text


def normalize_rss_date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    parsed = parse_datetime(text)
    return parsed.isoformat() if parsed else text


def parse_date(value: Any) -> date | None:
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            try:
                parsed = datetime.fromisoformat(text[:10])
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def compact_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def normalize_title(value: str) -> str:
    text = clean_text(value).lower()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())


def strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def source_id_for_provider(provider: str, group: QueryGroup) -> str:
    if provider == "google":
        return group.source_id.replace("gdelt_v2_", "google_news_v2_")
    return group.source_id


def parse_providers(value: str) -> set[str]:
    providers = {item.strip().lower() for item in value.split(",") if item.strip()}
    invalid = providers - {"google", "gdelt"}
    if invalid:
        raise ValueError(f"Unsupported providers: {', '.join(sorted(invalid))}")
    return providers or {"gdelt"}


def is_guarded_response(value: str) -> bool:
    lower = value[:5000].lower()
    return any(marker in lower for marker in CAPTCHA_MARKERS)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

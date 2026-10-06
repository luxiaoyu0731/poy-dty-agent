from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

DEFAULT_DB = Path("server/data/agent.db")
DEFAULT_OUTPUT_DIR = Path("server/data/backfill_reports")
DEFAULT_START = date(2025, 6, 16)
DEFAULT_END = date(2026, 6, 15)
DEFAULT_WINDOW = "half_month"
DEFAULT_PRICE_THRESHOLD_PCT = 3.0
DEFAULT_INDUSTRY_THRESHOLD_PCT = 2.0
DEFAULT_INTRADAY_THRESHOLD_PCT = 1.0

CRUDE_SERIES_LABELS = {
    "RWTC": "WTI",
    "DCOILWTICO": "WTI",
    "RBRTE": "Brent",
    "DCOILBRENTEU": "Brent",
}

SOURCE_SCORE = {"A": 90.0, "B": 74.0, "C": 55.0, "D": 38.0}
IMPACT_SCORE = {"high": 85.0, "medium": 65.0, "low": 45.0}

MACRO_KEYWORDS = (
    "dollar",
    "dxy",
    "exchange",
    "fed",
    "federal funds",
    "financial",
    "inflation",
    "interest",
    "macro",
    "rate",
    "sofr",
    "treasury",
    "usd",
    "yield",
    "cpi",
    "ppi",
    "unemployment",
    "jobs",
)

MACRO_SERIES_PREFIXES = ("DGS", "DTWEX", "FEDFUNDS", "SOFR", "EFFR", "CPI", "PPI", "UNRATE")

PRODUCT_KEYWORDS = {
    "crude_oil": ("brent", "wti", "crude", "oil", "opec", "eia", "tanker", "hormuz"),
    "naphtha": ("naphtha",),
    "PX": ("px", "paraxylene"),
    "PTA": ("pta",),
    "MEG": ("meg", "ethylene glycol"),
    "POY": ("poy", "polyester filament"),
    "DTY": ("dty",),
}

CATEGORY_KEYWORDS = {
    "sanctions_geopolitics": ("ofac", "sanction", "treasury", "war", "iran", "israel", "hormuz", "red sea"),
    "shipping_security": ("shipping", "tanker", "maritime", "vessel", "port", "hormuz", "red sea"),
    "oil_policy": ("opec", "eia", "iea", "output", "production", "quota", "crude"),
    "company_capacity": ("outage", "restart", "maintenance", "capacity", "plant", "refinery"),
    "macro_finance": MACRO_KEYWORDS,
    "market_signal": ("price", "quote", "settlement", "inventory", "stock"),
}


@dataclass(frozen=True)
class DateWindow:
    start: date
    end: date


def generate_event_candidates(
    db_path: str | Path,
    *,
    start: date = DEFAULT_START,
    end: date = DEFAULT_END,
    window: str = DEFAULT_WINDOW,
    limit: int | None = None,
    include_preclustered_news: bool = True,
    price_threshold_pct: float = DEFAULT_PRICE_THRESHOLD_PCT,
    industry_threshold_pct: float = DEFAULT_INDUSTRY_THRESHOLD_PCT,
    intraday_threshold_pct: float = DEFAULT_INTRADAY_THRESHOLD_PCT,
) -> dict[str, Any]:
    start_dt = start_of_day(start)
    end_dt = end_of_day(end)
    lookback_start = start - timedelta(days=max(window_lookback_days(window), 2))

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.row_factory = sqlite3.Row
        article_index = load_article_index(connection)
        news_articles = load_news_article_candidates(
            connection,
            start_dt=start_dt,
            end_dt=end_dt,
            skip_clustered_articles=include_preclustered_news,
        )
        news_clusters = (
            load_news_cluster_candidates(connection, article_index=article_index, start_dt=start_dt, end_dt=end_dt)
            if include_preclustered_news
            else []
        )
        event_observations = load_event_observation_candidates(connection, start_dt=start_dt, end_dt=end_dt)
        market_rows = load_market_rows(connection, start=start_dt.date() - timedelta(days=35), end=end)
        industry_rows = load_industry_rows(connection, start=lookback_start, end=end)
        intraday_rows = load_intraday_rows(connection, start=start, end=end)

    candidates: list[dict[str, Any]] = []
    candidates.extend(news_articles)
    candidates.extend(news_clusters)
    candidates.extend(event_observations)
    candidates.extend(price_move_candidates(market_rows, start=start, end=end, threshold_pct=price_threshold_pct))
    candidates.extend(industry_observation_candidates(industry_rows, start=start, end=end))
    candidates.extend(
        industry_price_move_candidates(industry_rows, start=start, end=end, threshold_pct=industry_threshold_pct)
    )
    candidates.extend(macro_finance_candidates(market_rows, start=start, end=end))
    candidates.extend(
        intraday_price_move_candidates(intraday_rows, start=start, end=end, threshold_pct=intraday_threshold_pct)
    )
    candidates = dedupe_candidates(candidates)
    candidates = sort_candidates(candidates)
    if limit is not None:
        candidates = candidates[: max(limit, 0)]

    return {
        "scope": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "window": window,
            "limit": limit,
            "price_threshold_pct": price_threshold_pct,
            "industry_threshold_pct": industry_threshold_pct,
            "intraday_threshold_pct": intraday_threshold_pct,
            "include_preclustered_news": include_preclustered_news,
        },
        "guardrails": {
            "provider_calls": 0,
            "remote_fetches": 0,
            "uses_paid_or_login_sources": False,
            "fake_data_policy": "no synthetic candidate rows are created when source rows are absent",
        },
        "input_counts": {
            "news_article_candidates": len(news_articles),
            "news_cluster_candidates": len(news_clusters),
            "event_observation_candidates": len(event_observations),
            "market_rows_loaded": len(market_rows),
            "industry_rows_loaded": len(industry_rows),
            "intraday_rows_loaded": len(intraday_rows),
        },
        "candidate_counts": dict(Counter(item["candidate_type"] for item in candidates)),
        "source_kind_counts": dict(Counter(item["source_kind"] for item in candidates)),
        "candidates": candidates,
    }


def load_news_article_candidates(
    connection: sqlite3.Connection,
    *,
    start_dt: datetime,
    end_dt: datetime,
    skip_clustered_articles: bool = True,
) -> list[dict[str, Any]]:
    if not table_exists(connection, "news_articles"):
        return []
    rows = connection.execute("SELECT * FROM news_articles").fetchall()
    candidates = []
    clustered_article_ids = article_ids_in_clusters(connection) if skip_clustered_articles else set()
    for row in rows:
        observed_at = first_datetime(row["published_at"], row["first_seen_at"], row["created_at"])
        if observed_at is None or observed_at < start_dt or observed_at > end_dt:
            continue
        article_id = str(row["article_id"])
        if article_id in clustered_article_ids:
            continue
        raw = parse_json_object(row["raw"])
        title = str(row["title"] or article_id)
        summary = str(row["summary"] or raw.get("summary") or "")
        category = str(row["category"] or classify_category(f"{title} {summary}"))
        tier = str(row["tier"] or "C")
        score = to_float(row["score"])
        candidates.append(
            build_candidate(
                candidate_id=f"cand_news_article_{safe_id(article_id)}",
                candidate_type="news_announcement",
                source_kind="news_article",
                source_record_id=article_id,
                as_of_time=observed_at,
                title=title,
                category=category,
                summary=summary,
                source_ids=[str(row["source_id"] or "")],
                cited_doc_ids=[f"news_article:{article_id}"],
                evidence_level=tier,
                affected_products=affected_products_from_text(f"{title} {summary} {raw}"),
                direction_hint=str(raw.get("direction") or "unknown"),
                heat_score=score if score is not None else SOURCE_SCORE.get(tier, 50.0),
                signals={"article_score": score, "language": row["language"], "url": row["url"]},
                requires_human_review=tier not in {"A", "B"} or not str(row["url"] or "").strip(),
            )
        )
    return candidates


def load_news_cluster_candidates(
    connection: sqlite3.Connection,
    *,
    article_index: dict[str, dict[str, Any]],
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict[str, Any]]:
    if not table_exists(connection, "news_event_clusters"):
        return []
    rows = connection.execute("SELECT * FROM news_event_clusters").fetchall()
    candidates = []
    for row in rows:
        cluster_id = str(row["cluster_id"])
        article_ids = [str(item) for item in parse_json_list(row["article_ids"])]
        article_times = [
            item["observed_at"]
            for article_id in article_ids
            if (item := article_index.get(article_id)) and item.get("observed_at") is not None
        ]
        observed_at = min(article_times) if article_times else first_datetime(row["created_at"], row["updated_at"])
        if observed_at is None or observed_at < start_dt or observed_at > end_dt:
            continue
        title = str(row["title"] or cluster_id)
        summary = str(row["summary"] or "")
        source_ids = [str(item) for item in parse_json_list(row["source_ids"])]
        evidence_level = str(row["evidence_level"] or "C")
        candidates.append(
            build_candidate(
                candidate_id=f"cand_news_cluster_{safe_id(cluster_id)}",
                candidate_type="news_announcement",
                source_kind="news_event_cluster",
                source_record_id=cluster_id,
                as_of_time=observed_at,
                title=title,
                category=str(row["category"] or classify_category(f"{title} {summary}")),
                summary=summary,
                source_ids=source_ids,
                cited_doc_ids=[f"news_event:{cluster_id}", *[f"news_article:{item}" for item in article_ids]],
                evidence_level=evidence_level,
                affected_products=parse_json_list(row["affected_products"])
                or affected_products_from_text(f"{title} {summary}"),
                direction_hint=str(row["direction"] or "unknown"),
                heat_score=to_float(row["heat_score"]) or SOURCE_SCORE.get(evidence_level, 50.0),
                signals={
                    "status": row["status"],
                    "article_ids": article_ids,
                    "event_record_id": row["event_record_id"],
                    "impact_strength": row["impact_strength"],
                },
                requires_human_review=evidence_level not in {"A", "B"} or str(row["status"] or "") != "featured",
            )
        )
    return candidates


def load_event_observation_candidates(
    connection: sqlite3.Connection,
    *,
    start_dt: datetime,
    end_dt: datetime,
) -> list[dict[str, Any]]:
    if not table_exists(connection, "event_observations"):
        return []
    rows = connection.execute("SELECT * FROM event_observations").fetchall()
    candidates = []
    for row in rows:
        observed_at = parse_datetime(row["occurred_at"])
        if observed_at is None or observed_at < start_dt or observed_at > end_dt:
            continue
        event_id = str(row["event_record_id"])
        evidence_level = str(row["evidence_level"] or "C")
        impact_strength = str(row["impact_strength"] or "").lower()
        title = str(row["title"] or event_id)
        summary = str(row["summary"] or "")
        candidates.append(
            build_candidate(
                candidate_id=f"cand_event_observation_{safe_id(event_id)}",
                candidate_type="news_announcement",
                source_kind="event_observation",
                source_record_id=event_id,
                as_of_time=observed_at,
                title=title,
                category=str(row["event_type"] or classify_category(f"{title} {summary}")),
                summary=summary,
                source_ids=[str(row["source_id"] or "")],
                cited_doc_ids=[f"event:{event_id}"],
                evidence_level=evidence_level,
                affected_products=parse_json_list(row["affected_products"])
                or affected_products_from_text(f"{title} {summary}"),
                direction_hint=str(row["direction"] or "unknown"),
                heat_score=IMPACT_SCORE.get(impact_strength, SOURCE_SCORE.get(evidence_level, 55.0)),
                signals={"impact_strength": row["impact_strength"], "evidence_url": row["evidence_url"]},
                requires_human_review=bool(row["requires_human_review"]),
            )
        )
    return candidates


def price_move_candidates(
    rows: list[dict[str, Any]],
    *,
    start: date,
    end: date,
    threshold_pct: float,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if is_macro_finance_row(row):
            continue
        label = market_label(row)
        if not label:
            continue
        grouped[label].append(row)

    candidates = []
    for label, series in grouped.items():
        ordered = sorted(series, key=lambda item: (item["observed_at_dt"], item["observation_id"]))
        previous: dict[str, Any] | None = None
        for row in ordered:
            observed_date = row["observed_at_dt"].date()
            if previous is not None and start <= observed_date <= end:
                previous_value = to_float(previous["value"])
                current_value = to_float(row["value"])
                if previous_value not in {None, 0.0} and current_value is not None:
                    change_pct = ((current_value - previous_value) / abs(previous_value)) * 100
                    if abs(change_pct) >= threshold_pct:
                        candidates.append(
                            build_price_move_candidate(
                                row,
                                label=label,
                                previous=previous,
                                change_pct=change_pct,
                                source_kind="market_observation",
                                candidate_id_prefix="cand_price_move",
                                cited_doc_prefix="market",
                            )
                        )
            previous = row
    return candidates


def industry_observation_candidates(rows: list[dict[str, Any]], *, start: date, end: date) -> list[dict[str, Any]]:
    candidates = []
    for row in rows:
        observed_date = row["observed_at_dt"].date()
        if observed_date < start or observed_date > end:
            continue
        product = str(row["product"] or "").upper()
        metric = str(row["metric"] or "")
        notes = str(row["notes"] or "")
        if metric == "spot_quote" and not notes:
            continue
        observation_id = str(row["observation_id"])
        evidence_level = str(row["evidence_level"] or "D")
        title = f"{product} {metric} observation on {observed_date.isoformat()}".strip()
        summary = notes or f"{product} {metric} {row['value']} {row['unit']}".strip()
        candidates.append(
            build_candidate(
                candidate_id=f"cand_industry_observation_{safe_id(observation_id)}",
                candidate_type="industry_observation",
                source_kind="industry_observation",
                source_record_id=observation_id,
                as_of_time=row["observed_at_dt"],
                title=title,
                category=classify_industry_category(metric, notes),
                summary=summary,
                source_ids=[str(row["source_id"] or "")],
                cited_doc_ids=[f"industry:{observation_id}"],
                evidence_level=evidence_level,
                affected_products=[product] if product else affected_products_from_text(summary),
                direction_hint="unknown",
                heat_score=SOURCE_SCORE.get(evidence_level, 38.0),
                signals={
                    "product": product,
                    "metric": metric,
                    "market": row["market"],
                    "value": row["value"],
                    "unit": row["unit"],
                },
                requires_human_review=evidence_level not in {"A", "B"},
            )
        )
    return candidates


def industry_price_move_candidates(
    rows: list[dict[str, Any]],
    *,
    start: date,
    end: date,
    threshold_pct: float,
) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if str(row["metric"] or "") != "spot_quote":
            continue
        key = (str(row["product"] or "").upper(), str(row["metric"] or ""), str(row["market"] or ""))
        grouped[key].append(row)

    candidates = []
    for (product, _metric, market), series in grouped.items():
        ordered = sorted(series, key=lambda item: (item["observed_at_dt"], item["observation_id"]))
        previous: dict[str, Any] | None = None
        for row in ordered:
            observed_date = row["observed_at_dt"].date()
            if previous is not None and start <= observed_date <= end:
                previous_value = to_float(previous["value"])
                current_value = to_float(row["value"])
                if previous_value not in {None, 0.0} and current_value is not None:
                    change_pct = ((current_value - previous_value) / abs(previous_value)) * 100
                    if abs(change_pct) >= threshold_pct:
                        candidates.append(
                            build_price_move_candidate(
                                row,
                                label=f"{product} {market}".strip(),
                                previous=previous,
                                change_pct=change_pct,
                                source_kind="industry_observation",
                                candidate_id_prefix="cand_industry_price_move",
                                cited_doc_prefix="industry",
                            )
                        )
            previous = row
    return candidates


def macro_finance_candidates(rows: list[dict[str, Any]], *, start: date, end: date) -> list[dict[str, Any]]:
    candidates = []
    for row in rows:
        observed_date = row["observed_at_dt"].date()
        if observed_date < start or observed_date > end or not is_macro_finance_row(row):
            continue
        observation_id = str(row["observation_id"])
        indicator = str(row["indicator"] or row["product"] or "macro")
        title = f"{indicator} macro-finance observation on {observed_date.isoformat()}"
        candidates.append(
            build_candidate(
                candidate_id=f"cand_macro_finance_{safe_id(observation_id)}",
                candidate_type="macro_finance",
                source_kind="market_observation",
                source_record_id=observation_id,
                as_of_time=row["observed_at_dt"],
                title=title,
                category="macro_finance",
                summary=str(row["notes"] or f"{indicator}: {row['value']} {row['unit']}"),
                source_ids=[str(row["source_id"] or "")],
                cited_doc_ids=[f"market:{observation_id}"],
                evidence_level="B" if str(row["source_id"] or "").lower().startswith("fred") else "C",
                affected_products=["crude_oil", "POY", "DTY"],
                direction_hint="macro_signal",
                heat_score=62.0,
                signals={
                    "indicator": row["indicator"],
                    "product": row["product"],
                    "value": row["value"],
                    "unit": row["unit"],
                    "region": row["region"],
                },
                requires_human_review=False,
            )
        )
    return candidates


def intraday_price_move_candidates(
    rows: list[dict[str, Any]],
    *,
    start: date,
    end: date,
    threshold_pct: float,
) -> list[dict[str, Any]]:
    candidates = []
    for row in rows:
        observed_date = row["observed_at_dt"].date()
        if observed_date < start or observed_date > end:
            continue
        move_pct = intraday_move_pct(row)
        if move_pct is None or abs(move_pct) < threshold_pct:
            continue
        observation_id = str(row["observation_id"])
        instrument = str(row["instrument"] or row["symbol"] or "intraday")
        direction = "up" if move_pct > 0 else "down"
        candidates.append(
            build_candidate(
                candidate_id=(
                    f"cand_intraday_price_move_{safe_id(instrument)}_"
                    f"{observed_date.isoformat()}_{safe_id(observation_id)}"
                ),
                candidate_type="price_move",
                source_kind="intraday_price_observation",
                source_record_id=observation_id,
                as_of_time=row["observed_at_dt"],
                title=f"{instrument} intraday {direction} {abs(move_pct):.2f}% on {observed_date.isoformat()}",
                category="market_signal",
                summary=intraday_summary(row, move_pct),
                source_ids=[str(row["source_id"] or "")],
                cited_doc_ids=[f"intraday:{observation_id}"],
                evidence_level="C",
                affected_products=affected_products_from_text(instrument),
                direction_hint=direction,
                heat_score=min(100.0, 50.0 + abs(move_pct) * 8),
                signals={
                    "instrument": row["instrument"],
                    "symbol": row["symbol"],
                    "price_type": row["price_type"],
                    "last": row["last"],
                    "open_value": row["open_value"],
                    "high_value": row["high_value"],
                    "low_value": row["low_value"],
                    "change_pct": round(move_pct, 4),
                    "unit": row["unit"],
                    "quality": row["quality"],
                    "source_url": row["source_url"],
                },
                requires_human_review=True,
            )
        )
    return candidates


def intraday_move_pct(row: dict[str, Any]) -> float | None:
    explicit_change = to_float(row.get("change_pct"))
    if explicit_change is not None and explicit_change != 0.0:
        return explicit_change
    open_value = to_float(row.get("open_value"))
    last = to_float(row.get("last"))
    if open_value not in {None, 0.0} and last is not None:
        return ((last - open_value) / abs(open_value)) * 100
    high = to_float(row.get("high_value"))
    low = to_float(row.get("low_value"))
    if low not in {None, 0.0} and high is not None:
        return ((high - low) / abs(low)) * 100
    return None


def intraday_summary(row: dict[str, Any], move_pct: float) -> str:
    return (
        f"{row['instrument']} {row['symbol']} intraday observation moved {move_pct:.2f}% "
        f"with last={row['last']}, open={row['open_value']}, high={row['high_value']}, low={row['low_value']}."
    )


def build_price_move_candidate(
    row: dict[str, Any],
    *,
    label: str,
    previous: dict[str, Any],
    change_pct: float,
    source_kind: str,
    candidate_id_prefix: str,
    cited_doc_prefix: str,
) -> dict[str, Any]:
    observation_id = str(row["observation_id"])
    direction = "up" if change_pct > 0 else "down"
    observed_date = row["observed_at_dt"].date().isoformat()
    return build_candidate(
        candidate_id=f"{candidate_id_prefix}_{safe_id(label)}_{observed_date}_{safe_id(observation_id)}",
        candidate_type="price_move",
        source_kind=source_kind,
        source_record_id=observation_id,
        as_of_time=row["observed_at_dt"],
        title=f"{label} {direction} {abs(change_pct):.2f}% on {observed_date}",
        category="market_signal",
        summary=f"{label} moved from {previous['value']} to {row['value']}.",
        source_ids=[str(row["source_id"] or "")],
        cited_doc_ids=[f"{cited_doc_prefix}:{previous['observation_id']}", f"{cited_doc_prefix}:{observation_id}"],
        evidence_level="C",
        affected_products=affected_products_from_text(label),
        direction_hint=direction,
        heat_score=min(100.0, 45.0 + abs(change_pct) * 5),
        signals={
            "label": label,
            "previous_observation_id": previous["observation_id"],
            "previous_observed_at": previous["observed_at"],
            "previous_value": previous["value"],
            "current_value": row["value"],
            "change_pct": round(change_pct, 4),
        },
        requires_human_review=True,
    )


def build_candidate(
    *,
    candidate_id: str,
    candidate_type: str,
    source_kind: str,
    source_record_id: str,
    as_of_time: datetime,
    title: str,
    category: str,
    summary: str,
    source_ids: list[str],
    cited_doc_ids: list[str],
    evidence_level: str,
    affected_products: list[Any],
    direction_hint: str,
    heat_score: float,
    signals: dict[str, Any],
    requires_human_review: bool,
) -> dict[str, Any]:
    cleaned_products = sorted({str(item) for item in affected_products if str(item).strip()})
    cleaned_sources = sorted({str(item) for item in source_ids if str(item).strip()})
    cleaned_citations = sorted({str(item) for item in cited_doc_ids if str(item).strip()})
    return {
        "candidate_id": candidate_id,
        "candidate_type": candidate_type,
        "source_kind": source_kind,
        "source_record_id": source_record_id,
        "as_of_time": as_of_time.isoformat(),
        "title": title,
        "category": category or "general",
        "summary": summary,
        "source_ids": cleaned_sources,
        "cited_doc_ids": cleaned_citations,
        "evidence_level": evidence_level or "C",
        "affected_products": cleaned_products,
        "direction_hint": direction_hint or "unknown",
        "heat_score": round(float(heat_score), 4),
        "signals": signals,
        "requires_human_review": requires_human_review,
    }


def load_article_index(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if not table_exists(connection, "news_articles"):
        return {}
    rows = connection.execute("SELECT * FROM news_articles").fetchall()
    index = {}
    for row in rows:
        observed_at = first_datetime(row["published_at"], row["first_seen_at"], row["created_at"])
        index[str(row["article_id"])] = {
            "observed_at": observed_at,
            "source_id": row["source_id"],
            "tier": row["tier"],
            "title": row["title"],
        }
    return index


def article_ids_in_clusters(connection: sqlite3.Connection) -> set[str]:
    if not table_exists(connection, "news_event_clusters"):
        return set()
    article_ids: set[str] = set()
    rows = connection.execute("SELECT article_ids FROM news_event_clusters").fetchall()
    for row in rows:
        article_ids.update(str(item) for item in parse_json_list(row["article_ids"]))
    return article_ids


def load_market_rows(connection: sqlite3.Connection, *, start: date, end: date) -> list[dict[str, Any]]:
    if not table_exists(connection, "market_observations"):
        return []
    rows = connection.execute("SELECT * FROM market_observations WHERE value IS NOT NULL").fetchall()
    result = []
    for row in rows:
        observed_at = parse_datetime(row["observed_at"])
        if observed_at is None or observed_at.date() < start or observed_at.date() > end:
            continue
        item = dict(row)
        item["observed_at_dt"] = observed_at
        item["raw"] = parse_json_object(item.get("raw"))
        result.append(item)
    return result


def load_industry_rows(connection: sqlite3.Connection, *, start: date, end: date) -> list[dict[str, Any]]:
    if not table_exists(connection, "industry_observations"):
        return []
    rows = connection.execute("SELECT * FROM industry_observations").fetchall()
    result = []
    for row in rows:
        observed_at = parse_datetime(row["observed_at"])
        if observed_at is None or observed_at.date() < start or observed_at.date() > end:
            continue
        item = dict(row)
        item["observed_at_dt"] = observed_at
        item["raw"] = parse_json_object(item.get("raw"))
        result.append(item)
    return result


def load_intraday_rows(connection: sqlite3.Connection, *, start: date, end: date) -> list[dict[str, Any]]:
    if not table_exists(connection, "intraday_price_observations"):
        return []
    rows = connection.execute("SELECT * FROM intraday_price_observations").fetchall()
    result = []
    for row in rows:
        observed_at = parse_datetime(row["observed_at"])
        if observed_at is None or observed_at.date() < start or observed_at.date() > end:
            continue
        item = dict(row)
        item["observed_at_dt"] = observed_at
        item["raw"] = parse_json_object(item.get("raw"))
        result.append(item)
    return result


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in candidates:
        by_id[item["candidate_id"]] = item
    return list(by_id.values())


def sort_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        candidates,
        key=lambda item: (
            str(item.get("as_of_time") or ""),
            -float(item.get("heat_score") or 0),
            str(item.get("candidate_type") or ""),
            str(item.get("candidate_id") or ""),
        ),
    )


def build_windows(start: date, end: date, window: str) -> list[DateWindow]:
    normalized = window.strip().lower().replace("-", "_")
    windows: list[DateWindow] = []
    cursor = start
    while cursor <= end:
        if normalized in {"day", "daily", "1d"}:
            window_end = cursor
        elif normalized in {"week", "weekly", "7d"}:
            window_end = min(cursor + timedelta(days=6), end)
        elif normalized in {"month", "monthly"}:
            next_month = date(cursor.year + int(cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1)
            window_end = min(next_month - timedelta(days=1), end)
        elif normalized in {"half_month", "halfmonth", "semi_month"}:
            if cursor.day <= 15:
                window_end = min(date(cursor.year, cursor.month, 15), end)
            else:
                next_month = date(
                    cursor.year + int(cursor.month == 12),
                    1 if cursor.month == 12 else cursor.month + 1,
                    1,
                )
                window_end = min(next_month - timedelta(days=1), end)
        elif normalized.isdigit() and int(normalized) > 0:
            window_end = min(cursor + timedelta(days=int(normalized) - 1), end)
        else:
            raise ValueError("window must be day, week, half_month, month, or a positive day count")
        windows.append(DateWindow(cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def window_lookback_days(window: str) -> int:
    normalized = window.strip().lower().replace("-", "_")
    if normalized in {"day", "daily", "1d"}:
        return 1
    if normalized in {"week", "weekly", "7d"}:
        return 7
    if normalized in {"month", "monthly"}:
        return 31
    if normalized in {"half_month", "halfmonth", "semi_month"}:
        return 15
    if normalized.isdigit() and int(normalized) > 0:
        return int(normalized)
    return 15


def market_label(row: dict[str, Any]) -> str:
    raw = parse_json_object(row.get("raw"))
    series = str(raw.get("series_id") or raw.get("series") or "")
    if series in CRUDE_SERIES_LABELS:
        return CRUDE_SERIES_LABELS[series]
    text = f"{row.get('indicator', '')} {row.get('product', '')}".lower()
    if "brent" in text:
        return "Brent"
    if "wti" in text or "cushing" in text:
        return "WTI"
    if "crude" in text or "oil" in text:
        return "crude_oil"
    if "naphtha" in text:
        return "naphtha"
    return ""


def is_macro_finance_row(row: dict[str, Any]) -> bool:
    raw = parse_json_object(row.get("raw"))
    series = str(raw.get("series_id") or raw.get("series") or "").upper()
    if any(series.startswith(prefix) for prefix in MACRO_SERIES_PREFIXES):
        return True
    text = f"{row.get('source_id', '')} {row.get('indicator', '')} {row.get('product', '')} {row.get('notes', '')}"
    lowered = text.lower()
    return any(keyword in lowered for keyword in MACRO_KEYWORDS)


def affected_products_from_text(value: Any) -> list[str]:
    text = str(value).lower()
    products = []
    for product, keywords in PRODUCT_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            products.append(product)
    return products or ["crude_oil", "POY", "DTY"]


def classify_category(value: str, default: str = "general") -> str:
    text = value.lower()
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return category
    return default


def classify_industry_category(metric: str, notes: str) -> str:
    text = f"{metric} {notes}".lower()
    if any(keyword in text for keyword in ("outage", "restart", "maintenance", "capacity", "operating_rate")):
        return "company_capacity"
    return classify_category(text, default="market_signal")


def count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(Counter(str(item.get(key) or "unknown") for item in items))


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def first_datetime(*values: Any) -> datetime | None:
    for value in values:
        parsed = parse_datetime(value)
        if parsed is not None:
            return parsed
    return None


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return datetime.combine(date.fromisoformat(text), time.min, tzinfo=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def start_of_day(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def end_of_day(value: date) -> datetime:
    return datetime.combine(value, time.max, tzinfo=UTC)


def parse_json_object(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in {None, ""}:
        return {}
    try:
        return json.loads(str(value))
    except json.JSONDecodeError:
        return {}


def parse_json_list(value: Any) -> list[Any]:
    parsed = parse_json_object(value)
    if isinstance(parsed, list):
        return parsed
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_id(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "_", str(value).strip()).strip("_").lower()
    if text:
        return text[:80]
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:16]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate V2 local event candidates from stored observations.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--start", default=DEFAULT_START.isoformat())
    parser.add_argument("--end", default=DEFAULT_END.isoformat())
    parser.add_argument("--window", default=DEFAULT_WINDOW)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--price-threshold-pct", type=float, default=DEFAULT_PRICE_THRESHOLD_PCT)
    parser.add_argument("--industry-threshold-pct", type=float, default=DEFAULT_INDUSTRY_THRESHOLD_PCT)
    parser.add_argument("--intraday-threshold-pct", type=float, default=DEFAULT_INTRADAY_THRESHOLD_PCT)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
        report = generate_event_candidates(
            args.db,
            start=start,
            end=end,
            window=args.window,
            limit=args.limit,
            price_threshold_pct=args.price_threshold_pct,
            industry_threshold_pct=args.industry_threshold_pct,
            intraday_threshold_pct=args.intraday_threshold_pct,
        )
        output = args.output_dir / f"event-candidates-{start.isoformat()}-to-{end.isoformat()}.json"
        summary = {
            "dry_run": args.dry_run,
            "output": str(output),
            "candidate_counts": report["candidate_counts"],
            "total_candidates": len(report["candidates"]),
            "provider_calls": report["guardrails"]["provider_calls"],
            "remote_fetches": report["guardrails"]["remote_fetches"],
        }
        if args.dry_run:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0
        args.output_dir.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - CLI should return concise automation errors.
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

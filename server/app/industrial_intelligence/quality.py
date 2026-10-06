"""Current admission policy; archival records and their hashes remain immutable."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

from ..news_relevance import publisher_host, unusable_title
from . import analysis, identity

POLICY_VERSION = "source-quality.2026-09-14.v3-product-identity"


def unsupported_product_identity(event: sqlite3.Row) -> bool:
    if "affected_products_json" not in event.keys():  # noqa: SIM118 -- sqlite3.Row membership tests values
        return False
    claimed = set(json.loads(str(event["affected_products_json"] or "[]"))) & {"poy", "dty"}
    return bool(claimed - set(analysis.detect_products(str(event["title"]))))


def daily_event_rejection(connection: sqlite3.Connection, event: sqlite3.Row, cutoff_at: str) -> str | None:
    if unusable_title(str(event["title"])):
        return "invalid_industrial_title"
    # Old frozen events remain auditable, but cannot carry a generic polyester
    # term into the current brief as a specific POY/DTY fact.
    if unsupported_product_identity(event):
        return "unsupported_product_identity"
    cutoff = identity.parse_iso(cutoff_at)
    # Since the previous business cutoff (weekends included), not an unbounded history.
    start = cutoff - timedelta(days=1)
    while start.weekday() >= 5:
        start -= timedelta(days=1)
    rows = connection.execute(
        "SELECT i.published_at, i.visible_at, i.canonical_url, i.source_tier, i.canonical_payload_json "
        "FROM intelligence_event_evidence e JOIN intelligence_item_revisions i "
        "ON i.item_revision_id=e.item_revision_id WHERE e.event_revision_id=? "
        "AND e.evidence_role IN ('fact','corroboration')",
        (event["event_revision_id"],),
    ).fetchall()
    fresh = []
    for row in rows:
        try:
            visible = identity.parse_iso(str(row["visible_at"]))
            if visible > cutoff:
                continue
            if row["published_at"]:
                published = identity.parse_iso(str(row["published_at"]))
                current = start < published <= cutoff
            else:
                published_date = json.loads(row["canonical_payload_json"]).get("published_date")
                # A date is evaluated at day precision, never converted to midnight.
                current = bool(published_date) and start.date() <= date.fromisoformat(published_date) <= cutoff.date()
            if current:
                fresh.append(row)
        except (ValueError, TypeError):
            continue
    if not fresh:
        return "no_current_publication_evidence"
    if any(row["source_tier"] in ("A", "B") and publisher_host(row["canonical_url"]) for row in fresh):
        return None
    publishers = {publisher_host(row["canonical_url"]) for row in fresh} - {""}
    if len(publishers) < 2:
        return "unverified_independent_publishers"
    return None

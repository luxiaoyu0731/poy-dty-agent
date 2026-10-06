"""Publication metadata from explicit source headers; never from crawl dates."""
from __future__ import annotations

import re
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse


def publication_instant(value: object) -> str | None:
    """Comparable UTC key for source dates, including legacy RSS RFC822 dates.

    A date-only source retains day precision in storage; this key is only for
    filtering. Unparseable values are unknown, never the time of retrieval.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            stamp = parsedate_to_datetime(text)
        except (ValueError, TypeError, OverflowError):
            return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat()


def normalize_feed_publication(value: str) -> str:
    text = value.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text if publication_instant(text) else ""
    return publication_instant(text) or ""


def texnet_publication_date(url: str, text: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host != "texnet.com.cn" and not host.endswith(".texnet.com.cn"):
        return ""
    # The source's publication banner, before article text and related-story links.
    # No timezone is declared there: retain only the justified calendar day.
    match = re.search(
        r"https?://(?:www\.)?texnet\.com\.cn/\s+(20\d{2}-\d{2}-\d{2})"
        r"\s+\d{2}:\d{2}:\d{2}\s+来源[:：]", text[:1600], re.I,
    )
    if not match:
        return ""
    try:
        return date.fromisoformat(match[1]).isoformat()
    except ValueError:
        return ""


def article_publication(row: dict) -> dict[str, str]:
    value = str(row.get("published_at") or "")
    origin = "stored_source_metadata"
    if not value:
        value = texnet_publication_date(str(row.get("url") or row.get("canonical_url") or ""),
                                       str(row.get("raw_text") or ""))
        origin = "texnet_publication_banner" if value else "unknown"
    precision = "unknown"
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            precision = "instant" if parsed.tzinfo else "day"
            if precision == "day":
                value = parsed.date().isoformat()
        except ValueError:
            # Existing RFC822 source timestamps remain unchanged; downstream
            # shared time parsing handles them. Never turn crawl time into source time.
            precision = "source_text"
    return {"published_at": value, "publication_precision": precision, "publication_basis": origin}

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from typing import Any, TypeVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

T = TypeVar("T")


def canonical_event_url(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    query = urlencode(sorted((key, item) for key, item in parse_qsl(parts.query) if not key.lower().startswith("utm_")))
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, ""))


def event_aliases(row: dict[str, Any]) -> set[str]:
    title = re.sub(r"[\W_]+", "", str(row.get("title") or "").casefold())
    event_time = str(row.get("occurred_at") or row.get("updated_at") or row.get("created_at") or "")[:10]
    aliases = {f"title:{title}|date:{event_time}"} if title else set()
    for field in ("cluster_id", "event_cluster_id"):
        if row.get(field):
            aliases.add(f"cluster:{row[field]}")
    for field in ("evidence_url", "canonical_url", "url"):
        url = canonical_event_url(row.get(field))
        if url:
            aliases.add(f"url:{url}")
    article_ids = row.get("article_ids")
    if isinstance(article_ids, str):
        try:
            article_ids = json.loads(article_ids)
        except json.JSONDecodeError:
            article_ids = []
    if isinstance(article_ids, list):
        aliases.update(f"article:{item}" for item in article_ids if item)
    return aliases


def deduplicate_by_aliases(items: Iterable[T], aliases_for: Callable[[T], set[str]]) -> list[T]:
    unique: list[T] = []
    groups: list[set[str]] = []
    for item in items:
        aliases = aliases_for(item)
        matches = [index for index, group in enumerate(groups) if aliases & group]
        if not matches:
            unique.append(item)
            groups.append(set(aliases))
            continue
        primary = matches[0]
        groups[primary].update(aliases)
        # Merge transitive aliases so URL→article→title chains form one event.
        for index in reversed(matches[1:]):
            groups[primary].update(groups[index])
            del groups[index]
            del unique[index]
    return unique

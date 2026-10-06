"""Read-only overlay for validated Chinese radar titles stored outside SQLite.

Cache invalidation follows the store's atomic rename contract. No model calls,
index writes or primary-record mutations occur on the search read path.
"""

from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .event_overview_store import read_overview, store_root


@lru_cache(maxsize=2)
def _entries(root_path: str, directory_mtime: int) -> tuple[tuple[str, str, str], ...]:
    root = Path(root_path)
    entries = []
    for path in sorted(root.glob("*.json")):
        if len(path.stem) != 64:
            continue
        try:
            data = json.loads(path.read_text())
            title = data.get("original_title") or data["source_title"]
            validated = read_overview(title, root)
            if validated and validated.get("generated_at"):
                entries.append((title, validated["overview_zh"], validated["generated_at"]))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return tuple(entries)


def matching_overview_titles(query: str, *, snapshot_at: str) -> list[str]:
    root = store_root()
    try:
        stamp = root.stat().st_mtime_ns
    except OSError:
        return []
    cutoff = datetime.fromisoformat(snapshot_at.replace("Z", "+00:00"))
    result = []
    for title, overview, generated_at in _entries(str(root), stamp):
        try:
            generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            if generated <= cutoff and query.strip().casefold() in overview.casefold():
                result.append(title)
        except (TypeError, ValueError):
            continue
    return result

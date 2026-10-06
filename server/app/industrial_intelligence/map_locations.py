"""Read-only location resolution from accepted facts and bundled Natural Earth.

No model coordinates, publisher addresses, or country guesses. An ambiguous
multi-country location stays unlocated. Country points are labels, not sites.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

COUNTRY_ALIASES = {
    "CHN": {"中国"}, "USA": {"美国", "united states", "u.s."},
    "RUS": {"俄罗斯"}, "SAU": {"沙特", "沙特阿拉伯"}, "KOR": {"韩国"},
    "PRK": {"朝鲜"}, "GBR": {"英国"}, "IRN": {"伊朗"}, "ARE": {"阿联酋"},
}
GEO_ROOT = Path(__file__).resolve().parents[3] / "public" / "geo"


@lru_cache(maxsize=1)
def country_locations() -> tuple[dict, ...]:
    features = json.loads((GEO_ROOT / "ne_110m_admin_0_countries.v5.1.2.geojson").read_text())["features"]
    return tuple({
        "names": {str(f["properties"].get(k) or "").lower() for k in
                  ("NAME", "NAME_LONG", "ADMIN", "NAME_ZH", "NAME_ZHT") } - {""}
        | COUNTRY_ALIASES.get(f["properties"].get("ADM0_A3"), set()),
        "coordinates": [f["properties"]["LABEL_X"], f["properties"]["LABEL_Y"]],
    } for f in features)


@lru_cache(maxsize=1)
def site_locations() -> tuple[dict, ...]:
    features = json.loads((GEO_ROOT / "industrial_nodes.v1.geojson").read_text())["features"]
    return tuple({"names": {str(name).lower() for name in
                           [f["properties"].get("name", ""), f["properties"].get("name_en", ""),
                            *f["properties"].get("aliases", [])] if name},
                  "geometry": f["geometry"],
                  "location_precision": f["properties"].get("location_precision", "source_point")}
                 for f in features if f.get("geometry", {}).get("type") == "Point")


def _contains(text: str, name: str) -> bool:
    return bool(name in text if re.search(r"[\u4e00-\u9fff]", name)
                else re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", text))


def resolve_location(location: str) -> dict | None:
    text = location.strip().lower()
    if not text:
        return None
    sites = [entry for entry in site_locations() if any(_contains(text, name) for name in entry["names"])]
    # More than one named site is not collapsed to an arbitrary point.
    if len(sites) > 1:
        return None
    if sites:
        return {"geometry": sites[0]["geometry"], "location_precision": sites[0]["location_precision"],
                "location_confidence": 0.9}
    matches = []
    for entry in country_locations():
        if any((name in text if re.search(r"[\u4e00-\u9fff]", name)
                else re.search(r"(?<![a-z])" + re.escape(name) + r"(?![a-z])", text))
               for name in entry["names"]):
            matches.append(entry)
    if len(matches) != 1:
        return None
    return {"geometry": {"type": "Point", "coordinates": matches[0]["coordinates"]},
            "location_precision": "country_area", "location_confidence": 0.6}


def accepted_event_location(
    connection: sqlite3.Connection, revision_id: str, *, as_of: str | None = None,
) -> dict | None:
    """Use completed, independently validated facts only, joined to this event's evidence."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='event_ai_summaries'").fetchone():
        return None
    rows = connection.execute("""
        SELECT s.fact_payload FROM intelligence_event_evidence e
        JOIN intelligence_item_revisions i ON i.item_revision_id=e.item_revision_id
        JOIN event_ai_summaries s ON s.article_id=i.projection_source_id
        JOIN news_articles n ON n.article_id=s.article_id
        WHERE e.event_revision_id=? AND i.projection_source_type='news_article'
          AND s.fact_summary_status='completed'
          AND s.summary_status='completed' AND s.quality_status='completed'
          AND s.source_hash=i.content_sha256 AND s.source_hash=n.content_hash
          AND julianday(s.generated_at)<=julianday(?)
        ORDER BY e.append_seq LIMIT 20
    """, (revision_id, as_of or datetime.now(UTC).isoformat())).fetchall()
    locations = []
    for row in rows:
        try:
            facts = json.loads(row["fact_payload"] or "{}")
            result = resolve_location(str(facts.get("location") or ""))
        except (ValueError, TypeError, AttributeError):
            continue
        if result and result not in locations:
            locations.append(result)
    return locations[0] if len(locations) == 1 else None

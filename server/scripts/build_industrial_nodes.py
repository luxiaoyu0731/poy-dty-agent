#!/usr/bin/env python3
"""Build the versioned industrial node catalog (industrial_nodes.v1.geojson).

Contract (docs/industrial-intelligence-center.md section 8.3):

- Ports derive ONLY from the pinned Natural Earth Ports GeoJSON; coordinates
  carry ``location_precision=source_point`` and are never navigation data.
- Canal nodes carry authority-published entrance coordinates with their
  A-tier source citation.
- Refinery/PX/PTA/MEG plants are NOT included until A/B first-party
  coordinates exist; the manifest lists them as explicit coverage gaps.
- Rerunning the converter is deterministic: identical inputs produce an
  identical file and manifest (modulo the generated_at stamp, which is not
  part of the content hash).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ASSET_DIR = REPO_ROOT / "public" / "geo"
NE_PORTS_ASSET = "ne_10m_ports.v5.1.2.geojson"
NE_PORTS_VERSION = "natural-earth-v5.1.2"
NE_PORTS_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_10m_ports.geojson"
)
CONVERTER_VERSION = "industrial-nodes-converter.v1"
NODES_CATALOG_VERSION = "industrial_nodes.v1"

# Curated chain-relevant ports matched by exact NE name; anything not matched
# here (e.g. Daesan) stays an explicit coverage gap instead of a guess.
CURATED_PORTS = {
    "Shanghai": {"country": "CN", "aliases": ["Shanghai Port", "上海港"]},
    "Ningbo": {"country": "CN", "aliases": ["Ningbo-Zhoushan", "宁波舟山港"]},
    "Kaohsiung": {"country": "TW", "aliases": ["Kaohsiung Port", "高雄港"]},
    "Singapore": {"country": "SG", "aliases": ["Port of Singapore", "新加坡港"]},
    "Ulsan": {"country": "KR", "aliases": ["Ulsan Port", "蔚山港"]},
    "Jubail": {"country": "SA", "aliases": ["Jubail Port", "朱拜勒港"]},
    "Rotterdam": {"country": "NL", "aliases": ["Port of Rotterdam", "鹿特丹港"]},
    "Houston": {"country": "US", "aliases": ["Port of Houston", "休斯敦港"]},
}

# Authority-published canal entrance coordinates (A-tier first-party facts).
CANAL_NODES = [
    {
        "node_id": "canal.suez.south_entrance",
        "name": "Suez Canal Southern Entrance",
        "aliases": ["Suez Canal", "苏伊士运河"],
        "country": "EG",
        "longitude": 32.548,
        "latitude": 29.966,
        "source": "Suez Canal Authority published canal overview",
        "source_url": "https://www.suezcanal.gov.eg/",
        "verified_date": "2026-09-05",
    },
    {
        "node_id": "canal.panama.atlantic_entrance",
        "node_type": "canal",
        "name": "Panama Canal Atlantic Entrance",
        "aliases": ["Panama Canal", "巴拿马运河"],
        "country": "PA",
        "longitude": -79.521,
        "latitude": 9.360,
        "source": "Panama Canal Authority published canal overview",
        "source_url": "https://www.pancanal.com/",
        "verified_date": "2026-09-05",
    },
]

COVERAGE_GAPS = [
    {
        "node_type": "port",
        "code": "port_missing_from_pinned_source",
        "detail": (
            "Daesan (KR) is not present in the pinned Natural Earth Ports file; "
            "left unresolved instead of guessing coordinates."
        ),
    },
    {
        "node_type": "refinery",
        "code": "no_verified_ab_coordinates",
        "detail": "No refinery nodes yet: A/B first-party facility coordinates have not been verified.",
    },
    {
        "node_type": "px_plant",
        "code": "no_verified_ab_coordinates",
        "detail": "No PX plant nodes yet: A/B first-party facility coordinates have not been verified.",
    },
    {
        "node_type": "pta_plant",
        "code": "no_verified_ab_coordinates",
        "detail": "No PTA plant nodes yet: A/B first-party facility coordinates have not been verified.",
    },
    {
        "node_type": "meg_plant",
        "code": "no_verified_ab_coordinates",
        "detail": "No MEG plant nodes yet: A/B first-party facility coordinates have not been verified.",
    },
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build() -> tuple[dict, dict]:
    ports_path = ASSET_DIR / NE_PORTS_ASSET
    if not ports_path.is_file():
        raise SystemExit(f"pinned Natural Earth Ports asset missing: {ports_path}")
    ne = json.loads(ports_path.read_text())
    ports_sha = sha256_file(ports_path)

    features: list[dict] = []
    used: set[str] = set()
    for feature in ne["features"]:
        properties = feature.get("properties") or {}
        name = str(properties.get("name") or "")
        geometry = feature.get("geometry")
        if name not in CURATED_PORTS or geometry is None:
            continue
        if name in used:
            continue  # NE lists some ports more than once; one node per name.
        metadata = CURATED_PORTS[name]
        used.add(name)
        node_id = f"port.{name.lower().replace(' ', '_')}.{NE_PORTS_VERSION}"
        features.append(
            {
                "type": "Feature",
                "id": node_id,
                "geometry": geometry,
                "properties": {
                    "node_id": node_id,
                    "node_type": "port",
                    "name": metadata["aliases"][-1],
                    "name_en": name,
                    "aliases": metadata["aliases"],
                    "country": metadata["country"],
                    "source_id": "natural_earth_ports",
                    "source_url": NE_PORTS_URL,
                    "source_version": NE_PORTS_VERSION,
                    "source_file_sha256": ports_sha,
                    "verified_date": "2026-09-05",
                    "rights": "Public domain (Natural Earth terms of use); not suitable for navigation.",
                    "location_precision": "source_point",
                },
            }
        )

    for canal in CANAL_NODES:
        node_type = canal.get("node_type", "canal")
        node_id = canal["node_id"]
        features.append(
            {
                "type": "Feature",
                "id": node_id,
                "geometry": {"type": "Point", "coordinates": [canal["longitude"], canal["latitude"]]},
                "properties": {
                    "node_id": node_id,
                    "node_type": node_type,
                    "name": canal["name"],
                    "name_en": canal["name"],
                    "aliases": canal["aliases"],
                    "country": canal["country"],
                    "source_id": node_id,
                    "source_url": canal["source_url"],
                    "source_version": "authority-published",
                    "source_note": canal["source"],
                    "verified_date": canal["verified_date"],
                    "rights": "Factual location data cited from authority publication.",
                    "location_precision": "source_point",
                },
            }
        )

    missing = sorted(set(CURATED_PORTS) - used)
    if missing:
        raise SystemExit(f"curated ports absent from pinned NE file: {missing}")

    catalog = {
        "type": "FeatureCollection",
        "name": NODES_CATALOG_VERSION,
        "features": features,
    }
    manifest = {
        "manifest_version": f"{NODES_CATALOG_VERSION}-manifest",
        "converter_version": CONVERTER_VERSION,
        "nodes_file": "industrial_nodes.v1.geojson",
        "nodes_content_sha256": hashlib.sha256(
            json.dumps(catalog, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "natural_earth_ports": {
            "asset": NE_PORTS_ASSET,
            "version": NE_PORTS_VERSION,
            "source_url": NE_PORTS_URL,
            "sha256": ports_sha,
            "license": "Public domain per Natural Earth terms of use; coordinate quality degraded, not for navigation.",
        },
        "natural_earth_countries": {
            "asset": "ne_110m_admin_0_countries.v5.1.2.geojson",
            "version": NE_PORTS_VERSION,
            "source_url": (
                "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/v5.1.2/geojson/ne_110m_admin_0_countries.geojson"
            ),
            "sha256": sha256_file(ASSET_DIR / "ne_110m_admin_0_countries.v5.1.2.geojson"),
            "license": "Public domain per Natural Earth terms of use.",
        },
        "coverage_gaps": COVERAGE_GAPS,
    }
    return catalog, manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify existing outputs without writing")
    args = parser.parse_args()
    catalog, manifest = build()
    nodes_path = ASSET_DIR / "industrial_nodes.v1.geojson"
    manifest_path = ASSET_DIR / "industrial-nodes.manifest.v1.json"
    if args.check:
        if not nodes_path.is_file() or not manifest_path.is_file():
            print("missing industrial node assets", file=sys.stderr)
            return 1
        existing = json.loads(nodes_path.read_text())
        if json.dumps(existing, sort_keys=True, ensure_ascii=False) != json.dumps(
            catalog, sort_keys=True, ensure_ascii=False
        ):
            print("industrial nodes drift from pinned source", file=sys.stderr)
            return 1
        print("industrial node assets verified")
        return 0
    nodes_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=1) + "\n")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {len(catalog['features'])} nodes and manifest")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

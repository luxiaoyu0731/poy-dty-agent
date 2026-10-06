#!/usr/bin/env python3
"""Fixed-scale, isolated API latency gate for the v37 intelligence domain."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app import storage  # noqa: E402
from app.industrial_intelligence import analysis, clustering, identity, providers  # noqa: E402
from app.industrial_intelligence import storage as intelligence_storage  # noqa: E402
from app.main import app  # noqa: E402
from app.settings import settings  # noqa: E402

SCHEMA_VERSION = "industrial-intelligence-performance.v1"
DEFAULT_ITEMS = 2_000
DEFAULT_ITERATIONS = 30
THRESHOLDS_MS = {
    "events": 500.0,
    "items": 500.0,
    "event_detail": 500.0,
    "search": 800.0,
    "map_low_zoom": 800.0,
    "map_high_zoom": 800.0,
}


def _percentile_95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _feature(index: int) -> dict[str, object]:
    longitude = -170.0 + float(index % 340)
    latitude = -70.0 + float((index // 340) % 140)
    return {
        "id": f"benchmark-{index:06d}",
        "properties": {
            "mag": 5.0 + float(index % 9) / 10.0,
            "place": f"PX plant fire benchmark marker {index:06d}",
            "time": 1780000000000 + index,
        },
        "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
    }


def _seed(connection, count: int) -> str:
    first_event_id = ""
    as_of_time = identity.utc_now_iso()
    with intelligence_storage.short_write_transaction(connection):
        for index in range(count):
            item = providers._earthquake_item(_feature(index))
            if item is None:
                raise RuntimeError("benchmark fixture rejected")
            item_revision_id, item_id, inserted = intelligence_storage.insert_item_revision(
                connection, item
            )
            if not inserted:
                raise RuntimeError("benchmark fixture identity collision")
            member = clustering.ClusterMember(
                item_id=item_id,
                item_revision_id=item_revision_id,
                origin_group_id=str(item["origin_group_id"]),
                title=str(item["title"]),
                category=str(item["category"]),
                source_tier=str(item["source_tier"]),
                visible_at=str(item["visible_at"]),
                collector_source_id=str(item["collector_source_id"]),
                aggregator_source_id=None,
                canonical_url=str(item["canonical_url"]),
                region_codes=[],
                geometry=item["geometry"],
                location_precision=str(item["location_precision"]),
            )
            event = analysis.analyze_cluster(
                clustering.Cluster(anchor=member, members=[member]),
                as_of_time=as_of_time,
            )
            _, event_id, event_inserted = intelligence_storage.insert_event_revision(
                connection, event
            )
            if not event_inserted:
                raise RuntimeError("benchmark event identity collision")
            if not first_event_id:
                first_event_id = event_id
    return first_event_id


def _measure(client: TestClient, path: str, iterations: int) -> dict[str, float]:
    response = client.get(path)
    response.raise_for_status()
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        response = client.get(path)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        response.raise_for_status()
        samples.append(elapsed_ms)
    return {
        "p50_ms": round(sorted(samples)[len(samples) // 2], 3),
        "p95_ms": round(_percentile_95(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def _atomic_write(path: Path, body: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(body, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--items", type=int, default=DEFAULT_ITEMS)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    args = parser.parse_args()
    if args.items < 1_000 or args.iterations < 20:
        parser.error("release evidence requires at least 1,000 items and 20 iterations")
    work_dir = args.work_dir.resolve()
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(work_dir, 0o700)
    database = work_dir / "intelligence-performance.sqlite"
    if database.exists():
        parser.error("work-dir must not contain an existing benchmark database")

    original = (
        settings.sqlite_path,
        settings.industrial_intelligence_enabled,
        settings.enforce_internal_token,
        settings.intraday_price_scheduler_enabled,
        settings.agent_governance_scheduler_enabled,
        settings.experience_settlement_scheduler_enabled,
    )
    object.__setattr__(settings, "sqlite_path", str(database))
    object.__setattr__(settings, "industrial_intelligence_enabled", True)
    object.__setattr__(settings, "enforce_internal_token", False)
    object.__setattr__(settings, "intraday_price_scheduler_enabled", False)
    object.__setattr__(settings, "agent_governance_scheduler_enabled", False)
    object.__setattr__(settings, "experience_settlement_scheduler_enabled", False)
    storage._MIGRATED_PATHS.clear()
    try:
        with closing(storage.connect()) as connection:
            first_event_id = _seed(connection, args.items)
        endpoints = {
            "events": "/api/v1/intelligence/events?limit=50",
            "items": "/api/v1/intelligence/items?limit=50",
            "event_detail": f"/api/v1/intelligence/events/{first_event_id}",
            "search": "/api/v1/intelligence/search?q=benchmark&limit=50",
            "map_low_zoom": "/api/v1/intelligence/map?bbox=-180,-85,180,85&zoom=2",
            "map_high_zoom": "/api/v1/intelligence/map?bbox=-180,-85,180,85&zoom=10",
        }
        with TestClient(app) as client:
            results = {
                name: _measure(client, path, args.iterations)
                for name, path in endpoints.items()
            }
        passed = all(
            results[name]["p95_ms"] < threshold
            for name, threshold in THRESHOLDS_MS.items()
        )
        report: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "fixture_items": args.items,
            "fixture_events": args.items,
            "iterations_after_warmup": args.iterations,
            "thresholds_ms": THRESHOLDS_MS,
            "results": results,
            "passed": passed,
        }
        _atomic_write(args.output.resolve(), report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0 if passed else 1
    finally:
        storage._MIGRATED_PATHS.clear()
        object.__setattr__(settings, "sqlite_path", original[0])
        object.__setattr__(settings, "industrial_intelligence_enabled", original[1])
        object.__setattr__(settings, "enforce_internal_token", original[2])
        object.__setattr__(settings, "intraday_price_scheduler_enabled", original[3])
        object.__setattr__(settings, "agent_governance_scheduler_enabled", original[4])
        object.__setattr__(settings, "experience_settlement_scheduler_enabled", original[5])


if __name__ == "__main__":
    raise SystemExit(main())

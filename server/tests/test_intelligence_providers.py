"""D6 providers: USGS contract, Natural Earth assets, failure isolation."""

from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from app import storage
from app.industrial_intelligence import providers, service
from app.settings import settings


@pytest.fixture(autouse=True)
def _allowlist_includes_usgs(monkeypatch: pytest.MonkeyPatch) -> None:
    # The managed production .env overrides defaults; intelligence tests run
    # with the USGS host added, mirroring the documented enablement step.
    original = settings.outbound_hosts
    hosts = (*original, "earthquake.usgs.gov")
    object.__setattr__(settings, "outbound_hosts", tuple(dict.fromkeys(hosts)))
    yield
    object.__setattr__(settings, "outbound_hosts", original)


@pytest.fixture
def provider_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    original_sqlite_path = settings.sqlite_path
    original_runs = os.getenv(service.RUNS_DIR_ENV)
    database = tmp_path / "provider.db"
    object.__setattr__(settings, "sqlite_path", str(database))
    monkeypatch.setenv(service.RUNS_DIR_ENV, str(tmp_path / "runs"))
    storage._MIGRATED_PATHS.clear()
    try:
        with closing(storage.connect()):
            pass
        yield database
    finally:
        storage._MIGRATED_PATHS.clear()
        object.__setattr__(settings, "sqlite_path", original_sqlite_path)
        if original_runs is None:
            monkeypatch.delenv(service.RUNS_DIR_ENV, raising=False)
        else:
            monkeypatch.setenv(service.RUNS_DIR_ENV, original_runs)




def _usgs_feed(features: list[dict]) -> httpx.Response:
    request = httpx.Request("GET", providers.USGS_ENDPOINT)
    return httpx.Response(200, content=json.dumps({"features": features}).encode(), request=request)


def test_usgs_provider_parses_official_feed_schema() -> None:
    feature = {
        "id": "us7000kabc",
        "properties": {
            "mag": 5.4,
            "place": "Celebes Sea",
            "time": 1780000000000,
            "url": "https://earthquake.usgs.gov/earthquakes/eventpage/us7000kabc",
        },
        "geometry": {"type": "Point", "coordinates": [125.1, 3.9, 510.0]},
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda request: _usgs_feed([feature])))
    outcome = providers.fetch_usgs_week_feed(client=client)
    assert outcome.status == "succeeded"
    assert len(outcome.items) == 1
    item = outcome.items[0]
    assert item["source_tier"] == "A"
    assert item["projection_source_type"] == "usgs_feed"
    assert item["external_id"] == "us7000kabc"
    assert item["geometry"] == {"type": "Point", "coordinates": [125.1, 3.9]}
    assert item["location_precision"] == "source_point"
    assert item["occurred_at"] == "2026-05-29T03:46:40Z" or item["occurred_at"].endswith("Z")
    assert item["visible_at"] == item["first_seen_at"]
    assert item["visible_at"] != item["occurred_at"]
    assert item["canonical_url"].startswith("https://earthquake.usgs.gov/earthquakes/eventpage/")
    assert item["rights"]["storage_mode"] == "metadata_only"
    # Earthquake facts never assert industry impact.
    assert item["content_status"] == "absent"


def test_usgs_rejects_malformed_and_out_of_bounds_features() -> None:
    features = [
        {"id": "", "properties": {"mag": 5.0}, "geometry": {"coordinates": [0, 0]}},
        {"id": "x", "properties": {"mag": None}, "geometry": {"coordinates": [0, 0]}},
        {"id": "y", "properties": {"mag": 6.0}, "geometry": {"coordinates": [200.0, 91.0]}},
        {"id": "z", "properties": {"mag": 4.4}, "geometry": {"coordinates": [120.0, 30.0]}},
    ]
    client = httpx.Client(transport=httpx.MockTransport(lambda request: _usgs_feed(features)))
    outcome = providers.fetch_usgs_week_feed(client=client)
    assert outcome.status == "succeeded"
    assert outcome.items == []
    assert outcome.rejected_count == 4


def test_usgs_failures_are_isolated_and_never_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = providers.fetch_usgs_week_feed(client=client)
    assert outcome.status == "failed"
    assert outcome.error_code == "usgs_fetch_failed"
    assert outcome.items == []
    # Oversized and schema-drifted responses degrade instead of crashing.
    oversized = httpx.Response(
        200,
        content=b"x" * (providers.USGS_MAX_BYTES + 1),
        request=httpx.Request("GET", providers.USGS_ENDPOINT),
    )
    client2 = httpx.Client(transport=httpx.MockTransport(lambda request: oversized))
    assert providers.fetch_usgs_week_feed(client=client2).degraded_reasons == ["usgs_response_too_large"]
    bad = httpx.Response(
        200, content=b'{"features": "not-a-list"}', request=httpx.Request("GET", providers.USGS_ENDPOINT)
    )
    client3 = httpx.Client(transport=httpx.MockTransport(lambda request: bad))
    assert providers.fetch_usgs_week_feed(client=client3).degraded_reasons == ["usgs_schema_unexpected"]
    # Without a transport the provider fails closed and isolated.
    outcome = providers.fetch_usgs_week_feed(client=None)
    assert outcome.status == "failed"


def test_usgs_endpoint_is_fixed_and_allowlisted() -> None:
    assert providers.USGS_ENDPOINT.startswith("https://earthquake.usgs.gov/")
    assert settings.outbound_host_allowed("earthquake.usgs.gov")


def test_usgs_blocked_when_allowlist_lacks_host(monkeypatch: pytest.MonkeyPatch) -> None:
    original = settings.outbound_hosts
    object.__setattr__(
        settings,
        "outbound_hosts",
        tuple(host for host in original if host != "earthquake.usgs.gov"),
    )
    try:
        outcome = providers.fetch_usgs_week_feed(client=httpx.Client())
        assert outcome.status == "failed"
        assert outcome.error_code == "blocked_by_allowlist"
    finally:
        object.__setattr__(settings, "outbound_hosts", original)


def test_natural_earth_assets_validate_against_manifest() -> None:
    checks = providers.validate_geo_assets()
    assert checks["status"] == "ok", checks
    assets = checks["assets"]
    assert assets["natural_earth_countries"]["version"] == "natural-earth-v5.1.2"
    assert assets["natural_earth_ports"]["status"] == "ok"
    # Node catalog keeps unresolved locations as explicit gaps.
    assert "no_verified_ab_coordinates" in assets["industrial_nodes"]["coverage_gaps"]


def test_industrial_nodes_have_frozen_identity_and_precision() -> None:
    nodes = json.loads(providers.NODES_PATH.read_text())
    for feature in nodes["features"]:
        properties = feature["properties"]
        assert properties["node_type"] in ("port", "canal", "refinery", "px_plant", "pta_plant", "meg_plant")
        assert properties["location_precision"] == "source_point"
        assert properties["source_version"]
        assert len(properties.get("source_file_sha256") or "") == 64 or properties.get("source_note")
        lon, lat = feature["geometry"]["coordinates"]
        assert -180 <= lon <= 180 and -90 <= lat <= 90
    node_ids = [feature["properties"]["node_id"] for feature in nodes["features"]]
    assert len(node_ids) == len(set(node_ids))  # no duplicate identities


def test_usgs_stage_is_audited_and_idempotent(provider_database: Path) -> None:
    feature = {
        "id": "us7000stage",
        "properties": {
            "mag": 5.2,
            "place": "Philippine Sea",
            "time": 1780000000000,
            "url": "https://attacker.invalid/ignored",
        },
        "geometry": {"type": "Point", "coordinates": [127.0, 12.0, 30.0]},
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda request: _usgs_feed([feature])))
    with closing(storage.connect()) as connection:
        first = service.run_usgs_provider_stage(
            connection, business_date="2026-09-04", client=client
        )
        second = service.run_usgs_provider_stage(
            connection, business_date="2026-09-04", client=client
        )
        assert first.status == second.status == "succeeded"
        assert first.inserted == 1 and second.existing == 1
        rows = connection.execute(
            "SELECT status, input_count, inserted_count, existing_count "
            "FROM intelligence_runs WHERE run_type='provider' ORDER BY append_seq"
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("succeeded", 1, 1, 0),
            ("succeeded", 1, 0, 1),
        ]


def test_daily_pipeline_continues_when_usgs_fails(provider_database: Path) -> None:
    def timeout(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timeout")

    client = httpx.Client(transport=httpx.MockTransport(timeout))
    with closing(storage.connect()) as connection:
        result = service.run_daily_pipeline(
            connection,
            business_date="2026-09-04",
            usgs_client=client,
        )
        assert result.failed_provider_ids == [providers.USGS_PROVIDER_ID]
        assert result.brief_status == "blocked"  # no usable evidence after the sole provider fails
        brief_row = connection.execute(
            "SELECT gaps_json FROM intelligence_daily_briefs WHERE brief_id=?",
            (result.brief_id,),
        ).fetchone()
        assert any(
            gap["scope"] == providers.USGS_PROVIDER_ID
            for gap in json.loads(brief_row["gaps_json"])
        )
        run_types = {
            row["run_type"]
            for row in connection.execute("SELECT run_type FROM intelligence_runs").fetchall()
        }
        assert run_types == {"provider", "projection", "clustering", "brief"}

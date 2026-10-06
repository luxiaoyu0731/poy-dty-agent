from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from app import public_benchmark_v2 as benchmark
from app.main import app
from app.source_registry import get_source, list_sources

NOW = datetime(2026, 8, 30, 8, 0, tzinfo=UTC)


def test_contract_has_exactly_nine_public_inputs_and_two_targets() -> None:
    assert [item["series_id"] for item in benchmark.PUBLIC_BENCHMARK_INPUTS] == [
        "public.brent.futures_proxy.usd_bbl",
        "public.wti.futures_proxy.usd_bbl",
        "public.naphtha.spot_assessment.usd_mt",
        "public.px.main_futures_proxy.cny_mt",
        "public.pta.main_futures_proxy.cny_mt",
        "public.meg.main_futures_proxy.cny_mt",
        "public.poy.spot_assessment.cny_mt",
        "public.dty.spot_assessment.cny_mt",
        "public.fx.usd_cny.reference.cny_per_usd",
    ]
    assert len(benchmark.PUBLIC_BENCHMARK_TARGETS) == 2
    assert all(item["source_cadence"] for item in benchmark.PUBLIC_BENCHMARK_INPUTS)
    assert all(item["expected_availability"] for item in benchmark.PUBLIC_BENCHMARK_INPUTS)
    assert all(int(item["max_age_seconds"]) > 0 for item in benchmark.PUBLIC_BENCHMARK_INPUTS)


def test_snapshot_derives_values_and_has_no_permission_gate(monkeypatch) -> None:
    rows = []
    prices = {"Brent": 80, "WTI": 75, "NAPHTHA": 700, "PX": 8500, "PTA": 6000, "MEG": 4500, "POY": 7600, "DTY": 9000}
    units = {str(item["lookup_key"]): str(item["unit"]) for item in benchmark.PUBLIC_BENCHMARK_INPUTS}
    for instrument, value in prices.items():
        rows.append(
            {
                "instrument": instrument,
                "observed_at": "2026-08-30T07:00:00+00:00",
                "last": value,
                "unit": units[instrument],
                "source_id": "public-test",
                "source_url": "https://example.com/quote",
            }
        )
    monkeypatch.setattr(benchmark, "latest_intraday_price_observations", lambda **_: rows)
    monkeypatch.setattr(
        benchmark,
        "list_market_observations",
        lambda **_: [
            {
                "observed_at": "2026-08-29",
                "value": 7.2,
                "unit": "cny_per_usd",
                "source_id": "cfets_cny_parity",
                "evidence_url": "https://example.com/fx",
            }
        ],
    )

    snapshot = benchmark.build_public_benchmark_snapshot(now=NOW)

    assert snapshot["status"] == "qualified"
    assert snapshot["blockers"] == []
    assert {item["status"] for item in snapshot["derived_values"]} == {"ready"}
    assert snapshot["governance"]["permission_or_manifest_gate"] is False
    assert snapshot["governance"]["personal_mode_affects_qualification"] is False
    assert snapshot["governance"]["ccf_operational_status"] == "soft_removed"


@pytest.mark.parametrize("source_id", ["ccf_dom_daily", "ccf_average_price", "ccf_manual_export", "dce_meg"])
def test_hard_removed_sources_are_not_lookup_or_scheduling_candidates(source_id) -> None:
    assert source_id not in {source.source_id for source in list_sources()}
    assert source_id not in {source.source_id for source in list_sources(include_soft_removed=True)}
    assert get_source(source_id) is None


def test_public_v2_openapi_contract_matches_controlled_document() -> None:
    runtime = app.openapi()
    controlled = yaml.safe_load((Path(__file__).parents[2] / "docs" / "openapi.yaml").read_text(encoding="utf-8"))

    assert controlled["paths"]["/api/v1/benchmarks/public-v2"] == runtime["paths"][
        "/api/v1/benchmarks/public-v2"
    ]
    for schema in (
        "PublicBenchmarkInputDefinition",
        "PublicBenchmarkTargetDefinition",
        "PublicBenchmarkObservation",
        "PublicBenchmarkDerivedValue",
        "PublicBenchmarkSnapshot",
        "SourceConfig",
    ):
        assert controlled["components"]["schemas"][schema] == runtime["components"]["schemas"][schema]

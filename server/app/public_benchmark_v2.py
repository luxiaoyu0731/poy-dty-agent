from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .source_publication_calendar import cfets_holiday_carry_reason
from .storage import latest_intraday_price_observations, list_market_observations

CONTRACT_ID = "public-benchmark.v2"
FORMULA_VERSION = "polyester_upstream_pressure.v1"

PUBLIC_BENCHMARK_INPUTS: tuple[dict[str, Any], ...] = (
    {
        "series_id": "public.brent.futures_proxy.usd_bbl",
        "label": "Brent public futures proxy",
        "channel": "intraday",
        "lookup_key": "Brent",
        "unit": "USD/bbl",
        "source_cadence": "exchange_session",
        "polling_interval_seconds": 300,
        "expected_availability": "during exchange sessions; weekend/holiday gaps expected",
        "max_age_seconds": 3 * 86400,
    },
    {
        "series_id": "public.wti.futures_proxy.usd_bbl",
        "label": "WTI public futures proxy",
        "channel": "intraday",
        "lookup_key": "WTI",
        "unit": "USD/bbl",
        "source_cadence": "exchange_session",
        "polling_interval_seconds": 300,
        "expected_availability": "during exchange sessions; weekend/holiday gaps expected",
        "max_age_seconds": 3 * 86400,
    },
    {
        "series_id": "public.naphtha.spot_assessment.usd_mt",
        "label": "Naphtha public assessment",
        "channel": "intraday",
        "lookup_key": "NAPHTHA",
        "unit": "USD/mt",
        "source_cadence": "business_day_or_less_frequent_publication",
        "polling_interval_seconds": 300,
        "expected_availability": "when the public assessment page publishes a dated value",
        "max_age_seconds": 7 * 86400,
    },
    {
        "series_id": "public.px.main_futures_proxy.cny_mt",
        "label": "PX public main-futures proxy",
        "channel": "intraday",
        "lookup_key": "PX",
        "unit": "CNY/mt",
        "source_cadence": "china_exchange_business_day",
        "polling_interval_seconds": 300,
        "expected_availability": "during China futures sessions; exchange holidays expected",
        "max_age_seconds": 4 * 86400,
    },
    {
        "series_id": "public.pta.main_futures_proxy.cny_mt",
        "label": "PTA public main-futures proxy",
        "channel": "intraday",
        "lookup_key": "PTA",
        "unit": "CNY/mt",
        "source_cadence": "china_exchange_business_day",
        "polling_interval_seconds": 300,
        "expected_availability": "during China futures sessions; exchange holidays expected",
        "max_age_seconds": 4 * 86400,
    },
    {
        "series_id": "public.meg.main_futures_proxy.cny_mt",
        "label": "MEG public main-futures proxy",
        "channel": "intraday",
        "lookup_key": "MEG",
        "unit": "CNY/mt",
        "source_cadence": "china_exchange_business_day",
        "polling_interval_seconds": 300,
        "expected_availability": "during China futures sessions; exchange holidays expected",
        "max_age_seconds": 4 * 86400,
    },
    {
        "series_id": "public.poy.spot_assessment.cny_mt",
        "label": "POY public assessment",
        "channel": "intraday",
        "lookup_key": "POY",
        "unit": "CNY/mt",
        "source_cadence": "business_day_publication",
        "polling_interval_seconds": 300,
        "expected_availability": "when the public textile page publishes a dated assessment",
        "max_age_seconds": 7 * 86400,
    },
    {
        "series_id": "public.dty.spot_assessment.cny_mt",
        "label": "DTY public assessment",
        "channel": "intraday",
        "lookup_key": "DTY",
        "unit": "CNY/mt",
        "source_cadence": "business_day_publication",
        "polling_interval_seconds": 300,
        "expected_availability": "when the public textile page publishes a dated assessment",
        "max_age_seconds": 7 * 86400,
    },
    {
        "series_id": "public.fx.usd_cny.reference.cny_per_usd",
        "label": "CFETS USD/CNY reference rate",
        "channel": "market_observation",
        "lookup_key": "cfets_cny_parity",
        "unit": "cny_per_usd",
        "source_cadence": "china_business_day",
        "polling_interval_seconds": 5400,
        "expected_availability": "after the CFETS business-day central-parity publication",
        "max_age_seconds": 4 * 86400,
    },
)

PUBLIC_BENCHMARK_TARGETS: tuple[dict[str, str], ...] = (
    {
        "target_id": "public.poy.upstream_cost_pressure.index",
        "label": "POY upstream-cost pressure",
        "unit": "ratio",
        "formula_version": FORMULA_VERSION,
        "formula": "(0.855 * PTA + 0.335 * MEG) / POY",
    },
    {
        "target_id": "public.dty.upstream_cost_pressure.index",
        "label": "DTY upstream-cost pressure",
        "unit": "ratio",
        "formula_version": FORMULA_VERSION,
        "formula": "(0.855 * PTA + 0.335 * MEG) / DTY",
    },
)


def build_public_benchmark_snapshot(*, now: datetime | None = None) -> dict[str, Any]:
    generated = (now or datetime.now(UTC)).astimezone(UTC)
    intraday = {
        str(row.get("instrument")): row
        for row in latest_intraday_price_observations(
            instruments=tuple(item["lookup_key"] for item in PUBLIC_BENCHMARK_INPUTS if item["channel"] == "intraday")
        )
    }
    fx_rows = list_market_observations(source_id="cfets_cny_parity", limit=1)
    observations: list[dict[str, Any]] = []
    for definition in PUBLIC_BENCHMARK_INPUTS:
        row = (
            intraday.get(definition["lookup_key"])
            if definition["channel"] == "intraday"
            else (fx_rows[0] if fx_rows else None)
        )
        observations.append(evaluate_observation(definition, row, now=generated))
    blockers = [item["series_id"] for item in observations if item["status"] != "ready"]
    derived = derive_targets(observations)
    return {
        "schema_version": "public_benchmark_snapshot.v2",
        "contract_id": CONTRACT_ID,
        "generated_at": generated.isoformat(),
        "status": "qualified" if not blockers else "blocked",
        "inputs": [dict(item) for item in PUBLIC_BENCHMARK_INPUTS],
        "targets": [dict(item) for item in PUBLIC_BENCHMARK_TARGETS],
        "observations": observations,
        "derived_values": derived,
        "blockers": blockers,
        "governance": {
            "qualification_basis": "technical_provenance_and_per_series_freshness",
            "permission_or_manifest_gate": False,
            "personal_mode_affects_qualification": False,
            "ccf_operational_status": "soft_removed",
        },
    }


def evaluate_observation(definition: dict[str, Any], row: dict[str, Any] | None, *, now: datetime) -> dict[str, Any]:
    base = {
        "series_id": definition["series_id"],
        "status": "missing",
        "observed_at": "",
        "value": None,
        "unit": "",
        "source_id": "",
        "source_url": "",
        "age_seconds": None,
        "reason": "observation_missing",
    }
    if not row:
        return base
    observed_at = str(row.get("observed_at") or "")
    value = row.get("last") if definition["channel"] == "intraday" else row.get("value")
    unit = str(row.get("unit") or "")
    source_url = str(row.get("source_url") or row.get("evidence_url") or "")
    base.update(
        observed_at=observed_at,
        value=float(value) if isinstance(value, (int, float)) else None,
        unit=unit,
        source_id=str(row.get("source_id") or ""),
        source_url=source_url,
    )
    observed = _parse_datetime(observed_at)
    if observed is None or base["value"] is None or float(base["value"]) <= 0:
        base.update(status="invalid", reason="timestamp_or_value_invalid")
        return base
    if unit != definition["unit"]:
        base.update(status="invalid", reason=f"unit_mismatch:expected={definition['unit']}")
        return base
    if observed > now:
        base.update(status="invalid", reason="observation_from_future")
        return base
    age_seconds = max(0, int((now - observed).total_seconds()))
    base["age_seconds"] = age_seconds
    if age_seconds > int(definition["max_age_seconds"]):
        carry = cfets_holiday_carry_reason(base["source_id"], observed, now) if (
            definition["lookup_key"] == "cfets_cny_parity"
            and definition["channel"] == "market_observation"
        ) else None
        base.update(status="ready" if carry else "stale", reason=carry or "outside_declared_source_cadence")
    else:
        base.update(status="ready", reason="")
    return base


def derive_targets(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {item["series_id"]: item for item in observations}
    pta = by_id["public.pta.main_futures_proxy.cny_mt"]
    meg = by_id["public.meg.main_futures_proxy.cny_mt"]
    result: list[dict[str, Any]] = []
    for definition, product_id in zip(
        PUBLIC_BENCHMARK_TARGETS,
        ("public.poy.spot_assessment.cny_mt", "public.dty.spot_assessment.cny_mt"),
        strict=True,
    ):
        product = by_id[product_id]
        inputs = (pta, meg, product)
        timestamps = {item["series_id"]: item["observed_at"] for item in inputs if item["observed_at"]}
        if any(item["status"] != "ready" for item in inputs):
            result.append(
                {
                    "target_id": definition["target_id"],
                    "status": "blocked",
                    "value": None,
                    "unit": definition["unit"],
                    "formula_version": FORMULA_VERSION,
                    "input_observed_at": timestamps,
                    "reason": "required_formula_input_not_ready",
                }
            )
            continue
        value = (0.855 * float(pta["value"]) + 0.335 * float(meg["value"])) / float(product["value"])
        result.append(
            {
                "target_id": definition["target_id"],
                "status": "ready",
                "value": round(value, 8),
                "unit": definition["unit"],
                "formula_version": FORMULA_VERSION,
                "input_observed_at": timestamps,
                "reason": "",
            }
        )
    return result


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)

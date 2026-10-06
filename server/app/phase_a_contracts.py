from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .source_registry import REMOVED_SOURCE_IDS

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "phase_a_contracts.v7.json"
V6_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "phase_a_contracts.v6.json"
V5_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "phase_a_contracts.v5.json"
V4_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "phase_a_contracts.v4.json"
V3_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "phase_a_contracts.v3.json"
V2_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "phase_a_contracts.v2.json"
LEGACY_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "phase_a_contracts.v1.json"
SOURCE_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "source_registry.json"

EXPECTED_CONTRACT_VERSION = "phase-a.v7"
V6_CONTRACT_VERSION = "phase-a.v6"
V5_CONTRACT_VERSION = "phase-a.v5"
V4_CONTRACT_VERSION = "phase-a.v4"
V2_CONTRACT_VERSION = "phase-a.v2"
V3_CONTRACT_VERSION = "phase-a.v3"
EXPECTED_FORMAL_NODES = {
    "brent",
    "wti",
    "coal",
    "naphtha",
    "mx",
    "px",
    "ethylene",
    "eo",
    "methanol",
    "pta",
    "meg",
    "polyester_melt",
    "polyester_chip",
    "poy_dty_upstream_cost_pressure",
}
EXPECTED_FORMAL_HORIZONS = {1, 7, 30}
LEGACY_READ_ONLY_HORIZONS = {14}
CONTRACTIBLE_STATES = {"contractible", "contractible_license_gated"}
BLOCKED_STATES = {
    "blocked_missing_source",
    "blocked_unresolved_spec",
    "blocked_missing_formula",
    "blocked_missing_roll_rule",
    "blocked_license_or_spec_unverified",
    "blocked_evidence_capture_pending",
    "blocked_daily_source_unresolved",
    "blocked_formula_implementation_pending",
}
SOURCE_PRODUCT_BY_NODE = {
    "brent": "crude_oil",
    "wti": "crude_oil",
    "coal": "coal",
    "naphtha": "naphtha",
    "mx": "mx",
    "px": "px",
    "ethylene": "ethylene",
    "eo": "eo",
    "methanol": "methanol",
    "pta": "pta",
    "meg": "meg",
    "polyester_melt": "polyester_melt",
    "polyester_chip": "polyester_chip",
    "poy_dty_upstream_cost_pressure": None,
    "__auxiliary_fx__": "usd_cny",
}
CONTINUOUS_CONTRACT_POLICY_ID = "china-futures-main-continuous.v1"
CONTINUOUS_CONTRACT_SERIES_IDS = (
    "px.czce.main.settlement.cny_mt",
    "methanol.czce.main.settlement.cny_mt",
    "pta.czce.main.settlement.cny_mt",
    "meg.dce.main.settlement.cny_mt",
)
CONTINUOUS_CONTRACT_POLICY = {
    "policy_id": CONTINUOUS_CONTRACT_POLICY_ID,
    "eligible_contracts": "exchange_listed_active_contracts",
    "ranking": ["open_interest_desc", "volume_desc", "nearer_expiry_asc"],
    "roll_confirmation_trading_days": 2,
    "roll_effective": "next_trading_day",
    "forced_roll_trigger": "current_contract_final_trading_month",
    "forced_roll_effective": "next_trading_day",
    "forced_roll_selection": "ranked_non_final_trading_month",
    "price_field": "official_daily_settlement",
    "back_adjustment": "prohibited",
    "synthetic_fill": "prohibited",
}
PUBLIC_SOURCE_PERSONAL_REUSE_POLICY = {
    "policy_id": "public-source-personal-reuse.v1",
    "permitted_use": "personal_internal_analysis_and_non_reversible_public_derived_conclusions",
    "source_attribution_required": True,
    "immutable_capture_evidence_required": True,
    "raw_data_redistribution": "prohibited",
}
INTERNATIONAL_FRONT_MONTH_POLICY = {
    "policy_id": "international-front-month.v1",
    "selection": "nearest_listed_contract_not_in_final_trading_month",
    "roll_effective": "next_trading_day",
    "price_field": "official_daily_settlement",
    "back_adjustment": "prohibited",
    "synthetic_fill": "prohibited",
}
DERIVED_COST_PRESSURE_FORMULA = {
    "formula_id": "poy-dty-upstream-cost-pressure.v1",
    "components": [
        {"series_id": "pta.ccf.domestic.daily_assessment.cny_mt", "weight": 0.855},
        {"series_id": "meg.ccf.domestic.daily_assessment.cny_mt", "weight": 0.335},
    ],
    "baseline": "trailing_20_common_business_days_arithmetic_mean",
    "pressure_index": "100_times_current_material_basket_divided_by_baseline_mean",
    "missing_input_behavior": "unscorable",
    "shared_by_targets": ["poy.upstream_cost_pressure.index", "dty.upstream_cost_pressure.index"],
}
INTERNATIONAL_FRONT_MONTH_SERIES_IDS = (
    "crude.brent.ice.front_month.settlement.usd_bbl",
    "crude.wti.cme.front_month.settlement.usd_bbl",
)
DERIVED_COST_PRESSURE_SERIES_IDS = (
    "poy.upstream_cost_pressure.index",
    "dty.upstream_cost_pressure.index",
)


class PhaseAContractError(ValueError):
    """Raised when the frozen Phase A contract or a payload violates its policy."""


def load_contract(path: Path = CONTRACT_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise PhaseAContractError("contract root must be an object")
    if path == CONTRACT_PATH:
        return _resolve_v7_contract(value)
    if path == V6_CONTRACT_PATH:
        return _resolve_v6_contract(value)
    if path == V5_CONTRACT_PATH:
        return _resolve_v5_contract(value)
    if path == V4_CONTRACT_PATH:
        return _resolve_v4_contract(value)
    if path == V3_CONTRACT_PATH:
        return _resolve_v3_contract(value)
    if path == V2_CONTRACT_PATH:
        return _resolve_v2_contract(value)
    return value


def _resolve_v2_contract(overlay: Mapping[str, Any]) -> dict[str, Any]:
    if (
        overlay.get("contract_id") != "phase-a"
        or overlay.get("contract_version") != V2_CONTRACT_VERSION
        or overlay.get("base_contract_version") != "phase-a.v1"
        or overlay.get("base_contract_path") != LEGACY_CONTRACT_PATH.name
    ):
        raise PhaseAContractError("phase-a.v2 overlay is invalid")
    with LEGACY_CONTRACT_PATH.open(encoding="utf-8") as handle:
        legacy = json.load(handle)
    if not isinstance(legacy, dict) or legacy.get("contract_version") != "phase-a.v1":
        raise PhaseAContractError("phase-a.v2 legacy base is invalid")
    resolved = json.loads(json.dumps(legacy))
    resolved["contract_version"] = V2_CONTRACT_VERSION
    resolved["governance_version"] = overlay.get("governance_version")
    resolved["formal_evidence_series_ids"] = overlay.get("formal_evidence_series_ids")
    resolved["non_prediction_series_ids"] = overlay.get("non_prediction_series_ids")
    overrides = _mapping(overlay.get("series_overrides"))
    for series in resolved["series"]:
        series["contract_version"] = V2_CONTRACT_VERSION
        override = _mapping(overrides.get(series["series_id"]))
        series.update(override)
    return resolved


def _resolve_v3_contract(overlay: Mapping[str, Any]) -> dict[str, Any]:
    if (
        overlay.get("contract_id") != "phase-a"
        or overlay.get("contract_version") != V3_CONTRACT_VERSION
        or overlay.get("base_contract_version") != "phase-a.v2"
        or overlay.get("base_contract_path") != V2_CONTRACT_PATH.name
    ):
        raise PhaseAContractError("phase-a.v3 overlay is invalid")
    with V2_CONTRACT_PATH.open(encoding="utf-8") as handle:
        base_overlay = json.load(handle)
    if not isinstance(base_overlay, dict):
        raise PhaseAContractError("phase-a.v3 v2 base is invalid")
    resolved = _resolve_v2_contract(base_overlay)
    resolved["contract_version"] = V3_CONTRACT_VERSION
    resolved["governance_version"] = overlay.get("governance_version")
    resolved["continuous_contract_policy"] = _mapping(overlay.get("continuous_contract_policy"))
    resolved["continuous_contract_series_ids"] = overlay.get("continuous_contract_series_ids")
    overrides = _mapping(overlay.get("series_overrides"))
    for series in resolved["series"]:
        series["contract_version"] = V3_CONTRACT_VERSION
        series.update(_mapping(overrides.get(series["series_id"])))
    return resolved


def _resolve_v4_contract(overlay: Mapping[str, Any]) -> dict[str, Any]:
    if (
        overlay.get("contract_id") != "phase-a"
        or overlay.get("contract_version") != V4_CONTRACT_VERSION
        or overlay.get("base_contract_version") != V3_CONTRACT_VERSION
        or overlay.get("base_contract_path") != V3_CONTRACT_PATH.name
    ):
        raise PhaseAContractError("phase-a.v4 overlay is invalid")
    with V3_CONTRACT_PATH.open(encoding="utf-8") as handle:
        base_overlay = json.load(handle)
    if not isinstance(base_overlay, dict):
        raise PhaseAContractError("phase-a.v4 v3 base is invalid")
    resolved = _resolve_v3_contract(base_overlay)
    resolved["contract_version"] = V4_CONTRACT_VERSION
    resolved["governance_version"] = overlay.get("governance_version")
    for policy_name in (
        "public_source_personal_reuse_policy",
        "international_front_month_policy",
        "derived_cost_pressure_formula",
    ):
        resolved[policy_name] = _mapping(overlay.get(policy_name))
    overrides = _mapping(overlay.get("series_overrides"))
    for series in resolved["series"]:
        series["contract_version"] = V4_CONTRACT_VERSION
        series.update(_mapping(overrides.get(series["series_id"])))
    return resolved


def _resolve_v5_contract(overlay: Mapping[str, Any]) -> dict[str, Any]:
    if (
        overlay.get("contract_id") != "phase-a"
        or overlay.get("contract_version") != V5_CONTRACT_VERSION
        or overlay.get("base_contract_version") != "phase-a.v4"
        or overlay.get("base_contract_path") != V4_CONTRACT_PATH.name
    ):
        raise PhaseAContractError("phase-a.v5 overlay is invalid")
    with V4_CONTRACT_PATH.open(encoding="utf-8") as handle:
        base_overlay = json.load(handle)
    if not isinstance(base_overlay, dict):
        raise PhaseAContractError("phase-a.v5 v4 base is invalid")
    resolved = _resolve_v4_contract(base_overlay)
    excluded = tuple(str(value) for value in overlay.get("excluded_series_ids", []))
    if excluded != ("crude.sc.ine.main.settlement.cny_bbl",):
        raise PhaseAContractError("phase-a.v5 excluded series are invalid")
    resolved["contract_version"] = V5_CONTRACT_VERSION
    resolved["governance_version"] = overlay.get("governance_version")
    resolved["formal_nodes"] = overlay.get("formal_nodes")
    resolved["formal_evidence_series_ids"] = overlay.get("formal_evidence_series_ids")
    resolved["continuous_contract_series_ids"] = overlay.get("continuous_contract_series_ids")
    resolved["series"] = [
        series for series in resolved["series"] if str(_mapping(series).get("series_id")) not in set(excluded)
    ]
    for series in resolved["series"]:
        series["contract_version"] = V5_CONTRACT_VERSION
    return resolved


def _resolve_v6_contract(overlay: Mapping[str, Any]) -> dict[str, Any]:
    if (
        overlay.get("contract_id") != "phase-a"
        or overlay.get("contract_version") != V6_CONTRACT_VERSION
        or overlay.get("base_contract_version") != V5_CONTRACT_VERSION
        or overlay.get("base_contract_path") != V5_CONTRACT_PATH.name
    ):
        raise PhaseAContractError("phase-a.v6 overlay is invalid")
    with V5_CONTRACT_PATH.open(encoding="utf-8") as handle:
        base_overlay = json.load(handle)
    if not isinstance(base_overlay, dict):
        raise PhaseAContractError("phase-a.v6 v5 base is invalid")
    resolved = _resolve_v5_contract(base_overlay)
    resolved["contract_version"] = V6_CONTRACT_VERSION
    resolved["governance_version"] = overlay.get("governance_version")
    overrides = _mapping(overlay.get("series_overrides"))
    for series in resolved["series"]:
        series["contract_version"] = V6_CONTRACT_VERSION
        series.update(_mapping(overrides.get(series["series_id"])))
    return resolved


def _resolve_v7_contract(overlay: Mapping[str, Any]) -> dict[str, Any]:
    if (
        overlay.get("contract_id") != "phase-a"
        or overlay.get("contract_version") != EXPECTED_CONTRACT_VERSION
        or overlay.get("base_contract_version") != V6_CONTRACT_VERSION
        or overlay.get("base_contract_path") != V6_CONTRACT_PATH.name
    ):
        raise PhaseAContractError("phase-a.v7 overlay is invalid")
    with V6_CONTRACT_PATH.open(encoding="utf-8") as handle:
        base_overlay = json.load(handle)
    if not isinstance(base_overlay, dict):
        raise PhaseAContractError("phase-a.v7 v6 base is invalid")
    resolved = _resolve_v6_contract(base_overlay)
    resolved["contract_version"] = EXPECTED_CONTRACT_VERSION
    resolved["governance_version"] = overlay.get("governance_version")
    overrides = _mapping(overlay.get("series_overrides"))
    for series in resolved["series"]:
        series["contract_version"] = EXPECTED_CONTRACT_VERSION
        series.update(_mapping(overrides.get(series["series_id"])))
    return resolved


def load_source_registry(path: Path = SOURCE_REGISTRY_PATH) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, list):
        raise PhaseAContractError("source registry root must be a list")
    return {str(item["source_id"]): item for item in value}


def contract_errors(
    contract: Mapping[str, Any], source_registry: Mapping[str, Mapping[str, Any]] | None = None
) -> list[str]:
    errors: list[str] = []
    registry = source_registry if source_registry is not None else load_source_registry()

    if contract.get("contract_version") != EXPECTED_CONTRACT_VERSION:
        errors.append("contract_version must be phase-a.v7")
    if set(contract.get("formal_nodes", [])) != EXPECTED_FORMAL_NODES:
        errors.append("formal_nodes must exactly match the 14 active governance nodes")

    horizon_policy = _mapping(contract.get("horizon_policy"))
    if set(horizon_policy.get("formal_write_days", [])) != EXPECTED_FORMAL_HORIZONS:
        errors.append("formal_write_days must be exactly 1, 7 and 30")
    if set(horizon_policy.get("legacy_read_only_days", [])) != LEGACY_READ_ONLY_HORIZONS:
        errors.append("D+14 must be the only legacy read-only horizon")

    definition_fields = set(contract.get("series_definition_required_fields", []))
    observation_fields = set(contract.get("observation_required_fields", []))
    if not definition_fields:
        errors.append("series definition required fields are missing")
    if not observation_fields:
        errors.append("observation required fields are missing")

    source_policy = _mapping(contract.get("source_policy"))
    prohibited_sources = set(source_policy.get("prohibited_formal_sources", []))
    seen_ids: set[str] = set()
    covered_nodes: set[str] = set()
    for index, raw_series in enumerate(contract.get("series", [])):
        series = _mapping(raw_series)
        series_id = str(series.get("series_id", f"index:{index}"))
        missing = definition_fields - set(series)
        if missing:
            errors.append(f"{series_id}: missing definition fields {sorted(missing)}")
        if series_id in seen_ids:
            errors.append(f"duplicate series_id: {series_id}")
        seen_ids.add(series_id)

        node_id = str(series.get("node_id", ""))
        if node_id in EXPECTED_FORMAL_NODES:
            covered_nodes.add(node_id)
        elif node_id != "__auxiliary_fx__":
            errors.append(f"{series_id}: unknown node_id {node_id}")

        status = series.get("eligibility_status")
        authoritative_source = series.get("authoritative_source_id")
        fallback_sources = [str(source_id) for source_id in series.get("fallback_source_ids", [])]
        candidate_sources = [str(source_id) for source_id in series.get("candidate_source_ids", [])]
        context_sources = [str(source_id) for source_id in series.get("context_source_ids", [])]
        source_ids = [authoritative_source, *fallback_sources, *candidate_sources, *context_sources]
        source_ids = [str(source_id) for source_id in source_ids if source_id]
        unknown_sources = set(source_ids) - set(registry)
        retired_sources = unknown_sources & REMOVED_SOURCE_IDS
        if retired_sources and status not in BLOCKED_STATES:
            errors.append(f"{series_id}: removed sources cannot be contractible")
        # Frozen, already-blocked contracts may retain historical references.
        # This is not a registry entry, a replacement source or an eligibility approval.
        unknown_sources -= retired_sources
        if unknown_sources:
            errors.append(f"{series_id}: sources absent from registry {sorted(unknown_sources)}")
        forbidden_sources = set(source_ids) & prohibited_sources
        if forbidden_sources:
            errors.append(f"{series_id}: prohibited formal sources {sorted(forbidden_sources)}")
        expected_product = SOURCE_PRODUCT_BY_NODE.get(node_id)
        if expected_product:
            for source_id in source_ids:
                if source_id in retired_sources:
                    continue
                products = {str(product).casefold() for product in registry.get(source_id, {}).get("products", [])}
                if expected_product.casefold() not in products:
                    errors.append(f"{series_id}: source {source_id} does not cover product {expected_product}")
        if authoritative_source and str(authoritative_source) in candidate_sources:
            errors.append(f"{series_id}: authoritative source cannot also be a candidate")
        if set(fallback_sources) & set(candidate_sources):
            errors.append(f"{series_id}: fallback sources cannot also be candidates")

        fallback_proofs = _mapping(source_policy.get("fallback_quote_basis_evidence"))
        series_proofs = _mapping(fallback_proofs.get(series_id))
        for fallback_source in fallback_sources:
            proof = _mapping(series_proofs.get(fallback_source))
            expected_proof = {
                "quote_type": series.get("quote_type"),
                "market": series.get("market"),
                "instrument_or_grade": series.get("instrument_or_grade"),
            }
            if any(proof.get(field) != value for field, value in expected_proof.items()) or not proof.get(
                "evidence_ref"
            ):
                errors.append(f"{series_id}: fallback {fallback_source} lacks same-quote-basis evidence")

        if status in CONTRACTIBLE_STATES:
            if not series.get("authoritative_source_id"):
                errors.append(f"{series_id}: contractible series requires an authoritative source")
            for field in ("market", "instrument_or_grade", "raw_unit", "conversion_version", "timezone", "calendar_id"):
                if not series.get(field):
                    errors.append(f"{series_id}: contractible series requires {field}")
            if series.get("blocked_reason") is not None:
                errors.append(f"{series_id}: contractible series cannot carry blocked_reason")
        elif status in BLOCKED_STATES:
            if not series.get("blocked_reason"):
                errors.append(f"{series_id}: blocked series requires blocked_reason")
            if series.get("authoritative_source_id"):
                errors.append(f"{series_id}: blocked series cannot claim an authoritative source")
            if fallback_sources:
                errors.append(f"{series_id}: blocked series cannot carry fallback sources")
        else:
            errors.append(f"{series_id}: unknown eligibility_status {status}")

    formal_evidence_ids = tuple(str(value) for value in contract.get("formal_evidence_series_ids", []))
    series_ids = tuple(str(_mapping(item).get("series_id", "")) for item in contract.get("series", []))
    if formal_evidence_ids != series_ids:
        errors.append("formal_evidence_series_ids must exactly match the 19 active versioned series")
    non_prediction_ids = tuple(str(value) for value in contract.get("non_prediction_series_ids", []))
    fx_id = "fx.usd_cny.cfets.central_parity.cny_per_usd"
    if non_prediction_ids != (fx_id,):
        errors.append("CFETS must be the only non-prediction formal evidence series")
    fx = next((item for item in contract.get("series", []) if _mapping(item).get("series_id") == fx_id), None)
    if (
        _mapping(fx).get("formal_evidence_only") is not True
        or _mapping(fx).get("governance_role") != "conversion_evidence"
    ):
        errors.append("CFETS must remain formal conversion evidence only")

    if tuple(contract.get("continuous_contract_series_ids", [])) != CONTINUOUS_CONTRACT_SERIES_IDS:
        errors.append("continuous contract series ids must exactly match active ZCE/DCE futures")
    if _mapping(contract.get("continuous_contract_policy")) != CONTINUOUS_CONTRACT_POLICY:
        errors.append("continuous contract policy must match the frozen China futures rule")
    continuous_series = {
        str(_mapping(item).get("series_id", "")): _mapping(item) for item in contract.get("series", [])
    }
    for series_id in CONTINUOUS_CONTRACT_SERIES_IDS:
        series = continuous_series.get(series_id, {})
        if series.get("continuous_contract_policy_id") != CONTINUOUS_CONTRACT_POLICY_ID:
            errors.append(f"{series_id}: missing frozen continuous contract policy")
        if series.get("eligibility_status") != "blocked_evidence_capture_pending":
            errors.append(f"{series_id}: must remain evidence-capture blocked")
    for series_id, series in continuous_series.items():
        if (
            series_id not in CONTINUOUS_CONTRACT_SERIES_IDS
            and "continuous_contract_policy_id" in series
            and series.get("continuous_contract_policy_id") != INTERNATIONAL_FRONT_MONTH_POLICY["policy_id"]
        ):
            errors.append(f"{series_id}: must not inherit an unknown continuous contract policy")

    if _mapping(contract.get("public_source_personal_reuse_policy")) != PUBLIC_SOURCE_PERSONAL_REUSE_POLICY:
        errors.append("public source personal reuse policy must match the frozen project policy")
    if _mapping(contract.get("international_front_month_policy")) != INTERNATIONAL_FRONT_MONTH_POLICY:
        errors.append("international front month policy must match the frozen policy")
    if _mapping(contract.get("derived_cost_pressure_formula")) != DERIVED_COST_PRESSURE_FORMULA:
        errors.append("derived cost pressure formula must match the frozen formula")
    for series_id in INTERNATIONAL_FRONT_MONTH_SERIES_IDS:
        series = continuous_series.get(series_id, {})
        if series.get("continuous_contract_policy_id") != INTERNATIONAL_FRONT_MONTH_POLICY["policy_id"]:
            errors.append(f"{series_id}: missing frozen international front month policy")
    for series_id in DERIVED_COST_PRESSURE_SERIES_IDS:
        series = continuous_series.get(series_id, {})
        if series.get("derived_formula_id") != DERIVED_COST_PRESSURE_FORMULA["formula_id"]:
            errors.append(f"{series_id}: missing frozen cost pressure formula")

    coal_id = "coal.benchmark.unresolved.assessment.cny_mt"
    coal = continuous_series.get(coal_id, {})
    if coal.get("eligibility_status") != "blocked_evidence_capture_pending":
        errors.append(f"{coal_id}: must remain evidence-capture blocked")
    if coal.get("candidate_source_ids") != ["coalchina_cctd_bohai_rim_5500_daily_reference"]:
        errors.append(f"{coal_id}: must use the registered daily CCTD candidate only")
    if coal.get("context_source_ids") != ["cctd_qinhuangdao_thermal_coal"]:
        errors.append(f"{coal_id}: must retain CCTD only as weekly contextual evidence")
    daily_coal = _mapping(registry.get("coalchina_cctd_bohai_rim_5500_daily_reference"))
    if daily_coal.get("frequency") != "business_day":
        errors.append(f"{coal_id}: daily CCTD candidate must be business-day frequency")
    if daily_coal.get("products") != ["coal", "thermal_coal"]:
        errors.append(f"{coal_id}: daily CCTD candidate product scope is invalid")

    mx_id = "mx.domestic.spot_assessment.cny_mt"
    mx = continuous_series.get(mx_id, {})
    if mx.get("eligibility_status") != "blocked_evidence_capture_pending":
        errors.append(f"{mx_id}: must remain evidence-capture blocked")
    if mx.get("candidate_source_ids") != ["sunsirs_mx_east_china_daily_assessment"]:
        errors.append(f"{mx_id}: must use the registered East-China SunSirs candidate only")
    if mx.get("market") != "East_China":
        errors.append(f"{mx_id}: market must remain the source's East-China assessment market")
    daily_mx = _mapping(registry.get("sunsirs_mx_east_china_daily_assessment"))
    if daily_mx.get("frequency") != "business_day" or daily_mx.get("products") != ["mx"]:
        errors.append(f"{mx_id}: East-China SunSirs candidate scope is invalid")

    missing_nodes = EXPECTED_FORMAL_NODES - covered_nodes
    if missing_nodes:
        errors.append(f"formal nodes without a series definition: {sorted(missing_nodes)}")

    schemas = _mapping(contract.get("schemas"))
    expected_schemas = {"event", "fact_summary", "prediction", "experience_card"}
    if set(schemas) != expected_schemas:
        errors.append("schemas must exactly define event, fact_summary, prediction and experience_card")
    for schema_name, raw_schema in schemas.items():
        schema = _mapping(raw_schema)
        if not str(schema.get("schema_version", "")).startswith(f"phase-a.{schema_name.replace('_', '-')}.v1"):
            errors.append(f"{schema_name}: invalid schema_version")
        if not schema.get("required_fields"):
            errors.append(f"{schema_name}: required_fields must not be empty")

    return errors


def assert_contract_valid(
    contract: Mapping[str, Any] | None = None,
    source_registry: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    errors = contract_errors(contract or load_contract(), source_registry)
    if errors:
        raise PhaseAContractError("; ".join(errors))


def payload_errors(
    schema_name: str,
    payload: Mapping[str, Any],
    contract: Mapping[str, Any] | None = None,
) -> list[str]:
    active_contract = contract or load_contract()
    schemas = _mapping(active_contract.get("schemas"))
    if schema_name not in schemas:
        return [f"unknown schema: {schema_name}"]
    schema = _mapping(schemas[schema_name])
    errors: list[str] = []

    missing = set(schema.get("required_fields", [])) - set(payload)
    if missing:
        errors.append(f"missing required fields: {sorted(missing)}")
    if payload.get("schema_version") != schema.get("schema_version"):
        errors.append(f"schema_version must be {schema.get('schema_version')}")
    forbidden = set(schema.get("forbidden_fields", [])) & set(payload)
    if forbidden:
        errors.append(f"forbidden fields: {sorted(forbidden)}")

    field_enums = _mapping(schema.get("field_enums"))
    for field, allowed in field_enums.items():
        if field in payload and payload[field] not in allowed:
            errors.append(f"{field} must be one of {list(allowed)}")

    nested_fields = _mapping(schema.get("nested_required_fields"))
    nested_enums = _mapping(schema.get("nested_field_enums"))
    for collection_name, required_fields in nested_fields.items():
        value = payload.get(collection_name, [])
        if not _is_sequence(value):
            errors.append(f"{collection_name} must be a list")
            continue
        for index, item in enumerate(value):
            if not isinstance(item, Mapping):
                errors.append(f"{collection_name}[{index}] must be an object")
                continue
            nested_missing = set(required_fields) - set(item)
            if nested_missing:
                errors.append(f"{collection_name}[{index}] missing fields: {sorted(nested_missing)}")
            errors.extend(
                f"{collection_name}[{index}]: {error}"
                for error in _field_enum_errors(item, _mapping(nested_enums.get(collection_name)))
            )

    if schema_name == "prediction":
        errors.extend(_prediction_grid_errors(payload, active_contract))
    if (
        schema_name == "experience_card"
        and payload.get("visibility_mode") == "reconstructed"
        and payload.get("scoreability") != "unscorable"
    ):
        errors.append("reconstructed experience cards must be unscorable")
    return errors


def assert_payload_valid(
    schema_name: str, payload: Mapping[str, Any], contract: Mapping[str, Any] | None = None
) -> None:
    errors = payload_errors(schema_name, payload, contract)
    if errors:
        raise PhaseAContractError("; ".join(errors))


def prediction_horizon_errors(horizon_days: int, *, write: bool) -> list[str]:
    if horizon_days in EXPECTED_FORMAL_HORIZONS:
        return []
    if not write and horizon_days in LEGACY_READ_ONLY_HORIZONS:
        return []
    if horizon_days in LEGACY_READ_ONLY_HORIZONS:
        return ["D+14 is historical read-only and cannot be written"]
    return ["formal prediction horizon must be D+1, D+7 or D+30"]


def experience_transition_errors(previous_stage: str, next_stage: str) -> list[str]:
    order = {"d1_preliminary": 1, "d7_intermediate": 2, "d30_mature": 3}
    if previous_stage not in order or next_stage not in order:
        return ["unknown experience maturity stage"]
    if order[next_stage] < order[previous_stage]:
        return ["experience maturity cannot move backwards"]
    return []


def _prediction_grid_errors(payload: Mapping[str, Any], contract: Mapping[str, Any]) -> list[str]:
    cells = payload.get("cells", [])
    if not _is_sequence(cells):
        return []
    actual: list[tuple[str, int]] = []
    errors: list[str] = []
    prediction_schema = _mapping(_mapping(contract.get("schemas")).get("prediction"))
    subtarget_required_fields = set(prediction_schema.get("subtarget_required_fields", []))
    subtarget_enums = _mapping(prediction_schema.get("subtarget_field_enums"))
    valid_nodes = {str(node) for node in contract.get("formal_nodes", [])}
    for index, item in enumerate(cells):
        if not isinstance(item, Mapping):
            continue
        node_id = str(item.get("node_id", ""))
        horizon = item.get("horizon_days")
        if node_id not in valid_nodes:
            errors.append(f"cells[{index}] has unknown node_id {node_id}")
        if not isinstance(horizon, int):
            errors.append(f"cells[{index}] horizon_days must be an integer")
            continue
        errors.extend(f"cells[{index}]: {error}" for error in prediction_horizon_errors(horizon, write=True))
        subtargets = item.get("subtarget_results", [])
        if not _is_sequence(subtargets):
            errors.append(f"cells[{index}].subtarget_results must be a list")
        elif node_id == "poy_dty_upstream_cost_pressure":
            targets: list[str] = []
            for subtarget_index, subtarget in enumerate(subtargets):
                if not isinstance(subtarget, Mapping):
                    errors.append(f"cells[{index}].subtarget_results[{subtarget_index}] must be an object")
                    continue
                missing = subtarget_required_fields - set(subtarget)
                if missing:
                    errors.append(
                        f"cells[{index}].subtarget_results[{subtarget_index}] missing fields: {sorted(missing)}"
                    )
                errors.extend(
                    f"cells[{index}].subtarget_results[{subtarget_index}]: {error}"
                    for error in _field_enum_errors(subtarget, subtarget_enums)
                )
                targets.append(str(subtarget.get("target", "")))
            if len(targets) != 2 or set(targets) != {"poy", "dty"}:
                errors.append(f"cells[{index}] must contain exactly one POY and one DTY subtarget result")
        elif subtargets:
            errors.append(f"cells[{index}] non-terminal node cannot contain subtarget results")
        actual.append((node_id, horizon))
    expected = {(node, horizon) for node in valid_nodes for horizon in EXPECTED_FORMAL_HORIZONS}
    if len(actual) != len(set(actual)):
        errors.append("prediction grid contains duplicate node/horizon cells")
    if set(actual) != expected:
        errors.append(
            f"prediction grid must contain exactly {len(valid_nodes)} nodes x D+1/D+7/D+30"
        )
    return errors


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _field_enum_errors(payload: Mapping[str, Any], field_enums: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field, allowed in field_enums.items():
        if field in payload and payload[field] not in allowed:
            errors.append(f"{field} must be one of {list(allowed)}")
    return errors


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))

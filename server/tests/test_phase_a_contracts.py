from __future__ import annotations

from copy import deepcopy

import pytest

from app.phase_a_contracts import (
    BLOCKED_STATES,
    CONTINUOUS_CONTRACT_POLICY,
    CONTINUOUS_CONTRACT_POLICY_ID,
    CONTINUOUS_CONTRACT_SERIES_IDS,
    CONTRACTIBLE_STATES,
    DERIVED_COST_PRESSURE_FORMULA,
    DERIVED_COST_PRESSURE_SERIES_IDS,
    EXPECTED_FORMAL_HORIZONS,
    EXPECTED_FORMAL_NODES,
    INTERNATIONAL_FRONT_MONTH_POLICY,
    INTERNATIONAL_FRONT_MONTH_SERIES_IDS,
    PhaseAContractError,
    assert_contract_valid,
    assert_payload_valid,
    contract_errors,
    experience_transition_errors,
    load_contract,
    load_source_registry,
    payload_errors,
    prediction_horizon_errors,
)


@pytest.fixture(scope="module")
def contract():
    return load_contract()


def _subtarget_result(target: str) -> dict[str, object]:
    return {
        "target": target,
        "direction": "uncertain",
        "direction_probability": None,
        "magnitude": None,
        "magnitude_unit": None,
        "confidence": None,
        "remaining_effective_probability": None,
        "data_completeness": None,
        "scoreability": "unscorable",
        "missing_series_ids": [],
    }


def _valid_prediction_payload(contract) -> dict[str, object]:
    schema = contract["schemas"]["prediction"]
    cell_fields = schema["nested_required_fields"]["cells"]
    cells = []
    for node in contract["formal_nodes"]:
        for horizon in contract["horizon_policy"]["formal_write_days"]:
            cell = {field: None for field in cell_fields}
            cell.update(
                node_id=node,
                horizon_days=horizon,
                direction="uncertain",
                scoreability="unscorable",
                driver_event_ids=[],
                counter_event_ids=[],
                missing_series_ids=[],
                subtarget_results=(
                    [_subtarget_result("poy"), _subtarget_result("dty")]
                    if node == "poy_dty_upstream_cost_pressure"
                    else []
                ),
            )
            cells.append(cell)
    payload = {field: None for field in schema["required_fields"]}
    payload.update(schema_version=schema["schema_version"], cells=cells)
    return payload


def test_contract_is_valid_against_read_only_source_registry(contract) -> None:
    assert_contract_valid(contract, load_source_registry())


def test_nodes_horizons_and_series_ids_are_frozen(contract) -> None:
    assert contract["contract_version"] == "phase-a.v7"
    assert set(contract["formal_nodes"]) == EXPECTED_FORMAL_NODES
    assert set(contract["horizon_policy"]["formal_write_days"]) == EXPECTED_FORMAL_HORIZONS
    assert contract["horizon_policy"]["legacy_read_only_days"] == [14]
    series_ids = [item["series_id"] for item in contract["series"]]
    assert len(series_ids) == len(set(series_ids))
    assert "crude.sc.ine.main.settlement.cny_bbl" not in series_ids
    assert len(contract["formal_nodes"]) == 14
    assert len(series_ids) == 19


def test_every_series_and_observation_envelope_has_required_fields(contract) -> None:
    definition_fields = set(contract["series_definition_required_fields"])
    assert all(definition_fields <= set(item) for item in contract["series"])
    assert {
        "raw_content_hash",
        "first_visible_at",
        "source_published_at",
        "parser_version",
        "authorization_status",
        "license_status",
        "raw_value",
        "standard_value",
        "conversion_version",
        "exchange_trade_date",
    } <= set(contract["observation_required_fields"])


def test_blocked_series_fail_closed_without_invented_specs_or_sources(contract) -> None:
    by_node = {}
    for item in contract["series"]:
        by_node.setdefault(item["node_id"], []).append(item)
    for node in {"coal", "mx", "ethylene", "eo", "polyester_melt", "polyester_chip"}:
        assert all(item["eligibility_status"] in BLOCKED_STATES for item in by_node[node])
        assert all(item["authoritative_source_id"] is None for item in by_node[node])
        assert all(item["blocked_reason"] for item in by_node[node])
    for item in by_node["poy_dty_upstream_cost_pressure"]:
        assert item["eligibility_status"] == "blocked_evidence_capture_pending"
        assert item["conversion_formula"] is None
    formal_series = [item for item in contract["series"] if item["node_id"] in EXPECTED_FORMAL_NODES]
    assert all(item["eligibility_status"] in BLOCKED_STATES for item in formal_series)
    assert all(item["authoritative_source_id"] is None for item in formal_series)
    assert all(item["fallback_source_ids"] == [] for item in formal_series)


def test_contractible_series_use_registered_non_proxy_sources(contract) -> None:
    registry = load_source_registry()
    prohibited = set(contract["source_policy"]["prohibited_formal_sources"])
    for item in contract["series"]:
        if item["eligibility_status"] not in CONTRACTIBLE_STATES:
            continue
        source_ids = {item["authoritative_source_id"], *item["fallback_source_ids"]}
        assert source_ids <= set(registry)
        assert source_ids.isdisjoint(prohibited)
    assert [item["series_id"] for item in contract["series"] if item["eligibility_status"] in CONTRACTIBLE_STATES] == [
        "fx.usd_cny.cfets.central_parity.cny_per_usd"
    ]


def test_china_futures_share_the_frozen_main_continuous_policy(contract) -> None:
    assert tuple(contract["continuous_contract_series_ids"]) == CONTINUOUS_CONTRACT_SERIES_IDS
    assert contract["continuous_contract_policy"] == CONTINUOUS_CONTRACT_POLICY
    by_id = {item["series_id"]: item for item in contract["series"]}
    for series_id in CONTINUOUS_CONTRACT_SERIES_IDS:
        series = by_id[series_id]
        assert series["continuous_contract_policy_id"] == CONTINUOUS_CONTRACT_POLICY_ID
        assert series["eligibility_status"] == "blocked_evidence_capture_pending"
    assert by_id["fx.usd_cny.cfets.central_parity.cny_per_usd"].get("continuous_contract_policy_id") is None


def test_authorized_ccf_series_are_capture_blocked_not_license_blocked(contract) -> None:
    by_id = {item["series_id"]: item for item in contract["series"]}
    for series_id in (
        "naphtha.ccf.domestic.daily_assessment.cny_mt",
        "px.ccf.domestic.daily_assessment.cny_mt",
        "pta.ccf.domestic.daily_assessment.cny_mt",
        "meg.ccf.domestic.daily_assessment.cny_mt",
    ):
        series = by_id[series_id]
        assert series["eligibility_status"] == "blocked_evidence_capture_pending"
        assert series["instrument_or_grade"] == "CCF authorized daily basis"
        assert series["raw_unit"] == series["standard_unit"] == "CNY/mt"
        assert series["calendar_id"] == "CCF_business_day"
        assert series["authoritative_source_id"] is None


def test_cctd_coal_daily_candidate_remains_evidence_capture_blocked(contract) -> None:
    registry = load_source_registry()
    coal = next(
        item
        for item in contract["series"]
        if item["series_id"] == "coal.benchmark.unresolved.assessment.cny_mt"
    )

    assert coal["eligibility_status"] == "blocked_evidence_capture_pending"
    assert coal["candidate_source_ids"] == ["coalchina_cctd_bohai_rim_5500_daily_reference"]
    assert coal["context_source_ids"] == ["cctd_qinhuangdao_thermal_coal"]
    assert registry["cctd_qinhuangdao_thermal_coal"]["frequency"] == "weekly"
    assert registry["cctd_qinhuangdao_thermal_coal"]["freshness_sla_minutes"] == 10080
    assert registry["coalchina_cctd_bohai_rim_5500_daily_reference"]["frequency"] == "business_day"
    assert registry["coalchina_cctd_bohai_rim_5500_daily_reference"]["products"] == ["coal", "thermal_coal"]

    mx = next(item for item in contract["series"] if item["series_id"] == "mx.domestic.spot_assessment.cny_mt")
    assert mx["eligibility_status"] == "blocked_evidence_capture_pending"
    assert mx["market"] == "East_China"
    assert mx["candidate_source_ids"] == ["sunsirs_mx_east_china_daily_assessment"]
    assert registry["sunsirs_mx_east_china_daily_assessment"]["frequency"] == "business_day"
    assert registry["sunsirs_mx_east_china_daily_assessment"]["products"] == ["mx"]

    broken = deepcopy(contract)
    broken_coal = next(item for item in broken["series"] if item["series_id"] == coal["series_id"])
    broken_coal["candidate_source_ids"] = ["cctd_qinhuangdao_thermal_coal"]
    errors = contract_errors(broken, registry)
    assert any("registered daily CCTD candidate only" in error for error in errors)


def test_delegated_public_source_and_cost_pressure_policies_are_frozen(contract) -> None:
    assert contract["public_source_personal_reuse_policy"] == {
        "policy_id": "public-source-personal-reuse.v1",
        "permitted_use": "personal_internal_analysis_and_non_reversible_public_derived_conclusions",
        "source_attribution_required": True,
        "immutable_capture_evidence_required": True,
        "raw_data_redistribution": "prohibited",
    }
    assert contract["international_front_month_policy"] == INTERNATIONAL_FRONT_MONTH_POLICY
    assert contract["derived_cost_pressure_formula"] == DERIVED_COST_PRESSURE_FORMULA
    by_id = {item["series_id"]: item for item in contract["series"]}
    for series_id in INTERNATIONAL_FRONT_MONTH_SERIES_IDS:
        assert by_id[series_id]["continuous_contract_policy_id"] == "international-front-month.v1"
    for series_id in DERIVED_COST_PRESSURE_SERIES_IDS:
        assert by_id[series_id]["derived_formula_id"] == "poy-dty-upstream-cost-pressure.v1"


def test_quote_basis_isolated_by_series_id_and_not_cross_fallback(contract) -> None:
    by_id = {item["series_id"]: item for item in contract["series"]}
    for product, exchange in (("px", "czce"), ("pta", "czce"), ("meg", "dce")):
        settlement = by_id[f"{product}.{exchange}.main.settlement.cny_mt"]
        assessment = by_id[f"{product}.ccf.domestic.daily_assessment.cny_mt"]
        assert settlement["quote_type"] == "futures_settlement"
        assert assessment["quote_type"] == "licensed_spot_assessment"
        assert not settlement["fallback_source_ids"]
        assert not assessment["fallback_source_ids"]
        assert set(settlement["candidate_source_ids"]).isdisjoint(assessment["candidate_source_ids"])


def test_standard_units_follow_frozen_policy(contract) -> None:
    for item in contract["series"]:
        if item["node_id"] in {"brent", "wti"}:
            assert (item["standard_unit"], item["standard_currency"]) == ("USD/bbl", "USD")
        elif item["node_id"] not in {"poy_dty_upstream_cost_pressure", "__auxiliary_fx__"}:
            assert (item["standard_unit"], item["standard_currency"]) == ("CNY/mt", "CNY")
    assert all(item["node_id"] != "sc" for item in contract["series"])


def test_static_validator_rejects_unknown_and_prohibited_sources(contract) -> None:
    broken = deepcopy(contract)
    brent = next(item for item in broken["series"] if item["node_id"] == "brent")
    brent["candidate_source_ids"] = ["yahoo_futures_daily_proxy", "not_registered"]
    errors = contract_errors(broken, load_source_registry())
    assert any("prohibited formal sources" in error for error in errors)
    assert any("absent from registry" in error for error in errors)


def test_blocked_series_cannot_carry_fallback_even_when_registered(contract) -> None:
    broken = deepcopy(contract)
    coal = next(item for item in broken["series"] if item["node_id"] == "coal")
    coal["fallback_source_ids"] = ["gacc_trade_statistics"]
    errors = contract_errors(broken, load_source_registry())
    assert any("blocked series cannot carry fallback" in error for error in errors)


def test_candidate_source_must_cover_the_series_product(contract) -> None:
    broken = deepcopy(contract)
    brent = next(item for item in broken["series"] if item["node_id"] == "brent")
    brent["candidate_source_ids"] = ["cfets_cny_parity"]
    errors = contract_errors(broken, load_source_registry())
    assert any("does not cover product crude_oil" in error for error in errors)


def test_fallback_requires_per_source_same_quote_basis_evidence(contract) -> None:
    broken = deepcopy(contract)
    fx = next(item for item in broken["series"] if item["node_id"] == "__auxiliary_fx__")
    fx["fallback_source_ids"] = ["cfets_cny_parity"]
    errors = contract_errors(broken, load_source_registry())
    assert any("lacks same-quote-basis evidence" in error for error in errors)


def test_four_versioned_schemas_expose_required_fields(contract) -> None:
    schemas = contract["schemas"]
    assert set(schemas) == {"event", "fact_summary", "prediction", "experience_card"}
    assert all(schema["schema_version"].endswith(".v1") for schema in schemas.values())
    assert all(schema["required_fields"] for schema in schemas.values())
    assert {"direction", "direction_probability", "trend"} <= set(schemas["fact_summary"]["forbidden_fields"])


def test_fact_summary_rejects_prediction_fields(contract) -> None:
    schema = contract["schemas"]["fact_summary"]
    payload = {field: None for field in schema["required_fields"]}
    payload["schema_version"] = schema["schema_version"]
    payload["facts"] = []
    payload["direction"] = "up"
    assert any("forbidden fields" in error for error in payload_errors("fact_summary", payload, contract))


def test_prediction_requires_complete_14_by_3_grid(contract) -> None:
    payload = _valid_prediction_payload(contract)
    assert_payload_valid("prediction", payload, contract)
    payload["cells"].pop()
    with pytest.raises(PhaseAContractError, match="14 nodes"):
        assert_payload_valid("prediction", payload, contract)


def test_prediction_rejects_invalid_nested_direction_and_scoreability(contract) -> None:
    payload = _valid_prediction_payload(contract)
    payload["cells"][0]["direction"] = "INVALID"
    payload["cells"][0]["scoreability"] = "INVALID"
    errors = payload_errors("prediction", payload, contract)
    assert any("cells[0]: direction must be one of" in error for error in errors)
    assert any("cells[0]: scoreability must be one of" in error for error in errors)


def test_terminal_prediction_cell_requires_exact_poy_and_dty_subtargets(contract) -> None:
    payload = _valid_prediction_payload(contract)
    terminal = next(cell for cell in payload["cells"] if cell["node_id"] == "poy_dty_upstream_cost_pressure")
    terminal["subtarget_results"] = [_subtarget_result("poy"), _subtarget_result("poy")]
    errors = payload_errors("prediction", payload, contract)
    assert any("exactly one POY and one DTY" in error for error in errors)


def test_d14_is_read_only_and_other_invented_horizons_are_rejected() -> None:
    assert prediction_horizon_errors(14, write=False) == []
    assert "read-only" in prediction_horizon_errors(14, write=True)[0]
    assert prediction_horizon_errors(1, write=True) == []
    assert prediction_horizon_errors(7, write=True) == []
    assert prediction_horizon_errors(30, write=True) == []
    assert prediction_horizon_errors(3, write=True)


def test_reconstructed_experience_is_not_strictly_scorable(contract) -> None:
    schema = contract["schemas"]["experience_card"]
    payload = {field: None for field in schema["required_fields"]}
    payload.update(
        schema_version=schema["schema_version"],
        maturity_stage="d1_preliminary",
        visibility_mode="reconstructed",
        mechanism_support_status="inconclusive",
        scoreability="scorable",
    )
    assert any("must be unscorable" in error for error in payload_errors("experience_card", payload, contract))
    payload["scoreability"] = "unscorable"
    assert_payload_valid("experience_card", payload, contract)


def test_experience_maturity_cannot_move_backwards() -> None:
    assert experience_transition_errors("d1_preliminary", "d7_intermediate") == []
    assert experience_transition_errors("d7_intermediate", "d30_mature") == []
    assert experience_transition_errors("d30_mature", "d7_intermediate")


def test_removed_source_cannot_be_promoted_by_historical_contract_compatibility(contract):
    import copy
    changed = copy.deepcopy(contract)
    series = next(row for row in changed['series'] if row['series_id'] == 'meg.dce.main.settlement.cny_mt')
    series['eligibility_status'] = 'contractible'
    assert any('removed sources cannot be contractible' in error for error in contract_errors(changed))

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.source_automation_policy import (
    SOURCE_AUTOMATION_POLICIES,
    SourceCapability,
    assess_readiness,
    get_source_policy,
    select_due_sources,
)

NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def test_policy_centralizes_non_ccf_capability_and_scheduling_metadata() -> None:
    assert "ccf_dom_daily" not in SOURCE_AUTOMATION_POLICIES
    assert set(SOURCE_AUTOMATION_POLICIES) == {
        "akshare_prototype",
        "coalchina_cctd_bohai_rim_5500_daily_reference",
        "cfets_cny_parity",
        "cftc_cot_petroleum",
        "czce_pta_px",
        "eia_petroleum_api",
        "fred_macro_api",
        "gacc_trade_statistics",
        "ine_sc_intraday",
        "internal_market_notes",
        "ofac_sanctions",
        "opec_press",
        "sunsirs_mx_east_china_daily_assessment",
        "tnc_polyester_history",
        "un_comtrade_api",
        "yahoo_futures_daily_proxy",
    }

    eia = get_source_policy("eia_petroleum_api")
    assert eia.capability is SourceCapability.ADAPTER
    assert eia.production_schedulable is True
    assert eia.timeout_seconds > 0
    assert eia.frequency_seconds > 0
    assert eia.sla_seconds >= eia.frequency_seconds

    assert get_source_policy("ine_sc_intraday").capability is SourceCapability.DIRECT_DOWNLOAD
    assert get_source_policy("czce_pta_px").capability is SourceCapability.ADAPTER
    assert get_source_policy("czce_pta_px").production_schedulable is True
    assert get_source_policy("cfets_cny_parity").capability is SourceCapability.ADAPTER
    assert get_source_policy("cfets_cny_parity").production_schedulable is True
    assert get_source_policy("coalchina_cctd_bohai_rim_5500_daily_reference").capability is SourceCapability.ADAPTER
    assert get_source_policy("coalchina_cctd_bohai_rim_5500_daily_reference").production_schedulable is False
    assert get_source_policy("sunsirs_mx_east_china_daily_assessment").capability is SourceCapability.MANUAL
    assert get_source_policy("sunsirs_mx_east_china_daily_assessment").production_schedulable is False
    for source_id in ("opec_press", "ofac_sanctions", "gacc_trade_statistics", "un_comtrade_api"):
        assert get_source_policy(source_id).capability is SourceCapability.ADAPTER
        assert get_source_policy(source_id).production_schedulable is True


def test_select_due_sources_only_returns_schedulable_adapters_in_priority_order() -> None:
    states = {
        "eia_petroleum_api": {
            "last_run_status": "success",
            "last_success_at": NOW - timedelta(hours=4),
            "latest_observed_at": NOW - timedelta(hours=4),
        },
        "fred_macro_api": {
            "last_run_status": "success",
            "last_success_at": NOW - timedelta(minutes=30),
            "latest_observed_at": NOW - timedelta(minutes=30),
        },
        "cftc_cot_petroleum": {},
        "ine_sc_intraday": {},
        "gacc_trade_statistics": {},
    }

    due = select_due_sources(states, now=NOW)

    assert [policy.source_id for policy in due] == [
        "eia_petroleum_api",
        "cfets_cny_parity",
        "cftc_cot_petroleum",
        "czce_pta_px",
        "gacc_trade_statistics",
        "ofac_sanctions",
        "opec_press",
        "tnc_polyester_history",
        "un_comtrade_api",
    ]


def test_configured_source_without_adapter_is_not_ready() -> None:
    result = assess_readiness(
        "un_comtrade_api",
        {
            "configured": True,
            "adapter": False,
            "last_run_status": "success",
            "last_success_at": NOW - timedelta(minutes=5),
            "latest_observed_at": NOW - timedelta(minutes=5),
        },
        now=NOW,
    )

    assert result.ready is False
    assert "adapter_unavailable" in result.reasons


def test_readiness_requires_successful_run_and_fresh_success_and_observation() -> None:
    ready = assess_readiness(
        "eia_petroleum_api",
        {
            "adapter": True,
            "last_run_status": "success",
            "last_success_at": NOW - timedelta(minutes=20),
            "latest_observed_at": NOW - timedelta(minutes=10),
        },
        now=NOW,
    )
    failed = assess_readiness(
        "eia_petroleum_api",
        {
            "adapter": True,
            "last_run_status": "failed",
            "last_success_at": NOW - timedelta(minutes=20),
            "latest_observed_at": NOW - timedelta(minutes=10),
        },
        now=NOW,
    )
    stale = assess_readiness(
        "eia_petroleum_api",
        {
            "adapter": True,
            "last_run_status": "success",
            "last_success_at": NOW - timedelta(hours=4),
            "latest_observed_at": NOW - timedelta(minutes=10),
        },
        now=NOW,
    )

    assert ready.ready is True
    assert failed.ready is False
    assert "last_run_not_successful" in failed.reasons
    assert stale.ready is False
    assert "last_success_outside_sla" in stale.reasons


def test_no_new_data_is_a_successful_source_check() -> None:
    result = assess_readiness(
        "eia_petroleum_api",
        {
            "adapter": True,
            "last_run_status": "no_new_data",
            "last_success_at": NOW - timedelta(minutes=20),
            "latest_observed_at": NOW - timedelta(days=1),
        },
        now=NOW,
    )

    assert result.ready is True
    assert "last_run_not_successful" not in result.reasons


def test_stale_running_is_not_ready_or_mistaken_for_an_active_run() -> None:
    result = assess_readiness(
        "eia_petroleum_api",
        {
            "adapter": True,
            "last_run_status": "running",
            "last_run_started_at": NOW - timedelta(minutes=20),
            "last_success_at": NOW - timedelta(minutes=30),
            "latest_observed_at": NOW - timedelta(minutes=30),
        },
        now=NOW,
    )

    assert result.ready is False
    assert result.stale_running is True
    assert "stale_running" in result.reasons


def test_missing_operational_evidence_is_not_ready_even_when_configured() -> None:
    result = assess_readiness("fred_macro_api", {"configured": True, "adapter": True}, now=NOW)

    assert result.ready is False
    assert set(result.reasons) >= {
        "last_run_status_missing",
        "last_success_missing",
        "latest_observation_missing",
    }


def test_fred_observation_sla_tolerates_weekend_but_not_a_missed_week() -> None:
    monday = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    weekend_state = {
        "adapter": True,
        "last_run_status": "success",
        "last_success_at": monday - timedelta(minutes=10),
        "latest_observed_at": datetime(2026, 8, 28, 0, 0, tzinfo=UTC),
    }
    stale_state = {
        **weekend_state,
        "latest_observed_at": monday - timedelta(days=7, seconds=1),
    }

    weekend = assess_readiness("fred_macro_api", weekend_state, now=monday)
    stale = assess_readiness("fred_macro_api", stale_state, now=monday)

    assert weekend.ready is True
    assert "latest_observation_outside_sla" not in weekend.reasons
    assert stale.ready is False
    assert "latest_observation_outside_sla" in stale.reasons


def test_daily_price_publishers_rechecked_after_intraday_publication():
    from datetime import UTC, datetime

    from app.source_automation_policy import assess_readiness, get_source_policy
    for source_id in ('czce_pta_px', 'tnc_polyester_history'):
        policy = get_source_policy(source_id)
        assert policy.frequency_seconds == 3600
        state = {'last_run_status': 'ok', 'last_run_started_at': '2026-09-14T06:00:00Z',
                 'last_success_at': '2026-09-14T06:01:00Z', 'latest_observed_at': '2026-09-11'}
        assert assess_readiness(policy, state, now=datetime(2026, 9, 14, 7, 1, tzinfo=UTC)).due

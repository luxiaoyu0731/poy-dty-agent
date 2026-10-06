from copy import deepcopy

import pytest
from scripts.experiments.cached_replay_port import digest
from scripts.experiments.frozen_settlement import LEGACY_POLICY
from scripts.experiments.paired_results import assemble_pair, settle_rehearsal
from test_frozen_chain_replay import bundle
from test_frozen_settlement import issued, observations


def rehearsal():
    data = bundle()
    identity = digest(data)
    rows = [
        {
            "business_date": data["business_date"],
            "target": "crude",
            "horizon_days": h,
            "baseline_direction": "down",
            "event_adjusted_direction": "down",
            "fusion_rule": "R3",
        }
        for h in (1, 7, 30)
    ]
    report = {"status": "ok", "business_date": data["business_date"], "experiment": {"input_sha256": identity}}
    packet = {
        "input_sha256": identity,
        "business_date": data["business_date"],
        "arms": {arm: {"chain_report": deepcopy(report), "fused_rows": deepcopy(rows)} for arm in ("control", "rag")},
    }
    packet["arms"]["rag"]["fused_rows"][1]["event_adjusted_direction"] = "up"
    return data, packet


def settle(data, packet, known_at="2025-02-15T00:00:00Z"):
    return settle_rehearsal(
        packet,
        bundle=data,
        issued_contracts={h: issued(h) for h in (1, 7, 30)},
        prices=observations(),
        known_at=known_at,
        policy=LEGACY_POLICY,
    )


def test_both_arms_use_same_labels_despite_different_predictions():
    data, packet = rehearsal()
    result = settle(data, packet)
    old, new = result["arms"]["control"], result["arms"]["rag"]
    assert old["status"] == new["status"] == "ok"
    assert old["cells"][1]["adjusted_direction"] != new["cells"][1]["adjusted_direction"]
    assert old["cells"][1]["label_input_sha256"] == new["cells"][1]["label_input_sha256"]
    assert old["cells"][1]["actual_direction"] == new["cells"][1]["actual_direction"]
    control, rag = assemble_pair([result], manifest={"settlement_policy": LEGACY_POLICY})
    assert not control["recall_voting"] and rag["recall_voting"]
    assert result["acceptance_complete"] is False


def test_pending_horizons_are_retained_instead_of_silently_reducing_sample():
    data, packet = rehearsal()
    result = settle(data, packet, "2025-01-10T00:00:00Z")
    for arm in result["arms"].values():
        assert arm["status"] == "incomplete" and len(arm["cells"]) == 3
        assert arm["cells"][2]["settlement_status"] == "pending_maturity"
        assert "actual_direction" not in arm["cells"][2]


def test_changed_inputs_incomplete_pairs_and_duplicate_dates_fail_closed():
    data, packet = rehearsal()
    changed = deepcopy(data)
    changed["signal_report"]["candidates"][0]["title"] = "changed"
    with pytest.raises(ValueError, match="input_changed"):
        settle(changed, packet)
    broken = deepcopy(packet)
    broken["arms"]["rag"]["fused_rows"].pop()
    with pytest.raises(ValueError, match="horizon_rows"):
        settle(data, broken)
    result = settle(data, packet)
    with pytest.raises(ValueError, match="duplicate_paired_date"):
        assemble_pair([result, result], manifest={"settlement_policy": LEGACY_POLICY})
    result["arms"]["rag"]["cells"][0]["actual_change_pct"] = 999
    with pytest.raises(ValueError, match="paired_result_changed"):
        assemble_pair([result], manifest={"settlement_policy": LEGACY_POLICY})


def test_incomplete_pair_cannot_attach_a_full_ranked_cohort_receipt():
    from test_replay_cohort import receipt

    cohort = receipt()
    data, packet = rehearsal()
    result = settle(data, packet)
    manifest = {
        "settlement_policy": LEGACY_POLICY,
        "data_sha256": cohort["data_sha256"],
        "sample_sha256": cohort["sample_sha256"],
        "cohort_sha256": cohort["receipt_sha256"],
    }
    with pytest.raises(ValueError, match="assembled_pair_cohort_mismatch"):
        assemble_pair([result], manifest=manifest, cohort_receipt=cohort)


def test_sealed_pair_still_requires_per_date_chain_input_binding():
    data, packet = rehearsal()
    result = settle(data, packet)
    result["arms"]["rag"]["chain_report"]["business_date"] = "2025-01-03"
    result["result_sha256"] = digest({key: value for key, value in result.items() if key != "result_sha256"})
    with pytest.raises(ValueError, match="assembled_pair_input_binding_mismatch"):
        assemble_pair([result], manifest={"settlement_policy": LEGACY_POLICY})

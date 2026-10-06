"""Chronological, frozen-input reflection A/B; no database or production writes."""

from __future__ import annotations

from copy import deepcopy

from scripts.experiments.cached_replay_port import digest
from scripts.experiments.frozen_chain_replay import FrozenReplayChain, validate_input
from scripts.experiments.frozen_settlement import settle_frozen_cell
from scripts.experiments.paired_results import LABEL_FIELDS
from scripts.experiments.reflection_protocol import POLICY
from scripts.experiments.reflection_timeline import ReflectionTimeline


def contracts_for(item: dict) -> dict:
    raw = item["issued_contracts"]
    if not isinstance(raw, dict):
        raise ValueError("reflection_complete_crude_contracts_required")
    # Frozen files use JSON object keys, which round-trip as strings. Accept
    # exactly those three spellings, but reject ambiguous mixed encodings.
    contracts = {}
    for key, value in raw.items():
        if type(key) is int and key in (1, 7, 30):
            horizon = key
        elif type(key) is str and key in ("1", "7", "30"):
            horizon = int(key)
        else:
            raise ValueError("reflection_complete_crude_contracts_required")
        if horizon in contracts:
            raise ValueError("reflection_duplicate_contract_horizon")
        contracts[horizon] = value
    if set(contracts) != {1, 7, 30}:
        raise ValueError("reflection_complete_crude_contracts_required")
    bundle = item["bundle"]
    for h, contract in contracts.items():
        if (
            contract["business_date"] != bundle["business_date"]
            or contract["as_of_time"] != bundle["as_of_time"]
            or contract["target"] != "crude"
            or contract["horizon_days"] != h
            or contract["baseline_direction"]
            != bundle["baseline_by_product"]["crude"][f"d{h}"]["direction"]
        ):
            raise ValueError("reflection_frozen_contract_mismatch")
    return contracts


def labels_for(item: dict, rows: list[dict], *, known_at: str) -> list[dict]:
    contracts = contracts_for(item)
    crude = [row for row in rows if row["target"] == "crude"]
    if len(crude) != 3 or {row["horizon_days"] for row in crude} != {1, 7, 30}:
        raise ValueError("reflection_complete_crude_rows_required")
    labels = []
    for row in sorted(crude, key=lambda cell: cell["horizon_days"]):
        contract = deepcopy(contracts[row["horizon_days"]])
        if row["baseline_direction"] != contract["baseline_direction"]:
            raise ValueError("reflection_baseline_changed")
        if (
            row["horizon_days"] == 1
            and row["event_adjusted_direction"] != row["baseline_direction"]
        ):
            raise ValueError("reflection_o1_changed")
        contract["event_adjusted_direction"] = row["event_adjusted_direction"]
        label = settle_frozen_cell(
            contract, item["prices"], known_at=known_at, policy=POLICY
        )
        shared = {k: v for k, v in contract.items() if k != "event_adjusted_direction"}
        labels.append(
            {
                **label,
                "fusion_rule": row["fusion_rule"],
                "label_input_sha256": digest(
                    {"contract": shared, "prices": item["prices"], "policy": POLICY}
                ),
            }
        )
    return labels


async def run_reflection_pair(
    inputs: list[dict], *, known_at: str, port_factory, recall_voting: bool = False
) -> dict:
    """Use identical candidates, priors, RAG switch and term labels in both arms.

    This produces auditable measurements, not a release gate or a claim of
    original historical availability. The caller must seal code/data/cohort and
    attach the independent source evidence before drawing an acceptance result.
    """
    if not inputs or type(recall_voting) is not bool:
        raise ValueError("explicit_nonempty_reflection_experiment_required")
    frozen = deepcopy(inputs)
    days = [item["bundle"]["business_date"] for item in frozen]
    if days != sorted(set(days)):
        raise ValueError("reflection_unique_chronological_dates_required")
    bases = {item["bundle"]["evidence_basis"] for item in frozen}
    if len(bases) != 1:
        raise ValueError("reflection_evidence_basis_mismatch")
    # Reject invalid or unmatured label inputs before constructing a paid port.
    for item in frozen:
        validate_input(item["bundle"])
        contracts = contracts_for(item)
        for contract in contracts.values():
            preview = settle_frozen_cell(
                contract, item["prices"], known_at=known_at, policy=POLICY
            )
            if preview["status"] != "settled":
                raise ValueError("reflection_unmatured_frozen_sample")
    timeline = ReflectionTimeline(first_business_date=days[0])
    records = []
    for item in frozen:
        bundle = item["bundle"]
        day = bundle["business_date"]
        lessons = await timeline.before_issuance(
            as_of=bundle["as_of_time"], port_factory=port_factory
        )
        arms = {}
        for arm, active in (("off", []), ("on", lessons)):
            if bundle["signal_report"]["candidates"]:
                chain = FrozenReplayChain(
                    bundle=bundle,
                    port=port_factory(arm, day),
                    recall_voting=recall_voting,
                    lessons=active,
                    background_lessons=arm == "on",
                )
                surviving = await chain.run_political_analysis()
                await chain.run_historical_analog(surviving)
                await chain.run_product_synthesis()
                await chain.run_skeptic_review()
                report = chain.report()
                rows = chain.fused_rows()
            else:
                report = {
                    "status": "quiet_baseline",
                    "business_date": day,
                    "experiment": {
                        "input_sha256": validate_input(bundle),
                        "recall_voting": recall_voting,
                        "background_lessons": arm == "on",
                        "lessons_sha256": digest(active),
                    },
                }
                rows = [
                    {
                        "target": "crude",
                        "horizon_days": h,
                        "baseline_direction": contract["baseline_direction"],
                        "event_adjusted_direction": contract["baseline_direction"],
                        "fusion_rule": "baseline_day",
                    }
                    for h, contract in contracts_for(item).items()
                ]
            labels = labels_for(item, rows, known_at=known_at)
            reasoning = {
                "input_sha256": validate_input(bundle),
                "as_of_time": bundle["as_of_time"],
                "status": report["status"],
                "evidence_basis": bundle["evidence_basis"],
                "candidates": deepcopy(bundle["signal_report"]["candidates"]),
                "case_cards_by_event": deepcopy(bundle["case_cards_by_event"]),
                "empirical_by_event": deepcopy(bundle["empirical_by_event"]),
                "artifacts": [
                    deepcopy(a)
                    for a in report.get("artifacts", [])
                    if a.get("stage") in {"political_analysis", "historical_analog"}
                    or (a.get("input_refs") or {}).get("target") == "crude"
                ],
            }
            for label in labels:
                label["issued_reasoning"] = reasoning
            arms[arm] = {
                "chain_report": report,
                "cells": labels,
                "lessons_sha256": digest(active),
                "lessons_active": len(active),
            }
        for old, new in zip(arms["off"]["cells"], arms["on"]["cells"], strict=True):
            if old["label_input_sha256"] != new["label_input_sha256"] or any(
                old.get(field) != new.get(field) for field in LABEL_FIELDS
            ):
                raise ValueError("reflection_paired_label_changed")
        timeline.add_issued_result(
            as_of=bundle["as_of_time"], settled_cells=arms["on"]["cells"]
        )
        body = {
            "business_date": day,
            "input_sha256": validate_input(bundle),
            "arms": arms,
        }
        records.append({**body, "record_sha256": digest(body)})
    total = len(records) * 3
    off = sum(
        c["outcome_adjusted"] == "hit"
        for r in records
        for c in r["arms"]["off"]["cells"]
    )
    on = sum(
        c["outcome_adjusted"] == "hit"
        for r in records
        for c in r["arms"]["on"]["cells"]
    )
    by_horizon = {}
    for h in (1, 7, 30):
        before = sum(
            c["outcome_adjusted"] == "hit"
            for r in records
            for c in r["arms"]["off"]["cells"]
            if c["horizon_days"] == h
        )
        after = sum(
            c["outcome_adjusted"] == "hit"
            for r in records
            for c in r["arms"]["on"]["cells"]
            if c["horizon_days"] == h
        )
        by_horizon[str(h)] = {
            "cells": len(records),
            "off_hits": before,
            "on_hits": after,
            "net_pp": (after - before) / len(records) * 100,
        }
    complete = all(
        r["arms"][arm]["chain_report"]["status"] in {"ok", "quiet_baseline"}
        for r in records
        for arm in ("off", "on")
    )
    exposure = any(
        r["arms"]["on"]["lessons_active"]
        for r in records
        if r["arms"]["on"]["chain_report"]["status"] != "quiet_baseline"
    )
    result = {
        "schema_version": "frozen-reflection-pair.v1",
        "settlement_policy": POLICY,
        "evidence_basis": next(iter(bases)),
        "experiment_inputs_sha256": digest(frozen),
        "recall_voting": recall_voting,
        "records": records,
        "timeline": timeline.report(),
        "summary": {
            "cells": total,
            "off_hits": off,
            "on_hits": on,
            "net_pp": (on - off) / total * 100,
            "by_horizon": by_horizon,
        },
        "all_chains_complete": complete,
        "reflection_exposed": exposure,
        "acceptance_complete": False,
    }
    return {**result, "result_sha256": digest(result)}

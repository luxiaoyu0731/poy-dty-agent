"""Bind paired chain rows to frozen labels, without model or database access."""

from __future__ import annotations

from copy import deepcopy

from scripts.experiments.cached_replay_port import digest
from scripts.experiments.frozen_chain_replay import validate_input
from scripts.experiments.frozen_settlement import settle_frozen_cell

LABEL_FIELDS = (
    "settlement_policy",
    "due_date",
    "origin_observation_id",
    "actual_observation_id",
    "settled_available_at",
    "actual_change_pct",
    "actual_direction",
    "band",
    "label_identity",
)


def settle_rehearsal(
    rehearsal: dict,
    *,
    bundle: dict,
    issued_contracts: dict[int, dict],
    prices: list[dict],
    known_at: str,
    policy: str,
    target: str = "crude",
) -> dict:
    """One date, three horizons; both arms share the exact same label inputs.

    Missing or pending cells remain visible and cannot become a smaller accepted
    sample. The caller must retain the frozen contract and source price receipts.
    """
    identity = validate_input(bundle)
    if (
        rehearsal.get("input_sha256") != identity
        or rehearsal.get("business_date") != bundle["business_date"]
    ):
        raise ValueError("rehearsal_input_changed")
    if set(issued_contracts) != {1, 7, 30}:
        raise ValueError("three_frozen_horizon_contracts_required")
    expected = {(target, h) for h in (1, 7, 30)}
    results = {}
    for arm in ("control", "rag"):
        raw = rehearsal["arms"][arm]
        chain = raw["chain_report"]
        if (
            chain["experiment"]["input_sha256"] != identity
            or chain["business_date"] != bundle["business_date"]
        ):
            raise ValueError("chain_input_binding_mismatch")
        rows = [row for row in raw["fused_rows"] if row["target"] == target]
        keys = {(row["target"], row["horizon_days"]) for row in rows}
        if keys != expected or len(rows) != len(expected):
            raise ValueError("paired_horizon_rows_incomplete_or_duplicate")
        labels, cells = {}, []
        for row in sorted(rows, key=lambda item: item["horizon_days"]):
            horizon = row["horizon_days"]
            contract = deepcopy(issued_contracts[horizon])
            if (
                contract["target"] != target
                or contract["horizon_days"] != horizon
                or contract["business_date"] != bundle["business_date"]
                or contract["as_of_time"] != bundle["as_of_time"]
                or row["baseline_direction"] != contract["baseline_direction"]
            ):
                raise ValueError("frozen_issuance_contract_mismatch")
            contract["event_adjusted_direction"] = row["event_adjusted_direction"]
            label = settle_frozen_cell(
                contract, prices, known_at=known_at, policy=policy
            )
            labels[horizon] = label
            # The shared fingerprint intentionally excludes the prediction being
            # tested, while retaining all source/clock/band/contract information.
            label_contract = {
                k: v for k, v in contract.items() if k != "event_adjusted_direction"
            }
            cell = {
                "product": target,
                "horizon": horizon,
                "baseline_direction": row["baseline_direction"],
                "adjusted_direction": row["event_adjusted_direction"],
                "fusion_rule": row["fusion_rule"],
                "settlement_status": label["status"],
                "label_input_sha256": digest(
                    {"contract": label_contract, "prices": prices, "policy": policy}
                ),
                **{field: label[field] for field in LABEL_FIELDS if field in label},
            }
            cells.append(cell)
        complete = all(cell["settlement_status"] == "settled" for cell in cells)
        results[arm] = {
            "business_date": bundle["business_date"],
            "status": "ok"
            if complete and chain.get("status") in {"ok", "quiet_baseline"}
            else "incomplete",
            "cells": cells,
            "chain_report": deepcopy(chain),
            "settlements": labels,
            "input_sha256": identity,
            "validation_target": target,
        }
    old = {cell["horizon"]: cell for cell in results["control"]["cells"]}
    for cell in results["rag"]["cells"]:
        before = old[cell["horizon"]]
        if before["label_input_sha256"] != cell["label_input_sha256"] or any(
            before.get(field) != cell.get(field) for field in LABEL_FIELDS
        ):
            raise ValueError("paired_label_contract_mismatch")
    packet = {
        "schema_version": "paired-settlement.v1",
        "input_sha256": identity,
        "settlement_policy": policy,
        "known_at": known_at,
        "validation_target": target,
        "arms": results,
        "acceptance_complete": False,
    }
    return {**packet, "result_sha256": digest(packet)}


def assemble_pair(
    records: list[dict], *, manifest: dict, cohort_receipt: dict | None = None
) -> tuple[dict, dict]:
    dates = [record["arms"]["control"]["business_date"] for record in records]
    if len(dates) != len(set(dates)):
        raise ValueError("duplicate_paired_date")
    control = {"manifest": deepcopy(manifest), "recall_voting": False, "records": []}
    rag = {"manifest": deepcopy(manifest), "recall_voting": True, "records": []}
    if cohort_receipt is not None:
        from app.replay_cohort import validate_cohort

        selected = validate_cohort(
            cohort_receipt,
            data_sha256=manifest["data_sha256"],
            sample_sha256=manifest["sample_sha256"],
        )
        if (
            cohort_receipt["receipt_sha256"] != manifest["cohort_sha256"]
            or set(dates) != selected
        ):
            raise ValueError("assembled_pair_cohort_mismatch")
        control["cohort_receipt"] = deepcopy(cohort_receipt)
        rag["cohort_receipt"] = deepcopy(cohort_receipt)
    for record in records:
        body = {key: value for key, value in record.items() if key != "result_sha256"}
        if record.get("result_sha256") != digest(body):
            raise ValueError("paired_result_changed")
        for arm in ("control", "rag"):
            result = record["arms"][arm]
            if (
                result["input_sha256"] != record["input_sha256"]
                or result["chain_report"]["business_date"] != result["business_date"]
                or result["chain_report"]["experiment"]["input_sha256"]
                != record["input_sha256"]
            ):
                raise ValueError("assembled_pair_input_binding_mismatch")
        if (
            record["arms"]["control"]["business_date"]
            != record["arms"]["rag"]["business_date"]
        ):
            raise ValueError("assembled_pair_date_mismatch")
        if record["settlement_policy"] != manifest["settlement_policy"]:
            raise ValueError("manifest_settlement_policy_mismatch")
        for arm, result in (("control", control), ("rag", rag)):
            result["records"].append(deepcopy(record["arms"][arm]))
    return control, rag

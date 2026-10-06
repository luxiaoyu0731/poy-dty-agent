"""Run an explicitly reconstructed, isolated paired corpus with paid receipts.

Every frozen date is retained. Independent RAG dates may overlap HTTP waits;
reflection is separately chronological. No production database or live index.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.deepseek_client import DeepSeekClient
from app.unified_memory import recall_case_cards, replay_gate
from scripts.experiments.cached_replay_port import CachedReplayJsonPort, atomic, digest
from scripts.experiments.campaign_budget import CampaignBudget, MeteredJsonClient
from scripts.experiments.frozen_chain_replay import run_rag_pair, validate_input
from scripts.experiments.frozen_settlement import LEGACY_POLICY
from scripts.experiments.paired_results import assemble_pair, settle_rehearsal


def code_receipt(root):
    paths = sorted(
        [
            *(root / "server/app").rglob("*.py"),
            *(root / "scripts/experiments").glob("*.py"),
            root / "server/uv.lock",
        ]
    )
    import hashlib

    files = {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in paths
    }
    return {"files": files, "code_sha256": digest(files)}


async def run_campaign(
    *, inputs: Path, output: Path, budget_directory: Path, concurrency: int
):
    if not 1 <= concurrency <= 4 or inputs.is_symlink() or output.is_symlink():
        raise ValueError("bounded_isolated_campaign_required")
    output.mkdir(mode=0o700, exist_ok=True)
    items = [json.loads(p.read_text()) for p in sorted(inputs.glob("*.json"))]
    if len(items) != 60:
        raise ValueError("complete_frozen_60_dates_required")
    cohort = items[0]["bundle"]["source_admission"]["archive_source_pack"][
        "cohort_receipt"
    ]
    if {i["bundle"]["business_date"] for i in items} != set(cohort["selected_dates"]):
        raise ValueError("campaign_frozen_dates_mismatch")
    if not any(
        recall_case_cards(r)
        for i in items
        for r in i["bundle"].get("memory_recalls", [])
    ):
        raise ValueError("no_qualified_recall_campaign")
    # Complete source/baseline/label checks occurred at preparation. Recompute
    # them here before any physical HTTP; no success boolean buys admission.
    for item in items:
        if validate_input(item["bundle"]) != item["input_sha256"]:
            raise ValueError("campaign_frozen_input_changed")
    root = Path(__file__).resolve().parents[2]
    source = code_receipt(root)
    manifest = {
        "code_sha256": source["code_sha256"],
        "data_sha256": cohort["data_sha256"],
        "sample_sha256": cohort["sample_sha256"],
        "cohort_sha256": cohort["receipt_sha256"],
        "clock": "08:00 Asia/Shanghai",
        "candidate_policy": "source-proved-reconstructed-corpus.v1",
        "settlement_policy": LEGACY_POLICY,
        "model": "deepseek-v4-pro",
        "reconstructed_input_sha256": digest([i["input_sha256"] for i in items]),
        "original_champion_replication": False,
    }
    path = output / "manifest.json"
    if path.exists() and json.loads(path.read_text())["manifest"] != manifest:
        raise ValueError("campaign_version_changed_on_resume")
    atomic(path, {"manifest": manifest, "source": source})
    budget = CampaignBudget(budget_directory)
    before = budget._read()
    semaphore = asyncio.Semaphore(concurrency)

    async def one(item):
        day = item["bundle"]["business_date"]
        result_path = output / (day + ".json")
        if result_path.exists():
            result = json.loads(result_path.read_text())
            if result.get("input_sha256") != item["input_sha256"] or result.get(
                "result_sha256"
            ) != digest({k: v for k, v in result.items() if k != "result_sha256"}):
                raise ValueError("campaign_result_changed")
            return result
        async with semaphore:

            def factory(arm, business_date):
                print("start", day, arm, flush=True)
                return CachedReplayJsonPort(
                    MeteredJsonClient(DeepSeekClient(), budget),
                    output.parent / "reconstructed-paid-cache-v1",
                )

            rehearsal = await run_rag_pair(
                item["bundle"], port_factory=factory, retain_unexposed_date=True
            )
            result = settle_rehearsal(
                rehearsal,
                bundle=item["bundle"],
                issued_contracts={
                    int(k): v for k, v in item["issued_contracts"].items()
                },
                prices=item["prices"],
                known_at=item["known_at"],
                policy=LEGACY_POLICY,
            )
            atomic(result_path, result)
            print(
                "done",
                day,
                {a: r["status"] for a, r in result["arms"].items()},
                flush=True,
            )
            return result

    results = await asyncio.gather(*(one(i) for i in items))
    if code_receipt(root)["code_sha256"] != source["code_sha256"]:
        raise ValueError("campaign_code_changed_during_run")
    control, rag = assemble_pair(results, manifest=manifest, cohort_receipt=cohort)
    atomic(output / "control.json", control)
    atomic(output / "rag.json", rag)
    gate = replay_gate(control, rag)
    after = budget._read()
    summary = {
        "manifest": manifest,
        "gate": gate,
        "new_http_attempts": after["attempts"] - before["attempts"],
        "incremental_peak_upper_cny": after["reserved_cny"] - before["reserved_cny"],
        "cumulative_peak_upper_cny": after["reserved_cny"],
        "cap_cny": after["cap_cny"],
        "days": len(results),
        "cells_per_arm": sum(len(r["cells"]) for r in control["records"]),
        "source_policy": "source-proved-reconstructed-corpus.v1",
        "production_voting_enabled": False,
    }
    atomic(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget-directory", type=Path, required=True)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    asyncio.run(
        run_campaign(
            inputs=args.inputs,
            output=args.output,
            budget_directory=args.budget_directory,
            concurrency=args.concurrency,
        )
    )


if __name__ == "__main__":
    main()

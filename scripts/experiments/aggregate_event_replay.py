"""Aggregate E1/E2 replay batches into the §7 gate verdict and E3-style splits.

Reads one or more replay output JSON files (records may overlap across batch
windows), dedupes by business_date, and prints/writes the combined summary:
overall + per-horizon + per-product hit rates, switch counts, LLM call usage,
and a split around --training-cutoff (honest note: with 2026-09-only events the
split measures recency, not training contamination; true E3 needs the 25-year
backfill, plan §9).

Usage:
  python scripts/experiments/aggregate_event_replay.py \
      --input e1-b1.json e1-b2.json e1-b3.json \
      --split-cutoff 2026-09-15 --output summary.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0}
    return {
        "n": n,
        "baseline_hit_rate": round(sum(r["baseline_hit"] for r in rows) / n, 4),
        "adjusted_hit_rate": round(sum(r["adjusted_hit"] for r in rows) / n, 4),
        "switch_count": sum(1 for r in rows if r["fusion_rule"] == "R2"),
        "rule_counts": dict(sorted(
            (rule, sum(1 for r in rows if r["fusion_rule"] == rule))
            for rule in {r["fusion_rule"] for r in rows}
        )),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, nargs="+", required=True)
    parser.add_argument("--split-cutoff", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    records: dict[str, dict] = {}
    for path in args.input:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for record in payload.get("records", []):
            day = str(record.get("business_date"))
            if day and day not in records:
                records[day] = record
    ordered = [records[day] for day in sorted(records)]
    rows = [cell for r in ordered if r.get("status") == "ok" for cell in r.get("cells", [])]
    llm_used = sum(int(r.get("llm_used") or 0) for r in ordered)

    by_horizon = defaultdict(list)
    by_product = defaultdict(list)
    for row in rows:
        by_horizon[row["horizon"]].append(row)
        by_product[row["product"]].append(row)

    summary = {
        "schema_version": "event_chain_replay_summary.v1",
        "dates": [r["business_date"] for r in ordered if r.get("status") == "ok"],
        "llm_calls_total": llm_used,
        "overall": summarize(rows),
        "by_horizon": {str(h): summarize(v) for h, v in sorted(by_horizon.items())},
        "by_product": {p: summarize(v) for p, v in sorted(by_product.items())},
        "gate": {
            "direction_min": 0.55,
            "min_samples": 20,
            "passed": bool(len(rows) >= 20 and summarize(rows).get("adjusted_hit_rate", 0) >= 0.55),
            "note": "淘汰门禁（§7.1）：不过=淘汰；过了也不直接晋级，晋级以生产影子期为准（ADR-6）",
        },
    }

    if args.split_cutoff:
        early = [c for r in ordered if r.get("status") == "ok" and r["business_date"] <= args.split_cutoff for c in r.get("cells", [])]
        late = [c for r in ordered if r.get("status") == "ok" and r["business_date"] > args.split_cutoff for c in r.get("cells", [])]
        summary["split_cutoff"] = args.split_cutoff
        summary["pre_cutoff"] = summarize(early)
        summary["post_cutoff"] = summarize(late)
        summary["split_note"] = (
            "事件史仅 2026-09：本分段测的是新近性差异，不是 LLM 训练污染；"
            "真污染测量（§7.3）需第 9 章 25 年回填后重跑"
        )

    # Retrieval evaluation (§7.4): analog validity distribution + case citations
    analog = [a for r in ordered for a in (r.get("analog_summary") or [])]
    summary["analog"] = {
        "n": len(analog),
        "validity_counts": dict(sorted(
            (v, sum(1 for a in analog if a.get("validity") == v))
            for v in {a.get("validity") for a in analog}
        )) if analog else {},
        "cited_case_ids": sorted({c for a in analog for c in (a.get("cited_cases") or [])}),
    }

    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

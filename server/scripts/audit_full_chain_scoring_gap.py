#!/usr/bin/env python3
"""Audit the 4008/2538 full-chain scoring gap without changing its denominator.

The legacy aggregate does not retain row-level exclusion reason codes.  This
script therefore reports only identities that are provable from the artifact
and marks the remaining candidate gap as requiring a reproducible rerun.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

DEFAULT_BACKTEST = Path(".codex-run/full-chain-delivery/full-chain-backtest-latest.json")
DEFAULT_OUTPUT = Path(".codex-run/full-chain-delivery/full-chain-scoring-gap-audit-latest.json")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_audit(backtest: dict[str, Any], *, source_path: Path, source_sha256: str) -> dict[str, Any]:
    summary = backtest.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("backtest.summary must be an object")

    required = ("total_rows", "non_neutral_candidates", "scored", "hit", "miss")
    try:
        values = {key: int(summary[key]) for key in required}
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"backtest.summary must contain integer fields: {', '.join(required)}") from exc

    total = values["total_rows"]
    candidates = values["non_neutral_candidates"]
    scored = values["scored"]
    if not (0 <= scored <= candidates <= total):
        raise ValueError("expected 0 <= scored <= non_neutral_candidates <= total_rows")
    if values["hit"] + values["miss"] != scored:
        raise ValueError("hit + miss must equal scored")

    filtered_before_candidate = total - candidates
    candidate_not_scored = candidates - scored
    total_not_scored = total - scored
    return {
        "schema_version": "full_chain_scoring_gap_audit.v1",
        "source": {"path": str(source_path), "sha256": source_sha256},
        "invariants": {
            "total_rows": total,
            "non_neutral_candidates": candidates,
            "scored": scored,
            "hit_plus_miss": values["hit"] + values["miss"],
            "total_not_scored": total_not_scored,
            "gap_reconciles": filtered_before_candidate + candidate_not_scored == total_not_scored,
        },
        "proven_attribution": [
            {
                "reason_code": "not_selected_by_candidate_filter",
                "count": filtered_before_candidate,
                "calculation": "total_rows - non_neutral_candidates",
                "repairability": "not_a_data_repair",
                "explanation": (
                    "旧产物定义为 prior 5-observation direction 与 final chain vote 均为中性；除非预注册规则改变，"
                    "否则不应补算成命中或失误。"
                ),
            },
            {
                "reason_code": "candidate_without_scored_verdict_unresolved",
                "count": candidate_not_scored,
                "calculation": "non_neutral_candidates - scored",
                "repairability": "conditional_rerun_required",
                "explanation": (
                    "旧聚合产物未保存逐行 reason code，无法从现有聚合值判定是实际方向中性、"
                    "缺未来价格、映射失败还是其他原因。"
                ),
            },
        ],
        "safe_salvage": {
            "immediately_rescorable_from_aggregate": 0,
            "candidate_rows_requiring_row_level_rebuild": candidate_not_scored,
            "required_before_rescore": [
                "定位原始生成器或以冻结规则重建等价生成器",
                "为每个 product/day 持久化 candidate_reason、score_status、unscored_reason",
                "保存预测时点可见输入哈希、配置哈希、代码版本和确定性声明",
                "仅对具有预测方向且具有下一有效价格的行计算 verdict",
                "保留 total_rows=4008，不以删除未评分行提高覆盖率",
            ],
        },
        "formal_status": {
            "eligible": False,
            "reason": "aggregate artifact cannot prove row-level causes or a reproducible rescore path",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = json.loads(args.backtest.read_text(encoding="utf-8"))
    audit = build_audit(payload, source_path=args.backtest, source_sha256=_sha256(args.backtest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit["invariants"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Rebuild a row-level audit from the preserved 4008-row full-chain artifact.

This is an evidence-preserving enrichment, not a rerun of the missing original
generator.  It derives reason codes only from fields that the artifact retained
and uses JSON pointers as immutable row references.  It never invents source
visibility timestamps or changes the historical denominator/verdicts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

DEFAULT_INPUT = Path(".codex-run/full-chain-delivery/full-chain-backtest-latest.json")
DEFAULT_OUTPUT = Path(".codex-run/full-chain-delivery/full-chain-row-audit-latest.json")
NEUTRAL = "震荡"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reason(row: dict[str, Any]) -> tuple[str, str | None, str]:
    prediction = row.get("prediction_direction")
    actual = row.get("actual_direction")
    verdict = row.get("verdict")
    if prediction == NEUTRAL:
        return (
            "row_prediction_neutral",
            "prediction_direction_neutral",
            "not_scored",
        )
    if prediction not in {"偏强", "偏弱"}:
        return ("row_prediction_unknown", "prediction_direction_unknown", "not_scored")
    if actual == NEUTRAL:
        return (
            "row_prediction_non_neutral",
            "actual_next_observation_within_neutral_band",
            "not_scored",
        )
    if verdict in {"hit", "miss"} and actual in {"偏强", "偏弱"}:
        return ("row_prediction_non_neutral", None, "scored")
    return ("row_prediction_non_neutral", "legacy_verdict_unresolved", "not_scored")


def build_row_audit(payload: dict[str, Any], *, source_path: Path, source_sha256: str) -> dict[str, Any]:
    rows = payload.get("rows")
    summary = payload.get("summary")
    scope = payload.get("scope")
    if not isinstance(rows, list) or not isinstance(summary, dict) or not isinstance(scope, dict):
        raise ValueError("artifact must contain rows[], summary{}, and scope{}")

    rebuilt: list[dict[str, Any]] = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise ValueError(f"rows[{index}] must be an object")
        candidate_reason, unscored_reason, score_status = _reason(raw)
        future_change = raw.get("future_change_pct")
        rebuilt.append(
            {
                "row_id": f"legacy-full-chain:{index}",
                "date": raw.get("date"),
                "period": raw.get("period"),
                "product": raw.get("product"),
                "candidate_reason": candidate_reason,
                "score_status": score_status,
                "unscored_reason": unscored_reason,
                "prediction_direction": raw.get("prediction_direction"),
                "actual_direction": raw.get("actual_direction"),
                "verdict": raw.get("verdict"),
                "price_window": {
                    "decision_date": raw.get("date"),
                    "prior_observation_count": 5,
                    "prior_change_pct": raw.get("prior_change_pct"),
                    "posterior_policy": "next valid observation",
                    "posterior_observation_date": None,
                    "posterior_date_status": "not_retained_in_legacy_artifact",
                    "future_change_pct": future_change,
                    "neutral_threshold_pct": 0.35,
                },
                "source_row_refs": [
                    {
                        "artifact_path": str(source_path),
                        "json_pointer": f"/rows/{index}",
                        "source_id": raw.get("source_id"),
                    }
                ],
            }
        )

    candidate_counts = Counter(row["candidate_reason"] for row in rebuilt)
    unscored_counts = Counter(row["unscored_reason"] for row in rebuilt if row["unscored_reason"])
    score_counts = Counter(row["score_status"] for row in rebuilt)
    expected = {
        "total": int(summary["total_rows"]),
        "candidates": int(summary["non_neutral_candidates"]),
        "scored": int(summary["scored"]),
    }
    reconciles = (
        len(rebuilt) == expected["total"]
        and candidate_counts["row_prediction_non_neutral"] == expected["candidates"]
        and score_counts["scored"] == expected["scored"]
    )
    if not reconciles:
        raise ValueError("row-level reconstruction does not reconcile with legacy summary")

    return {
        "schema_version": "full_chain_row_audit.v1",
        "method": "evidence_preserving_enrichment_of_retained_legacy_rows",
        "source": {"path": str(source_path), "sha256": source_sha256},
        "invariants": {
            "denominator_unchanged": len(rebuilt),
            "legacy_non_neutral_candidates": expected["candidates"],
            "legacy_scored": expected["scored"],
            "reconciles": reconciles,
        },
        "reason_counts": {
            "candidate_reason": dict(sorted(candidate_counts.items())),
            "unscored_reason": dict(sorted(unscored_counts.items())),
            "score_status": dict(sorted(score_counts.items())),
        },
        "limitations": [
            "The original generator command and commit were not retained; this output is not a generator rerun.",
            "Posterior observation dates were not retained and are intentionally null.",
            "source_id is preserved, but source-table primary keys and historical visible_at were not retained.",
            (
                "The 520 neutral-prediction rows remained in the 4008 denominator because the legacy artifact is"
                " row-complete; they are not relabelled or removed."
            ),
        ],
        "rows": rebuilt,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    result = build_row_audit(payload, source_path=args.input, source_sha256=_sha256(args.input))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps({"output": str(args.output), **result["invariants"], **result["reason_counts"]}, ensure_ascii=False)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

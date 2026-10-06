"""Offline comparison of issued main input archives. Never opens a database."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prediction_evidence_experiment import evaluate_archives  # noqa: E402
from app.prediction_evidence_runtime import seal  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, required=True)
    parser.add_argument(
        "--issued-inputs",
        type=Path,
        required=True,
        help="Verified main ledger map: input hash -> issue, cells, official outcomes",
    )
    parser.add_argument("--archive", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(args.archive) > 366 or sum(p.stat().st_size for p in args.archive) > 256 * 1024**2:
        parser.error("archive scope exceeds 366 files / 256MiB")
    archives = [json.loads(p.read_text()) for p in args.archive]
    prices = json.loads(args.prices.read_text())
    issued = json.loads(args.issued_inputs.read_text())
    results = [
        evaluate_archives(archives, prices, target=t, horizon=h, issued_inputs=issued)
        for t in prices["series"]
        for h in (1, 7, 30)
    ]
    result = seal({"cells": results, "automatic_promotion": False, "production_writes": False})
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    args.output.chmod(0o600)
    print(
        json.dumps(
            {
                "cells": len(results),
                "scored": sum(r["scored_count"] for r in results),
                "effect_validated": False,
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()

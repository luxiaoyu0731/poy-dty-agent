"""Offline replay. Only reads a frozen JSON export; never opens the database or calls models."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prediction_benchmark import run_benchmark  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve() or args.output.exists():
        parser.error("output must be a new file distinct from the input")
    body = json.loads(args.input.read_text())
    report = run_benchmark(body, start=args.start, end=args.end)
    with args.output.open("x") as stream:
        json.dump(report, stream, ensure_ascii=False, allow_nan=False)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "result_sha256": report["result_sha256"],
                "records": len(report["records"]),
                "cells": len(report["cells"]),
            }
        )
    )


if __name__ == "__main__":
    main()

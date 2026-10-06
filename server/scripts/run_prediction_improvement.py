"""Run fixed offline improvement candidates on a bound first-round replay."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.prediction_improvement import run_improvement  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("refusing to overwrite an existing research artifact")
    result = run_improvement(json.loads(args.input.read_text()), json.loads(args.baseline.read_text()))
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, allow_nan=False)
    print(json.dumps({"path": str(args.output.resolve()), "result_sha256": result["result_sha256"]}))


if __name__ == "__main__":
    main()

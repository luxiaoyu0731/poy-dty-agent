from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app.rag_evals import run_daily_rag_eval_suite  # noqa: E402
from app.settings import settings  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic isolated RAG quality gate.")
    parser.add_argument(
        "--database",
        help="Optional disposable SQLite path. By default a temporary isolated database is used.",
    )
    args = parser.parse_args()
    if args.database:
        object.__setattr__(settings, "sqlite_path", str(Path(args.database).resolve()))
        result = await run_daily_rag_eval_suite()
    else:
        with tempfile.TemporaryDirectory(prefix="poy-rag-eval-") as directory:
            object.__setattr__(settings, "sqlite_path", str(Path(directory) / "rag-eval.db"))
            result = await run_daily_rag_eval_suite()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

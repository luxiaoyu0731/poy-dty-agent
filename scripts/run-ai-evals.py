from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))


def _configure_isolated_runtime(database: Path) -> None:
    if os.getenv("DEEPSEEK_API_KEY", "").strip():
        raise RuntimeError("governed_eval_provider_must_be_disabled")
    os.environ.update(
        {
            "APP_ENV": "test",
            "SQLITE_PATH": str(database),
            "DEEPSEEK_API_KEY": "",
            "EMBEDDING_PROVIDER": "offline_eval",
            "EMBEDDING_MODEL": "offline-hash-fixture",
            "EMBEDDING_MODEL_VERSION": "1",
            "EMBEDDING_DIMENSIONS": "96",
            "EMBEDDING_FALLBACK_POLICY": "hash_fallback",
            "INTRADAY_PRICE_SCHEDULER_ENABLED": "false",
            "AGENT_GOVERNANCE_SCHEDULER_ENABLED": "false",
            "EXPERIENCE_SETTLEMENT_SCHEDULER_ENABLED": "false",
        }
    )


async def _run(database: Path) -> dict[str, object]:
    _configure_isolated_runtime(database)
    from app.evals import run_governed_eval_suite

    return await run_governed_eval_suite()


async def main() -> int:
    # A new outside-the-repository database is the default and only supported
    # execution mode. The result retains reproducibility metadata; the mutable
    # fixture database is deleted when the process exits.
    with tempfile.TemporaryDirectory(prefix="poy-dty-agent-eval-") as directory:
        result = await _run(Path(directory) / "agent-eval.db")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

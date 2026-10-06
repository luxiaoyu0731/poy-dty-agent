"""Run one business-date counter-evidence scan (反证扫描) against a frozen snapshot.

Reads the daily judgement snapshot and featured events read-only, performs the
deterministic evidence retrieval, and makes at most one paid provider attempt
per invocation (<=2 per business day across invocations, enforced by O_EXCL
reservation files). Writes its own artifact; never writes business tables.

Intentionally NOT wired into the daily chain in this batch (DESIGN 批次1); the
chain hook lands in a later batch after the vertical-slice comparison is
accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from runtime_guards import configure_runtime_sqlite_path  # noqa: E402

from app.counter_scan import run_counter_scan  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the counter-evidence scan for one business date.")
    parser.add_argument("--business-date", required=True, help="Business date (YYYY-MM-DD, Asia/Shanghai).")
    parser.add_argument("--db", type=Path, default=None, help="Database file (defaults to settings.sqlite_path).")
    parser.add_argument("--dry-run", action="store_true", help="Assemble inputs only; no provider call, no artifact.")
    parser.add_argument("--chain-status", default=None, help="Overall daily chain status, when invoked from the chain.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.db is not None:
        configure_runtime_sqlite_path(args.db.expanduser().resolve())
    result = asyncio.run(
        run_counter_scan(
            business_date=args.business_date,
            dry_run=args.dry_run,
            chain_status=args.chain_status,
        )
    )
    print(
        json.dumps(
            {
                key: result.get(key)
                for key in (
                    "business_date",
                    "status",
                    "failure_reason",
                    "failure_detail",
                    "scan_outcome",
                    "artifact_path",
                    "llm_cost_cny",
                    "timings",
                )
                if key in result
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.get("status") in {"completed", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

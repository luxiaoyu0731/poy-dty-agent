"""Read-only summary coverage, including excluded inputs and terminal rejections."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target-pct", type=float, default=90)  # reporting target, not a quality bypass
    args = parser.parse_args(argv)
    with closing(sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)) as con:
        total = con.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0]
        states = dict(con.execute("SELECT summary_status,COUNT(*) FROM event_ai_summaries GROUP BY summary_status"))
    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "articles": total,
        "summary_states": states,
        "without_summary_job": total - sum(states.values()),
        "target_pct": args.target_pct,
        "note": "无任务和拒绝均不计作已完成；是否适合摘要需按来源正文质量分别核验。",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.output.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

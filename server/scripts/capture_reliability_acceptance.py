#!/usr/bin/env python3
"""Read-only production sampling; each immutable sample is evidence, not an uptime guarantee."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def capture(runtime: Path, base_url: str) -> dict:
    result = {"sampled_at": datetime.now(UTC).isoformat(), "http": {}, "runtime": {}, "database": {}}
    for key, suffix in [
        ("live", "/api/v1/health/live"),
        ("ready", "/api/v1/health/ready"),
        ("index", "/api/v1/rag-index/status"),
        ("brief", "/api/v1/intelligence/brief"),
    ]:
        started = time.monotonic()
        try:
            req = urllib.request.Request(
                base_url.rstrip("/") + suffix, headers={"User-Agent": "poy-dty-release-gate/reliability-acceptance"}
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                result["http"][key] = {"status": response.status, "body": json.load(response)}
        except urllib.error.HTTPError as exc:
            result["http"][key] = {"status": exc.code, "error": "http_error"}
        except Exception as exc:
            result["http"][key] = {"error": type(exc).__name__}
        result["http"][key]["elapsed_seconds"] = round(time.monotonic() - started, 3)
    for name, relative in {
        "summary": "event-summary-worker/latest.json",
        "budget": "event-summary-worker/daily-budget.json",
        "index_maintenance": "event-summary-worker/index-maintenance.json",
        "news": "news-automation/latest-scheduler.json",
        "live_projection": "news-automation/live-intelligence.json",
    }.items():
        try:
            result["runtime"][name] = json.loads((runtime / "shared" / relative).read_text())
        except (OSError, ValueError) as exc:
            result["runtime"][name] = {"error": type(exc).__name__}
    path = runtime / "shared/data/agent.db"
    queries = {
        "summary_counts": "SELECT summary_status,COUNT(*) AS n FROM event_ai_summaries GROUP BY summary_status",
        "failed_summaries": (
            "SELECT article_id,attempts,error,updated_at FROM event_ai_summaries "
            "WHERE summary_status='failed' LIMIT 100"
        ),
        "summary_freshness": (
            "SELECT MAX(generated_at) AS latest_generated_at FROM event_ai_summaries WHERE summary_status='completed'"
        ),
        "indexes": (
            "SELECT index_id,status,created_at,completed_at,document_count,vector_count "
            "FROM semantic_indices ORDER BY created_at"
        ),
        "fetch_hourly": (
            "SELECT strftime('%Y-%m-%dT%H:00:00Z',created_at) AS hour,status,COUNT(*) AS n "
            "FROM source_fetch_audit WHERE datetime(created_at)>=datetime('now','-72 hours') "
            "GROUP BY hour,status ORDER BY hour"
        ),
    }
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)) as c:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA query_only=ON")
            for key, sql in queries.items():
                deadline = time.monotonic() + 5
                c.set_progress_handler(lambda deadline=deadline: int(time.monotonic() > deadline), 10000)
                try:
                    result["database"][key] = [dict(row) for row in c.execute(sql)]
                except sqlite3.Error as exc:
                    result["database"][key] = {"error": type(exc).__name__}
            c.set_progress_handler(None, 0)
    except sqlite3.Error as exc:
        result["database"]["error"] = type(exc).__name__
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--base-url", default="https://app.kaipingrc.com")
    args = parser.parse_args()
    sample = capture(args.runtime_root, args.base_url)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    with path.open("x") as f:
        json.dump(sample, f, ensure_ascii=False, indent=2)
    print(
        json.dumps(
            {
                "path": str(path.resolve()),
                "http": {k: v.get("status", v.get("error")) for k, v in sample["http"].items()},
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

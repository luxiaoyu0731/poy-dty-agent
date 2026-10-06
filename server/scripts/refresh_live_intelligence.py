"""Append bounded live radar revisions; never materialize or rewrite daily briefs."""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_guards import configure_runtime_sqlite_path  # noqa: E402

from app import storage  # noqa: E402
from app.industrial_intelligence import identity, service  # noqa: E402
from app.industrial_intelligence.counterevidence_stage import refresh_counterevidence  # noqa: E402

# Each 30-minute news cycle must drain what it fetched or the live radar lags
# further every cycle. 20s sustains a fast local disk; slower hosts (small
# cloud VMs on a 10GB database) raise this via env so one cycle can catch up.
LIVE_PROJECTION_DEADLINE_SECONDS = max(20, int(os.getenv("LIVE_INTELLIGENCE_PROJECTION_DEADLINE_SECONDS", "20")))


def refresh(db: Path, *, max_items: int = 1000) -> dict:
    configure_runtime_sqlite_path(db)
    bound = max(1, min(max_items, 1000))
    business_date = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    with closing(storage.connect()) as connection:
        with service.single_flight("projection:legacy_news_articles"):
            projection_run = service.run_news_projection_stage(
                connection, business_date=business_date,
                deadline_seconds=LIVE_PROJECTION_DEADLINE_SECONDS, max_items=bound,
            )
        cutoff = identity.utc_now_iso()
        # Bound both phases, including parser-version backfill. Remaining work
        # resumes next cycle; the current/frozen daily cutoff is never changed.
        head = connection.execute(
            "SELECT MAX(append_seq) AS hw FROM (SELECT i.append_seq FROM intelligence_item_revisions i "
            "WHERE i.revision_kind='upsert' AND i.visible_at<=? AND NOT EXISTS ("
            "SELECT 1 FROM intelligence_event_evidence e WHERE e.item_revision_id=i.item_revision_id) "
            "ORDER BY i.append_seq LIMIT ?)", (cutoff, bound),
        ).fetchone()["hw"]
        cluster_run = None
        new_events = 0
        if head:
            with service.single_flight(f"clustering:{business_date}"):
                cluster_run, new_events = service.run_clustering_analysis_stage(
                    connection, business_date=business_date, cutoff_at=cutoff,
                    item_high_water=int(head), parent_run_ids=[projection_run],
                )
        # Independent of new clustering: a later denial or source withdrawal
        # may affect an already visible event. Never reopen frozen daily briefs.
        with service.single_flight("analysis:explicit-counterevidence"):
            counterevidence_result = refresh_counterevidence(connection, cutoff_at=identity.utc_now_iso())
        return {"status": "completed", "projection_run_id": projection_run,
                "clustering_run_id": cluster_run, "new_events": new_events,
                "max_items": bound, "brief_created": False, "as_of_time": cutoff,
                "counterevidence": counterevidence_result}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = refresh(args.db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Translate public event headlines into a budgeted, durable reading cache.

Does not fetch news sources, open the primary database, or promote predictions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.event_overview_store import (  # noqa: E402
    atomic_json,
    ensure_overview,
    initialize_budget,
    read_overview,
    recover_paid_overviews,
    store_root,
)

# The public origin sits behind the password gate; on the server the worker
# reaches the backend directly over loopback via this override.
EVENT_LIBRARY_ORIGIN = os.environ.get(
    "EVENT_OVERVIEWS_EVENT_LIBRARY_ORIGIN", "https://app.kaipingrc.com"
).rstrip("/")

# Price-observation rows leak into the event library as title-only entries
# ("9月21日生意社石油焦基准价为3474.00元/吨"). They are market data, not
# events: overview generation burns paid attempts on them every cycle and
# always fails. Skip them outright; they stay visible via the price pages.
# A short source prefix ("生意社：", "EIA：") may lead the date.
_MARKET_DATA_TITLE = re.compile(r"^(?:[\u4e00-\u9fffA-Za-z]{1,6}[:：]\s*)?\d{1,2}月\d{1,2}日")


def is_market_data_title(title: str) -> bool:
    return bool(_MARKET_DATA_TITLE.match(title)) and len(title) <= 48 and bool(
        re.search(r"元|美元|库存|持平|上涨|下跌|涨|跌|仓|收盘|基准价", title)
    )


def _fetch_json(url: str, *, attempts: int = 3, timeout: float = 60.0) -> dict:
    # Container images ship without curl; stdlib urllib keeps the same
    # retry/timeout contract the previous `curl -fsS --retry 2` call had.
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.0)
    raise RuntimeError(f"event library fetch failed after {attempts} attempts: {last_error}") from last_error


def public_events() -> tuple[list[dict], int]:
    found = {}
    offset = 0
    total = 0
    for _ in range(101):
        url = f"{EVENT_LIBRARY_ORIGIN}/api/v1/workbench/event-library?limit=1000&offset={offset}"
        page = _fetch_json(url)
        total = page["total_events"]
        for event in page["events"]:
            found[event["id"]] = event
        offset += len(page["events"])
        if not page.get("has_more"):
            return list(found.values()), total
        if not page["events"]:
            raise ValueError("empty_page_with_more")
    raise ValueError("event_pagination_exceeded")


async def run_once(root: Path, *, concurrency: int = 4) -> dict:
    recovered = recover_paid_overviews(root)
    events, total = await asyncio.to_thread(public_events)
    atomic_json(root / "input-snapshot.json", {"total": total, "events": events})
    sources = {str(x.get("factual_title") or x.get("title") or ""): x.get("source_url", "") for x in events}
    # A duplicate title is one reading aid; preserve every event in coverage.
    # Market-data rows are skipped: they are not events and never summarize.
    titles = [
        title
        for title in dict.fromkeys(
            str(x.get("factual_title") or x.get("title") or "")
            for x in events
            if not (x.get("overview_basis") == "body" and x.get("overview_text"))
        )
        if not is_market_data_title(title)
    ]
    semaphore = asyncio.Semaphore(min(max(concurrency, 1), 8))
    counts = Counter()
    failures = []

    async def one(title):
        async with semaphore:
            try:
                result = await ensure_overview(title, root=root, source_url=sources[title])
                status = result["status"]
            except Exception as exc:
                status = (
                    str(exc)
                    if str(exc) in {"overview_budget_exhausted", "overview_attempts_exhausted"}
                    else type(exc).__name__
                )
            counts[status] += 1
            if status not in {"generated", "cached", "source_title_decoded"}:
                failures.append({"title": title, "status": status})
            processed = sum(counts.values())
            if processed % 50 == 0:
                atomic_json(
                    root / "progress.json", {"processed": processed, "selected": len(titles), "counts": dict(counts)}
                )
                print(json.dumps({"processed": processed, "selected": len(titles), "counts": dict(counts)}), flush=True)

    await asyncio.gather(*(one(title) for title in titles))
    recovered += recover_paid_overviews(root)
    # Coverage is measured over events eligible for an overview; market-data
    # rows are excluded from both numerator and denominator.
    eligible = [
        x for x in events if not is_market_data_title(str(x.get("factual_title") or x.get("title") or ""))
    ]
    completed = sum(
        bool(x.get("overview_text") and x.get("overview_basis") == "body")
        or bool(read_overview(str(x.get("factual_title") or x.get("title") or ""), root))
        for x in eligible
    )
    result = {
        "at": datetime.now(UTC).isoformat(),
        "snapshot_total": total,
        "unique_events": len(events),
        "eligible_events": len(eligible),
        "skipped_market_data_rows": len(events) - len(eligible),
        "completed": completed,
        "coverage_ratio": completed / len(eligible) if eligible else 0,
        "snapshot_complete": len(events) == total,
        "paid_outputs_recovered": recovered,
        "counts": dict(counts),
        "failures": failures,
    }
    atomic_json(root / "latest.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--initialize-approved-budget", action="store_true")
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    root = store_root()
    if args.initialize_approved_budget:
        initialize_budget(root, limit_microusd=10_000_000)
        return
    while True:
        try:
            result = asyncio.run(run_once(root, concurrency=args.concurrency))
            print(json.dumps({k: v for k, v in result.items() if k != "failures"}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__}), flush=True)
            if not args.watch:
                raise
        if not args.watch:
            break
        time.sleep(600)


if __name__ == "__main__":
    main()

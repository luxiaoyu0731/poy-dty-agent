"""Chapter 9 T2: GDELT-anchored precedent backfill over extreme price days.

docs/multi-agent-prediction-plan.md §9.1 T2+T3 combined: instead of crawling
the whole GDELT archive (tens of GB, 15-minute files), the backfill is
price-anchored (T3's design): for every Brent day whose return exceeds the
threshold, query the GDELT DOC 2.0 API (the project's existing collector
channel and keyword set) for that day's oil-chain coverage and keep the top
articles as T2 case rows — real posterior numbers from FRED, no LLM enrichment
(plan: structured first, LLM only on demand).

Runs LOCALLY (GDELT rate-limits the server egress IP shared with the news
scheduler): one request per day, >=8s apart, single retry after 300s on 429.
Output JSON is applied to production by apply_t2_cases.py on the server.

Usage:
  python scripts/experiments/fetch_gdelt_extreme_days.py \
      --price-history /tmp/price-history.json \
      --since 2015-02-18 --top-days 200 --threshold 0.025 \
      --output /tmp/t2-cases.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

QUERY = '(OPEC OR "crude oil" OR Hormuz OR OFAC OR tanker OR "oil price")'
API = "https://api.gdeltproject.org/api/v2/doc/doc"
REQUEST_INTERVAL_SECONDS = 8.0
RELEVANT = re.compile(
    r"oil|crude|opec|petrol|refinery|sanction|tanker|hormuz|pipeline|energy", re.I
)


def compact(day: str) -> str:
    return day.replace("-", "")


def extreme_days(brent: dict[str, float], *, since: str, threshold: float) -> list[tuple[str, float]]:
    """threshold=0 anchors EVERY trading day (all-days prior library, D1 fix)."""
    days = sorted(d for d in brent if d >= since)
    result = []
    for previous, current in zip(days, days[1:]):
        ret = brent[current] / brent[previous] - 1
        if threshold <= 0 or abs(ret) >= threshold:
            result.append((current, round(ret * 100, 2)))
    return result


def fetch_day(day: str) -> list[dict]:
    url = (
        f"{API}?query={urllib.request.quote(QUERY)}&mode=artlist&format=json"
        f"&maxrecords=10&sort=hybridrel"
        f"&startdatetime={compact(day)}000000&enddatetime={compact(day)}235959"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.load(response).get("articles", [])
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 2:
                raise
            time.sleep(300)  # global rate-limit penalty window
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(60)
    return []


def posterior(brent: dict[str, float], day: str, horizon: int) -> float | None:
    base_day = max((d for d in brent if d <= day), default=None)
    if base_day is None:
        return None
    target = max((d for d in brent if d <= (datetime.fromisoformat(day) + timedelta(days=horizon)).date().isoformat()), default=None)
    if target is None or target == base_day:
        return None
    return round((brent[target] / brent[base_day] - 1) * 100, 2)


OIL_ACTOR_PATTERN = re.compile(
    r"opec|rosneft|aramco|gazprom|exxon|shell|chevron|totalenergies|total |petrobras|"
    r"pemex|pdvsa|nnpc|kuwait petroleum|adnoc|sonatrach|cnpcc?|sinopec|cnooc|lukoil|"
    r"\boil\b|petroleum|refiner", re.I
)
PETRO_STATES = {"RU", "SA", "IQ", "IR", "AE", "KW", "QA", "NG", "VE", "LY", "DZ", "AO", "KZ", "NO"}
CONFLICT_ROOTS = {"14", "17", "18", "19", "20"}  # protest/coerce/assault/fight/mass
SLOTS = ("001500", "060000", "120000", "180000")
# all-days mode: 2 slots suffice for quiet-day anchoring (halves fetch time)
SLOTS_QUIET = ("063000", "150000")


def fetch_day_export(day: str, *, slots: tuple[str, ...] = SLOTS) -> list[dict]:
    """Raw GDELT v2 export snapshots for one day (DOC API is rate-limited).

    Streams the four 15-minute snapshots, filters oil-chain rows (actor match
    or petro-state + conflict root + |Goldstein|>=5), dedupes by
    (actor pair, root code) and returns the strongest rows with source URLs.
    """
    import io
    import zipfile

    rows: dict[tuple, dict] = {}
    for slot in slots:
        url = f"http://data.gdeltproject.org/gdeltv2/{compact(day)}{slot}.export.CSV.zip"
        request = urllib.request.Request(url, headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue  # 15-minute gap, normal in the archive
            raise
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            name = archive.namelist()[0]
            text = archive.read(name).decode("utf-8", "replace")
        for line in text.splitlines():
            fields = line.split("\t")
            if len(fields) < 58:
                continue
            actor1 = f"{fields[5]} {fields[6]}"
            actor2 = f"{fields[15]} {fields[16]}"
            country1, country2 = fields[7], fields[17]
            root = fields[28]
            try:
                goldstein = abs(float(fields[30]))
            except ValueError:
                continue
            source_url = fields[-1].strip()
            oil_signal = (
                OIL_ACTOR_PATTERN.search(actor1)
                or OIL_ACTOR_PATTERN.search(actor2)
                or OIL_ACTOR_PATTERN.search(source_url)
            )
            petro_conflict = (
                (country1 in PETRO_STATES or country2 in PETRO_STATES)
                and root in CONFLICT_ROOTS
                and goldstein >= 5.0
            )
            if not (oil_signal or petro_conflict) or not source_url.startswith("http"):
                continue
            try:
                mentions = int(fields[31])
            except ValueError:
                mentions = 0
            # Salience: Goldstein magnitude + media traction; one event per
            # (actor pair, root code), keep the strongest snapshot.
            score = goldstein + mentions / 100.0
            key = (fields[5], fields[15], root)
            if key not in rows or score > rows[key]["score"]:
                rows[key] = {
                    "title": f"{fields[6] or fields[5]} - {fields[16] or fields[15]} ({fields[30]})",
                    "url": source_url,
                    "goldstein": goldstein,
                    "mentions": mentions,
                    "score": score,
                    "root": root,
                }
        time.sleep(1.5)
    return sorted(rows.values(), key=lambda item: -item["score"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--price-history", type=Path, required=True)
    parser.add_argument("--since", default="2015-02-18", help="GDELT v2 DOC coverage start")
    parser.add_argument("--threshold", type=float, default=0.025)
    parser.add_argument("--top-days", type=int, default=200, help="largest |return| days first")
    parser.add_argument("--per-day", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-requests", type=int, default=250)
    parser.add_argument("--mode", choices=("doc", "export"), default="export",
                        help="export = raw v2 snapshot files (DOC API is rate-limited)")
    parser.add_argument("--skip-top", type=int, default=0,
                        help="skip the N largest-|return| days (for parallel shards)")
    parser.add_argument("--shard", default="",
                        help="i/n — process only the i-th of n interleaved shards after skip-top")
    parser.add_argument("--all-days", action="store_true",
                        help="anchor every trading day (threshold=0, 2 slots)")
    args = parser.parse_args()

    brent = json.loads(args.price_history.read_text())["brent"]
    if args.all_days:
        args.threshold = 0.0
    days = extreme_days(brent, since=args.since, threshold=args.threshold)
    days.sort(key=lambda item: -abs(item[1]))
    selected = days[args.skip_top: args.skip_top + args.top_days]
    if args.shard:
        index, total = (int(part) for part in args.shard.split("/"))
        selected = selected[index::total]
    print(f"extreme days >= {args.threshold:.1%}: {len(days)} | fetching top {len(selected)}")

    seen_urls: set[str] = set()
    cases: list[dict] = []
    for index, (day, ret) in enumerate(selected):
        if index >= args.max_requests:
            print("request budget reached")
            break
        try:
            if args.mode == "export":
                raw_rows = fetch_day_export(
                    day, slots=SLOTS_QUIET if args.all_days else SLOTS
                )
                articles = [
                    {"title": row["title"], "url": row["url"], "domain": ""}
                    for row in raw_rows
                ]
            else:
                articles = fetch_day(day)
        except Exception as exc:  # noqa: BLE001 - one bad day must not kill the run.
            print(f"{day} FAILED {type(exc).__name__}", flush=True)
            time.sleep(REQUEST_INTERVAL_SECONDS)
            continue
        kept = 0
        for article in articles:
            if kept >= args.per_day:
                break
            title = str(article.get("title") or "").strip()
            url = str(article.get("url") or "").strip()
            if not title or not url:
                continue
            if args.mode == "doc" and not RELEVANT.search(title):
                continue
            digest = hashlib.sha256(url.encode()).hexdigest()[:10]
            if digest in seen_urls:
                continue
            seen_urls.add(digest)
            cases.append(
                {
                    "case_id": f"backfill-t2-{day.replace('-', '')}-{digest}",
                    "event_date": day,
                    "brent_return_pct": ret,
                    "title": title[:160],
                    "url": url,
                    "domain": str(article.get("domain") or ""),
                    "language": str(article.get("language") or "eng"),
                    "posterior": {str(h): posterior(brent, day, h) for h in (1, 7, 30)},
                    "visible_at": (datetime.fromisoformat(day) + timedelta(days=30)).date().isoformat(),
                }
            )
            kept += 1
        print(f"{day} ret={ret:+.1f}% articles={len(articles)} kept={kept} total={len(cases)}", flush=True)
        time.sleep(REQUEST_INTERVAL_SECONDS)

    args.output.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"cases: {len(cases)} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

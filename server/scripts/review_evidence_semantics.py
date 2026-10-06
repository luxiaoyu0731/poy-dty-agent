"""Operator-run, cached semantic reviews; public HTTP inputs, no production DB.

A cumulative reservation ledger includes failed attempts. No browser request
runs this worker. Repeating a run reuses source/model/prompt-version receipts.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import math
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.deepseek_client import DeepSeekClient  # noqa: E402
from app.evidence_semantic_review import POLICY, SYSTEM, article_identity, validate_review  # noqa: E402
from app.prediction_evidence_runtime import publication_time  # noqa: E402
from app.prediction_inputs import digest  # noqa: E402


def atomic_json(path: Path, data: dict) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".review-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ReviewBudget:
    """Reserve worst-case cost before HTTP; never refund failed/unknown usage."""

    def __init__(self, directory: Path, cap: float):
        self.path = directory / "budget.json"
        self.lock = directory / ".budget.lock"
        self.cap = min(cap, 350.0)
        if not math.isfinite(cap) or self.cap <= 0:
            raise ValueError("review_budget_must_be_positive")

    def reserve(self, amount: float) -> None:
        with self.lock.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            data = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {"cap_cny": self.cap, "reserved_cny": 0.0, "attempts": 0}
            )
            # Increasing a persisted cap requires a separate operator decision.
            cap = min(self.cap, data["cap_cny"])
            if (
                not math.isfinite(amount)
                or amount <= 0
                or not math.isfinite(data["reserved_cny"])
                or data["reserved_cny"] < 0
                or data["reserved_cny"] + amount > cap
            ):
                raise RuntimeError("semantic_review_budget_exhausted")
            data.update(
                cap_cny=cap, reserved_cny=round(data["reserved_cny"] + amount, 6), attempts=data["attempts"] + 1
            )
            atomic_json(self.path, data)


async def run(args) -> dict:
    directory = Path(args.output_dir)
    if directory.is_symlink():
        raise ValueError("review_directory_symlink_forbidden")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    now = datetime.now(UTC)
    async with httpx.AsyncClient(timeout=120) as http:
        # One global newest-200 page is dominated by quotation feeds. Bound each
        # source tier/product read, then deduplicate before review; no time-window
        # widening, collection writes or manufactured cross-product material.
        queries = [
            {
                "tier": tier,
                "published_after": (now - timedelta(days=day + 1)).isoformat(),
                "published_before": (now - timedelta(days=day)).isoformat(),
            }
            for day in range(7)
            for tier in ("A", "B")
        ] + [
            *({"q": product} for product in ("crude", "原油", "PTA", "乙二醇", "POY", "DTY")),
        ]
        unique = {}
        for query in queries:
            response = await http.get(
                args.api_base.rstrip("/") + "/api/v1/news/articles",
                params={"published_after": (now - timedelta(days=7)).isoformat(), "limit": 200, **query},
            )
            response.raise_for_status()
            for article in response.json():
                unique[article_identity(article)] = article
        articles = sorted(unique.values(), key=lambda article: publication_time(article), reverse=True)
    client = DeepSeekClient()
    if not client.api_key:
        raise ValueError("semantic_review_provider_key_missing")
    client.max_output_tokens = 2500
    client.max_retries = 0
    client.timeout_s = 90
    budget = ReviewBudget(directory, args.budget_cny)
    accepted, skipped, failures, entries = 0, 0, [], []
    candidates = []
    import re

    for article in articles:
        raw = article.get("raw_text", "")
        if article.get("tier") not in {"A", "B"} or len(raw) < 200 or len(raw.encode()) > 40_000:
            skipped += 1
            continue
        # Exclude dedicated quotation tables, not event stories mentioning prices.
        if re.search(r"商品报价动态|价格动态|报价为|最新报价|商品价格走势图", article["title"]):
            skipped += 1
            continue
        if not re.search(
            r"停产|重启|减产|库存|航运|油轮|海峡|制裁|政策|\b(?:production|inventor\w*|tanker|strait|sanctions?|supply|exports?|imports?)\b",
            raw,
            re.I,
        ):
            skipped += 1
            continue
        candidates.append(article)
    for article in candidates[: args.limit]:
        identity = digest({"article": article_identity(article), "model": client.model, "prompt": digest(SYSTEM)})
        cache = directory / f"{identity}.json"
        if cache.exists():
            entries.append(json.loads(cache.read_text()))
            continue
        prompt = json.dumps(
            {"title": article["title"], "published_at": article["published_at"], "raw_text": article["raw_text"]},
            ensure_ascii=False,
        )
        # UTF-8 bytes upper-bound input tokens with extra framing allowance.
        # Deliberately conservative tariffs exceed current direct-provider rates;
        # actual usage is recorded separately, never used to release reservations.
        reservation = ((len((SYSTEM + prompt).encode()) + 1000) * 20 + 2500 * 50) / 1_000_000
        client.set_http_attempt_budget(1, on_attempt=lambda _, cost=reservation: budget.reserve(cost))
        try:
            data = await client._post_chat_completion(
                [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}], json_mode=True
            )
            result = json.loads(client._completion_content(data))
            reviewed_at = datetime.now(UTC).isoformat()
            reviews = []
            rejected = []
            for row in result.get("reviews", [])[:6]:
                try:
                    validate_review(article, row, reviewed_at=reviewed_at, model=client.model)
                    reviews.append(row)
                except (ValueError, KeyError, TypeError) as exc:
                    rejected.append({"reason": str(exc), "proposal": row})
            entry = {
                "policy": POLICY,
                "article": article,
                "reviews": reviews,
                "rejected": rejected,
                "reviewed_at": reviewed_at,
                "model": client.model,
                "usage": data.get("usage", {}),
                "reservation_cny": reservation,
            }
            atomic_json(cache, entry)
            entries.append(entry)
            accepted += len(reviews)
            print(
                json.dumps(
                    {
                        "article_id": article["article_id"],
                        "accepted": len(reviews),
                        "reservation_cny": round(reservation, 4),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (ValueError, RuntimeError, httpx.HTTPError, KeyError) as exc:
            failures.append({"article_id": article["article_id"], "error_type": type(exc).__name__})
            if str(exc) == "semantic_review_budget_exhausted":
                break
    # Merge cached earlier articles; keep exact sources and period validation.
    for cache in sorted(directory.glob("*.json")):
        if cache.name in {"latest.json", "budget.json", "summary.json", "associations.json"}:
            continue
        entry = json.loads(cache.read_text())
        current_identity = digest(
            {"article": article_identity(entry["article"]), "model": client.model, "prompt": digest(SYSTEM)}
        )
        if cache.stem != current_identity:
            continue
        if entry not in entries and publication_time(entry["article"]) >= now - timedelta(days=7):
            entries.append(entry)
    packet = {"policy": POLICY, "generated_at": datetime.now(UTC).isoformat(), "articles": entries[:200]}
    packet["content_sha256"] = digest(packet)
    if len(json.dumps(packet, ensure_ascii=False).encode()) > 4_000_000:
        raise ValueError("review_manifest_size_exceeded")
    atomic_json(directory / "latest.json", packet)
    summary = {
        "source_articles": len(articles),
        "candidates": len(candidates),
        "processed": len(entries),
        "accepted_new": accepted,
        "skipped": skipped,
        "failures": failures,
        "budget": json.loads(budget.path.read_text()) if budget.path.exists() else {"attempts": 0, "reserved_cny": 0},
    }
    atomic_json(directory / "summary.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--budget-cny", type=float, default=350)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    if not 1 <= args.limit <= 200:
        parser.error("limit must be 1..200")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))

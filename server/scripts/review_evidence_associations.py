"""Operator-run second-round repairs of cached source-bound review failures.

Uses the same persistent cumulative budget. A separate model call judges every
cross-sentence association; readers never invoke either model call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from review_evidence_semantics import ReviewBudget, atomic_json  # noqa: E402

from app.deepseek_client import DeepSeekClient  # noqa: E402
from app.evidence_semantic_bindings import CRITIC, POLICY, literal_span  # noqa: E402
from app.evidence_semantic_review import SYSTEM, article_identity, validate_review  # noqa: E402
from app.prediction_inputs import digest  # noqa: E402

ANALYST = (
    SYSTEM
    + """
这是一次原文核验失败后的重新抽取。不要照抄旧答案，只分析原始正文。
跨句关联时，quote保留事件本句，scope_quote保留原料/主体身份句，
另给binding_quote：原文中连续的一段（<=2400字符），同时包含两句及其实际连接上下文。
只有文章明确把主体/设施/群组成员和事件连接起来时才能提供；同地区不能当连接。
不要凭同篇价格讨论确定受影响原料。不把别种燃料库存/炼化困境映射为原油供应损失。
物流袭击可以解释运输风险，但实际损失及交付是否受阻尚待核验，理由必须写可能/条件，不能宣称已经减供。
this week/本周是从来源发布日所在周的周一到发布日的报告区间，不是唯一发生日；
time_kind=report_period，time_anchor逐字保留该词组，start/end采用该区间。
未给事件日也未给当前报告区间的已结束事件不要输出。
quote必须足以承载主体及动作，不要选择只有宏观评论或比较价格的句子。
仍然最多6项；宁可空数组，不修造原文。"""
)


async def completion(client, budget, system, payload):
    prompt = json.dumps(payload, ensure_ascii=False)
    reservation = ((len((system + prompt).encode()) + 1000) * 20 + 2500 * 50) / 1_000_000
    client.set_http_attempt_budget(1, on_attempt=lambda _: budget.reserve(reservation))
    result = await client._post_chat_completion(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}], json_mode=True
    )
    return json.loads(client._completion_content(result)), result.get("usage", {}), reservation


async def run(args):
    directory = Path(args.output_dir)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("private_existing_review_directory_required")
    os.chmod(directory, 0o700)
    packet = json.loads((directory / "latest.json").read_text())
    if packet["content_sha256"] != digest({k: v for k, v in packet.items() if k != "content_sha256"}):
        raise ValueError("source_manifest_integrity_failed")
    client = DeepSeekClient()
    if not client.api_key:
        raise ValueError("semantic_review_provider_key_missing")
    client.max_output_tokens, client.max_retries, client.timeout_s = 2500, 0, 90
    budget = ReviewBudget(directory, args.budget_cny)
    candidates = []
    for entry in packet["articles"][:200]:
        failures = []
        for row in [*entry.get("reviews", []), *(r["proposal"] for r in entry.get("rejected", []))]:
            try:
                validate_review(entry["article"], row, reviewed_at=entry["reviewed_at"], model=entry["model"])
            except (ValueError, TypeError, KeyError) as exc:
                failures.append(str(exc))
        if failures:
            candidates.append((entry, failures))
    # Association failures are the actual capability gap; do not merely re-pay
    # every article in search of a desired number of approvals.
    candidates.sort(
        key=lambda pair: (
            -sum(
                reason
                in {
                    "semantic_product_not_uniquely_bound",
                    "semantic_action_product_clause_mismatch",
                    "semantic_period_not_source_bound",
                    "semantic_date_not_source_bound",
                }
                for reason in pair[1]
            )
        )
    )
    attempts, accepted, errors = 0, 0, []
    for entry, failures in candidates[: args.limit]:
        article = entry["article"]
        identity = digest(
            {
                "article": article_identity(article),
                "model": client.model,
                "analysis": digest(ANALYST),
                "critic": digest(CRITIC),
            }
        )
        original_cache = directory / f"association-{identity}.json"
        cache = (
            directory / f"association-{identity}-aligned-bindings.json" if original_cache.is_file() else original_cache
        )
        if cache.is_file() and cache != original_cache:
            continue
        try:
            if original_cache.is_file():
                previous = json.loads(original_cache.read_text())
                result = {
                    "reviews": [*previous.get("reviews", []), *(r["proposal"] for r in previous.get("rejected", []))]
                }
                receipts = []
            else:
                result, usage, reserve = await completion(
                    client,
                    budget,
                    ANALYST,
                    {
                        "title": article["title"],
                        "published_at": article["published_at"],
                        "raw_text": article["raw_text"],
                        "previous_validation_failures": sorted(set(failures)),
                    },
                )
                attempts += 1
                receipts = [{"stage": "extract", "usage": usage, "reservation_cny": reserve}]
            rows, rejected = [], []
            for proposal in result.get("reviews", [])[:6]:
                row = dict(proposal)
                row.pop("binding_proof", None)
                for field in ("quote", "scope_quote"):
                    if row.get(field):
                        row[field] = literal_span(article["raw_text"], row[field]) or row[field]
                for field in ("subject", "action", "time_anchor"):
                    if row.get(field):
                        row[field] = literal_span(row["quote"], row[field]) or row[field]
                binding_quote = row.pop("binding_quote", "")
                needs_binding = False
                try:
                    validate_review(article, row, reviewed_at=datetime.now(UTC).isoformat(), model=client.model)
                except (ValueError, KeyError, TypeError) as exc:
                    if str(exc) not in {
                        "semantic_product_not_uniquely_bound",
                        "semantic_action_product_clause_mismatch",
                    }:
                        rejected.append({"reason": str(exc), "proposal": row})
                        continue
                    needs_binding = True
                if needs_binding and not binding_quote and row.get("scope_quote"):
                    raw = article["raw_text"]
                    quote_at, scope_at = raw.find(row["quote"]), raw.find(row["scope_quote"])
                    if quote_at >= 0 and scope_at >= 0:
                        first = min(quote_at, scope_at)
                        last = max(quote_at + len(row["quote"]), scope_at + len(row["scope_quote"]))
                        if last - first <= 2400:
                            binding_quote = raw[first:last]
                if binding_quote and needs_binding:
                    # Reject invented text before sending a second paid request.
                    if binding_quote not in article["raw_text"] or len(binding_quote) > 2400:
                        rejected.append({"reason": "binding_not_literal_source", "proposal": row})
                        continue
                    verdict, usage, reserve = await completion(
                        client,
                        budget,
                        CRITIC,
                        {
                            "binding_quote": binding_quote,
                            "raw_text": article["raw_text"],
                            "candidate": row,
                            "source_published_at": article["published_at"],
                        },
                    )
                    attempts += 1
                    receipts.append({"stage": "critic", "usage": usage, "reservation_cny": reserve})
                    proof = {
                        "policy": POLICY,
                        "proposal_sha256": digest(row),
                        "source_content_hash": article["content_hash"],
                        "binding_quote": binding_quote,
                        "verdict": verdict,
                        "model": client.model,
                        "reviewed_at": datetime.now(UTC).isoformat(),
                    }
                    proof["content_sha256"] = digest(proof)
                    row["binding_proof"] = proof
                now = datetime.now(UTC).isoformat()
                try:
                    validate_review(article, row, reviewed_at=now, model=client.model)
                    rows.append(row)
                except (ValueError, KeyError, TypeError) as exc:
                    rejected.append({"reason": str(exc), "proposal": row})
            record = {
                "policy": POLICY,
                "article": article,
                "reviews": rows,
                "rejected": rejected,
                "reviewed_at": datetime.now(UTC).isoformat(),
                "model": client.model,
                "analysis_prompt_sha256": digest(ANALYST),
                "critic_prompt_sha256": digest(CRITIC),
                "receipts": receipts,
            }
            atomic_json(cache, record)
            accepted += len(rows)
            print(
                json.dumps({"article_id": article["article_id"], "accepted": len(rows)}, ensure_ascii=False), flush=True
            )
        except (ValueError, TypeError, KeyError, RuntimeError, httpx.HTTPError) as exc:
            errors.append({"article_id": article["article_id"], "error_type": type(exc).__name__})
            if str(exc) == "semantic_review_budget_exhausted":
                break
    records = []
    for cache in sorted(directory.glob("association-*.json")):
        if cache.is_symlink() or cache.stat().st_size > 1_000_000:
            continue
        record = json.loads(cache.read_text())
        if (
            record.get("policy") == POLICY
            and record.get("analysis_prompt_sha256") == digest(ANALYST)
            and record.get("critic_prompt_sha256") == digest(CRITIC)
        ):
            records.append(record)
    sidecar = {"policy": POLICY, "generated_at": datetime.now(UTC).isoformat(), "articles": records[:100]}
    sidecar["content_sha256"] = digest(sidecar)
    if len(json.dumps(sidecar, ensure_ascii=False).encode()) > 4_000_000:
        raise ValueError("association_manifest_size_exceeded")
    atomic_json(directory / "associations.json", sidecar)
    return {
        "processed_sources": len(records),
        "accepted_new": accepted,
        "new_attempts": attempts,
        "errors": errors,
        "budget": json.loads(budget.path.read_text()),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--budget-cny", type=float, default=350)
    parser.add_argument("--limit", type=int, default=12)
    args = parser.parse_args()
    if not 1 <= args.limit <= 40:
        parser.error("limit must be 1..40")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))

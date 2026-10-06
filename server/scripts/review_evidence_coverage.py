"""Bounded source-extraction/independent association pilot on a sealed manifest.

No database writes, no votes, no HTTP reads from UI; shared cumulative budget.
Receipts retain exact original articles, proposals, rejection reasons and usage.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "server"))
from dotenv import load_dotenv  # noqa: E402 - runtime script imports after SERVER_ROOT is added
from scripts.experiments.campaign_budget import (  # noqa: E402 - runtime script imports after SERVER_ROOT is added
    CampaignBudget,
    MeteredJsonClient,
    atomic,
)

from app.deepseek_client import DeepSeekClient  # noqa: E402 - runtime script imports after SERVER_ROOT is added
from app.evidence_semantic_bindings import CRITIC  # noqa: E402 - runtime script imports after SERVER_ROOT is added
from app.evidence_semantic_bindings import POLICY as BINDING_POLICY  # noqa: E402
from app.evidence_semantic_review import (  # noqa: E402 - runtime script imports after SERVER_ROOT is added
    POLICY,
    SYSTEM,
    validate_review,
)
from app.prediction_inputs import digest  # noqa: E402 - runtime script imports after SERVER_ROOT is added

EXTRACTION = (
    SYSTEM
    + """
再次提醒：不得为了填满页面而提供不合格事实；只报真实逐字原文可支撑的项。
报价、柴油/LNG困难、采购来源替换不冒充原油供应减少。
quote 可以完整包含日期和归属所必需的同句上下文，不要截成丢失限定词的碎片。
如果设施事件需跨句关联，请在 reviews 外另提供 associations:[{review_index,binding_quote}]。
binding_quote 必须是包含该 review 的 quote 和 scope_quote 的原文连续片段（<=2400字符）。
scope_quote 必须明确写该原料。另一独立核验员会检查是否同一设施/运输群组，不能用标题或常识替代原文。
当前状态必须确实仍存在；没有实际动作日可选 current_state，但不掩盖本句已给出的日期。
最多4项，宁缺毋滥。
"""
)


def validate_packet(packet: dict) -> list[dict]:
    if packet.get("policy") != POLICY or packet.get("content_sha256") != digest(
        {k: v for k, v in packet.items() if k != "content_sha256"}
    ):
        raise ValueError("coverage_input_manifest_integrity_failed")
    return packet["articles"]


def binding_proof(article: dict, row: dict, binding: str, verdict: dict, model: str, reviewed_at: str) -> dict:
    if not isinstance(binding, str) or len(binding) > 2400 or binding not in article["raw_text"]:
        raise ValueError("binding_not_literal_source_span")
    if not row.get("scope_quote") or row["scope_quote"] not in binding or row["quote"] not in binding:
        raise ValueError("binding_does_not_contain_both_anchors")
    proof = {
        "policy": BINDING_POLICY,
        "proposal_sha256": digest(row),
        "source_content_hash": article["content_hash"],
        "binding_quote": binding,
        "verdict": verdict,
        "model": model,
        "reviewed_at": reviewed_at,
    }
    proof["content_sha256"] = digest(proof)
    return proof


async def recorded_completion(client, output: Path, messages: list[dict]) -> dict:
    identity = digest({"messages": messages, "model": client.model})
    path = output / f"response-{identity}.json"
    if path.exists():
        packet = json.loads(path.read_text())
        if packet.get("content_sha256") != digest({k: v for k, v in packet.items() if k != "content_sha256"}):
            raise ValueError("coverage_raw_response_changed")
        return packet["response"]
    client.set_http_attempt_budget(1)
    response = await client._post_chat_completion(messages, json_mode=True)
    packet = {"request_sha256": identity, "response": response, "received_at": datetime.now(UTC).isoformat()}
    packet["content_sha256"] = digest(packet)
    atomic(path, packet)
    return response


async def run(args):
    packet = json.loads(Path(args.input_manifest).read_text())
    entries = validate_packet(packet)
    selected = [e["article"] for e in entries if e["article"]["article_id"] in args.article_id]
    if len(selected) != len(set(args.article_id)) or not 1 <= len(selected) <= 8:
        raise ValueError("coverage_selection_missing_or_unbounded")
    output = Path(args.output_dir)
    if output.is_symlink():
        raise ValueError("coverage_output_symlink_forbidden")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    load_dotenv(args.env_file, override=False)
    client = MeteredJsonClient(DeepSeekClient(), CampaignBudget(Path(args.ledger_dir)))
    client.client.timeout_s = 90
    receipts = []
    for article in selected:
        request = {"title": article["title"], "published_at": article["published_at"], "raw_text": article["raw_text"]}
        identity = digest({"article": article["content_hash"], "prompt": digest(EXTRACTION), "model": client.model})
        path = output / f"{identity}.json"
        if path.exists():
            receipt = json.loads(path.read_text())
            if receipt.get("content_sha256") != digest({k: v for k, v in receipt.items() if k != "content_sha256"}):
                raise ValueError("coverage_receipt_changed")
            receipts.append(receipt)
            continue
        response = await recorded_completion(
            client,
            output,
            [
                {"role": "system", "content": EXTRACTION},
                {"role": "user", "content": json.dumps(request, ensure_ascii=False)},
            ],
        )
        result = json.loads(client._completion_content(response))
        if not isinstance(result.get("reviews"), list):
            raise ValueError("coverage_reviews_not_array")
        associations = {
            r["review_index"]: r["binding_quote"]
            for r in result.get("associations", [])
            if isinstance(r, dict) and type(r.get("review_index")) is int and isinstance(r.get("binding_quote"), str)
        }
        accepted, rejected, critics = [], [], []
        for index, proposal in enumerate(result["reviews"][:4]):
            row = dict(proposal)
            reviewed_at = datetime.now(UTC).isoformat()
            try:
                validate_review(article, row, reviewed_at=reviewed_at, model=response.get("model", client.model))
            except (ValueError, KeyError, TypeError) as exc:
                if (
                    str(exc) not in {"semantic_product_not_uniquely_bound", "semantic_action_product_clause_mismatch"}
                    or index not in associations
                ):
                    rejected.append({"proposal": row, "reason": str(exc)})
                    continue
                binding = associations[index]
                # Literal span guards run BEFORE any critic spends money.
                binding_proof(article, row, binding, {}, client.model, reviewed_at)
                critic_input = {
                    "quote": row["quote"],
                    "scope_quote": row.get("scope_quote"),
                    "source_target": row["source_target"],
                    "rationale": row.get("rationale"),
                    "conditions": row.get("conditions"),
                    "binding_quote": binding,
                }
                answer = await recorded_completion(
                    client,
                    output,
                    [
                        {"role": "system", "content": CRITIC},
                        {"role": "user", "content": json.dumps(critic_input, ensure_ascii=False)},
                    ],
                )
                verdict = json.loads(client._completion_content(answer))
                critics.append({"review_index": index, "response": answer})
                reviewed_at = datetime.now(UTC).isoformat()
                row["binding_proof"] = binding_proof(
                    article, row, binding, verdict, answer.get("model", client.model), reviewed_at
                )
                try:
                    validate_review(article, row, reviewed_at=reviewed_at, model=response.get("model", client.model))
                except (ValueError, KeyError, TypeError) as error:
                    rejected.append({"proposal": row, "reason": str(error)})
                    continue
            accepted.append(row)
        now = datetime.now(UTC).isoformat()
        receipt = {
            "policy": POLICY,
            "article": article,
            "model": response.get("model", client.model),
            "reviewed_at": now,
            "reviews": accepted,
            "rejected": rejected,
            "response": response,
            "critics": critics,
            "prompt_sha256": digest(EXTRACTION),
        }
        receipt["content_sha256"] = digest(receipt)
        atomic(path, receipt)
        receipts.append(receipt)
        print(
            json.dumps(
                {
                    "article_id": article["article_id"],
                    "accepted": len(accepted),
                    "rejected": len(rejected),
                    "critic_calls": len(critics),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    manifest = {"policy": POLICY, "generated_at": datetime.now(UTC).isoformat(), "articles": receipts}
    manifest["content_sha256"] = digest(manifest)
    if len(json.dumps(manifest).encode()) > 4_000_000:
        raise ValueError("coverage_manifest_too_large")
    atomic(output / "latest.json", manifest)
    return {
        "sources": len(receipts),
        "accepted": sum(len(r["reviews"]) for r in receipts),
        "rejected": sum(len(r["rejected"]) for r in receipts),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("input-manifest", "ledger-dir", "env-file", "output-dir"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--article-id", action="append", required=True)
    print(json.dumps(asyncio.run(run(parser.parse_args())), ensure_ascii=False))

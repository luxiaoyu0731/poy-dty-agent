"""Operator-approved live trial; evidence eligibility is independent of activation."""

from __future__ import annotations

import os
from copy import deepcopy

POLICY = "live-memory-trial.v1"
BACKGROUND_INSTRUCTION = (
    "教训仅作为背景假设，不是门槛、置信上限或方向命令。"
    "必须用本次事件的原文和执行证据独立判断；教训与本次证据矛盾时以本次证据为准。"
    "引用 lesson_id 说明适用条件，不得仅因历史成败将本次判断机械降级。"
    "模拟导入教训未经线上效果验收，不得视为已验证事实。"
)


def voting_enabled() -> bool:
    return os.getenv("AGENT_MEMORY_RECALL_ENABLED", "0") == "1" and os.getenv("AGENT_MEMORY_VOTING_ENABLED", "0") == "1"


def reflection_enabled() -> bool:
    return os.getenv("AGENT_REFLECTION_BACKGROUND_ENABLED", "0") == "1"


def scope_analog(output: dict, product: str, cards: dict[str, dict]) -> dict:
    """A cited recall cannot lend its aggregate votes to a different product/term.

    Mixed legacy/recall counts cannot be safely decomposed. Only the strongest
    validated cited group supplies support when any recall is selected.
    """
    result = deepcopy(output)
    cited = {item.get("case_id") for item in output.get("analog_top3", []) if isinstance(item, dict)}
    selected = [cards[c] for c in cited if c in cards]
    # Unknown recall ids must fail closed rather than fall back to model counts.
    if not selected and not any(str(c).startswith("recall-") for c in cited):
        return result
    scoped = {}
    for horizon in ("d1", "d7", "d30"):
        requested = (output.get("prior_by_horizon") or {}).get(horizon) or output.get("prior") or {}
        eligible = [
            c
            for c in selected
            if c["product"] == product
            and c["horizon"] == horizon
            and c["price_direction"] == requested.get("direction")
        ]
        strongest = max(eligible, key=lambda c: c["support_count"], default=None)
        scoped[horizon] = {
            "direction": strongest["price_direction"] if strongest else "neutral",
            "support_count": strongest["support_count"] if strongest else 0,
            "median_magnitude_pct": strongest["median_magnitude_pct"] if strongest else None,
            "case_ids": [strongest["case_id"]] if strongest else [],
        }
    result["prior_by_horizon"] = scoped
    result["prior"] = {"direction": "neutral", "support_count": 0}
    result["recall_scope_policy"] = "cited-product-horizon-only.v1"
    return result


def verified_report_cards(report: dict, event_id: str) -> dict[str, dict]:
    """Revalidate stored receipts at the rule boundary, including event ownership."""
    from .unified_memory import recall_case_cards

    memory = report.get("memory") or {}
    if not memory.get("voting_enabled"):
        return {}
    result = {}
    for receipt in memory.get("recalls") or []:
        if receipt.get("event_id") != event_id or receipt.get("as_of_time") != report.get("as_of_time"):
            continue
        for card in recall_case_cards(receipt):
            result[card["case_id"]] = card
    return result

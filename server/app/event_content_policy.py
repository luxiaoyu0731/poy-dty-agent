"""Conservative price-only exclusion for isolated candidate acceptance.

This is a candidate role classifier, never a mechanism verification or vote.
Unknown text remains eligible; a mixed price/shock item is preserved. The
policy must be explicitly selected, so deployment alone cannot change issuance.
"""
from __future__ import annotations

import json
import re

LEGACY_CANDIDATE_POLICY = "event-candidates.v1"
NON_PRICE_CANDIDATE_POLICY = "event-candidates.v2-non-price"

_PRICE = re.compile(
    r"报价|价格|结算价|收盘|涨价|跌价|涨跌|行情|\b(?:prices?|pricing|quotations?|settlement|brent|wti)\b",
    re.I,
)
# Do not treat general references to supply/demand as an event. These cues
# describe an action or measurable non-price state; qualification stays with
# the downstream evidence checker (including intentions/forecasts).
_NON_PRICE = re.compile(
    r"停产|停工|检修|复产|投产|减产|增产|装置|开工率|库存|产量|进口|出口|订单|"
    r"制裁|禁运|封锁|袭击|轰炸|火灾|爆炸|罢工|港口关闭|通航|运力|关税|限产|"
    r"\b(?:shutdowns?|outages?|maintenance|restart\w*|inventor\w*|production|"
    r"imports?|exports?|orders?|sanctions?|embargo\w*|blockade\w*|attack\w*|"
    r"bomb\w*|fires?|explosion\w*|strikes?|tariffs?|opec|output|capacity)\b",
    re.I,
)


def candidate_content_role(title: str, facts_json: str) -> str:
    try:
        facts = json.loads(facts_json or "[]")
    except (ValueError, TypeError):
        facts = []
    texts = [str(title or "")]
    if isinstance(facts, list):
        for fact in facts:
            if isinstance(fact, dict):
                # Only source assertions, not generated inferences or paths.
                texts.extend(str(fact.get(key) or "") for key in ("text", "statement", "quote", "evidence_quote"))
    text = "\n".join(texts)
    if _NON_PRICE.search(text):
        return "event_or_mixed"
    if _PRICE.search(text):
        return "price_only"
    return "unclassified"

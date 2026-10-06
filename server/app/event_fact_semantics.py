"""Conservative numeric roles and qualifications, independent of model judgement."""

from __future__ import annotations

import re

from .models import EventFactExtraction


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text).casefold()


def source_context(source: str, quote: str) -> str:
    """Keep the preceding attribution, which a model can omit from its quote."""
    text, needle = compact(source), compact(quote)
    index = text.find(needle)
    if index < 0:
        return ""
    start = max(text.rfind(mark, 0, index) for mark in "。！？\n") + 1
    return text[max(start, index - 100) : index + len(needle)]


def qualification(text: str) -> str:
    if re.search(r"传闻|传言|未经证实|据悉|rumou?r|unconfirmed", text, re.I):
        return "unconfirmed"
    if re.search(r"否认|不属实|没有发生|未重启|未恢复|denied", text, re.I):
        return "denied"
    if re.search(r"预计|计划|拟于|拟将|有望|预期|forecast|expected|planned", text, re.I):
        return "planned"
    if re.search(r"重启中|恢复中|逐步提升|正在恢复", text):
        return "in_progress"
    return "reported"


def semantic_reasons(facts: EventFactExtraction, source: str) -> list[str]:
    """Do not let exact digits/quotes stand in for units or epistemic status."""
    reasons = []
    lead = compact(facts.subject + facts.action + facts.object)
    for quote in facts.evidence_quotes:
        context = source_context(source, quote)
        if qualification(context) == "unconfirmed" and qualification(lead) != "unconfirmed":
            reasons.append("dropped_source_qualification:unconfirmed")
        # Bind the qualifier to the action being asserted, not unrelated forecasts
        # in a long article or an estimated duration of an already announced event.
        for action in ("重启", "恢复", "停产", "投产", "复工", "出料"):
            if action not in lead:
                continue
            if re.search(rf"(?:预计|计划|拟将|有望)[^，。；]{{0,16}}{action}", context) and not re.search(
                r"预计|计划|拟将|有望|尚未", lead
            ):
                reasons.append("dropped_source_qualification:planned")
            if action + "中" in context and action + "中" not in lead:
                reasons.append("dropped_source_qualification:in_progress")
    # A level and its change in one price statement must not silently switch currency.
    # Different countries' independent quotes elsewhere in the article are allowed.
    for sentence in re.split(r"[。！？\n]", source):
        if re.search(r"(?:上涨|下跌|涨|跌)\s*\d+(?:\.\d+)?美元/吨[^。]*价格为\s*\d+[\d.\-–]*元/吨", sentence) and any(
            compact(q) in compact(sentence) for q in facts.evidence_quotes
        ):
            reasons.append("source_price_currency_conflict")
    return list(dict.fromkeys(reasons))


def level_object(facts: EventFactExtraction) -> str:
    """Render 'rose TO a level' only with a level anchor, never infer from digits."""
    obj = facts.object
    if facts.action not in {"上涨", "下跌"}:
        return obj
    value = obj.removeprefix(facts.subject).strip()
    if not re.fullmatch(r"\d+(?:[.,]\d+)*(?:美元/桶|美元/吨|元/吨)?", value):
        return obj
    for number in facts.numbers:
        if compact(value) not in {compact(number.value), compact(number.value + number.unit)}:
            continue
        quote = compact(number.evidence_quote)
        # A delta has to stay a delta, including when its context says "price".
        if re.search(r"(?:价|价格|报价|结算价)(?:为|报|至|达到)" + re.escape(compact(value)), quote):
            return "至" + value
    return obj

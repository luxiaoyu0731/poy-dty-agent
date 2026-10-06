from __future__ import annotations

import html
import json
import re
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import ValidationError

from .models import (
    EventBusinessImpact,
    EventFactExtraction,
    EventSummaryQualityResult,
)

InputQuality = Literal["full_text", "partial_text", "title_only"]

_GROUNDING_MEMO: ContextVar[tuple[OrderedDict, list[int]] | None] = ContextVar("grounding_memo", default=None)
_GROUNDING_MAX_ENTRIES = 1024
_GROUNDING_MAX_BYTES = 32 * 1024 * 1024


@contextmanager
def grounding_validation_scope():
    """Reuse identical pure checks during one capture, never across requests.

    Frozen-input reconstruction still runs and compares every derived field.
    Returned models are copied so one caller cannot alter another's proof.
    """
    token = _GROUNDING_MEMO.set((OrderedDict(), [0]))
    try:
        yield
    finally:
        _GROUNDING_MEMO.reset(token)

_DISCLAIMER_TERMS = (
    "仅供参考",
    "作为ai",
    "作为 ai",
    "无法判断",
    "不构成建议",
    "请自行核实",
    "i cannot",
    "as an ai",
)
_MISSING_VALUES = {"", "未说明", "原文未说明", "未知", "unknown", "n/a", "null"}
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:[.,]\d+)*(?:%)?")
_ENGLISH_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_ENGLISH_NUMBERS = {
    1: ("one", "first"),
    2: ("two", "second"),
    3: ("three", "third"),
    4: ("four", "fourth"),
    5: ("five", "fifth"),
    6: ("six", "sixth"),
    7: ("seven", "seventh"),
    8: ("eight", "eighth"),
    9: ("nine", "ninth"),
    10: ("ten", "tenth"),
    11: ("eleven", "eleventh"),
    12: ("twelve", "twelfth"),
}
_NON_CONTENT_BLOCK_RE = re.compile(r"(?is)<(?:nav|footer|form|aside)[^>]*>.*?</(?:nav|footer|form|aside)>")
_BOILERPLATE_RE = re.compile(
    r"(?i)\b(?:skip to main content|main navigation|sign in(?: to read)?|log in(?: to read)?|"
    r"cookie settings|manage cookies|accept all cookies|enable javascript(?: and cookies)? to continue)\b"
)
_HAN_CHARACTER_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_LETTER_RE = re.compile(r"[A-Za-z]")
_PROMPT_INJECTION_RE = re.compile(
    r"(?i)(?:忽略|无视|绕过|覆盖|取消).{0,20}(?:系统|上述|先前|规则|指令)|"
    r"(?:ignore|disregard|override|bypass).{0,30}(?:system|previous|prior|instruction|rule)"
)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


_MEDIA_COUNTER_RE = re.compile(r"\b(?:subscribe|subscribers|followers)\s*[:：]?\s*[\d,]+\b", re.I)


def has_media_counter_contamination(facts: dict[str, Any], source_text: str) -> bool:
    """Do not treat a UI counter flattened next to a caption as its quantity."""
    original = _normalize(source_text).casefold()
    cleaned = _normalize(_MEDIA_COUNTER_RE.sub(" ", source_text)).casefold()
    for number in facts.get("numbers", []):
        if not isinstance(number, dict):
            continue
        quote = _normalize(str(number.get("evidence_quote") or "")).casefold()
        context = str(number.get("context") or "").casefold()
        if any(term in context for term in ("订阅", "粉丝", "subscriber", "follower")):
            return True
        if quote and quote in original and quote not in cleaned:
            return True
    return False


def clean_event_source_text(value: str) -> str:
    """Remove presentation chrome before source text reaches a model or customer."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<(?:script|style)\b[^>]*>.*?</(?:script|style)[^>]*>", " ", text)
    text = _NON_CONTENT_BLOCK_RE.sub(" ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(
        r"(?i)(?:https?://|/)[^\s]+\.(?:png|jpe?g|gif|webp|svg)(?:\?[^\s]*)?",
        " ",
        text,
    )
    text = _BOILERPLATE_RE.sub(" ", text)
    text = _MEDIA_COUNTER_RE.sub(" ", text)
    # Standalone controls are common in text extracted from institutional sites.
    text = re.sub(r"(?i)(?:^|[|·•\n])\s*(?:menu|search|close)\s*(?=$|[|·•\n])", " ", text)
    return re.sub(r"\s+", " ", text).strip(" -|·•\t\r\n")


def is_customer_chinese_summary(value: str) -> bool:
    """Accept natural Chinese summaries while allowing necessary Latin acronyms."""
    text = _normalize(value)
    han_count = len(_HAN_CHARACTER_RE.findall(text))
    latin_count = len(_LATIN_LETTER_RE.findall(text))
    # Customer copy must be Chinese prose, not an English excerpt with a short
    # Chinese prefix. Product and institution acronyms (PX/PTA/OPEC/EIA) remain
    # valid within otherwise Chinese narration.
    return han_count >= 8 and han_count / max(han_count + latin_count, 1) >= 0.5


def _contains_disclaimer(value: str) -> bool:
    lowered = _normalize(value).lower()
    return any(term in lowered for term in _DISCLAIMER_TERMS)


def _source_without_instruction_sentences(value: str) -> str:
    # Decimal points belong to quantities. Splitting 9242.50 into "9242. 50"
    # destroys the exact numeric anchor and rejects otherwise grounded facts.
    sentences = re.split(r"(?<=[。！？!?])\s*|(?<=\.)\s+", _normalize(value))
    return " ".join(sentence for sentence in sentences if not _PROMPT_INJECTION_RE.search(sentence))


def _han_bigrams(value: str) -> set[str]:
    compact = "".join(_HAN_CHARACTER_RE.findall(_normalize(value)))
    return {compact[index : index + 2] for index in range(max(0, len(compact) - 1))}


def _core_fact_supported(value: str, source_text: str, source_language: str) -> bool:
    """Require translated Chinese claims to retain anchors when the source is Chinese."""
    claim = _normalize(value)
    if not claim:
        return False
    source = _source_without_instruction_sentences(source_text)
    if not source_language.lower().startswith("zh") and len(_HAN_CHARACTER_RE.findall(source)) < 20:
        return True
    if claim in source:
        return True
    claim_bigrams = _han_bigrams(claim)
    if not claim_bigrams:
        return False
    source_bigrams = _han_bigrams(source)
    return len(claim_bigrams & source_bigrams) / len(claim_bigrams) >= 0.34


def parse_model_json(output: str | dict[str, Any]) -> dict[str, Any]:
    """Extract the first JSON object even when a provider wraps it in prose/fences."""
    if isinstance(output, dict):
        return output
    text = _normalize(str(output))
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("invalid_model_json")


def _quote_key(value: str) -> str:
    # Ignore only HTML inline-layout whitespace before punctuation.
    return re.sub(r"\s+([,，;；!?！？])", r"\1", _normalize(value)).casefold()


def _supported_quote(quote: str, source_text: str) -> bool:
    return bool(_quote_key(quote)) and _quote_key(quote) in _quote_key(source_text)


def _normalized_number(value: str) -> str:
    return re.sub(r"(?<=\d)[,_](?=\d)", "", _normalize(value))


def _numeric_value_present(number: str, source_text: str) -> bool:
    """Numeric-equivalence fallback for traceability.

    A summary number is traceable when the source contains the same numeric
    value, allowing leading-zero differences ("08" vs "8") and rounding to the
    summary's precision ("18.8" summarising source "18.83"). The source value
    must still exist; this never accepts numbers absent from the source.
    """

    try:
        target = Decimal(_normalized_number(number))
    except InvalidOperation:
        return False
    digits_after_point = max(0, -target.as_tuple().exponent)
    for match in re.finditer(r"\d[\d,]*(?:\.\d+)?", source_text):
        raw = match.group(0).replace(",", "")
        try:
            candidate = Decimal(raw)
        except InvalidOperation:
            continue
        if candidate == target:
            return True
        # Integer-looking summary values (including leading-zero forms such as
        # "08") require exact numeric equality. Rounding 7.6 to 8 would weaken
        # the traceability gate rather than normalize formatting.
        candidate_precision = max(0, -candidate.as_tuple().exponent)
        if digits_after_point == 0 or candidate_precision <= digits_after_point:
            continue
        try:
            if candidate.quantize(Decimal(1).scaleb(-digits_after_point), rounding=ROUND_HALF_UP) == target:
                return True
        except InvalidOperation:
            continue
    return False


def _number_is_source_supported(number: str, source_text: str, source_language: str) -> bool:
    normalized = _normalized_number(number)
    source = _normalized_number(source_text)
    # Percentage spellings are formatting equivalents, not new evidence.
    # Require an explicit source percentage and exact numeric equality; a bare
    # quantity, a different magnitude or a rounded rate cannot support it.
    percentage = re.fullmatch(r"(\d+(?:\.\d+)?)\s*%", normalized)
    if percentage:
        expected = Decimal(percentage[1])
        pattern = r"(?<![0-9A-Za-z_.])(\d+(?:\.\d+)?)\s*(?:%|percent\b|per\s+cent\b)"
        return any(Decimal(match[1]) == expected for match in re.finditer(pattern, source, re.I))
    if re.fullmatch(r"\d[\d,]*(?:\.\d+)?", number.strip()) and _numeric_value_present(normalized, source):
        return True
    if normalized in source:
        return True
    if source_language.lower().startswith("en") and normalized.isdigit():
        number = int(normalized)
        source_folded = source.casefold()
        if 1 <= number <= 12:
            month = _ENGLISH_MONTHS[number - 1]
            if re.search(rf"\b{month}\b", source_folded):
                return True
            # Publisher bylines commonly use Sep 11 2026. An abbreviation is
            # accepted only as part of an explicit, valid calendar date.
            for match in re.finditer(rf"\b{month[:3]}\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b", source_folded):
                try:
                    datetime(int(match[2]), number, int(match[1]))
                    return True
                except ValueError:
                    continue
        return any(re.search(rf"\b{re.escape(word)}\b", source_folded) for word in _ENGLISH_NUMBERS.get(number, ()))
    return False


def _natural_chinese_datetime(value: str) -> str:
    text = _normalize(value)
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return text
    rendered = f"{parsed.year}年{parsed.month}月{parsed.day}日"
    if "T" in text or re.search(r"\d{1,2}:\d{2}", text):
        rendered += f" {parsed.hour:02d}:{parsed.minute:02d}"
    return rendered


def _localized_calendar_value(value: str) -> str | None:
    """Localize recognized source date values without inventing a year or day."""
    months = "|".join(_ENGLISH_MONTHS)
    patterns = (
        rf"(?P<day>\d{{1,2}})\s+(?P<month>{months})\s+(?P<year>\d{{4}})",
        rf"(?P<month>{months})\s+(?P<day>\d{{1,2}}),?\s+(?P<year>\d{{4}})",
        rf"(?P<month>{months})\s+(?P<year>\d{{4}})",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value, re.IGNORECASE)
        if not match:
            continue
        month = _ENGLISH_MONTHS.index(match["month"].casefold()) + 1
        year = int(match["year"])
        day = match.groupdict().get("day")
        try:
            datetime(year, month, int(day) if day else 1)
        except ValueError:
            return None
        return f"{year}年{month}月" + (f"{int(day)}日" if day else "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        localized = _natural_chinese_datetime(value)
        return localized if localized != value else None
    return None


def _localized_quantity(value: str, unit: str) -> tuple[str, str]:
    raw_value = _normalize(value)
    raw_unit = _normalize(unit)
    calendar_value = _localized_calendar_value(raw_value)
    if calendar_value:
        # Dates carry their own year/month/day units. A model-provided label
        # such as "日期" or "产量" must not be appended as a quantity unit.
        return calendar_value, ""
    qualifier = ""
    qualifier_match = re.fullmatch(
        r"(?i)(more than|over|approximately|about|around)\s+(.+)",
        raw_value,
    )
    if qualifier_match:
        qualifier = {
            "more than": "超过",
            "over": "超过",
            "approximately": "约",
            "about": "约",
            "around": "约",
        }[qualifier_match.group(1).casefold()]
        raw_value = qualifier_match.group(2).strip()
    raw_value = re.sub(r"(?i)(\d+)(?:st|nd|rd|th)\b", r"\1", raw_value)
    english_approximation = {
        "dozens": "数十",
        "hundreds": "数百",
        "thousands": "数千",
    }.get(raw_value.casefold())
    if english_approximation:
        raw_value = english_approximation
    for number, words in _ENGLISH_NUMBERS.items():
        if raw_value.casefold() == words[0]:
            raw_value = str(number)
            break
    combined = f"{raw_value} {raw_unit}".casefold()
    scale = Decimal(1)
    if re.search(r"\bbillion\b", combined):
        scale = Decimal(1_000_000_000)
    elif re.search(r"\bmillion\b", combined):
        scale = Decimal(1_000_000)
    elif re.search(r"\bthousand\b", combined):
        scale = Decimal(1_000)
    number_text = re.sub(r"(?i)\b(?:billion|million|thousand)\b", "", raw_value).strip()
    try:
        number = Decimal(number_text.replace(",", "")) * scale
    except InvalidOperation:
        number = None
    if number is not None and scale != 1:
        if number >= Decimal(100_000_000):
            display = number / Decimal(100_000_000)
            value_text, magnitude = f"{display.normalize():f}", "亿"
        else:
            display = number / Decimal(10_000)
            value_text, magnitude = f"{display.normalize():f}", "万"
    else:
        value_text, magnitude = raw_value, ""
    normalized_unit = re.sub(r"(?i)\b(?:billion|million|thousand)\b", "", raw_unit).strip(" /")
    unit_key = normalized_unit.casefold()
    unit_text = {
        "barrel": "桶",
        "barrels": "桶",
        "bbl": "桶",
        "bbls": "桶",
        "barrels per day": "桶/日",
        "barrel per day": "桶/日",
        "bpd": "桶/日",
        "metric tons": "吨",
        "metric tonnes": "吨",
        "tons": "吨",
        "tonnes": "吨",
    }.get(unit_key, normalized_unit or raw_unit)
    return f"{qualifier}{value_text}", f"{magnitude}{unit_text}"


_MEDIA_ASSET_METRIC_TERMS = (
    "视频时长",
    "音频时长",
    "下载量",
    "浏览量",
    "观看量",
    "播放量",
    "分辨率",
    "文件大小",
    "video analytics",
    "high-res",
    "downloads",
    "views",
    "length:",
    "duration",
    "file size",
    "resolution",
)


def _is_media_asset_metric(context: str, evidence_quote: str) -> bool:
    combined = f"{context} {evidence_quote}".casefold()
    return any(term.casefold() in combined for term in _MEDIA_ASSET_METRIC_TERMS)


def _generic_sanctions_number(facts: EventFactExtraction, quote: str) -> bool:
    # Keep rule changes themselves. Omit standard enforcement boilerplate only
    # from the reading summary, retaining the original structured fact/quote.
    lead = f"{facts.action} {facts.object}"
    if any(term in lead for term in ("规则", "条例", "门槛", "奖励", "持股", "所有权")):
        return False
    text = _quote_key(quote)
    return ("monetary penalties exceeding" in text or
            ("50 percent or more" in text and "blocked persons" in text))


def _render_factual_summary(facts: EventFactExtraction) -> str:
    from .event_fact_semantics import level_object

    lead = facts.subject
    if facts.action:
        lead += facts.action
    if facts.object:
        object_text = level_object(facts)
        if (facts.action in {"上涨", "下跌"} and object_text.startswith(facts.subject)
                and re.fullmatch(r"\d+(?:[.,]\d+)*", object_text[len(facts.subject):].strip())):
            object_text = "至" + object_text[len(facts.subject):].strip()
        lead += object_text
    details = [
        _natural_chinese_datetime(facts.occurred_at) if facts.occurred_at else "",
        facts.location,
    ]
    details = [item for item in details if item]
    if details:
        lead += "，" + "，".join(details)
    for number in facts.numbers:
        if (_is_media_asset_metric(number.context, number.evidence_quote)
                or _generic_sanctions_number(facts, number.evidence_quote)):
            continue
        value, unit = _localized_quantity(number.value, number.unit)
        if value.endswith("%") and unit in {"%", "percent", "百分比"}:
            unit = ""
        # A change verb followed by a magnitude is not a price level.
        # “收跌为12元/吨” incorrectly reads as a settlement price of 12.
        is_change = re.search(r"(?:收涨|收跌|上涨|下跌|增加|减少|下降|上升|下滑)$", number.context.strip())
        connector = "" if is_change else "为"
        lead += f"；{number.context}{connector}{value} {unit}".rstrip()
    return lead.rstrip("。") + "。"


def _normalize_fact_payload(output: str | dict[str, Any]) -> dict[str, Any]:
    payload = parse_model_json(output)
    normalized = dict(payload)
    for key in ("subject", "action", "object", "occurred_at", "location", "source_language"):
        normalized[key] = str(payload.get(key) or "")
    quotes = payload.get("evidence_quotes")
    normalized["evidence_quotes"] = [str(item) for item in quotes] if isinstance(quotes, list) else []
    numbers = payload.get("numbers")
    normalized["numbers"] = (
        [
            {
                "value": str(item.get("value") or ""),
                "unit": str(item.get("unit") or ""),
                "context": str(item.get("context") or ""),
                "evidence_quote": str(item.get("evidence_quote") or ""),
            }
            for item in numbers
            if isinstance(item, dict)
        ]
        if isinstance(numbers, list)
        else []
    )
    return normalized


def _normalize_impact_payload(output: str | dict[str, Any]) -> dict[str, Any]:
    payload = parse_model_json(output)
    normalized = dict(payload)
    normalized["relevant"] = bool(payload.get("relevant"))
    normalized["relevance_reason"] = str(payload.get("relevance_reason") or "")
    for key in ("transmission_path", "invalidation_conditions", "gaps"):
        value = payload.get(key)
        normalized[key] = [str(item) for item in value] if isinstance(value, list) else []
    direction = str(payload.get("direction") or "不确定")
    normalized["direction"] = direction if direction in {"利多", "利空", "中性", "不确定"} else "不确定"
    return normalized


def build_grounded_event_summary(
    *,
    source_text: str,
    input_quality: InputQuality,
    fact_output: str | dict[str, Any],
    impact_output: str | dict[str, Any],
) -> EventSummaryQualityResult:
    memo = _GROUNDING_MEMO.get()
    if memo is None:
        return _build_grounded_event_summary(
            source_text=source_text, input_quality=input_quality, fact_output=fact_output, impact_output=impact_output
        )
    try:
        # Parsing both representations mirrors the validator's existing entry
        # point: inventory and dossier may supply the same JSON as text/dict.
        payloads = [parse_model_json(fact_output), parse_model_json(impact_output)]

        def json_native(value):
            if type(value) in (str, int, float, bool, type(None)):
                return True
            if type(value) is list:
                return all(json_native(item) for item in value)
            return type(value) is dict and all(type(k) is str and json_native(v) for k, v in value.items())

        if not all(json_native(payload) for payload in payloads):
            raise ValueError("non_json_grounding_payload")
        key = json.dumps(
            [source_text, input_quality, *payloads], ensure_ascii=False, sort_keys=True, allow_nan=False
        )
        size = len(key.encode())
    except (ValueError, TypeError, RecursionError):
        return _build_grounded_event_summary(
            source_text=source_text, input_quality=input_quality, fact_output=fact_output, impact_output=impact_output
        )
    entries, used = memo
    if key in entries:
        entries.move_to_end(key)
        return entries[key][0].model_copy(deep=True)
    result = _build_grounded_event_summary(
        source_text=source_text, input_quality=input_quality, fact_output=fact_output, impact_output=impact_output
    )
    size += len(result.model_dump_json().encode())
    if size <= _GROUNDING_MAX_BYTES:
        while entries and (len(entries) >= _GROUNDING_MAX_ENTRIES or used[0] + size > _GROUNDING_MAX_BYTES):
            _, (_, removed_size) = entries.popitem(last=False)
            used[0] -= removed_size
        entries[key] = (result.model_copy(deep=True), size)
        used[0] += size
    return result


def _build_grounded_event_summary(
    *,
    source_text: str,
    input_quality: InputQuality,
    fact_output: str | dict[str, Any],
    impact_output: str | dict[str, Any],
) -> EventSummaryQualityResult:
    """Validate facts and impact independently; a bad/irrelevant impact cannot erase facts."""
    reasons: list[str] = []
    if input_quality != "full_text":
        reasons.append("insufficient_source_text")
    if len(_normalize(source_text)) < 40:
        reasons.append("source_text_too_short")

    try:
        normalized_fact_payload = _normalize_fact_payload(fact_output)
        if not _normalize(str(normalized_fact_payload.get("object") or "")):
            return EventSummaryQualityResult(
                status="rejected",
                usable=False,
                input_quality=input_quality,
                fact_summary_status="rejected",
                rejection_reasons=[*reasons, "missing_object"],
            )
        facts = EventFactExtraction.model_validate(normalized_fact_payload)
    except (ValidationError, ValueError, TypeError):
        return EventSummaryQualityResult(
            status="rejected",
            usable=False,
            input_quality=input_quality,
            fact_summary_status="rejected",
            rejection_reasons=[*reasons, "invalid_fact_structure"],
        )

    if has_media_counter_contamination(facts.model_dump(), source_text):
        reasons.append("media_counter_contamination")
    from .event_fact_semantics import semantic_reasons

    reasons.extend(semantic_reasons(facts, source_text))
    subject = _normalize(facts.subject).lower()
    action = _normalize(facts.action).lower()
    object_text = _normalize(facts.object).lower()
    if subject in _MISSING_VALUES or _contains_disclaimer(subject):
        reasons.append("missing_subject")
    if action in _MISSING_VALUES or _contains_disclaimer(action):
        reasons.append("missing_action")
    if object_text in _MISSING_VALUES or _contains_disclaimer(object_text):
        reasons.append("missing_object")
    supported_core_fields = (
        _core_fact_supported(facts.subject, source_text, facts.source_language),
        _core_fact_supported(facts.action, source_text, facts.source_language),
        _core_fact_supported(facts.object, source_text, facts.source_language),
    )
    if not all(supported_core_fields):
        reasons.append("unsupported_core_fact")
    if subject and action and (subject.endswith(action) or action.endswith(subject)):
        reasons.append("repetitive_fact_composition")

    serialized = json.dumps({"facts": facts.model_dump()}, ensure_ascii=False)
    if _contains_disclaimer(serialized):
        reasons.append("disclaimer_pollution")

    rendered_factual_summary = _render_factual_summary(facts)
    if not is_customer_chinese_summary(rendered_factual_summary):
        reasons.append("non_chinese_factual_summary")
    for field_name, value in (
        ("subject", facts.subject),
        ("action", facts.action),
        ("object", facts.object),
        ("location", facts.location),
    ):
        normalized_value = _normalize(value)
        # A Chinese name followed by its original name/acronym is still Chinese
        # prose. Reject predominantly English sentences, not embedded names.
        language_text = re.sub(r"[（(][A-Za-z][A-Za-z .&-]*[）)]", "", normalized_value)
        latin_count = len(_LATIN_LETTER_RE.findall(language_text))
        han_count = len(_HAN_CHARACTER_RE.findall(language_text))
        english_phrase = latin_count >= 8 and " " in language_text and latin_count > han_count
        action_without_chinese = field_name == "action" and not _HAN_CHARACTER_RE.search(normalized_value)
        if normalized_value and (english_phrase or action_without_chinese):
            reasons.append(f"non_chinese_fact_field:{field_name}")

    grounded_quotes = [quote for quote in facts.evidence_quotes if _supported_quote(quote, source_text)]
    distinct_quotes = {_quote_key(quote) for quote in grounded_quotes}
    if len(grounded_quotes) != len(facts.evidence_quotes) or len(distinct_quotes) < 2:
        reasons.append("unsupported_evidence_quote")

    source_compact = _normalized_number(source_text)
    for number in facts.numbers:
        value = _normalized_number(number.value)
        quote = _normalized_number(number.evidence_quote)
        if (
            not value
            or not _number_is_source_supported(value, quote, facts.source_language)
            or quote.casefold() not in source_compact.casefold()
        ):
            reasons.append(f"untraceable_number:{number.value}")

    auditable_fact_claims = json.dumps(
        {
            "subject": facts.subject,
            "action": facts.action,
            "object": facts.object,
            "occurred_at": facts.occurred_at,
            "location": facts.location,
        },
        ensure_ascii=False,
    )
    for claim_number in _NUMBER_RE.findall(auditable_fact_claims):
        if not _number_is_source_supported(claim_number, source_text, facts.source_language):
            reasons.append(f"untraceable_number:{claim_number}")

    # Impact must never survive a failed fact/input gate. Keeping this boundary
    # here protects non-DeepSeek callers as well as the production worker.
    reasons = list(dict.fromkeys(reasons))
    if reasons:
        return EventSummaryQualityResult(
            status="rejected",
            usable=False,
            input_quality=input_quality,
            fact_summary_status="rejected",
            facts=facts,
            rejection_reasons=reasons,
            impact_analysis_status="not_requested",
        )

    impact_reasons: list[str] = []
    try:
        impact = EventBusinessImpact.model_validate(_normalize_impact_payload(impact_output))
    except (ValidationError, ValueError, TypeError):
        impact = None
        impact_reasons.append("invalid_impact_structure")
    if impact is not None:
        impact_serialized = json.dumps(impact.model_dump(), ensure_ascii=False)
        if _contains_disclaimer(impact_serialized):
            impact_reasons.append("disclaimer_pollution")
        for claim_number in _NUMBER_RE.findall(impact_serialized):
            if not _number_is_source_supported(claim_number, source_text, facts.source_language):
                impact_reasons.append(f"untraceable_number:{claim_number}")
        if impact.relevant and (
            not _normalize(impact.relevance_reason)
            or not impact.transmission_path
            or not impact.invalidation_conditions
        ):
            impact_reasons.append("incomplete_business_impact")
    impact_reasons = list(dict.fromkeys(impact_reasons))

    if impact is None:
        return EventSummaryQualityResult(
            status="completed",
            usable=True,
            input_quality=input_quality,
            fact_summary_status="completed",
            impact_analysis_status="rejected",
            factual_summary=rendered_factual_summary,
            facts=facts,
            impact_quality_reasons=["invalid_impact_structure"],
        )
    impact_status = "irrelevant" if not impact.relevant else "rejected" if impact_reasons else "completed"
    return EventSummaryQualityResult(
        status="completed",
        usable=True,
        input_quality=input_quality,
        fact_summary_status="completed",
        impact_analysis_status=impact_status,
        factual_summary=rendered_factual_summary,
        facts=facts,
        business_impact=impact,
        impact_quality_reasons=impact_reasons,
    )

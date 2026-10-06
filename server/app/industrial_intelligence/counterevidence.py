"""Separate denial discovery from conservative, dated proposition matching.

Candidate discovery is not a truth judgement. Attribution, uncertain dates and
unresolved paraphrases never become event counterclaims just to improve recall.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

POLICY_VERSION = "explicit-counter.v2"
MAX_EXCERPT_CHARS = 12000
MAX_PROPOSITION_CHARS = 350

_DENIAL_PATTERNS = (
    re.compile(
        r'[“「"](?P<claim>[^”」"\n]{12,350})[”」"](?:的)?'
        r"(?:报道|消息|传闻|说法|声明)(?:并)?(?:不实|为假|是假的|不属实)"
    ),
    re.compile(r'(?:否认|澄清)[：:\s]*[“「"](?P<claim>[^”」"\n]{12,350})[”」"]'),
    re.compile(
        r"\b(?:the\s+)?(?:claim|reports?|rumou?rs?)\s+that\s+"
        r"(?P<claim>[^!?\n]{20,350}?)\s+(?:is|are|was|were)\s+"
        r"(?:false|untrue|incorrect|unfounded)\b",
        re.I,
    ),
)
_DATE = re.compile(r"(?<!\d)(20\d{2})(?:年|[-/])(\d{1,2})(?:月|[-/])(\d{1,2})日?(?!\d)")
_MONTHS = {
    name: i
    for i, name in enumerate(
        (
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
        ),
        1,
    )
}
_EN_DATE = re.compile(r"\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2}),?\s+(20\d{2})\b", re.I)
_DIRECT_DENIAL = re.compile(r"(?:否认(?:了)?[：:\s]*|\bden(?:y|ies|ied)\s+(?:that\s+)?)(?P<claim>.+)", re.I)
_DENIAL_LEAD = re.compile(r"否认|辟谣|不实|不属实|\bden(?:y|ies|ied|ial)\b|\brules? out\b", re.I)
_UNSAFE = re.compile(
    r"\b(?:if|may|might|could|would|will|expected|forecast|alleged|reportedly|"
    r"not|no|never|without|denied|false|untrue|rumou?r|claim|report(?:s|ed)?|considering|planned|potential|"
    r"ignore|instruction|system|assistant|prompt)\b|"
    r"如果|假如|可能|或将|预计|计划|考虑|据称|传闻|消息称|报道|否认|不实|未|没有|"
    r"澄清|辟谣|忽略|指令|提示词|系统角色",
    re.I,
)
_INSTRUCTION = re.compile(
    r"ignore\s+(?:all|previous)|system\s*prompt|"
    r"\b(?:assistant|developer)\s*:|忽略.*指令|系统提示词",
    re.I,
)


@dataclass(frozen=True)
class ExplicitDenial:
    proposition: str
    quote: str
    observation_date: str | None
    matchable: bool = True
    blocked_reason: str | None = None


def sentences(text: str) -> list[str]:
    """Keep decimal points, numeric signs and common English abbreviations."""
    result, start = [], 0
    for i, char in enumerate(text):
        boundary = char in "。!?！？;；\n"
        if char == ".":
            before, after = text[max(0, i - 12) : i], text[i + 1 : i + 2]
            decimal = bool(before and before[-1].isdigit() and after.isdigit())
            abbreviation = bool(re.search(r"(?:\b[A-Za-z]\.)*[A-Za-z]$", before)) and (
                bool(after and after.isalpha()) or bool(re.search(r"\b(?:[A-Za-z]\.)+[A-Za-z]$", before))
            )
            common = bool(re.search(r"\b(?:Mr|Mrs|Ms|Dr|Prof|vs|Inc|Ltd)$", before, re.I))
            boundary = not (decimal or abbreviation or common)
        if boundary:
            if text[start:i].strip():
                result.append(text[start:i].strip())
            start = i + 1
    if text[start:].strip():
        result.append(text[start:].strip())
    return result


def _date_matches(text: str):
    for match in _DATE.finditer(text):
        yield match, tuple(map(int, match.groups()))
    for match in _EN_DATE.finditer(text):
        yield match, (int(match[3]), _MONTHS[match[1].casefold()], int(match[2]))


def normalized_proposition(text: str) -> str:
    """Normalize typography and *complete* dates, never entities or numbers."""
    normalized = unicodedata.normalize("NFKC", text).casefold().strip()
    normalized = _DATE.sub(lambda m: f"{int(m[1]):04}-{int(m[2]):02}-{int(m[3]):02}", normalized)
    normalized = _EN_DATE.sub(lambda m: f"{int(m[3]):04}-{_MONTHS[m[1].casefold()]:02}-{int(m[2]):02}", normalized)
    # A period between digits is part of a number, never typography. Preserve
    # signs, units, ranges and decimal precision; 8.55 must not equal 855.
    normalized = re.sub(r"(?<!\d)[.,]|[.,](?!\d)", "", normalized)
    return re.sub(r"[\s。!！?？；;：:\"“”「」]+", "", normalized)


def _unsafe_proposition(text: str) -> bool:
    # May in a complete calendar date is not a speculative modal verb.
    return bool(_UNSAFE.search(_EN_DATE.sub("", _DATE.sub("", text))))


def _observation_date(text: str) -> str | None:
    dates = set()
    for _, components in _date_matches(text):
        try:
            dates.add(date(*components).isoformat())
        except ValueError:
            return None
    return next(iter(dates)) if len(dates) == 1 else None


def _dated_claim_key(text: str) -> str:
    """Bounded paraphrase normalization; date is checked separately by caller.

    No entity, geography, quantity, qualification or actor aliases are inferred.
    Moving an explicit date or replacing the two documented Chinese action pairs
    does not require whole-source semantic inference.
    """
    text = re.sub(r"\bon\s+(?=(?:20\d{2}|" + "|".join(_MONTHS) + r")\b)", "", text, flags=re.I)
    text = re.sub(r"于(?=20\d{2}(?:年|[-/]))", "", text)
    text = _EN_DATE.sub("", _DATE.sub("", text))
    text = text.replace("发生火灾", "起火").replace("停止生产", "停产")
    return normalized_proposition(text)


def denial_candidates(excerpt: str) -> list[ExplicitDenial]:
    """Retain undated/qualified leads for diagnostics, never auto-link them."""
    if not excerpt or _INSTRUCTION.search(excerpt):
        return []
    found: dict[str, ExplicitDenial] = {}
    for sentence in sentences(excerpt[:MAX_EXCERPT_CHARS]):
        matches = [m for pattern in _DENIAL_PATTERNS for m in pattern.finditer(sentence)]
        if not matches:
            match = _DIRECT_DENIAL.search(sentence)
            if match:
                matches = [match]
        for match in matches:
            proposition = match["claim"].strip(' “”。."')
            observed = _observation_date(proposition)
            prefix = sentence[: match.start()]
            # A hypothetical denial is not an actual source denial.
            if re.search(
                r"\b(?:if|might|may|could|would|not|never)\b|如果|假如|可能|未|没有|拒绝|不愿|不予",
                prefix,
                re.I,
            ):
                continue
            reason = "missing_unambiguous_claim_date" if not observed else None
            if _unsafe_proposition(proposition):
                reason = "qualified_or_negative_proposition"
            if re.match(r"^(?:其|该|它|他|她)|^(?:it|its|they|their|this|that)\b", proposition, re.I):
                reason = "unresolved_subject"
            if len(proposition) > MAX_PROPOSITION_CHARS:
                reason = "proposition_too_long"
            if re.search(
                r"该否认(?:不实|有误|已撤回)|the denial (?:is|was) (?:false|incorrect|retracted)", sentence, re.I
            ):
                reason = "denial_itself_disputed"
            found.setdefault(
                normalized_proposition(proposition),
                ExplicitDenial(
                    proposition=proposition,
                    quote=sentence,
                    observation_date=observed,
                    matchable=reason is None,
                    blocked_reason=reason,
                ),
            )
        lead = _DENIAL_LEAD.search(sentence)
        if (
            not matches
            and lead
            and not re.search(
                r"\b(?:not|never|if|might|may|could|would|failing|failed|refused)\b|未|没有|如果|可能|拒绝",
                sentence[: lead.start()],
                re.I,
            )
        ):
            found.setdefault(
                normalized_proposition(sentence),
                ExplicitDenial(
                    proposition=sentence,
                    quote=sentence,
                    observation_date=None,
                    matchable=False,
                    blocked_reason="unresolved_attribution_or_claim",
                ),
            )
    return list(found.values())


def explicit_denials(excerpt: str) -> list[ExplicitDenial]:
    return [candidate for candidate in denial_candidates(excerpt) if candidate.matchable]


def affirmative_match(excerpt: str, denial: ExplicitDenial) -> str | None:
    """Return the original affirmative sentence, never a title or paraphrase."""
    if not excerpt or _INSTRUCTION.search(excerpt):
        return None
    if not denial.matchable or not denial.observation_date:
        return None
    target = _dated_claim_key(denial.proposition)
    for sentence in sentences(excerpt[:MAX_EXCERPT_CHARS]):
        sentence = sentence.strip()
        if not sentence or len(sentence) > MAX_PROPOSITION_CHARS or _unsafe_proposition(sentence):
            continue
        if _observation_date(sentence) != denial.observation_date:
            continue
        # Whole-proposition equality prevents substring matches to a different
        # entity, negation, region, contract, unit or embedded quotation.
        if _dated_claim_key(sentence) == target:
            return sentence
    return None

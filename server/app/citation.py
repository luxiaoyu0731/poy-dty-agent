from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime

from .models import RagEvidence

TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9/_-]{1,}|[\u4e00-\u9fff]{2,}")
NUMBER_PATTERN = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?(?:\s*(?:元/吨|美元/桶|%|万吨|吨|张))?")
CONCEPT_GROUPS = {
    "price": ("价格", "现货", "报价", "成交价", "参考价", "price", "quote"),
    "inventory": ("库存", "库容", "stock", "inventory"),
    "operating_rate": ("开工", "负荷", "utilization", "operating rate"),
    "profit": ("利润", "现金流", "margin", "profit"),
    "increase": ("上涨", "上行", "上升", "增加", "走高", "推高", "increase", "rise", "up"),
    "decrease": ("下跌", "下降", "减少", "走低", "回落", "decrease", "fall", "down"),
    "sanction": ("制裁", "sanction", "ofac"),
    "shipping": ("航运", "运价", "船运", "shipping", "freight", "tanker"),
}
OPPOSITES = {("increase", "decrease"), ("decrease", "increase")}
ENTITY_ALIASES = {
    "POY": ("poy", "涤纶长丝"),
    "DTY": ("dty", "低弹丝"),
    "PX": ("px", "对二甲苯"),
    "PTA": ("pta", "精对苯二甲酸"),
    "MEG": ("meg", "乙二醇"),
    "crude_oil": ("原油", "crude oil", "crude", "wti", "brent"),
    "naphtha": ("石脑油", "naphtha"),
}
STOPWORDS = {
    "当前",
    "本次",
    "材料",
    "证据",
    "显示",
    "相关",
    "需要",
    "建议",
    "可能",
    "以及",
    "不能",
    "作为",
}

# 领域内中英术语表：中文论断 vs 英文标题级证据的词项重叠会因语言不同而塌陷
# （C07：中文"EIA预测美国原油产量创纪录"vs英文"EIA increases U.S. crude
# production forecast"仅命中专名）。支持度计算前按本表做双语概念归一；
# 阈值不变，不降低校验标准。EN 匹配使用词边界，避免子串误命中。
BILINGUAL_GLOSSARY: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("原油", "石油"), ("crude", "brent", "wti", "oil", "petroleum")),
    (("石脑油",), ("naphtha",)),
    (("预测",), ("forecast", "expect", "predict", "outlook", "project", "increases")),
    (("创纪录", "纪录"), ("record",)),
    (("产量",), ("production", "output")),
    (("美国",), ("united states", "u.s.", "american")),
    (("美元",), ("dollar", "usd")),
    (("上涨", "上行", "走高"), ("rise", "surge", "gain", "climb", "higher")),
    (("下跌", "下行", "回落"), ("fall", "decline", "drop", "slip", "lower")),
    (("出口",), ("export",)),
    (("进口",), ("import",)),
    (("需求",), ("demand",)),
    (("供应",), ("supply",)),
    (("制裁",), ("sanction",)),
    (("航运",), ("shipping", "freight")),
    (("聚酯", "涤纶"), ("polyester", "filament")),
    (("关税",), ("tariff",)),
    (("管道",), ("pipeline",)),
    (("油轮",), ("tanker",)),
    (("地缘",), ("geopolitic",)),
    (("停火",), ("ceasefire", "truce")),
    (("袭击",), ("strike", "attack")),
    (("库存",), ("inventory", "stocks")),
    (("开工", "负荷"), ("utilization", "operating rate")),
    (("增产",), ("raise output", "increase production")),
    (("减产",), ("production cut", "output cut")),
)



@dataclass(frozen=True)
class CitationBinding:
    text: str
    doc_ids: tuple[str, ...]
    supported: bool
    score: float


def _observed_day(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return ""
    return parsed.date().isoformat()


def strip_citation_metadata(text: str) -> str:
    # Reference syntax is not part of the claim and its identifier digits are
    # not prices/dates. IDs written by the model are never trusted as bindings.
    text = re.sub(r"\[(?:证据[:：]\s*)?[A-Za-z][\w-]*:[^\]\n]+\]", "", text)
    text = re.sub(r"[（(]\s*(?:doc_id|evidence_id)\s*[:：=][^）)\n]+[）)]", "", text)
    text = re.sub(r"(?:doc_id|evidence_id)\s*[:：=]\s*[^）)\]\n]+", "", text)
    text = re.sub(r"[,，]\s*([）)])", r"\1", text)
    return text.strip()


def _canonical_numbers(text: str) -> set[str]:
    result = set()
    # Exact unit aliases only: no FX conversion or spot/futures basis merging.
    text = re.sub(r"CNY\s*/\s*(?:mt|tonne|ton)\b", "元/吨", text, flags=re.I)
    text = re.sub(r"USD\s*/\s*bbl\b", "美元/桶", text, flags=re.I)
    for token in NUMBER_PATTERN.findall(text):
        match = re.match(r"(\d+(?:\.\d+)?)(.*)", token)
        if match:
            value = format(Decimal(match[1]).normalize(), "f")
            result.add(value + re.sub(r"\s+", "", match[2]))
    return result


def bind_claims_to_evidence(
    claims: list[str],
    evidence: list[RagEvidence],
    *,
    minimum_score: float = 0.35,
    max_citations: int = 2,
) -> list[CitationBinding]:
    """Bind each claim to the evidence whose own text overlaps it.

    This deliberately abstains when the local material does not support a claim;
    it never uses a global "first document" citation.
    """
    evidence_terms = [
        (
            item,
            _terms(
                " ".join((item.title, item.summary, item.snippet))
                + (" " + _observed_day(str(item.observed_at)) if item.observed_at else "")
            ),
        )
        for item in evidence
    ]
    bindings: list[CitationBinding] = []
    for claim in claims:
        clean_claim = strip_citation_metadata(claim)
        sentences = [part.strip() for part in re.split(r"(?<=[。！？；!?;])", clean_claim) if part.strip()]
        if len(sentences) == 1:
            # A comma can join independently stated product quotes. Split only
            # at an explicit next product, never arbitrary clauses or numbers.
            clauses = re.split(r"[，,]\s*(?=(?:而|其中)?(?:POY|DTY|PTA|PX|MEG|原油|石脑油)(?![A-Za-z]))",
                               clean_claim, flags=re.I)
            if len(clauses) > 1 and all(len(_entities(c)) == 1 for c in clauses):
                dates = _dates_in(clean_claim)
                if len(dates) == 1:
                    year, month, day = next(iter(dates))
                    prefix = f"{year}年{month}月{day}日 " if year else f"{month}月{day}日 "
                    clauses = [c if _dates_in(c) else prefix + c for c in clauses]
                sentences = clauses
        if len(sentences) > 1:
            # Each sentence must pass independently. Concatenating evidence
            # would incorrectly permit prices/dates to swap between products.
            parts = bind_claims_to_evidence(sentences, evidence, minimum_score=minimum_score,
                                           max_citations=max_citations)
            representatives = tuple(dict.fromkeys(part.doc_ids[0] for part in parts if part.doc_ids))
            supported = all(part.supported for part in parts) and len(representatives) <= max_citations
            bindings.append(CitationBinding(clean_claim, representatives if supported else (), supported,
                                            min(part.score for part in parts) if supported else 0.0))
            continue
        scoring_claim = clean_claim
        for item in evidence:
            # Known evidence identifiers are reference syntax, not factual numbers.
            if ":" in item.doc_id:
                scoring_claim = re.sub(
                    rf"(?<![\w:]){re.escape(item.doc_id)}(?![\w:])", "", scoring_claim,
                )
        claim_terms = _terms(scoring_claim)
        scored: list[tuple[float, str]] = []
        for item, terms in evidence_terms:
            if not claim_terms or not terms:
                continue
            # 证据自身的观察日期属于证据内容：评分文本须包含它，否则
            # "9-11 发改委调价"类论断会因正文未复述日期而被误判无支持。
            evidence_text = " ".join((item.title, item.summary, item.snippet))
            if item.observed_at:
                evidence_text += f" {_observed_day(item.observed_at)}"
            score = _support_score(scoring_claim, evidence_text, claim_terms, terms)
            if score >= minimum_score:
                scored.append((score, item.doc_id))
        scored.sort(key=lambda pair: (-pair[0], pair[1]))
        best_score = scored[0][0] if scored else 0.0
        selected = tuple(doc_id for score, doc_id in scored if score >= max(minimum_score, best_score * 0.8))[
            :max_citations
        ]
        bindings.append(
            CitationBinding(
                text=strip_citation_metadata(claim),
                doc_ids=selected,
                supported=bool(selected),
                score=round(scored[0][0], 4) if scored else 0.0,
            )
        )
    return bindings


def render_cited_claims(bindings: list[CitationBinding]) -> list[str]:
    rendered: list[str] = []
    for item in bindings:
        # Provider-written identifiers are not verified bindings. Render only
        # the references selected by the factual support check below.
        text = strip_citation_metadata(item.text)
        if not item.supported:
            rendered.append(f"{text}（上下文中未找到足够直接支持，作为待核验推断）")
            continue
        rendered.append(f"{text} [{'；'.join(item.doc_ids)}]")
    return rendered


def citation_faithfulness(bindings: list[CitationBinding]) -> dict[str, object]:
    total = len(bindings)
    supported = sum(1 for item in bindings if item.supported)
    return {
        "claim_count": total,
        "supported_claim_count": supported,
        "unsupported_claim_count": total - supported,
        "support_ratio": round(supported / total, 4) if total else 0.0,
        "bindings": [
            {
                "text": item.text,
                "doc_ids": list(item.doc_ids),
                "supported": item.supported,
                "score": item.score,
            }
            for item in bindings
        ],
    }


def evaluate_citation_bindings(
    bindings: list[CitationBinding],
    evidence: list[RagEvidence],
    *,
    conflict_doc_ids: set[str] | None = None,
) -> dict[str, object]:
    """Reproducible four-layer citation audit for one answer."""
    allowed = {item.doc_id: item for item in evidence}
    conflicts = set(conflict_doc_ids or ())
    items: list[dict[str, object]] = []
    for binding in bindings:
        present = bool(binding.doc_ids)
        invalid = [doc_id for doc_id in binding.doc_ids if doc_id not in allowed]
        valid_ids = [doc_id for doc_id in binding.doc_ids if doc_id in allowed]
        conflict_ids = [doc_id for doc_id in valid_ids if doc_id in conflicts or bool(allowed[doc_id].risk_flags)]
        items.append(
            {
                "text": binding.text,
                "citation_presence": present,
                "citation_validity": present and not invalid,
                "invalid_doc_ids": invalid,
                "valid_doc_ids": valid_ids,
                "evidence_entailment": binding.supported and binding.score > 0,
                "entailment_score": binding.score,
                "contradiction_or_conflict": bool(conflict_ids),
                "conflict_doc_ids": conflict_ids,
            }
        )
    total = len(items)
    return {
        "claim_count": total,
        "citation_presence_rate": _ratio(items, "citation_presence"),
        "citation_validity_rate": _ratio(items, "citation_validity"),
        "evidence_entailment_rate": _ratio(items, "evidence_entailment"),
        "conflict_count": sum(bool(item["contradiction_or_conflict"]) for item in items),
        "items": items,
    }


def _terms(text: str) -> set[str]:
    terms = {token.lower() for token in TOKEN_PATTERN.findall(text) if token.lower() not in STOPWORDS}
    cjk_runs = re.findall(r"[\u4e00-\u9fff]+", text)
    terms.update(run[index : index + 2] for run in cjk_runs for index in range(max(len(run) - 1, 0)))
    return terms


MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
DATE_PATTERNS = (
    re.compile(r"(?P<y>20\d{2})\s*[-/年]\s*(?P<m>\d{1,2})\s*[-/月]\s*(?P<d>\d{1,2})\s*日?"),
    re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"),
    re.compile(
        r"\b(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<d>\d{1,2})(?:,?\s*(?P<y>20\d{2}))?\b",
        re.I,
    ),
)


def _dates_in(text: str) -> set[tuple[int | None, int, int]]:
    dates: set[tuple[int | None, int, int]] = set()
    for pattern in DATE_PATTERNS:
        for match in pattern.finditer(text):
            groups = match.groupdict()
            month_raw = groups.get("m") or groups.get("mon")
            day_raw = groups.get("d")
            year_raw = groups.get("y")
            if month_raw is None or day_raw is None:
                continue
            month = MONTH_NUMBERS.get(str(month_raw).lower()[:3]) if not str(month_raw).isdigit() else int(month_raw)
            if not month:
                continue
            dates.add((int(year_raw) if year_raw else None, month, int(day_raw)))
    # 同一文本内同一 (月, 日) 可能同时命中带年与不带年两种模式：保留带年表示，
    # 否则年份门禁会被无年变体绕过（2025 vs 2026 误判为同日）。
    full_day_keys = {(m, d) for (y, m, d) in dates if y is not None}
    return {
        (y, m, d)
        for (y, m, d) in dates
        if y is not None or (m, d) not in full_day_keys
    }


def _strip_dates(text: str) -> str:
    for pattern in DATE_PATTERNS:
        text = pattern.sub(" ", text)
    return text


def _cross_language_overlap(claim: str, evidence: str) -> set[str]:
    """Glossary concepts present on both sides regardless of language.

    中文论断对照英文证据时词项重叠天然塌陷；本表把领域内同概念的中英表述
    归一为一个虚拟重叠项。两侧必须各自出现该概念（任一语言），避免单向命中。
    """
    claim_lower = f" {claim.lower()} "
    evidence_lower = f" {evidence.lower()} "
    shared_terms = _terms(claim) & _terms(evidence)
    hits: set[str] = set()
    for cn_keys, en_patterns in BILINGUAL_GLOSSARY:
        claim_hit = any(key in claim_lower for key in cn_keys) or any(
            re.search(rf"\b{re.escape(pattern)}\b", claim_lower) for pattern in en_patterns
        )
        evidence_hit = any(key in evidence_lower for key in cn_keys) or any(
            re.search(rf"\b{re.escape(pattern)}\b", evidence_lower) for pattern in en_patterns
        )
        if claim_hit and evidence_hit:
            # A concept already represented by shared literal terms must not
            # earn another point merely by receiving an xl: prefix.
            if any(_terms(alias) & shared_terms for alias in (*cn_keys, *en_patterns)):
                continue
            hits.add(f"xl:{cn_keys[0]}")
    return hits


def _support_score(
    claim: str,
    evidence: str,
    claim_terms: set[str],
    evidence_terms: set[str],
) -> float:
    """Strict reproducible support rule, not an NLI claim.

    A fact claim must match its measurable concept, direction/negation and
    explicit numbers. Generic product-name overlap alone is never sufficient.
    """
    base_overlap = claim_terms & evidence_terms
    xl = _cross_language_overlap(claim, evidence)
    overlap = base_overlap | xl
    lexical = min(1.0, len(overlap) / max(1, len(claim_terms)))
    claim_concepts = _concepts(claim)
    evidence_concepts = _concepts(evidence)
    claim_entities = _entities(claim)
    evidence_entities = _entities(evidence)
    if claim_entities and not claim_entities <= evidence_entities:
        return 0.0
    # 反向：证据涉及特定品种而论断未提及任何已知品种 → 论断过于笼统，拒绝。
    if evidence_entities and not (claim_entities & evidence_entities):
        return 0.0
    factual = claim_concepts - {"increase", "decrease"}
    if factual and not factual <= evidence_concepts:
        return 0.0
    if any(left in claim_concepts and right in evidence_concepts for left, right in OPPOSITES):
        return 0.0
    directional = claim_concepts & {"increase", "decrease"}
    if directional and not directional <= evidence_concepts:
        return 0.0
    # 日期以归一化 (年?, 月, 日) 比较：2026-09-11 与 9月11日 视为同日；
    # 剩余数字（产量、百分比等）仍须被证据覆盖。
    claim_dates = _dates_in(claim)
    evidence_dates = _dates_in(evidence)
    claim_numbers = _canonical_numbers(_strip_dates(claim))
    evidence_numbers = _canonical_numbers(_strip_dates(evidence))
    evidence_years = {str(year) for year, _, _ in _dates_in(evidence) if year}
    claim_numbers -= {number for number in claim_numbers if number in evidence_years}
    if claim_numbers and not claim_numbers <= evidence_numbers:
        return 0.0
    for claim_year, claim_month, claim_day in claim_dates:
        if not any(
            (evidence_year is None or claim_year is None or evidence_year == claim_year)
            and evidence_month == claim_month
            and evidence_day == claim_day
            for evidence_year, evidence_month, evidence_day in evidence_dates
        ):
            return 0.0
    claim_negated = bool(re.search(r"未|没有|并非|不(?:是|会|能)|\bno\b|\bnot\b", claim, re.I))
    evidence_negated = bool(re.search(r"未|没有|并非|不(?:是|会|能)|\bno\b|\bnot\b", evidence, re.I))
    if claim_negated != evidence_negated and (factual or directional):
        return 0.0
    # Require more than a single entity/commodity token for factual support.
    if len(overlap) < 2:
        return 0.0
    return lexical


def _concepts(text: str) -> set[str]:
    lowered = text.lower()
    return {name for name, aliases in CONCEPT_GROUPS.items() if any(alias.lower() in lowered for alias in aliases)}


def _entities(text: str) -> set[str]:
    lowered = text.lower()
    return {entity for entity, aliases in ENTITY_ALIASES.items() if any(alias.lower() in lowered for alias in aliases)}


def _ratio(items: list[dict[str, object]], field: str) -> float:
    return round(sum(bool(item[field]) for item in items) / len(items), 4) if items else 0.0

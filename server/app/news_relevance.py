"""Shared chemical entity disambiguation; never infer a product from an acronym alone."""

from __future__ import annotations

import re
from urllib.parse import urlparse

AMBIGUOUS = {"px", "pta", "meg", "poy", "dty"}
CONTEXT = re.compile(
    r"\b(?:polyester|petrochemical|chemical|terephthalic|glycol|paraxylene|xylene|"
    r"filament|textile|refinery|feedstock|tonnes?|tons?|capacity|plant|futures|prices?|supply)\b"
    r"|聚酯|化工|石化|涤纶|纺织|装置|产能|开工|检修|库存|加工|吨|乙二醇|对二甲苯|苯二甲酸",
    re.I,
)
UNRELATED = re.compile(
    r"\b(?:pistol|handgun|firearm|school|parent.teacher|tennis|quarterfinals?|"
    r"phinma|aludo|pokemon|player of the year)\b|家长教师|小学|手枪|网球",
    re.I,
)
EXPLICIT_INDUSTRIAL = re.compile(
    r"\b(?:crude oil|refinery|petrochemical|polyester|paraxylene|terephthalic acid|ethylene glycol)\b"
    r"|原油|炼油|石化|聚酯|对二甲苯|苯二甲酸|乙二醇",
    re.I,
)


def product_term_matches(keyword: str, text: str) -> bool:
    if "\ufffd" in text:
        return False
    term = keyword.casefold()
    # These names appeared in actual discovery results. Industrial words such
    # as plant/capacity/price also describe companies and stocks, so they cannot
    # disambiguate their tickers. Explicit chemical names are matched separately.
    if term in {"px", "meg"} and re.search(
        rf"\b{term}\s+(?:energy|ltd|limited|inc|corporation|technical analysis)\b"
        rf"|\b(?:stock|ticker)\s+{term}\b", text, re.I,
    ):
        return False
    # PEG and DEG are different materials from the MEG target. Mask only the
    # derivative mention; a separate monomer mention in the sentence survives.
    if term in {"ethylene glycol", "乙二醇"}:
        text = re.sub(r"\bpoly\s*\(\s*ethylene\s+glycol\s*\)|\bpolyethylene\s+glycol\b|[聚二三四]乙二醇",
                      " ", text, flags=re.I)
    # Leading boundary stays strict (never inside a word/code); a trailing digit is
    # allowed because Chinese price titles put the date/spec right after the acronym
    # (e.g. 涤纶DTY9月12日, DTY150D/48F). Ambiguous acronyms remain UNRELATED/CONTEXT gated.
    found = bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z])", text.casefold()))
    if term not in AMBIGUOUS:
        return found if term.isascii() else term in text.casefold()
    return found and not UNRELATED.search(text) and bool(CONTEXT.search(text))


def error_page_title(title: str) -> bool:
    """Match page-level failures, not reporting about errors or outages."""
    value = re.sub(r"\s+", " ", title).strip().casefold()
    return bool(re.fullmatch(
        r"(?:eia\s*[-–—:]\s*)?(?:sorry!?\s*)?(?:unexpected error|"
        r"(?:404[: -]*)?page not found|(?:503[: -]*)?service unavailable|"
        r"access denied|页面不存在|页面未找到|系统维护中)[.!。\s]*", value
    ))


def unusable_title(title: str) -> bool:
    return (
        not title.strip()
        or error_page_title(title)
        or "\ufffd" in title
        or bool(UNRELATED.search(title) and not EXPLICIT_INDUSTRIAL.search(title))
    )


def publisher_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    # A syndication/discovery URL does not establish a publisher identity.
    if host in {"news.google.com", "news.yahoo.com", "finance.yahoo.com", "api.gdeltproject.org"}:
        return ""
    return host


def publisher_label(url: str, fallback: str = "公开来源") -> str:
    host = publisher_host(url)
    names = {"cnbc.com": "CNBC", "ndrc.gov.cn": "国家发展和改革委员会",
             "eia.gov": "美国能源信息署（EIA）", "tnc.com.cn": "全球纺织网",
             "texnet.com.cn": "纺织网", "100ppi.com": "生意社", "mpa.gov.sg": "新加坡海事及港务管理局"}
    for domain, label in names.items():
        if host == domain or host.endswith("." + domain):
            return label
    return host or fallback

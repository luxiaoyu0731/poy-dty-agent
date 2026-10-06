"""Conservative excerpts from known public publisher article layouts.

Collector identity is retained. Publisher tier is provenance, not claim truth.
Unknown layouts, feed snippets and access barriers remain metadata-only.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from .event_summary_quality import clean_event_source_text

POLICY_VERSION = "publisher-excerpts.v1"


def verified_publisher_excerpt(url: str, text: str, title: str) -> dict[str, str] | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    clean = clean_event_source_text(text)
    if any(term in clean.casefold() for term in (
        "access denied", "verify you are human", "subscribe to read", "enable javascript to continue",
        "请登录后", "验证码", "ignore previous instructions", "忽略系统指令",
    )):
        return None
    if host == "www.ndrc.gov.cn" and re.fullmatch(r"/xwdt/xwfb/\d{6}/t\d{8}_\d+\.html", parsed.path):
        header = re.search(r"发布时间[:：]\s*\d{4}/\d{2}/\d{2}\s+来源[:：].{1,60}?\[\s*打印\s*\]", clean)
        if not header or not title or title not in clean[:header.start()]:
            return None
        body = re.split(r"\s*(?:附：|附件：|排行榜)", clean[header.end():], maxsplit=1)[0].strip()
        if len(body) < 160 or body.count("。") < 3 or not body.endswith("。"):
            return None
        return {"excerpt": body[:1200], "publisher_id": "ndrc", "tier": "A", "basis": POLICY_VERSION}
    if host in ("www.cnbc.com", "cnbc.com") and re.fullmatch(r"/\d{4}/\d{2}/\d{2}/[\w-]+\.html", parsed.path):
        # A Google headline is not CNBC evidence. Require the resolved publisher
        # URL, repeated article heading, dated byline area, and original key points.
        heading = clean.rfind(title + " Published") if title else -1
        if heading < 0:
            return None
        article = clean[heading:]
        marker = re.search(r"Published .{10,100}?\d{4}.{0,250}? Key Points (.+?) In this article ", article)
        if not marker:
            return None
        excerpt = marker[1].strip()
        if len(excerpt) < 160 or len(excerpt) > 1800 or excerpt.count(".") < 2:
            return None
        return {"excerpt": excerpt, "publisher_id": "cnbc", "tier": "B", "basis": POLICY_VERSION}
    return None

"""Parse 纺织网 (texnet) daily price articles into market observations.

Two public article formats carry the same per-day market assessment:
- per-product dailies titled "X月X日涤纶POY为9342.5" (value in title/body);
- the daily summary table "YYYY年M月D日纺织大宗商品价格涨跌榜" whose rows look
  like "涤纶POY 纺织 7156.25 7162.50 +0.09% -7.86%" (today price column).

Both describe the same daily assessment; the recovery pipeline prefers the
per-product article and falls back to the summary table. Values are kept in
the source's own CNY/mt basis — never spliced with other bases.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

TEXNET_SOURCE_ID = "texnet_price_articles"
TEXNET_LIST_URL = "https://info.texnet.com.cn/list--20-{page}.html"
TEXNET_REFERER = "https://info.texnet.com.cn/list--20-.html"
TEXNET_USER_AGENT = "POY-DTY-Agent/1.0 personal-research"
TEXNET_SERIES_IDS = {
    "poy": "poy.texnet.daily_assessment.cny_mt",
    "dty": "dty.texnet.daily_assessment.cny_mt",
}
TEXNET_PRODUCT_LABELS = {"poy": "涤纶POY", "dty": "涤纶DTY"}

_DAILY_TITLE = re.compile(
    r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日(?P<prefix>[^，。；\s]{0,12}?)"
    r"涤纶(?P<product>POY|DTY)为(?P<value>[\d,]+(?:\.\d+)?)元?"
)
_ZDB_TITLE = re.compile(
    r"(?P<year>20\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日纺织大宗商品价格涨跌榜"
)
_ZDB_ROW = re.compile(
    r"涤纶(?P<product>POY|DTY)\s*纺织\s*(?P<prev>[\d,]+(?:\.\d+)?)\s*"
    r"(?P<cur>[\d,]+(?:\.\d+)?)\s*(?P<chg>[+-]?[\d.]+%)\s*(?P<yoy>[+-]?[\d.]+%)"
)
_BODY_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


@dataclass(frozen=True, slots=True)
class TexnetArticleRef:
    url: str
    title: str
    kind: str  # "daily" | "zdb"
    product: str | None  # poy/dty for dailies, None for zdb
    month: int
    day: int
    year_hint: int | None
    title_value: float | None


def parse_texnet_listing(html: str, *, year_hint: int | None = None) -> list[TexnetArticleRef]:
    """Extract price-article references from one channel listing page."""
    refs: list[TexnetArticleRef] = []
    seen: set[str] = set()
    for url, title in re.findall(r'href="(/detail-\d+\.html)"[^>]*title="([^"]{4,80})"', html):
        if url in seen:
            continue
        zdb = _ZDB_TITLE.search(title)
        if zdb:
            seen.add(url)
            refs.append(
                TexnetArticleRef(
                    url=f"https://info.texnet.com.cn{url}",
                    title=title,
                    kind="zdb",
                    product=None,
                    month=int(zdb.group("month")),
                    day=int(zdb.group("day")),
                    year_hint=int(zdb.group("year")),
                    title_value=None,
                )
            )
            continue
        daily = _DAILY_TITLE.search(title)
        if daily:
            seen.add(url)
            value = float(daily.group("value").replace(",", ""))
            refs.append(
                TexnetArticleRef(
                    url=f"https://info.texnet.com.cn{url}",
                    title=title,
                    kind="daily",
                    product=daily.group("product").lower(),
                    month=int(daily.group("month")),
                    day=int(daily.group("day")),
                    year_hint=year_hint,
                    title_value=value,
                )
            )
    return refs


def parse_texnet_zdb_article(html: str) -> dict[str, dict[str, Any]]:
    """Parse a 涨跌榜 article body into per-product daily assessments."""
    title_match = _ZDB_TITLE.search(html)
    if not title_match:
        return {}
    date = f"{title_match.group('year')}-{int(title_match.group('month')):02d}-{int(title_match.group('day')):02d}"
    # Cells carry nested link/font markup; normalize to plain rows before matching.
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    rows: dict[str, dict[str, Any]] = {}
    for match in _ZDB_ROW.finditer(text):
        product = match.group("product").lower()
        if product in rows:
            continue
        rows[product] = {
            "observed_at": date,
            "value": float(match.group("cur").replace(",", "")),
            "prev_value": float(match.group("prev").replace(",", "")),
            "change_text": match.group("chg"),
            "yoy_text": match.group("yoy"),
        }
    return rows


def resolve_article_year(html: str, *, year_hint: int | None) -> str | None:
    """Find the article's publication year from its body, else the page hint."""
    body_dates = _BODY_DATE.findall(html)
    if body_dates:
        years = sorted({year for year, _, _ in body_dates})
        return years[-1]
    return str(year_hint) if year_hint else None


def texnet_observation_payload(
    *,
    product: str,
    observed_at: str,
    value: float,
    evidence_url: str,
    captured_at: str,
    raw_sha256: str,
    notes_extra: str = "",
) -> dict[str, Any]:
    label = TEXNET_PRODUCT_LABELS[product]
    return {
        "source_id": TEXNET_SOURCE_ID,
        "observed_at": observed_at,
        "indicator": f"{label} 日度市场评估",
        "product": product,
        "value": value,
        "unit": "CNY/mt",
        "frequency": "published_day",
        "region": "China textile public assessment",
        "evidence_url": evidence_url,
        "notes": (
            f"纺织网公开日度行情；{label}市场评估价；非逐笔成交价；按页面原始日期和单位保存。{notes_extra}"
        ).strip(),
        "raw": {
            "captured_at": captured_at,
            "label": label,
            "page_url": evidence_url,
            "raw_sha256": raw_sha256,
            "visibility_rule": (
                "first successful system capture; historical page is not treated as live trade data"
            ),
        },
    }


def texnet_capture_payload(
    *,
    product: str,
    observed_at: str,
    value: float,
    evidence_url: str,
    captured_at: str,
    raw_sha256: str,
    contract_version: str,
    parser_version: str,
) -> dict[str, Any]:
    return {
        "source_id": TEXNET_SOURCE_ID,
        "semantic_series_id": TEXNET_SERIES_IDS[product],
        "observed_at": observed_at,
        "published_at": captured_at,
        "visible_at": captured_at,
        "captured_at": captured_at,
        "source_url": evidence_url,
        "raw_sha256": raw_sha256,
        "authorization_scope": "public_page",
        "contract_version": contract_version,
        "parser_version": parser_version,
        "canonical_payload": {
            "product": product.upper(),
            "value": value,
            "unit": "CNY/mt",
            "observed_at": observed_at,
            "source_id": TEXNET_SOURCE_ID,
            "source_url": evidence_url,
            "settle": None,
        },
    }


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

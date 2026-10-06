"""Conservative body selection and defects, independent of publisher/model truth.

No network, decoding of access-protected content, model calls or persistence.
Acquired bodies have no character cap. Model requests also preserve complete
source text; provider capacity errors must not trigger silent truncation.
"""
from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlparse

BODY_POLICY = "article-body.v3-full-input"


def ppi_chinese_article(text: str, *, url: str, title: str) -> dict[str, str] | None:
    """Bind the Chinese publisher's headline, URL date and explicit body end."""
    parsed = urlparse(url)
    path = re.fullmatch(r"/news/detail-(\d{8})-\d+\.html", parsed.path)
    if parsed.hostname not in {"www.100ppi.com", "100ppi.com"} or not path:
        return None
    header = re.search(
        r"本社首页\s*>\s*商品动态\s*>\s*正文\s+(.{5,180}?)\s+"
        r"https?://www\.100ppi\.com\s+(\d{4}年\d{2}月\d{2}日)\s+\d{2}:\d{2}\s+([^\s<>]{1,80})\s+",
        text,
    )
    if not header:
        return None
    headline = header[1].strip()
    supplied = re.sub(r"\s*-\s*商品动态\s*-\s*生意社$", "", title).strip()
    # The navigation H1 may precede the actual heading. Its legacy stored title
    # needs a second, matching document title before recovery is permissible.
    if supplied != headline and (
        supplied != "大宗商品涨跌榜-生意社" or not text.startswith(headline + " - 商品动态 - 生意社")
    ):
        return None
    try:
        published = datetime.strptime(header[2], "%Y年%m月%d日")
    except ValueError:
        return None
    if published.strftime("%Y%m%d") != path[1]:
        return None
    tail = text[header.end():]
    end = re.search(r"[（(]文章来源[:：]([^()（）]{1,80})[）)]", tail)
    if not end or end[1].strip() != header[3].strip():
        return None
    body = tail[:end.start()].strip()
    # The publisher appends this fixed pricing advertisement before its source
    # footer. Require its complete signature; do not feed its numbers/formula
    # to the model as part of the reported event.
    advert = body.rfind("【大宗商品公式定价原理】")
    if advert >= 0 and all(marker in body[advert:] for marker in (
        "生意社基准价是基于价格大数据", "定价公式：结算价", "物流成本、品牌价差、区域价差",
    )):
        body = body[:advert].rstrip()
    if len(body) < 80 or not re.search(r"[。！？.!?][\"'”’)]?$", body):
        return None
    return {"title": headline, "body": body, "published_at": published.date().isoformat()}


def _sunsirs_header(text: str, url: str, title: str) -> re.Match | None:
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() not in {"sunsirs.com", "www.sunsirs.com"}:
        return None
    if not re.fullmatch(r"/(?:uk|en)/detail_news-\d+\.html", parsed.path):
        return None
    stamp = re.search(
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2}\s+\d{4}\s+\d{2}:\d{2}:\d{2}\s+SunSirs\s*\([^)]{1,80}\)", text,
    )
    if not stamp:
        return None
    # A headline in a related-story list cannot bind a different article's body.
    prefix = text[:stamp.start()].rstrip()
    title = re.sub(r"^SunSirs:\s*|\s*[-–|]\s*SunSirs$", "", title.strip(), flags=re.IGNORECASE)
    if not title or not prefix.casefold().endswith(title.casefold()):
        return None
    return stamp


def sunsirs_publication_date(text: str, *, url: str, title: str) -> str:
    stamp = _sunsirs_header(text, url, title)
    if not stamp:
        return ""
    try:
        # Publisher supplies no timezone here; retain the date, not an invented instant.
        return datetime.strptime(stamp[0].split(" SunSirs")[0], "%B %d %Y %H:%M:%S").date().isoformat()
    except ValueError:
        return ""


def sunsirs_body(text: str, *, url: str, title: str) -> tuple[str, str] | None:
    """Return a title-bound, dated body only when both publisher boundaries exist."""
    stamp = _sunsirs_header(text, url, title)
    if not stamp:
        return None
    tail = text[stamp.end():].strip()
    footer = re.search(r"If you have any inquiries or purchasing needs,|Related Information", tail)
    if not footer:
        return None  # a stored prefix is not a complete article
    body = tail[:footer.start()].strip()
    if len(body) < 160 or not re.search(r"[.!?。！？][\"'”’)]?$", body):
        return None
    return body, "publisher_sunsirs"


def body_defect(text: str, *, method: str = "", truncated: bool = False) -> str:
    """Known defects only; passing this check does not prove factual accuracy."""
    if all(marker in text for marker in ("文章关键词", "打印文章", "相关报道")):
        return "navigation_contaminated_body"
    if truncated:
        return "article_body_truncated"
    if not method and len(text) in {5000, 8000}:
        return "legacy_body_boundary_unverified"
    # ROT47-like kAm markers are observed in unreadable publisher pages. Do not
    # decode them; the page may be deliberately restricting access.
    if text.count("kAm") >= 3 or text.count("\ufffd") > max(8, len(text) // 100):
        return "unreadable_article_body"
    markers = (
        "Related Information", "Commodity News", "Commodity Price", "Sign In", "My Portfolio",
        "Stock Screener", "Market Screener", "Privacy Policy", "Terms of Use", "All Rights Reserved",
        "首页", "网站导航", "网站地图", "联系我们", "免责声明", "相关资讯", "热门排行",
    )
    count = sum(marker.casefold() in text.casefold() for marker in markers)
    if count >= 4:
        return "navigation_contaminated_body"
    return ""


def titles_conflict(discovered: str, headline: str) -> bool:
    """Reject clearly unrelated headings; tolerate punctuation and site suffixes."""
    stop = {"the", "a", "an", "of", "on", "in", "and", "for", "to", "is", "with", "sunsirs", "news"}

    def tokens(value: str) -> set[str]:
        words = {w for w in re.findall(r"[a-z0-9]+", value.casefold()) if w not in stop}
        for part in re.findall(r"[\u4e00-\u9fff]+", value):
            words.update(part[i:i + 2] for i in range(len(part) - 1))
        return words

    left, right = tokens(discovered), tokens(headline)
    return min(len(left), len(right)) >= 3 and len(left & right) / min(len(left), len(right)) < 0.2


def texnet_body(text: str, *, url: str, title: str) -> tuple[str, str] | None:
    """Known complete publisher brief with headline, dated header and footer."""
    parsed = urlparse(url)
    if parsed.hostname not in {"info.texnet.com.cn", "www.texnet.com.cn"}:
        return None
    if not re.fullmatch(r"/detail-\d+\.html", parsed.path):
        return None
    title = re.split(r"\s+-\s+纺织资讯", title)[0].strip()
    header = re.search(r"https?://www\.texnet\.com\.cn/\s+\d{4}-\d{2}-\d{2}\s+"
                       r"\d{2}:\d{2}:\d{2}\s+来源[:：]\s*\S+\s+", text)
    if not header or not title or not text[:header.start()].rstrip().endswith(title):
        return None
    tail = text[header.end():]
    end = re.search(r"文章关键词[:：]", tail)
    if not end:
        return None
    body = tail[:end.start()].strip()
    if len(body) < 40:
        return None
    return body, "publisher_texnet"

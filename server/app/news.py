from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from html.parser import HTMLParser
from ipaddress import ip_address
from pathlib import Path
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4
from xml.etree import ElementTree

import httpx

from .article_body_quality import (
    BODY_POLICY,
    body_defect,
    ppi_chinese_article,
    sunsirs_body,
    sunsirs_publication_date,
    texnet_body,
    titles_conflict,
)
from .event_summary_quality import clean_event_source_text
from .models import EventSummaryQualityResult, NewsSource
from .news_relevance import error_page_title, product_term_matches, unusable_title
from .publication_time import normalize_feed_publication
from .settings import settings
from .storage import (
    create_news_fetch_run,
    enqueue_event_ai_summary,
    finish_news_fetch_run,
    list_retryable_event_ai_summaries,
    mark_event_ai_grounded_summary_result,
    mark_event_ai_summary_failed,
    mark_event_ai_summary_processing,
    promote_grounded_news_event,
    upsert_event_observation,  # noqa: F401 - compatibility hook for ingestion tests/extensions
    upsert_news_article,
    upsert_news_event_cluster,
)

EVENT_SUMMARY_PROMPT_VERSION = "event-grounded-v11-industry-context"
EVENT_SUMMARY_MAX_ATTEMPTS = 3
EVENT_SUMMARY_RUN_LIMIT = 6
FULL_TEXT_MIN_CHARS = 600
PARTIAL_TEXT_MIN_CHARS = 40
PUBLIC_NEWS_AUTH_TYPES = frozenset({"public", "public_personal_reuse"})

FETCH_ATTEMPTS = 2
MAX_FETCH_REDIRECTS = 5
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}
GDELT_MIN_REQUEST_INTERVAL_SECONDS = 6.5
GDELT_MAX_BACKOFF_SECONDS = 30.0
OPEC_HOME_URL = "https://www.opec.org/"
EIA_NEWS_FEEDS = {
    "eia_press": "https://www.eia.gov/rss/press_rss.xml",
    "eia_today_in_energy": "https://www.eia.gov/rss/todayinenergy.xml",
}
# oilprice.com robots.txt (checked 2026-09-21) disallows these exact article
# paths for every agent; the fetcher must never request them.
OILPRICE_ROBOTS_DISALLOWED_PATHS = frozenset(
    {
        "/Energy/Energy-General/The-Gas-Find-That-Could-Transform-Europes-Energy-Future.html",
        "/Energy/Energy-General/This-Could-Be-A-Gamechanger-For-Natural-Gas-In-Europe.html",
        "/Energy/Energy-General/How-To-Profit-From-Europes-800-Billion-Energy-Crisis.html",
        "/Energy/Energy-General/The-No1-Energy-Stock-for-2024.html",
        "/Energy/Energy-General/Forgotten-Gas-Reserves-Could-Be-A-Gamechanger-For-European-Energy.html",
        "/Energy/Energy-General/Meet-The-Man-Using-AI-To-Revive-An-Oil-Gas-Play-Supermajors-Left-For-Dead.html",
        "/Energy/Natural-Gas/How-Artificial-Intelligence-Could-Trigger-a-Natural-Gas-Boom-in-Europe.html",
    }
)
OPEC_DISCOVERY_RSS_URL = (
    "https://news.google.com/rss/search?q=site%3Aopec.org%2Fpr-detail%20OPEC%20when%3A30d&hl=en-US&gl=US&ceid=US%3Aen"
)
GOOGLE_NEWS_DECODE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je"
GOOGLE_NEWS_DECODE_ATTEMPTS = 3
GOOGLE_NEWS_RESOLUTION_COOLDOWN_SECONDS = 1800
_google_news_resolution_retry_at = 0.0


class GoogleNewsResolutionDeferred(RuntimeError):
    """A discovery provider challenged/rate-limited us; do not retry the batch."""

MONTH_PATTERN = (
    "January|February|March|April|May|June|July|August|September|October|November|December|"
    "Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
MONTH_NUMBERS = {
    "january": 1,
    "jan": 1,
    "february": 2,
    "feb": 2,
    "march": 3,
    "mar": 3,
    "april": 4,
    "apr": 4,
    "may": 5,
    "june": 6,
    "jun": 6,
    "july": 7,
    "jul": 7,
    "august": 8,
    "aug": 8,
    "september": 9,
    "sep": 9,
    "sept": 9,
    "october": 10,
    "oct": 10,
    "november": 11,
    "nov": 11,
    "december": 12,
    "dec": 12,
}


@dataclass(frozen=True)
class RawNewsItem:
    source_id: str
    tier: str
    url: str
    title: str
    published_at: str = ""
    first_seen_at: str = ""
    raw_text: str = ""
    language: str = "unknown"
    discovery_url: str = ""
    detail_reason: str = ""
    discovery_timestamp: str = ""
    body_method: str = ""
    body_truncated: bool = False
    body_reason: str = ""
    body_document_url: str = ""
    body_document_sha256: str = ""


NEWS_SOURCES = [
    NewsSource(
        source_id="ccfa_industry_news",
        source_name="中国化学纤维工业协会",
        tier="B",
        url="https://www.ccfa.com.cn/",
        category="polyester_chain",
        fetcher="html",
        cadence="daily",
    ),
    NewsSource(
        source_id="texnet_polyester_news",
        source_name="中国纺织网聚酯长丝行情资讯",
        tier="B",
        url="https://info.texnet.com.cn/list--20-.html",
        category="polyester_chain",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="ppi_commodity_news",
        source_name="生意社大宗商品动态与要闻",
        tier="B",
        url="https://www.100ppi.com/",
        category="polyester_chain",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="opec_press",
        source_name="OPEC Press Releases",
        tier="A",
        url="https://www.opec.org/press-releases.html",
        category="oil_policy",
        fetcher="html",
        cadence="15-30min",
    ),
    NewsSource(
        source_id="eia_press",
        source_name="EIA Press Room",
        tier="A",
        url="https://www.eia.gov/pressroom/",
        category="oil_policy",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="eia_wpsr",
        source_name="EIA Weekly Petroleum Status Report",
        tier="A",
        url="https://www.eia.gov/petroleum/supply/weekly/",
        category="oil_policy",
        fetcher="html",
        cadence="weekly",
    ),
    NewsSource(
        source_id="ofac_recent_actions",
        source_name="OFAC Recent Actions",
        tier="A",
        url="https://ofac.treasury.gov/recent-actions",
        category="sanctions_geopolitics",
        fetcher="html",
        cadence="15-30min",
    ),
    NewsSource(
        source_id="treasury_press",
        source_name="U.S. Treasury Press Releases",
        tier="A",
        url="https://home.treasury.gov/news/press-releases",
        category="sanctions_geopolitics",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="state_department_releases",
        source_name="U.S. State Department Releases",
        tier="A",
        url="https://www.state.gov/press-releases",
        category="sanctions_geopolitics",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="eu_council_press",
        source_name="EU Council Press Releases",
        tier="A",
        url="https://www.consilium.europa.eu/en/rss/pressreleases.ashx",
        category="sanctions_geopolitics",
        fetcher="rss",
        cadence="30min",
    ),
    NewsSource(
        source_id="ndrc_news",
        source_name="NDRC News",
        tier="A",
        url="https://www.ndrc.gov.cn/xwdt/",
        category="china_policy",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="nea_news",
        source_name="National Energy Administration",
        tier="A",
        url="https://www.nea.gov.cn/",
        category="china_policy",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="ukmto_incidents",
        source_name="UKMTO Recent Incidents",
        tier="B",
        url="https://www.ukmto.org/recent-incidents",
        category="shipping_security",
        fetcher="html",
        cadence="15-60min",
    ),
    NewsSource(
        source_id="marad_advisories",
        source_name="MARAD Advisories",
        tier="B",
        url="https://www.maritime.dot.gov/msci-advisories",
        category="shipping_security",
        fetcher="html",
        cadence="15-60min",
    ),
    NewsSource(
        source_id="white_house_news",
        source_name="White House News",
        tier="A",
        url="https://www.whitehouse.gov/news/",
        category="sanctions_geopolitics",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="white_house_statements",
        source_name="White House Briefings and Statements",
        tier="A",
        url="https://www.whitehouse.gov/briefings-statements/",
        category="sanctions_geopolitics",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="un_press_releases",
        source_name="UN Press Releases",
        tier="A",
        url="https://press.un.org/en/content/press-release",
        category="sanctions_geopolitics",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="un_security_council_press",
        source_name="UN Security Council Press Releases",
        tier="A",
        url="https://press.un.org/en/security-council",
        category="sanctions_geopolitics",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="iea_news",
        source_name="IEA News",
        tier="A",
        url="https://www.iea.org/news",
        category="oil_policy",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="federal_reserve_press",
        source_name="Federal Reserve Press Releases",
        tier="A",
        url="https://www.federalreserve.gov/newsevents/pressreleases.htm",
        category="macro_finance",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="cftc_press",
        source_name="CFTC Press Releases",
        tier="A",
        url="https://www.cftc.gov/PressRoom/PressReleases",
        category="macro_finance",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="imo_press_briefings",
        source_name="IMO Press Briefings",
        tier="B",
        url="https://www.imo.org/en/MediaCentre/PressBriefings/Pages/default.aspx",
        category="shipping_security",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="mpa_press_releases",
        source_name="Maritime and Port Authority of Singapore Press Releases",
        tier="B",
        url="https://www.mpa.gov.sg/feeds/media-releases",
        category="shipping_security",
        fetcher="rss",
        cadence="30min",
    ),
    NewsSource(
        source_id="google_news_oil_rss",
        source_name="Google News Oil and Geopolitics RSS",
        tier="C",
        url="https://news.google.com/rss/search?q=OPEC%20OR%20crude%20oil%20OR%20Hormuz%20OR%20OFAC%20when:1d&hl=en-US&gl=US&ceid=US:en",
        category="sanctions_geopolitics",
        fetcher="rss",
        cadence="15min",
    ),
    NewsSource(
        source_id="google_news_chemical_rss",
        source_name="Google News Polyester Chain RSS",
        tier="C",
        url=(
            "https://news.google.com/rss/search?q=%22monoethylene%20glycol%22%20OR%20"
            "%22ethylene%20glycol%22%20OR%20%22purified%20terephthalic%20acid%22%20OR%20"
            "paraxylene%20OR%20%22polyester%20filament%22%20OR%20%22polyester%20POY%22%20OR%20"
            "%22polyester%20DTY%22%20OR%20%22polyester%20yarn%22%20"
            "when:1d&hl=en-US&gl=US&ceid=US:en"
        ),
        category="company_capacity",
        fetcher="rss",
        cadence="15min",
    ),
    NewsSource(
        source_id="gdelt_oil_geopolitics_rss",
        source_name="GDELT Oil and Geopolitics Article RSS",
        tier="C",
        url="https://api.gdeltproject.org/api/v2/doc/doc?query=%28OPEC%20OR%20%22crude%20oil%22%20OR%20Hormuz%20OR%20OFAC%29&mode=artlist&format=rss&maxrecords=25&sort=hybridrel&timespan=3days",
        category="sanctions_geopolitics",
        fetcher="rss",
        cadence="15min",
    ),
    NewsSource(
        source_id="sse_announcements",
        source_name="Shanghai Stock Exchange Listed Announcements",
        tier="B",
        url="https://www.sse.com.cn/disclosure/listedinfo/announcement/",
        category="company_capacity",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="szse_announcements",
        source_name="Shenzhen Stock Exchange Listed Announcements",
        tier="B",
        url="https://www.szse.cn/disclosure/listed/notice/index.html",
        category="company_capacity",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="hkex_announcements",
        source_name="HKEX Listed Company Announcements",
        tier="B",
        url="https://www.hkexnews.hk/index.htm",
        category="company_capacity",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="cninfo_announcements",
        source_name="CNINFO Listed Company Announcements",
        tier="B",
        url="http://www.cninfo.com.cn/new/index",
        category="company_capacity",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="us_centcom_press",
        source_name="U.S. Central Command Public Affairs RSS",
        tier="A",
        url="https://www.dvidshub.net/rss/unit/72",
        category="sanctions_geopolitics",
        fetcher="rss",
        cadence="30min",
    ),
    NewsSource(
        source_id="us_dod_releases",
        source_name="U.S. Department of Defense / War News RSS",
        tier="A",
        url="https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=945&max=20",
        category="sanctions_geopolitics",
        fetcher="rss",
        cadence="30min",
    ),
    NewsSource(
        source_id="nato_press_releases",
        source_name="NATO Press Releases",
        tier="A",
        url="https://www.nato.int/cps/en/natohq/press_releases.htm",
        category="sanctions_geopolitics",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="european_commission_press",
        source_name="European Commission Press Corner",
        tier="A",
        url="https://ec.europa.eu/commission/presscorner/api/rss?language=en",
        category="sanctions_geopolitics",
        fetcher="rss",
        cadence="30min",
    ),
    NewsSource(
        source_id="uk_government_news",
        source_name="UK Government News and Communications",
        tier="A",
        url="https://www.gov.uk/search/news-and-communications",
        category="sanctions_geopolitics",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="uk_fcdo_news",
        source_name="UK FCDO News and Communications",
        tier="A",
        url="https://www.gov.uk/search/news-and-communications?organisations%5B%5D=foreign-commonwealth-development-office",
        category="sanctions_geopolitics",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="us_coast_guard_news",
        source_name="U.S. Coast Guard News",
        tier="B",
        url="https://www.news.uscg.mil/News-by-Region/Headquarters/",
        category="shipping_security",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="saudi_aramco_news",
        source_name="Saudi Aramco News",
        tier="B",
        url="https://www.aramco.com/en/news-media/news",
        category="oil_policy",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="adnoc_news",
        source_name="ADNOC News",
        tier="B",
        url="https://www.adnoc.ae/en/news-and-media/press-releases",
        category="oil_policy",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="qatarenergy_news",
        source_name="QatarEnergy News",
        tier="B",
        url="https://www.qatarenergy.qa/en/MediaCenter/Pages/News.aspx",
        category="oil_policy",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="sinopec_news",
        source_name="Sinopec News",
        tier="B",
        url="https://www.sinopec.com/listco/en/news/index.shtml",
        category="company_capacity",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="cnpc_news",
        source_name="CNPC News",
        tier="B",
        url="https://news.cnpc.com.cn/cnpcnews/",
        category="company_capacity",
        fetcher="html",
        cadence="30min",
    ),
    NewsSource(
        source_id="eia_today_in_energy",
        source_name="EIA Today in Energy",
        tier="A",
        url="https://www.eia.gov/todayinenergy/",
        category="oil_policy",
        fetcher="html",
        cadence="daily",
    ),
    NewsSource(
        source_id="oilprice_world_news",
        source_name="OilPrice World Energy News",
        tier="B",
        url="https://oilprice.com/Latest-Energy-News/World-News/",
        category="oil_policy",
        fetcher="html",
        cadence="30min",
    ),
]

KEYWORDS: dict[str, tuple[str, ...]] = {
    "oil_policy": (
        "opec",
        "opec+",
        "production",
        "output",
        "quota",
        "cut",
        "market stability",
        "ministerial meeting",
        "non-opec",
        "brent",
        "crude",
        "inventory",
        "lng",
        "natural gas",
        "middle east",
        "petroleum",
        "refinery",
        "refineries",
        "steo",
        "wpsr",
        "diesel",
        "gasoline",
        "fuel exports",
        "fuel export",
        "原油",
        "石油",
        "成品油",
        "炼厂",
        "库存",
        "石脑油",
    ),
    "sanctions_geopolitics": (
        "sanction",
        "sanctions",
        "ofac",
        "sdn",
        "iran",
        "russia",
        "shadow",
        "lpg",
        "tanker",
        "ceasefire",
        "truce",
        "de-escalation",
        "peace",
        "deal",
        "talks",
        "停火",
        "和谈",
        "缓和",
        "制裁",
    ),
    "shipping_security": (
        "hormuz",
        "red sea",
        "gulf of oman",
        "vessel",
        "shipping",
        "maritime",
        "tanker",
        "attack",
        "sunk",
        "collision",
        "grounding",
        "terminal",
        "port closure",
        "reopen",
        "resume transit",
        "shipping resumes",
        "复航",
        "通航恢复",
    ),
    "china_policy": (
        "油气",
        "成品油",
        "原油",
        "石油",
        "炼油",
        "lng",
        "沙特阿美",
        "中东",
        "进口来源",
        "能源安全",
        "化工",
        "化纤",
        "ndrc",
    ),
    "company_capacity": (
        "px",
        "pta",
        "meg",
        "polyester",
        "plant",
        "capacity",
        "outage",
        "maintenance",
        "涤纶",
        "聚酯",
        "长丝",
        "加弹",
        "乙二醇",
        "对二甲苯",
        "精对苯二甲酸",
    ),
    "macro_finance": ("dollar", "treasury yield", "10-year treasury", "rate", "inflation", "fed"),
}

PRODUCT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "crude_oil": ("crude", "brent", "wti", "oil", "petroleum", "原油", "石油", "成品油"),
    "naphtha": ("naphtha", "石脑油"),
    "PX": ("px", "paraxylene", "对二甲苯"),
    "PTA": ("pta", "精对苯二甲酸"),
    "MEG": ("meg", "ethylene glycol", "乙二醇"),
    "POY": ("poy", "涤纶长丝"),
    "DTY": ("dty", "加弹"),
    "LPG": ("lpg", "liquefied petroleum gas"),
}

DISCOVERY_SOURCE_PREFIXES = ("google_news_", "gdelt_")
DISCOVERY_HOSTS = {"news.google.com", "api.gdeltproject.org"}
# Curated free publishers that discovery wrappers may be resolved to and their
# articles fetched from. Each host must also stay in OUTBOUND_FETCH_HOSTS; the
# pair of gates keeps discovery from turning into an arbitrary outbound fetcher.
# Paywalled or bot-blocked outlets (WSJ, NYT, Reuters, ogj.com) are deliberately
# absent. visualcapitalist/eastdaley verified fetchable from the HK host 2026-09-24.
DISCOVERY_PUBLISHER_HOSTS = frozenset(
    {"finance.yahoo.com", "www.cnbc.com", "oilprice.com", "www.visualcapitalist.com", "www.eastdaley.com",
     "www.csis.org", "www.cfr.org", "maritime-executive.com",
     "www.aljazeera.com", "www.theguardian.com", "www.arabnews.com"}
)
# Some public listing pages only serve content with an explicit same-site Referer.
SOURCE_FETCH_REFERERS = {
    "ppi_commodity_news": "https://www.100ppi.com/",
    "texnet_polyester_news": "https://info.texnet.com.cn/list--20-.html",
}
ACCESS_BARRIER_TERMS = (
    "subscribe to continue",
    "subscribe to read",
    "sign in to read",
    "log in to read",
    "login to read",
    "complete the captcha",
    "verify you are human",
    "enable javascript and cookies to continue",
    "登录后阅读全文",
    "登录后阅读",
    "订阅后阅读",
    "付费后阅读",
)
ACTOR_HINT_TERMS: dict[str, tuple[str, ...]] = {
    "OPEC/OPEC+": ("opec", "opec+", "jmmc", "ministerial meeting"),
    "EIA": ("eia", "energy information administration", "wpsr", "steo"),
    "IEA": ("iea", "international energy agency"),
    "OFAC/US Treasury": ("ofac", "treasury", "sdn", "sanction"),
    "US Federal Reserve": ("federal reserve", "fed", "fomc", "powell"),
    "China policy bodies": ("ndrc", "mofcom", "customs", "海关", "发改委", "商务部"),
    "Iran": ("iran", "伊朗"),
    "Russia": ("russia", "俄罗斯"),
    "Israel": ("israel", "以色列"),
    "Shipping/insurers": ("shipping", "tanker", "maritime", "insurance", "航运", "油轮", "保险"),
    "Polyester producers": ("polyester", "poy", "dty", "涤纶", "长丝", "加弹"),
}
GEOGRAPHY_TERMS: dict[str, tuple[str, ...]] = {
    "Middle East": ("middle east", "gulf", "iran", "israel", "中东", "海湾", "伊朗", "以色列"),
    "Russia/Black Sea": ("russia", "black sea", "俄罗斯", "黑海"),
    "Red Sea": ("red sea", "houthi", "红海", "胡塞"),
    "China": ("china", "中国", "人民币", "海关"),
    "United States": ("united states", "u.s.", "us ", "美国", "ofac", "treasury"),
    "Europe": ("europe", "eu", "european", "欧洲", "欧盟"),
    "Southeast Asia": ("vietnam", "indonesia", "thailand", "东南亚", "越南", "印尼", "泰国"),
}
ROUTE_TERMS: dict[str, tuple[str, ...]] = {
    "Strait of Hormuz": ("hormuz", "霍尔木兹"),
    "Red Sea/Suez": ("red sea", "suez", "红海", "苏伊士"),
    "Gulf of Oman": ("gulf of oman", "阿曼湾"),
    "Black Sea": ("black sea", "黑海"),
    "Tanker freight": ("tanker", "vessel", "shipping", "freight", "油轮", "航运", "运费"),
}
POLICY_TOOL_TERMS: dict[str, tuple[str, ...]] = {
    "sanction": ("sanction", "ofac", "sdn", "制裁"),
    "quota": ("quota", "output cut", "production cut", "配额", "减产"),
    "tariff": ("tariff", "anti-dumping", "关税", "反倾销"),
    "subsidy": ("subsidy", "补贴"),
    "rate": ("interest rate", "fed funds", "rate cut", "rate hike", "利率", "降息", "加息"),
    "reserve_release": ("spr", "strategic petroleum reserve", "reserve release", "战略储备"),
    "military_action": ("strike", "attack", "missile", "military", "袭击", "打击", "军事"),
    "shipping_advisory": ("shipping advisory", "maritime warning", "port closure", "航运警告", "港口关闭"),
    "data_release": ("inventory", "stocks", "cpi", "ppi", "report", "库存", "报告"),
}

BULLISH_TERMS = (
    "sanction",
    "ofac",
    "attack",
    "strike",
    "missile",
    "cut",
    "outage",
    "shutdown",
    "disruption",
    "closed",
    "closure",
    "blockade",
    "seize",
    "threat",
    "hormuz",
    "risk remains high",
    "制裁",
    "袭击",
    "打击",
    "冲突",
    "中断",
    "封锁",
    "关闭",
    "威胁",
)

BEARISH_TERMS = (
    "increase production",
    "production adjustment",
    "raise output",
    "supply resumes",
    "resume flows",
    "inventory build",
    "inventories increased",
    "demand downgrade",
    "ceasefire",
    "truce",
    "peace agreement",
    "de-escalation",
    "diplomatic talks",
    "deal reached",
    "reopen",
    "shipping resumes",
    "resume transit",
    "sanctions relief",
    "lift sanctions",
    "strike cancelled",
    "strike canceled",
    "pause strikes",
    "库存增加",
    "增产",
    "恢复供应",
    "需求下修",
    "停火",
    "和谈",
    "和平协议",
    "局势缓和",
    "复航",
    "恢复通航",
    "解除制裁",
    "取消打击",
)

GENERIC_LINK_TITLES = {
    "about us",
    "home",
    "news",
    "press releases",
    "recent actions",
    "u.s. department of the treasury",
    "u.s. department of state",
    "the white house",
    "federal reserve board",
    "about ofac",
    # Site-brand <title> values that must never override a specific RSS/feed
    # article title during detail enrichment.
    "organization of the petroleum exporting countries",
    "organization of the petroleum exporting countries (opec)",
    "opec",
}

BROAD_CONTEXT_KEYWORDS = {"peace", "deal", "talks", "ofac"}
RELEVANCE_CONTEXT_TERMS = (
    "iran",
    "russia",
    "sanction",
    "opec",
    "crude",
    "oil",
    "petroleum",
    "tanker",
    "hormuz",
    "red sea",
    "middle east",
    "lpg",
    "lng",
    "natural gas",
    "px",
    "pta",
    "meg",
    "poy",
    "dty",
    "refinery",
    "inventory",
)
SANCTIONS_DIRECT_CONTEXT_TERMS = (
    "iran",
    "hormuz",
    "red sea",
    "middle east",
)
SANCTIONS_ENERGY_CONTEXT_TERMS = (
    "vessel",
    "tanker",
    "shipping",
    "shadow fleet",
    "crude",
    "oil",
    "petroleum",
    "lpg",
    "lng",
    "energy",
)
CHEMICAL_CHAIN_CONTEXT_TERMS = (
    "monoethylene glycol",
    "ethylene glycol",
    "purified terephthalic acid",
    "terephthalic acid",
    "paraxylene",
    "polyester filament",
    "polyester yarn",
    "polyester fiber",
    "polyester fibre",
    "petrochemical",
    "feedstock",
    "pta futures",
    "meg prices",
    "meg market",
    "poy",
    "dty",
)
CHEMICAL_CHAIN_NOISE_TERMS = (
    "meg ryan",
    "meg stalter",
    "meg jones",
    "meg o'neill",
    "flag",
    "apron",
    "shirt",
    "backdrop",
    "banner",
    "lace",
    "fringe trim",
    "photo background",
    "nylon boat flag",
    "golf",
    "koepka",
    "golfwrx",
)

LOW_SIGNAL_NEWS_TITLES = {
    "all rights reserved 2020 qatar petroleum",
    "exploration and production",
    "lng: a cleaner source of energy",
    "lng a cleaner source of energy",
}

LOW_SIGNAL_NEWS_TITLE_PATTERNS = (
    r"\[\s*image\s+\d+\s+of\s+\d+\s*\]",
    r"\bimage\s+\d+\s+of\s+\d+\b",
)


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.title = ""
        self._current_href = ""
        self._current_text: list[str] = []
        self._in_title = False
        self._title_text: list[str] = []
        self._visible_text: list[str] = []
        self._skip = 0
        self._svg_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "svg":
            self._svg_depth += 1
        if self._svg_depth:
            return
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
            return
        if tag == "title":
            self._in_title = True
        if tag == "a":
            self._current_href = dict(attrs).get("href") or ""
            self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "svg" and self._svg_depth:
            self._svg_depth -= 1
            return
        if self._svg_depth:
            return
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
            return
        if tag == "title":
            self._in_title = False
            self.title = _clean_text(" ".join(self._title_text))
        if tag == "a" and self._current_href:
            text = _clean_text(" ".join(self._current_text))
            if text:
                self.links.append((self._current_href, text))
            self._current_href = ""
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._skip or self._svg_depth:
            return
        text = _clean_text(data)
        if not text:
            return
        self._visible_text.append(text)
        if self._in_title:
            self._title_text.append(text)
        if self._current_href:
            self._current_text.append(text)

    @property
    def visible_text(self) -> str:
        return _clean_text(" ".join(self._visible_text))


class _ArticleBodyParser(HTMLParser):
    """Extract semantic article content without counting site chrome as正文."""

    _BODY_HINTS = (
        "article-body",
        "article-content",
        "entry-content",
        "main-content",
        "news-body",
        "news-detail__content",
        "post-content",
        "story-body",
        "field--name-body",
        "wysiwyg",
    )
    _BLOCKED_TAGS = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form"}
    _CHROME_MARKER = re.compile(
        r"(?:^|[\s_-])(?:related|recommended|recommendations|sidebar|share|social|menu|navigation|advertisement)"
        r"(?:$|[\s_-])"
    )
    _VOID_TAGS = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }

    def __init__(self, *, source_url: str = "") -> None:
        super().__init__()
        self._eia_article = _valid_eia_article_url(source_url)
        parsed = urlparse(source_url)
        host = (parsed.hostname or "").lower()
        # Publisher-specific containers observed in complete public pages. Keep
        # these selectors off discovery feeds and unrelated website layouts.
        self._cnbc_article = host == "www.cnbc.com" and bool(
            re.fullmatch(r"/\d{4}/\d{2}/\d{2}/[\w-]+\.html", parsed.path))
        self._oilprice_article = host == "oilprice.com" and bool(
            re.fullmatch(r"/Energy/[\w-]+/[\w-]+(?:\.amp)?\.html", parsed.path))
        self._cnpc_article = host == "news.cnpc.com.cn" and bool(
            re.fullmatch(r"/system/\d{4}/\d{2}/\d{2}/\d+\.shtml", parsed.path))
        self._candidates: dict[str, list[list[str]]] = {
            "body_hint": [],
            "article": [],
            "main": [],
        }
        self._active: list[tuple[int, str, int]] = []
        self._blocked = 0
        self._headline_depth = 0
        self._headline_text: list[str] = []
        self._publication_depth = 0
        self._publication_text: list[str] = []
        self._elements: list[str] = []
        self._blocked_depths: set[int] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._VOID_TAGS:
            return
        self._elements.append(tag)
        attrs_dict = dict(attrs)
        marker = f"{attrs_dict.get('class') or ''} {attrs_dict.get('id') or ''}".casefold()
        tokens = marker.split()
        publisher_body = (
            (self._cnbc_article and "articlebody-articlebody" in tokens)
            or (self._oilprice_article and "article_content" in tokens)
            or (self._cnpc_article and "sj-main" in tokens)
        )
        publisher_chrome = self._cnbc_article and any(token.startswith((
            "relatedquotes-", "articlebody-googlepreferredsource", "articlebody-mobileadhesion",
            "inlineimage-", "createfreeaccountbutton-",
        )) for token in tokens)
        publisher_heading = (
            (self._cnbc_article and "articleheader-headline" in tokens)
            or (self._cnpc_article and tag == "h2")
        )
        eia_body = self._eia_article and "tie-article" in marker.split()
        eia_chrome = self._eia_article and "do-not-print" in marker.split()
        hidden = ("hidden" in attrs_dict or attrs_dict.get("aria-hidden") == "true"
                  or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attrs_dict.get("style") or ""))
        if tag in self._BLOCKED_TAGS or self._CHROME_MARKER.search(marker) or hidden or eia_chrome or publisher_chrome:
            self._blocked += 1
            self._blocked_depths.add(len(self._elements))
            return
        if self._blocked:
            return
        if eia_body:
            # EIA's section banner is also an h1. Bind the article's own h1
            # inside its verified container, never the earlier site heading.
            self._headline_text = []
            self._headline_depth = 0
        if self._eia_article and self._active and tag == "span" and "date" in marker.split():
            self._publication_depth = len(self._elements)
        if (not self._headline_depth and not self._headline_text
                and (str(attrs_dict.get("id") or "").casefold() == "articleheadline"
                     or tag == "h1" or publisher_heading)):
            self._headline_depth = len(self._elements)
        kind = ""
        if (eia_body or publisher_body or any(hint in marker for hint in self._BODY_HINTS)
                or attrs_dict.get("itemprop") == "articleBody"):
            kind = "body_hint"
        elif tag == "article":
            kind = "article"
        elif tag == "main":
            kind = "main"
        if kind:
            index = len(self._candidates[kind])
            self._candidates[kind].append([])
            self._active.append((len(self._elements), kind, index))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self._VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag not in self._elements:
            return
        # A nested div closes itself, not the article-content div around it.
        # Retire a candidate only when its actual enclosing element closes.
        depth = len(self._elements) - self._elements[::-1].index(tag)
        self._elements = self._elements[: depth - 1]
        self._blocked_depths = {d for d in self._blocked_depths if d < depth}
        self._blocked = len(self._blocked_depths)
        if self._headline_depth >= depth:
            self._headline_depth = 0
        if self._publication_depth >= depth:
            self._publication_depth = 0
        self._active = [candidate for candidate in self._active if candidate[0] < depth]

    def handle_data(self, data: str) -> None:
        if self._blocked:
            return
        text = _clean_text(data)
        if not text:
            return
        if self._headline_depth:
            self._headline_text.append(text)
        if self._publication_depth:
            self._publication_text.append(text)
        if not self._active:
            return
        for _, kind, index in self._active:
            self._candidates[kind][index].append(text)

    @property
    def text(self) -> str:
        for kind in ("body_hint", "article", "main"):
            values = [_clean_text(" ".join(parts)) for parts in self._candidates[kind] if parts]
            if values:
                return max(values, key=len)
        return ""

    @property
    def headline(self) -> str:
        return _clean_text(" ".join(self._headline_text))

    @property
    def published_at(self) -> str:
        value = _extract_date(" ".join(self._publication_text))
        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return ""

    @property
    def method(self) -> str:
        return next((f"semantic_{k}" for k in ("body_hint", "article", "main") if any(self._candidates[k])), "")


class _IeaNewsListingParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.items: list[RawNewsItem] = []
        self._in_article = 0
        self._href = ""
        self._title_parts: list[str] = []
        self._date_parts: list[str] = []
        self._collect_title = 0
        self._collect_date = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        classes = (attrs_dict.get("class") or "").split()
        if tag == "article":
            self._in_article += 1
            self._href = ""
            self._title_parts = []
            self._date_parts = []
        if not self._in_article:
            return
        if tag == "a" and "m-news-detailed-listing__link" in classes:
            self._href = attrs_dict.get("href") or ""
        if "m-news-detailed-listing__hover" in classes:
            self._collect_title += 1
        if "m-news-detailed-listing__date" in classes:
            self._collect_date += 1

    def handle_endtag(self, tag: str) -> None:
        if self._collect_title and tag in {"span", "h5"}:
            self._collect_title -= 1
        if self._collect_date and tag == "div":
            self._collect_date -= 1
        if tag == "article" and self._in_article:
            self._in_article -= 1
            self._append_current_item()

    def handle_data(self, data: str) -> None:
        if not self._in_article:
            return
        text = _clean_text(data)
        if not text:
            return
        if self._collect_title:
            self._title_parts.append(text)
        if self._collect_date:
            self._date_parts.append(text)

    def _append_current_item(self) -> None:
        title = _clean_text(" ".join(self._title_parts))
        url = urljoin(self.base_url, self._href)
        if not title or "/news/" not in urlparse(url).path:
            return
        published_at = _extract_date(" ".join(self._date_parts))
        self.items.append(
            RawNewsItem(
                source_id="iea_news",
                tier="A",
                url=url,
                title=title,
                published_at=published_at,
                raw_text=title,
            )
        )


class _EiaTodayListingParser(HTMLParser):
    """Read dated TIE article cards without treating their body links as headlines."""

    def __init__(self, *, source: NewsSource, base_url: str) -> None:
        super().__init__()
        self.source = source
        self.base_url = base_url
        self.items: list[RawNewsItem] = []
        self._depth = 0
        self._in_headline = False
        self._in_date = False
        self._href = ""
        self._title: list[str] = []
        self._date: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "div":
            if self._depth:
                self._depth += 1
            elif "tie-article" in classes:
                self._depth = 1
                self._href = ""
                self._title = []
                self._date = []
                self._in_headline = self._in_date = False
        if not self._depth:
            return
        if tag == "h1":
            self._in_headline = True
        if tag == "span" and "date" in classes:
            self._in_date = True
        if tag == "a" and self._in_headline:
            self._href = attributes.get("href") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1":
            self._in_headline = False
        if tag == "span":
            self._in_date = False
        if tag == "div" and self._depth:
            self._depth -= 1
            if not self._depth:
                title = _clean_text(" ".join(self._title))
                published_at = _extract_date(" ".join(self._date))
                url = urljoin(self.base_url, self._href)
                parsed = urlparse(url)
                if (
                    title
                    and published_at
                    and parsed.scheme in {"http", "https"}
                    and parsed.hostname == urlparse(self.source.url).hostname
                    and parsed.path == "/todayinenergy/detail.php"
                    and re.fullmatch(r"id=\d+", parsed.query)
                    and _is_relevant(title, "")
                ):
                    self.items.append(
                        RawNewsItem(
                            source_id=self.source.source_id, tier=self.source.tier,
                            url=url, title=title, published_at=published_at, raw_text=title,
                        )
                    )

    def handle_data(self, data: str) -> None:
        if self._depth and self._in_headline:
            self._title.append(data)
        if self._depth and self._in_date:
            self._date.append(data)


class _EiaDatedListingParser(HTMLParser):
    def __init__(self, *, source: NewsSource, base_url: str) -> None:
        super().__init__()
        self.source = source
        self.base_url = base_url
        self.items: list[RawNewsItem] = []
        self._item_depth = 0
        self._href = ""
        self._title_parts: list[str] = []
        self._date_parts: list[str] = []
        self._collect_link = 0
        self._collect_date = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag in {"span", "li"}:
            self._item_depth += 1
            if self._item_depth == 1:
                self._href = ""
                self._title_parts = []
                self._date_parts = []
        if not self._item_depth:
            return
        classes = set((attrs_dict.get("class") or "").split())
        if tag == "a":
            self._href = attrs_dict.get("href") or self._href
            self._collect_link += 1
        if "tagline" in classes or "date" in classes:
            self._collect_date += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._collect_link:
            self._collect_link -= 1
        if tag in {"p", "span"} and self._collect_date:
            self._collect_date -= 1
        if tag in {"span", "li"} and self._item_depth:
            self._item_depth -= 1
            if self._item_depth == 0:
                self._append_current_item()

    def handle_data(self, data: str) -> None:
        if not self._item_depth:
            return
        text = _clean_text(data)
        if not text:
            return
        if self._collect_link:
            self._title_parts.append(text)
        if self._collect_date:
            self._date_parts.append(text)

    def _append_current_item(self) -> None:
        title = _clean_text(" ".join(self._title_parts))
        url = urljoin(self.base_url, self._href)
        published_at = _extract_date(" ".join(self._date_parts))
        if not title or not published_at:
            return
        if not _is_allowed_article_link(self.source, url):
            return
        if _is_relevant(title, ""):
            self.items.append(
                RawNewsItem(
                    source_id=self.source.source_id,
                    tier=self.source.tier,
                    url=url,
                    title=title,
                    published_at=published_at,
                    raw_text=title,
                )
            )


def news_sources() -> list[NewsSource]:
    return NEWS_SOURCES


def get_news_source(source_id: str) -> NewsSource | None:
    return next((source for source in NEWS_SOURCES if source.source_id == source_id), None)


async def fetch_news_sources(
    source_id: str | None = None,
    *,
    limit_per_source: int = 20,
    mode: str = "live",
    start_date: str | None = None,
    end_date: str | None = None,
    cursor_pages: int = 3,
    include_details: bool = True,
    source_timeout_seconds: float | None = None,
) -> dict[str, object]:
    selected = [get_news_source(source_id)] if source_id else NEWS_SOURCES
    sources = [source for source in selected if source is not None]
    runs = []
    for source in sources:
        runs.append(
            await fetch_news_source(
                source,
                limit=limit_per_source,
                mode=mode,
                start_date=start_date,
                end_date=end_date,
                cursor_pages=cursor_pages,
                include_details=include_details,
                source_timeout_seconds=source_timeout_seconds,
                process_summaries=False,
            )
        )
    articles_found = sum(int(run["articles_found"]) for run in runs)
    summary_result = (
        await process_event_summary_queue(
            limit=min(EVENT_SUMMARY_RUN_LIMIT, articles_found),
            concurrency=2,
            request_interval_seconds=0.5,
            source_ids=[source.source_id for source in sources],
        )
        if articles_found
        else {"selected": 0, "completed": 0, "failed": 0}
    )
    return {
        "mode": mode,
        "start_date": start_date,
        "end_date": end_date,
        "cursor_pages": cursor_pages,
        "include_details": include_details,
        "runs": runs,
        "articles_found": articles_found,
        "clusters_upserted": sum(int(run["clusters_upserted"]) for run in runs),
        "events_created": sum(int(run["events_created"]) for run in runs),
        "summaries_selected": summary_result["selected"],
        "summaries_completed": summary_result["completed"],
        "summaries_failed": summary_result["failed"],
    }


async def fetch_news_source(
    source: NewsSource,
    *,
    limit: int = 20,
    mode: str = "live",
    start_date: str | None = None,
    end_date: str | None = None,
    cursor_pages: int = 3,
    include_details: bool = True,
    source_timeout_seconds: float | None = None,
    process_summaries: bool = True,
) -> dict[str, object]:
    run_id = str(uuid4())
    create_news_fetch_run(run_id=run_id, source_id=source.source_id)
    finished = False

    def finish_once(
        *,
        status: str,
        articles_found: int = 0,
        clusters_upserted: int = 0,
        events_created: int = 0,
        error: str = "",
    ) -> dict[str, object]:
        nonlocal finished
        if finished:
            raise RuntimeError(f"news fetch run already finished: {run_id}")
        finished = True
        return finish_news_fetch_run(
            run_id=run_id,
            status=status,
            articles_found=articles_found,
            clusters_upserted=clusters_upserted,
            events_created=events_created,
            error=error,
        )

    try:
        fetch = _fetch_news_source_result(
            source,
            limit=limit,
            mode=mode,
            start_date=start_date,
            end_date=end_date,
            cursor_pages=cursor_pages,
            include_details=include_details,
        )
        if source_timeout_seconds is None:
            result, errors = await fetch
        else:
            async with asyncio.timeout(source_timeout_seconds):
                result, errors = await fetch
        summary_result = (
            await process_event_summary_queue(limit=result["articles_found"], source_ids=[source.source_id])
            if process_summaries and result["articles_found"]
            else {"selected": 0, "completed": 0, "failed": 0}
        )
        result["summaries_completed"] = summary_result["completed"]
        result["summaries_failed"] = summary_result["failed"]
        finished_run = finish_once(
            status=(
                "partial_error"
                if errors and result["articles_found"]
                else ("ok" if result["articles_found"] else "no_relevant_items")
            ),
            articles_found=result["articles_found"],
            clusters_upserted=result["clusters_upserted"],
            events_created=result["events_created"],
            error="; ".join(errors[:3]),
        )
        return {
            **finished_run,
            "summaries_selected": summary_result["selected"],
            "summaries_completed": summary_result["completed"],
            "summaries_failed": summary_result["failed"],
        }
    except TimeoutError as exc:
        status = "timeout" if source_timeout_seconds is not None else "error"
        return finish_once(status=status, error=_error_label(exc))
    except asyncio.CancelledError as exc:
        finish_once(status="error", error=_error_label(exc))
        raise
    except Exception as exc:  # noqa: BLE001 - failed fetch is recorded as run state.
        return finish_once(status="error", error=_error_label(exc))


async def _fetch_news_source_result(
    source: NewsSource,
    *,
    limit: int,
    mode: str,
    start_date: str | None,
    end_date: str | None,
    cursor_pages: int,
    include_details: bool,
) -> tuple[dict[str, int], list[str]]:
    items: list[RawNewsItem] = []
    errors: list[str] = []
    opec_discovery_succeeded = False
    urls = _source_urls_for_mode(
        source,
        mode=mode,
        start_date=start_date,
        end_date=end_date,
        cursor_pages=cursor_pages,
    )
    for url in urls:
        try:
            text, content_type = await _fetch_text(url, referer=SOURCE_FETCH_REFERERS.get(source.source_id))
            is_feed = source.fetcher == "rss" or "xml" in content_type or "<rss" in text[:500].lower()
            parsed_items = (
                _parse_feed(text, source, base_url=url) if is_feed else _parse_html(text, source, base_url=url)
            )
            items.extend(parsed_items)
            if source.source_id == "eia_press" and url == EIA_NEWS_FEEDS.get(source.source_id) and parsed_items:
                # Official RSS carries stable article URLs and publication dates.
                # TIE also reads the dated HTML listing: its RSS can omit article IDs.
                break
            if source.source_id == "opec_press" and url == OPEC_DISCOVERY_RSS_URL:
                opec_discovery_succeeded = True
        except Exception as exc:  # noqa: BLE001 - source-level partial errors are reported in the run.
            errors.append(f"{_error_label(exc)} @ {url[:120]}")
    if source.source_id == "opec_press" and opec_discovery_succeeded:
        # The official listing currently rejects automated clients. A valid public
        # discovery feed is sufficient when every retained item resolves back to
        # an allowlisted OPEC press-release URL.
        errors = [error for error in errors if OPEC_DISCOVERY_RSS_URL in error]
    if not items and errors:
        raise RuntimeError("; ".join(errors[:3]))
    candidates = _filter_items_for_archive_window(
        _dedupe_items(items),
        mode=mode,
        start_date=start_date,
        end_date=end_date,
        require_published_at=not include_details,
    )[:limit]
    if source.source_id == "opec_press":
        unresolved_count = len(candidates)
        candidates, resolution_errors = await _resolve_opec_discovery_items(candidates)
        errors.extend(resolution_errors[:3])
        if unresolved_count and not candidates and resolution_errors:
            raise RuntimeError("; ".join(resolution_errors[:3]))
    if include_details:
        candidates, detail_errors = await _enrich_items_with_details(candidates, source=source)
        errors.extend(detail_errors[:3])
        candidates = _filter_items_for_archive_window(
            candidates,
            mode=mode,
            start_date=start_date,
            end_date=end_date,
            require_published_at=True,
        )
    return ingest_news_items(candidates, source=source), errors


def ingest_news_items(
    items: list[RawNewsItem], *, source: NewsSource | None = None,
    expected_content_hashes: dict[str, str] | None = None,
) -> dict[str, int]:
    articles_found = 0
    clusters_upserted = 0
    events_created = 0
    for item in items:
        item = prepare_article_body(item)
        analysis = analyze_news_item(item, source=source)
        # Discovery admission is not a body-repair gate for a manifest-bound
        # existing article. Keep the restored original even when its full text
        # disproves the feed's apparent relevance; the summary/impact gates
        # still decide whether it can support an industrial conclusion.
        if analysis["score"] < 25 and expected_content_hashes is None:
            continue
        summary_input_quality = classify_summary_input(item, source=source)
        articles_found += 1
        # Resolving a discovery wrapper must update the existing article, not
        # leave its permanently pending card beside a new resolved duplicate.
        article_id = _id("art", item.discovery_url or item.url or item.title)
        article_payload = {
            "source_id": item.source_id,
            "tier": item.tier,
            "url": item.url,
            "canonical_url": _canonical_url(item.url),
            "title": item.title[:200],
            "published_at": item.published_at,
            "first_seen_at": item.first_seen_at,
            "content_hash": _hash(f"{item.title}\n{item.raw_text}"),
            "language": item.language,
            "raw_text": item.raw_text,
            "summary": analysis["summary"],
            "score": analysis["score"],
            "category": analysis["category"],
            "raw": {
                "source_url": source.url if source else item.url,
                "discovery_url": item.discovery_url,
                "discovery_timestamp": item.discovery_timestamp,
                "analysis": analysis,
                "summary_input_quality": summary_input_quality,
                "source_content": {
                    "status": summary_input_quality["level"],
                    "reason": summary_input_quality["reason"],
                    "text_chars": summary_input_quality["text_chars"],
                    "body_policy": BODY_POLICY,
                    "body_method": item.body_method,
                    "body_reason": item.body_reason,
                    "body_document_url": item.body_document_url,
                    "body_document_sha256": item.body_document_sha256,
                    "stored_text_sha256": _hash(item.raw_text),
                    "stored_text_truncated": item.body_truncated,
                    "stored_text_content_hash": _hash(f"{item.title}\n{item.raw_text}"),
                    "stored_text_verified_at": datetime.now(UTC).isoformat(),
                },
            },
        }
        if expected_content_hashes is not None and article_id not in expected_content_hashes:
            raise ValueError("recovery_article_not_in_manifest")
        upsert_news_article(
            article_id=article_id, payload=article_payload,
            **({"expected_content_hash": expected_content_hashes[article_id]}
               if expected_content_hashes is not None else {}),
        )
        if summary_input_quality["eligible_for_summary"]:
            enqueue_event_ai_summary(
                article_id,
                article_payload["content_hash"],
                _deepseek_client().model,
                EVENT_SUMMARY_PROMPT_VERSION,
            )

        cluster_id = _cluster_id(
            analysis["category"],
            item.title,
            published_at=item.published_at,
            affected_products=analysis["affected_products"],
            matched_keywords=analysis["matched_keywords"],
        )
        event_record_id = f"news_{cluster_id}"
        # Source tier expresses provenance quality, not correctness of an impact
        # direction. News remains a candidate until full text, grounded Chinese
        # facts, and the independent impact gate have completed.
        can_promote_to_event = False
        if not item.published_at:
            promotion_blocked_reason = "missing_published_at"
        elif not summary_input_quality["eligible_for_summary"]:
            promotion_blocked_reason = (
                summary_input_quality["summary_blocked_reason"] or "summary_input_quality_not_full_text"
            )
        else:
            promotion_blocked_reason = "awaiting_grounded_summary_and_impact_gate"
        cluster_payload = {
            "title": item.title[:200],
            "category": analysis["category"],
            "source_ids": [item.source_id],
            "article_ids": [article_id],
            "heat_score": analysis["score"],
            "evidence_level": item.tier,
            "affected_products": analysis["affected_products"],
            "direction": "中性",
            "impact_strength": "0.00",
            "summary": analysis["summary"],
            "status": "featured" if can_promote_to_event else "candidate",
            "event_record_id": event_record_id if can_promote_to_event else None,
            "raw": {
                "news_article_id": article_id,
                "keywords": analysis["matched_keywords"],
                "promotion_blocked_reason": promotion_blocked_reason,
            },
        }
        upsert_news_event_cluster(cluster_id=cluster_id, payload=cluster_payload)
        clusters_upserted += 1
    return {"articles_found": articles_found, "clusters_upserted": clusters_upserted, "events_created": events_created}


async def process_event_summary_queue(
    *,
    limit: int = 20,
    max_attempts: int = EVENT_SUMMARY_MAX_ATTEMPTS,
    client: object | None = None,
    concurrency: int = 1,
    request_interval_seconds: float = 0.0,
    article_ids: list[str] | None = None,
    source_ids: list[str] | None = None,
) -> dict[str, int]:
    """Process pending/failed factual summaries; safe for ingestion and CLI reuse."""
    deepseek = client or _deepseek_client()
    rows = list_retryable_event_ai_summaries(
        limit=limit,
        max_attempts=max_attempts,
        article_ids=article_ids,
        **({"source_ids": source_ids} if source_ids is not None else {}),
    )
    completed = 0
    failed = 0
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(max(1, min(concurrency, 20)))

    async def process_one(index: int, row: dict[str, object]) -> None:
        nonlocal completed, failed
        article_id = str(row["article_id"])
        if not mark_event_ai_summary_processing(article_id):
            return
        source_body = clean_event_source_text(str(row.get("raw_text") or ""))
        input_chars = len(source_body)
        try:
            async with semaphore:
                if request_interval_seconds > 0:
                    await asyncio.sleep(index * request_interval_seconds)
                source_id = str(row.get("source_id") or "")
                source = get_news_source(source_id)
                input_quality = classify_summary_input(
                    RawNewsItem(
                        source_id=source_id,
                        tier=source.tier if source else "C",
                        url=str(row.get("canonical_url") or row.get("url") or ""),
                        title=str(row.get("title") or ""),
                        published_at=str(row.get("published_at") or ""),
                        raw_text=str(row.get("raw_text") or ""),
                        language=str(row.get("language") or "unknown"),
                        **stored_body_fields(row),
                    ),
                    source=source,
                )
                if not bool(input_quality["eligible_for_summary"]):
                    result = EventSummaryQualityResult(
                        status="rejected",
                        usable=False,
                        input_quality=str(input_quality["level"]),
                        rejection_reasons=[
                            str(input_quality["summary_blocked_reason"] or "insufficient_source_text"),
                            f"source_input_reason:{input_quality['reason']}",
                        ],
                    )
                    mark_event_ai_grounded_summary_result(
                        article_id,
                        result,
                        provider="quality_gate",
                        model=deepseek.model,
                        prompt_version=EVENT_SUMMARY_PROMPT_VERSION,
                        source_hash=str(row["source_hash"]),
                        input_chars=input_chars,
                    )
                    async with lock:
                        failed += 1
                    return
                result = await deepseek.summarize_event_grounded(
                    title=str(row.get("title") or ""),
                    raw_text=source_body,
                    source_name=source.source_name if source else source_id,
                    published_at=str(row.get("published_at") or ""),
                    language=str(row.get("language") or "unknown"),
                    input_quality=str(input_quality["level"]),
                    prompt_version=EVENT_SUMMARY_PROMPT_VERSION,
                )
        except asyncio.CancelledError as exc:
            mark_event_ai_summary_failed(
                article_id,
                f"{exc.__class__.__name__}:{str(exc)[:400]}",
                "deepseek",
                deepseek.model,
                EVENT_SUMMARY_PROMPT_VERSION,
                str(row["source_hash"]),
                input_chars,
            )
            async with lock:
                failed += 1
            raise
        except Exception as exc:  # failure is persisted; no generated/template fallback is allowed.
            mark_event_ai_summary_failed(
                article_id,
                f"{exc.__class__.__name__}:{str(exc)[:400]}",
                "deepseek",
                deepseek.model,
                EVENT_SUMMARY_PROMPT_VERSION,
                str(row["source_hash"]),
                input_chars,
            )
            async with lock:
                failed += 1
            return
        mark_event_ai_grounded_summary_result(
            article_id,
            result,
            provider="deepseek",
            model=deepseek.model,
            prompt_version=EVENT_SUMMARY_PROMPT_VERSION,
            source_hash=str(row["source_hash"]),
            input_chars=input_chars,
        )
        if (
            result.usable
            and result.fact_summary_status == "completed"
            and result.impact_analysis_status == "completed"
            and result.business_impact is not None
        ):
            promote_grounded_news_event(
                article_id,
                result.factual_summary,
                result.business_impact.model_dump(mode="json"),
            )
        async with lock:
            if result.usable:
                completed += 1
            else:
                failed += 1

    await asyncio.gather(*(process_one(index, row) for index, row in enumerate(rows)))
    return {"selected": len(rows), "completed": completed, "failed": failed}


def _deepseek_client() -> object:
    # Local import avoids the deepseek_client -> rag -> news dependency cycle.
    from .deepseek_client import DeepSeekClient

    return DeepSeekClient()


def _empty_deep_structure_hints(
    item: RawNewsItem,
    source: NewsSource | None,
    *,
    category: str,
    event_status: str,
) -> dict[str, object]:
    source_id = (source.source_id if source else item.source_id).strip()
    discovery = _is_discovery_source(source_id)
    return {
        "actor_hints": [],
        "source_type": "discovery_signal" if discovery else _source_type(item, source),
        "event_status": "discovery_signal" if discovery else event_status,
        "geography": [],
        "affected_route": [],
        "policy_tool": [],
        "confidence_floor": 0.25 if discovery else 0.1,
        "category": category,
        "notes": ["lightweight_pre_llm_extract", "low_signal_or_unmatched"],
    }


def _deep_structure_hints(
    item: RawNewsItem,
    source: NewsSource | None,
    *,
    text: str,
    category: str,
    direction: str,
    affected_products: list[str],
    score: int,
) -> dict[str, object]:
    source_id = (source.source_id if source else item.source_id).strip()
    discovery = _is_discovery_source(source_id)
    actor_hints = _matched_labels(text, ACTOR_HINT_TERMS)
    geography = _matched_labels(text, GEOGRAPHY_TERMS)
    affected_route = _matched_labels(text, ROUTE_TERMS)
    policy_tool = _matched_labels(text, POLICY_TOOL_TERMS)
    notes = ["lightweight_pre_llm_extract"]
    if discovery:
        notes.append("c_tier_discovery_source_not_high_confidence")
    if category in {"oil_policy", "sanctions_geopolitics", "shipping_security"} and any(
        product in {"POY", "DTY"} for product in affected_products
    ):
        notes.append("long_transmission_chain_requires_llm_or_price_confirmation")
    return {
        "actor_hints": actor_hints,
        "source_type": "discovery_signal" if discovery else _source_type(item, source),
        "event_status": "discovery_signal" if discovery else _event_status(text, source=source, score=score),
        "geography": geography,
        "affected_route": affected_route,
        "policy_tool": policy_tool,
        "confidence_floor": _confidence_floor(item=item, source=source, discovery=discovery, score=score),
        "direction_hint": direction,
        "category": category,
        "affected_products": affected_products,
        "notes": notes,
    }


def _matched_labels(text: str, term_map: dict[str, tuple[str, ...]]) -> list[str]:
    return sorted(
        label for label, terms in term_map.items() if any(_term_in_text(term.lower(), text) for term in terms)
    )


def _is_discovery_source(source_id: str) -> bool:
    normalized = source_id.lower()
    return normalized.startswith(DISCOVERY_SOURCE_PREFIXES)


def classify_summary_input(
    item: RawNewsItem,
    *,
    source: NewsSource | None = None,
) -> dict[str, object]:
    """Classify source text before it can enter the formal-summary queue."""
    source_text = _clean_text(item.raw_text)
    raw_text = clean_event_source_text(item.raw_text)
    title = _clean_text(item.title)
    source_id = source.source_id if source else item.source_id
    discovery = _is_discovery_source(source_id)
    source_host = (urlparse(item.url).hostname or "").lower()
    defect = item.body_reason or body_defect(
        item.raw_text, method=item.body_method, truncated=item.body_truncated,
    ) or body_defect(
        source_text, method=item.body_method, truncated=item.body_truncated,
    ) or body_defect(
        raw_text, method=item.body_method, truncated=item.body_truncated,
    )

    if error_page_title(title):
        level = "title_only"
        reason = "source_error_page"
    elif _has_access_barrier(source_text) or item.detail_reason == "access_restricted":
        level = "title_only"
        reason = "access_restricted"
    elif not raw_text or raw_text.casefold() == title.casefold() or len(raw_text) < PARTIAL_TEXT_MIN_CHARS:
        level = "title_only"
        reason = "title_only"
    elif discovery and source_host in DISCOVERY_HOSTS:
        level = "partial_text"
        reason = "discovery_snippet"
    elif defect:
        level = "partial_text"
        reason = defect
    elif sunsirs_body(raw_text, url=item.url, title=item.title):
        level = "partial_text"
        reason = "publisher_body_needs_extraction"
    elif (item.body_method == "publisher_100ppi" and source_host in {"www.100ppi.com", "100ppi.com"}
          and re.fullmatch(r"/news/detail-\d{8}-\d+\.html", urlparse(item.url).path)
          and len(raw_text) >= 80) or (
          item.body_method == "publisher_texnet" and source_host == "info.texnet.com.cn"
          and re.fullmatch(r"/detail-\d+\.html", urlparse(item.url).path)
          and len(raw_text) >= PARTIAL_TEXT_MIN_CHARS):
        level = "full_text"
        reason = "verified_publisher_brief"
    elif _verified_short_official(item):
        level = "full_text"
        reason = "verified_short_official_article"
    elif len(raw_text) < FULL_TEXT_MIN_CHARS:
        level = "partial_text"
        reason = "insufficient_article_body"
    else:
        level = "full_text"
        reason = "sufficient_article_body"

    if level != "full_text" and item.detail_reason and reason != "source_error_page":
        reason = item.detail_reason

    return {
        "level": level,
        "reason": reason,
        "eligible_for_summary": level == "full_text",
        "summary_blocked_reason": "",
        "source_id": source_id,
        "source_url": item.url,
        "discovery_source": discovery,
        "text_chars": len(raw_text),
        "body_policy": BODY_POLICY,
        "body_method": item.body_method,
    }


def prepare_article_body(item: RawNewsItem) -> RawNewsItem:
    """Normalize on acquisition/ingest only, never silently rewrite a queued version."""
    text = clean_event_source_text(item.raw_text)
    chinese = ppi_chinese_article(text, url=item.url, title=item.title)
    if (chinese and not _has_access_barrier(text) and not item.body_truncated
            and item.body_reason in {"", "article_body_not_located"}):
        return replace(item, title=chinese["title"], raw_text=chinese["body"],
                       body_method="publisher_100ppi", body_reason="")
    excerpt = (sunsirs_body(text, url=item.url, title=item.title)
               or texnet_body(text, url=item.url, title=item.title))
    if excerpt and not _has_access_barrier(text) and not item.body_truncated and not item.body_reason:
        text, method = excerpt
        return replace(item, raw_text=text, body_method=method)
    return replace(item, raw_text=text, body_reason=item.body_reason or body_defect(
        _clean_text(item.raw_text), method=item.body_method, truncated=item.body_truncated,
    ))


def stored_body_fields(row: dict) -> dict[str, object]:
    """Propagate persisted defects through queue/recovery, including old caps."""
    metadata = row.get("raw") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            return {"body_reason": "invalid_body_metadata"}
    content = metadata.get("source_content", {}) if isinstance(metadata, dict) else {}
    if not isinstance(content, dict):
        return {"body_reason": "invalid_body_metadata"}
    reason = str(content.get("body_reason") or "")
    stored_hash = content.get("stored_text_sha256")
    if stored_hash and stored_hash != _hash(str(row.get("raw_text") or "")):
        reason = "stored_body_hash_mismatch"
    return {
        "body_method": str(content.get("body_method") or ""),
        "body_truncated": bool(content.get("stored_text_truncated")),
        "body_reason": reason,
        "body_document_url": str(content.get("body_document_url") or ""),
        "body_document_sha256": str(content.get("body_document_sha256") or ""),
    }


def _verified_short_official(item: RawNewsItem) -> bool:
    from .publisher_content import verified_publisher_excerpt
    result = verified_publisher_excerpt(item.url, item.raw_text, item.title)
    return bool(result and result["publisher_id"] == "ndrc" and item.source_id == "ndrc_news")


def _has_access_barrier(text: str) -> bool:
    normalized = _clean_text(text).casefold()
    return any(term in normalized for term in ACCESS_BARRIER_TERMS)


def _source_type(item: RawNewsItem, source: NewsSource | None) -> str:
    tier = source.tier if source else item.tier
    source_id = (source.source_id if source else item.source_id).lower()
    if _is_discovery_source(source_id):
        return "discovery_signal"
    if tier == "A":
        return "primary_or_official_signal"
    if tier == "B":
        return "institutional_signal"
    return "public_news_signal"


def _event_status(text: str, *, source: NewsSource | None, score: int) -> str:
    if score <= 0:
        return "filtered_low_signal"
    if any(_term_in_text(term, text) for term in ("rumor", "unconfirmed", "market talk", "传闻", "未经证实")):
        return "rumor"
    if any(_term_in_text(term, text) for term in ("warn", "warning", "advisory", "alert", "警告", "预警")):
        return "warning"
    if any(
        _term_in_text(term, text)
        for term in ("report", "data", "inventory", "stocks", "cpi", "ppi", "报告", "数据", "库存")
    ):
        return "data_release"
    if any(
        _term_in_text(term, text) for term in ("announce", "said", "statement", "press release", "公告", "声明", "表示")
    ):
        return "official_statement"
    if any(
        _term_in_text(term, text)
        for term in ("attack", "strike", "cut", "shutdown", "closed", "袭击", "打击", "关闭", "减产")
    ):
        return "executed_action"
    if source and source.tier in {"A", "B"}:
        return "structured_candidate"
    return "analyst_opinion"


def _confidence_floor(*, item: RawNewsItem, source: NewsSource | None, discovery: bool, score: int) -> float:
    if discovery:
        return 0.25
    tier = source.tier if source else item.tier
    floor = {"A": 0.45, "B": 0.35, "C": 0.25, "D": 0.15}.get(tier, 0.2)
    if score < 50:
        floor = min(floor, 0.25)
    return round(floor, 2)


def analyze_news_item(item: RawNewsItem, *, source: NewsSource | None = None) -> dict[str, object]:
    text = f"{item.title} {item.raw_text}".lower()
    if unusable_title(item.title) or _is_low_signal_news_text(item.title, item.raw_text):
        category = source.category if source else "general"
        return {
            "category": category,
            "matched_keywords": [],
            "affected_products": [],
            "direction": "中性",
            "impact_strength": 0.0,
            "score": 0,
            "summary": _summary(item, category, [], "中性"),
            "deep_structure_hints": _empty_deep_structure_hints(
                item,
                source,
                category=category,
                event_status="filtered_low_signal",
            ),
        }
    category_scores: dict[str, int] = {}
    matched_keywords: list[str] = []
    for category, keywords in KEYWORDS.items():
        hits = [keyword for keyword in keywords if _term_in_text(keyword, text)]
        if hits:
            category_scores[category] = len(hits)
            matched_keywords.extend(hits)
    category = (
        max(category_scores, key=category_scores.get) if category_scores else (source.category if source else "general")
    )
    if source and source.category != category and category_scores.get(category, 0) <= 1:
        category = source.category

    affected_products = [
        product
        for product, keywords in PRODUCT_KEYWORDS.items()
        if any(product_term_matches(keyword, text) for keyword in keywords)
    ]
    if _only_broad_context_hits(text, matched_keywords) and not affected_products:
        matched_keywords = []
        category_scores = {}
    if (
        category == "sanctions_geopolitics"
        and matched_keywords
        and not _has_sanctions_energy_context(text, affected_products)
    ):
        matched_keywords = []
        category_scores = {}

    if not matched_keywords and not affected_products:
        category = source.category if source else "general"
        return {
            "category": category,
            "matched_keywords": [],
            "affected_products": [],
            "direction": "中性",
            "impact_strength": 0.0,
            "score": 0,
            "summary": _summary(item, category, [], "中性"),
            "deep_structure_hints": _empty_deep_structure_hints(
                item,
                source,
                category=category,
                event_status="filtered_low_signal",
            ),
        }
    direction = _direction_for_text(text, category)
    impact_strength = _impact_strength(text, category, affected_products)
    tier_score = {"A": 34, "B": 26, "C": 16, "D": 8}.get(item.tier, 16)
    relevance_score = min(28, 7 * len(set(matched_keywords)))
    product_score = 16 if affected_products else 4
    freshness_score = 10 if item.published_at else 6
    score = min(100, tier_score + relevance_score + product_score + round(impact_strength * 12) + freshness_score)
    summary = _summary(item, category, affected_products, direction)
    return {
        "category": category,
        "matched_keywords": sorted(set(matched_keywords)),
        "affected_products": affected_products,
        "direction": direction,
        "impact_strength": impact_strength,
        "score": score,
        "summary": summary,
        "deep_structure_hints": _deep_structure_hints(
            item,
            source,
            text=text,
            category=category,
            direction=direction,
            affected_products=affected_products,
            score=score,
        ),
    }


def _source_urls_for_mode(
    source: NewsSource,
    *,
    mode: str,
    start_date: str | None,
    end_date: str | None,
    cursor_pages: int,
) -> list[str]:
    if mode != "archive":
        if source.source_id in EIA_NEWS_FEEDS:
            return [EIA_NEWS_FEEDS[source.source_id], source.url]
        if source.source_id == "opec_press":
            return [OPEC_DISCOVERY_RSS_URL, source.url, OPEC_HOME_URL]
        return [source.url]
    return _archive_urls(source, start_date=start_date, end_date=end_date, cursor_pages=cursor_pages)


def _archive_urls(
    source: NewsSource,
    *,
    start_date: str | None,
    end_date: str | None,
    cursor_pages: int = 3,
) -> list[str]:
    start = _date_only(start_date)
    end = _date_only(end_date)
    pages = _bounded_pages(cursor_pages)
    if source.source_id == "opec_press":
        if start and end:
            return [_opec_google_archive_url(start_date=start, end_date=end)]
        return [source.url, OPEC_HOME_URL]
    if source.source_id == "eia_press":
        return [
            _with_query("https://www.eia.gov/pressroom/releases.php", {"year": str(year)})
            for year in _years(start, end)
        ]
    if source.source_id == "eia_today_in_energy":
        return [
            _with_query("https://www.eia.gov/todayinenergy/archive.php", {"my": str(year)})
            for year in _years(start, end)
        ]
    if source.source_id in {"google_news_oil_rss", "google_news_chemical_rss"} and start and end:
        return [_google_news_archive_url(source, start_date=start, end_date=end)]
    if source.source_id == "ofac_recent_actions":
        return _paginated_query_urls(
            source.url,
            {
                "field_publish_date_value[min]": start,
                "field_publish_date_value[max]": end,
            },
            pages=pages,
            first_page=0,
        )
    if source.source_id == "treasury_press":
        return _paginated_query_urls(
            source.url,
            {
                "field_press_release_date_value[min]": start,
                "field_press_release_date_value[max]": end,
            },
            pages=pages,
            first_page=0,
        )
    if source.source_id == "eu_council_press":
        return _paginated_query_urls(source.url, {"dateFrom": start, "dateTo": end}, pages=pages, first_page=1)
    if source.source_id == "federal_reserve_press":
        return [
            f"https://www.federalreserve.gov/newsevents/pressreleases/{year}-press.htm" for year in _years(start, end)
        ]
    if source.source_id == "iea_news":
        base = source.url.rstrip("/")
        return [source.url, *[f"{base}?page={page}" for page in range(2, pages + 1)]]
    if source.source_id == "gdelt_oil_geopolitics_rss" and start and end:
        return _gdelt_daily_archive_urls(start_date=start, end_date=end)
    if source.source_id in {"white_house_news", "white_house_statements"}:
        base = source.url.rstrip("/")
        return [source.url, *[f"{base}/page/{page}/" for page in range(2, pages + 1)]]
    if source.source_id == "un_press_releases":
        return [source.url]
    if source.source_id == "un_security_council_press":
        return _paginated_query_urls(source.url, {}, pages=pages, first_page=0)
    if source.source_id in {"uk_government_news", "uk_fcdo_news"}:
        return _paginated_query_urls(source.url, {"from_date": start, "to_date": end}, pages=pages, first_page=1)
    return [source.url]


def _google_news_archive_url(source: NewsSource, *, start_date: str, end_date: str) -> str:
    query = (
        'OPEC OR "crude oil" OR Hormuz OR OFAC'
        if source.source_id == "google_news_oil_rss"
        else (
            '"monoethylene glycol" OR "ethylene glycol" OR '
            '"purified terephthalic acid" OR paraxylene OR '
            '"polyester filament" OR "polyester POY" OR "polyester DTY" OR "polyester yarn"'
        )
    )
    # Google News RSS treats before: as an exclusive upper bound, so move the end fence one day forward.
    before = (_date_from_iso(start_date=end_date) + timedelta(days=1)).isoformat()
    return _with_query(
        "https://news.google.com/rss/search",
        {
            "q": f"{query} after:{start_date} before:{before}",
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        },
    )


def _opec_google_archive_url(*, start_date: str, end_date: str) -> str:
    before = (_date_from_iso(start_date=end_date) + timedelta(days=1)).isoformat()
    return _with_query(
        "https://news.google.com/rss/search",
        {
            "q": f"site:opec.org/pr-detail OPEC after:{start_date} before:{before}",
            "hl": "en-US",
            "gl": "US",
            "ceid": "US:en",
        },
    )


def _gdelt_daily_archive_urls(*, start_date: str, end_date: str) -> list[str]:
    query = '(OPEC OR "crude oil" OR Hormuz OR OFAC OR tanker OR sanctions OR "Red Sea" OR "Strait of Hormuz")'
    urls: list[str] = []
    for day in _daily_dates(start_date=start_date, end_date=end_date):
        urls.append(
            _with_query(
                "https://api.gdeltproject.org/api/v2/doc/doc",
                {
                    "query": query,
                    "mode": "artlist",
                    "format": "rss",
                    "maxrecords": "25",
                    "sort": "hybridrel",
                    "startdatetime": f"{_compact_date(day.isoformat())}000000",
                    "enddatetime": f"{_compact_date(day.isoformat())}235959",
                },
            )
        )
    return urls


def _daily_dates(*, start_date: str, end_date: str) -> list[date]:
    start = _date_from_iso(start_date=start_date)
    end = _date_from_iso(start_date=end_date)
    if end < start:
        start, end = end, start
    days: list[date] = []
    cursor = start
    while cursor <= end:
        days.append(cursor)
        cursor += timedelta(days=1)
    return days


def _date_from_iso(*, start_date: str) -> date:
    return date.fromisoformat(start_date[:10])


def _bounded_pages(cursor_pages: int) -> int:
    return min(max(cursor_pages, 1), 10)


def _paginated_query_urls(
    url: str,
    params: dict[str, str | int | None],
    *,
    pages: int,
    first_page: int,
) -> list[str]:
    urls: list[str] = []
    for offset in range(pages):
        page = first_page + offset
        page_params: dict[str, str | None] = {
            key: str(value) for key, value in params.items() if value not in {None, ""}
        }
        page_params["page"] = str(page)
        urls.append(_with_query(url, page_params))
    return urls


def _with_query(url: str, params: dict[str, str | None]) -> str:
    clean = {key: value for key, value in params.items() if value}
    if not clean:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(clean, doseq=False)}"


def _date_only(value: str | None) -> str:
    if not value:
        return ""
    match = re.search(r"\d{4}-\d{2}-\d{2}", value)
    return match.group(0) if match else value[:10]


def _filter_items_for_archive_window(
    items: list[RawNewsItem],
    *,
    mode: str,
    start_date: str | None,
    end_date: str | None,
    require_published_at: bool,
) -> list[RawNewsItem]:
    if mode != "archive" or not start_date or not end_date:
        return items
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except ValueError:
        return items

    filtered: list[RawNewsItem] = []
    for item in items:
        published = _normalized_published_date(item.published_at)
        if not published:
            if not require_published_at:
                filtered.append(item)
            continue
        if start <= published <= end:
            filtered.append(replace(item, published_at=published.isoformat()))
    return filtered


def _normalized_published_date(value: str) -> date | None:
    normalized = _extract_date(value) or _date_only(value)
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def _compact_date(value: str) -> str:
    return value.replace("-", "")


def _years(start_date: str, end_date: str) -> list[int]:
    now_year = datetime.now(UTC).year
    start_year = int(start_date[:4]) if re.match(r"^\d{4}", start_date) else now_year
    end_year = int(end_date[:4]) if re.match(r"^\d{4}", end_date) else start_year
    if start_year > end_year:
        start_year, end_year = end_year, start_year
    return list(range(start_year, end_year + 1))


def _decode_news_response(response: httpx.Response) -> str:
    """Respect declared legacy Chinese HTML encodings before text extraction."""
    content = getattr(response, "content", b"")
    if isinstance(content, bytes):
        declared = re.search(rb"charset\s*=\s*[\"']?\s*(gb2312|gbk|gb18030)\b", content[:4096], re.I)
        if declared:
            try:
                return content.decode("gb18030", errors="strict")
            except UnicodeDecodeError:
                pass
    return response.text


async def _fetch_text(url: str, *, referer: str | None = None) -> tuple[str, str]:
    host = settings.require_outbound_url_allowed(url)
    headers = _fetch_headers()
    if referer:
        headers["Referer"] = referer
    last_error: Exception | None = None
    for attempt in range(FETCH_ATTEMPTS):
        if host == "api.gdeltproject.org":
            await asyncio.sleep(_gdelt_backoff_seconds(attempt))
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
                response = await _get_with_validated_redirects(client, url, headers=headers)
                response.raise_for_status()
                response_host = settings.require_outbound_url_allowed(str(getattr(response, "url", "") or url))
                if response_host == "api.gdeltproject.org" and "Please limit requests" in response.text:
                    raise RuntimeError("GDELT rate limited; retry after 5 seconds")
                if _is_guarded_browser_check(response.text, response_host):
                    raise RuntimeError(f"{response_host} guarded by browser check")
                return _decode_news_response(response), response.headers.get("content-type", "text/html")
        except httpx.HTTPStatusError as exc:
            last_error = exc
            status_code = exc.response.status_code
            if status_code == 406 and host == "press.un.org":
                return await asyncio.to_thread(_fetch_text_with_urllib, url)
            if status_code < 500 and status_code not in {408, 429}:
                raise
        except RuntimeError as exc:
            if host != "api.gdeltproject.org" or "GDELT rate limited" not in str(exc):
                raise
            last_error = exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
        if attempt + 1 < FETCH_ATTEMPTS:
            await asyncio.sleep(_retry_backoff_seconds(host, attempt))
    if last_error is not None:
        raise last_error
    raise RuntimeError("news fetch failed without an exception")


async def _get_with_validated_redirects(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
) -> httpx.Response:
    """Follow a bounded redirect chain only after validating every destination."""

    current_url = url
    for redirect_count in range(MAX_FETCH_REDIRECTS + 1):
        settings.require_outbound_url_allowed(current_url)
        response = await client.get(current_url, headers=headers)
        if response.status_code not in REDIRECT_STATUS_CODES:
            return response
        if redirect_count >= MAX_FETCH_REDIRECTS:
            raise ValueError("news fetch redirect limit exceeded")
        location = response.headers.get("location", "").strip()
        if not location:
            raise ValueError("news fetch redirect is missing Location")
        next_url = urljoin(current_url, location)
        settings.require_outbound_url_allowed(next_url)
        current_url = next_url
    raise ValueError("news fetch redirect limit exceeded")


def _gdelt_backoff_seconds(attempt: int) -> float:
    return min(GDELT_MAX_BACKOFF_SECONDS, GDELT_MIN_REQUEST_INTERVAL_SECONDS * (2**attempt))


def _retry_backoff_seconds(host: str, attempt: int) -> float:
    if host == "api.gdeltproject.org":
        return min(GDELT_MAX_BACKOFF_SECONDS, GDELT_MIN_REQUEST_INTERVAL_SECONDS * (2 ** (attempt + 1)))
    return 0.4 * (attempt + 1)


def _fetch_headers() -> dict[str, str]:
    return {
        "User-Agent": "POY-DTY-Agent/1.0 personal-research",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.7,zh;q=0.6",
    }


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


def _fetch_text_with_urllib(url: str) -> tuple[str, str]:
    opener = build_opener(_NoRedirectHandler())
    current_url = url
    for redirect_count in range(MAX_FETCH_REDIRECTS + 1):
        settings.require_outbound_url_allowed(current_url)
        request = Request(current_url, headers=_fetch_headers())
        try:
            response = opener.open(request, timeout=20)  # noqa: S310 - every redirect hop is allowlisted above.
        except HTTPError as exc:
            if exc.code not in REDIRECT_STATUS_CODES:
                raise
            if redirect_count >= MAX_FETCH_REDIRECTS:
                raise ValueError("news fetch redirect limit exceeded") from None
            location = exc.headers.get("location", "").strip()
            if not location:
                raise ValueError("news fetch redirect is missing Location") from None
            next_url = urljoin(current_url, location)
            settings.require_outbound_url_allowed(next_url)
            current_url = next_url
            continue
        with response:
            content_type = response.headers.get("content-type", "text/html")
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace"), content_type
    raise ValueError("news fetch redirect limit exceeded")


async def _enrich_items_with_details(
    items: list[RawNewsItem],
    *,
    source: NewsSource,
) -> tuple[list[RawNewsItem], list[str]]:
    enriched: list[RawNewsItem] = []
    errors: list[str] = []
    source_referer = SOURCE_FETCH_REFERERS.get(source.source_id)
    for item in items:
        if (urlparse(item.url).hostname or "").lower() in DISCOVERY_HOSTS:
            wrapper_url = item.url
            if _is_google_news_article_url(wrapper_url) and monotonic() < _google_news_resolution_retry_at:
                enriched.append(replace(item, detail_reason="discovery_rate_limited"))
                continue
            try:
                wrapper_text, wrapper_type = await _fetch_text(item.url, referer=source_referer)
                if "html" in wrapper_type.lower():
                    original_url = _public_original_url(wrapper_text, source=source)
                    if not original_url and _is_google_news_article_url(wrapper_url):
                        original_url = await _decode_google_news_url(wrapper_url, wrapper_html=wrapper_text)
                    if original_url:
                        candidate = replace(item, url=original_url, discovery_url=item.discovery_url or wrapper_url)
                        if _should_fetch_detail(candidate, source=source):
                            item = candidate
                        else:
                            auth = _source_registry_auth_by_host().get((urlparse(original_url).hostname or "").lower())
                            reason = (
                                "access_restricted" if auth and auth not in PUBLIC_NEWS_AUTH_TYPES
                                else "source_not_enabled"
                            )
                            # Retain the resolved identity for diagnosis/retry.
                            # The detail gate below still prevents the request.
                            item = replace(candidate if _public_publisher_identity(original_url) else item,
                                           detail_reason=reason)
                    else:
                        item = replace(item, detail_reason="original_url_unresolved")
            except Exception as exc:  # noqa: BLE001 - unresolved discovery items remain non-summary evidence.
                errors.append(f"detail_resolve:{_error_label(exc)} @ {item.url[:120]}")
                item = replace(item, detail_reason=_detail_failure_reason(exc, resolving=True))
        if not _should_fetch_detail(item, source=source):
            if not item.detail_reason:
                host = (urlparse(item.url).hostname or "").lower()
                auth = _source_registry_auth_by_host().get(host)
                reason = "access_restricted" if auth and auth not in PUBLIC_NEWS_AUTH_TYPES else "source_not_enabled"
                item = replace(item, detail_reason=reason)
            enriched.append(item)
            continue
        try:
            text, content_type = await _fetch_text(item.url, referer=source_referer)
            if "pdf" in content_type.lower():
                enriched.append(replace(item, detail_reason="unsupported_document"))
                continue
            if _has_access_barrier(text) or _page_disallows_extraction(text):
                errors.append(f"detail:access_restricted @ {item.url[:120]}")
                enriched.append(replace(item, detail_reason="access_restricted"))
                continue
            detail = _extract_article_detail(text, source_url=item.url)
            document = None
            if source.source_id == "mpa_press_releases":
                from .official_news_pdf import OfficialPdfError, read_attachment
                try:
                    document = await read_attachment(text, item.url, item.title, _fetch_headers())
                except OfficialPdfError as exc:
                    enriched.append(replace(item, detail_reason=str(exc)))
                    continue
                if document:
                    detail.update(text=document["text"], body_method="official_pdf_mpa", body_reason="")
            opec_brand_title = (
                source.source_id == "opec_press" and _is_official_opec_article_url(item.url)
                and _is_generic_navigation_link(item.title, item.url)
            )
            if not opec_brand_title and titles_conflict(item.title, detail["headline"]):
                enriched.append(replace(item, body_reason="article_title_mismatch"))
                continue
            # A shorter article is better than a long feed/navigation prefix.
            # Failed fetches preserve old content; successful detail fetches
            # use only that page's extracted body and its quality metadata.
            raw_text = clean_event_source_text(detail["text"])
            title = (
                detail["title"]
                if detail["title"] and not _is_generic_navigation_link(detail["title"], item.url)
                else item.title
            )
            published_at = item.published_at or detail["published_at"] or _extract_date(item.url)
            if item.source_id.startswith("gdelt_"):
                # Also correct rehydrated older rows that stored RSS timestamps
                # in published_at before the discovery/publication separation.
                item = replace(item, discovery_timestamp=item.discovery_timestamp or item.published_at)
                published_at = detail["published_at"] or _extract_date(item.url)
            if detail["published_at"].startswith(published_at) and len(detail["published_at"]) > len(published_at):
                published_at = detail["published_at"]
            enriched.append(replace(
                item, title=title, published_at=published_at, raw_text=raw_text, detail_reason="",
                body_method=detail["body_method"], body_truncated=detail["body_truncated"],
                body_reason=detail["body_reason"],
                body_document_url=document["url"] if document else "",
                body_document_sha256=document["sha256"] if document else "",
            ))
        except Exception as exc:  # noqa: BLE001 - detail hydration should not fail the fetch run.
            errors.append(f"detail:{_error_label(exc)} @ {item.url[:120]}")
            enriched.append(replace(item, detail_reason=_detail_failure_reason(exc)))
    return enriched, errors


def _detail_failure_reason(exc: Exception, *, resolving: bool = False) -> str:
    if isinstance(exc, GoogleNewsResolutionDeferred):
        return "discovery_rate_limited"
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None) or getattr(exc, "code", None)
    if status in {401, 403}:
        return "access_restricted"
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "source_timeout"
    return "original_url_unresolved" if resolving else "source_fetch_failed"


async def _resolve_opec_discovery_items(
    items: list[RawNewsItem],
) -> tuple[list[RawNewsItem], list[str]]:
    resolved: list[RawNewsItem] = []
    errors: list[str] = []
    for item in items:
        if not _is_google_news_article_url(item.url):
            resolved.append(item)
            continue
        try:
            official_url = await _decode_opec_google_news_url(item.url)
            resolved.append(replace(item, url=official_url))
        except Exception as exc:  # noqa: BLE001 - unresolved wrappers are never stored as Tier-A evidence.
            errors.append(f"opec_discovery_resolve:{_error_label(exc)} @ {item.url[:120]}")
    return resolved, errors


async def _decode_opec_google_news_url(wrapper_url: str) -> str:
    official_url = await _decode_google_news_url(wrapper_url)
    if not _is_official_opec_article_url(official_url):
        raise ValueError("decoded URL is not an official OPEC press release")
    return official_url


async def _decode_google_news_url(wrapper_url: str, *, wrapper_html: str | None = None) -> str:
    """Resolve public Google wrapper metadata; the caller still gates the target host.

    The public endpoint answers a well-formed request with a null payload at an
    unpredictable rate, so each attempt re-fetches the wrapper for fresh
    single-use metadata before re-posting.
    """
    if not _is_google_news_article_url(wrapper_url):
        raise ValueError("not a Google News article wrapper")
    if monotonic() < _google_news_resolution_retry_at:
        raise GoogleNewsResolutionDeferred("Google News resolution cooling down")
    last_error: ValueError | None = None
    for attempt in range(GOOGLE_NEWS_DECODE_ATTEMPTS):
        try:
            page_html = wrapper_html if (wrapper_html is not None and attempt == 0) else None
            if page_html is None:
                page_html, content_type = await _fetch_text(wrapper_url)
                if "html" not in content_type.lower():
                    raise ValueError("Google News wrapper did not return HTML")
            if _has_access_barrier(page_html):
                raise ValueError("Google News wrapper is access restricted")
            return await _decode_google_news_url_once(wrapper_url, page_html)
        except ValueError as exc:
            # Only the endpoint's intermittent null payload is transient.
            # Security rejections (invalid URL, access barrier) must fail closed
            # on the first attempt without re-requesting anything.
            if "did not contain a URL" not in str(exc):
                raise
            last_error = exc
    assert last_error is not None
    raise last_error


async def _decode_google_news_url_once(wrapper_url: str, wrapper_html: str) -> str:
    global _google_news_resolution_retry_at
    if _has_access_barrier(wrapper_html):
        raise ValueError("Google News wrapper is access restricted")
    article_id = _html_data_attribute(wrapper_html, "data-n-a-id")
    timestamp = _html_data_attribute(wrapper_html, "data-n-a-ts")
    signature = _html_data_attribute(wrapper_html, "data-n-a-sg")
    if not article_id or not timestamp.isdigit() or not signature:
        raise ValueError("Google News wrapper metadata is incomplete")

    batch_request = _google_news_batchexecute_request(article_id, timestamp, signature)
    settings.require_outbound_url_allowed(GOOGLE_NEWS_DECODE_URL)
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(
            GOOGLE_NEWS_DECODE_URL,
            data={"f.req": batch_request},
            headers={**_fetch_headers(), "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        location = urlparse(response.headers.get("location", ""))
        challenged = (
            response.status_code in REDIRECT_STATUS_CODES
            and location.hostname in {"www.google.com", "google.com", "news.google.com"}
            and location.path.startswith("/sorry/")
        )
        if response.status_code in {403, 429} or challenged:
            _google_news_resolution_retry_at = monotonic() + GOOGLE_NEWS_RESOLUTION_COOLDOWN_SECONDS
            raise GoogleNewsResolutionDeferred("Google News resolution challenged or rate limited")
        response.raise_for_status()
    original_url = _decoded_google_news_url(response.text)
    parsed = urlparse(original_url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Google News returned an invalid public URL")
    return original_url


def _google_news_batchexecute_request(article_id: str, timestamp: str, signature: str) -> str:
    """Build the public batchexecute body that resolves a Google News wrapper URL.

    The endpoint rejects the retired finance-index request template with a null
    payload; this minimal template is the shape the endpoint still answers.
    """

    inner_request = json.dumps(
        [
            "garturlreq",
            [
                ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, 0, 1],
                "X",
                "X",
                1,
                [1, 1, 1],
                1,
                1,
                None,
                0,
                0,
                None,
                0,
            ],
            article_id,
            int(timestamp),
            signature,
        ],
        separators=(",", ":"),
    )
    return json.dumps([[["Fbv4je", inner_request, None, "generic"]]], separators=(",", ":"))


def _html_data_attribute(text: str, name: str) -> str:
    match = re.search(rf'{re.escape(name)}=["\']([^"\']+)["\']', text)
    return match.group(1) if match else ""


def _decoded_google_news_url(response_text: str) -> str:
    for line in response_text.splitlines():
        if not line.startswith("[["):
            continue
        try:
            outer = json.loads(line)
        except (TypeError, ValueError):
            continue
        for item in outer if isinstance(outer, list) else []:
            if not isinstance(item, list) or len(item) < 3 or item[:2] != ["wrb.fr", "Fbv4je"]:
                continue
            try:
                decoded = json.loads(item[2])
            except (TypeError, ValueError):
                continue
            if isinstance(decoded, list) and len(decoded) > 1 and isinstance(decoded[1], str):
                return decoded[1]
    raise ValueError("Google News decode response did not contain a URL")


def _is_google_news_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https" and parsed.hostname == "news.google.com" and parsed.path.startswith("/rss/articles/")
    )


def _is_official_opec_article_url(url: str) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() in {"opec.org", "www.opec.org"}
        and parsed.path.startswith("/pr-detail/")
    )


def _public_original_url(wrapper_html: str, *, source: NewsSource) -> str:
    """Resolve only allowlisted, registry-public links exposed by a discovery page."""
    candidates = re.findall(
        r"""(?:href|content)\s*=\s*["'](https?://[^"'<> ]+)["']""",
        wrapper_html,
        flags=re.IGNORECASE,
    )
    for candidate in candidates:
        candidate = candidate.replace("&amp;", "&")
        if _should_fetch_detail(
            RawNewsItem(source.source_id, source.tier, candidate, ""),
            source=source,
        ):
            return candidate
    return ""


def _should_fetch_detail(item: RawNewsItem, *, source: NewsSource | None = None) -> bool:
    parsed = urlparse(item.url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or not host
        or host not in settings.outbound_hosts
    ):
        return False
    if host in DISCOVERY_HOSTS:
        return False
    registered_auth = _source_registry_auth_by_host().get(host)
    if registered_auth is not None:
        return registered_auth in PUBLIC_NEWS_AUTH_TYPES

    # OPEC's already-configured official source publishes on both canonical
    # hosts. Outbound and registry-access gates above still take precedence.
    if (source and source.source_id == "opec_press"
            and host in {"opec.org", "www.opec.org"}):
        return True
    if host in DISCOVERY_PUBLISHER_HOSTS:
        return True

    configured_public_hosts = {
        (urlparse(candidate.url).hostname or "").lower()
        for candidate in NEWS_SOURCES
        if not _is_discovery_source(candidate.source_id)
    }
    if host in configured_public_hosts:
        return True
    if source and not _is_discovery_source(source.source_id):
        return host == (urlparse(source.url).hostname or "").lower()
    return False


def _public_publisher_identity(url: str) -> bool:
    """Retain an unapproved public hostname for review, never an internal link."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443} or "." not in host
                or host.endswith((".localhost", ".local", ".internal"))):
            return False
        try:
            ip_address(host)
            return False
        except ValueError:
            return True
    except ValueError:
        return False


@lru_cache(maxsize=1)
def _source_registry_auth_by_host() -> dict[str, str]:
    registry_path = Path(__file__).resolve().parents[1] / "source_registry.json"
    try:
        entries = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    auth_by_host: dict[str, str] = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        host = (urlparse(str(entry.get("url") or "")).hostname or "").lower()
        if host:
            auth_by_host[host] = str(entry.get("auth_type") or "unknown").lower()
    return auth_by_host


def _page_disallows_extraction(text: str) -> bool:
    head = text[:20_000].casefold()
    return bool(
        re.search(
            r"<meta[^>]+name=[\"']robots[\"'][^>]+content=[\"'][^\"']*(?:noindex|nosnippet|noarchive)",
            head,
        )
        or re.search(
            r"<meta[^>]+content=[\"'][^\"']*(?:noindex|nosnippet|noarchive)[^\"']*[\"'][^>]+name=[\"']robots[\"']",
            head,
        )
    )


def _extract_article_detail(text: str, *, source_url: str = "") -> dict:
    from .publication_time import texnet_publication_date
    from .publisher_content import verified_publisher_excerpt
    parser = _LinkParser()
    parser.feed(text)
    body_parser = _ArticleBodyParser(source_url=source_url)
    body_parser.feed(text)
    title = body_parser.headline or parser.title
    visible_text = clean_event_source_text(body_parser.text or parser.visible_text)
    method = body_parser.method or "document_fallback"
    chinese = ppi_chinese_article(visible_text, url=source_url, title=parser.title)
    excerpt = (sunsirs_body(visible_text, url=source_url, title=title)
               or texnet_body(visible_text, url=source_url, title=title))
    if chinese:
        visible_text, method, title = chinese["body"], "publisher_100ppi", chinese["title"]
    elif excerpt:
        visible_text, method = excerpt
    elif ((official := verified_publisher_excerpt(source_url, visible_text, title))
          and official["publisher_id"] == "ndrc"):
        # Preserve the already-supported complete short official announcement.
        # Its header is needed by the short-article gate, so retain the text.
        method = "publisher_ndrc"
    reason = body_defect(visible_text, method=method)
    # A full-page fallback has no trustworthy article boundaries. Keep it as
    # discovery text, not formal model input. Known publisher layouts and
    # semantic containers remain eligible subject to the other gates.
    if method == "document_fallback" and not reason:
        reason = "article_body_not_located"
    return {
        "title": title,
        "headline": chinese["title"] if chinese else body_parser.headline,
        "published_at": (
            (chinese["published_at"] if chinese else "") or body_parser.published_at
            or sunsirs_publication_date(clean_event_source_text(parser.visible_text), url=source_url, title=title)
            or _extract_structured_date(text) or _extract_release_date(parser.visible_text)
            or _extract_release_date(visible_text) or texnet_publication_date(source_url, parser.visible_text)
            or _extract_date(parser.title)
        ),
        "text": visible_text,
        "body_method": method,
        "body_truncated": False,
        "body_reason": reason,
    }


def _parse_html(text: str, source: NewsSource, *, base_url: str | None = None) -> list[RawNewsItem]:
    if _is_guarded_browser_check(text, urlparse(base_url or source.url).hostname or ""):
        raise RuntimeError(f"{source.source_id} guarded by browser check")
    if source.source_id == "opec_press" and (base_url or "").rstrip("/") == OPEC_HOME_URL.rstrip("/"):
        return _parse_opec_home_latest(text, source)
    if source.source_id in {"eia_press", "eia_today_in_energy"}:
        return _parse_eia_dated_listing(text, source, base_url=base_url or source.url)
    if source.source_id == "iea_news":
        return _parse_iea_news_html(text, source, base_url=base_url or source.url)
    parser = _LinkParser()
    parser.feed(text)
    items: list[RawNewsItem] = []
    relevant_links: list[RawNewsItem] = []
    for href, title in parser.links:
        url = urljoin(base_url or source.url, href)
        if _is_generic_navigation_link(title, url):
            continue
        if not _is_allowed_article_link(source, url):
            continue
        relevant = _is_relevant(title, "") or (
            source.source_id == "ccfa_industry_news"
            and any(term in title for term in ("聚酯", "涤纶", "化纤行业运行", "化纤每周市场观察"))
        )
        if relevant and urlparse(url).scheme in {"http", "https"}:
            relevant_links.append(
                RawNewsItem(
                    source_id=source.source_id,
                    tier=source.tier,
                    url=url,
                    title=title,
                    published_at=_extract_date(title) or _extract_date(url),
                    raw_text=title,
                )
            )
    items.extend(relevant_links)
    if source.source_id == "ccfa_industry_news":
        # URL archive month is discovery priority only, never a publication date.
        items.sort(key=lambda item: urlparse(item.url).path.split("/")[2], reverse=True)
    return _dedupe_items(items)


def _parse_iea_news_html(text: str, source: NewsSource, *, base_url: str) -> list[RawNewsItem]:
    parser = _IeaNewsListingParser(base_url=base_url)
    parser.feed(text)
    items = [
        replace(item, source_id=source.source_id, tier=source.tier)
        for item in parser.items
        if _is_allowed_article_link(source, item.url) and _is_relevant(item.title, item.raw_text)
    ]
    return _dedupe_items(items)


def _parse_eia_dated_listing(text: str, source: NewsSource, *, base_url: str) -> list[RawNewsItem]:
    parser = _EiaDatedListingParser(source=source, base_url=base_url)
    parser.feed(text)
    items = list(parser.items)
    if source.source_id == "eia_today_in_energy":
        cards = _EiaTodayListingParser(source=source, base_url=base_url)
        cards.feed(text)
        items.extend(cards.items)
    return _dedupe_items(items)


def _parse_opec_home_latest(text: str, source: NewsSource) -> list[RawNewsItem]:
    parser = _LinkParser()
    parser.feed(text)
    visible_text = _clean_text(parser.visible_text)
    section_matches = list(re.finditer(r"Press Releases\s+(.*?)\s+News & Articles", visible_text, flags=re.IGNORECASE))
    if not section_matches:
        return []
    date_pattern = rf"\d{{1,2}}\s+(?:{MONTH_PATTERN})\s+20\d{{2}}"
    section = max(
        (match.group(1) for match in section_matches),
        key=lambda value: (len(re.findall(date_pattern, value, flags=re.IGNORECASE)), len(value)),
    )
    items: list[RawNewsItem] = []
    pattern = (
        rf"(.+?)\s+({date_pattern})"
        r"\s+Read\s+more"
    )
    for match in re.finditer(pattern, section, flags=re.IGNORECASE):
        title = _clean_text(match.group(1))
        published_at = _extract_date(match.group(2))
        if not title or _is_generic_navigation_link(title, OPEC_HOME_URL):
            continue
        if _is_relevant(title, ""):
            item_hash = hashlib.sha1(f"{published_at}:{title}".encode()).hexdigest()[:12]
            items.append(
                RawNewsItem(
                    source_id=source.source_id,
                    tier=source.tier,
                    url=f"{source.url}#latest-{item_hash}",
                    title=title,
                    published_at=published_at,
                    raw_text=title,
                )
            )
    return _dedupe_items(items)


def _parse_feed(text: str, source: NewsSource, *, base_url: str | None = None) -> list[RawNewsItem]:
    if "Please limit requests" in text:
        raise RuntimeError("GDELT rate limited; retry after 5 seconds")
    root = ElementTree.fromstring(text)
    items: list[RawNewsItem] = []
    for node in root.findall(".//item"):
        title = _clean_text(node.findtext("title") or "")
        link = urljoin(base_url or source.url, _clean_text(node.findtext("link") or source.url))
        published = _clean_text(node.findtext("pubDate") or "")
        description = _clean_text(node.findtext("description") or "")
        if source.source_id == "eia_today_in_energy" and not _valid_eia_article_url(link):
            continue
        if title and _is_relevant_feed_item(source, title, description):
            items.append(
                RawNewsItem(
                    source_id=source.source_id,
                    tier=source.tier,
                    url=link,
                    title=title,
                    # GDELT's aggregator timestamp is not the original article
                    # publication time. Preserve it without promoting it.
                    published_at="" if source.source_id.startswith("gdelt_") else normalize_feed_publication(published),
                    discovery_timestamp=published if source.source_id.startswith("gdelt_") else "",
                    raw_text=description,
                )
            )
    if not items:
        for node in root.findall(".//{http://www.w3.org/2005/Atom}entry"):
            title = _clean_text(node.findtext("{http://www.w3.org/2005/Atom}title") or "")
            link_node = node.find("{http://www.w3.org/2005/Atom}link")
            link = (
                link_node.attrib.get("href", base_url or source.url)
                if link_node is not None
                else base_url or source.url
            )
            # Atom updated is a revision time, not the original publication.
            published = _clean_text(node.findtext("{http://www.w3.org/2005/Atom}published") or "")
            summary = _clean_text(node.findtext("{http://www.w3.org/2005/Atom}summary") or "")
            if title and _is_relevant_feed_item(source, title, summary):
                items.append(RawNewsItem(
                    source_id=source.source_id, tier=source.tier,
                    url=urljoin(base_url or source.url, link), title=title,
                    published_at=normalize_feed_publication(published), raw_text=summary,
                ))
    return _dedupe_items(items)


def _valid_eia_article_url(url: str) -> bool:
    parsed = urlparse(url)
    ids = parse_qs(parsed.query).get("id", [])
    return (parsed.hostname == "www.eia.gov" and parsed.path == "/todayinenergy/detail.php"
            and len(ids) == 1 and ids[0].isdigit())


def _is_relevant_feed_item(source: NewsSource, title: str, text: str) -> bool:
    if source.source_id == "google_news_chemical_rss" and not _is_chemical_chain_news(title, text):
        return False
    return _is_relevant(title, text)


def _is_chemical_chain_news(title: str, text: str) -> bool:
    haystack = f"{title} {text}".lower()
    if any(_term_in_text(term, haystack) for term in CHEMICAL_CHAIN_NOISE_TERMS):
        return False
    if _term_in_text("meg", haystack) and not any(
        _term_in_text(term, haystack)
        for term in ("monoethylene glycol", "ethylene glycol", "petrochemical", "feedstock", "meg prices", "meg market")
    ):
        return False
    if _term_in_text("px", haystack) and not any(
        _term_in_text(term, haystack)
        for term in ("paraxylene", "petrochemical", "feedstock", "px price", "px market", "pta", "polyester")
    ):
        return False
    if _term_in_text("polyester", haystack) and not any(
        _term_in_text(term, haystack)
        for term in ("filament", "yarn", "fiber", "fibre", "pta", "meg", "feedstock", "petrochemical", "market")
    ):
        return False
    if (_term_in_text("poy", haystack) or _term_in_text("dty", haystack)) and not any(
        _term_in_text(term, haystack)
        for term in (
            "polyester",
            "filament",
            "yarn",
            "petrochemical",
            "feedstock",
            "pta",
            "meg",
            "paraxylene",
            "chemical",
        )
    ):
        return False
    return any(_term_in_text(term, haystack) for term in CHEMICAL_CHAIN_CONTEXT_TERMS)


def _is_relevant(title: str, text: str) -> bool:
    if _is_low_signal_news_text(title, text):
        return False
    haystack = f"{title} {text}".lower()
    matched = [keyword for keywords in KEYWORDS.values() for keyword in keywords if _term_in_text(keyword, haystack)]
    if not matched:
        return False
    return not _only_broad_context_hits(haystack, matched)


def _is_low_signal_news_text(title: str, text: str) -> bool:
    normalized_title = _clean_text(title).strip().lower()
    if normalized_title in LOW_SIGNAL_NEWS_TITLES:
        return True
    if any(re.search(pattern, normalized_title, flags=re.IGNORECASE) for pattern in LOW_SIGNAL_NEWS_TITLE_PATTERNS):
        return True
    return normalized_title.endswith(" all rights reserved") and "qatar petroleum" in f"{title} {text}".lower()


def _only_broad_context_hits(text: str, matched_keywords: list[str]) -> bool:
    if not matched_keywords:
        return False
    normalized = {keyword.lower() for keyword in matched_keywords}
    if not normalized <= BROAD_CONTEXT_KEYWORDS:
        return False
    return not any(_term_in_text(term, text) for term in RELEVANCE_CONTEXT_TERMS)


def _has_sanctions_energy_context(text: str, affected_products: list[str]) -> bool:
    return (
        bool(affected_products)
        or any(_term_in_text(term, text) for term in SANCTIONS_DIRECT_CONTEXT_TERMS)
        or any(_term_in_text(term, text) for term in SANCTIONS_ENERGY_CONTEXT_TERMS)
    )


def _term_in_text(term: str, text: str) -> bool:
    normalized = term.lower().strip()
    if not normalized:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9\s.'/-]*[a-z0-9]", normalized):
        pattern = r"(?<![a-z0-9])" + r"\s+".join(re.escape(part) for part in normalized.split()) + r"(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return normalized in text


def _is_generic_navigation_link(title: str, url: str) -> bool:
    normalized_title = _clean_text(title).strip().lower()
    parsed = urlparse(url)
    normalized_path = parsed.path.rstrip("/")
    if normalized_title in GENERIC_LINK_TITLES:
        return True
    title_parts = [part.strip() for part in normalized_title.split(" - ") if part.strip()]
    if title_parts and all(part in GENERIC_LINK_TITLES for part in title_parts):
        return True
    if normalized_title.startswith("about "):
        return True
    if normalized_path.startswith("/about"):
        return True
    return normalized_path in {"", "/", "/home"} and len(normalized_title.split()) <= 6


def _is_allowed_article_link(source: NewsSource, url: str) -> bool:
    path = urlparse(url).path.rstrip("/")
    if source.source_id == "ccfa_industry_news":
        return (
            urlparse(url).hostname == "www.ccfa.com.cn"
            and bool(re.fullmatch(r"/\d+/20\d{4}/\d+\.html", path))
        )
    if source.source_id == "texnet_polyester_news":
        return urlparse(url).hostname == "info.texnet.com.cn" and bool(re.fullmatch(r"/detail-\d+\.html", path))
    if source.source_id == "ppi_commodity_news":
        return urlparse(url).hostname == "www.100ppi.com" and bool(
            re.fullmatch(r"/news/detail-20\d{6}-\d+\.html", path)
        )
    if source.source_id == "cnpc_news":
        return bool(re.fullmatch(r"/system/20\d{2}/\d{2}/\d{2}/\d+\.shtml", path))
    if source.source_id == "oilprice_world_news":
        # robots.txt disallows a handful of specific promotional article URLs
        # for all agents; never follow them even if a listing surfaces one.
        if path in OILPRICE_ROBOTS_DISALLOWED_PATHS:
            return False
        return urlparse(url).hostname == "oilprice.com" and bool(
            re.fullmatch(r"/(?:Latest-Energy-News/[A-Za-z-]+|Energy/[A-Za-z-]+)/[A-Za-z0-9-]+\.html", path)
        )
    if source.source_id == "us_coast_guard_news":
        return bool(re.search(r"/Article/\d+/", path + "/", re.I))
    if source.source_id == "ofac_recent_actions":
        return bool(re.match(r"^/recent-actions/20\d{6}$", path))
    if source.source_id == "treasury_press":
        return path.startswith("/news/press-releases/")
    if source.source_id == "eia_wpsr":
        return path == "/petroleum/supply/weekly" or path.startswith("/petroleum/supply/weekly/")
    if source.source_id == "eia_press":
        return path.startswith("/pressroom/") and path != "/pressroom"
    if source.source_id == "eia_today_in_energy":
        return path.startswith("/todayinenergy/")
    if source.source_id == "iea_news":
        return path.startswith("/news/") and path != "/news"
    if source.source_id == "nato_press_releases":
        return bool(re.search(r"/cps/[a-z]{2}/natohq/(?:news|press_releases|opinions|official_texts)_\d+\.htm$", path))
    if source.source_id == "imo_press_briefings":
        return (
            path.startswith("/en/MediaCentre/PressBriefings/Pages/")
            and path != "/en/MediaCentre/PressBriefings/Pages/default.aspx"
        )
    if source.source_id in {"white_house_news", "white_house_statements"}:
        blocked_paths = {
            "/news",
            "/briefings-statements",
            "/presidential-actions",
            "/presidential-actions/executive-orders",
            "/presidential-actions/proclamations",
            "/presidential-actions/memoranda",
            "/presidential-actions/presidential-memoranda",
            "/fact-sheets",
            "/articles",
        }
        normalized = path.lower()
        if normalized in blocked_paths:
            return False
        return normalized.startswith(("/briefings-statements/", "/presidential-actions/", "/fact-sheets/"))
    return True


def _is_guarded_browser_check(text: str, host: str) -> bool:
    lower = text[:5000].lower()
    return (
        "browser check" in lower
        or "checking your browser" in lower
        or "cf-mitigated" in lower
        or (host == "www.consilium.europa.eu" and "enable javascript and cookies to continue" in lower)
    )


def _extract_release_date(value: str) -> str:
    publication = re.search(r"(?:发布时间|发布日期)\s*[:：]\s*(20\d{2}[-/.]\d{1,2}[-/.]\d{1,2})", value)
    if publication:
        normalized = _extract_date(publication.group(1))
        try:
            date.fromisoformat(normalized)
            return normalized
        except ValueError:
            pass
    release_patterns = (
        r"\bRelease Date\s+(\d{1,2})/(\d{1,2})/(20\d{2})\b",
        r"\bReleased on:\s*(\d{1,2})/(\d{1,2})/(20\d{2})\b",
    )
    for pattern in release_patterns:
        match = re.search(pattern, value, flags=re.IGNORECASE)
        if match:
            month, day, year = match.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2}),\s+(20\d{2})\s+-\s+(?:Sanctions List Updates|Press Release|Statement)\b",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        return _extract_date(match.group(0))
    return ""


def _extract_structured_date(html: str) -> str:
    preferred_patterns = (
        r"field--name-field-news-publication-date[\s\S]{0,500}?datetime=[\"']([^\"']+)[\"']",
        r"<meta[^>]+(?:property|name)=[\"'](?:article:published_time|publish-date|date|dc\.date|dcterms\.date)[\"'][^>]+content=[\"']([^\"']+)[\"']",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"'](?:article:published_time|publish-date|date|dc\.date|dcterms\.date)[\"']",
    )
    preferred_patterns += (r"<time[^>]+datetime=[\"']([^\"']+)[\"']",)
    preferred_patterns += (
        r'<script[^>]+type=[\"\']application/ld\+json[\"\'][^>]*>'
        r'(?:(?!</script>)[\s\S])*?"datePublished"\s*:\s*"([^\"]+)"',
    )
    for pattern in preferred_patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            raw = match.group(1).strip()
            try:
                instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if instant.tzinfo is not None:
                    return instant.isoformat()
            except ValueError:
                pass
            date = _extract_date(raw)
            if date:
                return date
    return ""


def _extract_date(value: str) -> str:
    match = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})(?:\b|T)", value)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    match = re.search(r"\b(20\d{2})(\d{2})(\d{2})\b", value)
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}"
    match = re.search(
        rf"\b({MONTH_PATTERN})\s+(\d{{1,2}}),?\s+(20\d{{2}})\b",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        month_name, day, year = match.groups()
        month = MONTH_NUMBERS[month_name.lower()]
        return f"{year}-{month:02d}-{int(day):02d}"
    match = re.search(
        rf"\b(\d{{1,2}})\s+({MONTH_PATTERN})\s+(20\d{{2}})\b",
        value,
        flags=re.IGNORECASE,
    )
    if match:
        day, month_name, year = match.groups()
        month = MONTH_NUMBERS[month_name.lower()]
        return f"{year}-{month:02d}-{int(day):02d}"
    return ""


def _direction_for_text(text: str, category: str) -> str:
    bearish_hits = sum(1 for term in BEARISH_TERMS if _term_in_text(term, text))
    bullish_hits = sum(1 for term in BULLISH_TERMS if _term_in_text(term, text))
    if bearish_hits and bearish_hits >= bullish_hits:
        return "利空"
    if bullish_hits:
        return "利多"
    return "中性"


def _impact_strength(text: str, category: str, products: list[str]) -> float:
    base = 0.45
    if category in {"sanctions_geopolitics", "shipping_security"}:
        base = 0.68
    if category == "oil_policy":
        base = 0.58
    if any(_term_in_text(term, text) for term in (*BULLISH_TERMS, *BEARISH_TERMS)):
        base += 0.18
    if {"POY", "DTY", "PTA", "PX"} & set(products):
        base += 0.08
    return round(min(base, 0.95), 2)


def _error_label(exc: BaseException) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.headers.get("cf-mitigated") == "challenge":
            return f"HTTPStatusError:{exc.response.status_code}:browser_check"
        return f"HTTPStatusError:{exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "TimeoutException"
    if isinstance(exc, httpx.TransportError):
        return exc.__class__.__name__
    if isinstance(exc, (RuntimeError, ValueError)) and str(exc):
        return f"{exc.__class__.__name__}:{str(exc)[:180]}"
    return exc.__class__.__name__


def _summary(item: RawNewsItem, category: str, products: list[str], direction: str) -> str:
    product_text = ", ".join(products) if products else "upstream cost chain"
    source_text = item.raw_text or item.title
    return (
        f"{item.title}。分类 {category}，初步方向 {direction}，影响对象 {product_text}。原文线索：{source_text[:240]}"
    )


def _dedupe_items(items: list[RawNewsItem]) -> list[RawNewsItem]:
    seen: set[str] = set()
    unique: list[RawNewsItem] = []
    for item in items:
        parsed = urlparse(item.url)
        key = (
            item.url
            if parsed.fragment.startswith("latest-")
            else _canonical_url(item.url) or _clean_text(item.title).lower()
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _cluster_id(
    category: str,
    title: str,
    *,
    published_at: str = "",
    affected_products: list[str] | None = None,
    matched_keywords: list[str] | None = None,
) -> str:
    date = _date_only(published_at)
    normalized_title = _normalize_cluster_title(title)
    terms = _cluster_terms(
        normalized_title,
        affected_products=affected_products or [],
        matched_keywords=matched_keywords or [],
    )
    if date and terms:
        return _id(f"evt_{category}", f"{date}|{category}|{','.join(terms[:8])}")
    words = re.findall(r"[\w\u4e00-\u9fff]+", normalized_title.lower())[:12]
    return _id(f"evt_{category}", " ".join(words))


def _normalize_cluster_title(title: str) -> str:
    normalized = re.sub(
        r"\s+\|\s+(?:Office of Foreign Assets Control|U\.S\. Department of the Treasury|The White House).*$",
        "",
        title,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(r"\s+Lock$", "", normalized, flags=re.IGNORECASE)
    return _clean_text(normalized)


def _cluster_terms(title: str, *, affected_products: list[str], matched_keywords: list[str]) -> list[str]:
    haystack = title.lower()
    candidates = [
        "opec",
        "ofac",
        "iran",
        "russia",
        "hormuz",
        "red sea",
        "eia",
        "inventory",
        "refinery",
        "fed",
        "dollar",
        "px",
        "pta",
        "meg",
        "poy",
        "dty",
        *[item.lower() for item in affected_products],
        *[item.lower() for item in matched_keywords],
    ]
    seen: set[str] = set()
    terms: list[str] = []
    for term in candidates:
        if (term and _term_in_text(term, haystack)) or term in {
            item.lower() for item in affected_products + matched_keywords
        }:
            normalized = term.strip().lower()
            if normalized and normalized not in seen:
                seen.add(normalized)
                terms.append(normalized)
    return terms


def _id(prefix: str, value: str) -> str:
    return f"{prefix}_{_hash(value)[:16]}"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname in {"www.eia.gov", "eia.gov"} and parsed.path == "/todayinenergy/detail.php":
        article_ids = parse_qs(parsed.query).get("id", [])
        if len(article_ids) == 1 and article_ids[0].isdigit():
            return parsed._replace(fragment="", query=urlencode({"id": article_ids[0]})).geturl()
    return parsed._replace(fragment="", query="").geturl()


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())

"""Source-bound business evidence, independent of model skill or promotion.

A mechanism hypothesis is an explanation, not an observed price effect. Exact
quotes establish provenance only. Unsupported semantics stay review candidates.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta

from .event_fact_semantics import qualification, source_context
from .event_summary_quality import build_grounded_event_summary, clean_event_source_text
from .industrial_intelligence.identity import detrack_url
from .models import EventFactExtraction
from .prediction_evidence_catalog import _products
from .prediction_evidence_runtime import publication_time, seal, verify
from .prediction_inputs import digest, safe_url
from .prediction_replay import SHANGHAI, TargetContract, load_export, outcome_for, timestamp
from .seven_product_forecast import FRESHNESS_DAYS

LEGACY_POLICY = "business-evidence-dossier.v1"
SOURCE_DATE_POLICY = "business-evidence-dossier.v2-source-dates"
SOURCE_ANCHOR_POLICY = "business-evidence-dossier.v3-source-anchors"
MECHANISM_POLICY = "business-evidence-dossier.v4-mechanism-checks"
POLICY = "business-evidence-dossier.v5-sentence-boundaries"
PRODUCT_LABELS = {
    "crude": "原油",
    "naphtha": "石脑油",
    "px": "PX",
    "pta": "PTA",
    "meg": "MEG",
    "poy": "POY",
    "dty": "DTY",
}
MECHANISMS = {
    "supply": {
        "label": "供应与装置",
        "cues": r"供应|产量|开工|停产|停车|重启|检修|复产|投产|装置",
        "automatic_scope": "明确主体、日期和已发生装置动作",
    },
    "inventory": {
        "label": "库存",
        "cues": r"库存|去库|累库|inventor",
        "automatic_scope": "美国API/EIA原油库存；其他库存需复核",
    },
    "demand": {
        "label": "需求与订单",
        "cues": r"需求|订单|消费|采购|demand|consumption",
        "automatic_scope": "明确报告期的需求数量变化；其他表述需复核",
    },
    "logistics": {
        "label": "航运与物流",
        "cues": r"航运|运费|运输|港口|封锁|shipping|freight|port",
        "automatic_scope": "召回材料，传导方向需复核",
    },
    "price": {
        "label": "价格与成本",
        "cues": r"价格|报价|结算价|成本|加工差|price|cost",
        "automatic_scope": "价格原文背景；与主标签序列分别核验",
    },
    "policy": {
        "label": "政策与贸易",
        "cues": r"政策|制裁|关税|配额|出口|进口|sanction|tariff",
        "automatic_scope": "召回材料，适用范围及方向需复核",
    },
}
ACTION = re.compile(r"停产|停车|复产|重启|投产|恢复生产|减产|增产|减负|降负|提负")
DATE = re.compile(r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日|(\d{4})-(\d{2})-(\d{2})")
MONTHS = {
    name: index
    for index, name in enumerate(
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
MONTH_PATTERN = "|".join(MONTHS)
ENGLISH_DATE = re.compile(
    rf"\b(?:(?P<month>{MONTH_PATTERN})\s+(?P<day>\d{{1,2}})(?:st|nd|rd|th)?|"
    rf"(?P<reverse_day>\d{{1,2}})(?:st|nd|rd|th)?\s+(?P<reverse_month>{MONTH_PATTERN}))"
    r"(?:,?\s+(?P<year>\d{4}))?\b",
    re.I,
)
SOURCE_ANCHOR_MECHANISMS = {
    **MECHANISMS,
    "inventory": {**MECHANISMS["inventory"], "cues": r"库存|去库|累库|\binventor\w*\b"},
    "demand": {**MECHANISMS["demand"], "cues": r"需求|订单|消费|采购|\b(?:demand|consumption)\b"},
    "logistics": {
        **MECHANISMS["logistics"],
        "cues": r"航运|运费|运输|港口|封锁|管道|海峡|\b(?:shipping|freight|ports?|pipeline|strait|blockade)\b",
    },
    "price": {**MECHANISMS["price"], "cues": r"价格|报价|结算价|成本|加工差|\b(?:prices?|costs?)\b"},
    "policy": {
        **MECHANISMS["policy"],
        "cues": r"政策|制裁|关税|配额|出口|进口|\b(?:sanctions?|tariffs?|exports?|imports?)\b",
    },
}
STATES = {
    "reported": "actual",
    "planned": "planned",
    "unconfirmed": "unconfirmed",
    "denied": "denied",
    "in_progress": "in_progress",
}


def _date(text: str, published, *, source_anchors: bool = False) -> str | None:
    found = set()
    for m in DATE.finditer(text):
        try:
            day = date(int(m[4] or m[1] or published.year), int(m[5] or m[2]), int(m[6] or m[3]))
        except ValueError:
            return None
        if not 0 <= (published.date() - day).days <= 90:
            return None
        found.add(day.isoformat())
    if source_anchors:
        for match in ENGLISH_DATE.finditer(text):
            # A report range is not a unique event day, even if its second
            # endpoint omits the repeated month (September 10-12).
            if re.match(r"\s*(?:[-–—]\s*\d{1,2}\b|(?:to|through|until)\b)", text[match.end() :], re.I):
                return None
            month = MONTHS[(match["month"] or match["reverse_month"]).lower()]
            year = int(match["year"] or published.year)
            try:
                day = date(year, month, int(match["day"] or match["reverse_day"]))
                # A stated month/day near a year boundary can refer to the prior
                # year. Publication anchors the year, never supplies a missing day.
                if not match["year"] and day > published.date():
                    day = day.replace(year=year - 1)
            except ValueError:
                return None
            if not 0 <= (published.date() - day).days <= 90:
                return None
            found.add(day.isoformat())
    return next(iter(found)) if len(found) == 1 else None


def _claim_subject(facts: EventFactExtraction, quote: str, sentence: str) -> tuple[str, bool]:
    """Bind each claim to a literal source span; do not guess translated aliases.

    For English, a constrained leading grammatical subject is an observable
    span, not a translated entity identity or a verified price mechanism.
    Statements without such a span stay unbound. Chinese keeps exact matching.
    """
    if facts.source_language.lower().startswith("en"):
        text = quote.strip()
        # Skip only a literal leading absolute date (not attribution/opinion).
        text = re.sub(rf"^(?:On\s+)?(?:{MONTH_PATTERN})\s+\d{{1,2}}(?:,?\s+\d{{4}})?[, ]+", "", text, flags=re.I)
        match = re.match(
            r"(?P<subject>(?:The |the )?[A-Z][A-Za-z0-9’'&(). -]{1,100}?)\s+"
            r"(?:has|have|had|is|are|was|were|halted|restored|shut|closed|reopened|announced|reported|said|imports|exports|rose|fell)\b",
            text,
        )
        if match:
            subject = match["subject"].strip()
            if subject.lower() not in {"it", "they", "he", "she", "this", "that"} and subject in quote:
                return subject, True
        # An English display subject is usable only when verbatim in this claim.
        if not re.search(r"[\u4e00-\u9fff]", facts.subject) and len(facts.subject) >= 2 and facts.subject in quote:
            return facts.subject, True
        return "本句主体待核验", False
    return facts.subject, len(facts.subject) >= 2 and (facts.subject in quote or facts.subject in sentence)


def _sentence_span(raw: str, quote: str, limit: int = 600, *, english_boundaries: bool = False) -> str:
    """Expand an exact quote to its enclosing sentence inside the raw body.

    Dates and normalized subjects usually sit beside the quoted fact, not
    inside the quoted span. The sentence - still a verbatim raw slice, never a
    different paragraph - may complete the subject/date binding; the quote
    stays the provenance anchor (claim identity and raw_start/raw_end are
    computed from the quote and are unchanged).
    """
    index = raw.find(quote)
    if index < 0:
        return quote
    start = max(raw.rfind(mark, 0, index) for mark in "。！？!?；;\n") + 1
    end = len(raw)
    for mark in "。！？!?；;\n":
        found = raw.find(mark, index + len(quote))
        if found != -1:
            end = min(end, found + 1)
    if english_boundaries:
        # Sentence stops require whitespace plus a capitalized next sentence;
        # decimal quantities and U.S./E.I.A. abbreviation periods stay intact.
        for boundary in re.finditer(r"(?<!\b[A-Z])[.!?]\s+(?=[A-Z])", raw):
            if boundary.end() <= index:
                start = max(start, boundary.end())
            elif boundary.start() >= index + len(quote) - 1:
                end = min(end, boundary.start() + 1)
                break
    sentence = raw[start:end]
    if len(sentence) > limit:
        # Keep the quote centred; a runaway paragraph must not widen the net.
        head = max(0, index - start - (limit - len(quote)) // 2)
        sentence = sentence[head : head + limit]
    return sentence


def _state(text: str) -> str:
    # Negation/epistemic status wins over a bare action word.
    if re.search(r"并未|未曾|尚未|未发生|未停车|未停产|未投产|并非|没有|不存在", text):
        return "denied"
    if re.search(r"可能|或将|或于|考虑|假如|如果|若|预计|预期|计划|拟于|拟将|拟定|有望", text):
        return "planned"
    if re.search(r"\b(?:plans?|planning|expects?|expected|forecasts?|may|might|would|will)\b", text, re.I):
        return "planned"
    return STATES[qualification(text)]


def _direction(
    quote: str,
    context: str,
    facts: EventFactExtraction,
    mechanism: str,
    day: str | None,
    sentence: str = "",
    *,
    source_anchors: bool = False,
    subject_bound: bool | None = None,
) -> tuple[str | None, list[str]]:
    """Only an explicit, dated, source-bound statement gets a mechanism inference."""
    gaps = []
    if not day:
        gaps.append("缺少明确且唯一的发生日期或报告期")
    # Do not borrow a subject from a headline or a different paragraph.
    bound = (
        subject_bound
        if subject_bound is not None
        else (len(facts.subject) >= 2 and (facts.subject in quote or facts.subject in sentence))
    )
    if not bound:
        gaps.append("系统尚未建立本句主体原文锚点，需核验" if source_anchors else "主体未与本句原文绑定")
    if _state(context) != "actual":
        gaps.append("非已发生事实，保留其限定状态")
    if gaps:
        return None, gaps
    if source_anchors and facts.source_language.lower().startswith("en") and mechanism in {"supply", "demand"}:
        # A raw English subject span establishes attribution only. Mixed-language
        # quotes must not acquire directional votes through the Chinese regexes.
        # Preserve the original v1/v2 reconstruction behavior explicitly.
        return None, [f"系统尚未实现英文{MECHANISMS[mechanism]['label']}方向核验，保留待核验"]
    if mechanism == "supply":
        actions = set(ACTION.findall(quote))
        if len(actions) != 1 or not re.search(r"装置|工厂|生产线|炼厂|油田", quote):
            return None, ["装置主体或动作不唯一"]
        action = next(iter(actions))
        # Publication of a plan, static capacity and completion estimates are not execution.
        if not re.search(r"(?:已|于|当日|当天|完成|正式)[^。；;]{0,35}" + re.escape(action), quote):
            return None, ["未确认装置动作已经发生"]
        return ("up" if action in {"停产", "停车", "减产", "减负", "降负"} else "down"), []
    if mechanism == "demand":
        # Industry opinion, an order intention or quoted price is not realized demand.
        matches = list(
            re.finditer(r"(?:消费量|需求量|订单量)(?:同比|环比)(增加|增长|上升|减少|下降|下滑)(\d+(?:\.\d+)?)%", quote)
        )
        if len(matches) == 1 and float(matches[0][2]) > 0:
            return ("up" if matches[0][1] in {"增加", "增长", "上升"} else "down"), []
        return None, ["缺少已发生的需求数量变化"]
    if source_anchors:
        if mechanism == "price":
            return None, ["价格背景，不作为独立事件方向证据"]
        return None, [f"系统尚未实现{MECHANISMS[mechanism]['label']}方向核验，保留待核验"]
    return None, ["该机制需要语义复核，未自动判断方向"]


def _article_claims(
    payload: dict, cutoff, *, source_dates: bool = True, source_anchors: bool = False, mechanism_checks: bool = False,
    english_boundaries: bool = False,
) -> tuple[list[dict], list[str]]:
    a, s = payload["article"], payload["summary"]
    raw = a["raw_text"]
    safe_url(a["canonical_url"])
    published = publication_time(a)
    clocks = [timestamp(a[k]) for k in ("created_at", "first_seen_at")]
    clocks.extend(timestamp(s[k]) for k in ("generated_at", "updated_at"))
    if payload.get("content_visible_at"):
        clocks.append(timestamp(payload["content_visible_at"]))
    if max([published, *clocks]) > cutoff:
        return [], ["after_cutoff"]
    expected = hashlib.sha256((a["title"] + "\n" + raw).encode()).hexdigest()
    if expected != a["content_hash"] or s["source_hash"] != expected:
        return [], ["body_summary_version_mismatch"]
    if a["tier"] not in {"A", "B"} or s["input_quality"] != "full_text" or s["fact_summary_status"] != "completed":
        return [], ["source_or_fact_not_eligible"]
    extracted = s["fact_payload"]
    if isinstance(extracted, str):
        extracted = json.loads(extracted)
    facts = EventFactExtraction.model_validate(extracted)
    checked = build_grounded_event_summary(
        source_text=f"{clean_event_source_text(raw)}\n发布时间：{a['published_at']}",
        input_quality="full_text",
        fact_output=extracted,
        impact_output={},
    )
    quotes = list(dict.fromkeys([*facts.evidence_quotes, *(n.evidence_quote for n in facts.numbers)]))
    claims, gaps = [], []
    for quote in quotes:
        if quote not in raw:
            gaps.append("quote_not_exact_raw_span")
            continue
        if len(quote) > 1600:
            gaps.append("quote_requires_segmentation")
            continue
        products, flags = _products(quote)
        if source_dates:
            # Material identity may be qualified outside the isolated numeric quote.
            from .prediction_evidence_catalog import NYLON, POLYESTER

            if NYLON.search(a["title"]) and not POLYESTER.search(a["title"]):
                products = [p for p in products if p not in {"poy", "dty"}]
        if not products:
            continue
        sentence = _sentence_span(raw, quote, english_boundaries=english_boundaries)
        context = sentence if mechanism_checks else source_context(raw, quote)
        state = _state(context)
        published_sh = published.astimezone(SHANGHAI)
        # Quote-internal date wins; otherwise the enclosing sentence may supply
        # the one unambiguous event date (never relative expressions).
        raw_day = _date(quote, published_sh, source_anchors=source_anchors) or _date(
            sentence, published_sh, source_anchors=source_anchors
        )
        subject, subject_bound = _claim_subject(facts, quote, sentence) if source_anchors else (facts.subject, None)
        for key, definition in (SOURCE_ANCHOR_MECHANISMS if source_anchors else MECHANISMS).items():
            if not re.search(definition["cues"], quote, re.I):
                continue
            day = raw_day
            day_source = "raw_sentence" if day else None
            # A price-report title dates only the price claim, never another
            # mechanism in the same quote (e.g. an undated plant shutdown).
            if (
                source_dates
                and key == "price"
                and not day
                and not DATE.search(sentence)
                and not (source_anchors and ENGLISH_DATE.search(sentence))
                and re.search(r"报价动态|价格动态", a["title"])
            ):
                day = _date(a["title"], published_sh, source_anchors=source_anchors)
                day_source = "dated_price_report_title" if day else None
            inferred, reasons = _direction(
                quote, context, facts, key, day, sentence, source_anchors=source_anchors, subject_bound=subject_bound
            )
            if mechanism_checks:
                from .evidence_mechanism_checks import check_mechanism

                reviewed = check_mechanism(quote, sentence, key, day)
                if reviewed is not None:
                    inferred, reasons, subject = reviewed
            reasons = [*flags, *reasons]
            if checked.fact_summary_status != "completed":
                reasons.append("摘要事实质量门禁未通过")
                inferred = None
            # Multi-product sentences do not prove which installation/quantity changed.
            if len(products) != 1:
                reasons.append("同句涉及多个品种，需核对动作归属")
                inferred = None
            if flags:
                inferred = None
            for product in products:
                claim_id = digest(
                    {
                        "url": detrack_url(a["canonical_url"]),
                        "hash": expected,
                        "quote": quote,
                        "product": product,
                        "mechanism": key,
                    }
                )
                claims.append(
                    {
                        "claim_id": claim_id,
                        "target": product,
                        "mechanism": key,
                        "subject": subject,
                        "event_date": day,
                        **({"event_date_source": day_source} if source_dates else {}),
                        "state": state if inferred or state != "actual" else "unknown",
                        "expected_direction": inferred,
                        "semantic_status": "rule_checked" if inferred else "needs_review",
                        "quote": quote,
                        "raw_start": raw.find(quote),
                        "raw_end": raw.find(quote) + len(quote),
                        "content_hash": expected,
                        "raw_text_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                        "source_url": a["canonical_url"],
                        "source_title": a["title"],
                        "source_tier": a["tier"],
                        "article_ids": [a["article_id"]],
                        "published_at": published.isoformat(),
                        "known_at": cutoff.isoformat(),
                        "source_available_at": max([published, *clocks]).isoformat(),
                        "conditions": {
                            "subject": subject,
                            "location": (
                                facts.location
                                if facts.location and (facts.location in quote or facts.location in sentence)
                                else ""
                            ),
                        },
                        "gaps": list(dict.fromkeys(reasons)),
                        "inference_boundary": "机制推断，其他条件相同；不等于价格必然变化或预测正确率",
                        "source_status": "reported_not_independently_confirmed",
                        "episode_key": digest(
                            {
                                "subject": subject,
                                "target": product,
                                "mechanism": key,
                                "day": day,
                                "quote": quote if not day else None,
                            }
                        ),
                    }
                )
    return claims, gaps


def _inventory_claim(fact: dict, article: dict) -> dict:
    return {
        "claim_id": fact["fact_id"],
        "target": "crude",
        "mechanism": "inventory",
        "subject": fact["conditions"]["origin"] + "美国原油库存",
        "event_date": fact["period_end"],
        "state": "actual",
        "expected_direction": fact["expected_direction"],
        "semantic_status": "rule_checked",
        "quote": fact["quote"],
        "raw_start": fact["raw_start"],
        "raw_end": fact["raw_end"],
        "content_hash": fact["content_hash"],
        "raw_text_sha256": fact["raw_text_sha256"],
        "source_url": fact["canonical_url"],
        "source_title": article["title"],
        "source_tier": article["tier"],
        "article_ids": fact["source_articles"],
        "published_at": fact["published_at"],
        "known_at": fact["known_at"],
        "source_available_at": fact["known_at"],
        "conditions": dict(fact["conditions"]),
        "gaps": [],
        "inference_boundary": "库存变化的条件性价格机制；API/EIA同一周不增加独立票数",
        "source_status": fact["source_status"],
        "episode_key": digest(
            {
                "target": "crude",
                "mechanism": "inventory",
                "region": fact["conditions"]["region"],
                "day": fact["period_end"],
            }
        ),
    }


def _history(current: list[dict], claims: list[dict], series, cutoff, horizon: int) -> list[dict]:
    current_episodes = {c["episode_key"] for c in current}
    # Selection by pre-outcome conditions; NEVER rank by subsequent price direction.
    selected = [
        r
        for r in claims
        if r["semantic_status"] == "rule_checked"
        and current
        and timestamp(r["published_at"]) < min(timestamp(c["published_at"]) for c in current)
        and r["episode_key"] not in current_episodes
        and any(
            r["mechanism"] == c["mechanism"]
            and r["expected_direction"] == c["expected_direction"]
            and r["conditions"].get("subject", r["conditions"].get("origin"))
            == c["conditions"].get("subject", c["conditions"].get("origin"))
            and r["conditions"].get("location", r["conditions"].get("region"))
            == c["conditions"].get("location", c["conditions"].get("region"))
            for c in current
        )
    ]
    results, seen, last_settled = [], set(), None
    for claim in sorted(selected, key=lambda r: (r["published_at"], r["claim_id"])):
        if claim["episode_key"] in seen:
            continue
        seen.add(claim["episode_key"])
        issue = timestamp(claim["published_at"])
        view = series.view(issue)
        outcome = {"state": "historical_base_unavailable"}
        if view.points and not view.latest_blocked:
            if (
                issue.astimezone(SHANGHAI).date() - date.fromisoformat(view.points[-1].observed_at[:10])
            ).days > FRESHNESS_DAYS[claim["target"]]:
                outcome = {"state": "historical_base_stale"}
            else:
                outcome = outcome_for(
                    series,
                    issue=issue,
                    cutoff=cutoff,
                    contract=TargetContract("calendar", horizon),
                    base=view.points[-1],
                )
        if outcome["state"] == "scored" and last_settled and issue <= last_settled:
            outcome = {"state": "overlapping_outcome_excluded"}
        if outcome["state"] == "scored":
            last_settled = timestamp(outcome["settled_at"])
        relation = "unresolved"
        if outcome["state"] == "scored":
            relation = (
                "neutral"
                if outcome["direction"] == "neutral"
                else "support"
                if outcome["direction"] == claim["expected_direction"]
                else "counter"
            )
        results.append(
            {
                "claim_id": claim["claim_id"],
                "relation": relation,
                "outcome": outcome,
                "use": "retrospective_context_not_historical_forecast_input",
                "causality_proven": False,
            }
        )
    return results


def build_dossier(receipt: dict, prices: dict, inventory_facts: dict, *, policy: str = POLICY) -> dict:
    if policy not in {POLICY, MECHANISM_POLICY, SOURCE_ANCHOR_POLICY, SOURCE_DATE_POLICY, LEGACY_POLICY}:
        raise ValueError("unknown_dossier_policy")
    source_anchors = policy in {POLICY, MECHANISM_POLICY, SOURCE_ANCHOR_POLICY}
    verify(receipt)
    verify(inventory_facts)
    cutoff = timestamp(receipt["as_of_time"])
    if prices["as_of_time"] != receipt["as_of_time"]:
        raise ValueError("dossier_cutoff_mismatch")
    claims, gaps = [], Counter(receipt.get("excluded", {}))
    for row in receipt["rows"]:
        if digest(row["payload"]) != row["payload_sha256"]:
            raise ValueError("dossier_raw_integrity_failed")
        try:
            found, reasons = _article_claims(
                row["payload"],
                cutoff,
                source_dates=policy != LEGACY_POLICY,
                source_anchors=source_anchors,
                mechanism_checks=policy in {POLICY, MECHANISM_POLICY},
                english_boundaries=policy == POLICY,
            )
            claims.extend(found)
            gaps.update(reasons)
        except (ValueError, TypeError, KeyError, AttributeError):
            gaps["malformed_source_excluded"] += 1
    articles = {r["payload"]["article"]["article_id"]: r["payload"]["article"] for r in receipt["rows"]}
    grounded_inventory = [_inventory_claim(r, articles[r["article_id"]]) for r in inventory_facts["facts"]]
    inventory_quotes = {(c["content_hash"], c["quote"]) for c in grounded_inventory}
    claims = [
        c for c in claims if not (c["mechanism"] == "inventory" and (c["content_hash"], c["quote"]) in inventory_quotes)
    ] + grounded_inventory
    # Merge exact same-source copies; unresolved cross-source independence is not assumed.
    unique = {}
    for claim in claims:
        key = (detrack_url(claim["source_url"]), claim["quote"], claim["target"], claim["mechanism"])
        if key in unique:
            unique[key]["article_ids"] = sorted(set(unique[key]["article_ids"] + claim["article_ids"]))
        else:
            unique[key] = claim
    claims = sorted(unique.values(), key=lambda c: c["claim_id"])
    series = load_export(prices)
    cells = {}
    for target in PRODUCT_LABELS:
        product_claims = [c for c in claims if c["target"] == target]
        recent = [c for c in product_claims if cutoff - timedelta(days=7) <= timestamp(c["published_at"]) <= cutoff]
        actual = [
            c
            for c in recent
            if c["semantic_status"] == "rule_checked"
            and (
                c["mechanism"] == "inventory"
                or c["event_date"]
                and 0 <= (cutoff.astimezone(SHANGHAI).date() - date.fromisoformat(c["event_date"])).days <= 7
            )
        ]
        episodes = {}
        for c in actual:
            episodes.setdefault(c["episode_key"], []).append(c)
        mixed = [rows for rows in episodes.values() if len({r["expected_direction"] for r in rows}) > 1]
        mixed_ids = {c["claim_id"] for rows in mixed for c in rows}
        support = [c["claim_id"] for c in actual if c["expected_direction"] == "up" and c["claim_id"] not in mixed_ids]
        counter = [
            c["claim_id"] for c in actual if c["expected_direction"] == "down" and c["claim_id"] not in mixed_ids
        ]
        coverage = []
        for key, definition in (SOURCE_ANCHOR_MECHANISMS if source_anchors else MECHANISMS).items():
            material = [c for c in recent if c["mechanism"] == key]
            accepted = [c for c in actual if c["mechanism"] == key]
            coverage.append(
                {
                    "mechanism": key,
                    **definition,
                    "status": "available" if accepted else "needs_review" if material else "no_material",
                    "source_claims": len(material),
                    "stored_claims": sum(c["mechanism"] == key for c in product_claims),
                    "usable_episodes": len({c["episode_key"] for c in accepted}),
                }
            )
        price_view = series[target].view(cutoff)
        last_price = price_view.points[-1] if price_view.points and not price_view.latest_blocked else None
        baseline = {
            "series_id": series[target].identity["series_id"],
            "value": last_price.value if last_price else None,
            "observed_at": last_price.observed_at if last_price else None,
            "unit": series[target].identity["unit"],
            "status": "unavailable"
            if last_price is None
            else "stale"
            if (cutoff.astimezone(SHANGHAI).date() - date.fromisoformat(last_price.observed_at[:10])).days
            > FRESHNESS_DAYS[target]
            else "fresh",
            "role": "price_baseline_not_independent_event_evidence",
        }
        for horizon in (1, 7, 30):
            historical = _history(actual, product_claims, series[target], cutoff, horizon)
            cell_gaps = []
            if not receipt.get("complete_requested_scope"):
                cell_gaps.append("本次来源读取未完成，不能据此判断证据不存在")
            if not actual:
                cell_gaps.append("近7日没有通过本轮机制规则的方向性事实")
            if any(c["semantic_status"] != "rule_checked" for c in recent):
                cell_gaps.append("仍有待核验、计划或传闻材料，未计入正反证")
            if not historical:
                cell_gaps.append("没有满足同品种、机制及事前条件的历史案例")
            elif not any(h["outcome"]["state"] == "scored" for h in historical):
                cell_gaps.append("历史候选尚无可用的同口径成熟价格结果")
            if mixed:
                cell_gaps.append("同一事件存在相反机制信号，单独列示，不互相抵消")
            cells[f"{target}:{horizon}"] = {
                "target": target,
                "market_baseline": baseline,
                "horizon_days": horizon,
                "hypothesis": f"其他条件相同，{PRODUCT_LABELS[target]}价格存在上行压力",
                "current_support": support,
                "current_counter": counter,
                "historical_support": [h for h in historical if h["relation"] == "support"],
                "historical_counter": [h for h in historical if h["relation"] == "counter"],
                "historical_other": [h for h in historical if h["relation"] in {"neutral", "unresolved"}],
                "mixed": [[c["claim_id"] for c in rows] for rows in mixed],
                "other_materials": [
                    c["claim_id"]
                    for c in product_claims
                    if c["claim_id"] not in {a["claim_id"] for a in actual}
                    and c["claim_id"] not in {h["claim_id"] for h in historical}
                ],
                "current_support_episodes": len({c["episode_key"] for c in actual if c["claim_id"] in support}),
                "current_counter_episodes": len({c["episode_key"] for c in actual if c["claim_id"] in counter}),
                "coverage": coverage,
                "gaps": cell_gaps,
            }
    return seal(
        {
            "schema_version": policy,
            "as_of_time": cutoff.isoformat(),
            "receipt_sha256": receipt["content_sha256"],
            "price_input_sha256": prices["content_sha256"],
            "source_rows": len(receipt["rows"]),
            "capture_complete": receipt.get("complete_requested_scope", False),
            "claims": claims,
            "cells": cells,
            "source_gaps": dict(gaps),
            "model_effect": "context_only",
            "accuracy_improvement_required": False,
            "forecast_effect_validated": False,
            "claim_count_is_not_independent_votes": True,
        }
    )

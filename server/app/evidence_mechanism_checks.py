"""Small deterministic admissions for explicitly reported source-bound changes.

No general sentiment, geopolitical direction or cross-product votes. Other
mechanisms are handled as labelled conditional semantic reviews, not rule votes.
"""

from __future__ import annotations

import re

from .event_fact_semantics import qualification


def check_mechanism(quote: str, sentence: str, mechanism: str, day: str | None):
    if not day or qualification(sentence) != "reported":
        return None
    if re.search(
        r"\b(?:may|might|would|will|plans?|expects?|forecasts?|denied|not|never)\b|预计|可能|计划|尚未|未发生|未停产|传闻",
        sentence,
        re.I,
    ):
        return None
    if mechanism == "inventory":
        # Authority and crude-stock quantity must be in this same source sentence.
        # Analyst surveys, balances/year-to-date totals and SPR programmes aren't
        # a realised commercial inventory delta.
        if not re.search(
            r"\b(?:EIA|API|Energy Information Administration|American Petroleum Institute)\b", sentence, re.I
        ):
            return None
        subject = re.search(
            r"(?:commercial )?crude oil inventories(?: in the United States)?|"
            r"(?:US|U\.S\.) crude (?:oil )?(?:stocks|inventories)",
            quote,
            re.I,
        )
        motion = list(
            re.finditer(
                r"\b(rose|grew|increased|gained|fell|declined|decreased|dropped)\s+by\s+((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(million|thousand)?\s+barrels\b"
                r"|\b(?:saw an?|recorded an?)\s+(increase|decrease|decline|build|draw)\s+of\s+"
                r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*(million|thousand)?\s+barrels\b",
                quote,
                re.I,
            )
        )
        if (
            not subject
            or len(motion) != 1
            or re.search(r"forecast|analysts?|survey|expected|SPR|year|last \d+ weeks", sentence, re.I)
        ):
            return None
        change = motion[0]
        verb = (change[1] or change[4]).lower()
        quantity = float((change[2] or change[5]).replace(",", ""))
        if quantity <= 0:
            return None
        direction = "down" if verb in {"rose", "grew", "increased", "gained", "increase", "build"} else "up"
        return direction, [], subject[0]
    if mechanism == "supply":
        # Unambiguous English facility execution, not a price or plan statement.
        subject = re.search(r"\b[A-Z][A-Za-z &'-]{1,80}?(?:refinery|oilfield|plant|facility)\b", quote)
        actions = list(
            re.finditer(
                r"\b(?:has |had )?(shut down|halted production|stopped production|"
                r"resumed production|restarted production)\b",
                quote,
                re.I,
            )
        )
        if subject and len(actions) == 1:
            action = actions[0][1].lower()
            return (
                ("up" if action in {"shut down", "halted production", "stopped production"} else "down"),
                [],
                subject[0],
            )
    return None

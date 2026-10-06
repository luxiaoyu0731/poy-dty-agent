from __future__ import annotations

import calendar
import csv
import io
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .models import SourceConfig
from .settings import settings
from .source_automation_policy import get_source_policy

OFAC_SDN_CSV_URL = "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.CSV"
GACC_MONTHLY_URL = "http://english.customs.gov.cn/statics/report/monthly.html"
UN_COMTRADE_DATA_URL = "https://comtradeapi.un.org/data/v1/get/C/M/HS"
UN_COMTRADE_PREVIEW_URL = "https://comtradeapi.un.org/public/v1/preview/C/M/HS"
UN_COMTRADE_AVAILABILITY_URL = "https://comtradeapi.un.org/data/v1/getDa/C/M/HS"
UN_COMTRADE_PUBLIC_AVAILABILITY_URL = "https://comtradeapi.un.org/public/v1/getDa/C/M/HS"

UN_HS_PRODUCTS: dict[str, dict[str, str]] = {
    "270900": {"product": "crude_oil", "label": "crude petroleum oils"},
    "271012": {"product": "naphtha", "label": "light oils and preparations (broad naphtha proxy)"},
    "290243": {"product": "px", "label": "p-xylene"},
    "291736": {"product": "pta", "label": "terephthalic acid and its salts"},
    "290531": {"product": "meg", "label": "ethylene glycol"},
}

GACC_COMMODITIES: dict[str, dict[str, str]] = {
    "coal and lignite": {"product": "coal", "label": "coal and lignite"},
    "crude petroleum oils": {"product": "crude_oil", "label": "crude petroleum oils"},
    "naphtha": {"product": "naphtha", "label": "naphtha"},
    # Broad published category: must never masquerade as pure PX (task rule C.1/C.2).
    "xylenes": {"product": "xylenes_broad", "label": "xylenes (broad GACC category, not pure PX)"},
    "ethylene glycol": {"product": "meg", "label": "ethylene glycol"},
}

OFAC_RELEVANCE_TERMS = (
    "crude",
    "energy",
    "gas",
    "lng",
    "maritime",
    "oil",
    "petrochem",
    "petroleum",
    "shipping",
    "tanker",
    "vessel",
)
OFAC_RELEVANT_PROGRAM_TERMS = ("IRAN", "RUSSIA", "VENEZUELA", "SYRIA", "DPRK")
OFAC_DOWNLOAD_HOST_SUFFIXES = (".s3.amazonaws.com", ".s3.us-gov-west-1.amazonaws.com")


@dataclass(slots=True)
class StructuredSourceResult:
    status: str
    content_type: str
    content_preview: str
    observations: list[dict[str, object]] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    capture_revisions: list[dict[str, object]] = field(default_factory=list)
    state_update: dict[str, object] | None = None


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self._in_row = False
        self._in_cell = False
        self._cell_parts: list[str] = []
        self._cells: list[str] = []
        self._links: list[tuple[str, str]] = []
        self._link_href = ""
        self._link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self._in_row = True
            self._cells = []
            self._links = []
        elif tag in {"td", "th"} and self._in_row:
            self._in_cell = True
            self._cell_parts = []
        elif tag == "a" and self._in_row:
            self._link_href = str(attributes.get("href") or "")
            self._link_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_cell:
            self._cell_parts.append(data)
        if self._link_href:
            self._link_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._in_cell:
            self._cells.append(_clean_text(" ".join(self._cell_parts)))
            self._in_cell = False
        elif tag == "a" and self._link_href:
            self._links.append((_clean_text(" ".join(self._link_parts)), self._link_href))
            self._link_href = ""
            self._link_parts = []
        elif tag == "tr" and self._in_row:
            if self._cells:
                self.rows.append({"cells": self._cells, "links": self._links})
            self._in_row = False


def parse_gacc_monthly_issue_urls(html_text: str, *, base_url: str = GACC_MONTHLY_URL, limit: int = 24) -> list[str]:
    """All published month issues for the major-import table, newest first."""

    parser = _TableParser()
    parser.feed(html_text)
    urls: list[str] = []
    seen: set[str] = set()
    for row in parser.rows:
        cells = row["cells"]
        if not cells or "major import commodities in quantity and value" not in cells[0].lower():
            continue
        for _label, link in row["links"]:
            href = str(link).replace("\\", "/")
            url = urljoin(base_url, href)
            if url not in seen:
                seen.add(url)
                urls.append(url)
    if not urls:
        raise ValueError("GACC monthly bulletin is missing the major-import table")
    # Row order is oldest → newest (the original code took the last link);
    # newest-first keeps "limit=1" equivalent to the previous latest-only path.
    return list(reversed(urls))[: max(1, limit)]


def parse_gacc_latest_major_import_url(html_text: str, *, base_url: str = GACC_MONTHLY_URL) -> str:
    return parse_gacc_monthly_issue_urls(html_text, base_url=base_url, limit=1)[0]


def parse_gacc_major_imports(html_text: str, *, evidence_url: str) -> list[dict[str, object]]:
    parser = _TableParser()
    parser.feed(html_text)
    title = next(
        (
            cell
            for row in parser.rows[:5]
            for cell in row["cells"]
            if "major import commodities in quantity and value" in cell.lower()
        ),
        "",
    )
    match = re.search(r",\s*(\d{1,2})\.(\d{4})", title)
    if match is None:
        raise ValueError("GACC major-import table title does not contain month and year")
    month = int(match.group(1))
    year = int(match.group(2))
    observed_at = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
    observations: list[dict[str, object]] = []
    found: set[str] = set()
    for row in parser.rows:
        cells = row["cells"]
        if len(cells) < 8:
            continue
        commodity = _clean_text(cells[0]).lower()
        config = GACC_COMMODITIES.get(commodity)
        if config is None:
            continue
        unit = _clean_text(cells[1])
        quantity = _number(cells[2])
        customs_value = _number(cells[3])
        if quantity is None or customs_value is None:
            raise ValueError(f"GACC supported row is missing numeric monthly values: {commodity}")
        quantity_tonnes, quantity_unit = _gacc_quantity(quantity, unit)
        common_raw = {
            "commodity": cells[0],
            "source_unit": unit,
            "period": f"{year:04d}{month:02d}",
            "classification": "GACC published major commodity category",
            "hs_code": "",
            "scope_note": config["label"],
            "monthly_quantity_raw": cells[2],
            "monthly_value_usd_thousand_raw": cells[3],
            "cumulative_quantity_raw": cells[4],
            "cumulative_value_usd_thousand_raw": cells[5],
        }
        observations.extend(
            [
                {
                    "source_id": "gacc_trade_statistics",
                    "observed_at": observed_at,
                    "indicator": f"China monthly imports quantity - {config['label']} - World",
                    "product": config["product"],
                    "value": quantity_tonnes,
                    "unit": quantity_unit,
                    "frequency": "monthly",
                    "region": "China imports from World",
                    "evidence_url": evidence_url,
                    "notes": (
                        "Official GACC monthly major-import bulletin; published category may be broader than HS6. "
                        "xylenes_broad is a context/proxy category and is excluded from the pure PX formal series."
                    ),
                    "raw": {**common_raw, "measure": "quantity"},
                },
                {
                    "source_id": "gacc_trade_statistics",
                    "observed_at": observed_at,
                    "indicator": f"China monthly imports customs value - {config['label']} - World",
                    "product": config["product"],
                    "value": customs_value * 1000,
                    "unit": "usd",
                    "frequency": "monthly",
                    "region": "China imports from World",
                    "evidence_url": evidence_url,
                    "notes": "Official GACC monthly major-import bulletin; value converted from US$1,000.",
                    "raw": {**common_raw, "measure": "customs_value"},
                },
            ]
        )
        found.add(config["product"])
    if not observations:
        raise ValueError("GACC major-import table contains none of the configured commodities")
    return observations


async def fetch_gacc_trade_statistics(source: SourceConfig, *, max_issues: int = 24) -> StructuredSourceResult:
    settings.require_outbound_url_allowed(GACC_MONTHLY_URL)
    headers = {"User-Agent": "POY-DTY-Agent/1.0 personal-research"}
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        index_response = await client.get(GACC_MONTHLY_URL, headers=headers)
        _raise_for_status(index_response, "GACC monthly bulletin")
        issue_urls = parse_gacc_monthly_issue_urls(index_response.text, limit=max_issues)
        observations: list[dict[str, object]] = []
        issue_errors: list[str] = []
        fetched_urls: list[str] = []
        content_type = "text/html"
        for detail_url in issue_urls:
            try:
                settings.require_outbound_url_allowed(detail_url)
                detail_response = await client.get(detail_url, headers=headers)
                _raise_for_status(detail_response, "GACC major-import table")
                observations.extend(parse_gacc_major_imports(detail_response.text, evidence_url=detail_url))
                fetched_urls.append(detail_url)
                content_type = detail_response.headers.get("content-type", content_type)
            except Exception as exc:  # noqa: BLE001 - one bad month must not block the rest of the backfill.
                issue_errors.append(f"{detail_url}: {exc}")
    if not observations:
        raise ValueError(f"GACC backfill produced no observations; errors: {issue_errors[:3]}")
    products = sorted({str(item["product"]) for item in observations})
    return StructuredSourceResult(
        status="ok",
        content_type=content_type,
        content_preview=json.dumps(
            {
                "records": len(observations),
                "products": products,
                "issues_fetched": fetched_urls,
                "issue_errors": issue_errors[:6],
            },
            ensure_ascii=False,
        ),
        observations=observations,
    )


def parse_ofac_sdn_csv(content: bytes) -> tuple[int, dict[str, dict[str, str]]]:
    text = content.decode("utf-8-sig", errors="replace")
    relevant: dict[str, dict[str, str]] = {}
    total = 0
    for row in csv.reader(io.StringIO(text)):
        if not row or not str(row[0]).strip():
            continue
        total += 1
        padded = [str(value).strip() for value in row] + [""] * 12
        entity_id, name, entity_type, programs = padded[0], padded[1], padded[2], padded[3]
        vessel_type, flag, remarks = padded[6], padded[9], padded[11]
        searchable = " ".join((name, entity_type, programs, vessel_type, flag, remarks)).lower()
        relevant_program = any(term in programs.upper() for term in OFAC_RELEVANT_PROGRAM_TERMS)
        relevant_term = any(term in searchable for term in OFAC_RELEVANCE_TERMS)
        if entity_type.upper() != "VESSEL" and not (relevant_program and relevant_term):
            continue
        fingerprint = sha256("\x1f".join(padded[:12]).encode("utf-8")).hexdigest()
        relevant[entity_id] = {
            "fingerprint": fingerprint,
            "name": name,
            "entity_type": entity_type,
            "programs": programs,
        }
    if total == 0:
        raise ValueError("OFAC SDN CSV contains no records")
    return total, relevant


def build_ofac_delta_events(
    current: dict[str, dict[str, str]],
    previous_state: dict[str, object] | None,
    *,
    occurred_at: str,
    snapshot_sha256: str,
) -> list[dict[str, object]]:
    previous = previous_state.get("relevant_records", {}) if isinstance(previous_state, dict) else {}
    if not isinstance(previous, dict) or not previous_state:
        return []
    previous_ids = set(str(key) for key in previous)
    current_ids = set(current)
    changes: list[tuple[str, str]] = [*(("added", key) for key in current_ids - previous_ids)]
    changes.extend(("removed", key) for key in previous_ids - current_ids)
    changes.extend(
        ("modified", key)
        for key in current_ids & previous_ids
        if str(previous.get(key)) != str(current[key]["fingerprint"])
    )
    events: list[dict[str, object]] = []
    for action, entity_id in sorted(changes):
        current_item = current.get(entity_id, {})
        # Full names must not enter the business database (task rule B.1);
        # identify the record by its stable SDN entry id and program tags only.
        entity_label = f"{current_item.get('entity_type', 'ENTITY').upper()} #{entity_id}"
        programs = current_item.get("programs", "")
        events.append(
            {
                "source_id": "ofac_sanctions",
                "occurred_at": occurred_at,
                "title": f"OFAC energy/shipping-relevant designation {action}: {entity_label}",
                "event_type": "sanctions_update",
                "evidence_level": "A",
                "summary": (
                    f"Official OFAC SDN snapshot comparison detected an {action} energy or shipping relevant record."
                ),
                "affected_products": ["crude_oil", "shipping", "energy_policy", "geopolitics"],
                "direction": "中性",
                "impact_strength": "",
                "evidence_url": OFAC_SDN_CSV_URL,
                "requires_human_review": True,
                "notes": (
                    "Directional market impact requires corroboration; full sanctions-list PII is not persisted here."
                ),
                "raw": {
                    "entity_id": entity_id,
                    "entity_type": current_item.get("entity_type", ""),
                    "programs": programs,
                    "action": action,
                    "snapshot_sha256": snapshot_sha256,
                },
            }
        )
    return events


async def fetch_ofac_sanctions(
    source: SourceConfig, *, previous_state: dict[str, object] | None
) -> StructuredSourceResult:
    settings.require_outbound_url_allowed(OFAC_SDN_CSV_URL)
    headers = {"User-Agent": "POY-DTY-Agent/1.0 personal-research"}
    async with httpx.AsyncClient(timeout=45, follow_redirects=False) as client:
        response = await client.get(OFAC_SDN_CSV_URL, headers=headers)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = urljoin(OFAC_SDN_CSV_URL, response.headers.get("location", ""))
            redirect_host = (urlparse(location).hostname or "").lower()
            if not ofac_download_host_allowed(redirect_host):
                raise ValueError(f"OFAC download redirected to an untrusted host: {redirect_host or 'missing'}")
            response = await client.get(location, headers=headers)
        _raise_for_status(response, "OFAC SDN CSV")
    digest = sha256(response.content).hexdigest()
    total, relevant = parse_ofac_sdn_csv(response.content)
    fetched_at = datetime.now(UTC).isoformat()
    events = build_ofac_delta_events(
        relevant,
        previous_state,
        occurred_at=fetched_at,
        snapshot_sha256=digest,
    )
    delta_counts = {"added": 0, "removed": 0, "modified": 0}
    for event in events:
        action = str(event["raw"]["action"])
        delta_counts[action] = delta_counts.get(action, 0) + 1
    try:
        frequency_seconds = get_source_policy("ofac_sanctions").frequency_seconds
        next_due_at = (datetime.now(UTC) + timedelta(seconds=frequency_seconds)).isoformat()
    except Exception:  # noqa: BLE001 - state metrics must never break the fetch itself.
        next_due_at = None
    state_update: dict[str, object] = {
        "schema_version": "ofac-snapshot.v1",
        "fetched_at": fetched_at,
        "source_url": OFAC_SDN_CSV_URL,
        "source_sha256": digest,
        "total_records": total,
        "relevant_count": len(relevant),
        "relevant_records": {entity_id: item["fingerprint"] for entity_id, item in sorted(relevant.items())},
        "delta_counts": delta_counts,
        "last_success_at": fetched_at,
        "next_due_at": next_due_at,
    }
    unchanged = isinstance(previous_state, dict) and previous_state.get("source_sha256") == digest
    return StructuredSourceResult(
        status="unchanged" if unchanged else "ok",
        content_type=response.headers.get("content-type", "text/csv"),
        content_preview=json.dumps(
            {
                "source_sha256": digest,
                "total_records": total,
                "relevant_count": len(relevant),
                "delta_events": len(events),
            },
            ensure_ascii=False,
        ),
        events=events,
        state_update=state_update,
    )


async def fetch_un_comtrade(
    source: SourceConfig,
    *,
    api_key: str,
    previous_state: dict[str, object] | None = None,
    max_periods: int = 24,
) -> StructuredSourceResult:
    """Fetch UN Comtrade monthly China imports with bounded multi-period backfill.

    The official free dataset lags (availability measured 2026-08-31 ends at
    2024-12), so this source is historical/context: it must never claim current
    formal freshness. Completed periods are checkpointed in the source state so
    rate-limited runs resume where they stopped.
    """

    settings.require_outbound_url_allowed(UN_COMTRADE_DATA_URL)
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        availability = await _fetch_comtrade_availability(client, api_key=api_key)
        periods = available_comtrade_periods(_comtrade_rows(availability))
        if not periods:
            return StructuredSourceResult(
                status="no_new_data",
                content_type="application/json",
                content_preview=json.dumps({"availability_rows": len(_comtrade_rows(availability)), "records": 0}),
            )
        already_done = set()
        if isinstance(previous_state, dict):
            done = previous_state.get("backfilled_periods")
            if isinstance(done, list):
                already_done = {str(item) for item in done}
        pending = [period for period in periods if period not in already_done][: max(1, max_periods)]
        if not pending:
            return StructuredSourceResult(
                status="unchanged",
                content_type="application/json",
                content_preview=json.dumps(
                    {
                        "available_max_period": periods[0],
                        "backfilled_periods": sorted(already_done),
                        "records": 0,
                        "note": "all available periods already backfilled",
                    },
                    ensure_ascii=False,
                ),
            )
        all_rows: list[dict[str, object]] = []
        used_preview = not bool(api_key) or bool(availability.get("_preview_fallback"))
        request_api_key = "" if availability.get("_preview_fallback") else api_key
        period_errors: list[str] = []
        completed: list[str] = []
        for period in pending:
            period_rows: list[dict[str, object]] = []
            try:
                payload = await _fetch_comtrade_json(
                    client,
                    api_key=request_api_key,
                    period=period,
                    cmd_code=",".join(UN_HS_PRODUCTS),
                    partner_code=None,
                )
            except Exception as exc:  # noqa: BLE001 - per-period isolation keeps the backfill resumable.
                period_errors.append(f"{period}/configured_hs_batch: {exc}")
                continue
            used_preview = used_preview or bool(payload.get("_preview_fallback"))
            rows = _comtrade_rows(payload)
            for hs_code in UN_HS_PRODUCTS:
                period_rows.extend(_select_comtrade_rows(rows, hs_code=hs_code))
            completed.append(period)
            all_rows.extend(period_rows)
    observations = comtrade_rows_to_observations(all_rows, evidence_url=UN_COMTRADE_DATA_URL)
    if not observations and not completed:
        raise ValueError("UN Comtrade returned rows but none matched the configured HS products")
    consistency = comtrade_consistency_findings(all_rows)
    state_update: dict[str, object] = {
        "schema_version": "un-comtrade-backfill.v1",
        "role": "historical_context",
        "current_formal_eligible": False,
        "available_max_period": periods[0],
        "backfilled_periods": sorted(already_done | set(completed)),
        "last_success_at": datetime.now(UTC).isoformat(),
    }
    return StructuredSourceResult(
        status="partial" if period_errors else "ok",
        content_type="application/json",
        content_preview=json.dumps(
            {
                "periods_completed": completed,
                "periods_pending_errors": period_errors[:6],
                "available_max_period": periods[0],
                "current_formal_eligible": False,
                "records": len(observations),
                "products": sorted({str(item["product"]) for item in observations}),
                "preview_fallback": used_preview,
                "consistency_findings": consistency,
            },
            ensure_ascii=False,
        ),
        observations=observations,
        state_update=state_update,
    )


def available_comtrade_periods(rows: list[dict[str, object]]) -> list[str]:
    """All valid monthly periods the official dataset offers, newest first."""

    latest_complete = int(_recent_complete_months(1)[0])
    periods = {
        str(row.get("period"))
        for row in rows
        if re.fullmatch(r"\d{6}", str(row.get("period") or "")) and int(row.get("period")) <= latest_complete
    }
    return sorted(periods, reverse=True)


def comtrade_consistency_findings(rows: list[dict[str, object]]) -> list[str]:
    """Cross-check world totals against top partners; findings never fail the run."""

    findings: list[str] = []
    grouped: dict[tuple[str, str], list[dict[str, object]]] = {}
    for row in rows:
        key = (str(row.get("period")), str(row.get("cmdCode")))
        grouped.setdefault(key, []).append(row)
    for (period, hs_code), group in sorted(grouped.items()):
        partners = [row for row in group if str(row.get("partnerCode") or "") not in {"", "0"}]
        codes = [str(row.get("partnerCode")) for row in partners]
        duplicated = sorted({code for code in codes if codes.count(code) > 1})
        if duplicated:
            findings.append(f"duplicate_partner:{period}:{hs_code}:{','.join(sorted(set(duplicated)))}")
        world = [row for row in group if str(row.get("partnerCode") or "") == "0"]
        if not world or not partners:
            continue
        world_value = _object_number(world[0].get("primaryValue"))
        partner_total = sum(_object_number(row.get("primaryValue")) or 0 for row in partners)
        if world_value and partner_total > world_value * 1.05:
            findings.append(f"partner_sum_exceeds_world:{period}:{hs_code}")
    return findings[:20]


def latest_comtrade_period(rows: list[dict[str, object]]) -> str:
    latest_complete = int(_recent_complete_months(1)[0])
    periods = []
    for row in rows:
        raw_period = str(row.get("period") or "")
        if re.fullmatch(r"\d{6}", raw_period) and int(raw_period) <= latest_complete:
            periods.append(raw_period)
    return max(periods, default="")


def comtrade_rows_to_observations(rows: list[dict[str, object]], *, evidence_url: str) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for row in rows:
        hs_code = str(row.get("cmdCode") or "")
        config = UN_HS_PRODUCTS.get(hs_code)
        period = str(row.get("period") or row.get("refPeriodId") or "")
        match = re.fullmatch(r"(\d{4})(\d{2})", period)
        if config is None or match is None:
            continue
        year, month = int(match.group(1)), int(match.group(2))
        observed_at = date(year, month, calendar.monthrange(year, month)[1]).isoformat()
        partner_code = str(row.get("partnerCode") or "")
        partner = str(row.get("partnerDesc") or ("World" if partner_code == "0" else partner_code))
        quantity_kg = _object_number(row.get("netWgt"))
        primary_value = _object_number(row.get("primaryValue"))
        raw = {
            "hs_code": hs_code,
            "hs_label": config["label"],
            "classification_code": row.get("clCode") or row.get("classificationCode") or "HS",
            "period": period,
            "reporter_code": row.get("reporterCode"),
            "partner_code": row.get("partnerCode"),
            "partner": partner,
            "flow_code": row.get("flowCode"),
            "is_reported": row.get("isReported"),
            "is_aggregate": row.get("isAggregate"),
            "publication_date": row.get("publicationDate") or row.get("publishedDate"),
        }
        common = {
            "source_id": "un_comtrade_api",
            "observed_at": observed_at,
            "product": config["product"],
            "frequency": "monthly",
            "region": f"China imports from {partner}",
            "evidence_url": evidence_url,
            "notes": "UN Comtrade final monthly China imports; HS revision and partner code preserved in raw data.",
        }
        if quantity_kg is not None:
            observations.append(
                {
                    **common,
                    "indicator": f"China monthly imports quantity - HS {hs_code} {config['label']} - {partner}",
                    "value": quantity_kg / 1000,
                    "unit": "metric_tonnes",
                    "raw": {**raw, "measure": "net_weight", "net_weight_kg": quantity_kg},
                }
            )
        if primary_value is not None:
            observations.append(
                {
                    **common,
                    "indicator": f"China monthly imports customs value - HS {hs_code} {config['label']} - {partner}",
                    "value": primary_value,
                    "unit": "usd",
                    "raw": {**raw, "measure": "primary_value", "primary_value_usd": primary_value},
                }
            )
    return observations


async def _fetch_comtrade_json(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    period: str,
    cmd_code: str,
    partner_code: str | None,
) -> dict[str, object]:
    params: dict[str, str] = {
        "period": period,
        "reporterCode": "156",
        "cmdCode": cmd_code,
        "flowCode": "M",
        "partner2Code": "0",
        "customsCode": "C00",
        "motCode": "0",
        "maxRecords": "500",
        "breakdownMode": "classic",
        "includeDesc": "true",
    }
    if partner_code is not None:
        params["partnerCode"] = partner_code
    if api_key:
        params["subscription-key"] = api_key
    endpoint = UN_COMTRADE_DATA_URL if api_key else UN_COMTRADE_PREVIEW_URL
    response = await client.get(endpoint, params=params, headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"})
    preview_fallback = False
    if api_key and response.status_code in {401, 403, 429}:
        params.pop("subscription-key", None)
        endpoint = UN_COMTRADE_PREVIEW_URL
        response = await client.get(
            endpoint,
            params=params,
            headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"},
        )
        preview_fallback = True
    _raise_for_status(response, "UN Comtrade API")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("UN Comtrade response is not an object")
    if preview_fallback:
        payload["_preview_fallback"] = True
    return payload


async def _fetch_comtrade_availability(client: httpx.AsyncClient, *, api_key: str) -> dict[str, object]:
    params = {"reportercode": "156"}
    if api_key:
        params["subscription-key"] = api_key
    endpoint = UN_COMTRADE_AVAILABILITY_URL if api_key else UN_COMTRADE_PUBLIC_AVAILABILITY_URL
    response = await client.get(endpoint, params=params, headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"})
    preview_fallback = False
    if api_key and response.status_code in {401, 403, 429}:
        params.pop("subscription-key", None)
        response = await client.get(
            UN_COMTRADE_PUBLIC_AVAILABILITY_URL,
            params=params,
            headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"},
        )
        preview_fallback = True
    _raise_for_status(response, "UN Comtrade data availability API")
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("UN Comtrade data availability response is not an object")
    if preview_fallback:
        payload["_preview_fallback"] = True
    return payload


def _select_comtrade_rows(rows: list[dict[str, object]], *, hs_code: str) -> list[dict[str, object]]:
    matching = [row for row in rows if str(row.get("cmdCode") or "") == hs_code]
    world = [row for row in matching if str(row.get("partnerCode") or "") == "0"]
    partners = [row for row in matching if str(row.get("partnerCode") or "") not in {"", "0"}]
    partners.sort(key=lambda row: _object_number(row.get("primaryValue")) or 0, reverse=True)
    return [*world[:1], *partners[:10]]


def _comtrade_rows(payload: dict[str, object]) -> list[dict[str, object]]:
    data = payload.get("data")
    return [dict(row) for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def load_source_state(
    path: Path,
    *,
    expected_schema: str | None = None,
    fail_on_corrupt: bool = False,
) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        if fail_on_corrupt:
            raise ValueError(f"source_state_corrupt:{path.name}") from exc
        return None
    if not isinstance(payload, dict):
        if fail_on_corrupt:
            raise ValueError(f"source_state_not_object:{path.name}")
        return None
    if expected_schema and payload.get("schema_version") != expected_schema:
        if fail_on_corrupt:
            raise ValueError(f"source_state_schema_mismatch:{path.name}")
        return None
    if expected_schema == "ofac-snapshot.v1":
        digest = str(payload.get("source_sha256") or "")
        records = payload.get("relevant_records")
        valid_records = isinstance(records, dict) and all(
            str(key).strip() and re.fullmatch(r"[0-9a-f]{64}", str(value) or "") for key, value in records.items()
        )
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not valid_records:
            if fail_on_corrupt:
                raise ValueError(f"source_state_integrity_invalid:{path.name}")
            return None
    return payload


def backup_source_state(
    path: Path,
    *,
    backup_dir: Path,
    retention_count: int = 10,
    expected_schema: str | None = None,
) -> Path | None:
    """Create a durable pre-replacement copy of a valid source state file."""

    if not path.exists():
        return None
    # Never publish a corrupt state as the newest recovery point.
    load_source_state(path, expected_schema=expected_schema, fail_on_corrupt=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{path.stem}.{stamp}.json"
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with path.open("rb") as source, temporary.open("xb") as target:
        shutil.copyfileobj(source, target)
        target.flush()
        os.fsync(target.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)
    backups = sorted(backup_dir.glob(f"{path.stem}.*.json"), reverse=True)
    for expired in backups[max(1, retention_count) :]:
        expired.unlink()
    return destination


def restore_source_state(backup: Path, *, destination: Path, expected_schema: str) -> None:
    payload = load_source_state(backup, expected_schema=expected_schema, fail_on_corrupt=True)
    if payload is None:  # pragma: no cover - strict loader cannot return None for an existing backup.
        raise ValueError("source_state_backup_missing")
    write_source_state(destination, payload)


def ofac_download_host_allowed(host: str) -> bool:
    normalized = host.strip().lower().rstrip(".")
    return any(normalized.endswith(suffix) for suffix in OFAC_DOWNLOAD_HOST_SUFFIXES)


def write_source_state(path: Path, payload: dict[str, object]) -> None:
    """Persist source state atomically: temp file, fsync, restrictive mode, replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)


def _recent_complete_months(count: int) -> list[str]:
    current = datetime.now(UTC).date().replace(day=1)
    periods: list[str] = []
    year, month = current.year, current.month
    for _ in range(count):
        month -= 1
        if month == 0:
            year -= 1
            month = 12
        periods.append(f"{year:04d}{month:02d}")
    return periods


def _gacc_quantity(value: float, unit: str) -> tuple[float, str]:
    normalized = unit.upper().replace(" ", "")
    if normalized == "10000T":
        return value * 10_000, "metric_tonnes"
    if normalized == "T":
        return value, "metric_tonnes"
    return value, unit or "source_unit"


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _number(value: str) -> float | None:
    cleaned = value.replace(",", "").strip()
    if not cleaned or cleaned == "-":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _object_number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _raise_for_status(response: httpx.Response, label: str) -> None:
    if response.status_code >= 400:
        raise RuntimeError(f"{label} returned HTTP {response.status_code}")

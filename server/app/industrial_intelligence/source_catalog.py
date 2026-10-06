"""Derived read-only source catalog for the intelligence domain.

The catalog is a merge of the existing core source registry (governance
authority) and ``NEWS_SOURCES`` (acquisition fields). It is never persisted as
a third writable registry: every read re-derives it, and daily briefs embed a
full safe snapshot so history stays recomputable. Soft-removed core entries
(DCE/CCF) are displayed with their frozen status but never count toward the
active baseline or readiness. USGS enters as a provider-declared capability
until its core-registry admission is reviewed with the operator.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .. import news as news_module
from .. import source_registry
from ..settings import settings
from . import identity
from .identity import canonical_json, sha256_hex

# Frozen baseline for the current fixture: 23 active core entries + 44 news
# definitions - 3 overlapping identities = 64 unique active catalog entries.
EXPECTED_ACTIVE_CORE_COUNT = 23
EXPECTED_NEWS_COUNT = 44
EXPECTED_OVERLAP_IDS = ("mpa_press_releases", "opec_press", "us_centcom_press")
EXPECTED_ACTIVE_BASELINE = EXPECTED_ACTIVE_CORE_COUNT + EXPECTED_NEWS_COUNT - len(EXPECTED_OVERLAP_IDS)

COST_STATUSES = ("free", "optional_paid", "paid", "unknown")
CREDENTIAL_STATUSES = ("not_required", "configured", "missing", "invalid", "unknown")

API_KEY_SOURCE_IDS = frozenset({"eia_petroleum_api", "fred_macro_api", "un_comtrade_api"})

# Provider-declared zero-key capabilities (not part of the 59 baseline).
PROVIDER_DECLARED_ENTRIES = (
    {
        "source_id": "usgs_eq_m45_weekly",
        "display_name": "USGS Earthquakes M4.5+ Past Week (official GeoJSON)",
        "source_type": "intelligence_provider",
        "tier": "A",
        "categories": ["weather_disaster"],
        "capabilities": ["https_geojson_weekly_feed"],
        "cadence": "daily",
        "cost_status": "free",
        "credential_status": "not_required",
        "operational_status": "active",
        "rights_summary": "USGS public-domain event feed; facts and coordinates only.",
        "last_attempt_at": None,
        "last_success_at": None,
        "quality_status": "unknown",
        "metadata_drift": False,
        "drift_fields": [],
    },
)


@dataclass(frozen=True)
class CatalogDerivation:
    entries: list[dict[str, object]]
    active_baseline_count: int
    overlap_ids: tuple[str, ...]
    drift_ids: tuple[str, ...]
    derived_at: str
    derivation: dict[str, object] = field(default_factory=dict)


def _capability_from_core(crawl_type: str) -> str:
    return {
        "static_html": "fetch_html",
        "rss": "fetch_rss",
        "api": "fetch_api",
        "manual": "manual",
    }.get(crawl_type, f"fetch_{crawl_type}")


def _capability_from_news(fetcher: str) -> str:
    return {"html": "fetch_html", "rss": "fetch_rss", "manual": "manual"}.get(fetcher, f"fetch_{fetcher}")


def _credential_status(source_id: str) -> str:
    if source_id in API_KEY_SOURCE_IDS:
        return "configured" if settings.source_credentials_configured(source_id) else "missing"
    return "not_required"


def _derive_entry(
    source_id: str,
    core: dict[str, object] | None,
    news: dict[str, object] | None,
) -> dict[str, object]:
    drift_fields: list[str] = []
    if core is not None and news is not None:
        for governance_field in ("tier", "source_name", "url"):
            core_value = core.get(governance_field)
            news_value = news.get(governance_field)
            if core_value is not None and news_value is not None and core_value != news_value:
                drift_fields.append(governance_field)
    categories: list[str] = []
    for origin in (core, news):
        if origin and origin.get("category") and origin["category"] not in categories:
            categories.append(str(origin["category"]))
    capabilities: list[str] = []
    if core is not None:
        capability = _capability_from_core(str(core.get("crawl_type", "static_html")))
        if capability not in capabilities:
            capabilities.append(capability)
    if news is not None:
        capability = _capability_from_news(str(news.get("fetcher", "html")))
        if capability not in capabilities:
            capabilities.append(capability)
    operational_status = (
        str(core.get("operational_status", "active")) if core is not None else "active"
    )
    entry: dict[str, object] = {
        "source_id": source_id,
        "display_name": str((core or news or {}).get("source_name", source_id)),
        # Governance authority: overlap entries keep the core identity type.
        "source_type": "core" if core is not None else "news",
        "tier": str((core or news or {}).get("tier", "D")),
        "categories": categories,
        "capabilities": capabilities,
        "cadence": str(
            (core or {}).get("frequency") or (news or {}).get("cadence") or "unknown"
        ),
        "cost_status": "free",
        "credential_status": _credential_status(source_id),
        "operational_status": operational_status,
        "rights_summary": str((core or {}).get("license_note") or "Public web source; metadata-first handling."),
        "last_attempt_at": None,
        "last_success_at": None,
        "quality_status": "unknown",
        "metadata_drift": bool(drift_fields),
        "drift_fields": drift_fields,
        "products": list((core or {}).get("products") or []),
        "data_role": str((core or {}).get("data_role", "current_candidate"))
        if core is not None
        else "event_evidence",
    }
    return entry


def derive_catalog(*, now: str | None = None) -> CatalogDerivation:
    core_sources = source_registry.load_sources()
    core_by_id = {source.source_id: source for source in core_sources}
    news_by_id = {source.source_id: source for source in news_module.news_sources()}

    entries: list[dict[str, object]] = []
    drift_ids: list[str] = []
    for source_id in sorted(set(core_by_id) | set(news_by_id)):
        core = core_by_id.get(source_id)
        news = news_by_id.get(source_id)
        core_dict = core.model_dump() if core is not None else None
        news_dict = news.model_dump() if news is not None else None
        entry = _derive_entry(source_id, core_dict, news_dict)
        if entry["metadata_drift"]:
            drift_ids.append(source_id)
        entries.append(entry)

    active_baseline = [
        entry
        for entry in entries
        if entry["operational_status"] == "active"
        and entry["source_type"] in ("core", "news")
    ]
    overlap_ids = tuple(sorted(set(core_by_id) & set(news_by_id)))
    derivation = {
        "core_total": len(core_by_id),
        "core_active": sum(
            1 for source in core_sources if source.operational_status == "active"
        ),
        "news_total": len(news_by_id),
        "policy_version": identity.SOURCE_CATALOG_POLICY_VERSION,
    }
    return CatalogDerivation(
        entries=entries,
        active_baseline_count=len(active_baseline),
        overlap_ids=overlap_ids,
        drift_ids=tuple(sorted(drift_ids)),
        derived_at=now or datetime.now(UTC).replace(microsecond=0).isoformat(),
        derivation=derivation,
    )


def safe_catalog_snapshot(derivation: CatalogDerivation) -> tuple[list[dict[str, object]], int, str]:
    """Return the full safe canonical snapshot, its entry count and SHA-256.

    The snapshot only carries identity, tier, capability/cadence, boolean or
    enum credential state, run/rights status, and safe gaps — never secret
    values, tokens, server paths, or unsanitized error detail.
    """

    snapshot = [
        {
            "source_id": entry["source_id"],
            "display_name": entry["display_name"],
            "source_type": entry["source_type"],
            "tier": entry["tier"],
            "categories": entry["categories"],
            "capabilities": entry["capabilities"],
            "cadence": entry["cadence"],
            "cost_status": entry["cost_status"],
            "credential_status": entry["credential_status"],
            "operational_status": entry["operational_status"],
            "rights_summary": entry["rights_summary"],
            "metadata_drift": entry["metadata_drift"],
            "drift_fields": entry["drift_fields"],
        }
        for entry in entries_with_provider_capabilities(derivation)
    ]
    return snapshot, len(snapshot), sha256_hex(canonical_json(snapshot))


def entries_with_provider_capabilities(
    derivation: CatalogDerivation,
) -> list[dict[str, object]]:
    # Intraday and daily providers are distinct channels. Never borrow a
    # successful intraday quote to claim the daily collector succeeded.
    price_channels = []
    for source_id, name in (
        ("yahoo_finance_proxy", "Yahoo 盘中期货代理行情"),
        ("sina_global_futures", "新浪外盘原油行情"),
        ("sina_futures_realtime", "新浪国内期货行情"),
        ("eastmoney_futures_realtime", "东方财富期货行情"),
        ("public_spot_page_refresh", "公开页面现货参考价"),
    ):
        price_channels.append({
            **PROVIDER_DECLARED_ENTRIES[0], "source_id": source_id, "display_name": name,
            "source_type": "price_channel", "tier": "C", "categories": ["energy"],
            "capabilities": ["public_price_read"],
            "cadence": f"{settings.intraday_price_interval_seconds // 60}min",
            "operational_status": "active" if settings.intraday_price_scheduler_enabled else "disabled",
            "rights_summary": "公开行情读取通道，与日线采集分别记录，不代表成交价。",
        })
    return sorted(
        [*derivation.entries, *PROVIDER_DECLARED_ENTRIES, *price_channels],
        key=lambda item: str(item["source_id"]),
    )


def catalog_entry_json(entry: dict[str, object]) -> str:
    return canonical_json(entry)


def with_runtime_health(
    connection: sqlite3.Connection, entries: list[dict[str, object]], *, as_of: str
) -> list[dict[str, object]]:
    """Join actual completed reads, never promote configured capability to success.

    This projection is read only. The cutoff also makes cursor paging stable
    when another collector finishes while a user is browsing the catalog.
    """
    rows = connection.execute(
        "SELECT source_id, created_at, status FROM news_fetch_runs "
        "WHERE julianday(created_at)<=julianday(?) AND finished_at IS NOT NULL "
        "UNION ALL SELECT source_id, created_at, status FROM source_fetch_audit "
        "WHERE julianday(created_at)<=julianday(?) "
        "UNION ALL SELECT provider_id, finished_at, "
        "CASE WHEN status='succeeded' THEN 'ok' ELSE status END FROM intelligence_runs "
        "WHERE provider_id IS NOT NULL AND julianday(finished_at)<=julianday(?)",
        (as_of, as_of, as_of),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["source_id"]), []).append(row)
    observed_sources = set()
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='intraday_price_observations'").fetchone():
        observed_sources = {str(row[0]) for row in connection.execute(
            "SELECT DISTINCT source_id FROM intraday_price_observations WHERE julianday(created_at)<=julianday(?)",
            (as_of,),
        )}
    result = []
    for entry in entries:
        item = dict(entry)
        attempts = sorted(
            grouped.get(str(item["source_id"]), []),
            key=lambda r: identity.parse_iso(r["created_at"]), reverse=True,
        )
        successful = [r for r in attempts if r["status"] in ("ok", "unchanged", "no_relevant_items")]
        item["last_attempt_at"] = attempts[0]["created_at"] if attempts else None
        item["last_success_at"] = successful[0]["created_at"] if successful else None
        item["quality_status"] = str(attempts[0]["status"]) if attempts else "not_observed"
        if not attempts and str(item["source_id"]) in observed_sources:
            item["quality_status"] = "observations_without_run"
        if attempts and item["quality_status"] in ("ok", "unchanged", "no_relevant_items"):
            cadence = str(item.get("cadence", ""))
            minutes = re.findall(r"\d+", cadence) if "min" in cadence else []
            if minutes:
                grace_seconds = max(int(value) for value in minutes) * 120
                age = (identity.parse_iso(as_of) - identity.parse_iso(attempts[0]["created_at"])).total_seconds()
                if age > grace_seconds:
                    item["quality_status"] = "overdue"
        if item["operational_status"] != "active":
            item["quality_status"] = "historical_only"
        result.append(item)
    return result

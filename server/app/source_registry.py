from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from .models import SourceConfig, Tier
from .settings import settings

ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "source_registry.json"
FETCHABLE_CRAWL_TYPES = {"static_html", "html_download", "api_json"}
# Tombstone identities for validating frozen historical contracts only. They
# are not source configurations and must never enter fetch/schedule lists.
REMOVED_SOURCE_IDS = frozenset({"ccf_dom_daily", "ccf_average_price", "ccf_manual_export", "dce_meg"})
# These sources have dedicated fetchers that independently call the runtime
# outbound allowlist before issuing a request. Keeping their registration
# separate from a deployment's current allowlist lets the application boot
# while the candidate remains safely blocked until operations permits its host.
RUNTIME_GUARDED_FETCHER_SOURCE_IDS = {
    "coalchina_cctd_bohai_rim_5500_daily_reference",
    "czce_pta_px",
    "tnc_polyester_history",
    "texnet_price_articles",
}


def validate_source_registry(sources: list[SourceConfig]) -> list[str]:
    issues: list[str] = []
    source_ids = [source.source_id for source in sources]
    duplicate_ids = sorted({source_id for source_id in source_ids if source_ids.count(source_id) > 1})
    for source_id in duplicate_ids:
        issues.append(f"duplicate source_id: {source_id}")

    for source in sources:
        if source.freshness_sla_minutes <= 0:
            issues.append(f"{source.source_id}: freshness_sla_minutes must be positive")
        parsed = urlparse(source.url)
        if source.url.startswith("internal://"):
            if source.auth_type != "internal":
                issues.append(f"{source.source_id}: internal URLs require auth_type=internal")
            continue
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            issues.append(f"{source.source_id}: url must be http(s) or internal://")
            continue
        if (
            source.crawl_type in FETCHABLE_CRAWL_TYPES
            and source.source_id not in RUNTIME_GUARDED_FETCHER_SOURCE_IDS
            and parsed.hostname not in settings.outbound_hosts
        ):
            issues.append(f"{source.source_id}: fetchable host is not in OUTBOUND_FETCH_HOSTS: {parsed.hostname}")
    return issues


@lru_cache(maxsize=1)
def load_sources() -> list[SourceConfig]:
    with REGISTRY_PATH.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    sources = [SourceConfig.model_validate(item) for item in payload]
    issues = validate_source_registry(sources)
    if issues:
        raise ValueError(f"source registry validation failed: {'; '.join(issues)}")
    return sources


def get_source(source_id: str) -> SourceConfig | None:
    return next((source for source in load_sources() if source.source_id == source_id), None)


def list_sources(tier: Tier | None = None, *, include_soft_removed: bool = False) -> list[SourceConfig]:
    sources = load_sources()
    if not include_soft_removed:
        sources = [source for source in sources if source.operational_status == "active"]
    if tier is None:
        return sources
    return [source for source in sources if source.tier == tier]

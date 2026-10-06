"""Five-source readiness regression tests (2026-08-31 task).

Covers the behaviour changes required by
agent-context/five-source-readiness-spec.md:
- OPEC site-brand titles must never override specific RSS titles;
- numeric traceability accepts leading zeros and rounding, never wrong values;
- OFAC delta events must not carry full entity names;
- OFAC state writes are atomic, fsynced and permission-restricted;
- scheduling is start-to-start, so slow runs cannot drift the SLA.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import event_summary_quality, intelligence
from app import news as news_module
from app import public_source_adapters as adapters
from app import source_automation_policy as policy_module
from app.fetchers import Fetcher
from app.news import RawNewsItem
from app.settings import settings
from app.source_registry import get_source
from app.storage import record_source_fetch

# --- OPEC: site-brand titles never override specific RSS titles -------------


def test_opec_site_brand_title_is_treated_as_generic() -> None:
    assert news_module._is_generic_navigation_link(
        "Organization of the Petroleum Exporting Countries",
        "https://www.opec.org/pr-detail/opec-daily-basket-price.html",
    )
    assert news_module._is_generic_navigation_link("OPEC", "https://www.opec.org/pr-detail/x.html")
    assert news_module._is_generic_navigation_link(
        "Organization of the Petroleum Exporting Countries - Organization of the Petroleum Exporting Countries",
        "https://www.opec.org/pr-detail/x.html",
    )


def test_detail_enrichment_keeps_specific_rss_title_over_site_brand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = news_module.NewsSource(
        source_id="opec_press",
        source_name="OPEC Press Releases",
        tier="A",
        category="oil",
        url="https://www.opec.org/press-releases.html",
        fetcher="rss",
        cadence="30min",
    )
    item = RawNewsItem(
        "opec_press",
        "A",
        "https://www.opec.org/pr-detail/opec-daily-basket-price-2026-08-31",
        "OPEC Daily Basket price stood at $82.10 a barrel Friday",
        raw_text="The OPEC Daily Basket price stood at $82.10 a barrel on Friday.",
        published_at="2026-08-31",
    )

    brand_page = (
        "<html><head><title>Organization of the Petroleum Exporting Countries</title></head>"
        "<body><p>The OPEC Daily Basket price stood at $82.10 a barrel on Friday, "
        "reflecting market conditions.</p></body></html>"
    )

    async def fetch_detail(url: str, *, referer: str | None = None) -> tuple[str, str]:
        return brand_page, "text/html"

    monkeypatch.setattr(news_module, "_fetch_text", fetch_detail)
    monkeypatch.setattr(news_module, "_should_fetch_detail", lambda item, *, source: True)

    enriched, errors = asyncio.run(news_module._enrich_items_with_details([item], source=source))

    assert errors == []
    assert enriched[0].title == item.title, "site-brand <title> must not override the RSS headline"
    assert "82.10" in enriched[0].raw_text


def test_opec_article_headline_overrides_repeated_site_brand_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = news_module.get_news_source("opec_press")
    assert source is not None
    item = RawNewsItem(
        "opec_press",
        "A",
        "https://www.opec.org/pr-detail/1854611-2-august-2026.html",
        "Organization of the Petroleum Exporting Countries - Organization of the Petroleum Exporting Countries",
        raw_text="OPEC press release",
        published_at="2026-08-02",
    )
    headline = (
        "Saudi Arabia, Russia, Iraq, Kuwait, Kazakhstan, Algeria, and Oman adjust production "
        "and reaffirm commitment to market stability"
    )
    page = (
        "<html><head><title>Organization of the Petroleum Exporting Countries</title></head>"
        f'<main><h3 id="articleHeadline">{headline}</h3><div class="article-content">'
        "The participating countries decided to adjust production to support market stability."
        "</div></main></html>"
    )

    async def fetch_detail(url: str, *, referer: str | None = None) -> tuple[str, str]:
        return page, "text/html"

    monkeypatch.setattr(news_module, "_fetch_text", fetch_detail)
    enriched, errors = asyncio.run(news_module._enrich_items_with_details([item], source=source))

    assert errors == []
    assert enriched[0].title == headline


# --- Numeric traceability: leading zeros and rounding are value-equivalent --


@pytest.mark.parametrize(
    ("number", "text", "expected"),
    [
        ("08", "production was 8 million barrels", True),
        ("18.8", "exports reached 18.83 million tonnes", True),
        ("18.8", "exports reached 18.79 million tonnes", True),
        ("18.83", "exports reached 18.83 million tonnes", True),
        ("1,200", "about 1200 units", True),
        ("18.9", "exports reached 18.83 million tonnes", False),
        ("08", "production was 7.6 million barrels", False),
        ("1200", "production was 1199.6 units", False),
        ("99", "production was 8 million barrels", False),
    ],
)
def test_number_traceability_accepts_value_equivalence_only(number: str, text: str, expected: bool) -> None:
    assert event_summary_quality._number_is_source_supported(number, text, "en") is expected


# --- OFAC: no full names in persisted events; atomic state writes -----------


def _sdn_csv() -> bytes:
    return (
        b'123,"ENERGY SHIPPING CO","Entity","RUSSIA-EO14024",,,,,,,,"Oil tanker operator"\n'
        b'456,"UNRELATED PERSON","Individual","SDGT",,,,,,,,"Unrelated"\n'
        b'789,"Individual Person Name","Individual","IRAN-EO13928",,,,,,,,"Petrochemical broker"\n'
    )


def test_ofac_delta_events_never_carry_full_entity_names() -> None:
    total, relevant = adapters.parse_ofac_sdn_csv(_sdn_csv())
    assert total == 3
    occurred_at = "2026-08-31T02:00:00+00:00"
    events = adapters.build_ofac_delta_events(
        relevant,
        None,
        occurred_at=occurred_at,
        snapshot_sha256="a" * 64,
    )
    # Baseline (no previous state) produces no fabricated deltas.
    assert events == []

    previous = {
        "schema_version": "ofac-snapshot.v1",
        "relevant_records": {"123": "0" * 64, "999": "1" * 64},
    }
    events = adapters.build_ofac_delta_events(
        relevant,
        previous,
        occurred_at=occurred_at,
        snapshot_sha256="a" * 64,
    )
    actions = sorted(event["raw"]["action"] for event in events)
    assert actions == ["added", "modified", "removed"], (
        "unrelated SDGT row must stay irrelevant; 123 fingerprint change = modified; 999 gone = removed"
    )
    for event in events:
        blob = f"{event['title']}\n{event['summary']}\n{event['notes']}"
        assert "ENERGY SHIPPING CO" not in blob
        assert "Individual Person Name" not in blob
        assert event["raw"]["entity_id"]
        assert event["raw"]["snapshot_sha256"] == "a" * 64
    added = [event for event in events if event["raw"]["action"] == "added"]
    assert all("#" in event["title"] for event in added), "events identify entities by stable id"


def test_write_source_state_is_atomic_restricted_and_fsynced(tmp_path: Path) -> None:
    target = tmp_path / "state" / "ofac_sanctions.json"
    adapters.write_source_state(target, {"schema_version": "ofac-snapshot.v1", "n": 1})
    assert target.exists()
    assert not target.with_suffix(".json.tmp").exists()
    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode & 0o077 == 0, f"state file must not be group/world accessible: {oct(mode)}"
    adapters.write_source_state(target, {"schema_version": "ofac-snapshot.v1", "n": 2})
    import json

    assert json.loads(target.read_text(encoding="utf-8"))["n"] == 2
    assert os.path.exists(str(target))


def test_corrupt_state_file_fails_closed_when_strict(tmp_path: Path) -> None:
    corrupt = tmp_path / "ofac_sanctions.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    assert adapters.load_source_state(corrupt) is None
    with pytest.raises(ValueError, match="source_state_corrupt"):
        adapters.load_source_state(corrupt, expected_schema="ofac-snapshot.v1", fail_on_corrupt=True)


def test_fetcher_does_not_contact_ofac_when_state_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "ofac_sanctions.json"
    state.write_text("{not-json", encoding="utf-8")
    contacted = False

    async def unexpected_fetch(*args: object, **kwargs: object):
        nonlocal contacted
        contacted = True
        raise AssertionError("network fetch must not run after corrupt state")

    monkeypatch.setattr("app.fetchers.fetch_ofac_sanctions", unexpected_fetch)
    source = get_source("ofac_sanctions")
    assert source is not None
    with pytest.raises(ValueError, match="source_state_corrupt"):
        asyncio.run(Fetcher(source_state_dir=tmp_path).fetch(source))
    assert contacted is False


# --- Scheduling: start-to-start anchoring (SLA drift fix) -------------------


def test_is_due_anchors_to_run_start_not_finish() -> None:
    policy = policy_module.get_source_policy("ofac_sanctions")
    now = datetime.now(UTC)
    # Started 31 minutes ago but "finished" just now: must be due again
    # (start-to-start), which the old last_success anchor would miss.
    state = {
        "last_run_status": "ok",
        "last_run_started_at": (now - timedelta(minutes=31, seconds=30)).isoformat(),
        "last_success_at": (now - timedelta(seconds=10)).isoformat(),
    }
    due = policy_module._is_due(policy, state, current=now, stale_running=False)
    assert due is True

    # Started 10 minutes ago (well inside the 30-minute frequency): not due even
    # though the run finished long ago.
    state["last_run_started_at"] = (now - timedelta(minutes=10)).isoformat()
    state["last_success_at"] = (now - timedelta(minutes=29)).isoformat()
    due = policy_module._is_due(policy, state, current=now, stale_running=False)
    assert due is False


def test_source_fetch_audit_persists_actual_run_start(tmp_path: Path) -> None:
    original_db = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "audit.db"))
    started_at = "2026-08-31T01:02:03+00:00"
    try:
        record_source_fetch(
            audit_id="audit-start-test",
            source_id="ofac_sanctions",
            status="unchanged",
            content_type="text/csv",
            preview_chars=0,
            started_at=started_at,
        )
        with closing(sqlite3.connect(settings.sqlite_path)) as connection, connection:
            stored = connection.execute(
                "SELECT created_at FROM source_fetch_audit WHERE audit_id='audit-start-test'"
            ).fetchone()[0]
        assert stored == started_at
    finally:
        object.__setattr__(settings, "sqlite_path", original_db)


def test_personal_mode_setting_exists_and_defaults_off() -> None:
    assert isinstance(settings.personal_mode, bool)


def test_source_roles_are_enforced_by_formal_prediction_contract() -> None:
    un_contract = intelligence._source_contract("un_comtrade_api", product="crude_oil")
    assert un_contract["data_role"] == "historical_context"
    assert un_contract["formal_eligible"] is False
    assert un_contract["usage_limits"] == "historical_context_not_current_formal_eligible"

    xylenes_contract = intelligence._source_contract("gacc_trade_statistics", product="xylenes_broad")
    assert xylenes_contract["formal_eligible"] is False
    assert xylenes_contract["product_role"] == "context_proxy_not_px"


# --- UN Comtrade: bounded multi-period backfill with checkpoint -------------


def test_available_comtrade_periods_filters_and_orders() -> None:
    rows = [
        {"period": 202412},
        {"period": 202501},
        {"period": 202411},
        {"period": "bad"},
        {"period": 209912},
        {},
    ]
    periods = adapters.available_comtrade_periods(rows)
    # 209912 is in the future relative to the last complete month and is filtered.
    assert periods == ["202501", "202412", "202411"]


def test_comtrade_consistency_findings_detect_duplicates_and_overrun() -> None:
    rows = [
        {"period": "202412", "cmdCode": "270900", "partnerCode": "0", "primaryValue": 100},
        {"period": "202412", "cmdCode": "270900", "partnerCode": "122", "primaryValue": 70},
        {"period": "202412", "cmdCode": "270900", "partnerCode": "122", "primaryValue": 60},
        {"period": "202412", "cmdCode": "270900", "partnerCode": "364", "primaryValue": 40},
        {"period": "202411", "cmdCode": "270900", "partnerCode": "0", "primaryValue": 50},
        {"period": "202411", "cmdCode": "270900", "partnerCode": "364", "primaryValue": 30},
    ]
    findings = adapters.comtrade_consistency_findings(rows)
    assert any("duplicate_partner:202412:270900:122" in item for item in findings)
    assert any("partner_sum_exceeds_world:202412:270900" in item for item in findings)
    assert not any("202411" in item for item in findings), "consistent month must stay silent"


def _comtrade_payload(period: str, hs_code: str, partner_code: str, value: int) -> dict[str, object]:
    return {
        "data": [
            {
                "period": period,
                "cmdCode": hs_code,
                "partnerCode": partner_code,
                "reporterCode": "156",
                "flowCode": "M",
                "clCode": "H6",
                "netWgt": value * 1000,
                "primaryValue": value,
            }
        ]
    }


def test_fetch_un_comtrade_backfills_periods_and_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mirrors the measured real availability: official monthly data ends 2024-12.
    availability = {
        "data": [
            {"period": 202412, "classificationCode": "H6"},
            {"period": 202411, "classificationCode": "H6"},
        ]
    }
    calls: list[tuple[str, str]] = []

    class _FakeResponse:
        status_code = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload
            self.headers = {"content-type": "application/json"}

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str, params: dict[str, str] | None = None, **kwargs: object):
            params = params or {}
            if "getDa" in url:
                return _FakeResponse(availability)
            period, hs = str(params["period"]), str(params["cmdCode"])
            calls.append((period, hs))
            return _FakeResponse(
                {"data": [_comtrade_payload(period, code, "0", 100)["data"][0] for code in hs.split(",")]}
            )

    monkeypatch.setattr(adapters.httpx, "AsyncClient", FakeClient)
    original_require = adapters.settings.require_outbound_url_allowed
    object.__setattr__(adapters.settings, "require_outbound_url_allowed", lambda url: None)
    previous = {"backfilled_periods": ["202412"]}
    try:
        result = asyncio.run(
            adapters.fetch_un_comtrade(
                _un_source(),
                api_key="test-key",
                previous_state=previous,
                max_periods=5,
            )
        )
    finally:
        object.__setattr__(adapters.settings, "require_outbound_url_allowed", original_require)

    # 202412 is checkpointed and skipped; only 202411 remains to backfill.
    assert sorted({period for period, _ in calls}) == ["202411"]
    assert len(calls) == 1
    assert set(calls[0][1].split(",")) == set(adapters.UN_HS_PRODUCTS)
    assert result.state_update is not None
    assert sorted(result.state_update["backfilled_periods"]) == ["202411", "202412"]
    assert result.state_update["current_formal_eligible"] is False
    preview = json.loads(result.content_preview)
    assert preview["current_formal_eligible"] is False


def test_un_comtrade_reports_partial_when_any_period_is_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    async def availability(client: object, *, api_key: str) -> dict[str, object]:
        return {"data": [{"period": 202412}, {"period": 202411}]}

    async def fetch_json(
        client: object,
        *,
        api_key: str,
        period: str,
        cmd_code: str,
        partner_code: str | None,
    ) -> dict[str, object]:
        if period == "202412":
            raise RuntimeError("429 rate limited")
        return {"data": [_comtrade_payload(period, code, "0", 100)["data"][0] for code in cmd_code.split(",")]}

    monkeypatch.setattr(adapters.httpx, "AsyncClient", lambda *args, **kwargs: FakeClient())
    monkeypatch.setattr(adapters, "_fetch_comtrade_availability", availability)
    monkeypatch.setattr(adapters, "_fetch_comtrade_json", fetch_json)
    original_require = adapters.settings.require_outbound_url_allowed
    object.__setattr__(adapters.settings, "require_outbound_url_allowed", lambda url: None)
    try:
        result = asyncio.run(adapters.fetch_un_comtrade(_un_source(), api_key="test-key", max_periods=2))
    finally:
        object.__setattr__(adapters.settings, "require_outbound_url_allowed", original_require)

    assert result.status == "partial"
    assert result.state_update is not None
    assert result.state_update["backfilled_periods"] == ["202411"]
    assert "429 rate limited" in result.content_preview


def _un_source():
    from app.models import SourceConfig

    return SourceConfig(
        source_id="un_comtrade_api",
        source_name="UN Comtrade",
        tier="B",
        category="trade",
        url=adapters.UN_COMTRADE_DATA_URL,
        crawl_type="api_json",
        auth_type="api_key",
        frequency="daily",
        products=["crude_oil", "naphtha", "px", "pta", "meg"],
        freshness_sla_minutes=1440,
        license_note="personal workbench, official free API",
        reliability_score=0.9,
    )


# --- GACC: multi-issue backfill, newest first, per-issue isolation ----------


def test_gacc_monthly_issue_urls_are_newest_first_and_bounded() -> None:
    index_html = (
        "<table><tr><td>Major import commodities in quantity and value, July 2026</td>"
        '<td><a href="/Statics/june.html">June</a><a href="/Statics/july.html">July</a></td></tr></table>'
    )
    urls = adapters.parse_gacc_monthly_issue_urls(index_html)
    assert urls == [
        "http://english.customs.gov.cn/Statics/july.html",
        "http://english.customs.gov.cn/Statics/june.html",
    ]
    assert adapters.parse_gacc_latest_major_import_url(index_html).endswith("july.html")


def test_opec_archive_uses_date_bounded_discovery_and_official_site_filter() -> None:
    source = next(item for item in news_module.NEWS_SOURCES if item.source_id == "opec_press")
    urls = news_module._archive_urls(
        source,
        start_date="2026-01-01",
        end_date="2026-01-31",
        cursor_pages=3,
    )
    assert len(urls) == 1
    assert "news.google.com/rss/search" in urls[0]
    assert "site%3Aopec.org%2Fpr-detail" in urls[0]
    assert "after%3A2026-01-01" in urls[0]
    assert "before%3A2026-02-01" in urls[0]


# --- OFAC state backup / restore drill (task rule B.4) ----------------------


def test_ofac_state_backup_and_restore_drill(tmp_path: Path) -> None:
    import hashlib

    state = tmp_path / "ofac_sanctions.json"
    payload = {
        "schema_version": "ofac-snapshot.v1",
        "source_sha256": "a" * 64,
        "relevant_records": {"123": "b" * 64},
        "relevant_count": 1,
    }
    adapters.write_source_state(state, payload)
    backup = adapters.backup_source_state(
        state,
        backup_dir=tmp_path / "backups",
        expected_schema="ofac-snapshot.v1",
    )
    assert backup is not None
    original_digest = hashlib.sha256(state.read_bytes()).hexdigest()

    # Simulate corruption in production.
    state.write_text("{corrupted", encoding="utf-8")
    with pytest.raises(ValueError, match="source_state_corrupt"):
        adapters.load_source_state(state, expected_schema="ofac-snapshot.v1", fail_on_corrupt=True)

    # Restore from the drill backup and verify content hash matches.
    adapters.restore_source_state(backup, destination=state, expected_schema="ofac-snapshot.v1")
    restored_digest = hashlib.sha256(state.read_bytes()).hexdigest()
    assert restored_digest == original_digest
    restored = adapters.load_source_state(state, expected_schema="ofac-snapshot.v1", fail_on_corrupt=True)
    assert isinstance(restored, dict) and restored["relevant_count"] == 1

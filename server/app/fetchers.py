from __future__ import annotations

import csv
import html
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx

from .models import SourceConfig
from .official_futures_daily import fetch_czce_pta_px_daily
from .public_source_adapters import (
    fetch_gacc_trade_statistics,
    fetch_ofac_sanctions,
    fetch_un_comtrade,
    load_source_state,
)
from .settings import settings
from .seven_product_contract import (
    CRUDE_EIA_SPOT_LABEL_REGISTRY_VERSION,
    CRUDE_EIA_SPOT_SERIES_ID,
)
from .textile_price_history import fetch_tnc_polyester_history


def _price_history_watermarks(source_id: str) -> dict[str, str]:
    from contextlib import closing

    from .storage import connect
    with closing(connect()) as connection:
        if source_id == "czce_pta_px":
            rows = connection.execute(
                "SELECT product,MAX(trade_date) AS day FROM futures_daily_bars "
                "WHERE source_id=? AND product IN ('PTA','PX') GROUP BY product", (source_id,),
            ).fetchall()
            return {str(row["product"]): str(row["day"]) for row in rows} if len(rows) == 2 else {}
        rows = connection.execute(
            "SELECT product,MAX(observed_at) AS day FROM market_observations "
            "WHERE source_id=? AND product IN ('poy','dty') GROUP BY product", (source_id,),
        ).fetchall()
        return {str(row["product"]): str(row["day"]) for row in rows}

EIA_POINTS_PER_SERIES_BY_FREQUENCY = {
    "daily": 420,
    "weekly": 80,
}
EIA_BRENT_PARSER_VERSION = "eia-brent-open-data.v2"
EIA_API_BASE = "https://api.eia.gov/v2/petroleum"
CFETS_CNY_PARITY_URL = "https://www.chinamoney.com.cn/r/cms/www/chinamoney/data/fx/ccpr.json"
COALCHINA_CCTD_BOHAI_RIM_DAILY_INDEX_URL = "https://www.coalchina.org.cn/list-25-1.html"
SUNSIRS_MX_EAST_CHINA_MONITOR_URL = "https://www.100ppi.com/monitor/detail-50.html"
EIA_SERIES_GROUPS: tuple[dict[str, object], ...] = (
    {
        "path": "pri/spt",
        "frequency": "daily",
        "series": {
            "RWTC": "crude_oil",
            "RBRTE": "crude_oil",
        },
    },
    # 库存（stoc/wstk）与炼厂开工/加工量（sum/sndw）序列已按运营决策于
    # 2026-09-21 下线：不再抓取入库；历史行保留只读。
)
FRED_SERIES: tuple[dict[str, str], ...] = (
    {"series_id": "DGS10", "label": "10-Year Treasury Constant Maturity Rate", "unit": "percent", "product": "macro"},
    {
        "series_id": "DTWEXBGS",
        "label": "Trade Weighted U.S. Dollar Index: Broad, Goods",
        "unit": "index",
        "product": "fx",
    },
    {
        "series_id": "DCOILWTICO",
        "label": "WTI Crude Oil Spot Price",
        "unit": "dollars_per_barrel",
        "product": "crude_oil",
    },
    {
        "series_id": "DCOILBRENTEU",
        "label": "Brent Crude Oil Spot Price",
        "unit": "dollars_per_barrel",
        "product": "crude_oil",
    },
    {"series_id": "FEDFUNDS", "label": "Effective Federal Funds Rate", "unit": "percent", "product": "macro"},
    {"series_id": "SOFR", "label": "Secured Overnight Financing Rate", "unit": "percent", "product": "macro"},
    {
        "series_id": "CPIAUCSL",
        "label": "Consumer Price Index for All Urban Consumers",
        "unit": "index",
        "product": "macro",
    },
    {
        "series_id": "PPIACO",
        "label": "Producer Price Index by Commodity: All Commodities",
        "unit": "index",
        "product": "macro",
    },
    {"series_id": "INDPRO", "label": "Industrial Production: Total Index", "unit": "index", "product": "macro"},
    {"series_id": "DEXCHUS", "label": "China / U.S. Foreign Exchange Rate", "unit": "cny_per_usd", "product": "fx"},
)
FRED_OBSERVATION_LIMIT = 420
CFTC_DISAGGREGATED_HISTORY_URL = "https://www.cftc.gov/files/dea/history/fut_disagg_txt_{year}.zip"
CFTC_HISTORY_YEARS = 2
CFTC_PETROLEUM_MARKET_KEYWORDS = (
    "CRUDE OIL",
    "BRENT",
    "WTI",
    "GASOLINE RBOB",
    "HEATING OIL",
    "ULSD",
)
CFTC_EXCLUDED_MARKET_KEYWORDS = ("PALM OIL",)


@dataclass
class FetchResult:
    source_id: str
    fetched_at: str
    status: str
    content_type: str
    content_preview: str
    observations: list[dict[str, object]] = field(default_factory=list)
    events: list[dict[str, object]] = field(default_factory=list)
    capture_revisions: list[dict[str, object]] = field(default_factory=list)
    futures_daily_bars: list[dict[str, object]] = field(default_factory=list)
    state_update: dict[str, object] | None = None
    stored_observations: int = 0
    stored_futures_daily_bars: int = 0

    def public_payload(self) -> dict[str, object]:
        """Return the stable fetch API response without internal evidence payloads."""

        return {
            "source_id": self.source_id,
            "fetched_at": self.fetched_at,
            "status": self.status,
            "content_type": self.content_type,
            "content_preview": self.content_preview,
            "observations": self.observations,
            "stored_observations": self.stored_observations,
            "futures_daily_bars": len(self.futures_daily_bars),
            "stored_futures_daily_bars": self.stored_futures_daily_bars,
        }


class Fetcher:
    """Compliance-first fetcher.

    This class intentionally does not bypass logins, paywalls, CAPTCHAs, or anti-bot
    mechanisms. Vendor sources return `requires_authorization` until an official
    connector is configured.
    """

    def __init__(self, *, source_state_dir: Path | None = None) -> None:
        self.source_state_dir = source_state_dir

    async def fetch(self, source: SourceConfig) -> FetchResult:
        if source.source_id == "ofac_sanctions":
            state_path = self.source_state_dir / "ofac_sanctions.json" if self.source_state_dir else None
            result = await fetch_ofac_sanctions(
                source,
                previous_state=(
                    load_source_state(
                        state_path,
                        expected_schema="ofac-snapshot.v1",
                        fail_on_corrupt=True,
                    )
                    if state_path
                    else None
                ),
            )
            return FetchResult(
                source_id=source.source_id,
                fetched_at=self._now(),
                status=result.status,
                content_type=result.content_type,
                content_preview=result.content_preview,
                events=result.events,
                state_update=result.state_update,
            )

        if source.source_id == "gacc_trade_statistics":
            result = await fetch_gacc_trade_statistics(source)
            return FetchResult(
                source_id=source.source_id,
                fetched_at=self._now(),
                status=result.status,
                content_type=result.content_type,
                content_preview=result.content_preview,
                observations=result.observations,
                capture_revisions=result.capture_revisions,
            )

        if source.source_id == "un_comtrade_api":
            state_path = self.source_state_dir / "un_comtrade_api.json" if self.source_state_dir else None
            result = await fetch_un_comtrade(
                source,
                api_key=settings.source_api_key(source.source_id),
                previous_state=(
                    load_source_state(
                        state_path,
                        expected_schema="un-comtrade-backfill.v1",
                        fail_on_corrupt=True,
                    )
                    if state_path
                    else None
                ),
            )
            return FetchResult(
                source_id=source.source_id,
                fetched_at=self._now(),
                status=result.status,
                content_type=result.content_type,
                content_preview=result.content_preview,
                observations=result.observations,
                state_update=result.state_update,
            )

        if source.source_id == "tnc_polyester_history":
            result = await fetch_tnc_polyester_history(
                source, known_latest_dates=_price_history_watermarks("tnc_polyester_history"),
            )
            return FetchResult(
                source_id=source.source_id,
                fetched_at=self._now(),
                status=result.status,
                content_type=result.content_type,
                content_preview=result.content_preview,
                observations=result.observations,
                capture_revisions=result.capture_revisions,
            )

        if source.source_id == "czce_pta_px":
            watermarks = _price_history_watermarks("czce_pta_px")
            days = 10
            if watermarks:
                latest = min(watermarks.values())
                today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
                days = max(2, min(31, (today - date.fromisoformat(latest)).days + 1))
            result = await fetch_czce_pta_px_daily(source, lookback_days=days)
            return FetchResult(
                source_id=source.source_id,
                fetched_at=self._now(),
                status=result.status,
                content_type=result.content_type,
                content_preview=result.content_preview,
                capture_revisions=result.capture_revisions,
                futures_daily_bars=result.bars,
            )

        if source.auth_type == "api_key":
            api_key = settings.source_api_key(source.source_id)
            if not api_key:
                return FetchResult(
                    source_id=source.source_id,
                    fetched_at=self._now(),
                    status="requires_api_key",
                    content_type="api_key",
                    content_preview=source.license_note,
                )
            if source.source_id == "eia_petroleum_api":
                return await self._fetch_eia(source, api_key)
            if source.source_id == "fred_macro_api":
                return await self._fetch_fred(source, api_key)
            return FetchResult(
                source_id=source.source_id,
                fetched_at=self._now(),
                status="api_key_configured_no_adapter",
                content_type="api_key",
                content_preview="API key is configured, but this source does not have a parser adapter yet.",
            )

        if source.auth_type == "vendor_license":
            return FetchResult(
                source_id=source.source_id,
                fetched_at=self._now(),
                status="requires_authorization",
                content_type="vendor_connector",
                content_preview=source.license_note,
            )

        if source.source_id == "cfets_cny_parity":
            return await self._fetch_cfets_cny_parity(source)

        if source.source_id == "coalchina_cctd_bohai_rim_5500_daily_reference":
            return await self._fetch_coalchina_cctd_bohai_rim_daily_reference(source)

        if source.source_id == "cftc_cot_petroleum":
            return await self._fetch_cftc_cot(source)

        if source.crawl_type in {"static_html", "html_download", "api_json"}:
            host = self._host_for_url(source.url)
            if not settings.outbound_host_allowed(host):
                return self._blocked(source, host)
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(
                    source.url,
                    headers={"User-Agent": "POY-DTY-Agent/1.0 contact=data-team@example.com"},
                )
                response.raise_for_status()
                return FetchResult(
                    source_id=source.source_id,
                    fetched_at=self._now(),
                    status="ok",
                    content_type=response.headers.get("content-type", "unknown"),
                    content_preview=response.text[: settings.max_source_preview_chars],
                )

        return FetchResult(
            source_id=source.source_id,
            fetched_at=self._now(),
            status="unsupported_crawl_type",
            content_type=source.crawl_type,
            content_preview="No fetcher configured yet.",
        )

    async def _fetch_eia(self, source: SourceConfig, api_key: str) -> FetchResult:
        if not settings.outbound_host_allowed("api.eia.gov"):
            return self._blocked(source, "api.eia.gov")
        captured_at = self._now()
        observations: list[dict[str, object]] = []
        capture_revisions: list[dict[str, object]] = []
        content_type = "application/json"
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for group in EIA_SERIES_GROUPS:
                series_map = group["series"]
                if not isinstance(series_map, dict):
                    continue
                frequency = str(group["frequency"])
                points_per_series = EIA_POINTS_PER_SERIES_BY_FREQUENCY.get(frequency, 120)
                params = [
                    ("frequency", str(group["frequency"])),
                    ("data[0]", "value"),
                    ("sort[0][column]", "period"),
                    ("sort[0][direction]", "desc"),
                    ("offset", "0"),
                    ("length", str(len(series_map) * points_per_series)),
                    ("api_key", api_key),
                ]
                params.extend(("facets[series][]", series_id) for series_id in series_map)
                endpoint = f"{EIA_API_BASE}/{group['path']}/data/"
                response = await client.get(endpoint, params=params)
                response.raise_for_status()
                content_type = response.headers.get("content-type", content_type)
                payload = response.json()
                group_observations = eia_rows_to_observations(
                    payload.get("response", {}).get("data", []),
                    source=source,
                    product_by_series={str(key): str(value) for key, value in series_map.items()},
                    frequency=frequency,
                    evidence_url=endpoint,
                )
                observations.extend(group_observations)
                capture_revisions.extend(
                    eia_brent_capture_revisions(
                        group_observations,
                        captured_at=captured_at,
                        source_url=endpoint,
                    )
                )
        preview = {
            "records": len(observations),
            "series": sorted({str(item["raw"]["series"]) for item in observations if isinstance(item["raw"], dict)}),
        }
        return FetchResult(
            source_id=source.source_id,
            fetched_at=self._now(),
            status="ok",
            content_type=content_type,
            content_preview=json.dumps(preview, ensure_ascii=False),
            observations=observations,
            capture_revisions=capture_revisions,
        )

    async def _fetch_cfets_cny_parity(self, source: SourceConfig) -> FetchResult:
        if not settings.outbound_url_allowed(CFETS_CNY_PARITY_URL):
            return self._blocked(source, self._host_for_url(CFETS_CNY_PARITY_URL))
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(
                CFETS_CNY_PARITY_URL,
                headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"},
            )
            response.raise_for_status()
            raw_content = response.content
            payload = response.json()
        captured_at = self._now()
        observation = cfets_cny_parity_to_observation(
            payload,
            source=source,
            evidence_url=CFETS_CNY_PARITY_URL,
            captured_at=captured_at,
        )
        published_at = cfets_published_at(str(observation["raw"]["source_last_date"]))
        return FetchResult(
            source_id=source.source_id,
            fetched_at=self._now(),
            status="ok",
            content_type=response.headers.get("content-type", "application/json"),
            content_preview=json.dumps(
                {
                    "series": "USD/CNY",
                    "observed_at": observation["observed_at"],
                    "value": observation["value"],
                },
                ensure_ascii=False,
            ),
            observations=[observation],
            capture_revisions=[
                {
                    "source_id": source.source_id,
                    "semantic_series_id": "fx.usd_cny.cfets.central_parity.cny_per_usd",
                    "observed_at": observation["observed_at"],
                    "published_at": published_at,
                    "visible_at": captured_at,
                    "captured_at": captured_at,
                    "source_url": CFETS_CNY_PARITY_URL,
                    "raw_sha256": sha256(raw_content).hexdigest(),
                    "authorization_scope": "public_personal_reuse",
                    "contract_version": "initial-source-evidence-contract.v1",
                    "parser_version": "cfets-cny-parity.v1",
                    "canonical_payload": observation,
                }
            ],
        )

    async def _fetch_coalchina_cctd_bohai_rim_daily_reference(self, source: SourceConfig) -> FetchResult:
        if not settings.outbound_url_allowed(COALCHINA_CCTD_BOHAI_RIM_DAILY_INDEX_URL):
            return self._blocked(source, self._host_for_url(COALCHINA_CCTD_BOHAI_RIM_DAILY_INDEX_URL))
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            index_response = await client.get(
                COALCHINA_CCTD_BOHAI_RIM_DAILY_INDEX_URL,
                headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"},
            )
            index_response.raise_for_status()
            detail_url = coalchina_cctd_bohai_rim_daily_detail_url(index_response.text)
            detail_response = await client.get(
                detail_url,
                headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"},
            )
            detail_response.raise_for_status()
        captured_at = self._now()
        try:
            observation = coalchina_cctd_bohai_rim_daily_to_observation(
                detail_response.text,
                source=source,
                evidence_url=detail_url,
                captured_at=captured_at,
            )
        except ValueError as exc:
            return FetchResult(
                source_id=source.source_id,
                fetched_at=captured_at,
                status="evidence_incomplete",
                content_type=detail_response.headers.get("content-type", "text/html"),
                content_preview=str(exc),
            )
        raw_content = index_response.content + b"\n---coalchina-detail---\n" + detail_response.content
        published_at = str(observation["raw"]["published_at"])
        return FetchResult(
            source_id=source.source_id,
            fetched_at=captured_at,
            status="ok",
            content_type=detail_response.headers.get("content-type", "text/html"),
            content_preview=json.dumps(
                {
                    "series": "CCTD Bohai-rim 5500K daily reference",
                    "observed_at": observation["observed_at"],
                    "value": observation["value"],
                },
                ensure_ascii=False,
            ),
            observations=[observation],
            capture_revisions=[
                {
                    "source_id": source.source_id,
                    "semantic_series_id": "coal.benchmark.unresolved.assessment.cny_mt",
                    "observed_at": observation["observed_at"],
                    "published_at": published_at,
                    "visible_at": captured_at,
                    "captured_at": captured_at,
                    "source_url": detail_url,
                    "raw_sha256": sha256(raw_content).hexdigest(),
                    "authorization_scope": "public_personal_reuse",
                    "contract_version": "phase-a.v6",
                    "parser_version": "coalchina-cctd-bohai-rim-5500.v1",
                    "canonical_payload": observation,
                }
            ],
        )

    async def _fetch_fred(self, source: SourceConfig, api_key: str) -> FetchResult:
        if not settings.outbound_host_allowed("api.stlouisfed.org"):
            return self._blocked(source, "api.stlouisfed.org")
        observations: list[dict[str, object]] = []
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            for series in FRED_SERIES:
                response = await client.get(
                    "https://api.stlouisfed.org/fred/series/observations",
                    params={
                        "series_id": series["series_id"],
                        "api_key": api_key,
                        "file_type": "json",
                        "sort_order": "desc",
                        "limit": str(FRED_OBSERVATION_LIMIT),
                    },
                )
                response.raise_for_status()
                payload = response.json()
                for row in payload.get("observations", []):
                    observation = fred_row_to_observation(row, source=source, series=series)
                    if observation is not None:
                        observations.append(observation)
        preview = {
            "records": len(observations),
            "series": [
                str(item["raw"]["series_id"])
                for item in observations
                if isinstance(item["raw"], dict) and item["raw"].get("series_id")
            ],
        }
        return FetchResult(
            source_id=source.source_id,
            fetched_at=self._now(),
            status="ok",
            content_type="application/json",
            content_preview=json.dumps(preview, ensure_ascii=False),
            observations=observations,
        )

    async def _fetch_cftc_cot(self, source: SourceConfig) -> FetchResult:
        current_year = datetime.now(UTC).year
        years = range(current_year - CFTC_HISTORY_YEARS + 1, current_year + 1)
        observations: list[dict[str, object]] = []
        content_type = "application/zip"
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for year in years:
                url = CFTC_DISAGGREGATED_HISTORY_URL.format(year=year)
                if not settings.outbound_url_allowed(url):
                    return self._blocked(source, self._host_for_url(url))
                response = await client.get(url, headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research"})
                response.raise_for_status()
                content_type = response.headers.get("content-type", content_type)
                observations.extend(cftc_zip_to_observations(response.content, source=source, evidence_url=url))
        preview = {
            "records": len(observations),
            "markets": sorted(
                {
                    str(item["raw"]["market"])
                    for item in observations
                    if isinstance(item["raw"], dict) and item["raw"].get("market")
                }
            )[:20],
        }
        return FetchResult(
            source_id=source.source_id,
            fetched_at=self._now(),
            status="ok",
            content_type=content_type,
            content_preview=json.dumps(preview, ensure_ascii=False),
            observations=observations,
        )

    def _blocked(self, source: SourceConfig, host: str) -> FetchResult:
        return FetchResult(
            source_id=source.source_id,
            fetched_at=self._now(),
            status="blocked_by_allowlist",
            content_type="outbound_guardrail",
            content_preview=f"Host is not in OUTBOUND_FETCH_HOSTS: {host}",
        )

    @staticmethod
    def _host_for_url(url: str) -> str:
        return settings.outbound_host_for_url(url)

    @staticmethod
    def _to_float(value: object) -> float | None:
        if value in {None, "", "."}:
            return None
        try:
            return float(str(value).replace(",", "").strip())
        except ValueError:
            return None

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()


def eia_rows_to_observations(
    rows: list[dict[str, object]],
    *,
    source: SourceConfig,
    product_by_series: dict[str, str],
    frequency: str,
    evidence_url: str,
) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    for row in rows:
        series = str(row.get("series") or "")
        value = Fetcher._to_float(row.get("value"))
        observed_at = str(row.get("period") or "")
        if not series or not observed_at or value is None:
            continue
        observations.append(
            {
                "source_id": source.source_id,
                "observed_at": observed_at,
                "indicator": str(row.get("series-description") or series),
                "product": product_by_series.get(series, "petroleum_products"),
                "value": value,
                "unit": str(row.get("units") or ""),
                "frequency": frequency,
                "region": str(row.get("area-name") or "global"),
                "evidence_url": evidence_url,
                "notes": f"Fetched from EIA Open Data API series {series}.",
                "raw": row,
            }
        )
    return observations


def eia_brent_capture_revisions(
    observations: list[dict[str, object]],
    *,
    captured_at: str,
    source_url: str,
) -> list[dict[str, object]]:
    """Build immutable EIA Brent capture facts from normalized daily rows."""

    revisions: list[dict[str, object]] = []
    for observation in observations:
        raw = observation.get("raw")
        observed_at = str(observation.get("observed_at") or "")
        source_id = str(observation.get("source_id") or "")
        if (
            source_id != "eia_petroleum_api"
            or not isinstance(raw, dict)
            or str(raw.get("series") or "") != "RBRTE"
        ):
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", observed_at) is None:
            continue
        try:
            datetime.fromisoformat(observed_at).date()
        except ValueError:
            continue
        raw_hash = sha256(
            json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        revisions.append(
            {
                "source_id": source_id,
                # EIA Brent spot is evidence only after the v5 label switch; keep
                # the EIA identity so v1-v4 forecasts still resolve, and never
                # inherit the current (futures) label series id.
                "semantic_series_id": CRUDE_EIA_SPOT_SERIES_ID,
                "observed_at": observed_at,
                # EIA v2 exposes the observation period but no row-level publication timestamp.
                # First successful capture is the conservative publication/visibility boundary.
                "published_at": captured_at,
                "visible_at": captured_at,
                "captured_at": captured_at,
                "source_url": source_url,
                "raw_sha256": raw_hash,
                "authorization_scope": "public_personal_reuse",
                "contract_version": CRUDE_EIA_SPOT_LABEL_REGISTRY_VERSION,
                "parser_version": EIA_BRENT_PARSER_VERSION,
                "canonical_payload": observation,
            }
        )
    return revisions


def fred_row_to_observation(
    row: dict[str, object],
    *,
    source: SourceConfig,
    series: dict[str, str],
) -> dict[str, object] | None:
    value = Fetcher._to_float(row.get("value"))
    observed_at = str(row.get("date") or "")
    series_id = series["series_id"]
    if not observed_at or value is None:
        return None
    return {
        "source_id": source.source_id,
        "observed_at": observed_at,
        "indicator": f"{series['label']} ({series_id})",
        "product": series["product"],
        "value": value,
        "unit": series["unit"],
        "frequency": "daily_or_release",
        "region": "United States",
        "evidence_url": source.url,
        "notes": f"Fetched from FRED series {series_id}.",
        "raw": {"series_id": series_id, **row},
    }


def cfets_cny_parity_to_observation(
    payload: dict[str, object],
    *,
    source: SourceConfig,
    evidence_url: str,
    captured_at: str,
) -> dict[str, object]:
    """Normalize CFETS's public USD/CNY central-parity record without inversion."""

    data = payload.get("data")
    records = payload.get("records")
    if not isinstance(data, dict) or not isinstance(records, list):
        raise ValueError("cfets_payload_schema_invalid")
    last_date = data.get("lastDate")
    if not isinstance(last_date, str):
        raise ValueError("cfets_last_date_invalid")
    try:
        observed_at = datetime.strptime(last_date, "%Y-%m-%d %H:%M").date().isoformat()
    except ValueError as exc:
        raise ValueError("cfets_last_date_invalid") from exc
    usd_cny = next(
        (item for item in records if isinstance(item, dict) and item.get("vrtEName") == "USD/CNY"),
        None,
    )
    if usd_cny is None:
        raise ValueError("cfets_usd_cny_record_missing")
    value = Fetcher._to_float(usd_cny.get("price"))
    if value is None or value <= 0:
        raise ValueError("cfets_usd_cny_price_invalid")
    return {
        "source_id": source.source_id,
        "observed_at": observed_at,
        "indicator": "CFETS USD/CNY central parity",
        "product": "fx",
        "value": value,
        "unit": "cny_per_usd",
        "frequency": "business_day",
        "region": "China",
        "evidence_url": evidence_url,
        "notes": "Official CFETS USD/CNY central parity candidate; not a standalone forecast target.",
        "raw": {
            "pair": "USD/CNY",
            "source_last_date": last_date,
            "captured_at": captured_at,
            "record": usd_cny,
        },
    }


def cfets_published_at(last_date: str) -> str:
    try:
        return datetime.strptime(last_date, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Shanghai")).isoformat()
    except ValueError as exc:
        raise ValueError("cfets_last_date_invalid") from exc


_COALCHINA_DAILY_LINK_PATTERN = re.compile(
    r'<a[^>]+href=["\'](?P<href>[^"\']+)["\'][^>]*>\s*'
    r"CCTD环渤海动力煤现货参考价日评（(?P<date>\d{4}年\d{1,2}月\d{1,2}日)）",
    re.IGNORECASE,
)
_COALCHINA_TITLE_DATE_PATTERN = re.compile(r"CCTD环渤海动力煤现货参考价日评（(\d{4})年(\d{1,2})月(\d{1,2})日）")
_COALCHINA_PUBLISHED_PATTERN = re.compile(
    r"发布时间[：:]\s*(\d{4})[-/]?(\d{1,2})[-/]?(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"
)
_COALCHINA_5500_TRIPLET_PATTERN = re.compile(
    r"5500\s*K\s*、\s*5000\s*K\s*、\s*4500\s*K[^。；]{0,180}?"
    r"(?:分别)?(?:收于|为|报|平均价格为)\s*(\d+(?:\.\d+)?)\s*、\s*\d+(?:\.\d+)?\s*、\s*\d+(?:\.\d+)?\s*元\s*/?\s*吨"
)
_COALCHINA_5500_SINGLE_PATTERN = re.compile(
    r"5500\s*K[^。；]{0,100}?(?:收于|为|报|平均价格为)\s*(\d+(?:\.\d+)?)\s*元\s*/?\s*吨"
)


def coalchina_cctd_bohai_rim_daily_detail_url(index_html: str) -> str:
    """Return the newest dated CCTD Bohai-rim daily-reference detail link."""

    candidates: list[tuple[datetime, str]] = []
    for match in _COALCHINA_DAILY_LINK_PATTERN.finditer(html.unescape(index_html)):
        date_parts = match.group("date").replace("年", " ").replace("月", " ").replace("日", "").split()
        year, month, day = (int(value) for value in date_parts)
        detail_url = urljoin(COALCHINA_CCTD_BOHAI_RIM_DAILY_INDEX_URL, match.group("href"))
        candidates.append((datetime(year, month, day), detail_url))
    if not candidates:
        raise ValueError("coalchina_cctd_daily_reference_link_missing")
    return max(candidates, key=lambda item: item[0])[1]


def coalchina_cctd_bohai_rim_daily_to_observation(
    content: str,
    *,
    source: SourceConfig,
    evidence_url: str,
    captured_at: str,
) -> dict[str, object]:
    """Normalize a fully evidenced CCTD Bohai-rim 5500K daily reference.

    The source is candidate-only. Missing title date, publication time, or an
    unambiguous 5500K CNY/mt value is intentionally rejected before storage.
    """

    text = html.unescape(re.sub(r"<[^>]+>", " ", content))
    text = re.sub(r"\s+", " ", text)
    title_match = _COALCHINA_TITLE_DATE_PATTERN.search(text)
    if title_match is None:
        raise ValueError("coalchina_cctd_daily_reference_date_missing")
    observed_at = (
        datetime(
            int(title_match.group(1)),
            int(title_match.group(2)),
            int(title_match.group(3)),
            tzinfo=ZoneInfo("Asia/Shanghai"),
        )
        .date()
        .isoformat()
    )
    published_match = _COALCHINA_PUBLISHED_PATTERN.search(text)
    if published_match is None:
        raise ValueError("coalchina_cctd_daily_reference_published_at_missing")
    year, month, day, hour, minute, second = (int(value or 0) for value in published_match.groups())
    published_at = datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo("Asia/Shanghai")).isoformat()
    value_match = _COALCHINA_5500_TRIPLET_PATTERN.search(text) or _COALCHINA_5500_SINGLE_PATTERN.search(text)
    if value_match is None:
        raise ValueError("coalchina_cctd_daily_reference_5500_price_missing")
    value = float(value_match.group(1))
    if value <= 0:
        raise ValueError("coalchina_cctd_daily_reference_5500_price_invalid")
    return {
        "source_id": source.source_id,
        "observed_at": observed_at,
        "indicator": "CCTD Bohai-rim thermal coal spot reference 5500K",
        "product": "coal",
        "value": value,
        "unit": "CNY/mt",
        "frequency": "business_day",
        "region": "China Bohai-rim ports",
        "evidence_url": evidence_url,
        "notes": "Candidate-only CCTD Bohai-rim 5500K daily reference; not a formal prediction input.",
        "raw": {
            "series": "CCTD Bohai-rim thermal coal spot reference",
            "grade": "5500K",
            "market": "Bohai_rim_ports",
            "quote_type": "public_spot_assessment",
            "published_at": published_at,
            "captured_at": captured_at,
        },
    }


_SUNSIRS_MX_EAST_CHINA_LINK_PATTERN = re.compile(
    r'<a[^>]+href=["\'](?P<href>[^"\']*?/news/detail-(?P<date>\d{8})-\d+\.html)["\'][^>]*>'
    r"(?P<title>[^<]*华东地区二甲苯市场价格[^<]*)</a>",
    re.IGNORECASE,
)
_SUNSIRS_MX_EAST_CHINA_TITLE_PATTERN = re.compile(r"(\d{1,2})月(\d{1,2})日华东地区二甲苯市场价格")
_SUNSIRS_MX_EAST_CHINA_PUBLISHED_PATTERN = re.compile(
    r"发布时间[：:]\s*(\d{4})[-年](\d{1,2})[-月](\d{1,2})(?:日)?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"
)
_SUNSIRS_MX_EAST_CHINA_RANGE_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*[-－至]\s*(\d+(?:\.\d+)?)\s*元\s*/\s*吨")


def sunsirs_mx_east_china_daily_detail_url(index_html: str) -> str:
    """Return the newest dated East-China mixed-xylene assessment detail link."""

    candidates: list[tuple[datetime, str]] = []
    for match in _SUNSIRS_MX_EAST_CHINA_LINK_PATTERN.finditer(html.unescape(index_html)):
        day = datetime.strptime(match.group("date"), "%Y%m%d")
        candidates.append((day, urljoin(SUNSIRS_MX_EAST_CHINA_MONITOR_URL, match.group("href"))))
    if not candidates:
        raise ValueError("sunsirs_mx_east_china_daily_link_missing")
    return max(candidates, key=lambda item: item[0])[1]


def sunsirs_mx_east_china_daily_to_observation(
    content: str,
    *,
    source: SourceConfig,
    evidence_url: str,
    captured_at: str,
) -> dict[str, object]:
    """Normalize a fully dated SunSirs East-China mixed-xylene assessment.

    The source's published range is preserved and standardized as its arithmetic
    midpoint. It is candidate-only and does not represent a transaction price.
    """

    text = html.unescape(re.sub(r"<[^>]+>", " ", content))
    text = re.sub(r"\s+", " ", text)
    title_match = _SUNSIRS_MX_EAST_CHINA_TITLE_PATTERN.search(text)
    if title_match is None:
        raise ValueError("sunsirs_mx_east_china_daily_title_missing")
    published_match = _SUNSIRS_MX_EAST_CHINA_PUBLISHED_PATTERN.search(text)
    if published_match is None:
        raise ValueError("sunsirs_mx_east_china_daily_published_at_missing")
    year, month, day, hour, minute, second = (int(value or 0) for value in published_match.groups())
    if (int(title_match.group(1)), int(title_match.group(2))) != (month, day):
        raise ValueError("sunsirs_mx_east_china_daily_observed_published_date_mismatch")
    range_match = _SUNSIRS_MX_EAST_CHINA_RANGE_PATTERN.search(text)
    if range_match is None:
        raise ValueError("sunsirs_mx_east_china_daily_range_missing")
    low, high = (float(value) for value in range_match.groups())
    if low <= 0 or high <= 0 or low > high:
        raise ValueError("sunsirs_mx_east_china_daily_range_invalid")
    value = (low + high) / 2
    observed_at = datetime(year, month, day, tzinfo=ZoneInfo("Asia/Shanghai")).date().isoformat()
    published_at = datetime(year, month, day, hour, minute, second, tzinfo=ZoneInfo("Asia/Shanghai")).isoformat()
    return {
        "source_id": source.source_id,
        "observed_at": observed_at,
        "indicator": "SunSirs East-China mixed-xylene daily assessment",
        "product": "mx",
        "value": value,
        "unit": "CNY/mt",
        "frequency": "business_day",
        "region": "East China",
        "evidence_url": evidence_url,
        "notes": (
            "Candidate-only SunSirs East-China mixed-xylene assessment, "
            "standardized as the published range midpoint; not a transaction quote or formal prediction input."
        ),
        "raw": {
            "series": "SunSirs East-China mixed-xylene daily assessment",
            "market": "East_China",
            "quote_type": "public_spot_assessment",
            "low_cny_mt": low,
            "high_cny_mt": high,
            "price_rule": "arithmetic_midpoint_of_published_range.v1",
            "published_at": published_at,
            "captured_at": captured_at,
        },
    }


def cftc_zip_to_observations(content: bytes, *, source: SourceConfig, evidence_url: str) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".txt")]
        if not names:
            return observations
        text = archive.read(names[0]).decode("latin1")
    for row in csv.DictReader(text.splitlines()):
        market = str(row.get("Market_and_Exchange_Names") or "").strip()
        if not is_cftc_petroleum_market(market):
            continue
        report_date = str(row.get("Report_Date_as_YYYY-MM-DD") or "").strip()
        if not report_date:
            continue
        observations.extend(cftc_row_to_observations(row, source=source, evidence_url=evidence_url))
    return observations


def is_cftc_petroleum_market(market: str) -> bool:
    upper = market.upper()
    if any(keyword in upper for keyword in CFTC_EXCLUDED_MARKET_KEYWORDS):
        return False
    return any(keyword in upper for keyword in CFTC_PETROLEUM_MARKET_KEYWORDS)


def cftc_row_to_observations(
    row: dict[str, str],
    *,
    source: SourceConfig,
    evidence_url: str,
) -> list[dict[str, object]]:
    report_date = str(row.get("Report_Date_as_YYYY-MM-DD") or "").strip()
    market = str(row.get("Market_and_Exchange_Names") or "").strip()
    product = cftc_product_for_market(market)
    managed_long = Fetcher._to_float(row.get("M_Money_Positions_Long_All"))
    managed_short = Fetcher._to_float(row.get("M_Money_Positions_Short_All"))
    commercial_long = Fetcher._to_float(row.get("Prod_Merc_Positions_Long_All"))
    commercial_short = Fetcher._to_float(row.get("Prod_Merc_Positions_Short_All"))
    candidates = [
        ("CFTC COT open interest", Fetcher._to_float(row.get("Open_Interest_All"))),
        ("CFTC COT managed money long", managed_long),
        ("CFTC COT managed money short", managed_short),
        (
            "CFTC COT managed money net",
            managed_long - managed_short if managed_long is not None and managed_short is not None else None,
        ),
        (
            "CFTC COT producer merchant net",
            commercial_long - commercial_short
            if commercial_long is not None and commercial_short is not None
            else None,
        ),
    ]
    observations = []
    for label, value in candidates:
        if value is None:
            continue
        observations.append(
            {
                "source_id": source.source_id,
                "observed_at": report_date,
                "indicator": f"{label} - {market}",
                "product": product,
                "value": value,
                "unit": "contracts",
                "frequency": "weekly",
                "region": "United States",
                "evidence_url": evidence_url,
                "notes": "Fetched from CFTC disaggregated futures-only historical COT file.",
                "raw": {
                    "market": market,
                    "report_date": report_date,
                    "contract_market_code": row.get("CFTC_Contract_Market_Code"),
                    "commodity_code": row.get("CFTC_Commodity_Code"),
                    "open_interest_all": row.get("Open_Interest_All"),
                    "managed_money_long_all": row.get("M_Money_Positions_Long_All"),
                    "managed_money_short_all": row.get("M_Money_Positions_Short_All"),
                    "producer_merchant_long_all": row.get("Prod_Merc_Positions_Long_All"),
                    "producer_merchant_short_all": row.get("Prod_Merc_Positions_Short_All"),
                },
            }
        )
    return observations


def cftc_product_for_market(market: str) -> str:
    upper = market.upper()
    if any(keyword in upper for keyword in ("GASOLINE", "HEATING OIL", "ULSD")):
        return "petroleum_products"
    return "crude_oil"

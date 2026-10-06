from __future__ import annotations

import json
import ssl
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser

import httpx

from .models import SourceConfig
from .public_source_adapters import StructuredSourceResult
from .settings import settings
from .seven_product_contract import CURRENT_LABEL_REGISTRY_VERSION, LABEL_REGISTRY

TNC_SOURCE_ID = "tnc_polyester_history"
TNC_PARSER_VERSION = "tnc-polyester-history.v2"
TNC_PRODUCTS = {
    "poy": {
        "label": "涤纶POY",
        "base_url": "https://www.tnc.com.cn/market/average-price-d92.html",
        "page_url": "https://www.tnc.com.cn/market/average-price-d92-p{page}.html",
    },
    "dty": {
        "label": "涤纶DTY",
        "base_url": "https://www.tnc.com.cn/market/average-price-d94.html",
        "page_url": "https://www.tnc.com.cn/market/average-price-d94-p{page}.html",
    },
}


class _TableRows(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, _attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def parse_tnc_polyester_history(
    html_text: str,
    *,
    product: str,
    evidence_url: str,
    captured_at: str,
    raw_sha256: str = "",
) -> list[dict[str, object]]:
    config = TNC_PRODUCTS.get(product)
    if config is None:
        raise ValueError("tnc_product_unsupported")
    parser = _TableRows()
    parser.feed(html_text)
    observations: list[dict[str, object]] = []
    for cells in parser.rows:
        if len(cells) < 5 or cells[0].replace(" ", "") != config["label"]:
            continue
        try:
            value = float(cells[1].replace(",", ""))
        except ValueError as exc:
            raise ValueError("tnc_price_invalid") from exc
        unit = cells[2].replace("元/吨", "CNY/mt").strip()
        observed_at = cells[4].strip()
        if unit != "CNY/mt" or len(observed_at) != 10 or value <= 0:
            raise ValueError("tnc_row_contract_invalid")
        observations.append(
            {
                "source_id": TNC_SOURCE_ID,
                "observed_at": observed_at,
                "indicator": f"{config['label']} public recent average",
                "product": product,
                "value": value,
                "unit": unit,
                "frequency": "published_day",
                "region": "China polyester public assessment",
                "evidence_url": evidence_url,
                "notes": "全球纺织网公开近期行情均价；非逐笔成交价；按页面原始日期和单位保存。",
                "raw": {
                    "captured_at": captured_at,
                    "change_text": cells[3],
                    "label": config["label"],
                    "page_url": evidence_url,
                    "raw_sha256": raw_sha256,
                    "visibility_rule": (
                        "first successful system capture; historical page is not treated as live trade data"
                    ),
                },
            }
        )
    return observations


async def fetch_tnc_polyester_history(
    source: SourceConfig,
    *,
    max_pages: int = 30,
    known_latest_dates: dict[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> StructuredSourceResult:
    if source.source_id != TNC_SOURCE_ID:
        raise ValueError("tnc_source_id_mismatch")
    if max_pages < 1 or max_pages > 100:
        raise ValueError("tnc_max_pages_invalid")
    captured_at = datetime.now(UTC).isoformat()
    own_client = client is None
    tls_context = ssl.create_default_context()
    # TNC's authenticated TLS endpoint rejects Python's narrower OpenSSL cipher
    # offer on this runtime. Keep certificate/hostname verification enabled and
    # offer only authenticated high-strength suites; never fall back to HTTP.
    tls_context.set_ciphers("HIGH:!aNULL:!eNULL")
    http_client = client or httpx.AsyncClient(verify=tls_context, timeout=20, follow_redirects=False)
    observations: list[dict[str, object]] = []
    capture_revisions: list[dict[str, object]] = []
    fetched_urls: list[str] = []
    try:
        for product, config in TNC_PRODUCTS.items():
            seen_dates: set[str] = set()
            for page in range(1, max_pages + 1):
                url = config["base_url"] if page == 1 else str(config["page_url"]).format(page=page)
                settings.require_outbound_url_allowed(url)
                response = await http_client.get(
                    url,
                    headers={"User-Agent": "POY-DTY-Agent/1.0 personal-research", "Referer": config["base_url"]},
                )
                response.raise_for_status()
                rows = parse_tnc_polyester_history(
                    response.text,
                    product=product,
                    evidence_url=url,
                    captured_at=captured_at,
                    raw_sha256=sha256(response.content).hexdigest(),
                )
                new_rows = [row for row in rows if str(row["observed_at"]) not in seen_dates]
                if not new_rows:
                    break
                observations.extend(new_rows)
                for row in new_rows:
                    raw = row.get("raw")
                    if not isinstance(raw, dict):
                        raise ValueError("tnc_capture_raw_missing")
                    raw_hash = str(raw.get("raw_sha256") or "")
                    capture_revisions.append(
                        {
                            "source_id": TNC_SOURCE_ID,
                            "semantic_series_id": LABEL_REGISTRY[product].series_id,
                            "observed_at": str(row["observed_at"]),
                            # The page exposes a date but not an exact publication timestamp.
                            # Use first successful capture for both fields rather than inventing one.
                            "published_at": captured_at,
                            "visible_at": captured_at,
                            "captured_at": captured_at,
                            "source_url": url,
                            "raw_sha256": raw_hash,
                            "authorization_scope": "public_personal_reuse",
                            "contract_version": CURRENT_LABEL_REGISTRY_VERSION,
                            "parser_version": TNC_PARSER_VERSION,
                            "canonical_payload": row,
                        }
                    )
                fetched_urls.append(url)
                seen_dates.update(str(row["observed_at"]) for row in new_rows)
                known_day = (known_latest_dates or {}).get(product)
                if known_day and min(str(row["observed_at"]) for row in new_rows) <= known_day:
                    # Re-read the overlap page to capture revisions, but do not
                    # download the entire immutable history on every hourly poll.
                    break
    finally:
        if own_client:
            await http_client.aclose()
    if not observations:
        raise ValueError("tnc_history_empty")
    return StructuredSourceResult(
        status="ok",
        content_type="text/html",
        content_preview=json.dumps(
            {
                "records": len(observations),
                "capture_revisions": len(capture_revisions),
                "products": sorted({str(row["product"]) for row in observations}),
                "pages": len(fetched_urls),
                "first_observed_at": min(str(row["observed_at"]) for row in observations),
                "last_observed_at": max(str(row["observed_at"]) for row in observations),
            },
            ensure_ascii=False,
        ),
        observations=observations,
        capture_revisions=capture_revisions,
    )

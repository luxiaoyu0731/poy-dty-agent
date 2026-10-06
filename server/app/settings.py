from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

PRODUCTION_LIKE_ENVS = {"production", "prod", "staging"}


def _csv_env(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    environment: str = os.getenv("APP_ENV", "development")
    cors_origins: tuple[str, ...] = tuple(_csv_env("CORS_ALLOW_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"))
    cors_methods: tuple[str, ...] = tuple(_csv_env("CORS_ALLOW_METHODS", "GET,POST,OPTIONS"))
    cors_headers: tuple[str, ...] = tuple(_csv_env("CORS_ALLOW_HEADERS", "Content-Type,X-Request-ID"))
    outbound_hosts: tuple[str, ...] = tuple(
        _csv_env(
            "OUTBOUND_FETCH_HOSTS",
            "api.eia.gov,www.eia.gov,opec.org,www.opec.org,www.ine.cn,english.czce.com.cn,"
            "www.chinamoney.org.cn,www.chinamoney.com.cn,api.stlouisfed.org,www.cftc.gov,"
            "sanctionslist.ofac.treas.gov,sanctionslistservice.ofac.treas.gov,ofac.treasury.gov,"
            "home.treasury.gov,www.state.gov,"
            "www.consilium.europa.eu,www.ndrc.gov.cn,www.nea.gov.cn,www.ukmto.org,"
            "www.maritime.dot.gov,english.customs.gov.cn,comtradeapi.un.org,www.whitehouse.gov,"
            "press.un.org,www.iea.org,www.federalreserve.gov,www.imo.org,www.mpa.gov.sg,"
            "news.google.com,api.gdeltproject.org,www.sse.com.cn,www.szse.cn,www.hkexnews.hk,"
            "www.cninfo.com.cn,www.centcom.mil,www.defense.gov,www.war.gov,www.dvidshub.net,"
            "www.nato.int,ec.europa.eu,www.gov.uk,www.news.uscg.mil,www.aramco.com,www.adnoc.ae,"
            "www.qatarenergy.qa,www.sinopecgroup.com,www.sinopec.com,www.cnpc.com.cn,news.cnpc.com.cn,query1.finance.yahoo.com,"
            "hq.sinajs.cn,push2.eastmoney.com,push2his.eastmoney.com,www.sunsirs.com,www.cctd.com.cn,"
            "www.coalchina.org.cn,info.texnet.com.cn,www.tnc.com.cn,www.czce.com.cn,"
            "zh.tradingeconomics.com,www.tradingeconomics.com,www.100ppi.com,"
            "dlpoy.100ppi.com,dldty.100ppi.com,"
            "www.cnbc.com,oilprice.com,finance.yahoo.com,"
            "www.visualcapitalist.com,www.eastdaley.com,"
            "www.csis.org,www.cfr.org,maritime-executive.com,"
            "www.aljazeera.com,www.theguardian.com,www.arabnews.com,"
            "earthquake.usgs.gov,www.ccfa.com.cn",
        )
    )
    max_chat_question_chars: int = int(os.getenv("MAX_CHAT_QUESTION_CHARS", "1200"))
    max_source_preview_chars: int = int(os.getenv("MAX_SOURCE_PREVIEW_CHARS", "500"))
    sqlite_path: str = os.getenv("SQLITE_PATH", "data/agent.db")
    internal_api_token: str = os.getenv("INTERNAL_API_TOKEN", "")
    local_session_secret: str = os.getenv("LOCAL_SESSION_SECRET", "")
    personal_mode: bool = _bool_env("PERSONAL_MODE", False)
    enable_local_session_auth: bool = _bool_env(
        "ENABLE_LOCAL_SESSION_AUTH",
        os.getenv("APP_ENV", "development") not in PRODUCTION_LIKE_ENVS,
    )
    local_session_ttl_seconds: int = int(os.getenv("LOCAL_SESSION_TTL_SECONDS", "43200"))
    enforce_internal_token: bool = _bool_env(
        "ENFORCE_INTERNAL_TOKEN",
        os.getenv("APP_ENV", "development") in PRODUCTION_LIKE_ENVS,
    )
    rate_limit_window_seconds: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    chat_rate_limit_per_window: int = int(os.getenv("CHAT_RATE_LIMIT_PER_WINDOW", "30"))
    source_fetch_rate_limit_per_window: int = int(os.getenv("SOURCE_FETCH_RATE_LIMIT_PER_WINDOW", "10"))
    intraday_price_scheduler_enabled: bool = _bool_env("INTRADAY_PRICE_SCHEDULER_ENABLED", True)
    intraday_price_interval_seconds: int = max(60, int(os.getenv("INTRADAY_PRICE_INTERVAL_SECONDS", "300")))
    intraday_price_initial_delay_seconds: int = max(0, int(os.getenv("INTRADAY_PRICE_INITIAL_DELAY_SECONDS", "5")))
    agent_governance_scheduler_enabled: bool = _bool_env("AGENT_GOVERNANCE_SCHEDULER_ENABLED", True)
    experience_settlement_scheduler_enabled: bool = _bool_env("EXPERIENCE_SETTLEMENT_SCHEDULER_ENABLED", False)
    experience_settlement_api_enabled: bool = _bool_env("EXPERIENCE_SETTLEMENT_API_ENABLED", False)
    eia_api_key: str = os.getenv("EIA_API_KEY", "")
    fred_api_key: str = os.getenv("FRED_API_KEY", "")
    un_comtrade_api_key: str = os.getenv("UN_COMTRADE_API_KEY", "")
    embedding_provider: str = os.getenv("EMBEDDING_PROVIDER", "fastembed")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
    embedding_model_version: str = os.getenv("EMBEDDING_MODEL_VERSION", "fastembed-0.7.4-mean-pooling")
    embedding_dimensions: int = max(1, int(os.getenv("EMBEDDING_DIMENSIONS", "384")))
    embedding_normalize: bool = _bool_env("EMBEDDING_NORMALIZE", True)
    embedding_batch_size: int = max(1, int(os.getenv("EMBEDDING_BATCH_SIZE", "32")))
    embedding_device: str = os.getenv("EMBEDDING_DEVICE", "cpu")
    embedding_timeout_seconds: float = max(0.1, float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "30")))
    embedding_fallback_policy: str = os.getenv("EMBEDDING_FALLBACK_POLICY", "hash_fallback")
    embedding_query_prefix: str = os.getenv("EMBEDDING_QUERY_PREFIX", "")
    embedding_document_prefix: str = os.getenv("EMBEDDING_DOCUMENT_PREFIX", "")
    industrial_intelligence_enabled: bool = _bool_env("INDUSTRIAL_INTELLIGENCE_ENABLED", False)
    intelligence_write_rate_limit_per_window: int = int(
        os.getenv("INTELLIGENCE_WRITE_RATE_LIMIT_PER_WINDOW", "30")
    )
    intelligence_cursor_secret: str = os.getenv("INTELLIGENCE_CURSOR_SECRET", "")
    rag_chunk_max_chars: int = max(200, int(os.getenv("RAG_CHUNK_MAX_CHARS", "900")))
    rag_chunk_overlap_chars: int = max(0, int(os.getenv("RAG_CHUNK_OVERLAP_CHARS", "120")))
    # Display-only USD->CNY anchor for the pipeline graph cost blocks. Static
    # by design (single-operator workbench): no live FX fetch, traceable via
    # the version string. Budget enforcement for the event-overview worker
    # keeps settling in native micro-USD and never reads this value.
    usd_cny_display_rate: float = max(0.0, float(os.getenv("USD_CNY_DISPLAY_RATE", "7.25")))
    usd_cny_fx_version: str = os.getenv("USD_CNY_FX_VERSION", "")

    @property
    def production_like(self) -> bool:
        return self.environment.lower() in PRODUCTION_LIKE_ENVS

    @property
    def usd_cny_display_fx_version(self) -> str:
        """Versioned static FX anchor, e.g. ``usd-cny-static-2026-09@7.25``."""

        if not self.usd_cny_fx_version:
            return f"usd-cny-static-2026-09@{self.usd_cny_display_rate:g}"
        return self.usd_cny_fx_version

    def __post_init__(self) -> None:
        if self.production_like:
            if not self.enforce_internal_token:
                raise RuntimeError("ENFORCE_INTERNAL_TOKEN must be enabled in staging/production")
            if not self.internal_api_token or self.internal_api_token == "local-dev-token":
                raise RuntimeError("INTERNAL_API_TOKEN must be a deployment secret in staging/production")
            if self.personal_mode:
                raise RuntimeError(
                    "PERSONAL_MODE must not be enabled in staging/production: reviewed evidence would "
                    "self-approve without an operator trail. Never use PERSONAL_MODE for a shared deployment."
                )

    def source_api_key(self, source_id: str) -> str:
        return {
            "eia_petroleum_api": self.eia_api_key,
            "fred_macro_api": self.fred_api_key,
            "un_comtrade_api": self.un_comtrade_api_key,
        }.get(source_id, "")

    def source_credentials_configured(self, source_id: str) -> bool:
        return bool(self.source_api_key(source_id))

    def outbound_host_allowed(self, host: str | None) -> bool:
        normalized = self.normalize_outbound_host(host)
        allowed = {self.normalize_outbound_host(item) for item in self.outbound_hosts}
        return bool(normalized and normalized in allowed)

    def outbound_url_allowed(self, url: str) -> bool:
        return self.outbound_host_allowed(self.outbound_host_for_url(url))

    def require_outbound_url_allowed(self, url: str) -> str:
        host = self.outbound_host_for_url(url)
        if not self.outbound_host_allowed(host):
            raise ValueError(f"outbound host is not in OUTBOUND_FETCH_HOSTS: {host or 'unknown'}")
        return host

    def outbound_host_for_url(self, url: str) -> str:
        return self.normalize_outbound_host(urlparse(url).hostname)

    @staticmethod
    def normalize_outbound_host(host: str | None) -> str:
        return (host or "").strip().lower().rstrip(".")


settings = Settings()

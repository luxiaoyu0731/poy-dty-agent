from __future__ import annotations

from .models import CrawlerPipeline, SourceConfig, SourceReadiness
from .settings import settings


def build_crawler_pipelines(sources: list[SourceConfig]) -> list[CrawlerPipeline]:
    by_id = {source.source_id: source for source in sources}
    return [
        CrawlerPipeline(
            pipeline_id="zero_cost_energy_market_data",
            tier="A",
            name="官方能源、宏观与交易所数据管线",
            schedule="公开源日/周/月频率，按 freshness_sla_minutes 分级",
            source_ids=[
                source_id
                for source_id in [
                    "eia_petroleum_api",
                    "opec_press",
                    "ine_sc_intraday",
                    "czce_pta_px",
                    "cfets_cny_parity",
                    "fred_macro_api",
                    "cftc_cot_petroleum",
                ]
                if source_id in by_id
            ],
            parser="api_json + html_table + pdf_digest",
            sink="market_timeseries, official_events, source_snapshots",
            status="ready",
            guardrails=["保留源链接和抓取时间", "公开官方 API 优先", "凭据型源不得经明文 HTTP", "不使用付费分钟级行情"],
        ),
        CrawlerPipeline(
            pipeline_id="zero_cost_policy_trade_events",
            tier="A",
            name="0 成本政策、制裁与贸易流管线",
            schedule="事件源 15-30 分钟检查，贸易流月度",
            source_ids=[
                source_id
                for source_id in [
                    "ofac_sanctions",
                    "gacc_trade_statistics",
                    "un_comtrade_api",
                ]
                if source_id in by_id
            ],
            parser="api_json + html_table + official_list_parser",
            sink="trade_flow, sanctions_events, policy_events",
            status="ready",
            guardrails=["只使用免费额度和公开页面", "政策事实与市场推断分栏", "贸易流 HS 编码需人工确认"],
        ),
        CrawlerPipeline(
            pipeline_id="weak_signal_human_loop",
            tier="D",
            name="弱信号与业务笔记管线",
            schedule="实时入库，进入人工复核队列",
            source_ids=[source.source_id for source in sources if source.tier == "D"],
            parser="manual_form + ocr + embedding",
            sink="internal_notes, weak_signals, review_tasks",
            status="manual_review",
            guardrails=["默认不进入高置信预测", "记录提交人和证据", "敏感判断必须二次确认"],
        ),
    ]


def assess_source_readiness(source: SourceConfig) -> SourceReadiness:
    if source.auth_type == "vendor_license":
        return SourceReadiness(
            source_id=source.source_id,
            tier=source.tier,
            status="requires_license",
            next_step="配置正式供应商 API、企业账号或导出适配器。",
        )
    if source.auth_type == "api_key":
        if settings.source_credentials_configured(source.source_id):
            return SourceReadiness(
                source_id=source.source_id,
                tier=source.tier,
                status="ready",
                next_step="所需 API 凭据已配置，可进入抓取和标准化入库流程。",
            )
        return SourceReadiness(
            source_id=source.source_id,
            tier=source.tier,
            status="requires_api_key",
            next_step="配置已获授权的 API 凭据或保持阻塞；不得绕过登录、付费墙或访问控制。",
        )
    if source.auth_type == "internal":
        return SourceReadiness(
            source_id=source.source_id,
            tier=source.tier,
            status="internal_only",
            next_step="接入内部表单、Obsidian/Nexus 同步目录或 CRM 笔记。",
        )
    return SourceReadiness(
        source_id=source.source_id,
        tier=source.tier,
        status="ready",
        next_step="公开访问配置就绪；不代表已实现采集或有效数据已入库，请核对自动采集状态与业务覆盖。",
    )

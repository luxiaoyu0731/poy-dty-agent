from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .fixtures import KNOWLEDGE_EDGES, KNOWLEDGE_NODES
from .foundation_utils import is_at_or_before
from .models import KnowledgeEdge, KnowledgeNode
from .news import news_sources
from .rag import collect_project_document_evidence, project_document_audit
from .source_registry import list_sources
from .storage import (
    list_event_intelligence_snapshots,
    list_event_observations,
    list_industry_observations,
    list_intraday_price_observations,
    list_llm_event_directions,
    list_market_observations,
    list_news_articles,
    list_news_event_clusters,
    list_prediction_ledger_records,
)

SERVER_DATA_DIR = Path(__file__).resolve().parents[1] / "data"

PRODUCTS: tuple[dict[str, str], ...] = (
    {"id": "crude_oil", "label": "原油", "summary": "最外层能源成本与风险溢价来源。"},
    {"id": "naphtha", "label": "石脑油", "summary": "PX 的关键成本输入，受原油和裂解需求影响。"},
    {"id": "PX", "label": "PX", "summary": "PTA 的直接原料，PXN 价差区分成本和自身供需。"},
    {"id": "PTA", "label": "PTA", "summary": "POY/DTY 上游成本主导节点之一。"},
    {"id": "MEG", "label": "MEG", "summary": "聚酯原料节点，库存和煤制供应影响弹性。"},
    {"id": "POY", "label": "POY", "summary": "非成交型现货评估价，代表长丝成本端观察。"},
    {"id": "DTY", "label": "DTY", "summary": "非成交型现货评估价，代表弹力丝传导结果。"},
)

PRODUCT_CHAIN: tuple[tuple[str, str, str], ...] = (
    ("crude_oil", "naphtha", "product_transmits_to_product"),
    ("naphtha", "PX", "product_transmits_to_product"),
    ("PX", "PTA", "product_cost_passes_to_product"),
    ("PTA", "POY", "product_cost_passes_to_product"),
    ("PTA", "DTY", "product_cost_passes_to_product"),
    ("MEG", "POY", "product_cost_passes_to_product"),
    ("MEG", "DTY", "product_cost_passes_to_product"),
)

STAKEHOLDERS: tuple[dict[str, object], ...] = (
    {
        "id": "opec",
        "label": "OPEC/OPEC+",
        "keywords": ["OPEC", "OPEC+", "quota", "output", "减产", "增产"],
        "capabilities": ["调产量", "供应预期管理"],
    },
    {
        "id": "eia",
        "label": "EIA",
        "keywords": ["EIA", "inventory", "forecast", "库存", "能源信息署"],
        "capabilities": ["发布库存", "发布预测"],
    },
    {
        "id": "ofac",
        "label": "OFAC/美国财政部",
        "keywords": ["OFAC", "sanction", "Treasury", "制裁"],
        "capabilities": ["制裁", "豁免"],
    },
    {
        "id": "us_government",
        "label": "美国政府/军方",
        "keywords": ["CENTCOM", "State Department", "White House", "US Navy", "U.S."],
        "capabilities": ["军事护航", "外交施压", "制裁执行"],
    },
    {
        "id": "middle_east",
        "label": "中东相关方",
        "keywords": ["Iran", "Israel", "Hormuz", "Gulf", "伊朗", "以色列", "霍尔木兹"],
        "capabilities": ["冲突升级", "和谈/停火"],
    },
    {
        "id": "shipping",
        "label": "航运与保险",
        "keywords": ["shipping", "tanker", "freight", "insurance", "航运", "油轮"],
        "capabilities": ["绕航", "调整运价", "保险定价"],
    },
    {
        "id": "refinery",
        "label": "炼厂",
        "keywords": ["refinery", "run rate", "炼厂", "开工"],
        "capabilities": ["调开工", "调整石脑油供应"],
    },
    {
        "id": "polyester_factory",
        "label": "聚酯工厂",
        "keywords": ["polyester", "POY", "DTY", "聚酯", "涤纶"],
        "capabilities": ["补库/去库", "调整开工", "压价采购"],
    },
    {
        "id": "trader",
        "label": "贸易商/资金方",
        "keywords": ["trader", "fund", "market", "risk premium", "资金", "贸易商"],
        "capabilities": ["风险交易", "库存转移"],
    },
    {
        "id": "downstream",
        "label": "下游客户",
        "keywords": ["downstream", "weaving", "textile", "织造", "终端"],
        "capabilities": ["延迟采购", "补库"],
    },
)

MECHANISMS: tuple[dict[str, object], ...] = (
    {
        "id": "risk_premium",
        "label": "风险溢价",
        "keywords": ["risk premium", "Hormuz", "war", "conflict", "风险", "冲突"],
        "polarity": "up",
    },
    {
        "id": "supply_disruption",
        "label": "供应中断",
        "keywords": ["blockade", "outage", "disruption", "封锁", "中断"],
        "polarity": "up",
    },
    {
        "id": "freight_insurance",
        "label": "运费/保险成本",
        "keywords": ["shipping", "tanker", "freight", "insurance", "航运", "保险"],
        "polarity": "up",
    },
    {"id": "inventory_pressure", "label": "库存压力", "keywords": ["inventory", "stock", "库存"], "polarity": "down"},
    {
        "id": "run_rate_change",
        "label": "开工率变化",
        "keywords": ["run rate", "operating", "开工"],
        "polarity": "neutral",
    },
    {
        "id": "demand_offset",
        "label": "需求抵消",
        "keywords": ["demand", "weak", "slow", "需求", "走弱"],
        "polarity": "down",
    },
    {
        "id": "supply_recovery",
        "label": "供应恢复",
        "keywords": ["recovery", "resume", "restore", "恢复"],
        "polarity": "down",
    },
    {
        "id": "risk_premium_decay",
        "label": "风险溢价消化",
        "keywords": ["priced in", "decay", "消化", "计价"],
        "polarity": "down",
    },
    {
        "id": "fx_pass_through",
        "label": "汇率传导",
        "keywords": ["dollar", "fx", "exchange", "美元", "汇率"],
        "polarity": "neutral",
    },
)

MACRO_FACTORS: tuple[dict[str, str], ...] = (
    {"id": "usd", "label": "美元/汇率", "summary": "影响进口成本和商品风险偏好。"},
    {"id": "rates", "label": "利率", "summary": "影响资金成本和风险资产估值。"},
    {"id": "inflation", "label": "通胀", "summary": "影响能源与化工品价格预期。"},
    {"id": "global_demand", "label": "全球需求", "summary": "决定风险事件能否传导成真实需求缺口。"},
)

ERROR_CAUSES: tuple[dict[str, str], ...] = (
    {"id": "risk_premium_decay", "label": "风险溢价消化", "summary": "事件已被价格提前计入或快速回吐。"},
    {"id": "demand_offset", "label": "需求抵消", "summary": "下游需求走弱抵消上游冲击。"},
    {"id": "supply_recovery", "label": "供应恢复", "summary": "供应恢复或增产削弱利多。"},
    {"id": "macro_offset", "label": "宏观抵消", "summary": "美元、利率、风险偏好抵消事件方向。"},
    {"id": "low_evidence", "label": "证据不足", "summary": "证据等级低、正文不足或缺少价格确认。"},
    {"id": "price_not_transmitted", "label": "价格未传导", "summary": "原油或 PX/PTA/MEG 未传导到 POY/DTY。"},
)


def graph_payload(
    *,
    q: str = "",
    product: str = "",
    evidence_level: str = "",
    include_technical: bool = False,
    limit: int = 60,
) -> dict[str, object]:
    builder = _GraphBuilder()
    _add_project_documents(builder)
    _add_quality_nodes(builder)
    _add_sources(builder)
    _add_product_chain(builder)
    _add_stakeholders_and_mechanisms(builder)
    _add_market_data(builder, product=product)
    _add_events_and_articles(builder, q=q, product=product, evidence_level=evidence_level, limit=limit)
    _add_llm_judgments(builder, q=q, limit=limit)
    _add_event_intelligence_snapshots(builder, q=q, product=product, limit=limit)
    _add_predictions_and_backtests(builder)
    _add_missing_documents(builder)
    payload = builder.payload(include_technical=include_technical)
    payload["summary"] = {
        "node_count": len(payload["nodes"]),
        "edge_count": len(payload["edges"]),
        "layers": _layer_counts(payload["nodes"]),
        "missing_documents": [item for item in project_document_audit() if item["status"] == "missing_document"],
    }
    payload["warnings"] = _graph_warnings(payload)
    return payload


def search_knowledge(query: str) -> dict[str, list[dict[str, object]]]:
    payload = graph_payload(q=query, limit=30)
    return {"nodes": payload["nodes"], "edges": payload["edges"]}  # type: ignore[index]


def node_detail(node_id: str) -> dict[str, object]:
    payload = graph_payload(limit=120, include_technical=True)
    nodes = {node["id"]: node for node in payload["nodes"]}  # type: ignore[index]
    if node_id not in nodes:
        return {
            "node": None,
            "edges": [],
            "evidence": [],
            "citations": [],
            "warnings": ["node_not_found"],
        }
    related_edges = [
        edge
        for edge in payload["edges"]  # type: ignore[index]
        if edge["source"] == node_id or edge["target"] == node_id
    ]
    related_ids = {edge["source"] for edge in related_edges} | {edge["target"] for edge in related_edges}
    related_ids.discard(node_id)
    related_nodes = [nodes[item] for item in related_ids if item in nodes]
    return {
        "node": nodes[node_id],
        "edges": related_edges,
        "related_nodes": related_nodes[:24],
        "evidence": _node_evidence(node_id, related_edges, nodes),
        "citations": _node_citations(nodes[node_id], related_nodes),
        "warnings": _node_warnings(nodes[node_id]),
    }


def graph_path(
    *,
    q: str = "",
    node_id: str = "",
    product: str = "POY",
    limit: int = 60,
) -> dict[str, object]:
    payload = graph_payload(q=q, product=product, include_technical=True, limit=limit)
    nodes = {node["id"]: node for node in payload["nodes"]}  # type: ignore[index]
    selected_id = node_id or _first_matching_node(nodes, q)
    selected = nodes.get(selected_id) if selected_id else None
    upstream_path = _product_path(product)
    stakeholder_paths = _paths_from_edges(payload, selected_id, relation_prefix="stakeholder_")
    counter_paths = _paths_from_edges(payload, selected_id, target_prefix="error:")
    prediction_paths = _paths_from_edges(payload, selected_id, target_prefix="prediction:")
    backtest_paths = _paths_from_edges(payload, selected_id, target_prefix="backtest:")
    return {
        "selected_node": selected,
        "nodes": payload["nodes"],
        "edges": payload["edges"],
        "upstream_path": upstream_path,
        "stakeholder_paths": stakeholder_paths,
        "counter_evidence_paths": counter_paths,
        "prediction_paths": prediction_paths,
        "backtest_paths": backtest_paths,
        "missing_data": payload["summary"]["missing_documents"],  # type: ignore[index]
        "warnings": payload["warnings"],
    }


def similar_cases(q: str = "", limit: int = 12, as_of_time: str | None = None) -> dict[str, object]:
    terms = _terms(q)
    judgments = list_llm_event_directions(limit=300)
    cases: list[dict[str, object]] = []
    for row in judgments:
        if as_of_time and not is_at_or_before(row["as_of_time"], as_of_time):
            continue
        text = f"{row['title']} {row['category']} {row['reasoning']} {row['counter_evidence']}".lower()
        score = sum(1 for term in terms if term and term.lower() in text)
        if q and score == 0:
            continue
        cases.append(
            {
                "case_id": row["judgment_id"],
                "event_id": row["event_id"],
                "title": row["title"],
                "as_of_time": row["as_of_time"],
                "category": row["category"],
                "llm_direction": row["llm_direction"],
                "confidence": row["confidence"],
                "evidence_level": row["evidence_level"],
                "reasoning": row["reasoning"],
                "counter_evidence": row["counter_evidence"],
                "cited_doc_ids": row["cited_doc_ids"],
                "similarity_score": score,
                "future_safe": True,
            }
        )
    cases.sort(key=lambda item: (item["similarity_score"], item["confidence"]), reverse=True)
    return {"query": q, "as_of_time": as_of_time, "items": cases[: min(max(limit, 1), 50)]}


def upstream_context(node_ids: list[str] | None = None) -> str:
    if not node_ids:
        selected_nodes: list[KnowledgeNode] = KNOWLEDGE_NODES
        selected_edges: list[KnowledgeEdge] = KNOWLEDGE_EDGES
    else:
        selected = set(node_ids)
        selected_nodes = [node for node in KNOWLEDGE_NODES if node.node_id in selected]
        selected_edges = [edge for edge in KNOWLEDGE_EDGES if edge.source_id in selected or edge.target_id in selected]

    node_text = "\n".join(f"- {node.label}: {node.summary}" for node in selected_nodes)
    edge_text = "\n".join(
        f"- {edge.source_id} -> {edge.target_id}: {edge.relation}, {edge.polarity}, confidence={edge.confidence}"
        for edge in selected_edges
    )
    mechanism_text = "\n".join(f"- {item['label']}: {item['polarity']}" for item in MECHANISMS)
    stakeholder_text = "\n".join(f"- {item['label']}: {', '.join(item['capabilities'])}" for item in STAKEHOLDERS)
    return (
        f"知识节点：\n{node_text}\n\n传导关系：\n{edge_text}\n\n"
        f"传导机制：\n{mechanism_text}\n\n利益相关方行动能力：\n{stakeholder_text}"
    )


class _GraphBuilder:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, object]] = {}
        self.edges: dict[str, dict[str, object]] = {}

    def add_node(
        self,
        node_id: str,
        *,
        label: str,
        node_type: str,
        layer: str,
        summary: str = "",
        tier: str = "B",
        status: str = "available",
        observed_at: str = "",
        url: str = "",
        metadata: dict[str, object] | None = None,
    ) -> None:
        if node_id in self.nodes:
            existing = self.nodes[node_id]
            existing["count"] = int(existing.get("count", 1)) + 1
            return
        self.nodes[node_id] = {
            "id": node_id,
            "node_id": node_id,
            "label": label,
            "type": node_type,
            "node_type": node_type,
            "layer": layer,
            "summary": summary,
            "tier": tier,
            "evidence_level": tier,
            "status": status,
            "observed_at": observed_at,
            "url": url,
            "count": 1,
            "metadata": metadata or {},
        }

    def add_edge(
        self,
        source: str,
        target: str,
        relation: str,
        *,
        label: str = "",
        polarity: str = "neutral",
        confidence: float = 0.6,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if source not in self.nodes or target not in self.nodes:
            return
        edge_id = f"{source}->{relation}->{target}"
        self.edges[edge_id] = {
            "id": edge_id,
            "source": source,
            "target": target,
            "source_id": source,
            "target_id": target,
            "relation": relation,
            "label": label or relation,
            "polarity": polarity,
            "confidence": confidence,
            "metadata": metadata or {},
        }

    def payload(self, *, include_technical: bool) -> dict[str, list[dict[str, object]]]:
        nodes = list(self.nodes.values())
        edges = list(self.edges.values())
        if not include_technical:
            for node in nodes:
                node["metadata"] = _public_metadata(node["metadata"])
            for edge in edges:
                edge["metadata"] = _public_metadata(edge["metadata"])
        return {"nodes": nodes, "edges": edges}


def _add_project_documents(builder: _GraphBuilder) -> None:
    builder.add_node(
        "system:rag_boundary",
        label="RAG 判断边界",
        node_type="SystemPolicy",
        layer="项目规则",
        summary="DeepSeek 必须先检索 RAG，缺证据时降级，C/D 级证据不得高置信。",
        tier="A",
    )
    for document in collect_project_document_evidence():
        builder.add_node(
            document.doc_id,
            label=document.title,
            node_type=str(document.metadata.get("doc_kind", "Document")),
            layer="项目规则",
            summary=document.summary[:360],
            tier=document.tier,
            observed_at=document.observed_at,
            url=document.url,
            metadata=document.metadata,
        )
        builder.add_edge(
            document.doc_id,
            "system:rag_boundary",
            _document_relation(str(document.metadata.get("doc_kind", ""))),
            label="定义系统规则",
            confidence=0.94,
        )


def _add_missing_documents(builder: _GraphBuilder) -> None:
    for item in project_document_audit():
        if item["status"] != "missing_document":
            continue
        builder.add_node(
            str(item["doc_id"]),
            label=str(item["title"]),
            node_type=str(item["doc_kind"]),
            layer="项目规则",
            summary=f"文档缺失：{item['path']}，未进入 RAG 检索。",
            tier=str(item["tier"]),
            status="missing_document",
            metadata={"path": item["path"]},
        )


def _add_sources(builder: _GraphBuilder) -> None:
    for source in list_sources():
        builder.add_node(
            f"source:{source.source_id}",
            label=source.source_name,
            node_type="Source",
            layer="数据源",
            summary=f"{source.category}；频率 {source.frequency}；授权 {source.auth_type}",
            tier=source.tier,
            url=source.url,
            metadata={"products": source.products, "freshness_sla_minutes": source.freshness_sla_minutes},
        )
        builder.add_edge(
            f"source:{source.source_id}",
            f"quality:{source.tier}",
            "source_has_quality",
            confidence=source.reliability_score,
        )
    for source in news_sources():
        builder.add_node(
            f"news_source:{source.source_id}",
            label=source.source_name,
            node_type="Source",
            layer="数据源",
            summary=f"{source.category}；抓取 {source.fetcher}；频率 {source.cadence}",
            tier=source.tier,
            url=source.url,
            metadata={"category": source.category, "fetcher": source.fetcher},
        )
        builder.add_edge(
            f"news_source:{source.source_id}", f"quality:{source.tier}", "source_has_quality", confidence=0.72
        )


def _add_quality_nodes(builder: _GraphBuilder) -> None:
    for tier, summary in {
        "A": "官方或强授权证据，可支撑较高置信判断。",
        "B": "准官方/稳定公开源，需要交叉验证。",
        "C": "公开新闻或页面线索，只能作为弱信号。",
        "D": "人工慢队列或低确定性信息，必须人工确认。",
    }.items():
        builder.add_node(
            f"quality:{tier}",
            label=f"{tier}级证据",
            node_type="EvidenceQuality",
            layer="证据质量",
            summary=summary,
            tier=tier,
        )


def _add_product_chain(builder: _GraphBuilder) -> None:
    for item in PRODUCTS:
        builder.add_node(
            f"product:{item['id']}",
            label=item["label"],
            node_type="Product",
            layer="产品链路",
            summary=item["summary"],
            tier="A" if item["id"] in {"PTA", "MEG"} else "B",
        )
    for source, target, relation in PRODUCT_CHAIN:
        builder.add_edge(
            f"product:{source}",
            f"product:{target}",
            relation,
            label="成本传导",
            polarity="up",
            confidence=0.84,
        )
    for item in MACRO_FACTORS:
        builder.add_node(
            f"macro:{item['id']}",
            label=item["label"],
            node_type="MacroFactor",
            layer="宏观因子",
            summary=item["summary"],
            tier="B",
        )
        builder.add_edge(f"macro:{item['id']}", "product:crude_oil", "macro_factor_affects_product", confidence=0.64)


def _add_stakeholders_and_mechanisms(builder: _GraphBuilder) -> None:
    for item in STAKEHOLDERS:
        stakeholder_id = f"stakeholder:{item['id']}"
        builder.add_node(
            stakeholder_id,
            label=str(item["label"]),
            node_type="Stakeholder",
            layer="利益相关方",
            summary=f"行动能力：{', '.join(item['capabilities'])}",
            tier="B",
            metadata={"keywords": item["keywords"], "capabilities": item["capabilities"]},
        )
        for capability in item["capabilities"]:
            capability_id = f"capability:{_slug(str(capability))}"
            builder.add_node(
                capability_id,
                label=str(capability),
                node_type="StakeholderCapability",
                layer="行动能力",
                summary=f"{item['label']} 可通过该能力改变事件传导路径。",
                tier="B",
            )
            builder.add_edge(
                stakeholder_id, capability_id, "stakeholder_has_capability", label="具备能力", confidence=0.72
            )
    for item in MECHANISMS:
        mechanism_id = f"mechanism:{item['id']}"
        builder.add_node(
            mechanism_id,
            label=str(item["label"]),
            node_type="TransmissionMechanism",
            layer="传导机制",
            summary=f"对产品价格的基础方向：{item['polarity']}",
            tier="B",
            metadata={"keywords": item["keywords"], "polarity": item["polarity"]},
        )
        if item["polarity"] == "up":
            builder.add_edge(
                mechanism_id,
                "product:crude_oil",
                "mechanism_pushes_product_up",
                label="推升",
                polarity="up",
                confidence=0.68,
            )
        elif item["polarity"] == "down":
            builder.add_edge(
                mechanism_id,
                "product:crude_oil",
                "mechanism_pushes_product_down",
                label="压制",
                polarity="down",
                confidence=0.62,
            )
        else:
            builder.add_edge(
                mechanism_id, "product:crude_oil", "mechanism_offsets_product_move", label="改变弹性", confidence=0.58
            )
    for item in ERROR_CAUSES:
        builder.add_node(
            f"error:{item['id']}",
            label=item["label"],
            node_type="ErrorCause",
            layer="错因与权重",
            summary=item["summary"],
            tier="B",
        )


def _add_market_data(builder: _GraphBuilder, *, product: str) -> None:
    market_rows = list_market_observations(limit=80)
    industry_rows = list_industry_observations(product=product or None, limit=80)
    intraday_rows = list_intraday_price_observations(instrument=product or None, limit=80)
    for row in market_rows[:40]:
        _add_price_node(builder, row, prefix="market", instrument=str(row["product"]), metric=str(row["indicator"]))
    for row in industry_rows[:60]:
        _add_price_node(builder, row, prefix="industry", instrument=str(row["product"]), metric=str(row["metric"]))
    for row in intraday_rows[:40]:
        _add_price_node(
            builder, row, prefix="intraday", instrument=str(row["instrument"]), metric=str(row["price_type"])
        )


def _add_price_node(builder: _GraphBuilder, row: dict[str, Any], *, prefix: str, instrument: str, metric: str) -> None:
    product_id = _product_node_id(instrument)
    value = row.get("value", row.get("last"))
    unit = row.get("unit", "")
    node_id = f"price:{prefix}:{row['observation_id']}"
    builder.add_node(
        node_id,
        label=f"{instrument} {metric}",
        node_type="PriceObservation",
        layer="价格观测",
        summary=f"{row.get('observed_at', '')}；{metric}={value if value is not None else '待补'} {unit}",
        tier=str(row.get("evidence_level") or "B"),
        observed_at=str(row.get("observed_at", "")),
        url=str(row.get("evidence_url") or row.get("source_url") or ""),
        metadata={"quote_type": row.get("price_type", "spot_public_valuation"), "metric": metric},
    )
    if product_id:
        builder.add_edge(node_id, product_id, "product_price_observed_by", label="价格观测", confidence=0.74)


def _add_events_and_articles(
    builder: _GraphBuilder,
    *,
    q: str,
    product: str,
    evidence_level: str,
    limit: int,
) -> None:
    clusters = list_news_event_clusters(tier=evidence_level or None, limit=min(max(limit * 3, 120), 360))
    if q:
        clusters = [
            row for row in clusters if _query_relevance(q, f"{row['title']} {row['summary']} {row['category']}") > 0
        ][:limit]
    articles = list_news_articles(limit=min(max(limit * 4, 160), 480))
    article_by_id = {row["article_id"]: row for row in articles}
    for cluster in clusters:
        if product and product not in cluster.get("affected_products", []):
            continue
        event_id = f"event_cluster:{cluster['cluster_id']}"
        builder.add_node(
            event_id,
            label=cluster["title"],
            node_type="EventCluster",
            layer="新闻事件",
            summary=cluster["summary"],
            tier=cluster["evidence_level"],
            observed_at=cluster["updated_at"],
            metadata={
                "category": cluster["category"],
                "direction": cluster["direction"],
                "impact_strength": cluster["impact_strength"],
                "affected_products": cluster["affected_products"],
            },
        )
        builder.add_edge(event_id, f"quality:{cluster['evidence_level']}", "evidence_supports_event", confidence=0.7)
        for source_id in cluster.get("source_ids", [])[:4]:
            builder.add_edge(
                f"news_source:{source_id}", event_id, "source_provides_article", label="来源提供", confidence=0.72
            )
        _connect_text_to_domain(builder, event_id, f"{cluster['title']} {cluster['summary']} {cluster['category']}")
        for product_name in cluster.get("affected_products", [])[:8]:
            product_id = _product_node_id(str(product_name))
            if product_id:
                relation = (
                    "event_pushes_product_up"
                    if cluster["direction"] == "利多"
                    else "event_pushes_product_down"
                    if cluster["direction"] == "利空"
                    else "event_affects_product"
                )
                builder.add_edge(
                    event_id,
                    product_id,
                    relation,
                    label=cluster["direction"],
                    polarity=_direction_polarity(cluster["direction"]),
                    confidence=float(cluster.get("heat_score", 0.5)) / 100
                    if float(cluster.get("heat_score", 0.5)) > 1
                    else 0.58,
                )
        for article_id in cluster.get("article_ids", [])[:5]:
            article = article_by_id.get(article_id)
            if article is not None:
                article_node = _add_article_node(builder, article)
                builder.add_edge(article_node, event_id, "article_mentions_event", label="归并事件", confidence=0.75)
    for row in list_event_observations(limit=min(limit, 100)):
        event_id = f"event:{row['event_record_id']}"
        builder.add_node(
            event_id,
            label=row["title"],
            node_type="EventObservation",
            layer="新闻事件",
            summary=row["summary"],
            tier=row["evidence_level"],
            observed_at=row["occurred_at"],
            url=row["evidence_url"],
            metadata={
                "event_type": row["event_type"],
                "direction": row["direction"],
                "affected_products": row["affected_products"],
            },
        )
        _connect_text_to_domain(
            builder, event_id, f"{row['title']} {row['summary']} {row['event_type']} {row['notes']}"
        )


def _add_article_node(builder: _GraphBuilder, row: dict[str, Any]) -> str:
    node_id = f"article:{row['article_id']}"
    builder.add_node(
        node_id,
        label=row["title"],
        node_type="NewsArticle",
        layer="新闻事件",
        summary=row["summary"],
        tier=row["tier"],
        observed_at=row["published_at"] or row["first_seen_at"],
        url=row["url"],
        metadata={"category": row["category"], "content_quality": _content_quality(row)},
    )
    builder.add_edge(f"news_source:{row['source_id']}", node_id, "source_provides_article", confidence=0.7)
    _connect_text_to_domain(builder, node_id, f"{row['title']} {row['summary']} {row['raw_text'][:500]}")
    return node_id


def _add_llm_judgments(builder: _GraphBuilder, *, q: str, limit: int) -> None:
    for row in list_llm_event_directions(limit=min(max(limit, 20), 200)):
        if (
            q
            and _query_relevance(
                q,
                f"{row['title']} {row['reasoning']} {row['counter_evidence']}",
            )
            <= 0
        ):
            continue
        node_id = f"llm:{row['judgment_id']}"
        builder.add_node(
            node_id,
            label=f"{row['llm_direction']}：{row['title'][:48]}",
            node_type="LlmJudgment",
            layer="LLM 判断",
            summary=row["reasoning"],
            tier=row["evidence_level"],
            observed_at=row["as_of_time"],
            metadata={
                "llm_direction": row["llm_direction"],
                "confidence": row["confidence"],
                "counter_evidence": row["counter_evidence"],
                "cited_doc_ids": row["cited_doc_ids"],
                "should_enter_backtest": row["should_enter_backtest"],
            },
        )
        event_node = f"event_cluster:{row['event_id']}"
        if event_node in builder.nodes:
            builder.add_edge(
                node_id, event_node, "llm_judges_event", label="判断事件", confidence=float(row["confidence"])
            )
        _connect_text_to_domain(builder, node_id, f"{row['title']} {row['reasoning']} {row['counter_evidence']}")
        for error_id in _error_causes_from_judgment(row):
            builder.add_edge(
                node_id, f"error:{error_id}", "judgment_has_counter_evidence", label="反证/错因", confidence=0.68
            )


def _add_event_intelligence_snapshots(builder: _GraphBuilder, *, q: str, product: str, limit: int) -> None:
    for row in list_event_intelligence_snapshots(limit=min(max(limit, 20), 200)):
        searchable = " ".join(
            [
                str(row.get("snapshot_id", "")),
                str(row.get("event_id", "")),
                str(row.get("title", "")),
                str(row.get("event_summary", "")),
                str(row.get("surface_narrative", "")),
                str(row.get("category", "")),
            ]
        ).lower()
        if q and _query_relevance(q, searchable) <= 0:
            continue
        affected_products = [str(item) for item in row.get("affected_products", [])]
        if product and product not in affected_products:
            continue
        node_id = f"intelligence:{row['snapshot_id']}"
        builder.add_node(
            node_id,
            label=f"事件智能：{str(row['title'])[:48]}",
            node_type="EventIntelligenceSnapshot",
            layer="事件智能快照",
            summary=str(row.get("event_summary") or row.get("surface_narrative") or ""),
            tier=_snapshot_tier(row),
            observed_at=str(row.get("as_of_time", "")),
            metadata={
                "event_id": row.get("event_id", ""),
                "category": row.get("category", ""),
                "facts": row.get("facts", []),
                "inferences": row.get("inferences", []),
                "hypotheses": row.get("hypotheses", []),
                "disconfirming_signals": row.get("disconfirming_signals", []),
                "analysis_boundary": "model_analysis_snapshot_not_raw_fact",
            },
        )
        event_node = (
            f"event:{row['event_id']}"
            if row.get("source_record_type") == "event_observation"
            else f"event_cluster:{row['event_id']}"
        )
        if event_node in builder.nodes:
            builder.add_edge(node_id, event_node, "snapshot_analyzes_event", label="分析事件", confidence=0.72)
        _connect_text_to_domain(
            builder,
            node_id,
            " ".join(
                [
                    str(row.get("title", "")),
                    str(row.get("event_summary", "")),
                    str(row.get("surface_narrative", "")),
                    _items_text(row.get("stakeholders", [])),
                    _items_text(row.get("supply_chain_paths", [])),
                ]
            ),
        )
        directions = row.get("expected_direction_by_product", {})
        if isinstance(directions, dict):
            for product_name, direction in directions.items():
                product_id = _product_node_id(str(product_name))
                if product_id:
                    builder.add_edge(
                        node_id,
                        product_id,
                        "snapshot_hypothesizes_product_direction",
                        label=str(direction),
                        polarity=_direction_polarity(str(direction)),
                        confidence=0.48,
                        metadata={"boundary": "hypothesis_not_fact"},
                    )


def _add_predictions_and_backtests(builder: _GraphBuilder) -> None:
    for row in list_prediction_ledger_records(limit=80):
        node_id = f"prediction:{row['prediction_id']}"
        builder.add_node(
            node_id,
            label=f"{row['target']} {row['direction']}",
            node_type="PredictionRecord",
            layer="预测复盘",
            summary=f"{row['horizon']}；置信度 {row['confidence']}；{row['rationale']}",
            tier="C",
            observed_at=row["created_at"],
            metadata={"review_status": row["review_status"], "tags": row["tags"]},
        )
        builder.add_edge(
            "system:rag_boundary", node_id, "judgment_enters_prediction", label="进入预测账本", confidence=0.56
        )
        for product_id in ("product:POY", "product:DTY"):
            builder.add_edge(node_id, product_id, "prediction_compared_with_price", label="复盘目标", confidence=0.54)
    backtest = _read_report("llm-backtest-2025-06-16-to-2026-06-15.json")
    if isinstance(backtest, dict):
        summary = backtest.get("summary", backtest)
        node_id = "backtest:llm_2025_2026"
        builder.add_node(
            node_id,
            label="LLM-only 真实价格回测",
            node_type="BacktestResult",
            layer="预测复盘",
            summary=f"已评分 {summary.get('scored_events', '待确认')}；命中率 {summary.get('hit_rate', '待确认')}",
            tier="B",
            metadata={"summary": _public_metadata(summary)},
        )
        for node in list(builder.nodes):
            if node.startswith("prediction:"):
                builder.add_edge(node, node_id, "prediction_compared_with_price", label="后验评分", confidence=0.62)
        for error in ERROR_CAUSES:
            builder.add_edge(
                node_id, f"error:{error['id']}", "backtest_explains_error", label="解释错因", confidence=0.58
            )


def _connect_text_to_domain(builder: _GraphBuilder, source_id: str, text: str) -> None:
    lowered = text.lower()
    for stakeholder in STAKEHOLDERS:
        if any(str(keyword).lower() in lowered for keyword in stakeholder["keywords"]):
            target = f"stakeholder:{stakeholder['id']}"
            builder.add_edge(source_id, target, "event_affects_stakeholder", label="影响相关方", confidence=0.66)
            builder.add_edge(target, source_id, "stakeholder_can_amplify_event", label="可放大/缓和", confidence=0.52)
    for mechanism in MECHANISMS:
        if any(str(keyword).lower() in lowered for keyword in mechanism["keywords"]):
            builder.add_edge(
                source_id,
                f"mechanism:{mechanism['id']}",
                "event_triggers_transmission_mechanism",
                label="触发机制",
                confidence=0.64,
            )
    for product in PRODUCTS:
        if product["label"].lower() in lowered or product["id"].lower() in lowered:
            builder.add_edge(
                source_id, f"product:{product['id']}", "event_affects_product", label="影响产品", confidence=0.56
            )


def _document_relation(doc_kind: str) -> str:
    if doc_kind == "PriceFreshnessPolicy":
        return "document_defines_price_freshness"
    if doc_kind == "BacktestPolicy":
        return "document_defines_backtest_rule"
    if doc_kind in {"RagPolicy", "SystemPolicy"}:
        return "document_defines_rag_boundary"
    if doc_kind in {"ApiContract"}:
        return "document_defines_policy"
    return "document_describes_system"


def _product_node_id(value: str) -> str:
    normalized = value.lower()
    aliases = {
        "brent": "product:crude_oil",
        "wti": "product:crude_oil",
        "crude": "product:crude_oil",
        "crude_oil": "product:crude_oil",
        "原油": "product:crude_oil",
        "石脑油": "product:naphtha",
        "naphtha": "product:naphtha",
        "px": "product:PX",
        "pta": "product:PTA",
        "meg": "product:MEG",
        "poy": "product:POY",
        "dty": "product:DTY",
    }
    for key, node_id in aliases.items():
        if key in normalized:
            return node_id
    return ""


def _direction_polarity(direction: str) -> str:
    if direction == "利多":
        return "up"
    if direction == "利空":
        return "down"
    return "neutral"


def _snapshot_tier(row: dict[str, Any]) -> str:
    evidence_quality = row.get("evidence_quality")
    if isinstance(evidence_quality, dict):
        tier = str(evidence_quality.get("tier") or evidence_quality.get("evidence_level") or "").upper()
        if tier in {"A", "B", "C", "D"}:
            return tier
    return "C"


def _items_text(value: object) -> str:
    if not isinstance(value, list):
        return ""
    parts: list[str] = []
    for item in value[:8]:
        if isinstance(item, dict):
            parts.extend(str(candidate) for candidate in item.values() if isinstance(candidate, str))
        elif isinstance(item, str):
            parts.append(item)
    return " ".join(parts)


def _content_quality(row: dict[str, Any]) -> str:
    raw_text = str(row.get("raw_text", ""))
    if len(raw_text) > 800:
        return "body_enriched"
    if len(raw_text) > 160:
        return "summary_level"
    return "title_only"


def _error_causes_from_judgment(row: dict[str, Any]) -> list[str]:
    causes: list[str] = []
    if row.get("risk_premium_decay"):
        causes.append("risk_premium_decay")
    if row.get("demand_weakness_offset"):
        causes.append("demand_offset")
    if row.get("supply_recovery_offset"):
        causes.append("supply_recovery")
    if row.get("evidence_level") in {"C", "D"}:
        causes.append("low_evidence")
    text = f"{row.get('reasoning', '')} {row.get('counter_evidence', '')}".lower()
    if "美元" in text or "dollar" in text or "macro" in text:
        causes.append("macro_offset")
    return sorted(set(causes)) or ["low_evidence"]


def _read_report(filename: str) -> object | None:
    path = SERVER_DATA_DIR / "backfill_reports" / filename
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _public_metadata(metadata: object) -> dict[str, object]:
    if not isinstance(metadata, dict):
        return {}
    hidden = {"raw", "raw_text", "provider", "token", "cursor", "html", "http_status"}
    return {str(key): value for key, value in metadata.items() if str(key).lower() not in hidden}


def _layer_counts(nodes: object) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(nodes, list):
        return counts
    for node in nodes:
        if isinstance(node, dict):
            layer = str(node.get("layer", "unknown"))
            counts[layer] = counts.get(layer, 0) + 1
    return counts


def _graph_warnings(payload: dict[str, object]) -> list[str]:
    warnings: list[str] = []
    missing = (
        payload.get("summary", {}).get("missing_documents", []) if isinstance(payload.get("summary"), dict) else []
    )
    if missing:
        warnings.append("部分项目文档缺失，未进入 RAG。")
    layers = payload.get("summary", {}).get("layers", {}) if isinstance(payload.get("summary"), dict) else {}
    if isinstance(layers, dict) and not layers.get("错因与权重"):
        warnings.append("错因节点不足，复盘解释可能不完整。")
    return warnings


def _node_evidence(
    node_id: str, edges: list[dict[str, object]], nodes: dict[str, dict[str, object]]
) -> list[dict[str, object]]:
    evidence_types = {"NewsArticle", "EventCluster", "EventObservation", "PriceObservation", "ProjectDocument"}
    result = []
    for edge in edges:
        for endpoint in (edge["source"], edge["target"]):
            if endpoint == node_id:
                continue
            node = nodes.get(str(endpoint))
            if node and node.get("type") in evidence_types:
                result.append(node)
    return result[:16]


def _node_citations(node: dict[str, object], related_nodes: list[dict[str, object]]) -> list[dict[str, object]]:
    citations = []
    for item in [node, *related_nodes]:
        if item.get("url"):
            citations.append({"title": item["label"], "url": item["url"], "tier": item["tier"]})
    return citations[:12]


def _node_warnings(node: dict[str, object]) -> list[str]:
    warnings: list[str] = []
    if node.get("tier") in {"C", "D"}:
        warnings.append("低等级证据，需要交叉验证或人工确认。")
    if node.get("status") == "missing_document":
        warnings.append("文档缺失，未进入 RAG 检索。")
    if node.get("type") == "PriceObservation" and not node.get("url"):
        warnings.append("价格观测缺少来源链接。")
    return warnings


def _first_matching_node(nodes: dict[str, dict[str, object]], q: str) -> str:
    if not q:
        return ""
    lowered = q.lower()
    for node_id, node in nodes.items():
        if lowered in f"{node.get('label', '')} {node.get('summary', '')}".lower():
            return node_id
    return ""


def _paths_from_edges(
    payload: dict[str, object],
    selected_id: str,
    *,
    relation_prefix: str = "",
    target_prefix: str = "",
) -> list[dict[str, object]]:
    if not selected_id:
        return []
    result = []
    for edge in payload["edges"]:  # type: ignore[index]
        if edge["source"] != selected_id and edge["target"] != selected_id:
            continue
        if relation_prefix and not str(edge["relation"]).startswith(relation_prefix):
            continue
        if target_prefix and not (
            str(edge["source"]).startswith(target_prefix) or str(edge["target"]).startswith(target_prefix)
        ):
            continue
        result.append(edge)
    return result[:20]


def _product_path(product: str) -> list[str]:
    if product.upper() == "DTY":
        return ["原油", "石脑油", "PX", "PTA", "DTY"]
    if product.upper() == "POY":
        return ["原油", "石脑油", "PX", "PTA", "POY"]
    return ["原油", "石脑油", "PX", "PTA/MEG", "POY/DTY"]


def _terms(query: str) -> list[str]:
    lowered = query.lower()
    terms = re.findall(r"[a-z0-9_+\-]{2,}", lowered)
    domain_terms = [
        str(item["label"]).lower()
        for item in [*PRODUCTS, *MECHANISMS, *STAKEHOLDERS]
        if str(item["label"]).lower() in lowered
    ]
    chinese_blocks = re.findall(r"[\u4e00-\u9fff]{2,}", query)
    chinese_terms: list[str] = []
    for block in chinese_blocks:
        if len(block) <= 8:
            chinese_terms.append(block)
        chinese_terms.extend(block[index : index + 2] for index in range(len(block) - 1))
    return list(dict.fromkeys([*terms, *domain_terms, *chinese_terms]))


def _query_relevance(query: str, text: str) -> int:
    lowered = text.lower()
    return sum(1 for term in _terms(query) if term and term in lowered)


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value.lower()).strip("-")[:80]

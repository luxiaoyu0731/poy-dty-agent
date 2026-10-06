from __future__ import annotations

import re
from collections import Counter
from contextlib import closing
from typing import Any

from .foundation_utils import (
    is_at_or_before,
    json_dumps,
    json_loads,
    new_id,
    now_iso,
    safe_summary,
    stable_hash,
)
from .storage import (
    connect,
    get_evidence_review_map,
    list_agent_runs,
    list_forecast_price_points,
    list_industry_observations,
    list_llm_traces,
    list_market_observations,
    list_news_event_clusters,
)

MEMORY_TYPES = ("working", "episodic", "semantic", "perceptual")
MEMORY_TYPE_LABELS = {
    "working": "工作记忆",
    "episodic": "情景记忆",
    "semantic": "语义记忆",
    "perceptual": "感知记忆",
}
MEMORY_PROJECTION_VERSION = "memory-projection-v2"
MODEL_GENERATED_SOURCES = {"llm_traces", "agent_runs", "runtime"}


class MemoryManager:
    """Business memory facade over existing operational tables.

    The manager stores a sanitized memory projection and can rebuild it from the
    source-of-truth tables. Raw vendor credentials or large payloads are not
    copied into the projection.
    """

    def sync(self, *, limit: int = 200, archive_missing: bool = False) -> dict[str, Any]:
        items = _project_existing_records(limit=limit)
        now = now_iso()
        projected_ids = {item["item_id"] for item in items}
        with closing(connect()) as connection, connection:
            for item in items:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO memory_items (
                      item_id, created_at, updated_at, memory_type, title, summary, source_table, source_id,
                      product, observed_at, evidence_level, payload, metadata
                    ) VALUES (
                      ?,
                      COALESCE((SELECT created_at FROM memory_items WHERE item_id = ?), ?),
                      ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        item["item_id"],
                        item["item_id"],
                        now,
                        now,
                        item["memory_type"],
                        item["title"],
                        item["summary"],
                        item["source_table"],
                        item["source_id"],
                        item["product"],
                        item["observed_at"],
                        item["evidence_level"],
                        json_dumps(item.get("payload", {})),
                        json_dumps(item.get("metadata", {})),
                    ),
                )
            archived = 0
            if archive_missing:
                rows = connection.execute("SELECT item_id, metadata FROM memory_items").fetchall()
                for row in rows:
                    if row["item_id"] in projected_ids:
                        continue
                    metadata = json_loads(row["metadata"], {})
                    if metadata.get("generation_version") != MEMORY_PROJECTION_VERSION or metadata.get("archived"):
                        continue
                    metadata["archived"] = True
                    metadata["archived_at"] = now
                    connection.execute(
                        "UPDATE memory_items SET updated_at = ?, metadata = ? WHERE item_id = ?",
                        (now, json_dumps(metadata), row["item_id"]),
                    )
                    archived += 1
        return {
            "synced": len(items),
            "archived": archived,
            "memory_types": dict(Counter(item["memory_type"] for item in items)),
            "generation_version": MEMORY_PROJECTION_VERSION,
        }

    def summary(self) -> dict[str, Any]:
        with closing(connect()) as connection:
            rows = connection.execute(
                """
                SELECT memory_type, COUNT(*) AS count, MAX(observed_at) AS latest_observed_at
                FROM memory_items
                GROUP BY memory_type
                """
            ).fetchall()
        counts = {memory_type: 0 for memory_type in MEMORY_TYPES}
        latest = {memory_type: "" for memory_type in MEMORY_TYPES}
        for row in rows:
            counts[row["memory_type"]] = int(row["count"])
            latest[row["memory_type"]] = row["latest_observed_at"] or ""
        return {
            "items": [
                {
                    "memory_type": memory_type,
                    "label": MEMORY_TYPE_LABELS[memory_type],
                    "count": counts[memory_type],
                    "latest_observed_at": latest[memory_type],
                }
                for memory_type in MEMORY_TYPES
            ],
            "total": sum(counts.values()),
        }

    def list_items(
        self,
        *,
        memory_type: str | None = None,
        product: str | None = None,
        evidence_level: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if memory_type:
            where.append("memory_type = ?")
            params.append(memory_type)
        if product:
            where.append("(product = ? OR product = '')")
            params.append(product)
        if evidence_level:
            where.append("evidence_level = ?")
            params.append(evidence_level)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(min(max(limit, 1), 500))
        with closing(connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM memory_items
                {clause}
                ORDER BY observed_at DESC, created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [_row_to_memory_item(row) for row in rows]

    def get_item(self, item_id: str) -> dict[str, Any] | None:
        with closing(connect()) as connection:
            row = connection.execute("SELECT * FROM memory_items WHERE item_id = ?", (item_id,)).fetchone()
        return _row_to_memory_item(row) if row else None

    def query(
        self,
        *,
        query: str,
        product: str | None = None,
        as_of_time: str | None = None,
        memory_types: tuple[str, ...] | None = None,
        limit: int = 12,
    ) -> dict[str, Any]:
        """Retrieve memory by relevance while enforcing generation-time visibility.

        Memory is context, not automatically adopted evidence. In particular,
        model/agent generated records remain ineligible as factual evidence.
        """
        if memory_types == ():
            return {
                "query": query,
                "as_of_time": as_of_time or "",
                "items": [],
                "item_ids": [],
                "retrieval_mode": "disabled_ablation",
                "generation_version": MEMORY_PROJECTION_VERSION,
                "future_filtered_count": 0,
                "expired_filtered_count": 0,
                "archived_filtered_count": 0,
                "rejected_filtered_count": 0,
                "warnings": ["memory_disabled_for_ablation"],
            }

        terms = _memory_terms(f"{query} {product or ''}")
        with closing(connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM memory_items ORDER BY observed_at DESC, created_at DESC LIMIT 1000"
            ).fetchall()
        candidate_doc_ids = [str(json_loads(row["metadata"], {}).get("doc_id") or "") for row in rows]
        review_map = get_evidence_review_map(doc_id for doc_id in candidate_doc_ids if doc_id)
        scored: list[tuple[float, dict[str, Any]]] = []
        future_filtered = 0
        expired_filtered = 0
        archived_filtered = 0
        rejected_filtered = 0
        for row in rows:
            item = _row_to_memory_item(row)
            metadata = item["metadata"]
            if metadata.get("archived"):
                archived_filtered += 1
                continue
            if memory_types and item["memory_type"] not in memory_types:
                continue
            doc_id = str(metadata.get("doc_id") or "")
            if doc_id and review_map.get(doc_id, {}).get("status") == "rejected":
                rejected_filtered += 1
                continue
            item_products = [
                candidate.strip().lower()
                for candidate in re.split(r"[/,\s]+", str(item["product"]))
                if candidate.strip()
            ]
            if (
                product
                and item_products
                and product.lower() not in item_products
                and not any(candidate in query.lower() for candidate in item_products)
            ):
                continue
            visible_at = str(metadata.get("visible_at") or item["observed_at"] or item["created_at"])
            generated_at = str(metadata.get("generated_at") or item["created_at"])
            if as_of_time and (
                not is_at_or_before(visible_at, as_of_time) or not is_at_or_before(generated_at, as_of_time)
            ):
                future_filtered += 1
                continue
            expires_at = str(metadata.get("expires_at") or "")
            effective_time = as_of_time or now_iso()
            if expires_at and expires_at < effective_time:
                expired_filtered += 1
                continue
            text = f"{item['title']} {item['summary']} {item['product']}".lower()
            relevance = sum(1.0 for term in terms if term in text)
            if query.strip() and relevance <= 0:
                continue
            freshness_bonus = 0.2 if item["memory_type"] in {"working", "perceptual"} else 0.0
            tier_bonus = {"A": 0.4, "B": 0.3, "C": 0.1, "D": 0.0}.get(item["evidence_level"], 0.0)
            item["relevance_score"] = round(relevance + freshness_bonus + tier_bonus, 3)
            trust_boundary = str(metadata.get("trust_boundary") or _trust_boundary(item["source_table"]))
            item["trust_boundary"] = trust_boundary
            item["visible_at"] = visible_at
            item["generation_version"] = str(metadata.get("generation_version") or "legacy_unversioned")
            item["evidence_eligible"] = trust_boundary == "external_evidence" and item["evidence_level"] in {
                "A",
                "B",
                "C",
            }
            scored.append((item["relevance_score"], item))
        scored.sort(
            key=lambda pair: (pair[0], pair[1]["observed_at"], pair[1]["created_at"]),
            reverse=True,
        )
        selected = [item for _, item in scored[: min(max(limit, 1), 100)]]
        return {
            "query": query,
            "as_of_time": as_of_time or "",
            "items": selected,
            "item_ids": [item["item_id"] for item in selected],
            "retrieval_mode": "relevance_time_filtered",
            "generation_version": MEMORY_PROJECTION_VERSION,
            "future_filtered_count": future_filtered,
            "expired_filtered_count": expired_filtered,
            "archived_filtered_count": archived_filtered,
            "rejected_filtered_count": rejected_filtered,
            "warnings": (
                (["部分 Memory 因晚于 as_of_time 已被隔离。"] if future_filtered else [])
                + (["已排除人工拒绝的 Memory 来源。"] if rejected_filtered else [])
            ),
        }


def _project_existing_records(*, limit: int) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []

    for run in list_agent_runs(limit=min(limit, 80)):
        items.append(
            _item(
                memory_type="episodic",
                source_table="agent_runs",
                source_id=run["run_id"],
                title=run["name"],
                summary=f"{run['agent_name']}：{run['goal']}；状态 {run['status']}",
                observed_at=run["updated_at"],
                payload={"status": run["status"], "trace_type": run["trace_type"]},
            )
        )

    for trace in list_llm_traces(limit=min(limit, 80)):
        items.append(
            _item(
                memory_type="episodic",
                source_table="llm_traces",
                source_id=trace["trace_id"],
                title="AI 回答记录",
                summary=f"{trace['question']}；证据等级 {trace['evidence_level']}；置信度 {trace['confidence']}",
                observed_at=trace["created_at"],
                evidence_level=trace["evidence_level"],
                payload={"fallback": trace["fallback"], "cited_source_ids": trace["cited_source_ids"]},
            )
        )

    for row in list_news_event_clusters(limit=min(limit, 100)):
        items.append(
            _item(
                memory_type="perceptual",
                source_table="news_event_clusters",
                source_id=row["cluster_id"],
                title=row["title"],
                summary=f"{row['summary']}；方向 {row['direction']}；影响 {row['impact_strength']}",
                product="/".join(row["affected_products"][:4]) if row["affected_products"] else "",
                observed_at=row.get("updated_at") or row.get("created_at") or "",
                evidence_level=row["evidence_level"],
                payload={"category": row["category"], "article_count": len(row["article_ids"])},
            )
        )

    for row in list_market_observations(limit=min(limit, 100)):
        value = "" if row["value"] is None else f"{row['value']} {row['unit']}"
        items.append(
            _item(
                memory_type="perceptual",
                source_table="market_observations",
                source_id=row["observation_id"],
                title=f"{row['product']} {row['indicator']}",
                summary=f"{row['observed_at']}：{row['indicator']} {value}；{row['notes']}",
                product=row["product"],
                observed_at=row["observed_at"],
                evidence_level="B",
                payload={"indicator": row["indicator"], "frequency": row["frequency"]},
            )
        )

    for row in list_industry_observations(limit=min(limit, 100)):
        value = "" if row["value"] is None else f"{row['value']} {row['unit']}"
        items.append(
            _item(
                memory_type="perceptual",
                source_table="industry_observations",
                source_id=row["observation_id"],
                title=f"{row['product']} {row['metric']}",
                summary=f"{row['observed_at']}：{row['metric']} {value}；{row['market']} {row['region']}",
                product=row["product"],
                observed_at=row["observed_at"],
                evidence_level=row["evidence_level"],
                payload={"metric": row["metric"], "market": row["market"]},
            )
        )

    for row in list_forecast_price_points(limit=min(limit, 120)):
        items.append(
            _item(
                memory_type="perceptual",
                source_table="forecast_price_points",
                source_id=row["point_id"],
                title=f"{row['product']} {row['spec']} 价格",
                summary=f"{row['observed_at']}：{row['price']} {row['unit']}；{row['dataset_type']}",
                product=row["product"],
                observed_at=row["observed_at"],
                evidence_level="B",
                payload={"dataset_type": row["dataset_type"], "quote_type": row["quote_type"]},
            )
        )

    items.append(
        _item(
            memory_type="semantic",
            source_table="domain_model",
            source_id="polyester_chain",
            title="聚酯产业链传导",
            summary="原油 -> 石脑油 -> PX -> PTA/MEG -> POY/DTY 是当前研判的核心成本传导链。",
            observed_at=now_iso(),
            evidence_level="B",
            payload={"chain": ["原油", "石脑油", "PX", "PTA", "MEG", "POY", "DTY"]},
        )
    )
    items.append(
        _item(
            memory_type="working",
            source_table="runtime",
            source_id="current_quality_context",
            title="当前研判工作上下文",
            summary="工作记忆由最新数据覆盖、质量门禁、RAG 检索结果和 Agent 运行状态组成。",
            observed_at=now_iso(),
            evidence_level="C",
            payload={"scope": "current_decision_context"},
        )
    )
    return items


def _item(
    *,
    memory_type: str,
    source_table: str,
    source_id: str,
    title: str,
    summary: str,
    product: str = "",
    observed_at: str = "",
    evidence_level: str = "C",
    payload: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item_id = f"mem_{stable_hash({'table': source_table, 'id': source_id, 'type': memory_type})}"
    resolved_metadata = dict(metadata or {})
    resolved_metadata.setdefault("visible_at", observed_at or now_iso())
    resolved_metadata.setdefault("generated_at", now_iso())
    resolved_metadata.setdefault("generation_version", MEMORY_PROJECTION_VERSION)
    resolved_metadata.setdefault("trust_boundary", _trust_boundary(source_table))
    resolved_metadata.setdefault("doc_id", _source_doc_id(source_table, source_id))
    resolved_metadata.setdefault(
        "content_hash",
        stable_hash(
            {
                "source_table": source_table,
                "source_id": source_id,
                "title": title,
                "summary": summary,
            }
        ),
    )
    resolved_metadata.setdefault("retention_policy", _retention_policy(memory_type))
    return {
        "item_id": item_id,
        "memory_type": memory_type,
        "title": safe_summary(title, max_chars=160) or source_id,
        "summary": safe_summary(summary, max_chars=600),
        "source_table": source_table,
        "source_id": source_id,
        "product": product,
        "observed_at": observed_at or now_iso(),
        "evidence_level": evidence_level if evidence_level in {"A", "B", "C", "D"} else "C",
        "payload": payload or {},
        "metadata": resolved_metadata,
    }


def _row_to_memory_item(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = json_loads(item.get("payload"), {})
    item["metadata"] = json_loads(item.get("metadata"), {})
    item["label"] = MEMORY_TYPE_LABELS.get(item["memory_type"], item["memory_type"])
    return item


def create_memory_snapshot(*, name: str, memory_type: str, query: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot_id = new_id("memory_snapshot")
    now = now_iso()
    payload = {"count": len(items), "summaries": [safe_summary(item.get("summary", "")) for item in items[:20]]}
    with closing(connect()) as connection, connection:
        connection.execute(
            """
            INSERT INTO memory_snapshots (
              snapshot_id, created_at, name, memory_type, query, items, payload, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot_id,
                now,
                name,
                memory_type,
                query,
                json_dumps([item["item_id"] for item in items]),
                json_dumps(payload),
                json_dumps({}),
            ),
        )
    return {"snapshot_id": snapshot_id, "created_at": now, "name": name, "memory_type": memory_type, **payload}


def _memory_terms(text: str) -> list[str]:
    ascii_terms = re.findall(r"[a-z0-9_+\-]{2,}", text.lower())
    chinese_blocks = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    chinese_terms: list[str] = []
    for block in chinese_blocks:
        chinese_terms.extend(block[index : index + 2] for index in range(max(1, len(block) - 1)))
    return list(dict.fromkeys([*ascii_terms, *chinese_terms]))


def _trust_boundary(source_table: str) -> str:
    if source_table in MODEL_GENERATED_SOURCES:
        return "model_generated" if source_table == "llm_traces" else "system_runtime"
    if source_table == "domain_model":
        return "system_rule"
    return "external_evidence"


def _retention_policy(memory_type: str) -> str:
    return {
        "working": "replace_on_sync",
        "episodic": "archive_when_source_missing",
        "semantic": "versioned_until_replaced",
        "perceptual": "archive_when_source_missing",
    }.get(memory_type, "archive_when_source_missing")


def _source_doc_id(source_table: str, source_id: str) -> str:
    prefix = {
        "news_event_clusters": "news_event",
        "market_observations": "market",
        "industry_observations": "industry",
        "forecast_price_points": "ccf_spot",
    }.get(source_table)
    return f"{prefix}:{source_id}" if prefix else ""

from __future__ import annotations

import hashlib
from contextlib import closing
from itertools import count
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.agent_trace_ledger import agent_run_trace, create_agent_turn
from app.context_pack import build_context_pack
from app.graphrag_reasoning import build_reasoning_path
from app.graphrag_store import materialize_graph_snapshot
from app.main import app, build_rag_visual_workbench
from app.memory import MemoryManager
from app.rag_index import rebuild_rag_index, retrieve_index_chunks
from app.settings import settings
from app.storage import bulk_upsert_forecast_price_points, upsert_evidence_review


@pytest.fixture()
def isolated_db(tmp_path: Path):
    original_sqlite_path = settings.sqlite_path
    original_enforce = settings.enforce_internal_token
    original_token = settings.internal_api_token
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "agent-foundation.db"))
    object.__setattr__(settings, "enforce_internal_token", False)
    object.__setattr__(settings, "internal_api_token", "")
    yield Path(settings.sqlite_path)
    object.__setattr__(settings, "sqlite_path", original_sqlite_path)
    object.__setattr__(settings, "enforce_internal_token", original_enforce)
    object.__setattr__(settings, "internal_api_token", original_token)


def test_foundation_schema_v9_tables_exist(isolated_db: Path) -> None:
    with closing(storage.connect()) as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual table')"
            ).fetchall()
        }
        migrations = {
            row["version"]: row["name"]
            for row in connection.execute("SELECT version, name FROM schema_migrations").fetchall()
        }
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])

    assert user_version == storage.SCHEMA_VERSION == 39
    assert migrations[22] == "data_source_governance_and_reconciliation"
    assert migrations[28] == storage.SOURCE_CAPTURE_REVISION_MIGRATION_NAME
    assert migrations[29] == storage.FORMAL_EVIDENCE_V2_MIGRATION_NAME
    assert migrations[30] == storage.FORECAST_CAPTURE_LINEAGE_MIGRATION_NAME
    assert migrations[31] == storage.AGENT_GOVERNANCE_REPORT_MIGRATION_NAME
    assert migrations[32] == storage.SEQUENTIAL_REPLAY_CHECKPOINT_MIGRATION_NAME
    assert migrations[34] == storage.FUTURES_PROJECTION_IDENTITY_MIGRATION_NAME
    assert "source_capture_revisions" in tables
    assert migrations[20] == "event_summary_quality_fields"
    assert migrations[19] == "daily_judgement_snapshots"
    assert "daily_judgement_snapshots" in tables
    assert migrations[18] == "event_ai_summaries"
    assert "event_ai_summaries" in tables
    assert migrations[7] == "agent_foundation_memory_rag_graph_trace"
    assert migrations[8] == "political_case_memory"
    assert migrations[9] == "futures_daily_bars"
    assert {
        "memory_items",
        "rag_documents",
        "rag_chunks",
        "context_packs",
        "graph_nodes",
        "graph_edges",
        "agent_turns",
        "agent_io_records",
        "agent_tool_calls",
        "agent_handoffs",
    } <= tables


def test_memory_rag_context_and_graphrag_roundtrip(isolated_db: Path) -> None:
    MemoryManager().sync(limit=120)
    memory_summary = MemoryManager().summary()
    assert memory_summary["total"] >= 2

    missing_chunks = retrieve_index_chunks("POY DTY 原料链", limit=3)
    assert missing_chunks["status"] == "missing_index"
    assert missing_chunks["count"] == 0


def test_graph_snapshot_returns_hash_of_persisted_payload(isolated_db: Path) -> None:
    snapshot = materialize_graph_snapshot(question="POY DTY", product="POY", limit=5)
    with closing(storage.connect()) as connection:
        persisted = connection.execute(
            "SELECT payload FROM graph_snapshots WHERE snapshot_id = ?", (snapshot["snapshot_id"],)
        ).fetchone()[0]
    assert snapshot["payload_sha256"] == hashlib.sha256(persisted.encode("utf-8")).hexdigest()

    # Build enough of the compatibility corpus to include customer-visible
    # business evidence; the first rows are intentionally internal documents
    # and must stay outside the Assistant allowlist.
    index = rebuild_rag_index(limit=200)
    assert index["document_count"] >= 1
    chunks = retrieve_index_chunks("POY DTY 原料链", limit=3)
    assert chunks["count"] >= 1

    pack = build_context_pack("POY DTY 原料链如何传导？", persist=True)
    assert pack["pack_id"].startswith("context_pack_")
    assert pack["evidence_ids"]
    assert pack["metadata"]["prompt_version"]

    snapshot = materialize_graph_snapshot(question="POY DTY 原料链", product="POY", limit=20)
    assert snapshot["node_count"] >= 1
    path = build_reasoning_path(question="POY DTY 原料链", product="POY")
    assert path["path_id"].startswith("graph_path_")


def test_rag_index_excludes_rejected_evidence(isolated_db: Path) -> None:
    index = rebuild_rag_index(limit=20)
    assert index["document_count"] >= 1
    chunks = retrieve_index_chunks("POY DTY 原料链", limit=5)
    assert chunks["count"] >= 1
    rejected_document_id = chunks["items"][0]["document_id"]

    upsert_evidence_review(
        doc_id=rejected_document_id,
        status="rejected",
        reviewer="pytest",
        notes="rejected index evidence should not be retrieved",
    )

    after_reject = retrieve_index_chunks("POY DTY 原料链", limit=20)
    assert rejected_document_id not in {item["document_id"] for item in after_reject["items"]}


def test_limited_rag_rebuild_does_not_truncate_existing_index(isolated_db: Path) -> None:
    full_index = rebuild_rag_index()
    assert full_index["status"] == "ready"
    assert full_index["document_count"] > 1

    blocked = rebuild_rag_index(limit=1)

    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "partial_rag_rebuild_would_truncate_existing_index"
    chunks = retrieve_index_chunks("POY DTY 原料链", limit=5)
    assert chunks["status"] == "ready"
    assert chunks["count"] >= 1


def test_rag_index_retrieves_unsegmented_chinese_query_from_real_chunks(isolated_db: Path) -> None:
    rebuild_rag_index()

    result = retrieve_index_chunks("原油石脑油成本传导", limit=6)

    assert result["status"] == "ready"
    assert result["count"] >= 1
    assert result["metadata"]["candidate_count"] >= result["count"]
    assert result["metadata"]["retrieval_mode"] in {"fts", "hybrid_semantic"}
    returned_text = " ".join(f"{item['title']} {item['text']}" for item in result["items"])
    assert any(term in returned_text.lower() for term in ("原油", "石脑油", "成本", "传导"))


def test_rag_visual_is_a_single_query_scoped_index_snapshot(isolated_db: Path) -> None:
    rebuild_rag_index()

    oil = build_rag_visual_workbench(question="原油石脑油成本传导", product="POY", limit=6)
    sanctions = build_rag_visual_workbench(question="PTA POY 成本传导", product="POY", limit=6)

    assert oil["summary"]["entered_count"] >= 1
    assert sanctions["summary"]["entered_count"] >= 1
    assert oil["summary"]["candidate_count"] >= oil["summary"]["entered_count"]
    assert sanctions["summary"]["candidate_count"] >= sanctions["summary"]["entered_count"]
    oil_ids = {item["id"] for item in oil["evidence_buckets"]["adopted"]}
    sanctions_ids = {item["id"] for item in sanctions["evidence_buckets"]["adopted"]}
    assert oil_ids != sanctions_ids


def test_rag_corpus_indexes_only_authorized_ccf_spot_price_points(isolated_db: Path) -> None:
    rows = []
    for product, price in (("PX", 8500), ("PTA", 6100), ("MEG", 4550), ("POY", 7600), ("DTY", 8900)):
        rows.append(
            {
                "source_id": "ccf_dom_daily",
                "dataset_type": "ccf_spot",
                "observed_at": "2026-07-10",
                "company": "CCF",
                "product": product,
                "spec": f"{product} 日均价",
                "price": price,
                "unit": "CNY/mt",
                "quote_type": "daily_average",
                "notes": f"授权 CCF {product} 现货日均价",
                "raw": {"source_url": "https://www.ccf.com.cn/datacenter/price.php"},
            }
        )
    rows.append(
        {
            "source_id": "akshare_prototype",
            "dataset_type": "ccf_spot",
            "observed_at": "2026-07-10",
            "company": "prototype",
            "product": "PTA",
            "spec": "PTA prototype",
            "price": 9999,
            "unit": "CNY/mt",
            "quote_type": "daily_average",
            "notes": "prototype must not enter formal evidence",
            "raw": {},
        }
    )
    for offset in range(4):
        rows.append(
            {
                "source_id": "ccf_dom_daily",
                "dataset_type": "ccf_spot",
                "observed_at": f"2026-07-0{9 - offset}",
                "company": "CCF",
                "product": "MEG",
                "spec": f"MEG 历史日均价 {offset}",
                "price": 4500 - offset,
                "unit": "CNY/mt",
                "quote_type": "daily_average",
                "notes": "用于验证生产规模下同产品不会重复填满结果",
                "raw": {"source_url": "https://www.ccf.com.cn/datacenter/price.php"},
            }
        )
    point_ids = count()
    bulk_upsert_forecast_price_points(rows, lambda: f"point-{next(point_ids)}")

    rebuilt = rebuild_rag_index()
    result = build_rag_visual_workbench(
        question="PX PTA MEG POY DTY 上游成本链现货日均价",
        product="POY",
        limit=12,
        as_of_time="2026-07-11T00:00:00+00:00",
    )
    result_limit_8 = build_rag_visual_workbench(
        question="PX PTA MEG POY DTY 上游成本链现货日均价",
        product="POY",
        limit=8,
        as_of_time="2026-07-11T00:00:00+00:00",
    )

    assert rebuilt["status"] == "ready"
    adopted_titles = {item["title"] for item in result["evidence_buckets"]["adopted"]}
    adopted = result["evidence_buckets"]["adopted"]
    assert all(any(product in title for title in adopted_titles) for product in ("PX", "PTA", "MEG", "POY", "DTY"))
    assert len([item for item in adopted if item["title"].startswith("MEG ")]) == 1
    assert len({item["id"] for item in adopted}) == len(adopted)
    assert len({item["title"] for item in adopted}) == len(adopted)
    assert result["summary"]["entered_count"] == len(adopted)
    for payload in (result_limit_8, result):
        adopted_payload = payload["evidence_buckets"]["adopted"]
        assert not any(
            item["category"] in {"历史复盘", "新闻与公告", "事件线索", "结构化事件"} for item in adopted_payload
        )
    assert {item["id"] for item in result_limit_8["evidence_buckets"]["adopted"]} == {item["id"] for item in adopted}
    assert result_limit_8["summary"]["entered_count"] == result["summary"]["entered_count"]
    assert result_limit_8["summary"]["confidence"] == result["summary"]["confidence"]
    assert "prototype" not in str(result).lower()


def test_agent_trace_ledger_api_replays_turns(isolated_db: Path) -> None:
    run = storage.create_agent_run(
        run_id="run-foundation",
        payload={
            "name": "Agent foundation run",
            "agent_name": "任务编排",
            "goal": "记录每轮输入输出",
            "status": "running",
            "source": "pytest",
            "trace_type": "agent_foundation",
        },
    )
    turn = create_agent_turn(
        run_id=run["run_id"],
        payload={
            "agent_name": "证据检索",
            "input_summary": "读取当前问题和上下文包。",
            "output_summary": "返回 3 条可引用证据。",
            "evidence_ids": ["doc-1", "doc-2", "doc-3"],
            "output_type": "retrieved_evidence",
            "confidence": 0.72,
            "handoff_to": "图谱构建",
            "handoff_reason": "证据集合已满足图谱构建输入。",
            "tool_calls": [
                {
                    "tool_name": "retrieve_rag",
                    "tool_category": "RAG 检索",
                    "input_summary": "POY/DTY 原料链",
                    "output_summary": "返回候选证据。",
                    "status": "success",
                }
            ],
        },
    )

    trace = agent_run_trace(run["run_id"])
    assert trace["turns"][0]["turn_id"] == turn["turn_id"]
    assert trace["turns"][0]["io_records"]
    assert trace["turns"][0]["tool_calls"][0]["tool_name"] == "retrieve_rag"
    assert trace["handoffs"][0]["to_agent"] == "图谱构建"

    client = TestClient(app)
    response = client.get(f"/api/v1/agent-runs/{run['run_id']}/trace")
    assert response.status_code == 200
    payload = response.json()
    assert payload["turns"][0]["agent_name"] == "证据检索"
    assert payload["timeline"]

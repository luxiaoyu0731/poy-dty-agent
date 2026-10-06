"""Pipeline graph read-layer tests — DESIGN §2.1 状态判定规则 + §2.7 API 契约.

The builder is exercised against an isolated database plus a fabricated shared
state root (env-overridable), so every badge rule is verified deterministically.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app import pipeline_graph, storage
from app.settings import settings

BUSINESS_DATE = "2026-09-16"


@pytest.fixture()
def isolated_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    database = tmp_path / "graph-test.db"
    object.__setattr__(settings, "sqlite_path", str(database))
    with closing(storage.connect()):
        pass
    return database


@pytest.fixture()
def state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "shared"
    root.mkdir(parents=True)
    monkeypatch.setenv("PIPELINE_GRAPH_STATE_ROOT", str(root))
    monkeypatch.delenv("LOCAL_PRODUCTION_OUTPUT_DIR", raising=False)
    (root / "local-production").mkdir(parents=True)
    (root / "state" / "local-daily").mkdir(parents=True)
    return root


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# business-day window helpers: 2026-09-16 Shanghai = 2026-09-15T16:00Z..2026-09-16T16:00Z
IN_WINDOW = "2026-09-16T02:00:00+00:00"
OUT_OF_WINDOW = "2026-09-16T17:00:00+00:00"


def _insert_fetch_runs(database: Path, rows: list[tuple[str, str]], created_at: str = IN_WINDOW) -> None:
    with closing(sqlite3.connect(database)) as connection:
        for run_id, status in rows:
            connection.execute(
                "INSERT INTO news_fetch_runs (run_id, created_at, source_id, status,"
                " articles_found, clusters_upserted, events_created, error)"
                " VALUES (?, ?, 'src', ?, 0, 0, 0, '')",
                (run_id, created_at, status),
            )
        connection.commit()


def _node(graph: dict, node_id: str) -> dict:
    return next(node for node in graph["nodes"] if node["id"] == node_id)


# ---------------------------------------------------------------------------
# API 契约：11 节点、11 边（9 flow + 2 dashed）、字段与枚举。
# ---------------------------------------------------------------------------


def test_graph_contract_has_eighteen_nodes_and_full_prediction_chain(isolated_db: Path, state_root: Path) -> None:
    graph = pipeline_graph.build_pipeline_graph(BUSINESS_DATE)

    assert graph["schema_version"] == "pipeline_graph.v1"
    assert graph["business_date"] == BUSINESS_DATE
    assert graph["generated_at"]
    assert [node["id"] for node in graph["nodes"]] == list(pipeline_graph.NODE_ORDER)
    expected_kinds = {
        "collect": "code", "clean": "code", "index": "code", "event_summary": "agent",
        "event_overview": "agent", "factor_score": "code",
        "event_signal": "code", "political_analysis": "agent", "historical_analog": "agent",
        "product_synthesis": "agent", "skeptic_review": "agent", "event_fusion": "code",
        "seven_product": "code", "shadow_eval": "code",
        "counter_scan": "agent", "daily_interpretation": "agent", "report_assembly": "code",
        "assistant": "agent",
    }
    for node in graph["nodes"]:
        assert node["kind"] == expected_kinds[node["id"]]
        assert node["status"] in {"ok", "degraded", "waiting", "idle"}
        assert set(node) >= {"id", "kind", "name", "status", "status_detail", "timestamp", "edge_group", "metrics"}
        assert node["name"]
    prediction_nodes = [node["id"] for node in graph["nodes"] if node["edge_group"] == "prediction"]
    assert prediction_nodes == [
        "event_signal", "political_analysis", "historical_analog", "product_synthesis",
        "skeptic_review", "event_fusion", "seven_product", "shadow_eval",
    ]

    flow = {(edge["from"], edge["to"]) for edge in graph["edges"] if edge["kind"] == "flow"}
    dashed = {(edge["from"], edge["to"]) for edge in graph["edges"] if edge["kind"] == "dashed"}
    feedback = {(edge["from"], edge["to"]) for edge in graph["edges"] if edge["kind"] == "feedback"}
    assert len(graph["edges"]) == 21
    assert flow == {
        ("collect", "clean"), ("clean", "index"), ("index", "event_summary"),
        ("event_summary", "event_overview"), ("event_overview", "factor_score"),
        ("factor_score", "event_signal"),
        ("event_signal", "political_analysis"), ("political_analysis", "historical_analog"),
        ("historical_analog", "product_synthesis"), ("product_synthesis", "skeptic_review"),
        ("skeptic_review", "event_fusion"), ("event_fusion", "seven_product"),
        ("seven_product", "shadow_eval"), ("shadow_eval", "counter_scan"),
        ("counter_scan", "daily_interpretation"), ("daily_interpretation", "report_assembly"),
    }
    # dashed=随时可问语义（2026-09-16 共识 + 多Agent链 §6）：事件摘要/日报解读/交叉质证 ⇢ 研判助手。
    assert dashed == {
        ("event_summary", "assistant"),
        ("daily_interpretation", "assistant"),
        ("skeptic_review", "assistant"),
    }
    # feedback=经验回灌（ADR-9 单主线学习闭环）：复盘校准 ⇢ 政局解读/历史经验。
    assert feedback == {
        ("shadow_eval", "political_analysis"),
        ("shadow_eval", "historical_analog"),
    }
    # 单主线术语锁定（ADR-9）：用户可见名不得出现 影子/融合/双轨 措辞。
    names = {node["id"]: node["name"] for node in graph["nodes"]}
    assert [names[node_id] for node_id in prediction_nodes] == [
        "当日事件精选", "政局解读", "历史经验", "品种研判",
        "交叉质证", "预测定案", "七产品预测", "复盘校准",
    ]


# ---------------------------------------------------------------------------
# 状态判定规则（DESIGN §2.1 徽章三态表）。
# ---------------------------------------------------------------------------


def test_collect_badge_rules(isolated_db: Path, state_root: Path) -> None:
    _write(
        state_root / "local-production" / "source-automation" / "source-automation-latest.json",
        {"status": "completed", "critical_failures": []},
    )
    _insert_fetch_runs(isolated_db, [(f"r{index}", "ok") for index in range(4)])
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "collect")["status"] == "ok"

    with closing(sqlite3.connect(isolated_db)) as connection:
        connection.execute("DELETE FROM news_fetch_runs")
        connection.execute(
            "INSERT INTO news_fetch_runs (run_id, created_at, source_id, status, articles_found,"
            " clusters_upserted, events_created, error) VALUES ('e1', ?, 'src', 'error', 0, 0, 0, 'x')",
            (IN_WINDOW,),
        )
        connection.execute(
            "INSERT INTO news_fetch_runs (run_id, created_at, source_id, status, articles_found,"
            " clusters_upserted, events_created, error) VALUES ('t1', ?, 'src', 'timeout', 0, 0, 0, 'x')",
            (IN_WINDOW,),
        )
        connection.commit()
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "collect")
    assert node["status"] == "degraded"
    assert "error+timeout 2/2" in node["status_detail"]

    with closing(sqlite3.connect(isolated_db)) as connection:
        connection.execute("DELETE FROM news_fetch_runs")
        connection.commit()
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "collect")["status"] == "waiting"


def test_collect_window_excludes_out_of_business_day_runs(isolated_db: Path, state_root: Path) -> None:
    _insert_fetch_runs(isolated_db, [("late", "ok")], created_at=OUT_OF_WINDOW)
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "collect")
    assert node["status"] == "waiting"  # the only run belongs to the next business day


def test_clean_badge_rules(isolated_db: Path, state_root: Path) -> None:
    quality_path = state_root / "local-production" / "source-automation" / "delivery-data-quality.json"
    _write(quality_path, {"overall_status": "success"})
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "clean")["status"] == "ok"

    _write(quality_path, {"overall_status": "needs_human_review"})
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "clean")
    assert node["status"] == "degraded"
    assert "needs_human_review" in node["status_detail"]

    quality_path.unlink()
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "clean")["status"] == "waiting"


def test_index_badge_rules(
    isolated_db: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        pipeline_graph, "semantic_index_status",
        lambda: {
            "status": "ready",
            "active_index": {
                "embedding_mode": "semantic_embedding", "document_count": 9713,
                "completed_at": "2026-09-16T07:06:00+00:00",
            },
            "building_index": None,
        },
    )
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "index")
    assert node["status"] == "ok"
    detail = pipeline_graph.build_pipeline_node_detail("index", BUSINESS_DATE)
    assert detail["status_block"]["metrics"]["documents"] == 9713

    monkeypatch.setattr(
        pipeline_graph, "semantic_index_status",
        lambda: {
            "status": "stale", "stale_reason": "grounded_summary_changed",
            "active_index": {"embedding_mode": "semantic_embedding"}, "building_index": None,
        },
    )
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "index")
    assert node["status"] == "degraded"
    assert "grounded_summary_changed" in node["status_detail"]

    monkeypatch.setattr(
        pipeline_graph, "semantic_index_status",
        lambda: {"status": "missing", "active_index": None, "building_index": {"index_id": "b1"}},
    )
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "index")["status"] == "waiting"


def test_event_summary_badge_rules(isolated_db: Path, state_root: Path) -> None:
    worker_path = state_root / "event-summary-worker" / "latest.json"
    fresh = "2026-09-16T12:00:00+00:00"
    # The heartbeat badge compares generated_at against the caller-supplied
    # ``now`` (600s threshold). Freeze ``now`` next to the fixture heartbeat so
    # the rule table stays deterministic instead of expiring with wall-clock.
    frozen_now = datetime.fromisoformat("2026-09-16T12:05:00+00:00")
    _write(worker_path, {"generated_at": fresh, "after": {"pending": 0, "failed": 0}})
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE, now=frozen_now), "event_summary")
    assert node["status"] == "ok"

    _write(worker_path, {"generated_at": fresh, "after": {"pending": 3, "failed": 0}})
    graph = pipeline_graph.build_pipeline_graph(BUSINESS_DATE, now=frozen_now)
    assert _node(graph, "event_summary")["status"] == "degraded"

    _write(worker_path, {"generated_at": "2026-09-14T12:00:00+00:00", "after": {"pending": 0, "failed": 0}})
    graph = pipeline_graph.build_pipeline_graph(BUSINESS_DATE, now=frozen_now)
    assert _node(graph, "event_summary")["status"] == "waiting"

    worker_path.unlink()
    graph = pipeline_graph.build_pipeline_graph(BUSINESS_DATE, now=frozen_now)
    assert _node(graph, "event_summary")["status"] == "waiting"


def test_event_overview_badge_rules(isolated_db: Path, state_root: Path, tmp_path: Path) -> None:
    budget_path = (
        Path(settings.sqlite_path).expanduser().resolve().parent / "event-overviews" / "budget.json"
    )
    _write(budget_path, {"limit_microusd": 10_000_000, "used_microusd": 100, "reservations": {}})
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "event_overview")["status"] == "ok"

    _write(budget_path, {"limit_microusd": 0, "used_microusd": 0, "reservations": {}})
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "event_overview")
    assert node["status"] == "waiting"
    assert "预算停用" in node["status_detail"]

    _write(budget_path, {"limit_microusd": 10_000_000, "used_microusd": 100, "reservations": {}})
    failure_dir = budget_path.parent / "failures"
    failure_dir.mkdir(parents=True, exist_ok=True)
    failure_path = failure_dir / "abc.json"
    failure_path.write_text("{}", encoding="utf-8")
    # BUSINESS_DATE is a fixed calendar day; pin the marker file inside that
    # window so the badge verdict does not depend on the wall clock at run time.
    pinned = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    os.utime(failure_path, (pinned, pinned))
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "event_overview")["status"] == "degraded"


def test_factor_and_seven_product_badge_rules(isolated_db: Path, state_root: Path) -> None:
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "factor_score")["status"] == "waiting"
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "seven_product")["status"] == "waiting"

    payload = {
        "business_date": BUSINESS_DATE,
        "as_of_time": "2026-09-16T01:37:40+00:00",
        "judgement": {
            "overview": {"status": "中性偏强", "cost_pressure_index": 61},
            "factors": [{"name": "EIA/FRED 原油", "direction": "利多", "data_status": "ready"}],
            "formal_predictions": [],
        },
    }
    import hashlib

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    storage.put_daily_judgement_snapshot(
        {
            "business_date": BUSINESS_DATE,
            "snapshot_id": f"daily-{BUSINESS_DATE}-{digest[:16]}",
            "generated_at": "2026-09-16T01:38:00+00:00",
            "as_of_time": payload["as_of_time"],
            "source_run_id": f"local-daily:{BUSINESS_DATE}",
            "status": "published",
            "payload": payload,
            "payload_sha256": digest,
        }
    )
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "factor_score")["status"] == "ok"

    with closing(sqlite3.connect(isolated_db)) as connection:
        connection.execute(
            """
            INSERT INTO seven_product_forecast_batches (
              batch_id, business_date, schema_version, as_of_time, generated_at, persisted_at,
              model_registry_revision, data_snapshot_sha256, configuration_sha256,
              formal_count, reference_count, unavailable_count, contract_complete, payload, payload_sha256
            ) VALUES ('b1', ?, 'seven-product-forecast.v1', ?, ?, ?, 'r1', ?, ?, 0, 21, 0, 1, '{}', ?)
            """,
            (
                BUSINESS_DATE, "2026-09-16T01:30:00+00:00", "2026-09-16T01:31:00+00:00",
                "2026-09-16T01:31:00+00:00", "a" * 64, "b" * 64, "c" * 64,
            ),
        )
        connection.commit()
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "seven_product")
    assert node["status"] == "degraded"  # formal=0 是现状降级（DESIGN 表）
    assert "formal=0" in node["status_detail"]


def test_artifact_nodes_and_report_assembly_badges(
    isolated_db: Path, state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_COUNTER_SCAN_ARTIFACT_DIR", str(tmp_path / "counter-scan"))
    monkeypatch.setenv("AI_DAILY_INTERPRETATION_ARTIFACT_DIR", str(tmp_path / "interpretation"))

    graph = pipeline_graph.build_pipeline_graph(BUSINESS_DATE)
    assert _node(graph, "counter_scan")["status"] == "waiting"
    assert _node(graph, "daily_interpretation")["status"] == "waiting"
    assert "链未成功" in _node(graph, "counter_scan")["status_detail"]

    (state_root / "state" / "local-daily" / f"{BUSINESS_DATE}.success").write_text("ok", encoding="utf-8")
    graph = pipeline_graph.build_pipeline_graph(BUSINESS_DATE)
    assert "跳过/等待重跑" in _node(graph, "counter_scan")["status_detail"]

    _write(tmp_path / "counter-scan" / f"{BUSINESS_DATE}.json", {
        "business_date": BUSINESS_DATE, "status": "completed", "generated_at": "2026-09-16T02:00:00+00:00",
        "scan_outcome": "counter_evidence_found",
        "findings": [{"claim": {"supports": [{"doc_id": "d1", "quote": "q"}]}}],
        "sanitized": {"findings_stripped": 1},
        "input_summary": {
            "snapshot_id": "s1", "payload_sha256": "p", "events": 4, "evidence_documents": 7,
        },
    })
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "counter_scan")
    assert node["status"] == "ok"

    _write(tmp_path / "interpretation" / f"{BUSINESS_DATE}.degraded.json", {
        "business_date": BUSINESS_DATE, "status": "degraded", "failure_reason": "provider_timeout",
        "generated_at": "2026-09-16T02:05:00+00:00",
    })
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "daily_interpretation")
    assert node["status"] == "degraded"
    assert "provider_timeout" in node["status_detail"]

    status_path = state_root / "local-production" / "latest-status.json"
    brief_path = state_root / "morning-brief" / f"{BUSINESS_DATE}.json"
    _write(status_path, {"overall_status": "ready", "warnings": [], "finished_at": "2026-09-16T02:10:00+00:00"})
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "report_assembly")
    assert node["status"] == "degraded"  # morning-brief 当日文件缺失

    _write(brief_path, {"business_date": BUSINESS_DATE})
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "report_assembly")["status"] == "ok"

    _write(status_path, {"overall_status": "blocked", "warnings": [], "finished_at": "2026-09-16T02:10:00+00:00"})
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "report_assembly")["status"] == "degraded"


def test_assistant_badge_rules(isolated_db: Path, state_root: Path) -> None:
    assert _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "assistant")["status"] == "idle"

    with closing(sqlite3.connect(isolated_db)) as connection:
        connection.execute(
            """
            INSERT INTO agent_runs (run_id, created_at, updated_at, name, agent_name, goal, status,
              source, trace_type, started_at, finished_at, metadata)
            VALUES ('run-1', '2026-09-16T05:00:00+00:00', '2026-09-16T05:00:20+00:00',
              'Assistant governed answer', '任务编排', 'PTA 库存怎么看', 'completed', 'assistant_pipeline',
              'assistant_governed_run', NULL, NULL, '{}')
            """
        )
        connection.commit()
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "assistant")
    assert node["status"] == "ok"
    assert node["timestamp"] == "2026-09-16T05:00:00+00:00"
    detail = pipeline_graph.build_pipeline_node_detail("assistant", BUSINESS_DATE)
    assert detail["input_summary"]["question"] == "PTA 库存怎么看"
    assert detail["agent_extra"]["recent_runs"][0]["run_id"] == "run-1"


def test_usd_cost_block_is_normalized_to_cny_display(isolated_db: Path, state_root: Path) -> None:
    budget_path = (
        Path(settings.sqlite_path).expanduser().resolve().parent / "event-overviews" / "budget.json"
    )
    _write(budget_path, {
        "limit_microusd": 10_000_000, "used_microusd": 2_000,
        "reservations": {"r1": {"status": "settled"}},
    })

    detail = pipeline_graph.build_pipeline_node_detail("event_overview", BUSINESS_DATE)
    cost = detail["agent_extra"]["daily_cost"]

    assert cost["native_currency"] == "USD"
    assert cost["native_amount_micros"] == 2_000
    assert cost["display_currency"] == "CNY"
    assert cost["display_amount_micros"] == 14_500  # 2000 micro-USD * 7.25
    assert cost["fx_rate"] == 7.25
    assert cost["fx_version"] == "usd-cny-static-2026-09@7.25"


def test_usd_cny_display_rate_env_override(isolated_db: Path, state_root: Path) -> None:
    budget_path = (
        Path(settings.sqlite_path).expanduser().resolve().parent / "event-overviews" / "budget.json"
    )
    _write(budget_path, {"limit_microusd": 10_000_000, "used_microusd": 1_000, "reservations": {}})
    original_rate = settings.usd_cny_display_rate
    object.__setattr__(settings, "usd_cny_display_rate", 7.0)
    try:
        cost = pipeline_graph.build_pipeline_node_detail("event_overview", BUSINESS_DATE)["agent_extra"]["daily_cost"]
        assert cost["fx_rate"] == 7.0
        assert cost["display_amount_micros"] == 7_000
        assert cost["fx_version"] == "usd-cny-static-2026-09@7"
    finally:
        object.__setattr__(settings, "usd_cny_display_rate", original_rate)


def test_cny_cost_block_passes_through_native(isolated_db: Path, state_root: Path) -> None:
    storage.record_llm_call(
        trace_id=f"assistant:{BUSINESS_DATE}:chat:abc12345",
        provider="deepseek", model="deepseek-v4-pro", stage="assistant",
        business_date=BUSINESS_DATE, question="q", latency_ms=1000,
        prompt_tokens=100, completion_tokens=100, usage_source="estimate",
        cost_micros=1234,
    )

    cost = pipeline_graph.build_pipeline_node_detail("assistant", BUSINESS_DATE)["agent_extra"]["daily_cost"]

    assert cost["native_currency"] == "CNY"
    assert cost["native_amount_micros"] == 1234
    assert cost["display_amount_micros"] == 1234
    assert cost["display_currency"] == "CNY"
    assert cost["fx_rate"] == 1.0
    assert cost["fx_version"] == "cny-native@1"


# ---------------------------------------------------------------------------
# 抽屉懒加载：四块 + Agent 两块。
# ---------------------------------------------------------------------------


def test_node_detail_lazy_load_blocks(
    isolated_db: Path, state_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_COUNTER_SCAN_ARTIFACT_DIR", str(tmp_path / "counter-scan"))
    _write(tmp_path / "counter-scan" / f"{BUSINESS_DATE}.json", {
        "business_date": BUSINESS_DATE, "status": "completed", "generated_at": "2026-09-16T02:00:00+00:00",
        "scan_outcome": "counter_evidence_found",
        "findings": [{"claim": {"supports": [{"doc_id": "doc-99", "quote": "逐字引文"}]}}],
        "sanitized": {"findings_stripped": 0},
        "input_summary": {"snapshot_id": "s1", "payload_sha256": "p" * 64, "events": 3, "evidence_documents": 5},
    })
    storage.record_llm_call(
        trace_id=f"counter_scan:{BUSINESS_DATE}:scan:abc12345",
        provider="deepseek", model="deepseek-v4-pro", stage="counter_scan",
        business_date=BUSINESS_DATE, question="q", latency_ms=8600,
        prompt_tokens=2194, completion_tokens=550, usage_source="provider_usage",
        prompt_version="counter-scan-v3", cost_micros=8788,
    )

    detail = pipeline_graph.build_pipeline_node_detail("counter_scan", BUSINESS_DATE)

    assert detail["kind"] == "agent"
    assert set(detail) >= {
        "status_block", "input_summary", "output_summary", "evidence_entries", "agent_extra",
    }
    assert detail["input_summary"]["snapshot_id"] == "s1"
    assert detail["output_summary"]["scan_outcome"] == "counter_evidence_found"
    evidence_ids = [entry["id"] for entry in detail["evidence_entries"]]
    assert "doc-99" in evidence_ids
    assert detail["agent_extra"]["daily_cost"] == {
        "calls": 1,
        "native_amount_micros": 8788,
        "native_currency": "CNY",
        "display_amount_micros": 8788,
        "display_currency": "CNY",
        "fx_rate": 1.0,
        "fx_version": "cny-native@1",
        "note": "",
    }
    assert detail["agent_extra"]["recent_runs"][0]["run_id"].startswith("counter_scan:")
    assert detail["agent_extra"]["recent_runs"][0]["latency_ms"] == 8600


def test_code_node_detail_has_no_agent_extra(isolated_db: Path, state_root: Path) -> None:
    detail = pipeline_graph.build_pipeline_node_detail("collect", BUSINESS_DATE)
    assert detail["kind"] == "code"
    assert "agent_extra" not in detail
    assert detail["status_block"]["status"] in {"ok", "degraded", "waiting"}


def test_unknown_node_raises_value_error(isolated_db: Path, state_root: Path) -> None:
    with pytest.raises(ValueError, match="pipeline_node_unknown"):
        pipeline_graph.build_pipeline_node_detail("does_not_exist", BUSINESS_DATE)


def test_event_summary_drawer_uses_worker_run_and_queue(isolated_db: Path, state_root: Path) -> None:
    _write(state_root / "event-summary-worker" / "latest.json", {
        "generated_at": "2026-09-16T12:00:00+00:00",
        "status": "completed",
        "after": {"pending": 0, "failed": 0, "completed": 300, "rejected": 2577},
        "daily_reserved_requests": 98,
    })
    detail = pipeline_graph.build_pipeline_node_detail("event_summary", BUSINESS_DATE)
    assert detail["agent_extra"]["recent_runs"][0]["run_id"] == "worker:last-cycle"
    assert detail["output_summary"]["completed"] == 300


# ---------------------------------------------------------------------------
# API 端点（懒加载入口 + 未知节点 404 + 会话鉴权拒绝匿名）。
# ---------------------------------------------------------------------------


def test_pipeline_graph_endpoints_via_api(
    isolated_db: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    original_enforce = settings.enforce_internal_token
    object.__setattr__(settings, "enforce_internal_token", False)
    try:
        with TestClient(app) as client:
            graph_response = client.get("/api/v1/pipeline/graph", params={"business_date": BUSINESS_DATE})
            assert graph_response.status_code == 200
            payload = graph_response.json()
            assert payload["business_date"] == BUSINESS_DATE
            assert len(payload["nodes"]) == 18

            detail_response = client.get(f"/api/v1/pipeline/nodes/event_summary?business_date={BUSINESS_DATE}")
            assert detail_response.status_code == 200
            assert detail_response.json()["node_id"] == "event_summary"

            unknown_response = client.get("/api/v1/pipeline/nodes/not-a-node")
            assert unknown_response.status_code == 404
            # The request-observability middleware rewrites HTTPException bodies.
            assert unknown_response.json()["error"]["code"] == "pipeline_node_not_found"
    finally:
        object.__setattr__(settings, "enforce_internal_token", original_enforce)


def test_pipeline_graph_requires_internal_token_when_enforced(
    isolated_db: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from app.main import app

    original_enforce = settings.enforce_internal_token
    original_token = settings.internal_api_token
    object.__setattr__(settings, "enforce_internal_token", True)
    object.__setattr__(settings, "internal_api_token", "token-value")
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/pipeline/graph")
            assert response.status_code == 401
    finally:
        # Frozen settings cannot use monkeypatch.setattr; restore manually so
        # later suites keep their original auth posture.
        object.__setattr__(settings, "enforce_internal_token", original_enforce)
        object.__setattr__(settings, "internal_api_token", original_token)


def test_collect_blocked_sources_do_not_degrade_the_node(
    isolated_db: Path, state_root: Path
) -> None:
    _write(
        state_root / "local-production" / "source-automation" / "source-automation-latest.json",
        {"status": "completed", "critical_failures": []},
    )
    with closing(sqlite3.connect(isolated_db)) as connection:
        # 一个整窗口持续失败的源（外部受限）+ 一个健康源；被封锁源的
        # 高频失败重试不应把节点拖成降级。
        for day in range(1, 15):
            day_str = f"2026-09-{day:02d}T02:00:00+00:00"
            connection.execute(
                "INSERT INTO news_fetch_runs (run_id, created_at, source_id, status,"
                " articles_found, clusters_upserted, events_created, error)"
                " VALUES (?, ?, 'blocked_src', 'error', 0, 0, 0, 'x')",
                (f"b{day}", day_str),
            )
        connection.execute(
            "INSERT INTO news_fetch_runs (run_id, created_at, source_id, status,"
            " articles_found, clusters_upserted, events_created, error)"
            " VALUES ('h1', ?, 'healthy_src', 'ok', 1, 0, 0, '')",
            (IN_WINDOW,),
        )
        connection.execute(
            "INSERT INTO news_fetch_runs (run_id, created_at, source_id, status,"
            " articles_found, clusters_upserted, events_created, error)"
            " VALUES ('h2', ?, 'healthy_src', 'no_relevant', 0, 0, 0, '')",
            (IN_WINDOW,),
        )
        connection.commit()
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "collect")
    assert node["status"] == "ok"
    assert "外部受限" in node["status_detail"]
    detail = pipeline_graph.build_pipeline_node_detail("collect", BUSINESS_DATE)
    assert detail["status_block"]["metrics"]["blocked_sources"] == ["blocked_src"]
    assert detail["status_block"]["metrics"]["reachable_ok_rate"] == 1.0
    # 全量口径仍保留（run 级 44% 类事实不隐藏）。
    assert detail["status_block"]["metrics"]["ok_rate"] == 0.5


def test_seven_product_node_splits_maturing_from_failing(isolated_db: Path, state_root: Path) -> None:
    with closing(sqlite3.connect(isolated_db)) as connection:
        connection.execute(
            """
            INSERT INTO seven_product_forecast_batches (
              batch_id, business_date, schema_version, as_of_time, generated_at, persisted_at,
              model_registry_revision, data_snapshot_sha256, configuration_sha256,
              formal_count, reference_count, unavailable_count, contract_complete, payload, payload_sha256
            ) VALUES ('b2', ?, 'seven-product-forecast.v1', ?, ?, ?, 'r1', ?, ?, 0, 21, 0, 1, '{}', ?)
            """,
            (
                BUSINESS_DATE, "2026-09-16T01:30:00+00:00", "2026-09-16T01:31:00+00:00",
                "2026-09-16T01:31:00+00:00", "a" * 64, "b" * 64, "c" * 64,
            ),
        )
        connection.commit()
    _write(
        state_root / "local-production" / "seven-product-evaluation" / "seven-product-evaluation-latest.json",
        {
            "evaluation": {
                "minimum_effective_samples": 20,
                "cells": [
                    {
                        "target": "crude", "horizon_days": 7,
                        "effective_sample_count": 13, "direction_accuracy": 0.214,
                        "error_improvement": 0.005,
                    },
                    {
                        "target": "naphtha", "horizon_days": 1,
                        "effective_sample_count": 3, "direction_accuracy": 0.333,
                        "error_improvement": -0.047,
                    },
                    {
                        "target": "poy", "horizon_days": 7,
                        "effective_sample_count": 0, "direction_accuracy": None,
                        "error_improvement": None,
                    },
                ],
            }
        },
    )
    node = _node(pipeline_graph.build_pipeline_graph(BUSINESS_DATE), "seven_product")
    assert node["status"] == "degraded"
    # 有效样本 ≥10 且方向/误差未达标的格子被点名；小样本格子归入成熟期。
    assert "crude h7" in node["status_detail"]
    assert "样本积累中 2 格" in node["status_detail"]
    detail = pipeline_graph.build_pipeline_node_detail("seven_product", BUSINESS_DATE)
    assert detail["status_block"]["metrics"]["failing_cells"] == ["crude h7"]
    assert detail["status_block"]["metrics"]["max_effective_samples"] == 13


def test_graph_preserves_chain_stage_metrics(isolated_db: Path, state_root: Path) -> None:
    counters = {
        "political_analysis": {"ok": 16, "fallback": 1, "rejected": 2},
        "historical_analog": {"ok": 8, "fallback": 0, "rejected": 0},
        "product_synthesis": {"ok": 7, "fallback": 0, "rejected": 0},
        "skeptic_review": {"ok": 7, "fallback": 0, "rejected": 0},
    }
    _write(state_root / "local-production" / "event-agent-chain-latest.json", {
        "budget": {"cap": 60, "stage_caps": {"historical_analog": 8, "political_analysis": 16,
                   "product_synthesis": 7, "skeptic_review": 7}},
        "business_date": BUSINESS_DATE, "as_of_time": IN_WINDOW,
        "run_id": "metrics-test", "counters": counters,
    })
    graph = pipeline_graph.build_pipeline_graph(BUSINESS_DATE)
    for stage, counts in counters.items():
        metrics = _node(graph, stage)["metrics"]
        assert metrics["calls"] == counts["ok"] > 0
        assert metrics["fallback"] == counts["fallback"]
        assert metrics["rejected"] == counts["rejected"]
        assert metrics["call_cap"] == {"historical_analog": 8, "political_analysis": 16,
                                      "product_synthesis": 7, "skeptic_review": 7}[stage]
        detail = pipeline_graph.build_pipeline_node_detail(stage, BUSINESS_DATE)
        assert metrics == detail["status_block"]["metrics"]

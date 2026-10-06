from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import storage as storage_module
from app.main import app
from app.rate_limit import WINDOWS
from app.settings import settings
from app.storage import bulk_upsert_forecast_price_points

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Keep the acceptance slice on real app/storage code without production data writes."""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    original_sqlite_path = settings.sqlite_path
    original_enforce = settings.enforce_internal_token
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "evidence-chain.db"))
    object.__setattr__(settings, "enforce_internal_token", False)
    WINDOWS.clear()
    client.cookies.clear()
    yield
    WINDOWS.clear()
    client.cookies.clear()
    object.__setattr__(settings, "sqlite_path", original_sqlite_path)
    object.__setattr__(settings, "enforce_internal_token", original_enforce)


def _import_public_observation(
    *,
    observed_at: str,
    series_id: str,
    instrument: str,
    product: str,
    notes: str,
    tier: str = "A",
    source_id: str = "eia_petroleum_api",
) -> None:
    payload = (
        "observed_at,source_id,tier,dataset,series_id,instrument,product,frequency,value,unit,currency,"
        "region,period_start,period_end,quality_flag,source_url,raw_field_name,ingested_by,notes\n"
        f"{observed_at},{source_id},{tier},eia,{series_id},{instrument},{product},daily,88.2,$/BBL,USD,"
        f"United States,{observed_at},{observed_at},ok,https://api.eia.gov/v2/petroleum/pri/spt/data/,value,acceptance,{notes}\n"
    )
    response = client.post(
        "/api/v1/imports/public-observations",
        content=payload,
        headers={"Content-Type": "text/csv"},
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def _adopted_titles(payload: dict[str, object]) -> set[str]:
    buckets = payload["evidence_buckets"]
    assert isinstance(buckets, dict)
    adopted = buckets["adopted"]
    assert isinstance(adopted, list)
    return {str(item["title"]) for item in adopted}


def test_rag_visual_is_query_bound_and_only_enters_customer_business_evidence() -> None:
    """P0: a graph must describe this retrieval, not a fixed sample of the global index."""
    _import_public_observation(
        observed_at="2026-07-09",
        series_id="WTI_SANCTIONS_ACCEPTANCE",
        instrument="WTI sanctions shipping shock",
        product="crude_oil",
        notes="OFAC tanker sanctions and crude shipping disruption acceptance marker alpha",
    )
    _import_public_observation(
        observed_at="2026-07-10",
        series_id="POY_DEMAND_ACCEPTANCE",
        instrument="POY terminal weaving demand",
        product="POY",
        notes="POY terminal weaving demand and polyester inventory acceptance marker beta",
    )
    sanctions = client.get(
        "/api/v1/workbench/rag-visual",
        params={"q": "OFAC tanker sanctions crude shipping disruption alpha", "product": "crude_oil", "limit": 8},
    )
    demand = client.get(
        "/api/v1/workbench/rag-visual",
        params={"q": "POY terminal weaving demand polyester inventory beta", "product": "POY", "limit": 8},
    )
    assert sanctions.status_code == 200
    assert demand.status_code == 200

    sanctions_payload = sanctions.json()
    demand_payload = demand.json()
    sanctions_titles = _adopted_titles(sanctions_payload)
    demand_titles = _adopted_titles(demand_payload)
    assert sanctions_titles != demand_titles

    for payload in (sanctions_payload, demand_payload):
        adopted = payload["evidence_buckets"]["adopted"]
        assert payload["summary"]["entered_count"] == len(adopted)
        assert payload["summary"]["graph_nodes"] == len(payload["graph"]["nodes"])
        assert payload["summary"]["graph_edges"] == len(payload["graph"]["edges"])
        assert all(item["category"] not in {"项目规则", "数据来源说明"} for item in adopted)


def test_partial_or_stale_full_chain_snapshot_rejects_formal_prediction_without_ledger_write() -> None:
    """P0: an unqualified explicit formalization attempt must fail atomically."""
    before = client.get("/api/v1/predictions")
    assert before.status_code == 200
    assert before.json() == []

    summary = client.get("/api/v1/full-chain/summary")
    assert summary.status_code == 200
    payload = summary.json()
    assert payload["status"] in {"partial", "data_not_ready"}
    assert payload["poy_dty_gate"]["qualified"] is False

    rejected = client.post(
        "/api/v1/predictions",
        json={
            "target": "POY/DTY 上游成本压力",
            "horizon": "7d",
            "direction": "中性偏强",
            "confidence": 0.55,
            "rationale": "显式正式化请求必须经过完整、时效合格的数据快照门禁。",
            "counter_evidence": "终端需求可能抵消成本传导。",
            "source_status": "acceptance_gate",
            "tags": ["acceptance"],
        },
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "formal_prediction_write_path_disabled"

    after = client.get("/api/v1/predictions")
    assert after.status_code == 200
    assert after.json() == []


def test_legacy_scalar_post_remains_disabled_even_for_a_qualified_snapshot() -> None:
    """P1: legacy scalar evidence cannot substitute for a 57→45 formal proof."""
    current_day = datetime.now(UTC).date().isoformat()
    previous_day = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    _import_public_observation(
        observed_at=current_day,
        series_id="RWTC",
        instrument="WTI",
        product="crude_oil",
        notes="fresh crude observation for formal acceptance",
    )
    rows = []
    for product, value in (("PX", 8500), ("PTA", 6100), ("MEG", 4550), ("POY", 7600), ("DTY", 8900)):
        rows.append(
            {
                "source_id": "ccf_dom_daily",
                "dataset_type": "ccf_spot",
                "observed_at": current_day,
                "company": "CCF",
                "product": product,
                "spec": f"{product} 日均价",
                "price": value,
                "unit": "CNY/mt",
                "quote_type": "daily_average",
                "notes": f"用户授权导出的 CCF {product} 现货日均价",
                "raw": {"source_url": "https://www.ccf.com.cn/datacenter/price.php"},
            }
        )
        rows.append(
            {
                **rows[-1],
                "observed_at": previous_day,
                "price": value * 0.97,
                "notes": f"用户授权导出的 CCF {product} 前一日现货日均价",
            }
        )
    imported = bulk_upsert_forecast_price_points(rows, iter([f"ccf-{index}" for index in range(10)]).__next__)
    assert len(imported) == 10

    snapshot = client.post("/api/v1/data-snapshots", json={"notes": "qualified acceptance snapshot"})
    assert snapshot.status_code == 200
    snapshot_id = snapshot.json()["snapshot_id"]
    # Current-day POY/DTY rows are ccf-6 and ccf-8 because each product is
    # followed by its prior-day comparison row.
    for doc_id in (
        "ccf_spot:ccf-0",
        "ccf_spot:ccf-1",
        "ccf_spot:ccf-2",
        "ccf_spot:ccf-3",
        "ccf_spot:ccf-6",
        "ccf_spot:ccf-7",
        "ccf_spot:ccf-8",
        "ccf_spot:ccf-9",
    ):
        role = (
            "upstream_cost_driver"
            if doc_id.endswith(("0", "1"))
            else "transmission_path"
            if doc_id.endswith(("2", "3"))
            else "downstream_transmission"
        )
        storage_module.upsert_evidence_review(
            doc_id=doc_id,
            status="reviewed",
            reviewer="codex",
            reviewer_type="codex",
            method="acceptance_price_review",
            version="1.0.0",
            criteria=["source", "timestamp", "unit"],
            result="approved",
            reason="Acceptance evidence checks passed.",
            notes="verified formal input",
            purpose="formal_cost_pressure",
            evidence_role=role,
        )
    created = client.post(
        "/api/v1/predictions",
        json={
            "target": "POY/DTY 上游成本压力",
            "horizon": "7d",
            "direction": "偏强",
            "confidence": 0.7,
            "rationale": "同一快照覆盖原油、PX、PTA、MEG、POY、DTY。",
            "counter_evidence": "需求走弱可能抵消成本传导。",
            "source_status": "snapshot_backed",
            "tags": ["acceptance"],
            "data_snapshot_id": snapshot_id,
        },
    )
    assert created.status_code == 409
    assert created.json()["error"]["code"] == "formal_prediction_write_path_disabled"
    listed = client.get("/api/v1/predictions")
    assert listed.status_code == 200
    assert listed.json() == []


def test_rag_visual_read_is_side_effect_free_for_prediction_ledger() -> None:
    """P1: retrieving evidence alone is not a formal forecast or ledger write."""
    with closing(storage_module.connect()) as connection, connection:
        before = int(connection.execute("SELECT COUNT(*) FROM prediction_ledger").fetchone()[0])

    response = client.get(
        "/api/v1/workbench/rag-visual",
        params={"q": "POY DTY 上游原料成本压力", "product": "POY", "limit": 8},
    )
    assert response.status_code == 200

    with closing(storage_module.connect()) as connection, connection:
        after = int(connection.execute("SELECT COUNT(*) FROM prediction_ledger").fetchone()[0])
    assert after == before


def test_rag_visual_entered_count_only_counts_adopted_ab_evidence() -> None:
    _import_public_observation(
        observed_at="2026-07-10",
        series_id="MIXED_TIER_A",
        instrument="mixed tier acceptance signal",
        product="crude_oil",
        notes="mixed tier acceptance marker gamma authoritative",
        tier="A",
    )
    _import_public_observation(
        observed_at="2026-07-10",
        series_id="MIXED_TIER_D",
        instrument="mixed tier acceptance signal",
        product="crude_oil",
        notes="mixed tier acceptance marker gamma unreviewed weak signal",
        tier="D",
        source_id="internal_market_notes",
    )

    response = client.get(
        "/api/v1/workbench/rag-visual",
        params={"q": "mixed tier acceptance marker gamma", "product": "crude_oil", "limit": 8},
    )
    assert response.status_code == 200
    payload = response.json()
    adopted = payload["evidence_buckets"]["adopted"]
    excluded = payload["evidence_buckets"]["excluded"]
    assert excluded
    assert all(item["reason"] != "与本次业务问题相关，进入证据复核链路。" for item in excluded)
    assert any("不足以进入正式结论" in item["reason"] for item in excluded)
    assert payload["summary"]["reviewed_count"] > len(adopted)
    assert payload["summary"]["entered_count"] == len(adopted)
    assert payload["node_details"]["conclusion"]["supports"][0] == f"纳入证据包 {len(adopted)} 条"

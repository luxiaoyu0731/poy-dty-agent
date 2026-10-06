from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.daily_decision import daily_judgement_response
from app.intelligence import (
    build_event_impacts,
    build_factor_scores,
    build_full_chain_summary,
    build_morning_brief,
    build_overview,
)
from app.price_intraday import build_latest_prices
from app.storage import (
    create_event_observation,
    create_industry_observation,
    create_market_observation,
    put_daily_judgement_snapshot,
    upsert_forecast_price_point,
    upsert_news_article,
    upsert_news_event_cluster,
)
from app.workbench_market import PRODUCTS, build_market_chain_workbench


def _controlled_database() -> Path:
    root_value = os.environ.get("DG01_TEST_DB_ROOT", "").strip()
    database_value = os.environ.get("SQLITE_PATH", "").strip()
    if not root_value or not database_value:
        raise RuntimeError("e2e_seed_requires_controlled_database_environment")
    root = Path(root_value).resolve(strict=True)
    database = Path(database_value).resolve(strict=False)
    if database == root or root not in database.parents:
        raise RuntimeError("e2e_seed_database_outside_controlled_root")
    if database.exists() or database.is_symlink():
        raise RuntimeError("e2e_seed_database_must_not_exist")
    return database


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _seed_market(now: datetime) -> None:
    canonical = {str(item["key"]): item["canonical_price"] for item in PRODUCTS}
    base_prices = {
        "POY": 7_650.0,
        "DTY": 8_850.0,
        "PX": 835.0,
        "PTA": 5_120.0,
        "MEG": 4_480.0,
        "NAPHTHA": 675.0,
        "CRUDE": 79.5,
    }
    for day_offset in (2, 1, 0):
        observed = now - timedelta(days=day_offset)
        observed_at = _iso(observed.replace(hour=6, minute=0, second=0, microsecond=0))
        create_market_observation(
            observation_id=f"e2e-fx-{day_offset}",
            payload={
                "source_id": "fred_macro_api",
                "observed_at": observed_at,
                "indicator": "China / U.S. Foreign Exchange Rate (DEXCHUS)",
                "product": "fx",
                "value": 7.12 + (2 - day_offset) * 0.01,
                "unit": "cny_per_usd",
                "frequency": "daily",
                "region": "global",
                "evidence_url": "https://example.test/e2e/fx",
                "notes": "synthetic E2E fixture",
                "raw": {"fixture": "e2e-v1"},
            },
        )
        create_market_observation(
            observation_id=f"e2e-crude-{day_offset}",
            payload={
                "source_id": "eia_petroleum_api",
                "observed_at": observed_at,
                "indicator": "Brent Spot Price FOB",
                "product": "crude_oil",
                "value": base_prices["CRUDE"] + (2 - day_offset) * 0.6,
                "unit": "USD/bbl",
                "frequency": "daily",
                "region": "global",
                "evidence_url": "https://example.test/e2e/crude",
                "notes": "synthetic E2E fixture",
                "raw": {"fixture": "e2e-v1"},
            },
        )
        for product, basis in canonical.items():
            point_product = "Brent" if product == "CRUDE" else product
            upsert_forecast_price_point(
                point_id=f"e2e-price-{product.lower()}-{day_offset}",
                payload={
                    "source_id": "ccf_dom_daily",
                    "dataset_type": "ccf_spot",
                    "observed_at": observed_at,
                    "company": "E2E synthetic source",
                    "product": point_product,
                    "series": "E2E daily series",
                    "spec": basis["spec"],
                    "market": "E2E market",
                    "price": base_prices[product] + (2 - day_offset) * 10.0,
                    "unit": basis["unit"],
                    "quote_type": basis["quote_type"],
                    "notes": "synthetic E2E fixture; never production data",
                    "raw": {
                        "fixture": "e2e-v1",
                        "source_url": f"https://example.test/e2e/{product.lower()}",
                    },
                },
            )
        for product, metric, value, unit in (
            ("POY", "poy_inventory", 18.0 + day_offset, "days"),
            ("DTY", "dty_inventory", 22.0 + day_offset, "days"),
            ("POLYESTER", "polyester_operating_rate", 82.0 - day_offset, "%"),
            ("POLYESTER", "polyester_profit", 420.0 + day_offset * 5, "CNY/mt"),
            ("POY", "poy_profit", 380.0 + day_offset * 5, "CNY/mt"),
            ("DTY", "dty_profit", 510.0 + day_offset * 5, "CNY/mt"),
        ):
            create_industry_observation(
                observation_id=f"e2e-industry-{product.lower()}-{metric}-{day_offset}",
                payload={
                    "source_id": "ccf_dom_daily",
                    "observed_at": observed_at,
                    "product": product,
                    "metric": metric,
                    "market": "全国",
                    "region": "全国",
                    "value": value,
                    "unit": unit,
                    "frequency": "daily",
                    "evidence_level": "A",
                    "evidence_url": "https://example.test/e2e/industry",
                    "notes": "synthetic E2E fixture",
                    "raw": {"fixture": "e2e-v1"},
                },
            )


def _seed_event(now: datetime) -> None:
    published_at = _iso(now - timedelta(hours=2))
    article_id = "e2e-news-article"
    event_id = "e2e-event-observation"
    upsert_news_article(
        article_id=article_id,
        payload={
            "source_id": "opec_press",
            "tier": "A",
            "url": "https://example.test/e2e/oil-supply",
            "canonical_url": "https://example.test/e2e/oil-supply",
            "title": "港口管理局公布原油装卸安排",
            "published_at": published_at,
            "content_hash": hashlib.sha256(b"e2e-oil-supply").hexdigest(),
            "language": "zh",
            "raw_text": "港口管理局公布原油装卸安排，市场继续核验实际到港变化。",
            "summary": "港口管理局公布原油装卸安排。",
            "score": 80,
            "category": "shipping_security",
            "raw": {
                "fixture": "e2e-v1",
                "source_content": {"status": "full_text"},
            },
        },
    )
    create_event_observation(
        event_record_id=event_id,
        payload={
            "source_id": "opec_press",
            "occurred_at": published_at,
            "title": "港口管理局公布原油装卸安排",
            "event_type": "shipping_security",
            "evidence_level": "A",
            "summary": "港口管理局公布原油装卸安排。",
            "affected_products": ["Brent", "PX", "POY", "DTY"],
            "direction": "利多",
            "impact_strength": "high",
            "evidence_url": "https://example.test/e2e/oil-supply",
            "requires_human_review": False,
            "notes": "synthetic E2E fixture",
            "raw": {"fixture": "e2e-v1"},
        },
    )
    upsert_news_event_cluster(
        cluster_id="e2e-news-cluster",
        payload={
            "title": "港口管理局公布原油装卸安排",
            "category": "shipping_security",
            "source_ids": ["opec_press"],
            "article_ids": [article_id],
            "heat_score": 80,
            "evidence_level": "A",
            "affected_products": ["Brent", "PX", "POY", "DTY"],
            "direction": "利多",
            "impact_strength": "high",
            "summary": "港口管理局公布原油装卸安排。",
            "status": "featured",
            "event_record_id": event_id,
            "raw": {"fixture": "e2e-v1"},
        },
    )


def _seed_daily_snapshot(now: datetime) -> None:
    as_of = now + timedelta(minutes=1)
    as_of_time = _iso(as_of)
    payload = {
        "contract_version": "1.0",
        "business_date": now.astimezone().date().isoformat(),
        "as_of_time": as_of_time,
        "source_run": {
            "run_id": "e2e-agent-run",
            "name": "E2E synthetic daily run",
            "status": "completed",
            "started_at": _iso(now - timedelta(minutes=10)),
            "finished_at": _iso(now),
        },
        "daily_report": {
            "status": "ready",
            "summary": "E2E synthetic report fixture",
            "formal_report_eligible": False,
            "quality_gate_status": "missing",
            "download_available": False,
            "content_status": "missing",
        },
        "judgement": {
            "overview": build_overview(as_of_time=as_of_time),
            "full_chain": build_full_chain_summary(as_of_time=as_of_time),
            "factors": [item.model_dump(mode="json") for item in build_factor_scores(as_of_time=as_of_time)],
            "formal_predictions": [],
        },
        "market": {
            "latest_prices": build_latest_prices(),
            "chain": build_market_chain_workbench(as_of_time=as_of_time),
        },
        "briefing": {
            "morning_brief": [item.model_dump(mode="json") for item in build_morning_brief()],
            "events": [item.model_dump(mode="json") for item in build_event_impacts()],
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()
    snapshot, inserted = put_daily_judgement_snapshot(
        {
            "business_date": payload["business_date"],
            "snapshot_id": f"e2e-daily-{digest[:16]}",
            "generated_at": _iso(now),
            "as_of_time": as_of_time,
            "source_run_id": "e2e-agent-run",
            "data_snapshot_id": payload["judgement"]["full_chain"]["data_snapshot_id"],
            "status": "published",
            "payload": payload,
            "payload_sha256": digest,
        }
    )
    if not inserted or daily_judgement_response(snapshot).get("snapshot_id") != snapshot["snapshot_id"]:
        raise RuntimeError("e2e_seed_snapshot_not_inserted")


def main() -> None:
    database = _controlled_database()
    now = datetime.now(UTC).replace(microsecond=0)
    _seed_market(now)
    _seed_event(now)
    _seed_daily_snapshot(now)
    if not database.is_file() or database.is_symlink() or database.stat().st_nlink != 1:
        raise RuntimeError("e2e_seed_database_identity_invalid")
    print(json.dumps({"database": str(database), "fixture": "e2e-v1"}, sort_keys=True))


if __name__ == "__main__":
    main()

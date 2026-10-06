from __future__ import annotations

from contextlib import closing
from pathlib import Path

from app import storage
from app.context_pack import (
    _prepend_structured_price_documents,
    latest_price_evidence,
)
from app.models import RagEvidence, RagSearchResponse
from app.settings import settings


def _seed(tmp_path: Path) -> None:
    object.__setattr__(settings, "sqlite_path", str(tmp_path / "price-evidence.db"))
    storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
    with closing(storage.connect()) as connection:
        connection.execute(
            """INSERT INTO market_observations VALUES(
                'obs-poy-1','2026-09-20T09:00:00+00:00','tnc_polyester_history','2026-09-20',
                '涤纶POY public recent average','poy',9700.0,'CNY/mt','published_day',
                'China','https://tnc.example/poy','全球纺织网公开近期行情均价','{}')"""
        )
        # A non-price unit row for meg must never be promoted.
        connection.execute(
            """INSERT INTO market_observations VALUES(
                'obs-meg-customs','2026-09-01T09:00:00+00:00','gacc_trade_statistics','2026-08-31',
                'China monthly imports quantity - ethylene glycol','meg',120000.0,'metric_tonnes','monthly',
                'China','https://gacc.example/meg','海关进口量','{}')"""
        )
        connection.execute(
            """INSERT INTO futures_daily_bars VALUES(
                'bar-px-1','2026-09-19T02:00:00+00:00','2026-09-18','CZCE','PX','PX611','next_month',
                1,1,1,9100.0,9300.0,9050.0,9200.0,9238.0,0.0,0.0,0.0,'CNY/mt',
                '2026-09-18T15:00:00+08:00','2026-09-18T15:00:00+08:00','czce_pta_px','郑商所',
                'https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20260918/FutureDataDaily.txt',
                '','','','','{}')"""
        )
        connection.execute(
            """INSERT INTO intraday_price_observations VALUES(
                'intraday-meg-1','2026-09-21T03:30:00+00:00','MEG','MEG',
                '2026-09-21T11:30:00+08:00',900,'exchange_proxy',6029.0,null,null,null,null,null,
                'CNY/mt','sina_global_futures','https://hq.sinajs.cn/list=MEG',0.2,'ok','备注','{}')"""
        )
        connection.commit()


def _teardown(original: str) -> None:
    storage._MIGRATED_PATHS.discard(Path(settings.sqlite_path))
    object.__setattr__(settings, "sqlite_path", original)


def test_price_question_promotes_latest_structured_observations(tmp_path: Path) -> None:
    original = settings.sqlite_path
    _seed(tmp_path)
    try:
        documents = latest_price_evidence("POY 最新价格是多少？请给出观测日期与来源。")
        assert len(documents) == 1
        assert documents[0].doc_id == "market:obs-poy-1"
        assert documents[0].doc_type == "market_observation"
        assert "9700.0 CNY/mt" in documents[0].summary
        assert documents[0].metadata.get("structured_price") is True

        # PX resolves to the is_main settlement bar even when its
        # contract_role is next_month (documented PX611 case).
        documents = latest_price_evidence("PX 现在什么价格")
        assert [d.doc_id for d in documents] == ["futures:bar-px-1"]
        assert documents[0].tier == "A"
        assert "9238.0 CNY/mt" in documents[0].summary

        # MEG falls back to the intraday quote table; customs volumes never win.
        documents = latest_price_evidence("MEG 最新价")
        assert [d.doc_id for d in documents] == ["intraday:intraday-meg-1"]
        assert "6029.0 CNY/mt" in documents[0].summary

        # Multi-product questions get one observation per product.
        documents = latest_price_evidence("PX 和 MEG 最新价格")
        assert {d.doc_id for d in documents} == {"futures:bar-px-1", "intraday:intraday-meg-1"}
    finally:
        _teardown(original)


def test_non_price_questions_and_off_scope_products_stay_unpromoted(tmp_path: Path) -> None:
    original = settings.sqlite_path
    _seed(tmp_path)
    try:
        assert latest_price_evidence("今天天气怎么样") == []
        assert latest_price_evidence("PX 的供需格局分析") == []
        assert latest_price_evidence("纯碱价格") == []
    finally:
        _teardown(original)


def test_prepend_puts_structured_price_first_and_dedupes(tmp_path: Path) -> None:
    original = settings.sqlite_path
    _seed(tmp_path)
    try:
        news_doc = RagEvidence(
            doc_id="news:1", doc_type="news_article", source_id="s", tier="C",
            title="锦纶新闻", summary="锦纶长丝价格", url="https://x", observed_at="2026-09-20",
            visible_at="2026-09-20",
        )
        retrieval = RagSearchResponse(
            query="POY 最新价格",
            documents=[news_doc],
            evidence_level="C",
            confidence=0.3,
        )
        _prepend_structured_price_documents(retrieval, "POY 最新价格是多少？", as_of_time=None)
        assert [d.doc_id for d in retrieval.documents] == ["market:obs-poy-1", "news:1"]

        # Idempotent: a second prepend must not duplicate the promoted doc.
        _prepend_structured_price_documents(retrieval, "POY 最新价格是多少？", as_of_time=None)
        assert [d.doc_id for d in retrieval.documents] == ["market:obs-poy-1", "news:1"]

        # No price intent -> documents untouched.
        retrieval2 = RagSearchResponse(
            query="事件", documents=[news_doc], evidence_level="C", confidence=0.3
        )
        _prepend_structured_price_documents(retrieval2, "最近有什么事件", as_of_time=None)
        assert [d.doc_id for d in retrieval2.documents] == ["news:1"]
    finally:
        _teardown(original)

"""Synthetic business semantics: not evidence of market predictive skill."""

import copy
import hashlib
import json
import threading
import time
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from test_prediction_evidence_runtime import prices, receipt, reseal_rows

from app import evidence_dossier_service as service
from app.evidence_dossier import build_dossier
from app.prediction_evidence_runtime import build_facts, joint_input
from app.prediction_main import MainInputSnapshot


def sample(product="PTA", prefix="", action="已停产", date="2026年9月25日"):
    quote = f"{prefix}{date}，甲公司{product}装置{action}。"
    other = f"甲公司{product}装置的公告由公司发布。"
    text = quote + other
    body = receipt()
    payload = copy.deepcopy(body["rows"][0]["payload"])
    a, s = payload["article"], payload["summary"]
    a.update(
        article_id="sample",
        title=f"{product}公告",
        raw_text=text,
        published_at="2026-09-25T07:00:00+00:00",
        first_seen_at="2026-09-25T07:10:00+00:00",
    )
    a["content_hash"] = hashlib.sha256((a["title"] + "\n" + text).encode()).hexdigest()
    s.update(
        source_hash=a["content_hash"],
        fact_payload={
            "subject": "甲公司",
            "action": action,
            "object": f"{product}装置",
            "occurred_at": "2026-09-25",
            "location": "",
            "numbers": [],
            "evidence_quotes": [quote, other],
            "source_language": "zh",
        },
    )
    body["rows"] = [{"payload": payload}]
    return reseal_rows(body)


def dossier(body):
    return build_dossier(body, prices(), build_facts(body))


@pytest.mark.parametrize(
    "product,target",
    [
        ("原油", "crude"),
        ("石脑油", "naphtha"),
        ("PX", "px"),
        ("PTA", "pta"),
        ("乙二醇", "meg"),
        ("涤纶POY", "poy"),
        ("涤纶DTY", "dty"),
    ],
)
def test_seven_products_actual_supply_and_21_cells(product, target):
    result = dossier(sample(product))
    assert len(result["cells"]) == 21
    cell = result["cells"][target + ":1"]
    assert cell["current_support_episodes"] == 1
    assert cell["current_counter_episodes"] == 0
    assert result["model_effect"] == "context_only"
    assert not result["accuracy_improvement_required"]
    assert not result["forecast_effect_validated"]


@pytest.mark.parametrize(
    "prefix,action,state",
    [
        ("据市场传闻，", "已停产", "unconfirmed"),
        ("", "计划停产", "planned"),
        ("", "尚未停产", "denied"),
        ("", "重启中", "in_progress"),
        ("如果", "已停产", "planned"),
    ],
)
def test_epistemic_states_never_become_directional_evidence(prefix, action, state):
    result = dossier(sample(prefix=prefix, action=action))
    cell = result["cells"]["pta:1"]
    assert not cell["current_support"] and not cell["current_counter"]
    assert any(c["state"] == state for c in result["claims"])
    assert cell["other_materials"]


def test_sentence_scope_binds_date_and_subject_from_same_sentence():
    # The quote itself lacks the date and uses a pronoun-free short span; the
    # enclosing sentence (verbatim raw slice) carries both. Same-sentence
    # binding admits the claim; the provenance span stays the quote.
    text = "2026年9月24日，甲公司在季度报告中披露，其PTA装置已停产。"
    quote = "其PTA装置已停产。"
    body = sample()
    payload = copy.deepcopy(body["rows"][0]["payload"])
    a, s = payload["article"], payload["summary"]
    a.update(article_id="sentence-scope", raw_text=text, title="PTA公告")
    a["content_hash"] = hashlib.sha256((a["title"] + "\n" + text).encode()).hexdigest()
    s["source_hash"] = a["content_hash"]
    s["fact_payload"] = {
        "subject": "甲公司",
        "action": "已停产",
        "object": "PTA装置",
        "occurred_at": "2026-09-24",
        "location": "",
        "numbers": [],
        "evidence_quotes": [quote, "季度报告中披露"],
        "source_language": "zh",
    }
    body["rows"] = [{"payload": payload}]
    result = dossier(reseal_rows(body))
    cell = result["cells"]["pta:1"]
    assert cell["current_support_episodes"] == 1
    claim = next(c for c in result["claims"] if c["claim_id"] == cell["current_support"][0])
    assert claim["quote"] == quote  # provenance anchor unchanged
    assert claim["event_date"] == "2026-09-24"


def test_sentence_scope_still_rejects_cross_paragraph_and_ambiguous_dates():
    # Subject only in a different paragraph must stay rejected (no borrowing).
    separated = "甲公司发布季度报告。\n其PTA装置已停产。"
    # Two dates in the enclosing sentence stay ambiguous -> rejected.
    ambiguous = "2026年9月20日发布公告，2026年9月24日其PTA装置已停产。"
    for text in (separated, ambiguous):
        body = sample()
        payload = copy.deepcopy(body["rows"][0]["payload"])
        a, s = payload["article"], payload["summary"]
        a.update(article_id=f"reject-{abs(hash(text))}", raw_text=text, title="PTA公告")
        a["content_hash"] = hashlib.sha256((a["title"] + "\n" + text).encode()).hexdigest()
        s["source_hash"] = a["content_hash"]
        s["fact_payload"] = {
            "subject": "甲公司",
            "action": "已停产",
            "object": "PTA装置",
            "occurred_at": "2026-09-24",
            "location": "",
            "numbers": [],
            "evidence_quotes": ["其PTA装置已停产。", "季度报告中披露"],
            "source_language": "zh",
        }
        body["rows"] = [{"payload": payload}]
        result = dossier(reseal_rows(body))
        assert not result["cells"]["pta:1"]["current_support"]
        assert any(c["semantic_status"] == "needs_review" for c in result["claims"])


def test_no_date_or_multi_product_never_auto_admitted():
    for body in [sample(date="今日"), sample(product="PTA与乙二醇")]:
        result = dossier(body)
        assert not result["cells"]["pta:1"]["current_support"]


def test_same_episode_reprints_do_not_inflate_counts_and_opposites_stay_mixed():
    body = sample()
    row = copy.deepcopy(body["rows"][0])
    row["payload"]["article"].update(article_id="reprint", canonical_url="https://example.test/reprint")
    body["rows"].append(row)
    body = reseal_rows(body)
    result = dossier(body)
    assert result["cells"]["pta:1"]["current_support_episodes"] == 1
    body["rows"].extend(sample(action="已重启")["rows"])
    result = dossier(reseal_rows(body))
    cell = result["cells"]["pta:1"]
    assert cell["mixed"] and not cell["current_support"] and not cell["current_counter"]


def test_raw_integrity_future_versions_and_nylon_remain_excluded():
    body = sample()
    body["rows"][0]["payload"]["content_visible_at"] = "2026-09-29T00:00:00Z"
    assert dossier(reseal_rows(body))["claims"] == []
    assert not dossier(sample(product="锦纶POY"))["cells"]["poy:1"]["current_support"]
    body = sample()
    body["rows"][0]["payload"]["article"]["raw_text"] += "修改"
    assert dossier(reseal_rows(body))["claims"] == []


def test_dossier_v2_rebuild_and_v1_compatibility():
    body = sample()
    v1 = joint_input(prices(), body)
    assert MainInputSnapshot(v1).evidence_dossier is None
    v2 = joint_input(prices(), body, include_dossier=True)
    assert MainInputSnapshot(v2).evidence_dossier["cells"]["pta:1"]["current_support"]
    assert v2["evidence_features"] == v1["evidence_features"]
    from app.prediction_evidence_runtime import seal

    bad = copy.deepcopy(v2)
    bad["evidence_dossier"]["cells"]["pta:1"]["current_support_episodes"] = 99
    bad.pop("content_sha256")
    with pytest.raises(ValueError, match="reconstruction"):
        MainInputSnapshot(seal(bad))


def test_read_api_current_is_single_flight_no_persist_and_pagination(monkeypatch):
    service._CACHE.clear()
    calls = []

    def capture(as_of):
        calls.append(as_of)
        return MainInputSnapshot(joint_input(prices(), sample(), include_dossier=True))

    monkeypatch.setattr(service, "capture_main_inputs", capture)
    monkeypatch.setattr(MainInputSnapshot, "persist", lambda *a: pytest.fail("GET persisted"))
    from app.main import app

    client = TestClient(app)
    response = client.get("/api/v1/forecasts/seven-product/evidence?view=current&target=pta&limit=1")
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["claims"]) == 1 and body["current_support_episodes"] == 1
    assert body["batch_id"] is None
    again = client.get("/api/v1/forecasts/seven-product/evidence?view=current&target=poy")
    assert again.status_code == 200 and len(calls) == 1
    assert client.get("/api/v1/forecasts/seven-product/evidence?target=wrong").status_code == 422
    assert client.get("/api/v1/forecasts/seven-product/evidence?horizon_days=14").status_code == 422
    assert client.get("/api/v1/forecasts/seven-product/evidence?offset=-1").status_code == 422
    assert body["model_effect"] == "context_only"
    assert "raw_text" not in json.dumps(body)
    service._CACHE.clear()


def test_empty_issued_and_archive_failure_are_not_empty_evidence(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(service, "get_latest_issued_seven_product_forecast", lambda: None)
    assert service.read_dossier(target="pta", horizon=1, view="issued").status == "unavailable"
    monkeypatch.setattr(service, "_issued_snapshot", lambda: (_ for _ in ()).throw(ValueError("bad archive")))
    from app.main import app

    assert TestClient(app).get("/api/v1/forecasts/seven-product/evidence").status_code == 503


@pytest.mark.parametrize("horizon", (1, 7, 30))
def test_explicit_http_horizon_is_parsed_as_integer(monkeypatch, horizon):
    service._CACHE.clear()
    monkeypatch.setattr(
        service,
        "capture_main_inputs",
        lambda _: MainInputSnapshot(joint_input(prices(), sample(), include_dossier=True)),
    )
    from app.main import app

    response = TestClient(app).get(
        "/api/v1/forecasts/seven-product/evidence",
        params={"target": "pta", "view": "current", "horizon_days": str(horizon)},
    )
    assert response.status_code == 200, response.text
    assert response.json()["horizon_days"] == horizon
    service._CACHE.clear()


def test_pagination_rejects_cross_snapshot_identity(monkeypatch):
    service._CACHE.clear()
    monkeypatch.setattr(
        service,
        "capture_main_inputs",
        lambda _: MainInputSnapshot(joint_input(prices(), sample(), include_dossier=True)),
    )
    with pytest.raises(ValueError, match="view_changed"):
        service.read_dossier(target="pta", horizon=1, view="current", offset=50, input_sha256="f" * 64)
    service._CACHE.clear()


def test_pagination_pin_survives_current_refresh_but_is_bounded(monkeypatch):
    service._CACHE.clear()
    now = [0.0]
    monkeypatch.setattr(service, "_clock", lambda: now[0])
    versions = [
        MainInputSnapshot(joint_input(prices(), sample(date=f"2026年9月{day}日"), include_dossier=True))
        for day in (20, 21, 22, 23)
    ]
    pending = iter(versions)
    monkeypatch.setattr(service, "capture_main_inputs", lambda _: next(pending))
    first = service.read_dossier(target="pta", horizon=1, view="current")
    # Explicit refresh must capture newly, not keep serving the old snapshot.
    now[0] = 61
    newer = service.read_dossier(target="pta", horizon=1, view="current", refresh=True)
    assert first.input_sha256 != newer.input_sha256
    pinned = service.read_dossier(
        target="pta",
        horizon=1,
        view="current",
        offset=50,
        input_sha256=first.input_sha256,
    )
    assert pinned.input_sha256 == first.input_sha256
    # Capacity evicts the oldest identity, without substituting newer data.
    for tick in (122, 183):
        now[0] = tick
        service.read_dossier(target="pta", horizon=1, view="current", refresh=True)
    # :current is v23; pins retain the three newest identities (v21, v22, v23).
    assert len({entry[1].sha256 for entry in service._CACHE.values()}) == 3
    with pytest.raises(ValueError, match="view_changed"):
        service.read_dossier(target="pta", horizon=1, view="current", input_sha256=first.input_sha256)
    now[0] = 700
    with pytest.raises(ValueError, match="view_changed"):
        service.read_dossier(target="pta", horizon=1, view="current", input_sha256=newer.input_sha256)
    service._CACHE.clear()


def test_expired_current_serves_previous_snapshot_while_revalidating(monkeypatch):
    """TTL 过期不再让用户等 15-30s：立即返回上次快照（revalidating 标记），
    一次共享重建完成后，下一次读取取得新资料；as_of 始终如实标注。"""
    service._CACHE.clear()
    now = [0.0]
    monkeypatch.setattr(service, "_clock", lambda: now[0])
    versions = [
        MainInputSnapshot(joint_input(prices(), sample(date=f"2026年9月{day}日"), include_dossier=True))
        for day in (20, 21)
    ]
    pending = iter(versions)
    monkeypatch.setattr(service, "capture_main_inputs", lambda _: next(pending))
    first = service.read_dossier(target="pta", horizon=1, view="current")
    assert first.revalidating is False

    # Deterministic stand-in for the background rebuild: run its body inline.
    def sync_spawn():
        snapshot = service.capture_main_inputs(datetime.now(UTC))
        with service._CACHE_LOCK:
            built = (service._clock(), snapshot)
            service._CACHE[f"{service.settings.sqlite_path}:current"] = built
            service._CACHE[f"{service.settings.sqlite_path}:{snapshot.sha256}"] = built

    monkeypatch.setattr(service, "_spawn_current_rebuild", sync_spawn)
    now[0] = 61
    stale = service.read_dossier(target="pta", horizon=1, view="current")
    assert stale.input_sha256 == first.input_sha256
    assert stale.revalidating is True
    assert stale.as_of_time == first.as_of_time
    # The rebuild landed; the next read is fresh again without waiting.
    refreshed = service.read_dossier(target="pta", horizon=1, view="current")
    assert refreshed.input_sha256 != first.input_sha256
    assert refreshed.revalidating is False
    service._CACHE.clear()


def test_refresh_is_rejected_outside_current_product_view():
    from app.main import app

    client = TestClient(app)
    assert client.get("/api/v1/forecasts/seven-product/evidence?view=issued&refresh=true").status_code == 422
    assert (
        client.get("/api/v1/forecasts/seven-product/evidence?view=current&refresh=true&event_id=e1").status_code == 422
    )


def test_prices_are_separate_not_double_counted_and_historical_materials_visible():
    body = sample(action="计划停产")
    body["rows"][0]["payload"]["article"]["published_at"] = "2026-09-15T07:00:00Z"
    body = reseal_rows(body)
    result = dossier(body)
    cell = result["cells"]["pta:1"]
    assert cell["market_baseline"]["value"] is not None
    assert cell["market_baseline"]["role"] == "price_baseline_not_independent_event_evidence"
    assert cell["other_materials"] and not cell["current_support"]
    assert any(c["stored_claims"] and not c["source_claims"] for c in cell["coverage"])


def test_openapi_endpoint_and_public_projection_contract():
    import pathlib

    import yaml

    from app.main import app

    schema = app.openapi()
    recorded = yaml.safe_load((pathlib.Path(__file__).parents[2] / "docs/openapi.yaml").read_text())
    path = "/api/v1/forecasts/seven-product/evidence"
    assert schema["paths"][path] == recorded["paths"][path]
    for name in ("EvidenceDossierResponse", "DossierClaim", "DossierHistorical", "DossierCoverage"):
        assert schema["components"]["schemas"][name] == recorded["components"]["schemas"][name]


def test_recent_article_cannot_refresh_old_supply_event_into_current_support():
    body = sample(date="2026年8月25日")
    s = body["rows"][0]["payload"]["summary"]
    s["fact_payload"]["occurred_at"] = "2026-08-25"
    result = dossier(reseal_rows(body))
    assert not result["cells"]["pta:1"]["current_support"]
    assert result["cells"]["pta:1"]["other_materials"]


def test_stale_snapshot_pin_over_http_is_422_with_reload_code(monkeypatch):
    """过期快照 pin 是客户端状态：422 + 机器可读 code，前端据此自动重读。"""
    service._CACHE.clear()
    monkeypatch.setattr(
        service,
        "capture_main_inputs",
        lambda _: MainInputSnapshot(joint_input(prices(), sample(), include_dossier=True)),
    )
    from app.main import app

    client = TestClient(app)
    assert client.get("/api/v1/forecasts/seven-product/evidence?target=pta&view=current").status_code == 200
    stale = client.get(
        "/api/v1/forecasts/seven-product/evidence",
        params={"target": "pta", "view": "current", "offset": 50, "input_sha256": "f" * 64},
    )
    assert stale.status_code == 422
    assert stale.json()["error"]["code"] == "evidence_view_changed_reload_first_page"
    service._CACHE.clear()


def test_concurrent_current_reads_join_inflight_capture(monkeypatch):
    """冷读期间并发读取等待同一份快照，而不是 1 秒后 503。"""
    import time

    service._CACHE.clear()
    started = threading.Event()
    release = threading.Event()

    def slow_capture(as_of):
        started.set()
        assert release.wait(timeout=10)
        return MainInputSnapshot(joint_input(prices(), sample(), include_dossier=True))

    monkeypatch.setattr(service, "capture_main_inputs", slow_capture)
    results: dict[str, object] = {}
    errors: dict[str, BaseException] = {}

    def reader(name):
        try:
            results[name] = service.read_dossier(target="pta", horizon=1, view="current")
        except BaseException as exc:  # noqa: BLE001 - test harness records any failure
            errors[name] = exc

    first = threading.Thread(target=reader, args=("first",))
    first.start()
    assert started.wait(timeout=5)
    second = threading.Thread(target=reader, args=("second",))
    second.start()
    time.sleep(0.3)  # let the second reader block on the capture lock
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not errors, errors
    assert results["first"].input_sha256 == results["second"].input_sha256
    service._CACHE.clear()


def test_pinned_read_is_not_blocked_by_a_slow_current_build(monkeypatch):
    """构建期间：命中缓存的 pin 读取与不同键读取不被全局串行化。"""

    service._CACHE.clear()
    now = [0.0]
    monkeypatch.setattr(service, "_clock", lambda: now[0])
    seed = MainInputSnapshot(joint_input(prices(), sample(), include_dossier=True))
    with service._CACHE_LOCK:
        service._CACHE[f"{service.settings.sqlite_path}:{seed.sha256}"] = (now[0], seed)
    started = threading.Event()
    release = threading.Event()

    def slow_capture(as_of):
        started.set()
        assert release.wait(timeout=10)
        return seed

    monkeypatch.setattr(service, "capture_main_inputs", slow_capture)
    errors: dict[str, BaseException] = {}
    pinned_result: dict[str, object] = {}

    def pinned_reader():
        try:
            pinned_result["dossier"] = service.read_dossier(
                target="pta", horizon=1, view="current", input_sha256=seed.sha256
            )
        except BaseException as exc:  # noqa: BLE001
            errors["pinned"] = exc

    blocker = threading.Thread(
        target=lambda: _ignore(lambda: service.read_dossier(target="meg", horizon=1, view="current"), errors, "current")
    )
    blocker.start()
    assert started.wait(timeout=5)
    reader = threading.Thread(target=pinned_reader)
    reader.start()
    reader.join(timeout=5)
    # The pinned cache hit completed while the current build is still running.
    assert not errors, errors
    assert pinned_result["dossier"].input_sha256 == seed.sha256
    release.set()
    blocker.join(timeout=10)
    service._CACHE.clear()


def _ignore(call, errors: dict, name: str) -> None:
    try:
        call()
    except BaseException as exc:  # noqa: BLE001
        errors[name] = exc


def test_pinned_read_of_expired_current_surfaces_revalidating(monkeypatch):
    """pin 命中已过 TTL 的当前快照时必须标 revalidating，并触发共享重建。"""
    service._CACHE.clear()
    now = [0.0]
    monkeypatch.setattr(service, "_clock", lambda: now[0])
    versions = [
        MainInputSnapshot(joint_input(prices(), sample(date=f"2026年9月{day}日"), include_dossier=True))
        for day in (20, 21)
    ]
    pending = iter(versions)
    monkeypatch.setattr(service, "capture_main_inputs", lambda _: next(pending))
    first = service.read_dossier(target="pta", horizon=1, view="current")

    spawned: list[bool] = []
    monkeypatch.setattr(service, "_spawn_current_rebuild", lambda: spawned.append(True))
    now[0] = 61
    # Same snapshot via its pin (as a product switch does): staleness is visible.
    pinned = service.read_dossier(target="meg", horizon=7, view="current", input_sha256=first.input_sha256)
    assert pinned.revalidating is True
    assert pinned.input_sha256 == first.input_sha256
    assert spawned == [True]
    # A freshly captured current entry does not flag revalidating through the
    # pin path even when its own pin timestamp is old.
    second = next(pending)
    now[0] = 70
    with service._CACHE_LOCK:
        service._CACHE[f"{service.settings.sqlite_path}:current"] = (now[0], second)
        service._CACHE[f"{service.settings.sqlite_path}:{second.sha256}"] = (now[0], second)
    fresh = service.read_dossier(target="dty", horizon=1, view="current", input_sha256=second.sha256)
    assert fresh.revalidating is False
    service._CACHE.clear()


def test_stale_reads_spawn_at_most_one_background_rebuild(monkeypatch):
    """SWR 防抖：过期读只允许一个后台重建；并发的其他过期读不再叠加扫描。"""

    service._CACHE.clear()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls: list[int] = []
    real_thread = threading.Thread

    class CountingThread(real_thread):
        def run(self):
            if self.name == "evidence-current-revalidate":
                calls.append(1)
                try:
                    super().run()
                finally:
                    finished.set()
            else:
                super().run()

    def slow_capture(as_of):
        started.set()
        assert release.wait(timeout=10)
        return MainInputSnapshot(joint_input(prices(), sample(), include_dossier=True))

    monkeypatch.setattr(service, "capture_main_inputs", slow_capture)
    monkeypatch.setattr(threading, "Thread", CountingThread)

    seed = MainInputSnapshot(joint_input(prices(), sample(), include_dossier=True))
    now = [0.0]
    monkeypatch.setattr(service, "_clock", lambda: now[0])
    with service._CACHE_LOCK:
        service._CACHE[f"{service.settings.sqlite_path}:current"] = (now[0], seed)
    now[0] = 61  # TTL 过期

    # 五个并发过期读：都立即拿到旧快照 + revalidating，但只 spawn 一个重建线程。
    results: list = []
    errors: list = []
    readers = []
    for _ in range(5):
        reader = threading.Thread(target=lambda: _swr_read(service, results, errors))
        readers.append(reader)
        reader.start()
        time.sleep(0.05)
    assert started.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=5)
    assert not errors, errors
    assert all(r["revalidating"] and r["sha"] == seed.sha256 for r in results), results
    assert len(calls) == 1, f"expected exactly one rebuild thread, got {len(calls)}"
    release.set()
    assert finished.wait(timeout=10)
    service._CACHE.clear()


def _swr_read(service, results: list, errors: list) -> None:
    try:
        d = service.read_dossier(target="pta", horizon=1, view="current")
        results.append({"revalidating": d.revalidating, "sha": d.input_sha256})
    except BaseException as exc:  # noqa: BLE001
        errors.append(exc)


def test_dated_price_report_title_is_a_report_period_not_execution_date():
    body = sample(product="涤纶POY", date="")
    p = body["rows"][0]["payload"]
    p["article"].update(
        title="涤纶POY商品报价动态（2026-09-25）", raw_text="交易商 涤纶POY最新报价9350元/吨。交易商披露出厂现汇口径。"
    )
    p["article"]["content_hash"] = hashlib.sha256(
        (p["article"]["title"] + "\n" + p["article"]["raw_text"]).encode()
    ).hexdigest()
    p["summary"].update(
        source_hash=p["article"]["content_hash"],
        fact_payload={
            "subject": "交易商",
            "action": "报价",
            "object": "涤纶POY",
            "occurred_at": "2026-09-25",
            "location": "",
            "numbers": [],
            "evidence_quotes": ["交易商 涤纶POY最新报价9350元/吨。", "交易商披露出厂现汇口径。"],
            "source_language": "zh",
        },
    )
    body = reseal_rows(body)
    result = dossier(body)
    claim = next(c for c in result["claims"] if c["mechanism"] == "price")
    assert claim["event_date"] == "2026-09-25"
    assert claim["event_date_source"] == "dated_price_report_title"
    assert claim["semantic_status"] == "needs_review"
    assert "缺少明确且唯一的发生日期或报告期" not in claim["gaps"]
    p["article"]["title"] = "涤纶POY装置停产（2026-09-25）"
    p["article"]["content_hash"] = hashlib.sha256(
        (p["article"]["title"] + "\n" + p["article"]["raw_text"]).encode()
    ).hexdigest()
    p["summary"]["source_hash"] = p["article"]["content_hash"]
    assert all(c["event_date"] is None for c in dossier(reseal_rows(body))["claims"])


def test_nylon_title_qualifies_short_numeric_quote_and_legacy_snapshot_rebuilds():
    from app.evidence_dossier import LEGACY_POLICY

    body = sample(product="POY")
    p = body["rows"][0]["payload"]
    p["article"]["title"] = "锦纶长丝价格动态"
    p["article"]["content_hash"] = hashlib.sha256(
        (p["article"]["title"] + "\n" + p["article"]["raw_text"]).encode()
    ).hexdigest()
    p["summary"]["source_hash"] = p["article"]["content_hash"]
    body = reseal_rows(body)
    assert not dossier(body)["claims"]
    old = joint_input(prices(), body, include_dossier=True, dossier_policy=LEGACY_POLICY)
    assert MainInputSnapshot(old).evidence_dossier["schema_version"] == LEGACY_POLICY
    assert old["evidence_dossier"]["claims"]


@pytest.mark.parametrize(
    "title_date,price_day",
    [
        ("2026-09-25", "2026-09-25"),
        ("2026-09-26", None),  # Future report periods are not facts at publication.
        ("2026-09-24、2026-09-25", None),
    ],
)
def test_report_period_cannot_date_a_shutdown_in_the_same_quote(title_date, price_day):
    body = sample(product="涤纶POY", date="", action="已停产，报价9350元/吨")
    p = body["rows"][0]["payload"]
    p["article"]["title"] = f"涤纶POY价格动态（{title_date}）"
    p["article"]["content_hash"] = hashlib.sha256(
        (p["article"]["title"] + "\n" + p["article"]["raw_text"]).encode()
    ).hexdigest()
    p["summary"]["source_hash"] = p["article"]["content_hash"]
    claims = dossier(reseal_rows(body))["claims"]
    price = next(c for c in claims if c["mechanism"] == "price")
    supply = next(c for c in claims if c["mechanism"] == "supply")
    assert price["event_date"] == price_day
    assert supply["event_date"] is None
    assert supply["semantic_status"] == "needs_review"


@pytest.mark.parametrize(
    "text,published,expected",
    [
        ("September 10 attacks", "2026-09-25", "2026-09-10"),
        ("10 September 2026 attacks", "2026-09-25", "2026-09-10"),
        ("On September 10th, 2026", "2026-09-25", "2026-09-10"),
        ("December 31", "2026-01-02", "2025-12-31"),
        ("September 10 and September 11", "2026-09-25", None),
        ("September 10-12", "2026-09-25", None),
        ("September 10 through 12", "2026-09-25", None),
        ("September 26, 2026", "2026-09-25", None),
        ("February 30, 2026", "2026-03-01", None),
        ("earlier today", "2026-09-25", None),
        ("September", "2026-09-25", None),
    ],
)
def test_source_anchor_dates_are_explicit_not_publication_substitutes(text, published, expected):
    from app.evidence_dossier import _date

    day = datetime.fromisoformat(published)
    assert _date(text, day, source_anchors=True) == expected
    # v1/v2 historical reconstruction must keep their original semantics.
    assert _date(text, day) is None


def test_english_subjects_are_per_claim_literal_spans_not_article_translation():
    from app.evidence_dossier import _claim_subject, _direction
    from app.models import EventFactExtraction

    facts = EventFactExtraction(
        subject="标准渣打银行分析师",
        action="表示",
        object="原油供应",
        source_language="en",
        evidence_quotes=[
            "Saudi Arabia has halted loadings at the port.",
            "Drone attacks have forced a crude pipeline shutdown.",
        ],
    )
    first, second = facts.evidence_quotes
    assert _claim_subject(facts, first, first) == ("Saudi Arabia", True)
    assert _claim_subject(facts, second, second) == ("Drone attacks", True)
    assert _claim_subject(facts, "It has closed the port.", "It has closed the port.")[1] is False
    direction, gaps = _direction(
        first, first, facts, "logistics", "2026-09-25", first, source_anchors=True, subject_bound=True
    )
    assert direction is None
    assert gaps == ["系统尚未实现航运与物流方向核验，保留待核验"]


def test_source_anchor_cues_use_english_word_boundaries():
    import re

    from app.evidence_dossier import SOURCE_ANCHOR_MECHANISMS

    cues = SOURCE_ANCHOR_MECHANISMS["logistics"]["cues"]
    for text in ("API reported crude inventory gains", "crude imports rose", "oil exports fell", "support improved"):
        assert not re.search(cues, text, re.I)
    for text in ("Saudi port closed", "crude pipeline damaged", "Strait of Hormuz shipping blockade"):
        assert re.search(cues, text, re.I)


def test_source_date_v2_archive_still_reconstructs_without_v3_anchor_rules():
    from app.evidence_dossier import SOURCE_DATE_POLICY

    archived = joint_input(prices(), sample(), include_dossier=True, dossier_policy=SOURCE_DATE_POLICY)
    assert MainInputSnapshot(archived).evidence_dossier["schema_version"] == SOURCE_DATE_POLICY


def test_price_background_diagnostic_does_not_request_direction_verification():
    from app.evidence_dossier import _direction
    from app.models import EventFactExtraction

    quote = "2026年9月25日，甲公司涤纶POY报价9350元/吨。"
    facts = EventFactExtraction(
        subject="甲公司", action="报价", object="涤纶POY", evidence_quotes=[quote, "甲公司报价"], source_language="zh"
    )
    direction, gaps = _direction(quote, quote, facts, "price", "2026-09-25", source_anchors=True)
    assert direction is None
    assert gaps == ["价格背景，不作为独立事件方向证据"]


def test_english_event_claims_keep_source_entities_and_do_not_gain_direction_votes():
    body = sample(product="原油")
    payload = body["rows"][0]["payload"]
    quote = "On September 10, Saudi Arabia has halted crude loadings at the Yanbu port."
    other = "On September 10, Drone attacks have forced a crude pipeline shutdown."
    article, summary = payload["article"], payload["summary"]
    article.update(title="Saudi crude export disruption", raw_text=quote + "\n" + other)
    article["content_hash"] = hashlib.sha256((article["title"] + "\n" + article["raw_text"]).encode()).hexdigest()
    summary.update(
        source_hash=article["content_hash"],
        fact_payload={
            "subject": "标准渣打银行分析师",
            "action": "表示",
            "object": "原油供应",
            "occurred_at": "2026-09-10",
            "location": "",
            "numbers": [],
            "evidence_quotes": [quote, other],
            "source_language": "en",
        },
    )
    result = dossier(reseal_rows(body))
    claims = [claim for claim in result["claims"] if claim["mechanism"] == "logistics"]
    assert len(claims) == 2
    assert {claim["subject"] for claim in claims} == {"Saudi Arabia", "Drone attacks"}
    assert all(claim["event_date"] == "2026-09-10" for claim in claims)
    assert all(claim["semantic_status"] == "needs_review" and claim["expected_direction"] is None for claim in claims)
    assert all("缺少明确且唯一的发生日期或报告期" not in claim["gaps"] for claim in claims)
    assert result["cells"]["crude:1"]["current_support"] == []
    assert result["cells"]["crude:1"]["current_counter"] == []


@pytest.mark.parametrize(
    "mechanism,quote,expected,label",
    [
        ("supply", "Saudi Arabia has confirmed 原油装置已停产。", "up", "供应与装置"),
        ("demand", "Saudi Arabia has confirmed 原油需求量同比增长10%。", "up", "需求与订单"),
    ],
)
def test_v3_english_subject_anchor_does_not_enable_chinese_rules_in_mixed_quotes(mechanism, quote, expected, label):
    from app.evidence_dossier import _claim_subject, _direction
    from app.models import EventFactExtraction

    facts = EventFactExtraction(
        subject="Saudi Arabia",
        action="confirmed",
        object="原油",
        source_language="en",
        evidence_quotes=[quote, "Saudi Arabia has confirmed crude figures."],
    )
    subject, bound = _claim_subject(facts, quote, quote)
    assert subject == "Saudi Arabia" and bound
    direction, gaps = _direction(quote, quote, facts, mechanism, "2026-09-25", source_anchors=True, subject_bound=bound)
    assert direction is None
    assert gaps == [f"系统尚未实现英文{label}方向核验，保留待核验"]
    # v1/v2 reconstruction keeps its prior exact-subject behavior, even if an
    # archived hybrid quote originally passed its Chinese mechanism regex.
    assert _direction(quote, quote, facts, mechanism, "2026-09-25") == (expected, [])


def test_v3_chinese_rule_checked_supply_is_unchanged():
    result = dossier(sample())
    claim = next(c for c in result["claims"] if c["mechanism"] == "supply")
    assert claim["semantic_status"] == "rule_checked"
    assert claim["expected_direction"] == "up"


def test_english_adjacent_forecast_does_not_contaminate_real_inventory():
    from app.evidence_dossier import _sentence_span
    from app.evidence_mechanism_checks import check_mechanism

    quote = (
        "Crude oil inventories in the United States saw an increase of 900,000 barrels "
        "during the week ending September 25, according to EIA."
    )
    raw = "Analysts forecast higher prices. " + quote + " Analysts expect a draw next week."
    old = _sentence_span(raw, quote)
    assert check_mechanism(quote, old, "inventory", "2026-09-25") is None
    current = _sentence_span(raw, quote, english_boundaries=True)
    assert current == quote
    assert check_mechanism(quote, current, "inventory", "2026-09-25")[0] == "down"


@pytest.mark.parametrize("prefix", ["Analysts forecast that ", "EIA expects that ", "The company denied that "])
def test_english_same_sentence_qualifier_cannot_be_dropped(prefix):
    from app.evidence_dossier import _sentence_span
    from app.evidence_mechanism_checks import check_mechanism
    quote = "US crude oil inventories increased by 1.019 million barrels on September 25, according to EIA."
    context = _sentence_span(prefix + quote + " A separate report follows.", quote, english_boundaries=True)
    assert context.startswith(prefix)
    assert check_mechanism(quote, context, "inventory", "2026-09-25") is None


def test_sentence_boundaries_preserve_us_abbreviation_and_decimal():
    from app.evidence_dossier import _sentence_span
    quote = "Crude oil inventories fell by 1.019 million barrels"
    raw = (
        "Earlier forecasts differed. According to the U.S. Energy Information Administration, "
        + quote
        + " on September 25. Analysts expect higher prices."
    )
    context = _sentence_span(raw, quote, english_boundaries=True)
    assert context.startswith("According to the U.S. Energy")
    assert "1.019" in context and "September 25" in context and "expect" not in context


def test_v4_frozen_dossier_reconstruction_is_preserved():
    from app.evidence_dossier import MECHANISM_POLICY
    old = joint_input(prices(), sample(), include_dossier=True, dossier_policy=MECHANISM_POLICY)
    assert MainInputSnapshot(old).evidence_dossier["schema_version"] == MECHANISM_POLICY

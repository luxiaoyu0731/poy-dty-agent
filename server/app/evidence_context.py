"""One source/time-bound dossier contract for event, report, answer and forecast consumers.

No source fetch, model invocation, forecast write, or hidden fallback to current data.
Stored report/answer payloads retain the exact business projection supplied at creation.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime

from .evidence_dossier import PRODUCT_LABELS
from .evidence_dossier_service import (
    CAPTURE_JOIN_TIMEOUT_SECONDS,
    EvidenceDossierResponse,
    project_dossier,
    read_dossier,
)
from .industrial_intelligence.identity import detrack_url
from .prediction_inputs import digest
from .prediction_main import capture_main_inputs
from .prediction_replay import timestamp
from .settings import settings
from .single_flight import SingleFlight

# Freezes one event revision at a time; the cache key already binds the
# revision, so unrelated events never queue behind a long freeze.
_EVENT_BUILDS: SingleFlight[tuple[dict, str]] = SingleFlight()
_EVENT_CACHE: dict[str, tuple[float, dict]] = {}
_EVENT_CACHE_LOCK = threading.Lock()


def bind_sources(dossier: dict, source_urls: list[str], source_texts: dict[str, str] | None = None) -> dict:
    """Exact source membership first; analogues need same subject/mechanism/direction.

    Merely sharing a product is insufficient. Similarity is selected without the
    outcome sign; mixed or unreviewed anchors cannot nominate a directional case.
    """
    result = copy.deepcopy(dossier)
    urls = {detrack_url(url) for url in source_urls if url}
    claims = {c["claim_id"]: c for c in result["claims"]}
    direct = {key for key, c in claims.items() if detrack_url(c["source_url"]) in urls}
    if source_texts is not None:
        # Same URL may have a later body revision. Only text actually supplied
        # to the answer can anchor its proof; URL overlap alone is insufficient.
        texts = {detrack_url(url): text for url, text in source_texts.items()}
        direct = {i for i in direct if claims[i]["quote"] in texts.get(detrack_url(claims[i]["source_url"]), "")}
    allowed = set(direct)
    for key, cell in result["cells"].items():
        target = key.split(":")[0]
        anchors = [
            claims[i]
            for i in direct
            if claims[i]["target"] == target
            and claims[i]["semantic_status"] == "rule_checked"
            and claims[i].get("event_date")
        ]
        mixed_ids = {i for group in cell["mixed"] for i in group}
        anchors = [a for a in anchors if a["claim_id"] not in mixed_ids]
        for name in ("current_support", "current_counter", "other_materials"):
            cell[name] = [i for i in cell[name] if i in direct]
        cell["mixed"] = [
            [i for i in group if i in direct] for group in cell["mixed"] if any(i in direct for i in group)
        ]
        for name in ("historical_support", "historical_counter", "historical_other"):
            selected = []
            for history in cell[name]:
                c = claims[history["claim_id"]]
                if c["claim_id"] in direct:
                    # An event's own later price reaction is not its historical analogue.
                    continue
                if any(
                    c.get("event_date")
                    and c["event_date"] < a["event_date"]
                    and c["mechanism"] == a["mechanism"]
                    and c["subject"].strip().casefold() == a["subject"].strip().casefold()
                    and c["expected_direction"] == a["expected_direction"]
                    and c.get("conditions", {}).get("location", "") == a.get("conditions", {}).get("location", "")
                    for a in anchors
                ):
                    selected.append(history)
                    allowed.add(c["claim_id"])
            cell[name] = selected
        for relation in ("support", "counter"):
            cell[f"current_{relation}_episodes"] = len({claims[i]["episode_key"] for i in cell[f"current_{relation}"]})
        scoped = [claims[i] for i in direct if claims[i]["target"] == target]
        for coverage in cell["coverage"]:
            found = [c for c in scoped if c["mechanism"] == coverage["mechanism"]]
            accepted_ids = set(cell["current_support"] + cell["current_counter"])
            usable = {c["episode_key"] for c in found if c["claim_id"] in accepted_ids}
            coverage.update(
                stored_claims=len(found),
                source_claims=len(found),
                usable_episodes=len(usable),
                status="available" if usable else "needs_review" if found else "no_material",
            )
        cell["gaps"] = list(cell["gaps"]) + [
            "仅关联当前对象的原始来源；历史类比要求同主体、机制、地点和机制预期，不按品种强配。"
        ]
        if not scoped:
            cell["gaps"].append("当前对象尚无通过正文、版本、引用与时间校验的该品种材料；原有来源链接仍可核对。")
        if not cell["historical_support"] and not cell["historical_counter"]:
            cell["gaps"].append("没有与该对象匹配且已形成结果的历史类似案例。")
    result["claims"] = [c for c in result["claims"] if c["claim_id"] in allowed]
    result["matched_source_claims"] = len(direct)
    return result


def freeze_evidence(
    as_of: datetime, *, source_urls: list[str] | None = None, source_texts: dict[str, str] | None = None
) -> dict:
    """Bounded capture; caller owns persistence as part of its existing record."""
    snapshot = capture_main_inputs(as_of)
    dossier = snapshot.evidence_dossier
    if dossier is None:
        raise ValueError("evidence_dossier_missing")
    if source_urls is not None:
        dossier = bind_sources(dossier, source_urls, source_texts)
    frame = {
        "schema_version": "consumer-evidence.v2",
        "as_of_time": snapshot.as_of.isoformat(),
        "input_sha256": snapshot.sha256,
        "dossier": dossier,
    }
    # Store the exact source-bound conditional analyses visible at creation.
    # Later cache changes must never rewrite a report/answer's explanation.
    from .evidence_semantic_review import project_reviews_many

    allowed_urls = {detrack_url(url) for url in source_urls or []}
    supplied_texts = {detrack_url(url): text for url, text in (source_texts or {}).items()}
    frame["semantic_reviews"] = {}
    projected = project_reviews_many(targets=list(PRODUCT_LABELS), cutoff=frame["as_of_time"])
    for target, reviews in projected.items():
        frame["semantic_reviews"][target] = [
            review.model_dump(mode="json") for review in reviews
            if (source_urls is None or detrack_url(review.source_url) in allowed_urls)
            and (source_texts is None or review.quote in supplied_texts.get(detrack_url(review.source_url), ""))
        ]
    frame["sha256"] = digest(frame)
    return frame


def try_freeze_evidence(
    as_of: datetime, *, source_urls: list[str] | None = None, source_texts: dict[str, str] | None = None
) -> dict:
    try:
        return freeze_evidence(as_of, source_urls=source_urls, source_texts=source_texts)
    except (OSError, ValueError, RuntimeError, TimeoutError, sqlite3.Error):
        return {
            "schema_version": "consumer-evidence.v1",
            "status": "unavailable",
            "as_of_time": as_of.isoformat(),
            "reason": "证据采集或校验未成功；不以空列表表示核验通过。",
        }


def evidence_text(frame: dict, *, targets: list[str] | None = None, allowed_quotes: bool = True) -> str:
    """Small, neutral context. This never authorizes novel citations or predictions."""
    dossier = frame.get("dossier")
    if not dossier:
        return frame.get("reason", "此记录没有冻结统一证据档案。")
    targets = targets or list(PRODUCT_LABELS)
    if not dossier.get("capture_complete"):
        return "本次统一证明资料读取不完整，不能按空计数判断证据不存在。"
    lines = ["统一证明体系：以下是分析材料，不是另一份价格预测；相反驱动不等于事实否认，历史反应不证明因果。"]
    for target in targets:
        cell = dossier["cells"][f"{target}:1"]
        lines.append(
            f"{PRODUCT_LABELS[target]}：当前支持 {cell['current_support_episodes']} 个事件，"
            f"相反驱动 {cell['current_counter_episodes']} 个事件；"
            f"历史类似正证 {len(cell['historical_support'])}，"
            f"历史类似反证 {len(cell['historical_counter'])}（1天反应）。"
        )
        if allowed_quotes:
            ids = set(cell["current_support"] + cell["current_counter"])
            for claim in [c for c in dossier["claims"] if c["claim_id"] in ids][:2]:
                # Complete quote, never slice away a trailing qualifier.
                lines.append(
                    f"- 来源报告：{claim['quote']}；"
                    f"机制预期{'偏强' if claim['expected_direction'] == 'up' else '偏弱'}（非必然价格结果）。"
                )
                lines.append(f"  原文：{claim['source_url']}")
        for review in frame.get("semantic_reviews", {}).get(target, []):
            lines.append(
                f"- 条件{'支持' if review['direction'] == 'up' else '相反'}依据（AI复核，不计票）："
                f"{review['rationale']}；成立条件：{'；'.join(review['conditions'])}。"
            )
            if allowed_quotes:
                lines.append(f"  原文：{review['quote']}；来源：{review['source_url']}")
        if cell["gaps"]:
            lines.append("边界：" + "；".join(cell["gaps"][:2]))
    return "\n".join(lines)


def _event_frame(event_id: str, revision_id: str | None) -> tuple[dict, str]:
    from .industrial_intelligence import storage

    with closing(storage.connect_domain_readonly()) as conn:
        conn.execute("BEGIN")
        row = (
            conn.execute(
                "SELECT * FROM intelligence_event_revisions WHERE event_id=? AND event_revision_id=?",
                (event_id, revision_id),
            ).fetchone()
            if revision_id
            else storage.latest_event_revision(conn, event_id)
        )
        if row is None or row["revision_kind"] == "invalidate" or row["status"] == "retracted":
            raise LookupError("event_not_found_or_retracted")
        revision_id = row["event_revision_id"]
        cutoff = timestamp(row["as_of_time"])
        urls = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT i.canonical_url FROM intelligence_event_evidence e JOIN intelligence_item_revisions i "
                "ON i.item_revision_id=e.item_revision_id WHERE e.event_revision_id=? LIMIT 201",
                (revision_id,),
            )
        ]
        if len(urls) > 200:
            raise ValueError("event_source_limit_exceeded")
    with _EVENT_CACHE_LOCK:
        for key in list(_EVENT_CACHE):
            if time.monotonic() - _EVENT_CACHE[key][0] > 600:
                del _EVENT_CACHE[key]
        cache_key = f"{settings.sqlite_path}:{revision_id}"
        cached = _EVENT_CACHE.get(cache_key)
    if cached is not None:
        return cached[1], revision_id

    def build() -> tuple[dict, str]:
        frame = freeze_evidence(cutoff, source_urls=urls)
        with _EVENT_CACHE_LOCK:
            if len(_EVENT_CACHE) >= 3:
                del _EVENT_CACHE[min(_EVENT_CACHE, key=lambda k: _EVENT_CACHE[k][0])]
            _EVENT_CACHE[cache_key] = (time.monotonic(), frame)
        return frame, revision_id

    result, _shared = _EVENT_BUILDS.run(cache_key, build, join_timeout=CAPTURE_JOIN_TIMEOUT_SECONDS)
    return result


def read_context_dossier(
    *,
    target,
    horizon,
    view,
    offset=0,
    limit=50,
    input_sha256=None,
    event_id=None,
    event_revision_id=None,
    report_id=None,
    context_pack_id=None,
    batch_id=None,
    refresh=False,
):
    if sum(x is not None for x in (event_id, report_id, context_pack_id, batch_id)) > 1:
        raise ValueError("choose_one_evidence_context")
    if event_revision_id and not event_id:
        raise ValueError("event_revision_requires_event_id")
    if refresh and (event_id or report_id or context_pack_id or batch_id or view != "current"):
        raise ValueError("refresh_only_for_current_product_view")
    if batch_id or not any((event_id, report_id, context_pack_id)):
        result = read_dossier(
            target=target,
            horizon=horizon,
            view="issued" if batch_id else view,
            offset=offset,
            limit=limit,
            input_sha256=input_sha256,
            batch_id=batch_id,
            refresh=refresh,
        )
        if batch_id:
            return result.model_copy(
                update={
                    "scope": "batch",
                    "context_id": batch_id,
                    "scope_note": "所选批次发行时冻结证据，不替换为最近一批或当前资料",
                }
            )
        return result
    revision = None
    if event_id:
        frame, revision = _event_frame(event_id, event_revision_id)
        scope, identity, note = "event", event_id, "按该事件修订截止时间及原始来源关联；历史类比另按事前条件筛选"
    elif report_id:
        from .information_reports import read_report

        record = read_report(report_id)
        frame = record["snapshot"].get("business_evidence")
        scope, identity, note = "report", report_id, "此报告生成时冻结的分析证据；报告中的价格预测仍以其原发行批次为准"
    else:
        from .storage import connect_readonly

        with closing(connect_readonly()) as conn:
            row = conn.execute("SELECT metadata FROM context_packs WHERE pack_id=?", (context_pack_id,)).fetchone()
        if row is None:
            raise LookupError("answer_context_not_found")
        frame = json.loads(row["metadata"]).get("business_evidence")
        scope, identity, note = (
            "answer",
            context_pack_id,
            "此回答上下文冻结证据，只关联当次检索来源；不代表每条材料被答案实际引用",
        )
    base = dict(
        view="current",
        target=target,
        horizon_days=horizon,
        scope=scope,
        context_id=identity,
        context_revision=revision,
        scope_note=note,
        offset=offset,
    )
    if not frame:
        return EvidenceDossierResponse(
            **base, status="legacy_input", gaps=["该记录没有冻结统一证据档案，不以当前资料补写历史依据。"]
        )
    if frame.get("status") == "unavailable":
        return EvidenceDossierResponse(
            **base, as_of_time=frame.get("as_of_time"), status="capture_failed", gaps=[frame["reason"]]
        )
    if frame.get("sha256") != digest({k: v for k, v in frame.items() if k != "sha256"}):
        raise ValueError("consumer_evidence_integrity_failed")
    sha = frame["sha256"]  # scoped content identity, not just shared product snapshot
    if input_sha256 and input_sha256 != sha:
        raise ValueError("evidence_view_changed_reload_first_page")
    base.update(
        as_of_time=frame["as_of_time"],
        input_sha256=sha,
        matched_source_claims=frame["dossier"].get("matched_source_claims", 0),
    )
    return project_dossier(
        frame["dossier"], base=base, target=target, horizon=horizon, offset=offset, limit=limit,
        frozen_reviews=frame.get("semantic_reviews", {}).get(target, []),
    )

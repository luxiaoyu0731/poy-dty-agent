"""Weekly calibration-memory distillation (governance §4.5, ADR-4).

Reads the settled calibration corpus (forecast_event_factors rows with outcomes,
joined to the issuing chain report), asks the LLM to distill bounded lessons
("in environment X this judgment failed"), validates evidence linkage, and
auto-publishes into agent_lessons (operator-revocable). Empty corpus -> clean
skip; lessons always cite run/artifact ids; hard cap on active lessons.

Usage:
  python server/scripts/run_agent_distillation.py --apply [--week 2026-W40]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app.live_memory_policy import reflection_enabled  # noqa: E402
from app.reflection_feedback import POLICY, calibration_sample  # noqa: E402
from app.storage import (  # noqa: E402
    connect,
    insert_agent_lesson,
    list_active_agent_lessons,
)

MAX_ACTIVE_LESSONS = 30
DISTILL_PROMPT = """你是预测系统的校准记忆蒸馏Agent。以下是本周期已结算的判断记录（含对错）。
提炼最多 3 条可复用教训：每条必须（1）指出一个具体失效模式或成功模式；（2）引用具体的 batch/target/horizon；
（3）给出对未来的操作建议（如"高库存环境下类比置信度上限0.5"式规则）；（4）置信度∈[0,1]。
禁止泛泛而谈；证据不足时少提或零条。只输出 JSON：{{"lessons": [{{"agent": "...", "lesson": "...", "category": "...", "confidence": 0.0}}]}}。

已结算记录：
{corpus}
"""  # noqa: E501 -- Keep the versioned prompt bytes unchanged.


def settled_corpus(connection: sqlite3.Connection, since: str) -> list[dict]:
    if reflection_enabled():
        now = datetime.now(UTC).isoformat()
        rows = connection.execute(
            """
            SELECT f.*, b.as_of_time, c.cell_payload, c.label_series_id, c.unit,
                   o.settled_at, o.actual_visible_at, o.actual_value, o.actual_source_id, o.actual_unit
            FROM forecast_event_factors f
            JOIN seven_product_forecast_batches b ON b.batch_id=f.batch_id
            JOIN seven_product_forecast_cells c ON c.batch_id=f.batch_id AND c.target=f.target
                 AND c.horizon_days=f.horizon_days
            JOIN seven_product_forecast_outcomes o ON o.cell_id=c.cell_id
            WHERE o.settled_at >= ?
              AND f.baseline_direction != f.event_adjusted_direction
            ORDER BY o.settled_at DESC LIMIT 400
        """,
            (since,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item["metadata"])
                item["cell_payload"] = json.loads(item["cell_payload"])
            except (ValueError, TypeError):
                continue
            sample = calibration_sample(item, known_at=now)
            if sample and sample.get("chain_run_id") and sample.get("input_sha256"):
                result.append(sample)
        return result
    rows = connection.execute(
        """
        SELECT business_date, batch_id, target, horizon_days,
               baseline_direction, event_factor_direction, fusion_rule,
               event_adjusted_direction, outcome_baseline, outcome_adjusted,
               switch_reason, metadata
        FROM forecast_event_factors
        WHERE outcome_adjusted IS NOT NULL AND business_date >= ?
        ORDER BY business_date DESC LIMIT 400
        """,
        (since,),
    ).fetchall()
    corpus = []
    for row in rows:
        item = dict(row)
        try:
            item["metadata"] = json.loads(item.get("metadata") or "{}")
        except ValueError:
            item["metadata"] = {}
        corpus.append(item)
    return corpus


def distill(corpus: list[dict], api_key: str, model: str) -> list[dict]:
    if reflection_enabled():
        prompt = (
            "从已结算的事件改写格提炼最多3条背景假设，不得提出置信上限、方向命令或硬规则。"
            "每条必须引用实际 sample_ids 并说明适用条件。证据不足输出空列表。"
            '只输出JSON：{"lessons":[{"agent":"historical_analog","lesson":"...",'
            '"category":"calibration","sample_ids":["..."],"confidence":0.5}]}\n'
            + json.dumps(corpus[:120], ensure_ascii=False)
        )
    else:
        prompt = DISTILL_PROMPT.format(
            corpus=json.dumps(
                [
                    {
                        k: item[k]
                        for k in (
                            "business_date",
                            "target",
                            "horizon_days",
                            "baseline_direction",
                            "event_factor_direction",
                            "fusion_rule",
                            "outcome_baseline",
                            "outcome_adjusted",
                        )
                    }
                    for item in corpus[:120]
                ],
                ensure_ascii=False,
            )
        )
    body = json.dumps(
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "只输出一个 JSON 对象。"},
                {"role": "user", "content": prompt},
            ],
            "response_format": {"type": "json_object"},
        }
    ).encode()
    # Same base-URL resolution as deepseek_client: deployments may reach the
    # provider through a relay (DEEPSEEK_BASE_URL); the previous hardcoded
    # api.deepseek.com failed silently in those environments.
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.load(response)
    parsed = json.loads(payload["choices"][0]["message"]["content"])
    lessons = parsed.get("lessons", [])
    return lessons if isinstance(lessons, list) else []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--week", default="")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()

    import os

    if args.db:
        from app.settings import settings

        object.__setattr__(settings, "sqlite_path", str(args.db))

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    since = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
    connection = connect()
    corpus = settled_corpus(connection, since)
    active = list_active_agent_lessons(limit=MAX_ACTIVE_LESSONS)
    print(f"settled since {since}: {len(corpus)} | active lessons: {len(active)}")
    if len(corpus) < 20:
        print("corpus below 20 settled cells — clean skip (calibration needs volume)")
        return 0
    if not api_key:
        print("DEEPSEEK_API_KEY missing", file=sys.stderr)
        return 2
    if not reflection_enabled() and len(active) >= MAX_ACTIVE_LESSONS:
        print("active lesson cap reached — revoke before distilling more")
        return 0

    lessons = distill(corpus, api_key, os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"))
    published = 0
    now = datetime.now(UTC).isoformat()
    by_id = {item["sample_id"]: item for item in corpus if item.get("sample_id")}
    expired = 0
    for index, lesson in enumerate(lessons[:3]):
        if not isinstance(lesson, dict):
            continue
        if not lesson.get("lesson") or not lesson.get("agent"):
            continue
        selected = []
        if reflection_enabled():
            refs = lesson.get("sample_ids")
            if (
                not isinstance(refs, list)
                or not refs
                or any(not isinstance(ref, str) or ref not in by_id for ref in refs)
            ):
                continue
            selected = [by_id[ref] for ref in set(refs)]
            confidence = lesson.get("confidence")
            if (
                lesson["agent"]
                not in {"political_analysis", "historical_analog", "product_synthesis", "skeptic_review"}
                or type(confidence) not in {int, float}
                or not 0 <= confidence <= 1
                or not isinstance(lesson["lesson"], str)
            ):
                continue
        lesson_id = f"lesson-{datetime.now(UTC).strftime('%Y%m%d')}-{index}"
        if selected:
            digest = hashlib.sha256(
                json.dumps(
                    [lesson, sorted(item["sample_id"] for item in selected)], sort_keys=True, ensure_ascii=False
                ).encode()
            ).hexdigest()[:20]
            lesson_id = f"lesson-v2-{digest}"
        if connection.execute("SELECT 1 FROM agent_lessons WHERE lesson_id=?", (lesson_id,)).fetchone():
            continue
        if args.apply:
            expire_id = None
            if reflection_enabled() and len(active) + published - expired >= MAX_ACTIVE_LESSONS:
                expire_id = active[-(expired + 1)]["lesson_id"]
                expired += 1
            insert_agent_lesson(
                lesson_id=lesson_id,
                expire_previous_lesson_id=expire_id,
                agent=str(lesson["agent"]),
                lesson=str(lesson["lesson"])[:500],
                category=str(lesson.get("category") or "calibration"),
                evidence_run_ids=sorted({item["chain_run_id"] for item in selected})
                if selected
                else [item["batch_id"] for item in corpus[:5]],
                valid_from=now,
                metadata={
                    "source": "weekly_distillation",
                    "policy": POLICY,
                    "sample_ids": sorted(item["sample_id"] for item in selected),
                    "samples": selected,
                    "effect_acceptance": "not_validated",
                }
                if selected
                else {},
            )
        published += 1
    print(f"lessons distilled: {published}" + ("" if args.apply else " (dry-run)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

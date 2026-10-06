"""Isolated Monday 09:05 reflection clock, never a production lesson writer.

Consumes only ON-arm settled switched cells under the registered v2 labels.
Future outcomes may exist in an offline file, but cannot enter a weekly prompt.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta
import json
from zoneinfo import ZoneInfo

from scripts.experiments.cached_replay_port import CachedReplayJsonPort, digest
from scripts.experiments.reflection_protocol import POLICY, clock, reflection_corpus

BJ = ZoneInfo("Asia/Shanghai")
MIN_CELLS = 20
SYSTEM = "只输出一个 JSON 对象。你是隔离实验的反思蒸馏 Agent。"
INSTRUCTION = (
    "仅依据下列已结算、发生方向改写的记录，提炼最多3条可检验的背景假设。"
    "按品种与期限说明适用条件、原判断的成功或失效模式，以及能推翻假设的证据。"
    "不得把教训变成置信上限、方向命令或准入门槛；不得将原油效果直接推广到其他品种。"
    "每条必须引用cell_refs中的真实样本编号；证据不足可返回零条。"
    '输出格式：{"lessons":[{"agent":"political_analysis或historical_analog",'
    '"lesson":"背景假设", "conditions":"适用条件", "falsifier":"反例条件",'
    '"cell_refs":["样本编号"]}]}。'
)


def cell_ref(cell: dict) -> str:
    # Full frozen label, outcome and reasoning context bind each training sample.
    return digest(cell)


class ReflectionTimeline:
    def __init__(self, *, first_business_date: str):
        first = date.fromisoformat(first_business_date)
        monday = first + timedelta(days=(7 - first.weekday()) % 7)
        self.next_tick = datetime.combine(monday, time(9, 5), tzinfo=BJ)
        self.cells: list[dict] = []
        self.lessons: list[dict] = []
        self.consumed: set[str] = set()
        self.timeline: list[dict] = []
        self.last_issue: datetime | None = None
        self.last_prepared: datetime | None = None

    def add_issued_result(self, *, as_of: str, settled_cells: list[dict]) -> None:
        issued = clock(as_of).astimezone(BJ)
        if (issued.hour, issued.minute, issued.second, issued.microsecond) != (
            8,
            0,
            0,
            0,
        ):
            raise ValueError("reflection_requires_production_issuance_clock")
        if self.last_issue is not None and issued <= self.last_issue:
            raise ValueError("reflection_results_must_be_chronological")
        if self.last_prepared is not None and issued < self.last_prepared:
            raise ValueError("reflection_clock_cannot_move_backwards")
        expected_day = issued.date().isoformat()
        seen = set()
        accepted_cells = []
        for cell in settled_cells:
            if (
                cell.get("business_date") != expected_day
                or cell.get("settlement_policy") != POLICY
            ):
                raise ValueError("reflection_result_date_or_policy_mismatch")
            if (
                not cell.get("settled_available_at")
                or clock(cell["settled_available_at"]) < issued
            ):
                raise ValueError("reflection_settlement_before_issuance")
            horizon = cell.get("horizon_days")
            if type(horizon) is not int or horizon not in {1, 7, 30}:
                raise ValueError("reflection_invalid_horizon")
            if clock(cell["settled_available_at"]).astimezone(
                BJ
            ).date() < issued.date() + timedelta(days=horizon):
                raise ValueError("reflection_settlement_before_due_date")
            key = (cell.get("target"), horizon)
            if key in seen:
                raise ValueError("duplicate_reflection_issued_cell")
            seen.add(key)
            # Validation here does not make a future outcome prompt-visible.
            checked = reflection_corpus([cell], known_at=cell["settled_available_at"])
            if checked["excluded"] == {"not_switched": 1}:
                continue
            if not checked["cells"]:
                raise ValueError("invalid_reflection_training_cell")
            accepted_cells.append(deepcopy(cell))
        self.cells.extend(accepted_cells)
        self.last_issue = issued

    async def before_issuance(self, *, as_of: str, port_factory) -> list[dict]:
        issued = clock(as_of).astimezone(BJ)
        if (issued.hour, issued.minute, issued.second, issued.microsecond) != (
            8,
            0,
            0,
            0,
        ):
            raise ValueError("reflection_requires_production_issuance_clock")
        if self.last_issue is not None and issued <= self.last_issue:
            raise ValueError("reflection_results_must_be_chronological")
        if self.last_prepared is not None and issued < self.last_prepared:
            raise ValueError("reflection_clock_cannot_move_backwards")
        self.last_prepared = issued
        # Monday issuance precedes Monday distillation. Tuesday can consume it.
        while self.next_tick < issued:
            await self._tick(self.next_tick, port_factory)
            self.next_tick += timedelta(days=7)
        return deepcopy(self.lessons)

    async def _tick(self, at: datetime, port_factory) -> None:
        corpus = reflection_corpus(self.cells, known_at=at.isoformat())
        pending = [
            cell for cell in corpus["cells"] if cell_ref(cell) not in self.consumed
        ]

        def inspectable(cell):
            evidence = cell.get("issued_reasoning")
            if not isinstance(evidence, dict) or evidence.get("status") != "ok":
                return False
            return (
                isinstance(evidence.get("input_sha256"), str)
                and len(evidence["input_sha256"]) == 64
                and evidence.get("evidence_basis")
                in {"verified-original-availability", "historical-backfill-simulation"}
                and evidence.get("candidates")
                and evidence.get("artifacts")
                and evidence.get("as_of_time")
                and clock(evidence["as_of_time"]).astimezone(BJ).date().isoformat()
                == cell["business_date"]
            )

        available = [cell for cell in pending if inspectable(cell)]
        entry = {
            "at": at.isoformat(),
            "policy": POLICY,
            "available_cells": len(available),
            "corpus_sha256": digest(available),
            "lessons_before": len(self.lessons),
            "excluded_without_issued_reasoning": len(pending) - len(available),
        }
        if len(available) < MIN_CELLS:
            self.timeline.append(
                {**entry, "status": "below_threshold", "lessons_added": 0}
            )
            return
        refs = {cell_ref(cell): cell for cell in available}
        reasoning = {}
        prompt_rows = []
        for ref, cell in refs.items():
            row = {
                key: value for key, value in cell.items() if key != "issued_reasoning"
            }
            if cell.get("issued_reasoning"):
                evidence_ref = digest(cell["issued_reasoning"])
                reasoning[evidence_ref] = cell["issued_reasoning"]
                row["reasoning_ref"] = evidence_ref
            prompt_rows.append({"cell_ref": ref, **row})
        # Repeated D7/D30 cells reference one sealed reasoning context. The
        # model sees original conditions/citations, not outcome counts alone.
        user = (
            INSTRUCTION
            + "\n"
            + json.dumps(
                {"records": prompt_rows, "issued_reasoning": reasoning},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        port = port_factory("reflection_distillation", at.date().isoformat())
        if not isinstance(port, CachedReplayJsonPort):
            raise ValueError("isolated_distillation_port_required")
        try:
            output = await port.complete_json(
                stage="reflection_distillation",
                business_date=at.date().isoformat(),
                system=SYSTEM,
                user=user,
            )
            proposed = output.get("lessons")
            if not isinstance(proposed, list) or len(proposed) > 3:
                raise ValueError("distillation_lesson_shape")
            accepted = []
            for item in proposed:
                if (
                    not isinstance(item, dict)
                    or item.get("agent")
                    not in {"political_analysis", "historical_analog"}
                    or any(
                        not isinstance(item.get(field), str)
                        or not item[field].strip()
                        or len(item[field]) > 500
                        for field in ("lesson", "conditions", "falsifier")
                    )
                    or not isinstance(item.get("cell_refs"), list)
                    or not item["cell_refs"]
                    or any(
                        not isinstance(ref, str) or ref not in refs
                        for ref in item["cell_refs"]
                    )
                ):
                    raise ValueError("distillation_lesson_without_frozen_evidence")
                if len(set(item["cell_refs"])) != len(item["cell_refs"]):
                    raise ValueError("duplicate_distillation_reference")
                accepted.append(
                    {
                        **deepcopy(item),
                        "lesson_id": "refl-v2-"
                        + digest(
                            {
                                "at": at.isoformat(),
                                "corpus": entry["corpus_sha256"],
                                "item": item,
                            }
                        )[:24],
                        "known_at": at.isoformat(),
                        "settlement_policy": POLICY,
                        "source": "isolated_weekly_distillation",
                        "training_cells_sha256": entry["corpus_sha256"],
                    }
                )
            if len({lesson["lesson_id"] for lesson in accepted}) != len(accepted):
                raise ValueError("duplicate_distilled_lesson")
            self.lessons = (self.lessons + accepted)[-30:]
            self.consumed.update(refs)
            self.timeline.append(
                {
                    **entry,
                    "status": "distilled",
                    "lessons_added": len(accepted),
                    "lesson_ids": [item["lesson_id"] for item in accepted],
                    "budget": port.budget_snapshot(),
                }
            )
        except Exception as exc:
            # No partial publication, no retry purchase, failed corpus retained.
            self.timeline.append(
                {
                    **entry,
                    "status": "failed",
                    "lessons_added": 0,
                    "error_type": type(exc).__name__,
                    "budget": port.budget_snapshot(),
                }
            )

    def report(self) -> dict:
        body = {
            "schema_version": "reflection-timeline.v1",
            "settlement_policy": POLICY,
            "next_tick": self.next_tick.isoformat(),
            "last_issue": self.last_issue.isoformat() if self.last_issue else None,
            "last_prepared": self.last_prepared.isoformat()
            if self.last_prepared
            else None,
            "lessons": deepcopy(self.lessons),
            "timeline": deepcopy(self.timeline),
            "consumed_cell_refs": sorted(self.consumed),
            "training_input_sha256": digest(self.cells),
            "acceptance_complete": False,
        }
        return {**body, "report_sha256": digest(body)}

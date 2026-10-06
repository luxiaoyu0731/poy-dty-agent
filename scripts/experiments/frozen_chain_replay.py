"""Frozen-input chain rehearsal. No database, production ledger or deployment.

This executes reasoning only: it is not a settlement or effect-acceptance report.
Both arms use the same sealed inputs and independent ports sharing paid receipts.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agent_chain import AgentChain
from app.unified_memory import recall_case_cards
from scripts.experiments.cached_replay_port import CachedReplayJsonPort, digest
from scripts.experiments.reflection_protocol import (
    BACKGROUND_LESSON_INSTRUCTION,
    POLICY as REFLECTION_POLICY,
)

POLICY = "frozen-chain-replay.v1"


def validate_lessons(lessons: list[dict], *, as_of: str, background: bool) -> str:
    if len(lessons) > 30:
        raise ValueError("reflection_active_cap_exceeded")
    ids = set()
    for lesson in lessons:
        identity = lesson.get("lesson_id")
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError("unique_lesson_ids_required")
        ids.add(identity)
        if not lesson.get("known_at") or clock(lesson["known_at"]) > clock(as_of):
            raise ValueError("future_or_unknown_lesson_availability")
        if background and lesson.get("settlement_policy") != REFLECTION_POLICY:
            raise ValueError("reflection_lesson_policy_mismatch")
    return digest(lessons)


async def run_rag_pair(
    bundle: dict, *, port_factory, retain_unexposed_date: bool = False
) -> dict:
    """Reasoning smoke pair; refuse to spend money on zero RAG exposure.

    The factory must construct a separate client per arm. Its shared cache and
    campaign ledger retain the common responses and the cumulative cost.
    """
    identity = validate_input(bundle)
    if type(retain_unexposed_date) is not bool:
        raise ValueError("explicit_unexposed_date_policy_required")
    qualified = any(
        recall_case_cards(receipt) for receipt in bundle.get("memory_recalls", [])
    )
    if not qualified and not retain_unexposed_date:
        raise ValueError("no_qualified_recall_before_paid_rehearsal")
    reports = {}
    for arm, enabled in (("control", False), ("rag", True)):
        if not bundle["signal_report"].get("candidates"):
            reports[arm] = {
                "chain_report": {
                    "business_date": bundle["business_date"],
                    "as_of_time": bundle["as_of_time"],
                    "status": "quiet_baseline",
                    "artifacts": [],
                    "experiment": {"input_sha256": identity, "recall_voting": enabled},
                    "memory": {"recalls": []},
                },
                "fused_rows": [
                    {
                        "target": product,
                        "horizon_days": h,
                        "baseline_direction": baseline[f"d{h}"]["direction"],
                        "event_adjusted_direction": baseline[f"d{h}"]["direction"],
                        "fusion_rule": "R4",
                    }
                    for product, baseline in bundle["baseline_by_product"].items()
                    for h in (1, 7, 30)
                ],
            }
            continue
        chain = FrozenReplayChain(
            bundle=bundle,
            port=port_factory(arm, bundle["business_date"]),
            recall_voting=enabled,
        )
        surviving = await chain.run_political_analysis()
        await chain.run_historical_analog(surviving)
        await chain.run_product_synthesis()
        await chain.run_skeptic_review()
        reports[arm] = {
            "chain_report": chain.report(),
            "fused_rows": chain.fused_rows(),
        }
    return {
        "schema_version": "frozen-chain-rehearsal.v1",
        "input_sha256": identity,
        "business_date": bundle["business_date"],
        "acceptance_complete": False,
        "scope_note": "仅推理预跑；尚无配对样本完整性、结算标签及效果验收",
        "qualified_recall_available": qualified,
        "arms": reports,
    }


def clock(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("frozen_clock_requires_timezone")
    return parsed


def validate_input(bundle: dict) -> str:
    if bundle.get("schema_version") != "frozen-chain-input.v1":
        raise ValueError("frozen_input_schema_required")
    at = clock(bundle["as_of_time"]).astimezone(ZoneInfo("Asia/Shanghai"))
    if at.date().isoformat() != bundle["business_date"] or (
        at.hour,
        at.minute,
        at.second,
        at.microsecond,
    ) != (8, 0, 0, 0):
        raise ValueError("production_issuance_clock_required")
    if bundle.get("evidence_basis") not in {
        "historical-backfill-simulation",
        "verified-original-availability",
    }:
        raise ValueError("explicit_historical_evidence_basis_required")
    if bundle["evidence_basis"] == "verified-original-availability":
        from scripts.experiments.strict_replay_admission import (
            validate_source_admission,
        )

        validate_source_admission(bundle)
    candidates = bundle["signal_report"].get("candidates", [])
    ids = [item["event_id"] for item in candidates]
    if len(ids) != len(set(ids)) or len(ids) > 16:
        raise ValueError("unique_frozen_candidates_required")
    for event in candidates:
        visibility = bundle["candidate_visibility"][event["event_id"]]
        if clock(visibility) > at or clock(event["event_time"]) > at:
            raise ValueError("future_candidate_input")
        # Absence is not a cue to query the live database.
        if (
            event["event_id"] not in bundle["case_cards_by_event"]
            or event["event_id"] not in bundle["empirical_by_event"]
        ):
            raise ValueError("frozen_case_and_empirical_context_required")
        for case in bundle["case_cards_by_event"][event["event_id"]]:
            if not case.get("case_id") or clock(case["known_at"]) > at:
                raise ValueError("future_or_unidentified_case")
    for receipt in bundle.get("memory_recalls", []):
        if receipt.get("event_id") not in ids or clock(receipt["as_of_time"]) != at:
            raise ValueError("recall_input_binding_mismatch")
    return digest(bundle)


class FrozenReplayChain(AgentChain):
    """Private experiment adapter; no voting switch exists on production API."""

    def __init__(
        self,
        *,
        bundle: dict,
        port: CachedReplayJsonPort,
        recall_voting: bool,
        lessons: list[dict] | None = None,
        background_lessons: bool = False,
    ):
        self.input_digest = validate_input(bundle)
        if not isinstance(port, CachedReplayJsonPort):
            raise ValueError("isolated_cached_replay_port_required")
        if type(recall_voting) is not bool or type(background_lessons) is not bool:
            raise ValueError("explicit_experiment_arms_required")
        self.lesson_digest = validate_lessons(
            lessons or [], as_of=bundle["as_of_time"], background=background_lessons
        )
        self.bundle = deepcopy(bundle)
        self.recall_voting = recall_voting
        self.background_lessons = background_lessons
        super().__init__(
            port=port,
            signal_report=deepcopy(bundle["signal_report"]),
            business_date=bundle["business_date"],
            as_of_time=bundle["as_of_time"],
            lessons=deepcopy(lessons or []),
            baseline_by_product=deepcopy(bundle["baseline_by_product"]),
        )
        # Stable experiment identity prevents resume from changing training
        # references and accidentally repurchasing identical distillation.
        self.run_id = (
            "replay-"
            + digest(
                {
                    "input": self.input_digest,
                    "lessons": self.lesson_digest,
                    "recall_voting": recall_voting,
                    "background": background_lessons,
                }
            )[:24]
        )
        # Frozen recalls only; never open an index through production settings.
        self.memory_recall_enabled = False
        self.memory_recalls = {
            r["event_id"]: deepcopy(r) for r in bundle.get("memory_recalls", [])
        }
        self.recall_cards = {}
        for receipt in self.memory_recalls.values():
            for card in recall_case_cards(receipt):
                self.recall_cards[card["case_id"]] = card

    def _empirical_prior_for_event(self, event):
        return deepcopy(self.bundle["empirical_by_event"][event["event_id"]])

    def _lessons_prompt_text(self):
        if digest(self.lessons) != self.lesson_digest:
            raise ValueError("frozen_lessons_changed")
        text = super()._lessons_prompt_text()
        return (
            BACKGROUND_LESSON_INSTRUCTION + "\n" + text
            if self.background_lessons and self.lessons
            else text
        )

    async def run_historical_analog(self, surviving_event_ids, cases_by_event=None):
        if cases_by_event is not None:
            raise ValueError("cannot_replace_frozen_case_context")
        cases = deepcopy(self.bundle["case_cards_by_event"])
        if self.recall_voting:
            for event_id in surviving_event_ids:
                for card in recall_case_cards(self.memory_recalls.get(event_id, {})):
                    cases[event_id].append(card)
        self._retrieved_case_ids.update(
            c["case_id"] for cards in cases.values() for c in cards
        )
        await super().run_historical_analog(surviving_event_ids, cases)

    def scoped_analog(self, event_id: str, product: str) -> dict:
        """Recalled posteriors can support only their recorded product/term.

        Mixed original/recall aggregate counts cannot be decomposed safely.
        When recall is cited, use only the validated recalled group's count;
        never infer additional votes from the model's aggregate support count.
        """
        output = deepcopy(self.analog_by_event[event_id]["output"])
        cited = {item.get("case_id") for item in output.get("analog_top3", [])}
        cards = [self.recall_cards[c] for c in cited if c in self.recall_cards]
        if not cards:
            return output
        scoped = {}
        for horizon in ("d1", "d7", "d30"):
            requested = (
                (output.get("prior_by_horizon") or {}).get(horizon)
                or output.get("prior")
                or {}
            )
            eligible = [
                c
                for c in cards
                if c["product"] == product
                and c["horizon"] == horizon
                and c["price_direction"] == requested.get("direction")
            ]
            strongest = max(eligible, key=lambda c: c["support_count"], default=None)
            scoped[horizon] = {
                "direction": strongest["price_direction"] if strongest else "neutral",
                "support_count": strongest["support_count"] if strongest else 0,
                "median_magnitude_pct": strongest["median_magnitude_pct"]
                if strongest
                else None,
            }
        output["prior_by_horizon"] = scoped
        output["prior"] = {"direction": "neutral", "support_count": 0}
        output["recall_scope_policy"] = "cited-product-horizon-only.v1"
        return output

    def _synthesis_user_prompt(self, product):
        # Reuse production wording; scope only the frozen analog JSON field.
        originals = {
            event_id: item["output"] for event_id, item in self.analog_by_event.items()
        }
        scoped = {
            event_id: self.scoped_analog(event_id, product) for event_id in originals
        }
        import json

        prompt = super()._synthesis_user_prompt(product)
        old = "历史类比与先验：" + json.dumps(originals, ensure_ascii=False)
        new = "历史类比与先验：" + json.dumps(scoped, ensure_ascii=False)
        return prompt.replace(old, new, 1)

    def persist(self):
        raise RuntimeError("experiment_cannot_persist_to_production")

    def fused_rows(self) -> list[dict]:
        """Run the unmodified production rules on each product's scoped priors."""
        from types import SimpleNamespace
        from app.event_fusion import fuse_batch

        raw = self.report()
        rows = []
        for product, baseline in self.baseline.items():
            report = deepcopy(raw)
            for artifact in report["artifacts"]:
                if artifact["stage"] == "historical_analog":
                    artifact["output"] = self.scoped_analog(
                        artifact["input_refs"]["event_id"], product
                    )
            batch = SimpleNamespace(
                batch_id=self.input_digest,
                cells=[
                    SimpleNamespace(
                        target=product,
                        horizon_days=h,
                        direction=baseline[f"d{h}"]["direction"],
                    )
                    for h in (1, 7, 30)
                ],
            )
            rows.extend(fuse_batch(batch=batch, chain_report=report)["rows"])
        return rows

    def report(self, **kwargs):
        report = super().report(**kwargs)
        report["experiment"] = {
            "policy": POLICY,
            "input_sha256": self.input_digest,
            "lessons_sha256": self.lesson_digest,
            "evidence_basis": self.bundle["evidence_basis"],
            "recall_voting": self.recall_voting,
            "background_lessons": self.background_lessons,
            "scope_policy": "cited-product-horizon-only.v1",
        }
        report["memory"]["recall_enabled"] = bool(self.memory_recalls)
        report["memory"]["voting_enabled"] = self.recall_voting
        report["scoped_priors_by_product"] = {
            product: {
                event_id: self.scoped_analog(event_id, product)
                for event_id in self.analog_by_event
            }
            for product in self.baseline
        }
        return report

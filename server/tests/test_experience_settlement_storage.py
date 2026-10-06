from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest

from app import storage
from app.experience_settlement_storage import (
    ExperienceSettlementPersistenceError,
    save_experience_settlement_plan,
)
from app.settings import settings


def _fingerprint(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _card(
    horizon: int,
    *,
    previous: dict | None = None,
    seed: str | None = None,
    subtarget: str = "poy",
    diagnostic: bool = False,
) -> dict:
    seed = seed or f"{subtarget}-{horizon}"
    evaluation_as_of = "2026-07-01T20:00:00+08:00"
    return {
        "schema_version": "phase-a.experience-card.v1",
        "experience_card_id": f"ec-{subtarget}",
        "prediction_id": f"pred-{subtarget}-{horizon}",
        "revision_id": f"ecr-{seed}",
        "previous_revision_id": previous["revision_id"] if previous else None,
        "as_of_time": "2026-06-01T08:20:00+08:00",
        "evaluation_as_of": evaluation_as_of,
        "horizon_days": horizon,
        "maturity_stage": {1: "d1_preliminary", 7: "d7_intermediate", 30: "d30_mature"}[horizon],
        "benchmark_series_id": "benchmark.target",
        "scoreability": "unscorable" if diagnostic else "scorable",
        "exclusion_reasons": ["target_effective_dates_missing"] if diagnostic else [],
        "visibility_mode": "strict_as_of",
        "mechanism_support_status": "inconclusive" if diagnostic else "supported",
        "reusable_experience": [],
        "eligible_for_retrieval_at": None if diagnostic else evaluation_as_of,
        "prediction_batch_id": "batch-1",
        "checkpoint_prediction_id": f"pred-{subtarget}-{horizon}",
        "node_id": "poy_dty_upstream_cost_pressure",
        "subtarget": subtarget,
        "target_series_id": f"{subtarget}.target",
        "prediction_revision_id": "prediction-r1",
        "data_snapshot_id": "snapshot-1",
        "calendar_id": "effective-days",
        "calendar_version": "2026.v1",
        "diagnostic_only": diagnostic,
        "calculation_fingerprint": _fingerprint(seed),
    }


def _actions(subtarget: str = "poy", *, diagnostic: bool = False) -> list[dict]:
    d1 = _card(1, subtarget=subtarget, diagnostic=diagnostic)
    d7 = _card(7, previous=d1, subtarget=subtarget, diagnostic=diagnostic)
    d30 = _card(30, previous=d7, subtarget=subtarget, diagnostic=diagnostic)
    return [
        {
            "checkpoint_horizon": card["horizon_days"],
            "expected_previous_revision_id": card["previous_revision_id"],
            "card": card,
        }
        for card in (d1, d7, d30)
    ]


def _item(
    subtarget: str = "poy",
    *,
    status: str = "planned",
    actions: list[dict] | None = None,
) -> dict:
    reasons = [f"target_series_{status}"] if status in {"blocked", "unavailable"} else []
    return {
        "settlement_key": f"batch-1/poy_dty_upstream_cost_pressure/{subtarget}/{subtarget}.target",
        "prediction_batch_id": "batch-1",
        "node_id": "poy_dty_upstream_cost_pressure",
        "subtarget": subtarget,
        "target_series_id": f"{subtarget}.target",
        "status": status,
        "reason_codes": reasons,
        "upstream_statuses": {
            "prediction_status": "available",
            "target_observation_status": "available",
            "benchmark_observation_status": "available",
            "target_series_status": "eligible" if status not in {"blocked", "unavailable"} else status,
            "benchmark_series_status": "eligible",
        },
        "actions": actions if actions is not None else (_actions(subtarget) if status == "planned" else []),
        "next_checkpoint": None if status in {"planned", "unchanged"} else 1,
        "next_expected_date": None,
    }


def _plan(items: list[dict]) -> dict:
    statuses = ("planned", "pending", "unchanged", "blocked", "unavailable")
    counts = {status: sum(item["status"] == status for item in items) for status in statuses}
    counts["actions"] = sum(len(item["actions"]) for item in items)
    return {
        "schema_version": "experience-settlement-plan.v1",
        "evaluation_as_of": "2026-07-01T20:00:00+08:00",
        "items": items,
        "counts": counts,
    }


@pytest.fixture
def experience_db(tmp_path: Path):
    path = tmp_path / "settlement.db"
    original = settings.sqlite_path
    object.__setattr__(settings, "sqlite_path", str(path))
    storage._MIGRATED_PATHS.clear()
    try:
        yield path
    finally:
        storage._MIGRATED_PATHS.clear()
        object.__setattr__(settings, "sqlite_path", original)


def _row_count() -> int:
    with closing(storage.connect()) as connection, connection:
        return int(connection.execute("SELECT COUNT(*) FROM experience_card_revisions").fetchone()[0])


def test_three_action_candidate_commits_atomically_and_exact_replay_is_unchanged(experience_db: Path) -> None:
    plan = _plan([_item()])
    inserted = save_experience_settlement_plan(plan)
    assert inserted["items"][0]["status"] == "inserted"
    assert [item["card"]["horizon_days"] for item in inserted["items"][0]["action_results"]] == [1, 7, 30]
    assert _row_count() == 3

    replay = save_experience_settlement_plan(deepcopy(plan))
    assert replay["items"][0]["status"] == "unchanged"
    assert _row_count() == 3


@pytest.mark.parametrize("failure_index", [2, 3])
def test_failure_on_later_action_rolls_back_entire_candidate(
    experience_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_index: int,
) -> None:
    original = storage._save_experience_card_revision_locked
    calls = 0

    def fail_later(connection, prepared, persisted_at):
        nonlocal calls
        calls += 1
        if calls == failure_index:
            raise sqlite3.OperationalError("injected_settlement_write_failure")
        return original(connection, prepared, persisted_at)

    monkeypatch.setattr(storage, "_save_experience_card_revision_locked", fail_later)
    with pytest.raises(sqlite3.OperationalError, match="injected_settlement_write_failure"):
        save_experience_settlement_plan(_plan([_item()]))
    assert _row_count() == 0


def test_mixed_existing_prefix_requires_replan_without_new_rows(experience_db: Path) -> None:
    actions = _actions()
    storage.save_experience_card_revision(actions[0]["card"])
    with pytest.raises(storage.ExperienceRevisionConflict, match="stale_experience_revision"):
        save_experience_settlement_plan(_plan([_item(actions=actions)]))
    assert _row_count() == 1


def test_concurrent_head_change_rejects_stale_batch_without_partial_rows(experience_db: Path) -> None:
    competing = _card(1, seed="competing")
    storage.save_experience_card_revision(competing)
    with pytest.raises(storage.ExperienceRevisionConflict, match="stale_experience_revision"):
        save_experience_settlement_plan(_plan([_item()]))
    assert storage.get_experience_card_head("ec-poy")["revision_id"] == competing["revision_id"]
    assert _row_count() == 1


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda plan: plan["items"][0]["actions"][0].update(checkpoint_horizon=7), "action_horizon_invalid"),
        (
            lambda plan: plan["items"][0]["actions"][1].update(expected_previous_revision_id="wrong"),
            "action_predecessor_invalid",
        ),
        (
            lambda plan: plan["items"][0]["actions"][0]["card"].update(evaluation_as_of="2026-07-02T20:00:00+08:00"),
            "action_evaluation_asof_invalid",
        ),
        (
            lambda plan: plan["items"][0]["actions"][1]["card"].update(target_series_id="other"),
            "action_identity_invalid",
        ),
        (lambda plan: plan["counts"].update(actions=2), "counts_invalid"),
    ],
)
def test_plan_tampering_fails_before_any_write(
    experience_db: Path,
    mutate,
    code: str,
) -> None:
    plan = _plan([_item()])
    mutate(plan)
    with pytest.raises(ExperienceSettlementPersistenceError, match=code):
        save_experience_settlement_plan(plan)
    assert _row_count() == 0


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("prediction_status", "pending"),
        ("target_observation_status", "unavailable"),
        ("benchmark_observation_status", "pending"),
        ("target_series_status", "blocked"),
        ("benchmark_series_status", "unavailable"),
    ],
)
def test_planned_item_requires_all_upstream_inputs_to_be_available_or_eligible(
    experience_db: Path,
    field: str,
    invalid_value: str,
) -> None:
    plan = _plan([_item()])
    plan["items"][0]["upstream_statuses"][field] = invalid_value
    with pytest.raises(
        ExperienceSettlementPersistenceError,
        match="experience_settlement_planned_upstream_status_invalid:0",
    ):
        save_experience_settlement_plan(plan)
    assert _row_count() == 0


def test_cross_candidate_revision_id_duplicate_rejects_complete_plan_before_writes(
    experience_db: Path,
) -> None:
    poy = _item("poy")
    dty = _item("dty")
    duplicate = poy["actions"][0]["card"]["revision_id"]
    dty["actions"][0]["card"]["revision_id"] = duplicate
    dty["actions"][1]["card"]["previous_revision_id"] = duplicate
    dty["actions"][1]["expected_previous_revision_id"] = duplicate
    with pytest.raises(
        ExperienceSettlementPersistenceError,
        match="experience_settlement_revision_id_duplicate",
    ):
        save_experience_settlement_plan(_plan([poy, dty]))
    assert _row_count() == 0


def test_calendar_invalid_evaluation_time_is_rejected_before_writes(experience_db: Path) -> None:
    plan = _plan([_item()])
    plan["evaluation_as_of"] = "2026-02-30T20:00:00+08:00"
    with pytest.raises(
        ExperienceSettlementPersistenceError,
        match="experience_settlement_evaluation_asof_invalid",
    ):
        save_experience_settlement_plan(plan)
    assert _row_count() == 0


@pytest.mark.parametrize("status", ["blocked", "pending", "unchanged", "unavailable"])
def test_nonplanned_items_never_write(experience_db: Path, status: str) -> None:
    result = save_experience_settlement_plan(_plan([_item(status=status)]))
    assert result["items"][0]["status"] == status
    assert result["items"][0]["action_results"] == []
    assert _row_count() == 0


def test_diagnostic_actions_are_persisted_but_remain_nonretrievable(experience_db: Path) -> None:
    result = save_experience_settlement_plan(_plan([_item(actions=_actions(diagnostic=True))]))
    cards = [item["card"] for item in result["items"][0]["action_results"]]
    assert all(card["diagnostic_only"] is True for card in cards)
    assert all(card["eligible_for_retrieval_at"] is None for card in cards)
    assert _row_count() == 3


def test_poy_and_dty_candidates_commit_independently(experience_db: Path) -> None:
    result = save_experience_settlement_plan(_plan([_item("poy"), _item("dty")]))
    assert [item["status"] for item in result["items"]] == ["inserted", "inserted"]
    assert storage.get_experience_card_head("ec-poy")["horizon_days"] == 30
    assert storage.get_experience_card_head("ec-dty")["horizon_days"] == 30


def test_failure_in_second_candidate_does_not_rollback_first(
    experience_db: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = storage.save_experience_card_revision_batch
    calls = 0

    def fail_second(cards):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise sqlite3.OperationalError("second_candidate_failure")
        return original(cards)

    monkeypatch.setattr(storage, "save_experience_card_revision_batch", fail_second)
    with pytest.raises(sqlite3.OperationalError, match="second_candidate_failure"):
        save_experience_settlement_plan(_plan([_item("poy"), _item("dty")]))
    assert storage.get_experience_card_head("ec-poy")["horizon_days"] == 30
    assert storage.get_experience_card_head("ec-dty") is None

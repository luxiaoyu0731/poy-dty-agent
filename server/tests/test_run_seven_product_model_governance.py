from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.seven_product_evaluation import evaluate_seven_product_forecast
from app.seven_product_forecast import LoadedLabelSeries, PricePoint

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = SERVER_ROOT / "scripts" / "run_seven_product_model_governance.py"
SPEC = importlib.util.spec_from_file_location("run_seven_product_model_governance", SCRIPT_PATH)
assert SPEC and SPEC.loader
governance_runner = importlib.util.module_from_spec(SPEC)
sys.modules["run_seven_product_model_governance"] = governance_runner
SPEC.loader.exec_module(governance_runner)


def _series(target: str, _as_of: datetime) -> LoadedLabelSeries:
    start = datetime(2025, 1, 1, tzinfo=UTC)
    return LoadedLabelSeries(
        points=tuple(
            PricePoint(
                observation_id=f"{target}-{index}",
                observed_at=(start + timedelta(days=index)).isoformat(),
                visible_at=(start + timedelta(days=index, hours=1)).isoformat(),
                value=100 * (1.01**index),
                unit="USD/mt",
                source_id=f"source-{target}",
                source_url=f"https://example.com/{target}/{index}",
            )
            for index in range(200)
        ),
        source_matches_label=True,
    )


def _write_evidence(path: Path) -> None:
    evaluation = evaluate_seven_product_forecast(
        as_of_time="2026-01-01T00:00:00Z",
        series_loader=_series,
        bootstrap_replicates=100,
    )
    body = {
        "schema_version": "seven-product-evaluation-evidence.v1",
        "generated_at": "2026-01-01T00:00:00+00:00",
        "as_of_time": evaluation.as_of_time,
        "status": "passed",
        "database": {
            "path": "/private/tmp/test.db",
            "sha256": "a" * 64,
            "schema_version": 36,
            "integrity_check": "ok",
            "unchanged_during_run": True,
        },
        "forecast": {},
        "evaluation": evaluation.model_dump(mode="json"),
        "summary": {"contract_complete": True, "passed_count": 21},
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    payload = {**body, "evidence_body_sha256": hashlib.sha256(encoded.encode()).hexdigest()}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_promotion_cli_writes_content_addressed_proposal_without_mutating_registry(tmp_path: Path) -> None:
    registry = SERVER_ROOT / "model_registry" / "seven_product_registry.json"
    before = governance_runner.sha256_file(registry)
    evidence = tmp_path / "evaluation.json"
    output = tmp_path / "output"
    _write_evidence(evidence)

    exit_code = governance_runner.main(
        [
            "promote",
            "--registry",
            str(registry),
            "--evaluation-evidence",
            str(evidence),
            "--model-version",
            "robust-drift-reference.v1",
            "--cell",
            "crude:1",
            "--actor",
            "test-owner",
            "--reason",
            "frozen cell passed",
            "--approve",
            "--approved-at",
            "2026-01-02T00:00:00Z",
            "--output-dir",
            str(output),
        ]
    )

    assert exit_code == 0
    assert governance_runner.sha256_file(registry) == before
    proposal_paths = list(output.glob("seven-product-governance-proposal-*.json"))
    registry_paths = list(output.glob("seven-product-registry-*.json"))
    assert len(proposal_paths) == 1
    assert len(registry_paths) == 1
    proposal = json.loads(proposal_paths[0].read_text(encoding="utf-8"))
    proposed_registry = json.loads(registry_paths[0].read_text(encoding="utf-8"))
    assert proposal["applied_to_live_registry"] is False
    assert proposal["source_registry"]["unchanged_during_proposal"] is True
    assert proposed_registry["champions"]["crude"]["1"] == "robust-drift-reference.v1"
    assert proposed_registry["champions"]["crude"]["7"] is None
    assert governance_runner.canonical_sha256(proposed_registry) == proposal["proposed_registry_sha256"]


def test_promotion_cli_rejects_tampered_evidence_and_missing_approval(tmp_path: Path) -> None:
    registry = SERVER_ROOT / "model_registry" / "seven_product_registry.json"
    evidence = tmp_path / "evaluation.json"
    _write_evidence(evidence)
    payload = json.loads(evidence.read_text(encoding="utf-8"))
    payload["evaluation"]["cells"][0]["candidate_mae"] = 123.456
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    assert governance_runner.main(
        [
            "promote",
            "--registry",
            str(registry),
            "--evaluation-evidence",
            str(evidence),
            "--model-version",
            "robust-drift-reference.v1",
            "--cell",
            "crude:1",
            "--actor",
            "test-owner",
            "--reason",
            "tampered",
            "--approve",
            "--output-dir",
            str(tmp_path / "tampered"),
        ]
    ) == 1

    _write_evidence(evidence)
    assert governance_runner.main(
        [
            "promote",
            "--registry",
            str(registry),
            "--evaluation-evidence",
            str(evidence),
            "--model-version",
            "robust-drift-reference.v1",
            "--cell",
            "crude:1",
            "--actor",
            "test-owner",
            "--reason",
            "not approved",
            "--output-dir",
            str(tmp_path / "unapproved"),
        ]
    ) == 1


def test_rollback_cli_uses_recorded_target_and_leaves_promoted_input_unchanged(tmp_path: Path) -> None:
    source_registry = SERVER_ROOT / "model_registry" / "seven_product_registry.json"
    evidence = tmp_path / "evaluation.json"
    promote_output = tmp_path / "promote"
    _write_evidence(evidence)
    assert governance_runner.main(
        [
            "promote",
            "--registry",
            str(source_registry),
            "--evaluation-evidence",
            str(evidence),
            "--model-version",
            "robust-drift-reference.v1",
            "--cell",
            "crude:1",
            "--actor",
            "test-owner",
            "--reason",
            "formal pass",
            "--approve",
            "--approved-at",
            "2026-01-02T00:00:00Z",
            "--output-dir",
            str(promote_output),
        ]
    ) == 0
    promoted_registry = next(promote_output.glob("seven-product-registry-*.json"))
    before = governance_runner.sha256_file(promoted_registry)
    rollback_output = tmp_path / "rollback"

    assert governance_runner.main(
        [
            "rollback",
            "--registry",
            str(promoted_registry),
            "--cell",
            "crude:1",
            "--actor",
            "test-owner",
            "--reason",
            "runtime regression",
            "--approve",
            "--approved-at",
            "2026-01-03T00:00:00Z",
            "--output-dir",
            str(rollback_output),
        ]
    ) == 0
    assert governance_runner.sha256_file(promoted_registry) == before
    rolled_back = json.loads(next(rollback_output.glob("seven-product-registry-*.json")).read_text())
    assert rolled_back["champions"]["crude"]["1"] is None
    assert rolled_back["rollback_targets"]["crude:1"] == "robust-drift-reference.v1"

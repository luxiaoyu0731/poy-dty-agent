from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from app.models import SevenProductEvaluationBatch  # noqa: E402
from app.seven_product_contract import CURRENT_FORMAL_HORIZONS, CURRENT_FORMAL_TARGETS  # noqa: E402
from app.seven_product_model_governance import (  # noqa: E402
    DEFAULT_MODEL_REGISTRY_PATH,
    approve_model_promotion,
    load_model_registry,
    rollback_model_cells,
)
from app.sqlite_permissions import secure_private_directory  # noqa: E402

DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "seven-product-model-governance"
PROPOSAL_SCHEMA_VERSION = "seven-product-model-governance-proposal.v1"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create an immutable seven-product registry promotion or rollback proposal."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    promote = commands.add_parser("promote", help="Propose an explicitly approved formal champion transition.")
    promote.add_argument("--registry", type=Path, default=DEFAULT_MODEL_REGISTRY_PATH)
    promote.add_argument("--evaluation-evidence", type=Path, required=True)
    promote.add_argument("--model-version", required=True)
    _add_approval_arguments(promote)

    rollback = commands.add_parser("rollback", help="Propose a rollback to recorded per-cell targets.")
    rollback.add_argument("--registry", type=Path, default=DEFAULT_MODEL_REGISTRY_PATH)
    _add_approval_arguments(rollback)
    return parser.parse_args(argv)


def _add_approval_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cell", action="append", required=True, help="Repeat target:horizon, e.g. crude:1.")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--approve", action="store_true", help="Required explicit approval acknowledgement.")
    parser.add_argument("--approved-at", default="", help="Optional timezone-aware ISO timestamp.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_evaluation_evidence(path: Path) -> tuple[SevenProductEvaluationBatch, dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("evaluation_evidence_unavailable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "seven-product-evaluation-evidence.v1":
        raise ValueError("evaluation_evidence_schema_invalid")
    expected_digest = str(payload.get("evidence_body_sha256") or "")
    body = dict(payload)
    body.pop("evidence_body_sha256", None)
    if len(expected_digest) != 64 or canonical_sha256(body) != expected_digest:
        raise ValueError("evaluation_evidence_hash_invalid")
    database = payload.get("database")
    if (
        not isinstance(database, dict)
        or database.get("unchanged_during_run") is not True
        or database.get("integrity_check") != "ok"
    ):
        raise ValueError("evaluation_evidence_database_invalid")
    evaluation_payload = payload.get("evaluation")
    if not isinstance(evaluation_payload, dict):
        raise ValueError("evaluation_payload_missing")
    evaluation = SevenProductEvaluationBatch.model_validate(evaluation_payload)
    return evaluation, {
        "path": str(path),
        "file_sha256": sha256_file(path),
        "evidence_body_sha256": expected_digest,
        "evaluation_id": evaluation.evaluation_id,
        "evaluation_report_sha256": evaluation.report_sha256,
    }


def parse_cells(values: list[str]) -> list[tuple[str, int]]:
    cells: list[tuple[str, int]] = []
    for value in values:
        target, separator, horizon_text = value.partition(":")
        if (
            not separator
            or target not in CURRENT_FORMAL_TARGETS
            or not horizon_text.isdigit()
            or int(horizon_text) not in CURRENT_FORMAL_HORIZONS
        ):
            raise ValueError(f"governance_cell_invalid:{value}")
        cells.append((target, int(horizon_text)))
    if len(set(cells)) != len(cells):
        raise ValueError("governance_cells_duplicate")
    return cells


def build_proposal(args: argparse.Namespace) -> dict[str, Any]:
    registry_path = args.registry.expanduser().resolve(strict=True)
    registry_sha256 = sha256_file(registry_path)
    registry = load_model_registry(registry_path)
    cells = parse_cells(args.cell)
    approved_at = args.approved_at or None
    evidence_identity: dict[str, str] | None = None
    if args.command == "promote":
        evidence_path = args.evaluation_evidence.expanduser().resolve(strict=True)
        evaluation, evidence_identity = load_evaluation_evidence(evidence_path)
        proposed = approve_model_promotion(
            registry=registry,
            evaluation=evaluation,
            candidate_model_version=args.model_version,
            approved_cells=cells,
            actor=args.actor,
            reason=args.reason,
            approved=bool(args.approve),
            approved_at=approved_at,
        )
    else:
        proposed = rollback_model_cells(
            registry=registry,
            cells=cells,
            actor=args.actor,
            reason=args.reason,
            approved=bool(args.approve),
            rolled_back_at=approved_at,
        )
    if sha256_file(registry_path) != registry_sha256:
        raise RuntimeError("live_registry_changed_during_proposal")
    return {
        "schema_version": PROPOSAL_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "operation": args.command,
        "source_registry": {
            "path": str(registry_path),
            "file_sha256": registry_sha256,
            "registry_revision": registry["registry_revision"],
            "unchanged_during_proposal": True,
        },
        "source_evaluation_evidence": evidence_identity,
        "approved_cells": [
            {"target": target, "horizon_days": horizon} for target, horizon in sorted(cells)
        ],
        "proposed_registry_revision": proposed["registry_revision"],
        "proposed_registry_sha256": canonical_sha256(proposed),
        "proposed_registry": proposed,
        "applied_to_live_registry": False,
        "next_gate": "review, commit and deploy the exact proposed registry in a new immutable release",
    }


def write_proposal(proposal: dict[str, Any], *, output_dir: Path) -> tuple[Path, Path, str]:
    output_dir = output_dir.expanduser().resolve()
    secure_private_directory(output_dir)
    digest = canonical_sha256(proposal)
    envelope = {**proposal, "proposal_body_sha256": digest}
    proposal_path = output_dir / f"seven-product-governance-proposal-{digest[:16]}.json"
    registry_path = output_dir / f"seven-product-registry-{proposal['proposed_registry_revision']}.json"
    _atomic_write(proposal_path, envelope)
    _atomic_write(registry_path, proposal["proposed_registry"])
    return proposal_path, registry_path, digest


def _atomic_write(path: Path, value: Any) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = Path(stream.name)
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        proposal = build_proposal(args)
        proposal_path, registry_path, digest = write_proposal(proposal, output_dir=args.output_dir)
    except Exception as exc:  # noqa: BLE001 - emit one bounded operator-facing failure.
        print(json.dumps({"status": "blocked", "error": str(exc)[:500]}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "proposed_not_applied",
                "proposal_path": str(proposal_path),
                "registry_path": str(registry_path),
                "proposal_body_sha256": digest,
                "proposed_registry_revision": proposal["proposed_registry_revision"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

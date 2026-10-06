#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "server/data/agent.db"
ASSET_NAME = "697样本严格时序失败基线"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tracked_commit(path: Path) -> str | None:
    relative = path.resolve().relative_to(ROOT)
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", str(relative)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() or None


def load_asset(db: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(db)) as connection, connection:
        row = connection.execute(
            "SELECT payload FROM historical_validation_assets WHERE name=? ORDER BY registered_at DESC LIMIT 1",
            (ASSET_NAME,),
        ).fetchone()
    if not row:
        raise RuntimeError(f"asset not found: {ASSET_NAME}")
    return json.loads(row[0])


def build_audit(db: Path) -> dict[str, Any]:
    asset = load_asset(db)
    lineage = asset["lineage"]
    artifact = ROOT / lineage["artifact_path"]
    generator = ROOT / lineage["generator"]
    rebuild_inputs = [artifact, generator, ROOT / ".codex-run/full-chain-75/run_full_chain_75_experiment.py"]
    files = [
        {
            "path": str(path.relative_to(ROOT)),
            "exists": path.is_file(),
            "sha256": sha256(path) if path.is_file() else None,
            "git_commit": tracked_commit(path) if path.is_file() else None,
        }
        for path in rebuild_inputs
    ]
    artifact_matches = files[0]["sha256"] == lineage.get("artifact_sha256")
    generator_matches = files[1]["sha256"] in set(lineage.get("input_hashes") or [])
    blockers: list[str] = []
    if not artifact_matches:
        blockers.append("artifact hash does not match registered lineage")
    if not generator_matches:
        blockers.append("generator hash is not bound by registered input_hashes")
    if not files[1]["git_commit"]:
        blockers.append("generator has no recorded Git commit")
    blockers.extend(
        [
            "historical source rows do not retain authoritative first_seen_at/visible_at/revision timestamps",
            "feature construction explicitly treats observed_at as business visibility for historical imports",
            "CFTC visibility is approximated as report date +3 days rather than proven from capture logs",
            (
                "the original database snapshot hash is not recorded; the current mutable database cannot prove the"
                " original input"
            ),
        ]
    )
    return {
        "schema_version": "strict_697_governance_audit.v1",
        "asset_id": asset["asset_id"],
        "asset_name": asset["name"],
        "checks": {
            "registered_artifact_hash_matches": artifact_matches,
            "registered_generator_hash_matches": generator_matches,
            "generator_commit_recorded": bool(files[1]["git_commit"]),
            "authoritative_input_snapshot_recorded": False,
            "record_level_visibility_evidence_complete": False,
        },
        "visibility_audit": {
            "status": "caveated",
            "basis": "No retrospective timestamp substitution is accepted.",
            "caveats": blockers,
        },
        "reproduction_manifest": {
            "status": "incomplete",
            "command": lineage.get("rebuild_command"),
            "files": files,
            "missing": ["generator_commit", "immutable_database_snapshot_hash", "record_level_visibility_ledger"],
        },
        "strict_candidate": False,
        "decision": "Retain as diagnostic historical asset; do not patch leakage to passed.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Evidence-only governance audit for the strict 697-sample asset.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_audit(args.db.resolve())
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()

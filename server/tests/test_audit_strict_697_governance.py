from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from server.scripts import audit_strict_697_governance as audit


def test_missing_visibility_evidence_cannot_be_promoted(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    generator = root / ".codex-run/full-chain-75/probe_strict_ml_direction.py"
    base = root / ".codex-run/full-chain-75/run_full_chain_75_experiment.py"
    artifact = root / ".codex-run/full-chain-75/strict-ml-direction-probe.json"
    for path, content in ((generator, "generator"), (base, "base"), (artifact, "{}")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    db = root / "agent.db"
    payload = {
        "asset_id": "hva-test",
        "name": audit.ASSET_NAME,
        "lineage": {
            "artifact_path": str(artifact.relative_to(root)),
            "artifact_sha256": audit.sha256(artifact),
            "generator": str(generator.relative_to(root)),
            "rebuild_command": "python generator.py",
            "input_hashes": [audit.sha256(generator)],
        },
    }
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("CREATE TABLE historical_validation_assets(name TEXT, registered_at TEXT, payload TEXT)")
        connection.execute(
            "INSERT INTO historical_validation_assets VALUES(?,?,?)",
            (audit.ASSET_NAME, "2026-01-01", json.dumps(payload)),
        )
    monkeypatch.setattr(audit, "ROOT", root)
    monkeypatch.setattr(audit, "tracked_commit", lambda _path: None)

    report = audit.build_audit(db)

    assert report["checks"]["registered_artifact_hash_matches"] is True
    assert report["checks"]["registered_generator_hash_matches"] is True
    assert report["visibility_audit"]["status"] == "caveated"
    assert report["reproduction_manifest"]["status"] == "incomplete"
    assert report["strict_candidate"] is False

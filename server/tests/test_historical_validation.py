from __future__ import annotations

import hashlib
from pathlib import Path

from server.app import historical_validation as hv


def _payload(path: str) -> dict:
    return {
        "name": "strict holdout",
        "validation_layer": "strict",
        "trust_level": "strict_reproducible",
        "train_window": {"start": "2025-01-01", "end": "2025-12-31"},
        "test_window": {"start": "2026-01-01", "end": "2026-06-30"},
        "scoring": {"scored": 20, "total": 20, "coverage": 1.0},
        "leakage": {"status": "passed", "details": None},
        "limitations": ["仅验证历史留出集，不代表当前方向"],
        "flags": {"posthoc": False, "small_sample": False},
        "lineage": {
            "artifact_path": path,
            "artifact_sha256": None,
            "artifact_generated_at": "2026-07-01T00:00:00Z",
            "generator": "backtest.py",
            "rebuild_command": "python backtest.py",
            "input_hashes": ["a" * 64],
            "config_hash": "b" * 64,
        },
    }


def test_strict_asset_is_eligible_and_hash_bound(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / ".codex-run"
    root.mkdir()
    artifact = root / "result.json"
    artifact.write_text("{}")
    monkeypatch.setattr(hv, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(hv, "ASSET_ROOT", root.resolve())
    monkeypatch.setattr(hv, "connect", lambda: __import__("sqlite3").connect(":memory:"))
    payload = _payload(".codex-run/result.json")
    payload["visibility_audit"] = {"status": "passed"}
    payload["lineage"]["manifest"] = {"status": "verified"}
    assert hv._eligible(payload) is True
    assert hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_diagnostic_or_leaky_asset_never_eligible() -> None:
    payload = _payload(".codex-run/result.json")
    payload["validation_layer"] = "diagnostic"
    assert hv._eligible(payload) is False


def test_visibility_or_manifest_caveat_blocks_eligibility() -> None:
    payload = _payload(".codex-run/result.json")
    payload["visibility_audit"] = {"status": "caveated"}
    payload["lineage"]["manifest"] = {"status": "verified"}
    assert hv._eligible(payload) is False
    payload["visibility_audit"]["status"] = "passed"
    payload["lineage"]["manifest"]["status"] = "incomplete"
    assert hv._eligible(payload) is False
    payload["validation_layer"] = "strict"
    payload["leakage"]["status"] = "failed"
    assert hv._eligible(payload) is False

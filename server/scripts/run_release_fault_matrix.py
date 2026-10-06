from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVER_ROOT = REPO_ROOT / "server"
DEFAULT_MANIFEST = SERVER_ROOT / "contracts" / "seven_product_release_fault_matrix.v1.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / ".codex-run" / "release-fault-matrix"
EXPECTED_SCENARIOS = (
    "process_restart",
    "machine_service_restart_recovery",
    "network_offline",
    "http_403",
    "http_429",
    "timeout",
    "upstream_empty_data",
    "partial_month_failure",
    "state_corruption",
    "duplicate_schedule",
    "database_backup",
    "backup_restore",
    "disk_insufficient",
    "llm_unavailable",
    "numeric_model_unavailable",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the frozen seven-product release fault matrix.")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args(argv)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    scenarios = payload.get("scenarios")
    if payload.get("schema_version") != "seven-product-release-fault-matrix.v1" or not isinstance(scenarios, list):
        raise ValueError("release_fault_matrix_manifest_invalid")
    ids = tuple(str(item.get("id") or "") for item in scenarios if isinstance(item, dict))
    nodeids = [str(item.get("test_nodeid") or "") for item in scenarios if isinstance(item, dict)]
    if ids != EXPECTED_SCENARIOS or len(set(nodeids)) != len(EXPECTED_SCENARIOS) or any(not node for node in nodeids):
        raise ValueError("release_fault_matrix_scenarios_invalid")
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest_path = args.manifest.expanduser().resolve(strict=True)
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(manifest_path)
    nodeids = [str(item["test_nodeid"]) for item in manifest["scenarios"]]
    gate_root = Path(tempfile.mkdtemp(prefix="seven-product-fault-matrix.", dir="/private/tmp"))
    gate_root.chmod(0o700)
    tmp_dir = gate_root / "tmp"
    tmp_dir.mkdir(mode=0o700)
    junit_path = output_dir / "fault-matrix-junit.xml"
    python = SERVER_ROOT / ".venv" / "bin" / "python"
    command = [str(python), "-m", "pytest", *nodeids, "-q", f"--junitxml={junit_path}"]
    environment = os.environ.copy()
    environment.update(
        {
            "DG01_TEST_DB_ROOT": str(gate_root),
            "TMPDIR": str(tmp_dir),
            "SQLITE_PATH": str(gate_root / "runtime.db"),
            "PYTHONPATH": str(SERVER_ROOT),
        }
    )
    started_at = datetime.now(UTC).isoformat()
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    report_body = {
        "schema_version": "seven-product-release-fault-evidence.v1",
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "status": "passed" if completed.returncode == 0 else "failed",
        "scenario_count": len(nodeids),
        "passed_count": len(nodeids) if completed.returncode == 0 else 0,
        "manifest_path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "gate_root": str(gate_root),
        "junit_path": str(junit_path),
        "junit_sha256": sha256_file(junit_path) if junit_path.is_file() else None,
        "pytest_returncode": completed.returncode,
        "test_nodeids": nodeids,
        "stdout_tail": completed.stdout[-8000:],
        "stderr_tail": completed.stderr[-4000:],
    }
    canonical = json.dumps(report_body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    report = {**report_body, "report_body_sha256": hashlib.sha256(canonical.encode()).hexdigest()}
    report_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    latest = output_dir / "fault-matrix-latest.json"
    latest.write_text(report_text, encoding="utf-8")
    addressed = output_dir / f"fault-matrix-{report['report_body_sha256'][:16]}.json"
    addressed.write_text(report_text, encoding="utf-8")
    print(report_text, end="")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())

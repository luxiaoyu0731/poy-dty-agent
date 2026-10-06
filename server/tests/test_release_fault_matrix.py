from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parent
SCRIPT_PATH = SERVER_ROOT / "scripts" / "run_release_fault_matrix.py"
SPEC = importlib.util.spec_from_file_location("run_release_fault_matrix", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
fault_matrix = importlib.util.module_from_spec(SPEC)
sys.modules["run_release_fault_matrix"] = fault_matrix
SPEC.loader.exec_module(fault_matrix)


def test_fault_matrix_contract_has_exactly_15_unique_resolvable_scenarios() -> None:
    manifest = fault_matrix.load_manifest(fault_matrix.DEFAULT_MANIFEST)
    scenarios = manifest["scenarios"]

    assert len(scenarios) == 15
    assert tuple(item["id"] for item in scenarios) == fault_matrix.EXPECTED_SCENARIOS
    for item in scenarios:
        path_text, function_name = str(item["test_nodeid"]).split("::", 1)
        test_path = REPO_ROOT / path_text
        assert test_path.is_file(), item["id"]
        source = test_path.read_text(encoding="utf-8")
        assert re.search(rf"^def {re.escape(function_name)}\(", source, flags=re.MULTILINE), item["id"]

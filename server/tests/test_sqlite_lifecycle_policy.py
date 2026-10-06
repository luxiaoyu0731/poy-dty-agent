from __future__ import annotations

import ast
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
SCANNED_ROOTS = (SERVER_ROOT / "app", SERVER_ROOT / "scripts", SERVER_ROOT / "tests")


def test_production_code_does_not_treat_sqlite_context_as_closing() -> None:
    violations: list[str] = []
    for root in SCANNED_ROOTS:
        for path in root.rglob("*.py"):
            relative_path = path.relative_to(SERVER_ROOT)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.With, ast.AsyncWith)):
                    continue
                for item in node.items:
                    expression = item.context_expr
                    if not isinstance(expression, ast.Call):
                        continue
                    function = expression.func
                    function_name = (
                        function.attr
                        if isinstance(function, ast.Attribute)
                        else function.id
                        if isinstance(function, ast.Name)
                        else ""
                    )
                    if function_name in {
                        "connect",
                        "connect_read_only",
                        "connect_readonly",
                        "_readonly_connection",
                    }:
                        violations.append(f"{relative_path}:{node.lineno}")

    assert violations == [], (
        "sqlite3.Connection.__exit__ commits or rolls back but does not close; "
        f"wrap connections with contextlib.closing: {violations}"
    )

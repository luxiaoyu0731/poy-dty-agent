from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

data_governance = importlib.import_module("app.data_governance")
audit_database = data_governance.audit_database
persist_audit = data_governance.persist_audit
render_markdown = data_governance.render_markdown
list_sources = importlib.import_module("app.source_registry").list_sources

DEFAULT_DB = Path("data/agent.db")
DEFAULT_OUTPUT_DIR = Path("../.codex-run/data-source-audits")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit every local database table for source attribution and explicit authoritative reconciliation."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--no-persist", action="store_true", help="Write reports without persisting the audit run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(args.db)) as connection, connection:
            connection.row_factory = sqlite3.Row
            registry = [source.model_dump(mode="json") for source in list_sources()]
            report = audit_database(connection, registry=registry)
            json_path = args.output_dir / "authoritative-data-audit-latest.json"
            markdown_path = args.output_dir / "authoritative-data-audit-latest.md"
            json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            markdown_path.write_text(render_markdown(report), encoding="utf-8")
            if not args.no_persist:
                persist_audit(
                    connection,
                    report,
                    json_path=str(json_path),
                    markdown_path=str(markdown_path),
                )
    except Exception as exc:  # noqa: BLE001 - concise automation failure.
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "status": report["status"],
                "summary": report["summary"],
                "json": str(json_path),
                "markdown": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())

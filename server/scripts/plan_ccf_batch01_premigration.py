#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import validate_ccf_batch01_capture_package as validator  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".codex-run/ccf-authorized-capture")
DEFAULT_PACKAGE_DIR = DEFAULT_OUTPUT_DIR / "ccf-capture-templates"
DEFAULT_DB = Path("server/data/agent.db")
BACKUP_DIR = DEFAULT_OUTPUT_DIR / "db-backups"


def main() -> int:
    parser = argparse.ArgumentParser(description="Plan CCF batch 01 pre-migration commands and blockers.")
    parser.add_argument("--package-dir", type=Path, default=DEFAULT_PACKAGE_DIR)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(package_dir=args.package_dir.resolve(), db_path=args.db.resolve(), output_dir=output_dir)
    json_path = output_dir / "ccf-batch01-premigration-plan-latest.json"
    md_path = output_dir / "35-ccf-batch01-premigration-plan.md"
    write_json(json_path, report)
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(*, package_dir: Path, db_path: Path, output_dir: Path) -> dict[str, Any]:
    validation = validator.build_report(package_dir)
    ready = bool(validation["summary"]["promotion_review_ready"])
    price_template = package_dir / "ccf_batch_01_price_template.csv"
    industry_template = package_dir / "ccf_batch_01_industry_template.csv"
    dry_run_commands = [
        command(
            "price_dry_run",
            [
                "server/.venv/bin/python",
                "server/scripts/import_ccf_authorized_csvs.py",
                "--dry-run",
                "--input-dir",
                str(package_dir),
                "--pattern",
                price_template.name,
                "--db",
                str(db_path),
                "--summary-output",
                str(output_dir / "ccf-batch01-price-import-dry-run-summary.json"),
            ],
        ),
        command(
            "industry_dry_run",
            [
                "server/.venv/bin/python",
                "server/scripts/import_ccf_industry_observations.py",
                "--dry-run",
                "--input",
                str(industry_template),
                "--db",
                str(db_path),
                "--summary-output",
                str(output_dir / "ccf-batch01-industry-import-dry-run-summary.json"),
            ],
        ),
    ]
    apply_commands = [
        command(
            "price_apply",
            [
                "server/.venv/bin/python",
                "server/scripts/import_ccf_authorized_csvs.py",
                "--apply",
                "--backup-db",
                "--backup-dir",
                str(BACKUP_DIR),
                "--input-dir",
                str(package_dir),
                "--pattern",
                price_template.name,
                "--db",
                str(db_path),
                "--summary-output",
                str(output_dir / "ccf-batch01-price-import-apply-summary.json"),
            ],
        ),
        command(
            "industry_apply",
            [
                "server/.venv/bin/python",
                "server/scripts/import_ccf_industry_observations.py",
                "--apply",
                "--backup-db",
                "--backup-dir",
                str(BACKUP_DIR),
                "--input",
                str(industry_template),
                "--db",
                str(db_path),
                "--summary-output",
                str(output_dir / "ccf-batch01-industry-import-apply-summary.json"),
            ],
        ),
    ]
    return {
        "schema_version": "ccf_batch01_premigration_plan.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "mode": "premigration_plan_only",
            "db_writes": 0,
            "validation_required_before_apply": True,
            "apply_allowed_now": ready,
            "backup_required_before_apply": True,
        },
        "summary": {
            "promotion_review_ready": ready,
            "blockers": validation["summary"]["blockers"],
            "warnings": validation["summary"]["warnings"],
            "price_rows": validation["summary"]["price_rows"],
            "industry_rows": validation["summary"]["industry_rows"],
            "evidence_rows": validation["summary"]["evidence_rows"],
            "db_writes": 0,
        },
        "validation_summary": validation["summary"],
        "top_blockers": validation["issues"][:30],
        "dry_run_commands": dry_run_commands,
        "apply_commands": apply_commands if ready else [],
        "blocked_apply_commands": [] if ready else apply_commands,
        "rollback": {
            "method": (
                "Stop the app/server, then copy the selected pre-import backup over"
                " /path/to/project/server/data/agent.db."
            ),
            "backup_dir": str(BACKUP_DIR),
            "note": "Apply commands are intentionally withheld until validation passes.",
        },
        "artifacts": {
            "json": str(output_dir / "ccf-batch01-premigration-plan-latest.json"),
            "report": str(output_dir / "35-ccf-batch01-premigration-plan.md"),
            "validation_report": str(output_dir / "34-ccf-batch01-capture-validation.md"),
        },
    }


def command(name: str, parts: list[str]) -> dict[str, str]:
    return {"name": name, "command": " ".join(shell_quote(part) for part in parts)}


def shell_quote(value: str) -> str:
    if not value or any(ch.isspace() for ch in value):
        return "'" + value.replace("'", "'\"'\"'") + "'"
    return value


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    return (
        "\n".join(
            [
                "# 35. CCF Batch 01 Pre-Migration Plan",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"promotion_review_ready: `{summary['promotion_review_ready']}`",
                f"apply_allowed_now: `{report['scope']['apply_allowed_now']}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| blockers | {summary['blockers']} |",
                f"| warnings | {summary['warnings']} |",
                f"| price_rows | {summary['price_rows']} |",
                f"| industry_rows | {summary['industry_rows']} |",
                f"| evidence_rows | {summary['evidence_rows']} |",
                "",
                "## Dry-Run Commands",
                "",
                command_block(report["dry_run_commands"]),
                "",
                "## Apply Commands",
                "",
                (
                    command_block(report["apply_commands"])
                    if report["apply_commands"]
                    else "Apply commands are blocked until validation passes."
                ),
                "",
                "## Top Blockers",
                "",
                markdown_table(report["top_blockers"], ["severity", "code", "message"]),
                "",
                "## DB Writes",
                "",
                "No database writes were performed by this planner.",
                "",
                "## Rollback",
                "",
                report["rollback"]["method"],
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- Validation report: `{report['artifacts']['validation_report']}`",
            ]
        )
        + "\n"
    )


def command_block(commands: list[dict[str, str]]) -> str:
    if not commands:
        return "_No commands_"
    return "\n\n".join(f"**{item['name']}**\n\n```bash\n{item['command']}\n```" for item in commands)


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No rows_"
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(header, "")) for header in headers) + " |")
    return "\n".join(lines)


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())

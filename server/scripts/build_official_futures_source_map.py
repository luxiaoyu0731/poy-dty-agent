#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")

SOURCES = [
    {
        "exchange": "INE",
        "products": ["SC"],
        "official_entry_url": "https://www.ine.cn/reports/tradedata/datadownload/",
        "evidence_page_url": "https://www.ine.cn/reports/tradedata/datadownload/",
        "source_kind": "official_exchange_data_download",
        "required_fields": (
            "open,high,low,close,settle,volume,open_interest,source_publish_time,visible_at,source_url,license_scope"
        ),
        "current_status": "candidate_identified_browser_visible_not_materialized",
        "blocker": "official_file_not_downloaded_or_hashed",
        "db_write_needed": "yes_after_official_file_capture_and_backup",
        "notes": (
            "INE public data download page advertises current-year and historical data downloads. A 2026-08-05 local"
            " scripted request received a WEB application firewall page, while a normal browser verification displayed"
            " the SC daily contract table. No export file or precise publication timestamp was captured, so source-file"
            " hash, timestamp and publish policy still need normal-access capture."
        ),
    },
    {
        "exchange": "CZCE",
        "products": ["PTA", "PX", "METHANOL"],
        "official_entry_url": "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20260831/FutureDataDaily.txt",
        "evidence_page_url": "https://www.czce.com.cn/cn/DFSStaticFiles/Future/2026/20260831/FutureDataDaily.txt",
        "source_kind": "official_exchange_daily_text",
        "required_fields": (
            "open,high,low,close,settle,volume,open_interest,source_publish_time,visible_at,source_url,license_scope"
        ),
        "current_status": "adapter_materialized_readonly_capture_verified",
        "blocker": "point_in_time_history_accumulation_required",
        "db_write_needed": "no_for_current_adapter; yes_after_backup_for_historical_backfill",
        "notes": (
            "The bounded czce_pta_px adapter reads fixed official daily text files without redirects, preserves the"
            " source SHA-256 and first-capture time, and applies the frozen close-open-interest main rule. The"
            " 2026-08-31 file and a ten-day read-only window were verified; this does not manufacture historical"
            " point-in-time visibility."
        ),
    },
    {
        "exchange": "DCE",
        "products": ["MEG"],
        "official_entry_url": "https://www.dce.com.cn/dceg/channel/list/468.html",
        "evidence_page_url": "https://www.dce.com.cn/dceg/channel/list/468.html",
        "source_kind": "official_exchange_historical_data",
        "required_fields": (
            "open,high,low,close,settle,volume,open_interest,source_publish_time,visible_at,source_url,license_scope"
        ),
        "current_status": "soft_removed_historical_audit_only",
        "blocker": "source_soft_removed",
        "db_write_needed": "no",
        "notes": (
            "DCE/MEG was soft-removed from current acquisition on 2026-09-01. Keep this mapping only to explain"
            " historical rows; do not download, materialize, import or use it for current-formal qualification."
        ),
    },
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build official futures source map for replacing AkShare prototype rows."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args(argv)

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(generated_at=datetime.now(UTC))
    json_path = output_dir / "official-futures-source-map-latest.json"
    md_path = output_dir / "74-official-futures-source-map.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0


def build_report(*, generated_at: datetime) -> dict[str, Any]:
    rows = []
    for source in SOURCES:
        for product in source["products"]:
            rows.append(
                {
                    "product": product,
                    "exchange": source["exchange"],
                    "official_entry_url": source["official_entry_url"],
                    "evidence_page_url": source["evidence_page_url"],
                    "source_kind": source["source_kind"],
                    "required_fields": source["required_fields"],
                    "current_status": source["current_status"],
                    "blocker": source["blocker"],
                    "db_write_needed": source["db_write_needed"],
                    "notes": source["notes"],
                }
            )
    blockers = sorted({row["blocker"] for row in rows})
    return {
        "schema_version": "official_futures_source_map.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "source_mapping_only",
            "db_writes": 0,
            "no_synthetic_values": True,
            "akshare_policy": "AkShare remains internal prototype / public proxy / cross-check only.",
            "acceptance_policy": (
                "This map does not make futures rows action-grade until official files, hashes, timestamps and import"
                " evidence pass readiness gates."
            ),
        },
        "summary": {
            "products": len(rows),
            "official_sources": len(SOURCES),
            "download_materialized": sum(
                1 for row in rows if row["current_status"] == "adapter_materialized_readonly_capture_verified"
            ),
            "blockers": len(blockers),
            "db_writes": 0,
        },
        "rows": rows,
        "blockers": [{"code": code, "message": blocker_message(code)} for code in blockers],
        "next_actions": [
            "Keep accumulating daily CZCE captures; acquire INE and DCE files only through normal public channels.",
            (
                "Record source_file, source_file_sha256, source_page_url, captured_at, source_publish_policy and"
                " personal-use policy identity."
            ),
            (
                "For still-blocked products, fill official_futures_p1_template.csv from official files only; do not"
                " copy AkShare values into formal-label fields."
            ),
            (
                "Run the readiness gate, back up the main DB, import, rebuild as-of features, then rerun full-chain 75"
                " acceptance."
            ),
        ],
    }


def blocker_message(code: str) -> str:
    if code == "official_file_not_downloaded_or_hashed":
        return (
            "Official futures source is identified, but no downloadable/exported file has been captured, hashed and"
            " mapped to rows yet."
        )
    if code == "point_in_time_history_accumulation_required":
        return (
            "The current official adapter is verified, but historical backfill cannot be relabelled as if every row"
            " had been visible to the system on its original trading date."
        )
    return code


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    return (
        "\n".join(
            [
                "# 74. Official Futures Source Map",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                (
                    "CZCE PTA/PX/methanol current daily files are materialized by the official adapter."
                    " SC and MEG still need"
                    " normal-access official capture, and every product still needs adequate point-in-time history."
                    " AkShare remains cross-check only."
                ),
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| products | {summary['products']} |",
                f"| official_sources | {summary['official_sources']} |",
                f"| download_materialized | {summary['download_materialized']} |",
                f"| blockers | {summary['blockers']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Source Map",
                "",
                markdown_table(
                    report["rows"],
                    ["product", "exchange", "source_kind", "current_status", "blocker", "official_entry_url"],
                ),
                "",
                "## Next Actions",
                "",
                *[f"- {item}" for item in report["next_actions"]],
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- This report does not claim full-chain 75% achieved.",
                (
                    "- Current CZCE rows have captured files and evidence; formal eligibility still requires adequate"
                    " point-in-time history and OOS performance."
                ),
                "- Any future DB apply must first back up `/path/to/project/server/data/agent.db`.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
            ]
        )
        + "\n"
    )


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No rows_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())

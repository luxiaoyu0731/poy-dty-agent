#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")


@dataclass(frozen=True)
class SafariState:
    title: str
    url: str
    readable: bool = True
    error: str = ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit current Safari CCF session/page state without capturing data or writing DB."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(read_safari_state(), generated_at=datetime.now(UTC))
    write_artifacts(output_dir, report)
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["capture_can_continue"] else 2


def read_safari_state() -> SafariState:
    script = """
tell application "Safari"
  if (count of windows) > 0 then
    set tabTitle to name of current tab of front window
    set tabUrl to URL of current tab of front window
    return tabTitle & "\n" & tabUrl
  end if
end tell
""".strip()
    result = run_osascript(script)
    parts = str(result.get("stdout") or "").splitlines()
    return SafariState(
        title=parts[0].strip() if parts else "",
        url=parts[1].strip() if len(parts) > 1 else "",
        readable=not result.get("error"),
        error=str(result.get("error") or ""),
    )


def run_osascript(script: str) -> dict[str, str]:
    try:
        result = subprocess.run(["osascript", "-e", script], text=True, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"stdout": "", "error": "osascript_timeout"}
    if result.returncode != 0:
        return {
            "stdout": result.stdout.strip(),
            "error": result.stderr.strip() or f"osascript_exit_{result.returncode}",
        }
    return {"stdout": result.stdout.strip(), "error": ""}


def build_report(state: SafariState, *, generated_at: datetime) -> dict[str, Any]:
    blockers = detect_blockers(state)
    capture_can_continue = not blockers and is_ccf_data_center_url(state.url)
    return {
        "schema_version": "ccf_safari_session_state.v1",
        "generated_at": generated_at.isoformat(),
        "scope": {
            "mode": "safari_current_tab_state_audit_only",
            "db_writes": 0,
            "provider_data_rows": 0,
            "no_auth_bypass": True,
            "no_captcha_bypass": True,
            "no_paywall_bypass": True,
        },
        "source": {
            "browser": "Safari",
            "current_title": state.title,
            "current_url": state.url,
            "safari_state_readable": state.readable,
            "read_error": state.error,
        },
        "summary": {
            "ccf_domain_detected": is_ccf_url(state.url),
            "ccf_data_center_detected": is_ccf_data_center_url(state.url),
            "capture_can_continue": capture_can_continue,
            "blockers": len(blockers),
            "db_writes": 0,
        },
        "blockers": blockers,
        "next_actions": next_actions(blockers),
    }


def detect_blockers(state: SafariState) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    url = state.url.lower()
    title = state.title.lower()
    if not state.readable:
        blockers.append(
            blocker("safari_state_unreadable", "Safari current tab state could not be read by AppleScript.")
        )
    if not state.url:
        blockers.append(blocker("safari_no_current_url", "Safari has no readable current tab URL."))
    elif not is_ccf_url(state.url):
        blockers.append(blocker("ccf_page_not_open", "Current Safari tab is not on ccf.com.cn."))
    if "member.php?action=login" in url or "login" in url or "会员登录" in state.title or "请登录" in state.title:
        blockers.append(
            blocker(
                "ccf_login_required",
                "Current CCF tab is a login page; user must complete normal login and required verification.",
            )
        )
    if any(
        marker in url or marker in title
        for marker in ["captcha", "verify", "verification", "扫码", "验证码", "二次验证"]
    ):
        blockers.append(
            blocker(
                "ccf_verification_required",
                "CCF appears to require verification; user action is required before capture.",
            )
        )
    if (
        is_ccf_url(state.url)
        and not is_ccf_data_center_url(state.url)
        and not any(item["code"] == "ccf_login_required" for item in blockers)
    ):
        blockers.append(blocker("ccf_data_page_not_open", "Current CCF tab is not a recognized data-center page."))
    return blockers


def is_ccf_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host == "ccf.com.cn" or host.endswith(".ccf.com.cn")


def is_ccf_data_center_url(url: str) -> bool:
    parsed = urlparse(url)
    return is_ccf_url(url) and "/datacenter/" in parsed.path.lower()


def blocker(code: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": "blocker", "message": message}


def next_actions(blockers: list[dict[str, str]]) -> list[dict[str, str]]:
    codes = {item["code"] for item in blockers}
    actions: list[dict[str, str]] = []
    if "safari_state_unreadable" in codes or "safari_no_current_url" in codes:
        actions.append(
            {
                "rank": "1",
                "action": (
                    "Bring Safari to the CCF data-center tab and rerun this preflight after the current URL is"
                    " readable."
                ),
                "why": "The audit could not verify the current URL, so authorized capture cannot safely proceed.",
            }
        )
    if "ccf_login_required" in codes:
        actions.append(
            {
                "rank": str(len(actions) + 1),
                "action": (
                    "In Safari, finish the normal CCF login flow and return to"
                    " https://www.ccf.com.cn/datacenter/price.php."
                ),
                "why": "The current tab is a CCF login redirect, so authorized capture cannot proceed yet.",
            }
        )
    if "ccf_verification_required" in codes:
        actions.append(
            {
                "rank": str(len(actions) + 1),
                "action": "Complete the visible CCF verification challenge manually.",
                "why": "The automation must not bypass CAPTCHA, QR, second-factor, or other verification.",
            }
        )
    if "ccf_page_not_open" in codes or "ccf_data_page_not_open" in codes:
        actions.append(
            {
                "rank": str(len(actions) + 1),
                "action": "Open the authorized CCF data-center price page in Safari before running capture.",
                "why": "The capture scripts only validate authorized visible data pages.",
            }
        )
    if not actions:
        actions.append(
            {
                "rank": "1",
                "action": "Run the CCF live capture validation against the current Safari data-center page.",
                "why": "No session/page blocker was detected by this preflight.",
            }
        )
    return actions[:3]


def write_artifacts(output_dir: Path, report: dict[str, Any]) -> None:
    json_path = output_dir / "ccf-safari-session-state-latest.json"
    md_path = output_dir / "87-ccf-safari-session-state.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    source = report["source"]
    return (
        "\n".join(
            [
                "# 87. CCF Safari Session State",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"capture_can_continue: `{summary['capture_can_continue']}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| ccf_domain_detected | {summary['ccf_domain_detected']} |",
                f"| ccf_data_center_detected | {summary['ccf_data_center_detected']} |",
                f"| blockers | {summary['blockers']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Current Safari Tab",
                "",
                f"- title: `{source['current_title']}`",
                f"- url: `{source['current_url']}`",
                f"- readable: `{source['safari_state_readable']}`",
                "",
                "## Blockers",
                "",
                markdown_table(report["blockers"], ["code", "severity", "message"]),
                "",
                "## Next Actions",
                "",
                markdown_table(report["next_actions"], ["rank", "action", "why"]),
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- No provider data rows were captured by this audit.",
                "- No login, paywall, CAPTCHA, QR, or second-factor bypass was attempted.",
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

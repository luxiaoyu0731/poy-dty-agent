#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import capture_ccf_price_page_from_safari as live_capture  # noqa: E402
import materialize_ccf_live_capture_package as materialize  # noqa: E402

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DEFAULT_RUN_DIR = DEFAULT_OUTPUT_DIR / "ccf-live-batch-capture"
CCF_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_LOOKBACK_DAYS = 7

PRICE_PRODUCTS = {
    "内盘MEG现货": {"prod_id": "5", "expected": "内盘MEG现货"},
    # CCF displays this market label in source-native order.  The canonical
    # importer name remains "PX CFR中国" in the materialization layer.
    "PX CFR中国": {"prod_id": "127", "expected": "CFR中国PX"},
    "内盘PTA": {"prod_id": "3", "expected": "内盘PTA"},
    "日本石脑油": {"prod_id": "36", "expected": "CFR日本石脑油"},
    "DTY 150D/144F轻网": {"prod_id": "119", "expected": "DTY 150D/144F轻网"},
    "DTY 150D/48F低弹": {"prod_id": "9", "expected": "DTY 150D/48F低弹"},
    "DTY75/36": {"prod_id": "339", "expected": "DTY75/36"},
    "DTY 75D/72F轻网": {"prod_id": "118", "expected": "DTY 75D/72F轻网"},
    "POY 150D/48F": {"prod_id": "10", "expected": "POY 150D/48F"},
    "POY 150D/144F": {"prod_id": "116", "expected": "POY 150D/144F"},
    "POY 75D/36F": {"prod_id": "114", "expected": "POY 75D/36F"},
    "POY 75D/72F": {"prod_id": "115", "expected": "POY 75D/72F"},
}

DEFAULT_BATCH_PRODUCTS = ["内盘MEG现货", "PX CFR中国", "内盘PTA", "日本石脑油"]


class SafariExecutor(Protocol):
    def submit_product(self, product_name: str, prod_id: str, start_date: str, end_date: str) -> dict[str, str]: ...

    def current_page(self) -> dict[str, str]: ...


class OsaScriptSafariExecutor:
    def submit_product(self, product_name: str, prod_id: str, start_date: str, end_date: str) -> dict[str, str]:
        js = f"""
        (function(){{
          const result = {{status: "unknown", url: location.href, message: ""}};
          const text = document.body ? document.body.innerText : "";
          if (location.href.includes("/member/member.php") || text.includes("会员登录") || text.includes("请登录")) {{
            result.status = "blocked";
            result.code = "ccf_login_required";
            result.message = "CCF login page is active.";
            return JSON.stringify(result);
          }}
          const form = document.querySelector("#adminForm");
          if (!form) {{
            result.status = "blocked";
            result.code = "ccf_price_form_missing";
            result.message = "CCF price form #adminForm is missing.";
            return JSON.stringify(result);
          }}
          const verified = window.verify === true || text.includes("验证通过");
          if (!verified) {{
            result.status = "blocked";
            result.code = "ccf_verification_required";
            result.message = "CCF slider/verification is not complete.";
            return JSON.stringify(result);
          }}
          const start = document.querySelector("#startdate");
          const end = document.querySelector("#enddate");
          const monitor = document.querySelector("#monitorId");
          const type = document.querySelector("#type");
          const selected = document.querySelector(".pro_select .mypro ul");
          if (start) start.value = {json.dumps(start_date)};
          if (end) end.value = {json.dumps(end_date)};
          if (monitor) monitor.value = {json.dumps(prod_id)};
          if (type) type.value = "dd";
          if (selected) selected.innerHTML =
            '<li><div class="proname" prod-id="{prod_id}">' +
            '{escape_js_html(product_name)}</div><span class="close"></span></li>';
          result.status = "submitted";
          result.url = location.href;
          form.submit();
          return JSON.stringify(result);
        }})()
        """
        return self._run_json(js)

    def current_page(self) -> dict[str, str]:
        js = "JSON.stringify({url: location.href, html: document.documentElement.outerHTML})"
        return self._run_json(js)

    def _run_json(self, js: str) -> dict[str, str]:
        script = "\n".join(
            [
                "on run argv",
                '  tell application "Safari" to do JavaScript (item 1 of argv) in current tab of front window',
                "end run",
            ]
        )
        try:
            result = subprocess.run(["osascript", "-e", script, js], check=True, text=True, capture_output=True)
            return json.loads(result.stdout)
        except subprocess.CalledProcessError as exc:
            return {
                "status": "blocked",
                "code": "safari_automation_error",
                "message": (exc.stderr or exc.stdout or str(exc)).strip()[:1000],
            }
        except json.JSONDecodeError as exc:
            return {"status": "blocked", "code": "safari_json_decode_error", "message": str(exc)}


def default_capture_window(*, now: datetime | None = None) -> tuple[str, str]:
    """Return the bounded recent CCF query window in the source's local calendar."""

    local_now = (now or datetime.now(UTC)).astimezone(CCF_TIMEZONE)
    end_date = local_now.date()
    start_date = end_date - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    return start_date.strftime("%Y/%m/%d"), end_date.strftime("%Y/%m/%d")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run guarded CCF live price page capture batch from Safari.")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--run-dir", type=Path, default=DEFAULT_RUN_DIR)
    parser.add_argument("--product", action="append", default=[])
    default_start_date, default_end_date = default_capture_window()
    parser.add_argument("--start-date", default=default_start_date)
    parser.add_argument("--end-date", default=default_end_date)
    parser.add_argument("--wait-seconds", type=float, default=3.0)
    parser.add_argument(
        "--license-scope", default="authorized CCF account; internal research/action-grade candidate review"
    )
    args = parser.parse_args()

    product_names = args.product or DEFAULT_BATCH_PRODUCTS
    output_dir = args.output_dir.resolve()
    run_dir = args.run_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir.mkdir(parents=True, exist_ok=True)
    report = run_batch(
        executor=OsaScriptSafariExecutor(),
        product_names=product_names,
        run_dir=run_dir,
        start_date=args.start_date,
        end_date=args.end_date,
        wait_seconds=args.wait_seconds,
        license_scope=args.license_scope,
    )
    json_path = output_dir / "ccf-live-batch-capture-latest.json"
    md_path = output_dir / "55-ccf-live-batch-capture.md"
    write_json(json_path, report)
    md_path.write_text(render_report(report, json_path=json_path), encoding="utf-8")
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["ready_captures"] > 0 and report["summary"]["blocked"] == 0 else 2


def run_batch(
    *,
    executor: SafariExecutor,
    product_names: list[str],
    run_dir: Path,
    start_date: str,
    end_date: str,
    wait_seconds: float,
    license_scope: str,
) -> dict[str, Any]:
    generated_at = datetime.now(UTC).isoformat()
    results = []
    for product_name in product_names:
        product = PRICE_PRODUCTS.get(product_name)
        if not product:
            results.append(
                product_result(
                    product_name, "blocked", "unsupported_product", "Product is not in PRICE_PRODUCTS mapping."
                )
            )
            break
        submit = executor.submit_product(product_name, product["prod_id"], start_date, end_date)
        if submit.get("status") != "submitted":
            results.append(
                product_result(
                    product_name, "blocked", submit.get("code", "submit_failed"), submit.get("message", "submit failed")
                )
            )
            break
        time.sleep(wait_seconds)
        current = executor.current_page()
        page = live_capture.parse_page(str(current.get("html") or ""), source_url=str(current.get("url") or ""))
        capture_report = live_capture.build_report(page, expected_products=[product["expected"]])
        capture_path = run_dir / f"{slug(product_name)}-capture.json"
        write_json(capture_path, capture_report)
        package_dir = run_dir / f"{slug(product_name)}-package"
        package_report = materialize.build_package(
            capture=capture_report,
            package_dir=package_dir,
            license_scope=license_scope,
            account_scope="",
        )
        package_path = run_dir / f"{slug(product_name)}-package.json"
        write_json(package_path, package_report)
        status = (
            "ready"
            if capture_report["summary"]["capture_ready"] and package_report["summary"]["package_ready"]
            else "blocked"
        )
        code = "" if status == "ready" else first_issue_code(capture_report, package_report)
        results.append(
            {
                "product": product_name,
                "prod_id": product["prod_id"],
                "expected_product": product["expected"],
                "status": status,
                "code": code,
                "capture_ready": capture_report["summary"]["capture_ready"],
                "package_ready": package_report["summary"]["package_ready"],
                "price_rows": capture_report["summary"]["price_rows"],
                "capture_json": str(capture_path),
                "package_json": str(package_path),
                "package_dir": str(package_dir),
            }
        )
        if status != "ready":
            break
    return build_report(
        generated_at=generated_at, run_dir=run_dir, start_date=start_date, end_date=end_date, results=results
    )


def build_report(
    *, generated_at: str, run_dir: Path, start_date: str, end_date: str, results: list[dict[str, Any]]
) -> dict[str, Any]:
    ready = [row for row in results if row.get("status") == "ready"]
    blocked = [row for row in results if row.get("status") == "blocked"]
    return {
        "schema_version": "ccf_live_batch_capture.v1",
        "generated_at": generated_at,
        "scope": {
            "mode": "guarded_safari_authorized_capture_batch",
            "authorization_scope": "ccf_authorized_page_capture_internal_only",
            "db_writes": 0,
            "provider_calls": "Safari page submissions only; no direct CCF scraping outside logged-in browser context",
            "no_auth_bypass": True,
            "no_captcha_bypass": True,
            "start_date": start_date,
            "end_date": end_date,
        },
        "summary": {
            "requested_products": len(results),
            "ready_captures": len(ready),
            "blocked": len(blocked),
            "price_rows": sum(int(row.get("price_rows") or 0) for row in ready),
            "db_writes": 0,
        },
        "results": results,
        "artifacts": {"run_dir": str(run_dir)},
    }


def product_result(product_name: str, status: str, code: str, message: str) -> dict[str, Any]:
    return {
        "product": product_name,
        "prod_id": "",
        "expected_product": product_name,
        "status": status,
        "code": code,
        "message": message,
        "capture_ready": False,
        "package_ready": False,
        "price_rows": 0,
    }


def first_issue_code(capture_report: dict[str, Any], package_report: dict[str, Any]) -> str:
    for issue in capture_report.get("issues", []):
        return str(issue.get("code") or "capture_blocked")
    for issue in package_report.get("issues", []):
        return str(issue.get("code") or "package_blocked")
    return "capture_or_package_not_ready"


def render_report(report: dict[str, Any], *, json_path: Path) -> str:
    summary = report["summary"]
    return (
        "\n".join(
            [
                "# 55. CCF Live Batch Capture",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"ready_captures: `{summary['ready_captures']}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| requested_products | {summary['requested_products']} |",
                f"| ready_captures | {summary['ready_captures']} |",
                f"| blocked | {summary['blocked']} |",
                f"| price_rows | {summary['price_rows']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Results",
                "",
                markdown_table(
                    report["results"], ["product", "status", "code", "price_rows", "capture_ready", "package_ready"]
                ),
                "",
                "## Guardrails",
                "",
                "- Stops on CCF login page, missing price form, or missing slider verification.",
                "- Uses Safari logged-in page context only; no direct unauthenticated CCF HTTP scraping.",
                "- No database writes were performed.",
                "- Any future DB apply must first back up `/path/to/project/server/data/agent.db`.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- run_dir: `{report['artifacts']['run_dir']}`",
            ]
        )
        + "\n"
    )


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def slug(text: str) -> str:
    keep = [ch.lower() if ch.isalnum() else "-" for ch in text]
    return "-".join("".join(keep).strip("-").split("-"))[:80]


def escape_js_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No rows_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())

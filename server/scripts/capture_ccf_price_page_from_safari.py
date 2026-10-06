#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

DEFAULT_OUTPUT_DIR = Path(".codex-run/full-chain-delivery")
DATE_RE = re.compile(r"^\d{4}/\d{2}/\d{2}$")

# CCF's selected-product chip and result table can use different, but
# documented source-native, word order for the same product.  This is a
# deliberately closed equivalence set; any unlisted label still fails closed.
SOURCE_NATIVE_PRODUCT_ALIASES = {
    "PX CFR中国": "CFR中国PX",
    "日本石脑油": "CFR日本石脑油",
}


@dataclass
class ParsedPage:
    title: str
    url: str
    html_sha256: str
    text: str
    selected_products: list[str]
    rows: list[dict[str, str]]


class CcfTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.url = ""
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._table_stack: list[list[list[str]]] = []
        self.tables: list[list[list[str]]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._cell_depth = 0
        self._in_selected_product = False
        self._selected_depth = 0
        self._selected_parts: list[str] = []
        self.selected_products: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key: value or "" for key, value in attrs}
        if tag == "title":
            self._in_title = True
        if tag == "meta" and attrs_map.get("property") == "og:url":
            self.url = attrs_map.get("content", "")
        if tag == "table":
            self._table_stack.append([])
        if tag == "tr" and self._table_stack:
            self._current_row = []
        if tag in {"td", "th"} and self._current_row is not None:
            self._current_cell = []
            self._cell_depth = 1
        elif self._current_cell is not None:
            self._cell_depth += 1
        class_attr = attrs_map.get("class", "")
        if tag in {"div", "span"} and "proname" in class_attr.split():
            self._in_selected_product = True
            self._selected_depth = 1
            self._selected_parts = []
        elif self._in_selected_product:
            self._selected_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            self.title = normalize("".join(self._title_parts))
        if tag in {"td", "th"} and self._current_cell is not None and self._current_row is not None:
            self._current_row.append(normalize("".join(self._current_cell)))
            self._current_cell = None
            self._cell_depth = 0
        elif self._current_cell is not None and self._cell_depth > 0:
            self._cell_depth -= 1
        if tag == "tr" and self._current_row is not None and self._table_stack:
            if any(cell for cell in self._current_row):
                self._table_stack[-1].append(self._current_row)
            self._current_row = None
        if tag == "table" and self._table_stack:
            self.tables.append(self._table_stack.pop())
        if self._in_selected_product:
            self._selected_depth -= 1
            if self._selected_depth <= 0:
                selected = normalize("".join(self._selected_parts))
                if selected:
                    self.selected_products.append(selected)
                self._in_selected_product = False
                self._selected_parts = []

    def handle_data(self, data: str) -> None:
        self._text_parts.append(data)
        if self._in_title:
            self._title_parts.append(data)
        if self._current_cell is not None:
            self._current_cell.append(data)
        if self._in_selected_product:
            self._selected_parts.append(data)

    @property
    def text(self) -> str:
        return normalize(" ".join(self._text_parts))


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture and validate a CCF price page table without DB writes.")
    parser.add_argument("--html-file", type=Path)
    parser.add_argument("--from-safari", action="store_true")
    parser.add_argument("--expected-product", action="append", default=[])
    parser.add_argument("--source-url", default="https://www.ccf.com.cn/datacenter/price.php")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    html, source_url = load_html(args)
    page = parse_page(html, source_url=source_url or args.source_url)
    report = build_report(page, expected_products=args.expected_product)
    write_artifacts(output_dir, report)
    print(json.dumps({"status": "success", "summary": report["summary"]}, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["capture_ready"] else 2


def load_html(args: argparse.Namespace) -> tuple[str, str]:
    if args.html_file:
        return args.html_file.read_text(encoding="utf-8"), args.source_url
    if args.from_safari:
        js = "JSON.stringify({url: location.href, html: document.documentElement.outerHTML})"
        script = f'tell application "Safari" to do JavaScript {json.dumps(js)} in current tab of front window'
        result = subprocess.run(["osascript", "-e", script], check=True, text=True, capture_output=True)
        payload = json.loads(result.stdout)
        return str(payload.get("html") or ""), str(payload.get("url") or "")
    raise SystemExit("Specify --html-file or --from-safari")


def parse_page(html: str, *, source_url: str = "") -> ParsedPage:
    parser = CcfTableParser()
    parser.feed(html)
    rows = extract_price_rows(parser.tables)
    return ParsedPage(
        title=parser.title,
        url=source_url or parser.url,
        html_sha256=sha256(html.encode("utf-8")).hexdigest(),
        text=parser.text,
        selected_products=parser.selected_products,
        rows=rows,
    )


def extract_price_rows(tables: list[list[list[str]]]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for table in tables:
        normalized_rows = [[normalize(cell) for cell in row] for row in table]
        has_price_header = any(
            len(row) >= 3 and row[0] == "产品" and row[1] == "日期" and row[2] == "均价" for row in normalized_rows
        )
        if not has_price_header:
            continue
        for row in normalized_rows:
            if len(row) < 3 or row[0] == "产品":
                continue
            product, observed_at, price = row[0], row[1], row[2]
            if DATE_RE.match(observed_at) and is_number(price):
                rows.append({"product": product, "observed_at": observed_at, "price": price})
    return dedupe_rows(rows)


def build_report(page: ParsedPage, *, expected_products: list[str]) -> dict[str, Any]:
    expected = [item.strip() for item in expected_products if item.strip()]
    issues = validate_page(page, expected)
    blockers = [issue for issue in issues if issue["severity"] == "blocker"]
    products = sorted({row["product"] for row in page.rows})
    return {
        "schema_version": "ccf_live_price_page_capture.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": {
            "mode": "authorized_page_capture_validation_only",
            "db_writes": 0,
            "provider_calls": 0,
            "no_auth_bypass": True,
            "no_captcha_bypass": True,
        },
        "source": {
            "title": page.title,
            "source_url": page.url,
            "source_html_sha256": page.html_sha256,
            "selected_products": page.selected_products,
            "expected_products": expected,
        },
        "summary": {
            "capture_ready": not blockers and bool(page.rows),
            "price_rows": len(page.rows),
            "products": products,
            "blockers": len(blockers),
            "issues": len(issues),
            "db_writes": 0,
        },
        "issues": issues,
        "price_rows": page.rows,
    }


def validate_page(page: ParsedPage, expected_products: list[str]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    if is_login_page(page):
        issues.append(
            issue(
                "blocker",
                "ccf_login_required",
                "CCF page is a member login page; user must re-login and complete required verification.",
            )
        )
    if not page.rows:
        issues.append(issue("blocker", "no_price_rows", "No valid 产品/日期/均价 rows were found."))
    found_products = {comparable_product_label(row["product"]) for row in page.rows}
    selected = {comparable_product_label(product) for product in page.selected_products}
    if expected_products:
        missing = [
            product for product in expected_products if comparable_product_label(product) not in found_products
        ]
        if missing:
            issues.append(
                issue(
                    "blocker",
                    "expected_product_missing",
                    f"Expected product(s) missing from table: {', '.join(missing)}",
                )
            )
    if selected and found_products and not (selected & found_products):
        issues.append(
            issue(
                "blocker",
                "selected_product_table_mismatch",
                f"Selected product(s) {sorted(selected)} do not match table product(s) {sorted(found_products)}.",
            )
        )
    return issues


def comparable_product_label(product: str) -> str:
    """Return the closed source-native label used for table/selection comparison."""

    return SOURCE_NATIVE_PRODUCT_ALIASES.get(product, product)


def is_login_page(page: ParsedPage) -> bool:
    text = page.text
    title = page.title
    return "会员登录" in text or "请登录" in text or "member.php?action=login" in page.url or "会员登录" in title


def write_artifacts(output_dir: Path, report: dict[str, Any]) -> None:
    json_path = output_dir / "ccf-live-price-page-capture-latest.json"
    csv_path = output_dir / "ccf-live-price-page-capture-latest.csv"
    md_path = output_dir / "53-ccf-live-price-page-capture.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(csv_path, report["price_rows"])
    md_path.write_text(render_report(report, json_path=json_path, csv_path=csv_path), encoding="utf-8")


def render_report(report: dict[str, Any], *, json_path: Path, csv_path: Path) -> str:
    summary = report["summary"]
    return (
        "\n".join(
            [
                "# 53. CCF Live Price Page Capture",
                "",
                f"Generated at: `{report['generated_at']}`",
                "",
                "## Conclusion",
                "",
                f"capture_ready: `{summary['capture_ready']}`",
                "",
                "| metric | value |",
                "| --- | ---: |",
                f"| price_rows | {summary['price_rows']} |",
                f"| blockers | {summary['blockers']} |",
                f"| issues | {summary['issues']} |",
                f"| db_writes | {summary['db_writes']} |",
                "",
                "## Products",
                "",
                ", ".join(summary["products"]) if summary["products"] else "_No products captured_",
                "",
                "## Issues",
                "",
                markdown_table(report["issues"], ["severity", "code", "message"]),
                "",
                "## Guardrails",
                "",
                "- No database writes were performed.",
                "- This capture is not action-grade until source evidence review passes.",
                (
                    "- If the page is a login page or the table product does not match the selected/expected product,"
                    " capture is blocked."
                ),
                "- Any future DB apply must first back up `/path/to/project/server/data/agent.db`.",
                "",
                "## Artifacts",
                "",
                f"- JSON: `{json_path}`",
                f"- CSV: `{csv_path}`",
            ]
        )
        + "\n"
    )


def issue(severity: str, code: str, message: str) -> dict[str, str]:
    return {"severity": severity, "code": code, "message": message}


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_number(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


def dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    out = []
    for row in rows:
        key = (row["product"], row["observed_at"], row["price"])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    fieldnames = ["product", "observed_at", "price"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def markdown_table(rows: list[dict[str, Any]], headers: list[str]) -> str:
    if not rows:
        return "_No issues_"
    out = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        out.append("| " + " | ".join(str(row.get(header, "")).replace("|", "/") for header in headers) + " |")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())

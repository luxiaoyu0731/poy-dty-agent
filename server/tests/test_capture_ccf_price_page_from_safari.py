from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    script_path = SERVER_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


capture = load_script("capture_ccf_price_page_from_safari")


def test_parse_valid_ccf_price_page() -> None:
    page = capture.parse_page(
        """
        <html><head><title>CCF数据中心 | 化纤信息网</title></head>
        <body>
          <form id="adminForm"><div class="proname" prod-id="5">内盘MEG现货</div></form>
          <table>
            <tr><td>产品</td><td>日期</td><td>均价</td></tr>
            <tr><td>内盘MEG现货</td><td>2026/07/03</td><td>4550</td></tr>
            <tr><td>内盘MEG现货</td><td>2026/07/02</td><td>4520.5</td></tr>
          </table>
        </body></html>
        """,
        source_url="https://www.ccf.com.cn/datacenter/price.php",
    )

    report = capture.build_report(page, expected_products=["内盘MEG现货"])

    assert report["summary"]["capture_ready"] is True
    assert report["summary"]["price_rows"] == 2
    assert report["source"]["source_url"] == "https://www.ccf.com.cn/datacenter/price.php"
    assert report["source"]["source_html_sha256"]
    assert report["price_rows"][0] == {"product": "内盘MEG现货", "observed_at": "2026/07/03", "price": "4550"}


def test_login_page_blocks_capture() -> None:
    page = capture.parse_page(
        """
        <html><head><title>会员登录</title></head>
        <body><h3>当前位置： 会员登录</h3><p>请登录或注册会员！</p></body></html>
        """,
        source_url="https://www.ccf.com.cn/member/member.php?action=login&url=/datacenter/price.php",
    )

    report = capture.build_report(page, expected_products=["内盘MEG现货"])

    assert report["summary"]["capture_ready"] is False
    codes = {issue["code"] for issue in report["issues"]}
    assert "ccf_login_required" in codes
    assert "no_price_rows" in codes


def test_selected_product_mismatch_blocks_stale_dom_capture() -> None:
    page = capture.parse_page(
        """
        <html><body>
          <div class="proname" prod-id="5">内盘MEG现货</div>
          <table>
            <tr><td>产品</td><td>日期</td><td>均价</td></tr>
            <tr><td>WTI期货</td><td>2026/07/02</td><td>68.69</td></tr>
          </table>
        </body></html>
        """,
        source_url="https://www.ccf.com.cn/datacenter/price.php",
    )

    report = capture.build_report(page, expected_products=["内盘MEG现货"])

    assert report["summary"]["capture_ready"] is False
    codes = {issue["code"] for issue in report["issues"]}
    assert "expected_product_missing" in codes
    assert "selected_product_table_mismatch" in codes


def test_source_native_naphtha_result_label_matches_selected_product() -> None:
    page = capture.parse_page(
        """
        <html><body>
          <div class="proname" prod-id="36">日本石脑油</div>
          <table>
            <tr><td>产品</td><td>日期</td><td>均价</td></tr>
            <tr><td>CFR日本石脑油</td><td>2026/08/05</td><td>600</td></tr>
          </table>
        </body></html>
        """,
        source_url="https://www.ccf.com.cn/datacenter/price.php",
    )

    report = capture.build_report(page, expected_products=["CFR日本石脑油"])

    assert report["summary"]["capture_ready"] is True
    assert report["summary"]["products"] == ["CFR日本石脑油"]

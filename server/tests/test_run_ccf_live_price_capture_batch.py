from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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


batch = load_script("run_ccf_live_price_capture_batch")


def test_default_capture_window_uses_the_shanghai_calendar() -> None:
    start, end = batch.default_capture_window(
        now=datetime(2026, 8, 3, 16, 30, tzinfo=ZoneInfo("UTC"))
    )

    assert (start, end) == ("2026/07/28", "2026/08/04")


def test_px_capture_uses_the_source_native_table_label() -> None:
    assert batch.PRICE_PRODUCTS["PX CFR中国"] == {"prod_id": "127", "expected": "CFR中国PX"}


def test_naphtha_capture_uses_the_source_native_table_label() -> None:
    assert batch.PRICE_PRODUCTS["日本石脑油"] == {"prod_id": "36", "expected": "CFR日本石脑油"}


class FakeBlockedExecutor:
    def submit_product(self, product_name: str, prod_id: str, start_date: str, end_date: str) -> dict[str, str]:
        return {"status": "blocked", "code": "ccf_login_required", "message": "login"}

    def current_page(self) -> dict[str, str]:
        raise AssertionError("current_page should not be called when submit is blocked")


class FakeReadyExecutor:
    def submit_product(self, product_name: str, prod_id: str, start_date: str, end_date: str) -> dict[str, str]:
        assert product_name == "内盘MEG现货"
        assert prod_id == "5"
        assert start_date == "2026/01/05"
        assert end_date == "2026/07/05"
        return {"status": "submitted", "url": "https://www.ccf.com.cn/datacenter/price.php"}

    def current_page(self) -> dict[str, str]:
        return {
            "url": "https://www.ccf.com.cn/datacenter/price.php",
            "html": """
            <html><head><title>CCF数据中心 | 化纤信息网</title></head>
            <body>
              <div class="proname" prod-id="5">内盘MEG现货</div>
              <table>
                <tr><td>产品</td><td>日期</td><td>均价</td></tr>
                <tr><td>内盘MEG现货</td><td>2026/07/03</td><td>4550</td></tr>
              </table>
            </body></html>
            """,
        }


class FakeAutomationErrorExecutor:
    def submit_product(self, product_name: str, prod_id: str, start_date: str, end_date: str) -> dict[str, str]:
        return {"status": "blocked", "code": "safari_automation_error", "message": "osascript failed"}

    def current_page(self) -> dict[str, str]:
        raise AssertionError("current_page should not be called when automation is blocked")


def test_batch_stops_on_login_blocker(tmp_path: Path) -> None:
    report = batch.run_batch(
        executor=FakeBlockedExecutor(),
        product_names=["内盘MEG现货"],
        run_dir=tmp_path,
        start_date="2026/01/05",
        end_date="2026/07/05",
        wait_seconds=0,
        license_scope="authorized",
    )

    assert report["summary"]["ready_captures"] == 0
    assert report["summary"]["blocked"] == 1
    assert report["results"][0]["code"] == "ccf_login_required"


def test_batch_converts_safari_automation_error_to_blocker(tmp_path: Path) -> None:
    report = batch.run_batch(
        executor=FakeAutomationErrorExecutor(),
        product_names=["内盘MEG现货"],
        run_dir=tmp_path,
        start_date="2026/01/05",
        end_date="2026/07/05",
        wait_seconds=0,
        license_scope="authorized",
    )

    assert report["summary"]["ready_captures"] == 0
    assert report["summary"]["blocked"] == 1
    assert report["results"][0]["code"] == "safari_automation_error"


def test_batch_materializes_ready_capture_package(tmp_path: Path) -> None:
    report = batch.run_batch(
        executor=FakeReadyExecutor(),
        product_names=["内盘MEG现货"],
        run_dir=tmp_path,
        start_date="2026/01/05",
        end_date="2026/07/05",
        wait_seconds=0,
        license_scope="authorized",
    )

    assert report["summary"]["ready_captures"] == 1
    assert report["summary"]["blocked"] == 0
    assert report["summary"]["price_rows"] == 1
    result = report["results"][0]
    assert result["capture_ready"] is True
    assert result["package_ready"] is True
    assert Path(result["capture_json"]).exists()
    assert Path(result["package_json"]).exists()
    assert (Path(result["package_dir"]) / "ccf_live_price_capture.csv").exists()

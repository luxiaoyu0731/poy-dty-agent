from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
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


audit = load_script("audit_ccf_safari_session_state")


def test_login_redirect_blocks_capture() -> None:
    report = audit.build_report(
        audit.SafariState(
            title="化纤、纺织行业研究咨询机构 | 化纤信息网",
            url="https://www.ccf.com.cn/member/member.php?action=login&url=/datacenter/price.php",
        ),
        generated_at=datetime.now(UTC),
    )

    assert report["summary"]["capture_can_continue"] is False
    assert report["summary"]["ccf_domain_detected"] is True
    assert {item["code"] for item in report["blockers"]} == {"ccf_login_required"}
    assert report["summary"]["db_writes"] == 0


def test_authorized_data_center_page_can_continue() -> None:
    report = audit.build_report(
        audit.SafariState(
            title="CCF数据中心 | 化纤信息网",
            url="https://www.ccf.com.cn/datacenter/price.php",
        ),
        generated_at=datetime.now(UTC),
    )

    assert report["summary"]["capture_can_continue"] is True
    assert report["summary"]["ccf_domain_detected"] is True
    assert report["summary"]["ccf_data_center_detected"] is True
    assert report["blockers"] == []


def test_non_ccf_page_blocks_capture() -> None:
    report = audit.build_report(
        audit.SafariState(title="Example", url="https://example.com/"),
        generated_at=datetime.now(UTC),
    )

    assert report["summary"]["capture_can_continue"] is False
    assert {item["code"] for item in report["blockers"]} == {"ccf_page_not_open"}


def test_unreadable_safari_state_blocks_capture() -> None:
    report = audit.build_report(
        audit.SafariState(title="", url="", readable=False, error="osascript_timeout"),
        generated_at=datetime.now(UTC),
    )

    assert report["summary"]["capture_can_continue"] is False
    codes = {item["code"] for item in report["blockers"]}
    assert "safari_state_unreadable" in codes
    assert "safari_no_current_url" in codes

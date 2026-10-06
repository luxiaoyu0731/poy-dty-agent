from __future__ import annotations

import pytest

from app import delivery_status
from app.report_delivery import normalize_client_report


@pytest.mark.parametrize(
    ("overrides", "expected_content_status"),
    [
        ({"formal_report_eligible": False}, "missing"),
        ({"download_available": False}, "missing"),
        ({"content_status": "missing"}, "missing"),
        ({"path": ""}, "missing"),
        ({"quality_gate_status": "missing"}, "missing"),
    ],
)
def test_client_report_readiness_fails_closed_when_a_delivery_gate_is_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object], expected_content_status: str
) -> None:
    report_file = tmp_path / "daily.md"
    report_file.write_text("# 日报\n有效正文", encoding="utf-8")
    monkeypatch.setattr(delivery_status, "REPO_ROOT", tmp_path)
    report = {
        "id": "daily-test",
        "status": "ready",
        "path": str(report_file),
        "formal_report_eligible": True,
        "quality_gate_status": "passed",
        "download_available": True,
        "content_status": "ready",
        **overrides,
    }

    normalized = normalize_client_report(report, project_root=tmp_path)

    assert normalized["status"] == "needs_human_review"
    assert normalized["content_status"] == expected_content_status
    assert normalized["download_available"] is False
    assert normalized["formal_report_eligible"] is False
    assert normalized["path"] == ""


def test_client_report_readiness_accepts_nonempty_downloadable_formal_report(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report_file = tmp_path / "daily.md"
    report_file.write_text("# 日报\n有效正文", encoding="utf-8")
    monkeypatch.setattr(delivery_status, "REPO_ROOT", tmp_path)

    normalized = normalize_client_report(
        {
            "id": "daily-test",
            "status": "ready",
            "path": str(report_file),
            "formal_report_eligible": True,
            "quality_gate_status": "passed",
            "download_available": True,
            "content_status": "ready",
        },
        project_root=tmp_path,
    )

    assert normalized["status"] == "ready"
    assert normalized["content_status"] == "ready"
    assert normalized["download_available"] is True
    assert normalized["path"] == str(report_file)


def test_client_report_readiness_accepts_ready_page_report_without_claiming_download(tmp_path) -> None:
    normalized = normalize_client_report(
        {
            "id": "daily-page",
            "status": "ready",
            "delivery_mode": "page",
            "formal_report_eligible": False,
            "quality_gate_status": "passed",
            "download_available": False,
            "content_status": "ready",
            "summary": "页面日报已生成。",
        },
        project_root=tmp_path,
    )

    assert normalized["status"] == "ready"
    assert normalized["content_status"] == "ready"
    assert normalized["download_available"] is False
    assert normalized["path"] == ""

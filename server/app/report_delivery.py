from __future__ import annotations

from pathlib import Path
from typing import Any


def normalize_client_report(report: dict[str, Any], *, project_root: Path) -> dict[str, Any]:
    """Expose ready only when every formal delivery condition is verifiably true."""
    path = str(report.get("path") or "").strip()
    candidate = Path(path) if path else None
    if candidate is not None and not candidate.is_absolute():
        candidate = project_root / candidate
    content_exists = bool(candidate and candidate.exists() and candidate.is_file() and candidate.stat().st_size > 0)
    page_ready = (
        report.get("delivery_mode") == "page"
        and report.get("content_status") == "ready"
        and report.get("quality_gate_status") == "passed"
        and bool(str(report.get("summary") or "").strip())
    )
    if page_ready:
        return {
            **report,
            "path": "",
            "status": "ready",
            "download_available": False,
            "content_status": "ready",
            "disabled_reason": str(report.get("disabled_reason") or "当前提供页面版日报，暂不提供文件下载。"),
        }
    eligible = (
        report.get("formal_report_eligible") is True
        and report.get("quality_gate_status") == "passed"
        and report.get("download_available") is True
        and report.get("content_status") == "ready"
        and content_exists
    )
    if eligible:
        return {**report, "path": path}
    return {
        **report,
        "path": "",
        "status": "needs_human_review",
        "download_available": False,
        "content_status": "missing",
        "disabled_reason": str(report.get("disabled_reason") or "报告文件尚未生成或未通过正式交付门禁。"),
        "formal_report_eligible": False,
        "quality_gate_status": str(report.get("quality_gate_status") or "missing"),
    }

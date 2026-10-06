"""Handoff-file state distinctions in the pipeline graph.

节点间的 JSON 交接文件此前"损坏"与"未运行"不可区分，画布只能显示 waiting。
这组测试锁住 missing / corrupt / stale / ok 四态。
"""

from __future__ import annotations

import json
from pathlib import Path

from app import pipeline_graph


def _write_report(path: Path, business_date: str) -> None:
    path.write_text(
        json.dumps({"business_date": business_date, "as_of_time": f"{business_date}T00:30:00+00:00"}),
        encoding="utf-8",
    )


def test_chain_report_state_distinguishes_missing_corrupt_stale(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline_graph, "_local_production_dir", lambda: tmp_path)
    path = tmp_path / "event-agent-chain-latest.json"

    assert pipeline_graph._chain_report_state("2026-10-02") == ({}, "missing")

    path.write_text("{broken", encoding="utf-8")
    assert pipeline_graph._chain_report_state("2026-10-02") == ({}, "corrupt")

    _write_report(path, "2026-10-01")
    assert pipeline_graph._chain_report_state("2026-10-02") == ({}, "stale")

    _write_report(path, "2026-10-02")
    report, state = pipeline_graph._chain_report_state("2026-10-02")
    assert state == ""
    assert report["business_date"] == "2026-10-02"


def test_chain_stage_node_reports_corrupt_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline_graph, "_local_production_dir", lambda: tmp_path)
    (tmp_path / "event-agent-chain-latest.json").write_text("{broken", encoding="utf-8")
    node = pipeline_graph._chain_stage_node(
        "2026-10-02",
        stage="political_analysis",
        label="政局解读",
        build_detail=lambda *args: "",
    )
    assert node["status"] == "waiting"
    assert "损坏" in node["status_detail"]


def test_chain_stage_node_reports_stale_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline_graph, "_local_production_dir", lambda: tmp_path)
    _write_report(tmp_path / "event-agent-chain-latest.json", "2026-10-01")
    node = pipeline_graph._chain_stage_node(
        "2026-10-02",
        stage="political_analysis",
        label="政局解读",
        build_detail=lambda *args: "",
    )
    assert node["status"] == "waiting"
    assert "非当日" in node["status_detail"]


def test_event_signal_node_reports_corrupt_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline_graph, "_local_production_dir", lambda: tmp_path)
    (tmp_path / "event-signal-latest.json").write_text("[not-an-object", encoding="utf-8")
    node = pipeline_graph._event_signal_node("2026-10-02", pipeline_graph._now())
    assert node["status"] == "waiting"
    assert "损坏" in node["status_detail"]

from pathlib import Path

import pytest

from app.pipeline_state_paths import local_production_directory, shared_state_root


def test_docker_direct_volume_and_legacy_local_layout_share_correct_root(monkeypatch, tmp_path):
    monkeypatch.delenv("PIPELINE_GRAPH_STATE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_PRODUCTION_OUTPUT_DIR", raising=False)
    assert shared_state_root("/data/agent.db") == Path("/data")
    assert local_production_directory(shared_state_root("/data/agent.db")) == Path("/data/local-production")
    assert shared_state_root(tmp_path / "legacy" / "data" / "agent.db") == tmp_path / "legacy"


def test_graph_root_and_scheduler_output_overrides_are_respected(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPELINE_GRAPH_STATE_ROOT", str(tmp_path / "state"))
    assert shared_state_root("/data/agent.db") == tmp_path / "state"
    monkeypatch.setenv("LOCAL_PRODUCTION_OUTPUT_DIR", str(tmp_path / "reports"))
    assert local_production_directory(shared_state_root("/data/agent.db")) == tmp_path / "reports"


def test_actual_default_port_and_graph_use_one_report_and_budget_location(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from app import chain_http_budget, pipeline_graph
    from app import settings as settings_module
    from app.agent_chain import DeepSeekJsonPort

    class Client:
        model = "fixture"

        def set_http_attempt_budget(self, value, on_attempt):
            self.http_attempt_callback = on_attempt

    monkeypatch.setenv("AGENT_CHAIN_DAILY_CAP", "40")
    monkeypatch.setenv("PIPELINE_GRAPH_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("LOCAL_PRODUCTION_OUTPUT_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(chain_http_budget, "current_business_date", lambda: "2026-10-04")
    monkeypatch.setattr(settings_module, "settings", SimpleNamespace(sqlite_path="/data/agent.db"))
    port = DeepSeekJsonPort(Client())
    port.bind_business_date("2026-10-04")
    assert port._journal.root == pipeline_graph._local_production_dir() / "chain-http-budget"
    assert port._journal.report_path == pipeline_graph._local_production_dir() / "event-agent-chain-latest.json"
    assert port.budget_snapshot()["attempts_used"] == 0


@pytest.mark.parametrize(
    "module_name,subdir",
    [
        ("counter_scan", "counter-scan"),
        ("daily_interpretation", "daily-interpretation"),
    ],
)
def test_post_snapshot_defaults_follow_graph_layout(monkeypatch, tmp_path, module_name, subdir):
    import importlib

    from app import pipeline_graph

    module = importlib.import_module(f"app.{module_name}")
    monkeypatch.delenv(f"AI_{module_name.upper()}_ARTIFACT_DIR", raising=False)
    monkeypatch.delenv("PIPELINE_GRAPH_STATE_ROOT", raising=False)
    monkeypatch.delenv("LOCAL_PRODUCTION_OUTPUT_DIR", raising=False)
    from types import SimpleNamespace

    monkeypatch.setattr(module, "settings", SimpleNamespace(sqlite_path="/data/agent.db"))
    assert module._artifact_dir() == Path("/data/local-production") / subdir
    monkeypatch.setattr(pipeline_graph, "settings", module.settings)
    assert module._artifact_dir() == pipeline_graph._local_production_dir() / subdir
    monkeypatch.setattr(module.settings, "sqlite_path", str(tmp_path / "legacy/data/agent.db"))
    assert module._artifact_dir() == tmp_path / "legacy/local-production" / subdir
    monkeypatch.setenv("PIPELINE_GRAPH_STATE_ROOT", str(tmp_path / "custom-state"))
    assert module._artifact_dir() == tmp_path / "custom-state/local-production" / subdir


@pytest.mark.parametrize(
    "module_name,subdir",
    [
        ("counter_scan", "counter-scan"),
        ("daily_interpretation", "daily-interpretation"),
    ],
)
def test_post_snapshot_shared_and_stage_overrides_write_read_same_artifact(monkeypatch, tmp_path, module_name, subdir):
    import importlib

    module = importlib.import_module(f"app.{module_name}")
    monkeypatch.delenv(f"AI_{module_name.upper()}_ARTIFACT_DIR", raising=False)
    from types import SimpleNamespace

    monkeypatch.setattr(module, "settings", SimpleNamespace(sqlite_path="/data/agent.db"))
    monkeypatch.setenv("LOCAL_PRODUCTION_OUTPUT_DIR", str(tmp_path / "reports"))
    assert module._artifact_dir() == tmp_path / "reports" / subdir
    # Explicit per-stage paths take precedence, even over the shared scheduler path.
    monkeypatch.setenv(f"AI_{module_name.upper()}_ARTIFACT_DIR", str(tmp_path / "explicit"))
    assert module._artifact_dir() == tmp_path / "explicit"
    day = "2026-10-07"
    completed = module.completed_artifact_path(day)
    degraded = module.degraded_artifact_path(day)
    module._atomic_write_json(degraded, {"status": "degraded", "business_date": day})
    loader = getattr(module, f"load_{module_name}_artifact")
    assert loader(day)["artifact_path"] == str(degraded)
    module._atomic_write_json(completed, {"status": "completed", "business_date": day})
    assert loader(day)["artifact_path"] == str(completed)
    assert loader(day)["status"] == "completed"
    assert completed.stat().st_mode & 0o777 == 0o600
    assert loader("2026-10-08") is None

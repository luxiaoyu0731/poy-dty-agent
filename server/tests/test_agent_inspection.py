import hashlib

import pytest

from app.agent_chain import _STAGE_CONSTITUTIONS, PROMPT_VERSIONS, STAGE_BUDGETS
from app.agent_inspection import agent_implementation_profile


@pytest.mark.parametrize("node", ["political_analysis", "historical_analog", "product_synthesis", "skeptic_review"])
def test_inspection_is_exact_current_code_not_a_fabricated_historical_request(node, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "configured-test-model")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "never-expose-this")
    profile = agent_implementation_profile(node)
    assert profile["system_prompt"] == _STAGE_CONSTITUTIONS[node]
    assert profile["prompt_version"] == PROMPT_VERSIONS[node]
    assert profile["prompt_sha256"] == hashlib.sha256(profile["system_prompt"].encode()).hexdigest()
    assert profile["stage_cap"] == STAGE_BUDGETS[node]
    assert profile["configured_model"] == "configured-test-model"
    assert "不是当次调用快照" in profile["basis"]
    assert "不能" in profile["historical_request"]
    assert "never-expose-this" not in str(profile)


@pytest.mark.parametrize("node", ["collect", "event_fusion", "assistant", "unified_memory", "event_summary"])
def test_other_nodes_never_receive_an_invented_chain_prompt(node):
    assert agent_implementation_profile(node) is None

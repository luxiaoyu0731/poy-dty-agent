import asyncio
import importlib.util
from pathlib import Path

import pytest

from app.evidence_semantic_review import POLICY
from app.prediction_inputs import digest

spec = importlib.util.spec_from_file_location(
    "coverage_worker", Path(__file__).parents[1] / "scripts/review_evidence_coverage.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_changed_source_manifest_cannot_start_review():
    packet = {"policy": POLICY, "articles": []}
    packet["content_sha256"] = digest(packet)
    assert m.validate_packet(packet) == []
    packet["articles"].append({"changed": True})
    with pytest.raises(ValueError, match="integrity"):
        m.validate_packet(packet)


def test_raw_receipt_prevents_repeated_http_when_parsing_or_validation_fails(tmp_path):
    calls = []

    class Client:
        model = "deepseek-v4-pro"

        def set_http_attempt_budget(self, limit):
            assert limit == 1

        async def _post_chat_completion(self, messages, **kwargs):
            calls.append(messages)
            return {"choices": [{"message": {"content": "invalid JSON"}}]}

    messages = [{"role": "user", "content": "source"}]
    client = Client()
    first = asyncio.run(m.recorded_completion(client, tmp_path, messages))
    second = asyncio.run(m.recorded_completion(client, tmp_path, messages))
    assert first == second and len(calls) == 1
    raw = next(tmp_path.glob("response-*.json"))
    raw.write_text("{}")
    with pytest.raises(ValueError, match="raw_response_changed"):
        asyncio.run(m.recorded_completion(client, tmp_path, messages))
    assert len(calls) == 1


def test_binding_cannot_expand_past_original_source():
    article = {"raw_text": "Exact subject fact. Scope crude oil.", "content_hash": "hash"}
    row = {"quote": "Exact subject fact.", "scope_quote": "Scope crude oil."}
    with pytest.raises(ValueError, match="literal_source_span"):
        m.binding_proof(article, row, "Invented text", {}, "test", "2026-10-04T00:00:00Z")
    with pytest.raises(ValueError, match="both_anchors"):
        m.binding_proof(article, row, "Exact subject fact.", {}, "test", "2026-10-04T00:00:00Z")

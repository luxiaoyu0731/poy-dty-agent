import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_event_summary_quality_gate import SOURCE, _facts, _impact
from test_prediction_evidence_runtime import prices, receipt

from app import event_summary_quality as quality
from app import prediction_evidence_runtime as runtime
from app.prediction_main import MainInputSnapshot


def request(**updates):
    return dict(source_text=SOURCE, input_quality="full_text", fact_output=_facts(), impact_output=_impact(), **updates)


def test_json_representations_share_checks_without_sharing_mutable_proof(monkeypatch):
    actual = quality._build_grounded_event_summary
    calls = []
    monkeypatch.setattr(quality, "_build_grounded_event_summary", lambda **kw: (calls.append(kw), actual(**kw))[1])
    expected = actual(**request())
    with quality.grounding_validation_scope():
        first = quality.build_grounded_event_summary(**request())
        first.facts.numbers[0].value = "999999"
        second_request = request()
        second_request["fact_output"] = json.dumps(second_request["fact_output"], ensure_ascii=False)
        second = quality.build_grounded_event_summary(**second_request)
        assert second == expected and len(calls) == 1
    assert quality._GROUNDING_MEMO.get() is None
    assert quality.build_grounded_event_summary(**request()) == expected
    assert len(calls) == 2


def test_changed_raw_input_and_non_json_structure_never_reuse_a_pass():
    with quality.grounding_validation_scope():
        assert quality.build_grounded_event_summary(**request()).usable
        missing_quote = request()
        missing_quote["source_text"] = SOURCE.replace("预计持续24小时", "预计持续12小时")
        assert not quality.build_grounded_event_summary(**missing_quote).usable
        changed_fact = request()
        changed_fact["fact_output"]["numbers"][0]["value"] = "999999"
        assert not quality.build_grounded_event_summary(**changed_fact).usable
        # JSON encoders collapse tuples to arrays; do not allow that collision.
        tuple_fact = request()
        tuple_fact["fact_output"]["numbers"] = tuple(tuple_fact["fact_output"]["numbers"])
        assert quality.build_grounded_event_summary(**tuple_fact) == quality._build_grounded_event_summary(**tuple_fact)


def test_scope_is_bounded_exception_safe_and_thread_isolated(monkeypatch):
    monkeypatch.setattr(quality, "_GROUNDING_MAX_ENTRIES", 1)
    with quality.grounding_validation_scope():
        quality.build_grounded_event_summary(**request())
        outer = quality._GROUNDING_MEMO.get()
        with pytest.raises(RuntimeError), quality.grounding_validation_scope():
            assert quality._GROUNDING_MEMO.get() is not outer
            raise RuntimeError("cancelled capture")
        assert quality._GROUNDING_MEMO.get() is outer
        revised = request()
        revised["source_text"] += " 次日追踪。"
        quality.build_grounded_event_summary(**revised)
        assert len(outer[0]) == 1 and outer[1][0] <= quality._GROUNDING_MAX_BYTES
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(lambda: quality._GROUNDING_MEMO.get()).result() is None
    assert quality._GROUNDING_MEMO.get() is None


def test_capture_reconstruction_keeps_exact_content_and_rejects_resealed_feature_forgery():
    exported = runtime.joint_input(prices(), receipt(), include_dossier=True)
    with quality.grounding_validation_scope():
        reused = runtime.joint_input(prices(), receipt(), include_dossier=True)
        assert reused == exported
        assert MainInputSnapshot(reused).exported == exported
        tampered = copy.deepcopy(reused)
        tampered["evidence_features"]["crude:1"]["values"]["current_support"] = 999
        tampered.pop("content_sha256")
        with pytest.raises(ValueError, match="reconstruction"):
            MainInputSnapshot(runtime.seal(tampered))

from __future__ import annotations

from app import main as main_module
from app import seven_product_evaluation


def setup_function(_function) -> None:
    main_module._SEVEN_PRODUCT_EVALUATION_CACHE.clear()


def test_evaluation_is_computed_once_within_ttl(monkeypatch) -> None:
    calls = {"count": 0}
    real = seven_product_evaluation.evaluate_seven_product_forecast

    def counting(**kwargs):
        calls["count"] += 1
        return real(**kwargs)

    monkeypatch.setattr(main_module, "evaluate_seven_product_forecast", counting)
    first = main_module._cached_seven_product_evaluation(None)
    second = main_module._cached_seven_product_evaluation(None)
    assert calls["count"] == 1
    assert second is first
    # Distinct cutoffs must not share the cached default entry.
    main_module._cached_seven_product_evaluation("2026-09-01T00:00:00+00:00")
    assert calls["count"] == 2


def test_zero_ttl_disables_caching(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_SEVEN_PRODUCT_EVALUATION_CACHE_TTL_SECONDS", 0.0)
    calls = {"count": 0}
    real = seven_product_evaluation.evaluate_seven_product_forecast

    def counting(**kwargs):
        calls["count"] += 1
        return real(**kwargs)

    monkeypatch.setattr(main_module, "evaluate_seven_product_forecast", counting)
    main_module._cached_seven_product_evaluation(None)
    main_module._cached_seven_product_evaluation(None)
    assert calls["count"] == 2

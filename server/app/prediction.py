from __future__ import annotations

from .models import FactorScore


def compute_cost_pressure_index(factors: list[FactorScore]) -> dict[str, object]:
    base = 50
    raw_contribution = sum(factor.contribution for factor in factors)
    score = max(0, min(100, base + round(raw_contribution * 0.58)))
    if score >= 70:
        status = "偏强"
    elif score >= 56:
        status = "中性偏强"
    elif score >= 44:
        status = "震荡"
    else:
        status = "偏弱"
    sorted_factors = sorted(factors, key=lambda item: abs(item.contribution), reverse=True)
    return {
        "cost_pressure_index": score,
        "status": status,
        "confidence": 0.76,
        "key_drivers": [factor.name for factor in sorted_factors[:4]],
    }

from __future__ import annotations

from .models import EventImpact, EventReasoning


def reason_about_event(event: EventImpact) -> EventReasoning:
    facts = [
        f"事件类型：{event.event_type}",
        f"影响周期：{event.horizon}",
        f"影响对象：{', '.join(event.affected_products)}",
        f"证据等级：{event.evidence_level}",
    ]
    motive_paths = [
        f"{stakeholder} 可能通过库存、产量、航线、舆论或交易仓位改变市场预期。"
        for stakeholder in event.stakeholders[:4]
    ]
    likely_impacts = [
        "上游成本压力指数可能出现短期抬升或波动扩大。",
        "若高等级数据确认，预警系统应提高原油-PX-PTA 链条权重。",
        "若反证出现，事件半衰期应缩短，避免被旧风险溢价误导。",
    ]
    return EventReasoning(
        event_id=event.event_id,
        thesis=(
            f"{event.title} 的核心不是简单判断涨跌，而是识别谁有动机、谁有能力、"
            "市场已经交易了多少，以及哪些数据能证伪。"
        ),
        facts=facts,
        possible_stakeholders=event.stakeholders,
        motive_paths=motive_paths,
        likely_impacts=likely_impacts,
        counter_evidence=event.counter_evidence,
        evidence_level=event.evidence_level,
        confidence=event.confidence,
    )

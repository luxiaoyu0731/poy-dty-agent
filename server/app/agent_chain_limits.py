"""Current production limits; historical reports retain their own snapshots."""

DEFAULT_HTTP_ATTEMPT_CAP = 60
STAGE_CALL_CAPS = {
    "political_analysis": 16,
    "historical_analog": 16,
    "product_synthesis": 7,
    "skeptic_review": 7,
    "event_adjudication": 2,
}

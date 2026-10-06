from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from types import MappingProxyType

from .source_publication_calendar import cfets_holiday_carry_reason


class SourceCapability(StrEnum):
    ADAPTER = "adapter"
    DIRECT_DOWNLOAD = "direct-download"
    MANUAL = "manual"


class SourceCriticality(StrEnum):
    CRITICAL = "critical"
    IMPORTANT = "important"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class SourceAutomationPolicy:
    source_id: str
    capability: SourceCapability
    criticality: SourceCriticality
    timeout_seconds: int
    frequency_seconds: int
    sla_seconds: int
    observation_sla_seconds: int
    production_schedulable: bool

    def __post_init__(self) -> None:
        if min(self.timeout_seconds, self.frequency_seconds, self.sla_seconds, self.observation_sla_seconds) <= 0:
            raise ValueError(f"{self.source_id}: timeout, frequency, and SLA must be positive")
        if self.production_schedulable and self.capability is not SourceCapability.ADAPTER:
            raise ValueError(f"{self.source_id}: only adapter-backed sources may be production schedulable")


@dataclass(frozen=True, slots=True)
class SourceReadiness:
    source_id: str
    ready: bool
    due: bool
    stale_running: bool
    reasons: tuple[str, ...]


def _policy(
    source_id: str,
    capability: SourceCapability,
    criticality: SourceCriticality,
    *,
    timeout_minutes: int,
    frequency_minutes: int,
    sla_minutes: int,
    observation_sla_minutes: int | None = None,
    production_schedulable: bool = False,
) -> SourceAutomationPolicy:
    return SourceAutomationPolicy(
        source_id=source_id,
        capability=capability,
        criticality=criticality,
        timeout_seconds=timeout_minutes * 60,
        frequency_seconds=frequency_minutes * 60,
        sla_seconds=sla_minutes * 60,
        observation_sla_seconds=(observation_sla_minutes or sla_minutes) * 60,
        production_schedulable=production_schedulable,
    )


# This matrix intentionally describes current operational capability, not
# historical registry presence. Soft-removed sources are excluded.
_POLICIES = (
    _policy(
        "eia_petroleum_api",
        SourceCapability.ADAPTER,
        SourceCriticality.CRITICAL,
        timeout_minutes=5,
        frequency_minutes=180,
        sla_minutes=180,
        observation_sla_minutes=14400,
        production_schedulable=True,
    ),
    _policy(
        "fred_macro_api",
        SourceCapability.ADAPTER,
        SourceCriticality.CRITICAL,
        timeout_minutes=5,
        frequency_minutes=1440,
        sla_minutes=1440,
        # Daily FRED series publish on business days. Seven calendar days
        # tolerates weekends/market holidays without masking a missed week.
        observation_sla_minutes=10080,
        production_schedulable=True,
    ),
    _policy(
        "cftc_cot_petroleum",
        SourceCapability.ADAPTER,
        SourceCriticality.IMPORTANT,
        timeout_minutes=10,
        # Publication is weekly; daily polling avoids a Monday success hiding
        # the next Friday release until the following Monday. SLA is unchanged.
        frequency_minutes=1440,
        sla_minutes=10080,
        observation_sla_minutes=20160,
        production_schedulable=True,
    ),
    _policy(
        "ine_sc_intraday",
        SourceCapability.DIRECT_DOWNLOAD,
        SourceCriticality.IMPORTANT,
        timeout_minutes=5,
        frequency_minutes=1440,
        sla_minutes=1440,
    ),
    _policy(
        "czce_pta_px",
        SourceCapability.ADAPTER,
        SourceCriticality.IMPORTANT,
        timeout_minutes=5,
        frequency_minutes=60,
        sla_minutes=1440,
        observation_sla_minutes=5760,
        production_schedulable=True,
    ),
    _policy(
        "akshare_prototype",
        SourceCapability.MANUAL,
        SourceCriticality.CONTEXT,
        timeout_minutes=5,
        frequency_minutes=1440,
        sla_minutes=1440,
    ),
    _policy(
        "cfets_cny_parity",
        SourceCapability.ADAPTER,
        SourceCriticality.IMPORTANT,
        timeout_minutes=5,
        frequency_minutes=90,
        sla_minutes=2880,
        observation_sla_minutes=5760,
        production_schedulable=True,
    ),
    _policy(
        "coalchina_cctd_bohai_rim_5500_daily_reference",
        SourceCapability.ADAPTER,
        SourceCriticality.IMPORTANT,
        timeout_minutes=5,
        frequency_minutes=1440,
        sla_minutes=1440,
    ),
    _policy(
        "sunsirs_mx_east_china_daily_assessment",
        SourceCapability.MANUAL,
        SourceCriticality.IMPORTANT,
        timeout_minutes=5,
        frequency_minutes=1440,
        sla_minutes=1440,
    ),
    _policy(
        "yahoo_futures_daily_proxy",
        SourceCapability.MANUAL,
        SourceCriticality.CONTEXT,
        timeout_minutes=5,
        frequency_minutes=1440,
        sla_minutes=1440,
    ),
    _policy(
        "opec_press",
        SourceCapability.ADAPTER,
        SourceCriticality.IMPORTANT,
        timeout_minutes=5,
        frequency_minutes=30,
        sla_minutes=30,
        production_schedulable=True,
    ),
    _policy(
        "ofac_sanctions",
        SourceCapability.ADAPTER,
        SourceCriticality.IMPORTANT,
        timeout_minutes=5,
        frequency_minutes=30,
        sla_minutes=30,
        production_schedulable=True,
    ),
    _policy(
        "gacc_trade_statistics",
        SourceCapability.ADAPTER,
        SourceCriticality.IMPORTANT,
        timeout_minutes=10,
        frequency_minutes=1440,
        sla_minutes=43200,
        observation_sla_minutes=89280,
        production_schedulable=True,
    ),
    _policy(
        "un_comtrade_api",
        SourceCapability.ADAPTER,
        SourceCriticality.CONTEXT,
        timeout_minutes=10,
        frequency_minutes=1440,
        sla_minutes=43200,
        observation_sla_minutes=89280,
        production_schedulable=True,
    ),
    _policy(
        "tnc_polyester_history",
        SourceCapability.ADAPTER,
        SourceCriticality.IMPORTANT,
        timeout_minutes=5,
        frequency_minutes=60,
        sla_minutes=2880,
        observation_sla_minutes=5760,
        production_schedulable=True,
    ),
    _policy(
        "internal_market_notes",
        SourceCapability.MANUAL,
        SourceCriticality.CONTEXT,
        timeout_minutes=5,
        frequency_minutes=15,
        sla_minutes=15,
    ),
)

SOURCE_AUTOMATION_POLICIES: Mapping[str, SourceAutomationPolicy] = MappingProxyType(
    {policy.source_id: policy for policy in _POLICIES}
)

_CRITICALITY_ORDER = {
    SourceCriticality.CRITICAL: 0,
    SourceCriticality.IMPORTANT: 1,
    SourceCriticality.CONTEXT: 2,
}
SUCCESSFUL_SOURCE_RUN_STATUSES = frozenset(
    {
        "ok",
        "success",
        "succeeded",
        "completed",
        "no_new_data",
        "no-new-data",
        "no_relevant_items",
        "up_to_date",
        "unchanged",
    }
)


def source_run_succeeded(status: object) -> bool:
    return str(status or "").strip().lower() in SUCCESSFUL_SOURCE_RUN_STATUSES


def get_source_policy(source_id: str) -> SourceAutomationPolicy:
    try:
        return SOURCE_AUTOMATION_POLICIES[source_id]
    except KeyError as exc:
        raise KeyError(f"unknown non-CCF source policy: {source_id}") from exc


def assess_readiness(
    source: str | SourceAutomationPolicy,
    state: Mapping[str, object] | None = None,
    *,
    now: datetime | None = None,
) -> SourceReadiness:
    policy = get_source_policy(source) if isinstance(source, str) else source
    values = state or {}
    current = _as_utc(now or datetime.now(UTC))
    reasons: list[str] = []

    adapter_available = policy.capability is SourceCapability.ADAPTER
    if "adapter" in values:
        adapter_available = bool(values["adapter"])
    if not adapter_available:
        reasons.append("adapter_unavailable")
    if not policy.production_schedulable:
        reasons.append("not_production_schedulable")

    status = str(values.get("last_run_status") or "").strip().lower()
    started_at = _parse_timestamp(values.get("last_run_started_at"))
    stale_running = status == "running" and (
        started_at is None or (current - started_at).total_seconds() > policy.timeout_seconds
    )
    if not status:
        reasons.append("last_run_status_missing")
    elif status == "running":
        reasons.append("stale_running" if stale_running else "run_in_progress")
    elif not source_run_succeeded(status):
        reasons.append("last_run_not_successful")

    last_success_at = _parse_timestamp(values.get("last_success_at"))
    if last_success_at is None:
        reasons.append("last_success_missing")
    elif _age_seconds(last_success_at, current) > policy.sla_seconds:
        reasons.append("last_success_outside_sla")

    latest_observed_at = _parse_timestamp(values.get("latest_observed_at"))
    if latest_observed_at is None:
        reasons.append("latest_observation_missing")
    elif latest_observed_at > current:
        reasons.append("latest_observation_from_future")
    elif (_age_seconds(latest_observed_at, current) > policy.observation_sla_seconds
          and not cfets_holiday_carry_reason(policy.source_id, latest_observed_at, current)):
        reasons.append("latest_observation_outside_sla")

    due = _is_due(policy, values, current=current, stale_running=stale_running)
    return SourceReadiness(
        source_id=policy.source_id,
        ready=not reasons,
        due=due,
        stale_running=stale_running,
        reasons=tuple(reasons),
    )


def is_source_ready(
    source: str | SourceAutomationPolicy,
    state: Mapping[str, object] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    return assess_readiness(source, state, now=now).ready


def select_due_sources(
    states: Mapping[str, Mapping[str, object]] | None = None,
    *,
    now: datetime | None = None,
    policies: Mapping[str, SourceAutomationPolicy] = SOURCE_AUTOMATION_POLICIES,
) -> list[SourceAutomationPolicy]:
    current = _as_utc(now or datetime.now(UTC))
    source_states = states or {}
    due: list[SourceAutomationPolicy] = []
    for policy in policies.values():
        state = source_states.get(policy.source_id, {})
        status = str(state.get("last_run_status") or "").strip().lower()
        started_at = _parse_timestamp(state.get("last_run_started_at"))
        stale_running = status == "running" and (
            started_at is None or (current - started_at).total_seconds() > policy.timeout_seconds
        )
        if _is_due(policy, state, current=current, stale_running=stale_running):
            due.append(policy)
    return sorted(due, key=lambda item: (_CRITICALITY_ORDER[item.criticality], item.source_id))


def select_due_source_ids(
    states: Mapping[str, Mapping[str, object]] | None = None,
    *,
    now: datetime | None = None,
) -> list[str]:
    return [policy.source_id for policy in select_due_sources(states, now=now)]


def _is_due(
    policy: SourceAutomationPolicy,
    state: Mapping[str, object],
    *,
    current: datetime,
    stale_running: bool,
) -> bool:
    if not policy.production_schedulable or policy.capability is not SourceCapability.ADAPTER:
        return False
    status = str(state.get("last_run_status") or "").strip().lower()
    if status == "running" and not stale_running:
        return False
    if stale_running or (status and not source_run_succeeded(status)):
        return True
    # Start-to-start scheduling: due time is anchored to when the last run
    # STARTED, not when it finished, so a slow run cannot push the next run
    # arbitrarily late (task rule B.7).
    started_at = _parse_timestamp(state.get("last_run_started_at"))
    if started_at is not None:
        return _age_seconds(started_at, current) >= policy.frequency_seconds
    last_success_at = _parse_timestamp(state.get("last_success_at"))
    return last_success_at is None or _age_seconds(last_success_at, current) >= policy.frequency_seconds


def _parse_timestamp(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value.strip().replace("Z", "+00:00")))
    except ValueError:
        return None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _age_seconds(value: datetime, current: datetime) -> float:
    return max(0.0, (current - value).total_seconds())

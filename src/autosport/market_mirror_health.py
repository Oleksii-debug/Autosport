from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .ingestion_health import SourceHealthStore, parse_source_timestamp
from .market_mirror import MirrorSnapshot
from .market_mirror_runtime import FocusedMirrorDependencyIndex


class ProviderDecisionEligibility(str, Enum):
    """Fail-closed provider eligibility at the live decision boundary."""

    ELIGIBLE = "eligible"
    UNKNOWN = "unknown"
    DEGRADED = "degraded"
    FAILED = "failed"
    STALE_HEALTH = "stale_health"
    FUTURE_HEALTH = "future_health"


@dataclass(frozen=True, slots=True)
class ProviderHealthDecision:
    source_id: str
    eligibility: ProviderDecisionEligibility
    source_status: str
    last_success_at: str | None

    @property
    def eligible(self) -> bool:
        return self.eligibility is ProviderDecisionEligibility.ELIGIBLE


class HealthGatedMirrorDecisionIndex:
    """Join canonical Market Mirror truth with causal durable provider health.

    ``MarketMirror`` remains the only live quote authority and ``SourceHealthStore``
    remains the provider-health authority. Every decision read starts from one coherent
    focused mirror revision, then reads the provider state that was durably known at the
    requested decision timestamp. Later health transitions therefore cannot rewrite an
    earlier replay boundary.

    The gate fails closed for unknown, degraded, failed, stale, or otherwise invalid
    provider health. It owns no duplicate mutable market/provider state.
    """

    def __init__(
        self,
        dependencies: FocusedMirrorDependencyIndex,
        health_store: SourceHealthStore,
        *,
        max_health_age: timedelta,
    ) -> None:
        if not isinstance(dependencies, FocusedMirrorDependencyIndex):
            raise TypeError("dependencies must be a FocusedMirrorDependencyIndex")
        if not isinstance(health_store, SourceHealthStore):
            raise TypeError("health_store must be a SourceHealthStore")
        if not isinstance(max_health_age, timedelta):
            raise TypeError("max_health_age must be a timedelta")
        if max_health_age < timedelta(0):
            raise ValueError("max_health_age must be non-negative")
        self._dependencies = dependencies
        self._health_store = health_store
        self._max_health_age = max_health_age

    @staticmethod
    def _as_of(value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("as_of must be a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _source_id(value: str) -> str:
        if not isinstance(value, str) or not value or value.strip() != value:
            raise ValueError("source_id must be a non-empty trimmed string")
        return value

    def provider_health(
        self,
        source_id: str,
        *,
        as_of: datetime,
    ) -> ProviderHealthDecision:
        """Return exact fail-closed eligibility using only health known by ``as_of``."""
        normalized_source = self._source_id(source_id)
        boundary = self._as_of(as_of)
        state = self._health_store.get_as_of(normalized_source, as_of=boundary)

        if state.status == "unknown":
            eligibility = ProviderDecisionEligibility.UNKNOWN
        elif state.status == "degraded":
            eligibility = ProviderDecisionEligibility.DEGRADED
        elif state.status == "failed":
            eligibility = ProviderDecisionEligibility.FAILED
        elif state.status != "healthy":
            eligibility = ProviderDecisionEligibility.UNKNOWN
        elif state.last_success_at is None:
            eligibility = ProviderDecisionEligibility.UNKNOWN
        else:
            last_success = parse_source_timestamp(state.last_success_at)
            if last_success > boundary:
                # Defensive invariant fence. get_as_of() should make this unreachable,
                # but retain an explicit fail-closed reason if storage semantics change.
                eligibility = ProviderDecisionEligibility.FUTURE_HEALTH
            elif boundary - last_success > self._max_health_age:
                eligibility = ProviderDecisionEligibility.STALE_HEALTH
            else:
                eligibility = ProviderDecisionEligibility.ELIGIBLE

        return ProviderHealthDecision(
            source_id=normalized_source,
            eligibility=eligibility,
            source_status=state.status,
            last_success_at=state.last_success_at,
        )

    def decision_view(
        self,
        input_id: str,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> MirrorSnapshot:
        """Return a focused decision view with causal provider-health fencing applied."""
        boundary = self._as_of(as_of)
        captured = self._dependencies.decision_view(
            input_id,
            as_of=boundary,
            max_age=max_age,
        )
        health_by_source = {
            source_id: self.provider_health(source_id, as_of=boundary)
            for source_id in {event.source_id for event in captured.events}
        }
        return MirrorSnapshot(
            revision=captured.revision,
            events=tuple(
                event
                for event in captured.events
                if health_by_source[event.source_id].eligible
            ),
        )

    def affected_inputs_for_source(
        self,
        source_id: str,
        *,
        as_of: datetime,
        max_age: timedelta,
    ) -> tuple[str, ...]:
        """Route a provider-health transition only to inputs that use that provider."""
        normalized_source = self._source_id(source_id)
        boundary = self._as_of(as_of)
        affected: list[str] = []
        for input_id in self._dependencies.input_ids:
            captured = self._dependencies.decision_view(
                input_id,
                as_of=boundary,
                max_age=max_age,
            )
            if any(event.source_id == normalized_source for event in captured.events):
                affected.append(input_id)
        return tuple(affected)

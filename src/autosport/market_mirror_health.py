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
    """Join canonical Market Mirror truth with durable provider-health eligibility.

    ``MarketMirror`` remains the only live quote authority and ``SourceHealthStore``
    remains the operational provider-health authority. This gate owns no duplicate
    mutable market/provider state: every decision read starts from one coherent focused
    mirror revision, then fails closed for provider sources whose durable health is not
    proven healthy and recent enough at the requested decision timestamp.

    This prevents a still-fresh quote from remaining decision-eligible after a provider
    poll fails, becomes degraded/quarantined, silently stops reporting, or publishes a
    health success timestamp from the future. Historical market reconstruction remains
    the responsibility of the mirror/store causal replay path; current source-health
    projection is deliberately not rewritten into historical evidence.
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
        """Return the exact fail-closed eligibility reason for one provider source."""
        normalized_source = self._source_id(source_id)
        boundary = self._as_of(as_of)
        state = self._health_store.get(normalized_source)

        if state.status == "unknown":
            eligibility = ProviderDecisionEligibility.UNKNOWN
        elif state.status == "degraded":
            eligibility = ProviderDecisionEligibility.DEGRADED
        elif state.status == "failed":
            eligibility = ProviderDecisionEligibility.FAILED
        elif state.status != "healthy":
            # SourceHealthState validation currently restricts statuses, but keep the
            # decision boundary fail closed if the durable schema expands later.
            eligibility = ProviderDecisionEligibility.UNKNOWN
        elif state.last_success_at is None:
            eligibility = ProviderDecisionEligibility.UNKNOWN
        else:
            last_success = parse_source_timestamp(state.last_success_at)
            if last_success > boundary:
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
        """Return a focused live decision view with provider-health fencing applied."""
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
        """Route a provider-health transition only to live inputs currently using it.

        Health transitions are not quote mutations, so they do not appear in the
        quote-key invalidation buffer. This method derives their affected subgraph from
        the canonical focused views without caching a second dependency or quote map.
        Consumers can call it when SourceHealthStore changes and then recompute only the
        returned decision inputs through :meth:`decision_view`.
        """
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

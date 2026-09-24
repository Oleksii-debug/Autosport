from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from .ingestion_health import SourceHealthState, SourceHealthStore, parse_source_timestamp
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
class ProviderHealthReplayBoundary:
    """Durable provider-health log horizon bound into one decision identity.

    ``transition_order`` is the source-local append order already persisted by
    ``SourceHealthStore`` schema v3. ``recorded_at`` is retained alongside it so a
    replay cannot accidentally reuse the same integer against a different/corrupt
    history. Order zero represents a store with no durable transition for the source.
    """

    source_id: str
    recorded_at: str | None
    transition_order: int


@dataclass(frozen=True, slots=True)
class ProviderHealthDecision:
    source_id: str
    eligibility: ProviderDecisionEligibility
    source_status: str
    last_success_at: str | None
    replay_boundary: ProviderHealthReplayBoundary

    @property
    def eligible(self) -> bool:
        return self.eligibility is ProviderDecisionEligibility.ELIGIBLE


@dataclass(frozen=True, slots=True)
class HealthGatedMirrorSnapshot(MirrorSnapshot):
    """Backward-compatible mirror view plus exact durable health replay horizons."""

    health_boundaries: tuple[ProviderHealthReplayBoundary, ...]


class HealthGatedMirrorDecisionIndex:
    """Join canonical Market Mirror truth with causal durable provider health.

    ``MarketMirror`` remains the only live quote authority and ``SourceHealthStore``
    remains the provider-health authority. Every decision read starts from one coherent
    focused mirror revision, then reads provider state through an explicit durable
    health-log horizon. A fresh read binds the latest horizon visible at ``as_of``; a
    replay can pass those exact horizons back, so neither a later equal-evidence-time
    transition nor a future transition can rewrite already-bound historical identity.

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

    @staticmethod
    def _boundary_order(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("health replay transition_order must be a non-negative integer")
        return value

    def _health_at_boundary(
        self,
        source_id: str,
        *,
        as_of: datetime,
        replay_boundary: ProviderHealthReplayBoundary | None,
        raw: dict | None = None,
    ) -> tuple[SourceHealthState, ProviderHealthReplayBoundary]:
        """Read one source at a durable log horizon without inventing evidence time.

        ``SourceHealthStore.get_as_of`` intentionally returns the freshest state for a
        datetime and therefore cannot distinguish two transitions sharing that datetime.
        This decision-layer reader binds the store's already-durable source-local order
        as the missing replay identity. A fresh read binds only transitions whose
        evidence time is visible at ``as_of``; future durable writes are not exposed in
        historical decision evidence. Explicit replay boundaries are also rejected when
        their durable evidence timestamp is later than ``as_of``. The reader performs no
        mutation and delegates persisted state decoding/validation to ``SourceHealthStore``.
        """
        # A caller that resolves more than one provider for the same economic
        # decision must pass one already-validated store image here. Reading the
        # store once per provider can otherwise manufacture a cross-source state
        # combination that never existed durably.
        if raw is None:
            raw = self._health_store._read()
        schema_version = raw["schema_version"]
        entries = raw.get("history", {}).get(source_id, ())

        if schema_version == 1:
            payload = raw["sources"].get(source_id)
            if payload is None:
                available_order = 0
                available_recorded_at = None
            else:
                state = self._health_store._state_from_payload(payload)
                available_recorded_at = self._health_store._transition_at(state)
                available_order = 1
        elif entries:
            available_order = (
                entries[-1]["transition_order"]
                if schema_version == 3
                else len(entries)
            )
            available_recorded_at = entries[-1]["recorded_at"]
        else:
            available_order = 0
            available_recorded_at = None

        if replay_boundary is None:
            horizon_order = 0
            horizon_recorded_at = None
            if schema_version == 1:
                if (
                    available_order == 1
                    and available_recorded_at is not None
                    and parse_source_timestamp(available_recorded_at) <= as_of
                ):
                    horizon_order = 1
                    horizon_recorded_at = available_recorded_at
            else:
                for index, entry in enumerate(entries, start=1):
                    if parse_source_timestamp(entry["recorded_at"]) > as_of:
                        break
                    horizon_order = (
                        entry["transition_order"]
                        if schema_version == 3
                        else index
                    )
                    horizon_recorded_at = entry["recorded_at"]
        else:
            if not isinstance(replay_boundary, ProviderHealthReplayBoundary):
                raise TypeError("replay_boundary must be a ProviderHealthReplayBoundary")
            if replay_boundary.source_id != source_id:
                raise ValueError("health replay boundary source_id mismatch")
            horizon_order = self._boundary_order(replay_boundary.transition_order)
            horizon_recorded_at = replay_boundary.recorded_at
            if horizon_order > available_order:
                raise ValueError("health replay boundary is newer than durable source history")
            if horizon_order == 0:
                if horizon_recorded_at is not None:
                    raise ValueError("zero health replay boundary cannot carry recorded_at")
            else:
                if schema_version == 1:
                    expected_recorded_at = available_recorded_at
                else:
                    expected = entries[horizon_order - 1]
                    expected_order = (
                        expected["transition_order"]
                        if schema_version == 3
                        else horizon_order
                    )
                    if expected_order != horizon_order:
                        raise ValueError("health replay boundary order is unavailable")
                    expected_recorded_at = expected["recorded_at"]
                if horizon_recorded_at != expected_recorded_at:
                    raise ValueError("health replay boundary does not match durable evidence")
                if parse_source_timestamp(horizon_recorded_at) > as_of:
                    raise ValueError("health replay boundary is later than as_of")

        bound = ProviderHealthReplayBoundary(
            source_id=source_id,
            recorded_at=horizon_recorded_at,
            transition_order=horizon_order,
        )
        if horizon_order == 0:
            return SourceHealthState(source_id=source_id), bound

        if schema_version == 1:
            payload = raw["sources"].get(source_id)
            if payload is None:
                return SourceHealthState(source_id=source_id), bound
            state = self._health_store._state_from_payload(payload)
            recorded_at = self._health_store._transition_at(state)
            if recorded_at is None or parse_source_timestamp(recorded_at) > as_of:
                return SourceHealthState(source_id=source_id), bound
            return state, bound

        selected: dict | None = None
        for index, entry in enumerate(entries, start=1):
            order = entry["transition_order"] if schema_version == 3 else index
            if order > horizon_order:
                break
            if parse_source_timestamp(entry["recorded_at"]) <= as_of:
                selected = entry["state"]
            else:
                break
        if selected is None:
            return SourceHealthState(source_id=source_id), bound
        return self._health_store._state_from_payload(
            selected,
            normalize_failed_flags=False,
        ), bound

    def _provider_health_from_raw(
        self,
        raw: dict,
        source_id: str,
        *,
        as_of: datetime,
        replay_boundary: ProviderHealthReplayBoundary | None = None,
    ) -> ProviderHealthDecision:
        """Resolve one provider from one already-validated durable store image."""
        normalized_source = self._source_id(source_id)
        boundary = self._as_of(as_of)
        state, bound = self._health_at_boundary(
            normalized_source,
            as_of=boundary,
            replay_boundary=replay_boundary,
            raw=raw,
        )

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
            replay_boundary=bound,
        )

    def provider_health_snapshot(
        self,
        source_ids: tuple[str, ...],
        *,
        as_of: datetime,
        replay_boundaries: Mapping[str, ProviderHealthReplayBoundary] | None = None,
    ) -> dict[str, ProviderHealthDecision]:
        """Resolve a complete provider set from exactly one durable health-store read.

        This is the causal-cut API for multi-provider economic decisions. The returned
        mapping cannot combine source A from one durable file image with source B from
        a later image, even if another process appends health transitions concurrently.
        """
        if not isinstance(source_ids, tuple):
            raise TypeError("source_ids must be a tuple")
        normalized_sources = tuple(self._source_id(source_id) for source_id in source_ids)
        if len(normalized_sources) != len(set(normalized_sources)):
            raise ValueError("source_ids must be unique")
        if tuple(sorted(normalized_sources)) != normalized_sources:
            raise ValueError("source_ids must be sorted")

        boundary = self._as_of(as_of)
        if replay_boundaries is None:
            supplied: Mapping[str, ProviderHealthReplayBoundary] = {}
        else:
            if not isinstance(replay_boundaries, Mapping):
                raise TypeError("replay_boundaries must be a mapping or null")
            if set(replay_boundaries) != set(normalized_sources):
                raise ValueError(
                    "health replay boundaries must exactly match provider snapshot sources"
                )
            supplied = replay_boundaries

        raw = self._health_store._read()
        return {
            source_id: self._provider_health_from_raw(
                raw,
                source_id,
                as_of=boundary,
                replay_boundary=(
                    supplied[source_id] if replay_boundaries is not None else None
                ),
            )
            for source_id in normalized_sources
        }

    def provider_health(
        self,
        source_id: str,
        *,
        as_of: datetime,
        replay_boundary: ProviderHealthReplayBoundary | None = None,
    ) -> ProviderHealthDecision:
        """Return fail-closed eligibility bound to an explicit durable health horizon."""
        normalized_source = self._source_id(source_id)
        raw = self._health_store._read()
        return self._provider_health_from_raw(
            raw,
            normalized_source,
            as_of=as_of,
            replay_boundary=replay_boundary,
        )

    def decision_view(
        self,
        input_id: str,
        *,
        as_of: datetime,
        max_age: timedelta,
        health_boundaries: Mapping[str, ProviderHealthReplayBoundary] | None = None,
    ) -> HealthGatedMirrorSnapshot:
        """Return a focused view and the durable health horizons used to filter it.

        Pass ``health_boundaries`` from an earlier returned snapshot to reproduce that
        health decision identity after additional equal-time transitions are appended.
        The returned object remains a ``MirrorSnapshot`` subtype for existing callers.
        """
        boundary = self._as_of(as_of)
        captured: MirrorSnapshot = self._dependencies.decision_view(
            input_id,
            as_of=boundary,
            max_age=max_age,
        )
        source_ids = tuple(sorted({event.source_id for event in captured.events}))
        supplied: Mapping[str, ProviderHealthReplayBoundary]
        if health_boundaries is None:
            supplied = {}
        else:
            if not isinstance(health_boundaries, Mapping):
                raise TypeError("health_boundaries must be a mapping or null")
            if set(health_boundaries) != set(source_ids):
                raise ValueError("health replay boundaries must match decision-view sources")
            supplied = health_boundaries

        decisions = self.provider_health_snapshot(
            source_ids,
            as_of=boundary,
            replay_boundaries=(
                supplied if health_boundaries is not None else None
            ),
        )
        return HealthGatedMirrorSnapshot(
            revision=captured.revision,
            events=tuple(
                event
                for event in captured.events
                if decisions[event.source_id].eligible
            ),
            health_boundaries=tuple(
                decisions[source_id].replay_boundary for source_id in source_ids
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

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from time import perf_counter
from typing import Callable

from .ingestion_health import (
    IngestionPolicy,
    SourceHealthState,
    SourceHealthStore,
    parse_source_timestamp,
)
from .market_bus import MarketEventBus, MarketEventDeliveryError
from .providers import CanonicalNormalizer, MarketProvider, ProviderBatch
from .source_continuity import (
    ProviderContinuityWitness,
    SourceContinuityStore,
)


Clock = Callable[[], str]
ContinuityWitnessResolver = Callable[
    [MarketProvider, ProviderBatch], ProviderContinuityWitness | None
]


@dataclass(frozen=True, slots=True)
class IngestionStats:
    source_id: str
    received: int
    accepted: int
    rejected: int
    elapsed_seconds: float
    cursor: str | None
    quality_flags: tuple[str, ...] = ()
    health_status: str = "unknown"
    continuity_status: str = "unknown"

    @property
    def accepted_per_second(self) -> float:
        elapsed = self.elapsed_seconds
        if (
            isinstance(elapsed, bool)
            or not isinstance(elapsed, (int, float))
            or not isfinite(elapsed)
            or elapsed <= 0
        ):
            raise ValueError("elapsed_seconds must be a finite positive number")
        rate = self.accepted / elapsed
        if not isfinite(rate):
            raise ValueError("accepted_per_second must be finite")
        return rate


@dataclass(frozen=True, slots=True)
class _SourceHealthSnapshot:
    source_id: str
    status: str
    poll_count: int
    total_received: int
    total_accepted: int
    total_rejected: int
    total_failures: int
    consecutive_failures: int
    last_success_at: str | None
    last_error_at: str | None
    last_error: str | None
    last_cursor: str | None
    latest_source_ts: str | None
    quality_flags: tuple[str, ...]

    @classmethod
    def from_state(cls, state: SourceHealthState) -> "_SourceHealthSnapshot":
        return cls(
            source_id=state.source_id,
            status=state.status,
            poll_count=state.poll_count,
            total_received=state.total_received,
            total_accepted=state.total_accepted,
            total_rejected=state.total_rejected,
            total_failures=state.total_failures,
            consecutive_failures=state.consecutive_failures,
            last_success_at=state.last_success_at,
            last_error_at=state.last_error_at,
            last_error=state.last_error,
            last_cursor=state.last_cursor,
            latest_source_ts=state.latest_source_ts,
            quality_flags=state.quality_flags,
        )

    def to_state(self) -> SourceHealthState:
        return SourceHealthState(
            source_id=self.source_id,
            status=self.status,
            poll_count=self.poll_count,
            total_received=self.total_received,
            total_accepted=self.total_accepted,
            total_rejected=self.total_rejected,
            total_failures=self.total_failures,
            consecutive_failures=self.consecutive_failures,
            last_success_at=self.last_success_at,
            last_error_at=self.last_error_at,
            last_error=self.last_error,
            last_cursor=self.last_cursor,
            latest_source_ts=self.latest_source_ts,
            quality_flags=self.quality_flags,
        )

    def after_success(
        self, outcome: "CommittedIngestionOutcome"
    ) -> "_SourceHealthSnapshot":
        latest_source_ts = self.latest_source_ts
        if outcome.latest_source_ts is not None:
            if latest_source_ts is None or (
                parse_source_timestamp(outcome.latest_source_ts)
                >= parse_source_timestamp(latest_source_ts)
            ):
                latest_source_ts = outcome.latest_source_ts
        quality_flags = tuple(sorted(outcome.quality_flags))
        return _SourceHealthSnapshot(
            source_id=self.source_id,
            status="degraded" if quality_flags else "healthy",
            poll_count=self.poll_count + 1,
            total_received=self.total_received + outcome.received,
            total_accepted=self.total_accepted + outcome.accepted,
            total_rejected=self.total_rejected + outcome.rejected,
            total_failures=self.total_failures,
            consecutive_failures=0,
            last_success_at=outcome.now,
            last_error_at=self.last_error_at,
            last_error=None,
            last_cursor=outcome.cursor,
            latest_source_ts=latest_source_ts,
            quality_flags=quality_flags,
        )


@dataclass(frozen=True, slots=True)
class CommittedIngestionOutcome:
    """Exact market-commit result whose source-health projection is still pending."""

    source_id: str
    now: str
    received: int
    accepted: int
    rejected: int
    elapsed_seconds: float
    cursor: str | None
    latest_source_ts: str | None
    quality_flags: tuple[str, ...]
    health_before: _SourceHealthSnapshot | None = None
    continuity_witness: ProviderContinuityWitness | None = None

    def _record_health_once(self, store: SourceHealthStore) -> SourceHealthState:
        return store.record_success(
            self.source_id,
            now=self.now,
            received=self.received,
            accepted=self.accepted,
            rejected=self.rejected,
            cursor=self.cursor,
            latest_source_ts=self.latest_source_ts,
            quality_flags=self.quality_flags,
        )

    def _record_continuity_once(self, store: SourceContinuityStore) -> str:
        state = store.record_success(
            self.source_id,
            now=self.now,
            cursor=self.cursor,
            witness=self.continuity_witness,
        )
        return state.status

    def record_health(self, store: SourceHealthStore) -> SourceHealthState:
        """Repair health only when compare-and-apply is atomic and provably safe."""
        if self.health_before is None:
            raise RuntimeError(
                "committed ingestion outcome lacks pre-health state for a safe retry"
            )
        expected_after = self.health_before.after_success(self)
        return store.record_success_if_current(
            self.health_before.to_state(),
            ambiguous_after=expected_after.to_state(),
            now=self.now,
            received=self.received,
            accepted=self.accepted,
            rejected=self.rejected,
            cursor=self.cursor,
            latest_source_ts=self.latest_source_ts,
            quality_flags=self.quality_flags,
        )

    def stats(
        self,
        *,
        health_status: str | None = None,
        continuity_status: str = "unknown",
    ) -> IngestionStats:
        if health_status is None:
            health_status = "degraded" if self.quality_flags else "healthy"
        return IngestionStats(
            self.source_id,
            self.received,
            self.accepted,
            self.rejected,
            self.elapsed_seconds,
            self.cursor,
            self.quality_flags,
            health_status,
            continuity_status,
        )


class CommittedIngestionHealthError(RuntimeError):
    """Market persistence succeeded, but durable source-health publication failed."""

    def __init__(
        self,
        outcome: CommittedIngestionOutcome,
        *,
        delivery_error: MarketEventDeliveryError | None = None,
    ) -> None:
        super().__init__(
            "market events were committed but source health persistence failed"
        )
        self.outcome = outcome
        self.delivery_error = delivery_error


class CommittedIngestionContinuityError(RuntimeError):
    """Market persistence succeeded, but durable source-continuity publication failed."""

    def __init__(
        self,
        outcome: CommittedIngestionOutcome,
        *,
        delivery_error: MarketEventDeliveryError | None = None,
    ) -> None:
        super().__init__(
            "market events were committed but source continuity persistence failed"
        )
        self.outcome = outcome
        self.delivery_error = delivery_error


class IngestionEngine:
    """Deterministic provider -> quality -> normalize -> transactional persistence -> subscriber pipeline."""

    def __init__(
        self,
        bus: MarketEventBus,
        normalizer: CanonicalNormalizer | None = None,
        *,
        policy: IngestionPolicy | None = None,
        health_store: SourceHealthStore | None = None,
        continuity_store: SourceContinuityStore | None = None,
        continuity_witness_resolver: ContinuityWitnessResolver | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.bus = bus
        self.normalizer = normalizer or CanonicalNormalizer()
        self.policy = policy or IngestionPolicy()
        self.health_store = health_store
        if continuity_store is not None and not isinstance(
            continuity_store, SourceContinuityStore
        ):
            raise TypeError("continuity_store must be SourceContinuityStore or null")
        if continuity_store is None and isinstance(health_store, SourceHealthStore):
            continuity_store = SourceContinuityStore(
                health_store.path.with_name("source_continuity.json")
            )
        if continuity_witness_resolver is not None and not callable(
            continuity_witness_resolver
        ):
            raise TypeError("continuity_witness_resolver must be callable or null")
        self.continuity_store = continuity_store
        self.continuity_witness_resolver = continuity_witness_resolver
        self.clock = clock or _utc_now_iso

    def _continuity_witness(
        self,
        provider: MarketProvider,
        batch: ProviderBatch,
    ) -> ProviderContinuityWitness | None:
        resolver = self.continuity_witness_resolver
        if resolver is None:
            return None
        witness = resolver(provider, batch)
        if witness is not None and type(witness) is not ProviderContinuityWitness:
            raise TypeError(
                "continuity_witness_resolver must return ProviderContinuityWitness or null"
            )
        return witness

    def _record_committed_projections(
        self,
        outcome: CommittedIngestionOutcome,
        *,
        delivery_error: MarketEventDeliveryError | None = None,
    ) -> tuple[str, str]:
        """Attempt each durable projection even when its sibling projection fails."""
        health_status = "degraded" if outcome.quality_flags else "healthy"
        continuity_status = "unknown"
        health_error: Exception | None = None
        continuity_error: Exception | None = None

        if self.health_store is not None:
            try:
                state = outcome._record_health_once(self.health_store)
            except Exception as exc:
                health_error = exc
            else:
                health_status = state.status

        if self.continuity_store is not None:
            try:
                continuity_status = outcome._record_continuity_once(
                    self.continuity_store
                )
            except Exception as exc:
                continuity_error = exc

        if health_error is not None:
            wrapped = CommittedIngestionHealthError(
                outcome,
                delivery_error=delivery_error,
            )
            if continuity_error is not None:
                wrapped.add_note(
                    "source continuity persistence also failed: "
                    f"{type(continuity_error).__name__}: {continuity_error}"
                )
            raise wrapped from health_error
        if continuity_error is not None:
            raise CommittedIngestionContinuityError(
                outcome,
                delivery_error=delivery_error,
            ) from continuity_error
        return health_status, continuity_status

    def poll_once(self, provider: MarketProvider, max_items: int = 1000) -> IngestionStats:
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
            raise ValueError("max_items must be a positive integer")
        if max_items > self.policy.max_batch_size:
            raise ValueError(
                f"requested batch {max_items} exceeds backpressure limit {self.policy.max_batch_size}"
            )
        started = perf_counter()

        # Bind provider identity exactly once before acquisition. Only provider
        # acquisition/validation failures change current provider health; continuity
        # witness resolution is a separate evidence domain and runs after this block.
        provider_source_id: str | None = None
        try:
            provider_source_id = provider.source_id
            batch = provider.read_batch(max_items=max_items)
            if batch.source_id != provider_source_id:
                raise ValueError("provider returned mismatched source_id")
            if len(batch.quotes) > max_items:
                raise ValueError(
                    f"provider returned {len(batch.quotes)} quotes above requested batch bound {max_items}"
                )
        except Exception as exc:
            failure_now = self.clock()
            health_error: Exception | None = None
            continuity_error: Exception | None = None
            if self.health_store is not None and provider_source_id is not None:
                try:
                    self.health_store.record_failure(
                        provider_source_id, now=failure_now, error=exc
                    )
                except Exception as projection_error:
                    health_error = projection_error
            if self.continuity_store is not None and provider_source_id is not None:
                try:
                    self.continuity_store.record_failure(
                        provider_source_id,
                        now=failure_now,
                    )
                except Exception as projection_error:
                    continuity_error = projection_error

            if health_error is not None:
                exc.add_note(
                    "source health failure persistence also failed: "
                    f"{type(health_error).__name__}: {health_error}"
                )
                if continuity_error is not None:
                    exc.add_note(
                        "source continuity failure persistence also failed: "
                        f"{type(continuity_error).__name__}: {continuity_error}"
                    )
                raise exc from health_error
            if continuity_error is not None:
                exc.add_note(
                    "source continuity failure persistence also failed: "
                    f"{type(continuity_error).__name__}: {continuity_error}"
                )
                raise exc from continuity_error
            raise

        # Continuity provenance is validated before normalization/persistence but does
        # not rewrite current provider-health truth when the resolver itself is invalid.
        continuity_witness = self._continuity_witness(provider, batch)

        # One post-acquisition evidence instant governs both quote-age truth and this
        # poll's health transition. Equal instants remain distinct via durable
        # transition_order; genuinely older direct evidence still fails closed.
        now = self.clock()

        health_before = None
        previous_source_ts = None
        if self.health_store is not None:
            health_before = _SourceHealthSnapshot.from_state(
                self.health_store.get(batch.source_id)
            )
            previous_source_ts = health_before.latest_source_ts

        flags = set(batch.quality_flags)
        normalized = []
        rejected = 0
        latest_source: datetime | None = None
        now_point = parse_source_timestamp(now)
        for quote in batch.quotes:
            source_point: datetime | None = None
            if quote.source_ts is not None:
                try:
                    source_point = parse_source_timestamp(quote.source_ts)
                except (AttributeError, TypeError, ValueError):
                    flags.add("INVALID_SOURCE_TIMESTAMP")
                    rejected += 1
                    continue
                age_seconds = (now_point - source_point).total_seconds()
                if age_seconds > self.policy.stale_after_seconds:
                    flags.add("STALE_SOURCE")
                if age_seconds < -self.policy.max_future_skew_seconds:
                    flags.add("FUTURE_CLOCK_SKEW")
            try:
                event = self.normalizer.normalize(batch.source_id, quote)
            except (TypeError, ValueError):
                flags.add("INVALID_QUOTE")
                rejected += 1
                continue
            normalized.append(event)
            if source_point is not None and (
                latest_source is None or source_point > latest_source
            ):
                latest_source = source_point

        latest_source_ts = latest_source.isoformat() if latest_source is not None else None
        if previous_source_ts is not None and latest_source is not None:
            if latest_source < parse_source_timestamp(previous_source_ts):
                flags.add("SOURCE_TIME_REGRESSION")

        # Persistence and subscriber delivery are local pipeline stages. A failure here
        # must still propagate, but it must not be attributed to provider health after
        # acquisition/validation/normalization already succeeded.
        ordered_flags = tuple(sorted(flags))
        try:
            accepted = self.bus.publish_many(normalized)
        except MarketEventDeliveryError as delivery_error:
            # MarketEventDeliveryError can only be raised after transactional
            # persistence succeeds. Preserve the exact storage-derived outcome in
            # provider progress before re-raising the consumer delivery failure.
            outcome = CommittedIngestionOutcome(
                source_id=batch.source_id,
                now=now,
                received=len(batch.quotes),
                accepted=delivery_error.accepted_count,
                rejected=rejected,
                elapsed_seconds=perf_counter() - started,
                cursor=batch.cursor,
                latest_source_ts=latest_source_ts,
                quality_flags=ordered_flags,
                health_before=health_before,
                continuity_witness=continuity_witness,
            )
            self._record_committed_projections(
                outcome,
                delivery_error=delivery_error,
            )
            raise

        outcome = CommittedIngestionOutcome(
            source_id=batch.source_id,
            now=now,
            received=len(batch.quotes),
            accepted=accepted,
            rejected=rejected,
            elapsed_seconds=perf_counter() - started,
            cursor=batch.cursor,
            latest_source_ts=latest_source_ts,
            quality_flags=ordered_flags,
            health_before=health_before,
            continuity_witness=continuity_witness,
        )
        health_status, continuity_status = self._record_committed_projections(outcome)
        return outcome.stats(
            health_status=health_status,
            continuity_status=continuity_status,
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

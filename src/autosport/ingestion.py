from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from math import isfinite
from time import perf_counter
from typing import Callable

from .domain import MarketEvent
from .ingestion_health import (
    IngestionPolicy,
    SourceHealthState,
    SourceHealthStore,
    parse_source_timestamp,
)
from .market_bus import MarketEventBus, MarketEventDeliveryError
from .providers import CanonicalNormalizer, MarketProvider, ProviderUnavailableError


Clock = Callable[[], str]


def _stamp_live_event(
    event: object,
    ingest_ts: str,
    *,
    _market_event_type: type[MarketEvent] = MarketEvent,
    _replace=replace,
) -> MarketEvent:
    if type(event) is not _market_event_type:
        raise TypeError("normalizer must return exact MarketEvent")
    stamped = _replace(event, ingest_ts=ingest_ts)
    if type(stamped) is not _market_event_type or stamped.ingest_ts != ingest_ts:
        raise TypeError("live ingestion timestamp stamping lost canonical authority")
    return stamped


def _publish_normalized_live_batch(
    bus: object,
    events: list[MarketEvent],
    *,
    _market_bus_type: type[MarketEventBus] = MarketEventBus,
    _live_publish=MarketEventBus._publish_many_live_ingestion,
    _generic_publish=MarketEventBus.publish_many,
) -> int:
    if type(bus) is _market_bus_type:
        return _live_publish(bus, events)
    if isinstance(bus, _market_bus_type):
        # Subclasses are non-canonical and stay provenance-neutral even if they
        # override publication methods.
        return _generic_publish(bus, events)
    return bus.publish_many(events)


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
    last_failure_kind: str | None
    consecutive_failure_kind_count: int

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
            last_failure_kind=state.last_failure_kind,
            consecutive_failure_kind_count=state.consecutive_failure_kind_count,
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
            last_failure_kind=self.last_failure_kind,
            consecutive_failure_kind_count=self.consecutive_failure_kind_count,
        )

    def after_success(
        self,
        outcome: "CommittedIngestionOutcome",
        *,
        _parse_timestamp=parse_source_timestamp,
    ) -> "_SourceHealthSnapshot":
        latest_source_ts = self.latest_source_ts
        if outcome.latest_source_ts is not None:
            if latest_source_ts is None or (
                _parse_timestamp(outcome.latest_source_ts)
                >= _parse_timestamp(latest_source_ts)
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
            last_failure_kind=None,
            consecutive_failure_kind_count=0,
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

    def stats(self, *, health_status: str | None = None) -> IngestionStats:
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


class IngestionEngine:
    """Deterministic provider -> quality -> normalize -> transactional persistence -> subscriber pipeline."""

    def __init__(
        self,
        bus: MarketEventBus,
        normalizer: CanonicalNormalizer | None = None,
        *,
        policy: IngestionPolicy | None = None,
        health_store: SourceHealthStore | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.bus = bus
        self.normalizer = normalizer or CanonicalNormalizer()
        self.policy = policy or IngestionPolicy()
        self.health_store = health_store
        self.clock = clock or _utc_now_iso

    def poll_once(
        self,
        provider: MarketProvider,
        max_items: int = 1000,
        *,
        _stamp=_stamp_live_event,
        _publish=_publish_normalized_live_batch,
        _parse_timestamp=parse_source_timestamp,
        _market_bus_type=MarketEventBus,
    ) -> IngestionStats:
        # Freeze one operator-owned dependency snapshot before provider-controlled
        # acquisition. Reentrant provider code must not be able to swap receive-time,
        # normalization, publication, policy or health authority mid-poll.
        bus = self.bus
        live_store = bus.store if type(bus) is _market_bus_type else None
        normalizer = self.normalizer
        policy = self.policy
        health_store = self.health_store
        poll_clock = self.clock

        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
            raise ValueError("max_items must be a positive integer")
        if max_items > policy.max_batch_size:
            raise ValueError(
                f"requested batch {max_items} exceeds backpressure limit {policy.max_batch_size}"
            )
        started = perf_counter()

        # Bind provider identity exactly once before acquisition. If acquisition or
        # provider-owned validation fails, failure-health evidence is sampled after the
        # failed I/O rather than carrying a stale pre-I/O timestamp.
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
            if health_store is not None and provider_source_id is not None:
                try:
                    health_store.record_failure(
                        provider_source_id,
                        now=poll_clock(),
                        error=exc,
                        failure_kind=(
                            "provider_unavailable"
                            if isinstance(exc, ProviderUnavailableError)
                            else "provider_or_validation"
                        ),
                    )
                except Exception as health_error:
                    exc.add_note(
                        "source health failure persistence also failed: "
                        f"{type(health_error).__name__}: {health_error}"
                    )
                    raise exc from health_error
            raise

        # One post-acquisition evidence instant governs both quote-age truth and this
        # poll's health transition. Equal instants remain distinct via durable
        # transition_order; genuinely older direct evidence still fails closed.
        now = poll_clock()

        health_before = None
        previous_source_ts = None
        if health_store is not None:
            health_before = _SourceHealthSnapshot.from_state(
                health_store.get(batch.source_id)
            )
            previous_source_ts = health_before.latest_source_ts

        flags = set(batch.quality_flags)
        normalized = []
        rejected = 0
        latest_source: datetime | None = None
        now_point = _parse_timestamp(now)
        for quote in batch.quotes:
            if quote.source_ts is not None:
                try:
                    _parse_timestamp(quote.source_ts)
                except (AttributeError, TypeError, ValueError):
                    flags.add("INVALID_SOURCE_TIMESTAMP")
                    rejected += 1
                    continue
            try:
                event = normalizer.normalize(batch.source_id, quote)
                # Provider/adaptor observation clocks remain evidence fields.
                # Durable ingestion time is owned by this post-acquisition
                # product clock, never by provider-controlled quote payloads.
                event = _stamp(event, now)
                if event.source_id != batch.source_id:
                    raise ValueError("normalizer returned mismatched source_id")
                source_point = (
                    _parse_timestamp(event.source_ts)
                    if event.source_ts is not None
                    else None
                )
                observed_point = _parse_timestamp(event.observed_ts)
            except (TypeError, ValueError):
                flags.add("INVALID_QUOTE")
                rejected += 1
                continue

            # Health must classify the exact temporal truth persisted in MarketEvent,
            # because MarketMirror.active_view later makes decisions from these same
            # normalized source/observation clocks.
            freshness_point = (
                source_point if source_point is not None else observed_point
            )
            age_seconds = (now_point - freshness_point).total_seconds()
            if age_seconds > policy.stale_after_seconds:
                flags.add("STALE_SOURCE")
            if age_seconds < -policy.max_future_skew_seconds:
                flags.add("FUTURE_CLOCK_SKEW")

            normalized.append(event)
            if source_point is not None and (
                latest_source is None or source_point > latest_source
            ):
                latest_source = source_point

        latest_source_ts = latest_source.isoformat() if latest_source is not None else None
        if previous_source_ts is not None and latest_source is not None:
            if latest_source < _parse_timestamp(previous_source_ts):
                flags.add("SOURCE_TIME_REGRESSION")

        # Persistence and subscriber delivery are local pipeline stages. A failure here
        # must still propagate, but it must not be attributed to provider health after
        # acquisition/validation/normalization already succeeded.
        ordered_flags = tuple(sorted(flags))
        try:
            if live_store is not None and bus.store is not live_store:
                raise RuntimeError(
                    "live ingestion store authority changed during provider I/O"
                )
            accepted = _publish(bus, normalized)
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
            )
            if health_store is not None:
                try:
                    outcome._record_health_once(health_store)
                except Exception as health_error:
                    raise CommittedIngestionHealthError(
                        outcome,
                        delivery_error=delivery_error,
                    ) from health_error
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
        )
        health_status = "degraded" if ordered_flags else "healthy"
        if health_store is not None:
            try:
                state = outcome._record_health_once(health_store)
            except Exception as health_error:
                raise CommittedIngestionHealthError(outcome) from health_error
            health_status = state.status
        return outcome.stats(health_status=health_status)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

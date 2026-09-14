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
from .providers import CanonicalNormalizer, MarketProvider


Clock = Callable[[], str]


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
        """Safely repair a failed post-commit health projection without double counting."""
        if self.health_before is None:
            raise RuntimeError(
                "committed ingestion outcome lacks pre-health state for a safe retry"
            )
        current = store.get(self.source_id)
        current_snapshot = _SourceHealthSnapshot.from_state(current)
        expected = self.health_before.after_success(self)
        if current_snapshot == expected:
            return current
        if current_snapshot != self.health_before:
            raise RuntimeError(
                "source health changed since the committed ingestion outcome; "
                "refusing ambiguous retry"
            )
        return self._record_health_once(store)

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

    def poll_once(self, provider: MarketProvider, max_items: int = 1000) -> IngestionStats:
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
            raise ValueError("max_items must be a positive integer")
        if max_items > self.policy.max_batch_size:
            raise ValueError(
                f"requested batch {max_items} exceeds backpressure limit {self.policy.max_batch_size}"
            )
        started = perf_counter()
        now = self.clock()

        # Only provider acquisition and provider-owned batch-contract validation may
        # transition provider health to failed. Local health projection, clock,
        # normalization, persistence and subscriber failures are separate pipeline
        # failures and must never be misattributed to the external source.
        try:
            batch = provider.read_batch(max_items=max_items)
            if batch.source_id != provider.source_id:
                raise ValueError("provider returned mismatched source_id")
            if len(batch.quotes) > max_items:
                raise ValueError(
                    f"provider returned {len(batch.quotes)} quotes above requested batch bound {max_items}"
                )
        except Exception as exc:
            if self.health_store is not None:
                try:
                    self.health_store.record_failure(provider.source_id, now=now, error=exc)
                except Exception as health_error:
                    exc.add_note(
                        "source health failure persistence also failed: "
                        f"{type(health_error).__name__}: {health_error}"
                    )
                    raise exc from health_error
            raise

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
            )
            if self.health_store is not None:
                try:
                    outcome._record_health_once(self.health_store)
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
        if self.health_store is not None:
            try:
                state = outcome._record_health_once(self.health_store)
            except Exception as health_error:
                raise CommittedIngestionHealthError(outcome) from health_error
            health_status = state.status
        return outcome.stats(health_status=health_status)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

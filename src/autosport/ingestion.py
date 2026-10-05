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
from .providers import CanonicalNormalizer, MarketProvider, ProviderBatch, ProviderUnavailableError
from .storage import _timezone_aware_instant


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

    def __post_init__(self) -> None:
        if (
            type(self.source_id) is not str
            or not self.source_id
            or self.source_id.strip() != self.source_id
            or "|" in self.source_id
        ):
            raise ValueError("source_id must be a canonical exact string")
        for name in ("received", "accepted", "rejected"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.accepted + self.rejected > self.received:
            raise ValueError("accepted and rejected cannot exceed received")
        if (
            type(self.elapsed_seconds) not in {int, float}
            or not isfinite(self.elapsed_seconds)
            or self.elapsed_seconds < 0
        ):
            raise ValueError("elapsed_seconds must be a finite non-negative number")
        if self.cursor is not None and type(self.cursor) is not str:
            raise TypeError("cursor must be an exact string or None")
        if type(self.quality_flags) is not tuple:
            raise TypeError("quality_flags must be an exact tuple")
        seen_flags: set[str] = set()
        for flag in self.quality_flags:
            if type(flag) is not str or not flag or flag.strip() != flag:
                raise ValueError(
                    "quality_flags must contain exact non-empty trimmed strings"
                )
            if flag in seen_flags:
                raise ValueError("quality_flags must not contain duplicates")
            seen_flags.add(flag)
        if (
            type(self.health_status) is not str
            or self.health_status not in {"unknown", "healthy", "degraded", "failed"}
        ):
            raise ValueError("health_status must be a canonical health state")

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
        self, outcome: "CommittedIngestionOutcome"
    ) -> "_SourceHealthSnapshot":
        latest_source_ts = self.latest_source_ts
        effective_flags = set(outcome.quality_flags)
        if outcome.latest_source_ts is not None:
            if latest_source_ts is not None and (
                parse_source_timestamp(outcome.latest_source_ts)
                < parse_source_timestamp(latest_source_ts)
            ):
                effective_flags.add("SOURCE_TIME_REGRESSION")
            if latest_source_ts is None or (
                parse_source_timestamp(outcome.latest_source_ts)
                >= parse_source_timestamp(latest_source_ts)
            ):
                latest_source_ts = outcome.latest_source_ts
        quality_flags = tuple(sorted(effective_flags))
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
        if type(bus) is not MarketEventBus:
            raise TypeError("bus must be an exact MarketEventBus")
        if normalizer is not None and type(normalizer) is not CanonicalNormalizer:
            raise TypeError("normalizer must be an exact CanonicalNormalizer or null")
        if policy is not None and type(policy) is not IngestionPolicy:
            raise TypeError("policy must be an exact IngestionPolicy or null")
        if health_store is not None and type(health_store) is not SourceHealthStore:
            raise TypeError("health_store must be an exact SourceHealthStore or null")
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable or null")
        self.bus = bus
        self.normalizer = normalizer if normalizer is not None else CanonicalNormalizer()
        self.policy = policy if policy is not None else IngestionPolicy()
        self.health_store = health_store
        self.clock = clock if clock is not None else _utc_now_iso

    def poll_once(self, provider: MarketProvider, max_items: int = 1000) -> IngestionStats:
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be a positive integer")
        if max_items > self.policy.max_batch_size:
            raise ValueError(
                f"requested batch {max_items} exceeds backpressure limit {self.policy.max_batch_size}"
            )
        started = perf_counter()

        # Bind provider identity exactly once before acquisition. If acquisition or
        # provider-owned validation fails, failure-health evidence is sampled after the
        # failed I/O rather than carrying a stale pre-I/O timestamp.
        provider_source_id: str | None = None
        try:
            provider_source_id = provider.source_id
            if type(provider_source_id) is not str:
                raise TypeError("provider source_id must be an exact string")
            if (
                not provider_source_id
                or provider_source_id.strip() != provider_source_id
                or "|" in provider_source_id
            ):
                raise ValueError("provider source_id must be canonical")
            batch = provider.read_batch(max_items=max_items)
            post_read_source_id = provider.source_id
            if type(post_read_source_id) is not str:
                raise TypeError("provider source_id must remain an exact string")
            if post_read_source_id != provider_source_id:
                raise ValueError("provider source_id changed during batch acquisition")
            if type(batch) is not ProviderBatch:
                raise TypeError("provider must return an exact ProviderBatch")
            if type(batch.source_id) is not str:
                raise TypeError("provider batch source_id must be an exact string")
            if batch.source_id != provider_source_id:
                raise ValueError("provider returned mismatched source_id")
            if len(batch.quotes) > max_items:
                raise ValueError(
                    f"provider returned {len(batch.quotes)} quotes above requested batch bound {max_items}"
                )
        except Exception as exc:
            if self.health_store is not None and provider_source_id is not None:
                try:
                    self.health_store.record_failure(
                        provider_source_id,
                        now=self.clock(),
                        error=exc,
                        failure_kind=(
                            "provider_unavailable"
                            if isinstance(exc, ProviderUnavailableError)
                            else "provider_or_validation"
                        ),
                    )
                except BaseException as health_error:
                    try:
                        try:
                            health_detail = str(health_error)
                        except BaseException:
                            health_detail = "<unprintable exception>"
                        exc.add_note(
                            "source health failure persistence also failed: "
                            f"{type(health_error).__name__}: {health_detail}"
                        )
                    except BaseException:
                        pass
                    raise exc from health_error
            raise

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
            try:
                observed_point = _timezone_aware_instant(
                    quote.observed_ts,
                    "observed_ts",
                ).astimezone(timezone.utc)
            except (AttributeError, TypeError, ValueError):
                flags.add("INVALID_QUOTE")
                rejected += 1
                continue
            if observed_point > now_point:
                # observed_ts is local receipt evidence, not provider clock truth.
                # A receipt claimed after this already-completed acquisition instant
                # cannot be causally true for the current poll.
                flags.add("FUTURE_OBSERVATION_TIMESTAMP")
                rejected += 1
                continue

            source_point: datetime | None = None
            freshness_point = observed_point
            if quote.source_ts is not None:
                try:
                    source_point = _timezone_aware_instant(
                        quote.source_ts,
                        "source_ts",
                    ).astimezone(timezone.utc)
                except (AttributeError, TypeError, ValueError):
                    flags.add("INVALID_SOURCE_TIMESTAMP")
                    rejected += 1
                    continue
                freshness_point = source_point

            age_seconds = (now_point - freshness_point).total_seconds()
            if age_seconds > self.policy.stale_after_seconds:
                flags.add("STALE_SOURCE")
            if source_point is not None and (
                age_seconds < -self.policy.max_future_skew_seconds
            ):
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
                except BaseException as health_error:
                    # Market persistence already committed. Preserve that typed
                    # disposition even if health publication is interrupted by a
                    # BaseException so live retry logic cannot replay durable events.
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
        final_quality_flags = ordered_flags
        if self.health_store is not None:
            try:
                state = outcome._record_health_once(self.health_store)
            except BaseException as health_error:
                # The market transaction is already durable at this point. Always
                # surface the committed-outcome wrapper, including interrupts.
                raise CommittedIngestionHealthError(outcome) from health_error
            health_status = state.status
            final_quality_flags = state.quality_flags
        return IngestionStats(
            outcome.source_id,
            outcome.received,
            outcome.accepted,
            outcome.rejected,
            outcome.elapsed_seconds,
            outcome.cursor,
            final_quality_flags,
            health_status,
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

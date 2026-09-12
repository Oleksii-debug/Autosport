from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
from typing import Callable

from .ingestion_health import IngestionPolicy, SourceHealthStore, parse_source_timestamp
from .market_bus import MarketEventBus
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
        return self.accepted / self.elapsed_seconds if self.elapsed_seconds > 0 else float("inf")


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
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        if max_items > self.policy.max_batch_size:
            raise ValueError(
                f"requested batch {max_items} exceeds backpressure limit {self.policy.max_batch_size}"
            )
        started = perf_counter()
        now = self.clock()
        try:
            batch = provider.read_batch(max_items=max_items)
            if batch.source_id != provider.source_id:
                raise ValueError("provider returned mismatched source_id")
            if len(batch.quotes) > max_items:
                raise ValueError(
                    f"provider returned {len(batch.quotes)} quotes above requested batch bound {max_items}"
                )
            flags = set(batch.quality_flags)
            previous_source_ts = None
            if self.health_store is not None:
                previous_source_ts = self.health_store.get(batch.source_id).latest_source_ts

            normalized = []
            rejected = 0
            latest_source: datetime | None = None
            now_point = parse_source_timestamp(now)
            for quote in batch.quotes:
                if quote.source_ts is not None:
                    try:
                        source_point = parse_source_timestamp(quote.source_ts)
                    except ValueError:
                        flags.add("INVALID_SOURCE_TIMESTAMP")
                        rejected += 1
                        continue
                    if latest_source is None or source_point > latest_source:
                        latest_source = source_point
                    age_seconds = (now_point - source_point).total_seconds()
                    if age_seconds > self.policy.stale_after_seconds:
                        flags.add("STALE_SOURCE")
                    if age_seconds < -self.policy.max_future_skew_seconds:
                        flags.add("FUTURE_CLOCK_SKEW")
                try:
                    normalized.append(self.normalizer.normalize(batch.source_id, quote))
                except (TypeError, ValueError):
                    rejected += 1

            latest_source_ts = latest_source.isoformat() if latest_source is not None else None
            if previous_source_ts is not None and latest_source is not None:
                if latest_source < parse_source_timestamp(previous_source_ts):
                    flags.add("SOURCE_TIME_REGRESSION")

            accepted = self.bus.publish_many(normalized)
            elapsed = perf_counter() - started
            ordered_flags = tuple(sorted(flags))
            health_status = "degraded" if ordered_flags else "healthy"
            if self.health_store is not None:
                state = self.health_store.record_success(
                    batch.source_id,
                    now=now,
                    received=len(batch.quotes),
                    accepted=accepted,
                    rejected=rejected,
                    cursor=batch.cursor,
                    latest_source_ts=latest_source_ts,
                    quality_flags=ordered_flags,
                )
                health_status = state.status
            return IngestionStats(
                batch.source_id,
                len(batch.quotes),
                accepted,
                rejected,
                elapsed,
                batch.cursor,
                ordered_flags,
                health_status,
            )
        except Exception as exc:
            if self.health_store is not None:
                self.health_store.record_failure(provider.source_id, now=now, error=exc)
            raise


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

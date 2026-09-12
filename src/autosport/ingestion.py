from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter

from .market_bus import MarketEventBus
from .providers import CanonicalNormalizer, MarketProvider


@dataclass(frozen=True, slots=True)
class IngestionStats:
    source_id: str
    received: int
    accepted: int
    rejected: int
    elapsed_seconds: float
    cursor: str | None

    @property
    def accepted_per_second(self) -> float:
        return self.accepted / self.elapsed_seconds if self.elapsed_seconds > 0 else float("inf")


class IngestionEngine:
    """Fast deterministic provider -> normalize -> transactional batch persistence -> subscriber pipeline."""

    def __init__(self, bus: MarketEventBus, normalizer: CanonicalNormalizer | None = None) -> None:
        self.bus = bus
        self.normalizer = normalizer or CanonicalNormalizer()

    def poll_once(self, provider: MarketProvider, max_items: int = 1000) -> IngestionStats:
        started = perf_counter()
        batch = provider.read_batch(max_items=max_items)
        if batch.source_id != provider.source_id:
            raise ValueError("provider returned mismatched source_id")
        normalized = []
        rejected = 0
        for quote in batch.quotes:
            try:
                normalized.append(self.normalizer.normalize(batch.source_id, quote))
            except (TypeError, ValueError):
                rejected += 1
        accepted = self.bus.publish_many(normalized)
        elapsed = perf_counter() - started
        return IngestionStats(batch.source_id, len(batch.quotes), accepted, rejected, elapsed, batch.cursor)

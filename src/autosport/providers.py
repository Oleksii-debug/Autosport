from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from .domain import MarketEvent, MarketType


@dataclass(frozen=True, slots=True)
class ProviderQuote:
    provider_event_id: str
    provider_market_id: str
    provider_selection_id: str
    decimal_odds: Decimal
    observed_ts: str
    sequence: int
    market_type: MarketType = MarketType.OTHER
    status: str = "open"
    source_ts: str | None = None
    score_state: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderBatch:
    source_id: str
    quotes: tuple[ProviderQuote, ...]
    cursor: str | None = None
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.source_id:
            raise ValueError("source_id required")
        if len(set(self.quality_flags)) != len(self.quality_flags):
            raise ValueError("duplicate provider batch quality flag")


class MarketProvider(Protocol):
    source_id: str

    def read_batch(self, max_items: int = 1000) -> ProviderBatch: ...


class CanonicalNormalizer:
    """Provider IDs are scoped under source_id so provider-specific identifiers never collide locally."""

    def normalize(self, source_id: str, quote: ProviderQuote) -> MarketEvent:
        if quote.decimal_odds <= 1:
            raise ValueError("decimal odds must be greater than 1")
        prefix = source_id.replace("|", "_")
        return MarketEvent(
            event_id=f"{prefix}:{quote.provider_event_id}",
            market_id=f"{prefix}:{quote.provider_market_id}",
            selection_id=f"{prefix}:{quote.provider_selection_id}",
            decimal_odds=quote.decimal_odds,
            observed_ts=quote.observed_ts,
            source_id=source_id,
            sequence=quote.sequence,
            market_type=quote.market_type,
            status=quote.status,
            source_ts=quote.source_ts,
            score_state=quote.score_state,
            metadata=dict(quote.metadata),
        )


class InMemoryProvider:
    """Deterministic provider used for fixtures, replay bridges and provider-contract tests."""

    def __init__(
        self,
        source_id: str,
        quotes: list[ProviderQuote],
        quality_flags: tuple[str, ...] = (),
    ) -> None:
        self.source_id = source_id
        self._quotes = list(quotes)
        self._offset = 0
        self.quality_flags = quality_flags

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if max_items <= 0:
            raise ValueError("max_items must be positive")
        items = self._quotes[self._offset : self._offset + max_items]
        self._offset += len(items)
        cursor = str(self._offset)
        return ProviderBatch(self.source_id, tuple(items), cursor, self.quality_flags)

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from .domain import MarketEvent, MarketType


_MAX_PROVIDER_METADATA_NESTING = 64
_SQLITE_SEQUENCE_MIN = -(1 << 63)
_SQLITE_SEQUENCE_MAX = (1 << 63) - 1


def _validate_source_id(source_id: object) -> str:
    if not isinstance(source_id, str):
        raise TypeError("source_id must be str")
    if not source_id or source_id != source_id.strip():
        raise ValueError("source_id must be non-empty and trimmed")
    if "|" in source_id:
        raise ValueError("source_id must not contain reserved identity delimiter '|'")
    return source_id


def _validate_provider_component(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty and trimmed")
    if "|" in value:
        raise ValueError(f"{name} must not contain reserved identity delimiter '|'")
    return value


def _validate_provider_event_id(value: object) -> str:
    event_id = _validate_provider_component(value, "provider_event_id")
    if ":" in event_id:
        raise ValueError("provider_event_id must not contain reserved source-scope delimiter ':'")
    return event_id


def _validate_sequence(value: object) -> int:
    """Keep provider sequence identity stable across JSON/SQLite round trips."""

    if type(value) is not int:
        raise TypeError("sequence must be a non-boolean int")
    if value < _SQLITE_SEQUENCE_MIN or value > _SQLITE_SEQUENCE_MAX:
        raise ValueError("sequence must fit signed 64-bit SQLite INTEGER")
    return value


def _validate_provider_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty and trimmed")
    return value


def _validate_provider_timestamp(value: object, name: str) -> str:
    timestamp = _validate_provider_text(value, name)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware ISO-8601")
    return timestamp


def _validate_json_value(value: object, field: str) -> None:
    """Require bounded durable JSON without type drift, cycles, or non-finite numbers."""

    stack: list[tuple[object, str, int, bool]] = [(value, field, 0, False)]
    active_containers: set[int] = set()

    while stack:
        current, path, depth, exiting = stack.pop()
        if exiting:
            active_containers.remove(id(current))
            continue

        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise ValueError(f"{path} contains non-finite JSON number")
            continue
        if isinstance(current, (list, dict)):
            if depth > _MAX_PROVIDER_METADATA_NESTING:
                raise ValueError(
                    f"{field} exceeds maximum JSON nesting depth "
                    f"{_MAX_PROVIDER_METADATA_NESTING}"
                )
            container_id = id(current)
            if container_id in active_containers:
                raise ValueError(f"{path} contains cyclic JSON container")
            active_containers.add(container_id)
            stack.append((current, path, depth, True))

            if isinstance(current, list):
                for index, item in enumerate(current):
                    stack.append((item, f"{path}[{index}]", depth + 1, False))
            else:
                for key, item in current.items():
                    if not isinstance(key, str):
                        raise TypeError(f"{path} contains non-string JSON object key")
                    stack.append((item, f"{path}.{key}", depth + 1, False))
            continue
        raise TypeError(
            f"{path} contains non-canonical JSON value type {type(current).__name__}"
        )


def _scoped_identity(source_id: str, provider_component: str) -> str:
    """Preserve the deployed canonical ``source_id:provider_id`` representation.

    Colon is intentionally data inside existing source IDs and market/selection IDs
    (the live Parlay adapter emits market IDs such as ``book:h2h``). Provider event
    IDs are colon-free, which makes the source-scoped event identity unambiguous and
    therefore prevents cross-source quote-key aliasing without re-keying deployed
    market/selection identities. The top-level quote-key delimiter ``|`` remains
    forbidden in every identity component.
    """

    return f"{source_id}:{provider_component}"


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

    def __post_init__(self) -> None:
        _validate_provider_event_id(self.provider_event_id)
        _validate_provider_component(self.provider_market_id, "provider_market_id")
        _validate_provider_component(self.provider_selection_id, "provider_selection_id")
        _validate_sequence(self.sequence)


@dataclass(frozen=True, slots=True)
class ProviderBatch:
    source_id: str
    quotes: tuple[ProviderQuote, ...]
    cursor: str | None = None
    quality_flags: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _validate_source_id(self.source_id)
        if type(self.quotes) is not tuple:
            raise TypeError("provider batch quotes must be a tuple of ProviderQuote values")
        for quote in self.quotes:
            if type(quote) is not ProviderQuote:
                raise TypeError("provider batch quote must be ProviderQuote")
        if self.cursor is not None and not isinstance(self.cursor, str):
            raise TypeError("provider batch cursor must be str or None")
        if type(self.quality_flags) is not tuple:
            raise TypeError("provider batch quality_flags must be a tuple of strings")
        for flag in self.quality_flags:
            if type(flag) is not str:
                raise TypeError("provider batch quality flag must be str")
            if not flag or flag != flag.strip():
                raise ValueError("provider batch quality flag must be non-empty and trimmed")
        if len(set(self.quality_flags)) != len(self.quality_flags):
            raise ValueError("duplicate provider batch quality flag")


class MarketProvider(Protocol):
    source_id: str

    def read_batch(self, max_items: int = 1000) -> ProviderBatch: ...


class CanonicalNormalizer:
    """Provider IDs are scoped under a validated source without changing deployed IDs."""

    def normalize(self, source_id: str, quote: ProviderQuote) -> MarketEvent:
        source_id = _validate_source_id(source_id)
        if not isinstance(quote.decimal_odds, Decimal):
            raise TypeError("decimal odds must be Decimal")
        if not quote.decimal_odds.is_finite():
            raise ValueError("decimal odds must be finite")
        if quote.decimal_odds <= 1:
            raise ValueError("decimal odds must be greater than 1")
        observed_ts = _validate_provider_timestamp(quote.observed_ts, "observed_ts")
        if not isinstance(quote.market_type, MarketType):
            raise TypeError("market_type must be MarketType")
        status = _validate_provider_text(quote.status, "status")
        source_ts = quote.source_ts
        if source_ts is not None:
            source_ts = _validate_provider_timestamp(source_ts, "source_ts")
        score_state = quote.score_state
        if score_state is not None:
            score_state = _validate_provider_text(score_state, "score_state")
        if not isinstance(quote.metadata, dict):
            raise TypeError("metadata must be dict")
        _validate_json_value(quote.metadata, "metadata")
        return MarketEvent(
            event_id=_scoped_identity(source_id, quote.provider_event_id),
            market_id=_scoped_identity(source_id, quote.provider_market_id),
            selection_id=_scoped_identity(source_id, quote.provider_selection_id),
            decimal_odds=quote.decimal_odds,
            observed_ts=observed_ts,
            source_id=source_id,
            sequence=quote.sequence,
            market_type=quote.market_type,
            status=status,
            source_ts=source_ts,
            ingest_ts=observed_ts,
            score_state=score_state,
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
        self.source_id = _validate_source_id(source_id)
        self._quotes = list(quotes)
        self._offset = 0
        self.quality_flags = quality_flags

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if isinstance(max_items, bool) or not isinstance(max_items, int) or max_items <= 0:
            raise ValueError("max_items must be a positive non-boolean integer")
        items = self._quotes[self._offset : self._offset + max_items]
        self._offset += len(items)
        cursor = str(self._offset)
        return ProviderBatch(self.source_id, tuple(items), cursor, self.quality_flags)

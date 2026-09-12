from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class EventKind(StrEnum):
    ODDS = "odds"
    SCORE = "score"
    STATUS = "status"
    RESULT = "result"


@dataclass(frozen=True, slots=True)
class MarketKey:
    provider: str
    match_id: str
    market_id: str
    selection_id: str

    def __post_init__(self) -> None:
        for name, value in (
            ("provider", self.provider),
            ("match_id", self.match_id),
            ("market_id", self.market_id),
            ("selection_id", self.selection_id),
        ):
            if not value or not value.strip():
                raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_id: str
    sequence: int
    observed_at: datetime
    key: MarketKey
    kind: EventKind
    decimal_odds: Decimal | None = None
    value: str | None = None
    source_timestamp: datetime | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id or not self.event_id.strip():
            raise ValueError("event_id must be non-empty")
        if self.sequence < 0:
            raise ValueError("sequence must be >= 0")
        _require_aware_utc(self.observed_at, "observed_at")
        if self.source_timestamp is not None:
            _require_aware_utc(self.source_timestamp, "source_timestamp")
        if self.kind is EventKind.ODDS:
            if self.decimal_odds is None:
                raise ValueError("odds event requires decimal_odds")
            if self.decimal_odds <= Decimal("1"):
                raise ValueError("decimal_odds must be > 1")
        elif self.decimal_odds is not None:
            raise ValueError("decimal_odds is only valid for odds events")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def sort_key(self) -> tuple[datetime, int, str]:
        return (self.observed_at, self.sequence, self.event_id)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_aware_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be normalized to UTC")

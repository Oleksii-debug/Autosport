from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


class MarketType(str, Enum):
    WINNER = "winner"
    TOTAL = "total"
    HANDICAP = "handicap"
    OTHER = "other"


class TicketStatus(str, Enum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"
    VOID = "void"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_canonical_market_identity(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{key} must be a canonical non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class MarketEvent:
    event_id: str
    market_id: str
    selection_id: str
    decimal_odds: Decimal
    observed_ts: str
    source_id: str
    sequence: int
    market_type: MarketType = MarketType.OTHER
    status: str = "open"
    source_ts: str | None = None
    ingest_ts: str = field(default_factory=utc_now_iso)
    score_state: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def quote_key(self) -> str:
        return f"{self.event_id}|{self.market_id}|{self.selection_id}"

    @property
    def dedupe_key(self) -> str:
        return f"{self.source_id}|{self.event_id}|{self.market_id}|{self.selection_id}|{self.sequence}"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MarketEvent":
        event_id = _require_canonical_market_identity(raw, "event_id")
        market_id = _require_canonical_market_identity(raw, "market_id")
        selection_id = _require_canonical_market_identity(raw, "selection_id")
        source_id_raw = raw.get("source_id", "fixture")
        if not isinstance(source_id_raw, str) or not source_id_raw or source_id_raw != source_id_raw.strip():
            raise ValueError("source_id must be a canonical non-empty string")
        decimal_odds = Decimal(str(raw["decimal_odds"]))
        if not decimal_odds.is_finite():
            raise ValueError("decimal_odds must be finite")
        if decimal_odds <= 1:
            raise ValueError("decimal_odds must be greater than 1")
        return cls(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=decimal_odds,
            observed_ts=str(raw["observed_ts"]),
            source_id=source_id_raw,
            sequence=int(raw["sequence"]),
            market_type=MarketType(str(raw.get("market_type", "other"))),
            status=str(raw.get("status", "open")),
            source_ts=raw.get("source_ts"),
            ingest_ts=str(raw.get("ingest_ts", utc_now_iso())),
            score_state=raw.get("score_state"),
            metadata=dict(raw.get("metadata", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "decimal_odds": str(self.decimal_odds),
            "observed_ts": self.observed_ts,
            "source_id": self.source_id,
            "sequence": self.sequence,
            "market_type": self.market_type.value,
            "status": self.status,
            "source_ts": self.source_ts,
            "ingest_ts": self.ingest_ts,
            "score_state": self.score_state,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class TicketLeg:
    event_id: str
    market_id: str
    selection_id: str
    locked_odds: Decimal

    @property
    def quote_key(self) -> str:
        return f"{self.event_id}|{self.market_id}|{self.selection_id}"


@dataclass(slots=True)
class PaperTicket:
    ticket_id: str
    stake: Decimal
    legs: tuple[TicketLeg, ...]
    placed_at: str
    status: TicketStatus = TicketStatus.OPEN
    payout: Decimal = Decimal("0")
    strategy_reason: str = ""

    @property
    def combined_odds(self) -> Decimal:
        value = Decimal("1")
        for leg in self.legs:
            value *= leg.locked_odds
        return value

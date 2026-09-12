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
    def dedupe_key(self) -> str:
        return f"{self.source_id}:{self.sequence}"

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MarketEvent":
        return cls(
            event_id=str(raw["event_id"]),
            market_id=str(raw["market_id"]),
            selection_id=str(raw["selection_id"]),
            decimal_odds=Decimal(str(raw["decimal_odds"])),
            observed_ts=str(raw["observed_ts"]),
            source_id=str(raw.get("source_id", "fixture")),
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

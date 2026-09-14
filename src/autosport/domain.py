from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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


def _required_canonical_string(raw: dict[str, Any], field_name: str) -> str:
    value = raw.get(field_name)
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    return value


def _required_sequence(raw: dict[str, Any]) -> int:
    value = raw.get("sequence")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("sequence must be a non-boolean int")
    return value


def _required_decimal_odds(raw: dict[str, Any]) -> Decimal:
    try:
        value = Decimal(str(raw["decimal_odds"]))
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise ValueError("decimal_odds must be a finite decimal greater than 1") from exc
    if not value.is_finite() or value <= 1:
        raise ValueError("decimal_odds must be a finite decimal greater than 1")
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
        event_id = _required_canonical_string(raw, "event_id")
        market_id = _required_canonical_string(raw, "market_id")
        selection_id = _required_canonical_string(raw, "selection_id")
        observed_ts = _required_canonical_string(raw, "observed_ts")
        source_id = _required_canonical_string(raw, "source_id")
        sequence = _required_sequence(raw)
        decimal_odds = _required_decimal_odds(raw)

        ingest_ts = raw.get("ingest_ts", observed_ts)
        if not isinstance(ingest_ts, str) or not ingest_ts or ingest_ts.strip() != ingest_ts:
            raise ValueError("ingest_ts must be a non-empty trimmed string")

        return cls(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=decimal_odds,
            observed_ts=observed_ts,
            source_id=source_id,
            sequence=sequence,
            market_type=MarketType(str(raw.get("market_type", "other"))),
            status=str(raw.get("status", "open")),
            source_ts=raw.get("source_ts"),
            ingest_ts=ingest_ts,
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

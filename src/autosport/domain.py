from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any


_MAX_SERIALIZED_METADATA_NESTING = 64


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


def _require_utf8_encodable(value: str, field_name: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be UTF-8 encodable") from exc
    return value


def _canonical_string_value(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    return _require_utf8_encodable(value, field_name)


def _timezone_aware_iso8601_value(value: object, field_name: str) -> str:
    timestamp = _canonical_string_value(value, field_name)
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware ISO-8601")
    return timestamp


def _required_canonical_string(raw: dict[str, Any], field_name: str) -> str:
    return _canonical_string_value(raw.get(field_name), field_name)


def _optional_canonical_string(raw: dict[str, Any], field_name: str) -> str | None:
    value = raw.get(field_name)
    if value is None:
        return None
    return _canonical_string_value(value, field_name)


def _optional_canonical_timestamp(raw: dict[str, Any], field_name: str) -> str | None:
    value = raw.get(field_name)
    if value is None:
        return None
    return _timezone_aware_iso8601_value(value, field_name)


def _required_sequence(raw: dict[str, Any]) -> int:
    value = raw.get("sequence")
    if type(value) is not int:
        raise ValueError("sequence must be a non-boolean int")
    return value


def _required_decimal_odds(raw: dict[str, Any]) -> Decimal:
    raw_value = raw.get("decimal_odds")
    if (
        type(raw_value) is not str
        or not raw_value
        or raw_value.strip() != raw_value
    ):
        raise ValueError("decimal_odds must be a finite decimal greater than 1")
    try:
        raw_value.encode("utf-8")
        value = Decimal(raw_value)
    except (UnicodeEncodeError, InvalidOperation, ValueError) as exc:
        raise ValueError("decimal_odds must be a finite decimal greater than 1") from exc
    if not value.is_finite() or value <= 1 or str(value) != raw_value:
        raise ValueError("decimal_odds must be a finite decimal greater than 1")
    return value


def _validate_serialized_json_value(value: object, field_name: str) -> None:
    """Require metadata to survive JSON persistence without type drift or ambiguity."""

    stack: list[tuple[object, str, int, bool]] = [(value, field_name, 0, False)]
    active_containers: set[int] = set()

    while stack:
        current, path, depth, exiting = stack.pop()
        if exiting:
            active_containers.remove(id(current))
            continue

        if current is None or type(current) is bool or type(current) is int:
            continue
        if type(current) is str:
            _require_utf8_encodable(current, path)
            continue
        if type(current) is float:
            if not math.isfinite(current):
                raise ValueError(f"{path} contains non-finite JSON number")
            continue
        if type(current) is list or type(current) is dict:
            if depth > _MAX_SERIALIZED_METADATA_NESTING:
                raise ValueError(
                    f"{field_name} exceeds maximum JSON nesting depth "
                    f"{_MAX_SERIALIZED_METADATA_NESTING}"
                )
            container_id = id(current)
            if container_id in active_containers:
                raise ValueError(f"{path} contains cyclic JSON container")
            active_containers.add(container_id)
            stack.append((current, path, depth, True))

            if type(current) is list:
                for index, item in enumerate(current):
                    stack.append((item, f"{path}[{index}]", depth + 1, False))
            else:
                for key, item in current.items():
                    if type(key) is not str:
                        raise ValueError(f"{path} contains non-string JSON object key")
                    _require_utf8_encodable(key, f"{path} object key")
                    stack.append((item, f"{path}.{key}", depth + 1, False))
            continue
        raise ValueError(
            f"{path} contains non-canonical JSON value type {type(current).__name__}"
        )


def _serialized_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    metadata = raw.get("metadata", {})
    if type(metadata) is not dict:
        raise ValueError("metadata must be a JSON object")
    _validate_serialized_json_value(metadata, "metadata")
    return copy.deepcopy(metadata)


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
        if type(raw) is not dict:
            raise ValueError("serialized market event must be a JSON object")

        event_id = _required_canonical_string(raw, "event_id")
        market_id = _required_canonical_string(raw, "market_id")
        selection_id = _required_canonical_string(raw, "selection_id")
        observed_ts = _required_canonical_string(raw, "observed_ts")
        source_id = _required_canonical_string(raw, "source_id")
        sequence = _required_sequence(raw)
        decimal_odds = _required_decimal_odds(raw)

        ingest_ts = _canonical_string_value(raw.get("ingest_ts", observed_ts), "ingest_ts")

        market_type_raw = _canonical_string_value(raw.get("market_type", "other"), "market_type")
        try:
            market_type = MarketType(market_type_raw)
        except ValueError as exc:
            raise ValueError("market_type must be a supported market type") from exc

        status = _canonical_string_value(raw.get("status", "open"), "status")
        source_ts = _optional_canonical_timestamp(raw, "source_ts")
        score_state = _optional_canonical_string(raw, "score_state")
        metadata = _serialized_metadata(raw)

        return cls(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=decimal_odds,
            observed_ts=observed_ts,
            source_id=source_id,
            sequence=sequence,
            market_type=market_type,
            status=status,
            source_ts=source_ts,
            ingest_ts=ingest_ts,
            score_state=score_state,
            metadata=metadata,
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

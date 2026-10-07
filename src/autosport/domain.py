from __future__ import annotations

import base64
import binascii
import copy
import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any


_MAX_SERIALIZED_METADATA_NESTING = 64
_SEMANTIC_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789._:/-")
_RESERVED_SEMANTIC_IDENTITIES = frozenset({"unknown", "mixed", "unspecified"})
_EXCHANGE_SIDES = frozenset({"back", "lay"})


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
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    return value


def _canonical_string_value(value: object, field_name: str) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ValueError(f"{field_name} must be a non-empty trimmed string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field_name} must not contain control characters")
    return _require_utf8_encodable(value, field_name)


def _canonical_semantic_identity(value: object, field_name: str) -> str:
    identity = _canonical_string_value(value, field_name)
    if identity != identity.lower():
        raise ValueError(f"{field_name} must be a lowercase canonical semantic identity")
    if any(character not in _SEMANTIC_ID_CHARS for character in identity):
        raise ValueError(
            f"{field_name} must use lowercase ASCII letters, digits, '.', '_', ':', '/', or '-' only"
        )
    if identity in _RESERVED_SEMANTIC_IDENTITIES:
        raise ValueError(f"{field_name} must not use reserved identity {identity!r}")
    return identity


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


def _optional_semantic_identity(raw: dict[str, Any], field_name: str) -> str | None:
    value = raw.get(field_name)
    if value is None:
        return None
    return _canonical_semantic_identity(value, field_name)


_RESERVED_EVENT_SPORT_IDENTITIES = frozenset({"unknown", "mixed"})


def _canonical_sport_value(value: object, field_name: str = "sport") -> str:
    sport = _canonical_string_value(value, field_name)
    if sport != sport.lower():
        raise ValueError(f"{field_name} must be lowercase canonical sport identity")
    if "|" in sport or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in sport
    ):
        raise ValueError(
            f"{field_name} must use lowercase ASCII letters, digits, '_' or '-' only"
        )
    if sport in _RESERVED_EVENT_SPORT_IDENTITIES:
        raise ValueError(
            f"{field_name} must not use reserved dataset scope identity {sport!r}"
        )
    return sport


def _canonical_exchange_side(value: object, field_name: str = "exchange_side") -> str:
    side = _canonical_string_value(value, field_name)
    if side not in _EXCHANGE_SIDES:
        raise ValueError(f"{field_name} must be canonical 'back' or 'lay'")
    return side


def _encoded_component_boundary_identity(kind: str, *components: object) -> str:
    """Encode legacy identity components whose delimiter would otherwise alias boundaries.

    Delimiter-free legacy identities stay byte-for-byte unchanged. This
    namespace is used only when a canonical component contains '|'. Base64url
    never contains '|', so encoded keys cannot collide with legacy pipe keys.
    """
    payload = json.dumps(
        [kind, *components],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"component-boundary-v1-{token}"


def _encoded_sport_identity(kind: str, *components: object) -> str:
    """Return an explicit-sport identity disjoint from every legacy pipe key.

    Legacy quote/dedupe identities always contain pipe separators. URL-safe
    base64 never contains a pipe, so this namespace cannot alias legacy keys.
    """
    payload = json.dumps(
        [kind, *components],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"sport-v2-{token}"


def _encoded_exchange_side_identity(kind: str, *components: object) -> str:
    """Return an exchange-side identity disjoint from legacy and sport-v2 keys."""
    payload = json.dumps(
        [kind, *components],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"exchange-side-v1-{token}"


def _optional_sport(raw: dict[str, Any]) -> str | None:
    value = raw.get("sport")
    if value is None:
        return None
    return _canonical_sport_value(value)


def _optional_exchange_side(raw: dict[str, Any]) -> str | None:
    value = raw.get("exchange_side")
    if value is None:
        return None
    return _canonical_exchange_side(value)


def _quote_identity(
    event_id: str,
    market_id: str,
    selection_id: str,
    sport: str | None,
    exchange_side: str | None = None,
) -> str:
    # This helper is also a direct lookup boundary (for example MarketMirror.get),
    # not only a constructor helper. Re-prove every identity component here so a
    # str subclass cannot reach f-string/JSON dispatch before canonical admission.
    canonical_event_id = _canonical_string_value(event_id, "event_id")
    canonical_market_id = _canonical_string_value(market_id, "market_id")
    canonical_selection_id = _canonical_string_value(selection_id, "selection_id")
    if exchange_side is not None:
        canonical_side = _canonical_exchange_side(exchange_side)
        canonical_sport = None if sport is None else _canonical_sport_value(sport)
        return _encoded_exchange_side_identity(
            "quote",
            canonical_sport,
            canonical_event_id,
            canonical_market_id,
            canonical_selection_id,
            canonical_side,
        )
    if sport is None:
        if any(
            "|" in component
            for component in (
                canonical_event_id,
                canonical_market_id,
                canonical_selection_id,
            )
        ):
            return _encoded_component_boundary_identity(
                "quote",
                canonical_event_id,
                canonical_market_id,
                canonical_selection_id,
            )
        return (
            f"{canonical_event_id}|{canonical_market_id}|{canonical_selection_id}"
        )
    canonical_sport = _canonical_sport_value(sport)
    return _encoded_sport_identity(
        "quote",
        canonical_sport,
        canonical_event_id,
        canonical_market_id,
        canonical_selection_id,
    )


def _decode_quote_identity_payload(quote_key: str, prefix: str) -> list[object]:
    token = quote_key[len(prefix) :]
    if not token or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in token
    ):
        raise ValueError("quote_key encoded identity token is not canonical base64url")
    padding = "=" * (-len(token) % 4)
    try:
        raw = base64.b64decode(
            (token + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("quote_key encoded identity payload is invalid") from exc
    if type(payload) is not list:
        raise ValueError("quote_key encoded identity payload must be a JSON array")
    return payload


def _quote_identity_components(
    value: object,
) -> tuple[str | None, str, str, str, str | None]:
    """Decode one canonical quote identity without accepting ambiguous aliases.

    Returns (sport, event_id, market_id, selection_id, exchange_side).
    Every encoded form must byte-round-trip through the canonical encoder.
    """
    quote_key = _canonical_string_value(value, "quote_key")
    if "|" not in quote_key:
        if quote_key.startswith("component-boundary-v1-"):
            payload = _decode_quote_identity_payload(
                quote_key, "component-boundary-v1-"
            )
            if len(payload) != 4 or payload[0] != "quote":
                raise ValueError("quote_key component-boundary payload is not a quote")
            event_id = _canonical_string_value(payload[1], "event_id")
            market_id = _canonical_string_value(payload[2], "market_id")
            selection_id = _canonical_string_value(payload[3], "selection_id")
            if _quote_identity(event_id, market_id, selection_id, None, None) != quote_key:
                raise ValueError("quote_key component-boundary encoding is not canonical")
            return None, event_id, market_id, selection_id, None

        if quote_key.startswith("sport-v2-"):
            payload = _decode_quote_identity_payload(quote_key, "sport-v2-")
            if len(payload) != 5 or payload[0] != "quote":
                raise ValueError("quote_key sport-v2 payload is not a quote")
            sport = _canonical_sport_value(payload[1])
            event_id = _canonical_string_value(payload[2], "event_id")
            market_id = _canonical_string_value(payload[3], "market_id")
            selection_id = _canonical_string_value(payload[4], "selection_id")
            if _quote_identity(event_id, market_id, selection_id, sport, None) != quote_key:
                raise ValueError("quote_key sport-v2 encoding is not canonical")
            return sport, event_id, market_id, selection_id, None

        if quote_key.startswith("exchange-side-v1-"):
            payload = _decode_quote_identity_payload(quote_key, "exchange-side-v1-")
            if len(payload) != 6 or payload[0] != "quote":
                raise ValueError("quote_key exchange-side payload is not a quote")
            sport = None if payload[1] is None else _canonical_sport_value(payload[1])
            event_id = _canonical_string_value(payload[2], "event_id")
            market_id = _canonical_string_value(payload[3], "market_id")
            selection_id = _canonical_string_value(payload[4], "selection_id")
            exchange_side = _canonical_exchange_side(payload[5])
            if _quote_identity(
                event_id,
                market_id,
                selection_id,
                sport,
                exchange_side,
            ) != quote_key:
                raise ValueError("quote_key exchange-side encoding is not canonical")
            return sport, event_id, market_id, selection_id, exchange_side

    parts = quote_key.split("|")
    if len(parts) != 3 or not all(parts):
        raise ValueError("quote_key is not a canonical decodable quote identity")
    event_id = _canonical_string_value(parts[0], "event_id")
    market_id = _canonical_string_value(parts[1], "market_id")
    selection_id = _canonical_string_value(parts[2], "selection_id")
    if _quote_identity(event_id, market_id, selection_id, None, None) != quote_key:
        raise ValueError("quote_key legacy identity is not canonical")
    return None, event_id, market_id, selection_id, None


def _optional_canonical_timestamp(raw: dict[str, Any], field_name: str) -> str | None:
    value = raw.get(field_name)
    if value is None:
        return None
    return _timezone_aware_iso8601_value(value, field_name)


def _canonical_sequence_value(value: object) -> int:
    if type(value) is not int:
        raise ValueError("sequence must be a non-boolean int")
    if value < -(2**63) or value > 2**63 - 1:
        raise ValueError("sequence must fit signed 64-bit integer")
    return value


def _required_sequence(raw: dict[str, Any]) -> int:
    return _canonical_sequence_value(raw.get("sequence"))


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
    sport: str | None = None
    competition_id: str | None = None
    market_semantics_id: str | None = None
    provider_source_class: str | None = None
    exchange_side: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "event_id",
            "market_id",
            "selection_id",
            "source_id",
        ):
            _canonical_string_value(getattr(self, field_name), field_name)
        _canonical_sequence_value(self.sequence)
        _timezone_aware_iso8601_value(self.observed_ts, "observed_ts")
        _timezone_aware_iso8601_value(self.ingest_ts, "ingest_ts")
        if self.source_ts is not None:
            _timezone_aware_iso8601_value(self.source_ts, "source_ts")
        object.__setattr__(
            self,
            "metadata",
            _serialized_metadata({"metadata": self.metadata}),
        )
        if self.sport is not None:
            _canonical_sport_value(self.sport)
        for field_name in (
            "competition_id",
            "market_semantics_id",
            "provider_source_class",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _canonical_semantic_identity(value, field_name)
        if self.exchange_side is not None:
            _canonical_exchange_side(self.exchange_side)

    @property
    def quote_key(self) -> str:
        return _quote_identity(
            self.event_id,
            self.market_id,
            self.selection_id,
            self.sport,
            self.exchange_side,
        )

    @property
    def dedupe_key(self) -> str:
        # Dedupe identity is a public canonical identity surface too. Re-prove the
        # durable identity components on every read so post-construction mutation
        # cannot reach formatting/JSON encoding before fail-closed admission.
        canonical_source_id = _canonical_string_value(self.source_id, "source_id")
        canonical_event_id = _canonical_string_value(self.event_id, "event_id")
        canonical_market_id = _canonical_string_value(self.market_id, "market_id")
        canonical_selection_id = _canonical_string_value(
            self.selection_id, "selection_id"
        )
        canonical_sequence = _canonical_sequence_value(self.sequence)
        if self.exchange_side is not None:
            canonical_side = _canonical_exchange_side(self.exchange_side)
            canonical_sport = (
                None if self.sport is None else _canonical_sport_value(self.sport)
            )
            return _encoded_exchange_side_identity(
                "dedupe",
                canonical_source_id,
                canonical_sport,
                canonical_event_id,
                canonical_market_id,
                canonical_selection_id,
                canonical_side,
                canonical_sequence,
            )
        if self.sport is None:
            if any(
                "|" in component
                for component in (
                    canonical_source_id,
                    canonical_event_id,
                    canonical_market_id,
                    canonical_selection_id,
                )
            ):
                return _encoded_component_boundary_identity(
                    "dedupe",
                    canonical_source_id,
                    canonical_event_id,
                    canonical_market_id,
                    canonical_selection_id,
                    canonical_sequence,
                )
            return (
                f"{canonical_source_id}|{canonical_event_id}|{canonical_market_id}|"
                f"{canonical_selection_id}|{canonical_sequence}"
            )
        return _encoded_sport_identity(
            "dedupe",
            canonical_source_id,
            _canonical_sport_value(self.sport),
            canonical_event_id,
            canonical_market_id,
            canonical_selection_id,
            canonical_sequence,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MarketEvent":
        if type(raw) is not dict:
            raise ValueError("serialized market event must be a JSON object")

        required_fields = {
            "event_id",
            "market_id",
            "selection_id",
            "decimal_odds",
            "observed_ts",
            "source_id",
            "sequence",
        }
        optional_fields = {
            "market_type",
            "status",
            "source_ts",
            "ingest_ts",
            "score_state",
            "metadata",
            "sport",
            "competition_id",
            "market_semantics_id",
            "provider_source_class",
            "exchange_side",
        }
        raw_keys = tuple(raw.keys())
        if (
            any(type(key) is not str for key in raw_keys)
            or not required_fields.issubset(raw_keys)
            or any(key not in required_fields | optional_fields for key in raw_keys)
        ):
            raise ValueError("serialized market event fields mismatch")

        event_id = _required_canonical_string(raw, "event_id")
        market_id = _required_canonical_string(raw, "market_id")
        selection_id = _required_canonical_string(raw, "selection_id")
        observed_ts = _timezone_aware_iso8601_value(raw.get("observed_ts"), "observed_ts")
        source_id = _required_canonical_string(raw, "source_id")
        sequence = _required_sequence(raw)
        decimal_odds = _required_decimal_odds(raw)

        ingest_ts = _timezone_aware_iso8601_value(
            raw.get("ingest_ts", observed_ts),
            "ingest_ts",
        )

        market_type_raw = _canonical_string_value(raw.get("market_type", "other"), "market_type")
        try:
            market_type = MarketType(market_type_raw)
        except ValueError as exc:
            raise ValueError("market_type must be a supported market type") from exc

        status = _canonical_string_value(raw.get("status", "open"), "status")
        source_ts = _optional_canonical_timestamp(raw, "source_ts")
        score_state = _optional_canonical_string(raw, "score_state")
        metadata = _serialized_metadata(raw)
        sport = _optional_sport(raw)
        competition_id = _optional_semantic_identity(raw, "competition_id")
        market_semantics_id = _optional_semantic_identity(raw, "market_semantics_id")
        provider_source_class = _optional_semantic_identity(raw, "provider_source_class")
        exchange_side = _optional_exchange_side(raw)

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
            sport=sport,
            competition_id=competition_id,
            market_semantics_id=market_semantics_id,
            provider_source_class=provider_source_class,
            exchange_side=exchange_side,
        )

    def to_dict(self) -> dict[str, Any]:
        # Serialization is a canonical identity use-boundary. Re-prove every
        # identity-bearing component before publishing it so post-construction
        # mutation cannot leak a noncanonical alias into durable/downstream state.
        canonical_event_id = _canonical_string_value(self.event_id, "event_id")
        canonical_market_id = _canonical_string_value(self.market_id, "market_id")
        canonical_selection_id = _canonical_string_value(
            self.selection_id, "selection_id"
        )
        canonical_source_id = _canonical_string_value(self.source_id, "source_id")
        canonical_sequence = _canonical_sequence_value(self.sequence)
        canonical_sport = (
            None if self.sport is None else _canonical_sport_value(self.sport)
        )
        canonical_competition_id = (
            None
            if self.competition_id is None
            else _canonical_semantic_identity(self.competition_id, "competition_id")
        )
        canonical_market_semantics_id = (
            None
            if self.market_semantics_id is None
            else _canonical_semantic_identity(
                self.market_semantics_id, "market_semantics_id"
            )
        )
        canonical_provider_source_class = (
            None
            if self.provider_source_class is None
            else _canonical_semantic_identity(
                self.provider_source_class, "provider_source_class"
            )
        )
        canonical_exchange_side = (
            None
            if self.exchange_side is None
            else _canonical_exchange_side(self.exchange_side)
        )
        # Chronology is authority-bearing too. Frozen dataclasses can still be
        # tampered with through object.__setattr__, so re-prove every causal
        # timestamp at the public serialization boundary instead of publishing
        # unchecked post-construction values.
        canonical_observed_ts = _timezone_aware_iso8601_value(
            self.observed_ts, "observed_ts"
        )
        canonical_ingest_ts = _timezone_aware_iso8601_value(
            self.ingest_ts, "ingest_ts"
        )
        canonical_source_ts = (
            None
            if self.source_ts is None
            else _timezone_aware_iso8601_value(self.source_ts, "source_ts")
        )

        payload = {
            "event_id": canonical_event_id,
            "market_id": canonical_market_id,
            "selection_id": canonical_selection_id,
            "decimal_odds": str(self.decimal_odds),
            "observed_ts": canonical_observed_ts,
            "source_id": canonical_source_id,
            "sequence": canonical_sequence,
            "market_type": self.market_type.value,
            "status": self.status,
            "source_ts": canonical_source_ts,
            "ingest_ts": canonical_ingest_ts,
            "score_state": self.score_state,
            "metadata": _serialized_metadata({"metadata": self.metadata}),
        }
        if canonical_sport is not None:
            payload["sport"] = canonical_sport
        if canonical_competition_id is not None:
            payload["competition_id"] = canonical_competition_id
        if canonical_market_semantics_id is not None:
            payload["market_semantics_id"] = canonical_market_semantics_id
        if canonical_provider_source_class is not None:
            payload["provider_source_class"] = canonical_provider_source_class
        if canonical_exchange_side is not None:
            payload["exchange_side"] = canonical_exchange_side
        return payload


@dataclass(frozen=True, slots=True)
class TicketLeg:
    event_id: str
    market_id: str
    selection_id: str
    locked_odds: Decimal
    sport: str | None = None
    exchange_side: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("event_id", "market_id", "selection_id"):
            _canonical_string_value(getattr(self, field_name), field_name)
        if self.sport is not None:
            _canonical_sport_value(self.sport)
        if self.exchange_side is not None:
            _canonical_exchange_side(self.exchange_side)

    @property
    def quote_key(self) -> str:
        return _quote_identity(
            self.event_id,
            self.market_id,
            self.selection_id,
            self.sport,
            self.exchange_side,
        )


@dataclass(slots=True)
class PaperTicket:
    ticket_id: str
    stake: Decimal
    legs: tuple[TicketLeg, ...]
    placed_at: str
    status: TicketStatus = TicketStatus.OPEN
    payout: Decimal = Decimal("0")
    strategy_reason: str = ""
    provider_source_ids: tuple[str, ...] = ()
    # Bookmaker account identity is source-scoped; bare account_id is not global.
    provider_accounts: tuple[tuple[str, str], ...] = ()
    bankroll_id: str | None = None
    currency: str | None = None
    settled_at: str | None = None

    @property
    def combined_odds(self) -> Decimal:
        value = Decimal("1")
        for leg in self.legs:
            value *= leg.locked_odds
        return value

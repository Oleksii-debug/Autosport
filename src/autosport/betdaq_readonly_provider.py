from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import math
from typing import Callable, Protocol, Sequence

from .betdaq_readonly_market_wire import (
    BetdaqGetPricesWireResponse,
    BetdaqSoapProtocolError,
    parse_get_prices_response,
)
from .domain import MarketType
from .providers import ProviderBatch, ProviderQuote, ProviderUnavailableError

BETDAQ_GET_PRICES_ENDPOINT = "https://api.betdaq.com/v2.0/ReadOnlyService.asmx"
BETDAQ_GET_PRICES_SOAP_ACTION = "http://www.GlobalBettingExchange.com/ExternalAPI/GetPrices"
BETDAQ_GET_PRICES_MAX_MARKETS = 50


class BetdaqTransientTransportError(RuntimeError):
    pass


class BetdaqReadOnlyTransport(Protocol):
    def get_prices(
        self, request: "BetdaqGetPricesRequest", *, timeout_seconds: float
    ) -> bytes | str: ...


@dataclass(frozen=True, slots=True)
class BetdaqGetPricesRequest:
    request_id: int
    market_ids: tuple[int, ...]
    threshold_amount: Decimal
    number_for_prices_required: int = 1
    number_against_prices_required: int = 1
    want_market_matched_amount: bool = False
    want_selections_matched_amounts: bool = False
    want_selection_matched_details: bool = False

    def __post_init__(self) -> None:
        if type(self.request_id) is not int or self.request_id < 0:
            raise ValueError("request_id must be a non-negative integer")
        if (
            type(self.market_ids) is not tuple
            or not self.market_ids
            or len(self.market_ids) > BETDAQ_GET_PRICES_MAX_MARKETS
            or any(type(value) is not int or value < 0 for value in self.market_ids)
            or len(set(self.market_ids)) != len(self.market_ids)
        ):
            raise ValueError("market_ids must be 1..50 unique non-negative integers")
        if (
            not isinstance(self.threshold_amount, Decimal)
            or not self.threshold_amount.is_finite()
            or self.threshold_amount <= 0
        ):
            raise ValueError("threshold_amount must be a positive finite Decimal")
        if self.number_for_prices_required != 1 or self.number_against_prices_required != 1:
            raise ValueError("canonical adapter requires exactly one price per side")
        if any(
            type(value) is not bool
            for value in (
                self.want_market_matched_amount,
                self.want_selections_matched_amounts,
                self.want_selection_matched_details,
            )
        ):
            raise TypeError("GetPrices Want* dimensions must be bool")


@dataclass(frozen=True, slots=True)
class BetdaqMarketBinding:
    market_id: int
    provider_event_id: str
    sport: str
    market_type: MarketType = MarketType.OTHER

    def __post_init__(self) -> None:
        if type(self.market_id) is not int or self.market_id < 0:
            raise ValueError("market_id must be non-negative")
        if (
            type(self.provider_event_id) is not str
            or not self.provider_event_id
            or self.provider_event_id != self.provider_event_id.strip()
            or ":" in self.provider_event_id
            or "|" in self.provider_event_id
        ):
            raise ValueError("provider_event_id is not canonical")
        if (
            type(self.sport) is not str
            or not self.sport
            or self.sport != self.sport.strip().lower()
            or self.sport in {"unknown", "mixed"}
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in self.sport)
        ):
            raise ValueError("sport is not canonical")
        if not isinstance(self.market_type, MarketType):
            raise TypeError("market_type must be MarketType")


@dataclass(frozen=True, slots=True)
class BetdaqRequestEvidence:
    request_id: int
    market_ids: tuple[int, ...]
    attempts: int
    response_received_at: str
    request_fingerprint: str
    response_sha256: str
    call_id: str | None
    message_created_at: str | None


@dataclass(frozen=True, slots=True)
class BetdaqSnapshotEvidence:
    observed_at: str
    requests: tuple[BetdaqRequestEvidence, ...]
    aggregate_sha256: str
    quote_source_timestamp_available: bool = False
    live_entitlement_verified: bool = False
    provider_origin_verified: bool = False
    receipt_clock_verified: bool = False


Clock = Callable[[], str]
RequestIdFactory = Callable[[], int]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _time(value: str, field: str) -> tuple[datetime, str]:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be trimmed ISO-8601")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ValueError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed, value


def _sequence(observed: datetime) -> int:
    delta = observed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    value = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    if not 0 <= value <= (1 << 63) - 1:
        raise ValueError("observed_at cannot be represented as sequence")
    return value


def _fingerprint(request: BetdaqGetPricesRequest) -> str:
    payload = json.dumps(
        {
            "market_ids": request.market_ids,
            "threshold_amount": format(request.threshold_amount, "f"),
            "for": request.number_for_prices_required,
            "against": request.number_against_prices_required,
            "want_market_matched": request.want_market_matched_amount,
            "want_selections_matched": request.want_selections_matched_amounts,
            "want_selection_details": request.want_selection_matched_details,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class BetdaqReadOnlyProvider:
    """Observation-only BETDAQ top-of-book adapter.

    GetPrices has no documented per-price update timestamp, so source_ts is always
    None. StartTime is event schedule metadata; WS-Security Created is message
    metadata. Market scope is complete, but depth is explicitly TOP_OF_BOOK_ONLY.
    """

    source_id = "betdaq-readonly"

    def __init__(
        self,
        *,
        transport: BetdaqReadOnlyTransport,
        market_bindings: Sequence[BetdaqMarketBinding],
        threshold_amount: Decimal,
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
        max_message_age_seconds: int | None = None,
        clock: Clock = utc_now_iso,
        request_id_factory: RequestIdFactory | None = None,
    ) -> None:
        bindings = tuple(market_bindings)
        if not callable(getattr(transport, "get_prices", None)):
            raise TypeError("transport must expose get_prices")
        if not bindings or any(type(item) is not BetdaqMarketBinding for item in bindings):
            raise ValueError("market_bindings must be non-empty BetdaqMarketBinding values")
        ids = tuple(item.market_id for item in bindings)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate market binding")
        if (
            not isinstance(threshold_amount, Decimal)
            or not threshold_amount.is_finite()
            or threshold_amount <= 0
        ):
            raise ValueError("threshold_amount must be positive finite Decimal")
        if (
            isinstance(timeout_seconds, bool)
            or type(timeout_seconds) not in {int, float}
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive finite")
        if type(max_attempts) is not int or max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if max_message_age_seconds is not None and (
            type(max_message_age_seconds) is not int or max_message_age_seconds <= 0
        ):
            raise ValueError("max_message_age_seconds must be positive or None")
        self.transport = transport
        self._bindings = {item.market_id: item for item in bindings}
        self._market_ids = ids
        self.threshold_amount = threshold_amount
        self.timeout_seconds = float(timeout_seconds)
        self.max_attempts = max_attempts
        self.max_message_age_seconds = max_message_age_seconds
        self.clock = clock
        self._receipt_clock_verified = clock is utc_now_iso
        self.request_id_factory = request_id_factory
        self._next_id = 1
        self._pending: tuple[ProviderQuote, ...] = ()
        self._offset = 0
        self._cursor: str | None = None
        self._flags: tuple[str, ...] = ()
        self._evidence: BetdaqSnapshotEvidence | None = None

    @property
    def last_request_evidence(self) -> BetdaqSnapshotEvidence | None:
        return self._evidence

    def _id(self) -> int:
        value = self.request_id_factory() if self.request_id_factory else self._next_id
        if self.request_id_factory is None:
            self._next_id += 1
        if type(value) is not int or value < 0:
            raise ValueError("request_id_factory returned invalid value")
        return value

    def _fetch(self, request: BetdaqGetPricesRequest) -> tuple[bytes | str, int]:
        for attempt in range(1, self.max_attempts + 1):
            try:
                payload = self.transport.get_prices(request, timeout_seconds=self.timeout_seconds)
                if not isinstance(payload, (bytes, str)):
                    raise TypeError("transport must return bytes or str")
                return payload, attempt
            except (BetdaqTransientTransportError, TimeoutError, ConnectionError) as exc:
                if attempt == self.max_attempts:
                    raise ProviderUnavailableError(
                        f"BETDAQ unavailable after {attempt} bounded attempts"
                    ) from exc
        raise AssertionError("unreachable")

    def _check_message_time(
        self, response: BetdaqGetPricesWireResponse, received: datetime
    ) -> None:
        created = response.provider_created_at
        if created is None:
            return
        age = (received.astimezone(timezone.utc) - created.astimezone(timezone.utc)).total_seconds()
        if age < 0:
            raise BetdaqSoapProtocolError("WS-Security Created is in the future")
        if self.max_message_age_seconds is not None and age > self.max_message_age_seconds:
            raise BetdaqSoapProtocolError("WS-Security message envelope is stale")

    def _map(
        self,
        response: BetdaqGetPricesWireResponse,
        market_ids: tuple[int, ...],
        observed_text: str,
        sequence: int,
    ) -> list[ProviderQuote]:
        actual = {market.market_id for market in response.markets}
        if actual != set(market_ids):
            raise BetdaqSoapProtocolError("GetPrices market set mismatch")
        result: list[ProviderQuote] = []
        for market in sorted(response.markets, key=lambda x: x.market_id):
            binding = self._bindings[market.market_id]
            for selection in sorted(market.selections, key=lambda x: x.selection_id):
                if len(selection.for_side_prices) > 1 or len(selection.against_side_prices) > 1:
                    raise BetdaqSoapProtocolError("GetPrices returned deeper ladder than the canonical one-level request")
                base = {
                    "betdaq_market_name": market.name,
                    "betdaq_market_type_code": market.market_type_code,
                    "betdaq_market_status_code": market.status_code,
                    "betdaq_market_start_time": market.start_time_text,
                    "betdaq_selection_name": selection.name,
                    "betdaq_selection_status_code": selection.status_code,
                    "betdaq_selection_reset_count": selection.reset_count,
                    "betdaq_withdrawal_sequence_number": market.withdrawal_sequence_number,
                    "betdaq_is_currently_in_running": market.is_currently_in_running,
                    "betdaq_in_running_delay_seconds": market.in_running_delay_seconds,
                    "betdaq_message_created_at": response.provider_created_at_text,
                }
                if selection.deduction_factor is not None:
                    base["betdaq_deduction_factor"] = format(selection.deduction_factor, "f")
                for side, levels in (
                    ("back", selection.for_side_prices),
                    ("lay", selection.against_side_prices),
                ):
                    if not levels:
                        continue
                    level = levels[0]
                    if level.price <= 1:
                        raise BetdaqSoapProtocolError("canonical decimal odds must be greater than 1")
                    metadata = dict(base)
                    metadata["betdaq_available_amount"] = format(level.stake, "f")
                    metadata["betdaq_provider_side"] = level.provider_side
                    result.append(
                        ProviderQuote(
                            provider_event_id=binding.provider_event_id,
                            provider_market_id=str(market.market_id),
                            provider_selection_id=str(selection.selection_id),
                            decimal_odds=level.price,
                            observed_ts=observed_text,
                            sequence=sequence,
                            market_type=binding.market_type,
                            status=f"betdaq:{selection.status_code}",
                            source_ts=None,
                            metadata=metadata,
                            sport=binding.sport,
                            exchange_side=side,
                        )
                    )
        return result

    def _load(self) -> None:
        chunks = tuple(
            self._market_ids[i:i + BETDAQ_GET_PRICES_MAX_MARKETS]
            for i in range(0, len(self._market_ids), BETDAQ_GET_PRICES_MAX_MARKETS)
        )
        parsed: list[tuple[BetdaqGetPricesRequest, BetdaqGetPricesWireResponse, bytes, int, datetime, str]] = []
        for market_ids in chunks:
            request = BetdaqGetPricesRequest(self._id(), market_ids, self.threshold_amount)
            payload, attempts = self._fetch(request)
            received, received_text = _time(self.clock(), "response_received_at")
            response = parse_get_prices_response(payload)
            self._check_message_time(response, received)
            if {m.market_id for m in response.markets} != set(market_ids):
                raise BetdaqSoapProtocolError("GetPrices market set mismatch")
            raw = payload.encode() if isinstance(payload, str) else payload
            parsed.append((request, response, raw, attempts, received, received_text))

        observed, observed_text = _time(self.clock(), "observed_at")
        if observed.astimezone(timezone.utc) < max(
            row[4].astimezone(timezone.utc) for row in parsed
        ):
            raise ValueError("observed_at precedes response receipt")
        sequence = _sequence(observed)
        digest = hashlib.sha256()
        quotes: list[ProviderQuote] = []
        evidence: list[BetdaqRequestEvidence] = []
        missing_message_time = False
        for request, response, raw, attempts, _, received_text in parsed:
            quotes.extend(self._map(response, request.market_ids, observed_text, sequence))
            request_hash = _fingerprint(request)
            response_hash = hashlib.sha256(raw).hexdigest()
            digest.update(bytes.fromhex(request_hash))
            digest.update(bytes.fromhex(response_hash))
            missing_message_time |= response.provider_created_at is None
            evidence.append(
                BetdaqRequestEvidence(
                    request.request_id,
                    request.market_ids,
                    attempts,
                    received_text,
                    request_hash,
                    response_hash,
                    response.call_id,
                    response.provider_created_at_text,
                )
            )

        cursor = digest.hexdigest()
        flags = [
            "READ_ONLY",
            "QUOTE_SOURCE_TIMESTAMP_UNAVAILABLE",
            "FULL_MARKET_SCOPE",
            "TOP_OF_BOOK_ONLY",
            "LIVE_ENTITLEMENT_UNVERIFIED",
            "UNVERIFIED_PROVIDER_ORIGIN",
        ]
        if not self._receipt_clock_verified:
            flags.append("UNVERIFIED_RECEIPT_CLOCK")
        if missing_message_time:
            flags.append("MESSAGE_TIMESTAMP_UNAVAILABLE")
        snapshot_evidence = BetdaqSnapshotEvidence(
            observed_text,
            tuple(evidence),
            cursor,
            provider_origin_verified=False,
            receipt_clock_verified=self._receipt_clock_verified,
        )
        self._pending = tuple(quotes)
        self._offset = 0
        self._cursor = cursor
        self._flags = tuple(flags)
        self._evidence = snapshot_evidence

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if type(max_items) is not int or max_items <= 0:
            raise ValueError("max_items must be positive")
        if self._offset >= len(self._pending):
            self._load()
        end = min(self._offset + max_items, len(self._pending))
        quotes = self._pending[self._offset:end]
        self._offset = end
        flags = list(self._flags)
        if self._offset < len(self._pending):
            flags.append("TRUNCATED_BATCH")
        if not self._pending:
            flags.append("EMPTY_RESPONSE")
        batch = ProviderBatch(self.source_id, quotes, cursor=self._cursor, quality_flags=tuple(flags))
        if self._offset >= len(self._pending):
            self._pending, self._offset, self._cursor, self._flags = (), 0, None, ()
        return batch

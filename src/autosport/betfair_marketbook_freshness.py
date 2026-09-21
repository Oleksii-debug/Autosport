"""Provider-native Betfair MarketBook delay observations.

This is a narrow read-only extension of the canonical Betfair JSON-RPC client.
It issues one exact ``listMarketBook`` request and turns the required provider
``isMarketDataDelayed`` boolean into an origin-bound observation. Positive
(non-delayed) authority additionally requires the canonical production network
transport; injected transports remain usable for deterministic negative/parser
tests but can never mint FRESH truth. The client's account label is recorded
only as a configured reference; listMarketBook does not authenticate that label.
The module never performs provider writes or stores credentials in evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from weakref import ref

from . import betfair_account_readonly as _base


_LIST_MARKET_BOOK = "SportsAPING/v1.0/listMarketBook"
_CANONICAL_NETWORK_POST = _base.UrllibBetfairHttpTransport.post


class BetfairMarketBookFreshnessError(_base.BetfairReadOnlyError):
    """Raised when canonical MarketBook delay evidence cannot be obtained."""


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairMarketBookDelayObservation:
    """Exact provider-native delay observation for one Betfair market."""

    venue_id: str
    configured_account_ref: str
    adapter_id: str
    adapter_version: str
    market_id: str
    is_market_data_delayed: bool
    observed_at: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        _base._required_text(self.venue_id, "venue_id")
        _base._required_text(self.configured_account_ref, "configured_account_ref")
        if self.adapter_id != _base.ADAPTER_ID:
            raise BetfairMarketBookFreshnessError("market-book adapter_id mismatch")
        if self.adapter_version != _base.ADAPTER_VERSION:
            raise BetfairMarketBookFreshnessError("market-book adapter_version mismatch")
        _base._required_text(self.market_id, "market_id")
        if type(self.is_market_data_delayed) is not bool:
            raise BetfairMarketBookFreshnessError(
                "is_market_data_delayed must be exact bool"
            )
        _base._iso_timestamp(self.observed_at, "observed_at")
        _base._sha256_hex(self.source_payload_sha256, "source_payload_sha256")

    def _authority_fingerprint(self) -> str:
        payload = (
            self.venue_id,
            self.configured_account_ref,
            self.adapter_id,
            self.adapter_version,
            self.market_id,
            self.is_market_data_delayed,
            self.observed_at,
            self.source_payload_sha256,
        )
        return sha256(repr(payload).encode("utf-8")).hexdigest()

    @property
    def proves_provider_account_identity(self) -> bool:
        """MarketBook does not authenticate the caller's configured account label."""

        return False

    def assert_authoritative(self) -> None:
        raise BetfairMarketBookFreshnessError(
            "market-book observation was not issued by canonical Betfair adapter"
        )

    def assert_positive_authoritative(self) -> None:
        raise BetfairMarketBookFreshnessError(
            "positive market-book observation lacks canonical production network origin"
        )


def _canonical_network_transport(client: _base.BetfairReadOnlyClient) -> bool:
    """Return true only for the unmodified built-in production HTTP transport."""

    transport = client._transport
    if type(transport) is not _base.UrllibBetfairHttpTransport:
        return False
    if type(transport).post is not _CANONICAL_NETWORK_POST:
        return False
    transport_dict = getattr(transport, "__dict__", None)
    if type(transport_dict) is not dict:
        return False
    if set(transport_dict) != {"_max_response_bytes"}:
        return False
    return True


def _read_market_book_delay(
    client: _base.BetfairReadOnlyClient,
    market_id: str,
) -> BetfairMarketBookDelayObservation:
    if type(client) is not _base.BetfairReadOnlyClient:
        raise TypeError("client must be an exact BetfairReadOnlyClient")
    market = _base._required_text(market_id, "market_id")
    network_origin = _canonical_network_transport(client)
    request_id = client._next_request_id()
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": _LIST_MARKET_BOOK,
            "params": {"marketIds": [market]},
            "id": request_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Application": client._credentials.application_key,
        "X-Authentication": client._credentials.session_token,
    }
    payload = client._transport.post(
        _base.BETTING_JSON_RPC_ENDPOINT,
        headers=headers,
        body=body,
        timeout_seconds=client._timeout_seconds,
    )
    if not isinstance(payload, bytes):
        raise BetfairMarketBookFreshnessError("Betfair transport must return bytes")

    observed_at = (
        datetime.now(timezone.utc).isoformat()
        if network_origin
        else client._observed_at()
    )
    source_payload_sha256 = sha256(payload).hexdigest()
    decoded = _base._decode_json(payload)
    envelope = _base._mapping(decoded, "JSON-RPC response")
    if envelope.get("jsonrpc") != "2.0":
        raise BetfairMarketBookFreshnessError(
            "Betfair response has invalid jsonrpc version"
        )
    response_id = envelope.get("id")
    if type(response_id) is not int or response_id != request_id:
        raise BetfairMarketBookFreshnessError(
            "Betfair response id does not match request id"
        )
    if "error" in envelope and envelope["error"] is not None:
        if "result" in envelope:
            raise BetfairMarketBookFreshnessError(
                "Betfair response contains both error and result"
            )
        raise BetfairMarketBookFreshnessError(
            "Betfair JSON-RPC returned an error for listMarketBook"
        )
    if "result" not in envelope:
        raise BetfairMarketBookFreshnessError("Betfair response is missing result")

    rows = _base._sequence(envelope["result"], "listMarketBook result")
    if len(rows) != 1:
        raise BetfairMarketBookFreshnessError(
            "listMarketBook must return exactly the requested market"
        )
    row = _base._mapping(rows[0], "listMarketBook[0]")
    returned_market = _base._provider_text(row, "marketId", "market_id")
    if returned_market != market:
        raise BetfairMarketBookFreshnessError(
            "listMarketBook returned a different market"
        )
    if "isMarketDataDelayed" not in row:
        raise BetfairMarketBookFreshnessError(
            "isMarketDataDelayed is missing from provider response"
        )
    delayed = row["isMarketDataDelayed"]
    if type(delayed) is not bool:
        raise BetfairMarketBookFreshnessError(
            "isMarketDataDelayed must be bool"
        )

    return BetfairMarketBookDelayObservation(
        venue_id=client._venue_id,
        configured_account_ref=client._account_id,
        adapter_id=_base.ADAPTER_ID,
        adapter_version=_base.ADAPTER_VERSION,
        market_id=market,
        is_market_data_delayed=delayed,
        observed_at=observed_at,
        source_payload_sha256=source_payload_sha256,
    )


def _install_market_book_authority() -> None:
    issued: dict[int, tuple[object, str, bool]] = {}
    validate = BetfairMarketBookDelayObservation.__post_init__

    def read_market_book_delay(
        client: _base.BetfairReadOnlyClient,
        market_id: str,
    ) -> BetfairMarketBookDelayObservation:
        positive_origin = _canonical_network_transport(client)
        observation = _read_market_book_delay(client, market_id)
        key = id(observation)

        def forget(_weakref: object, *, observation_id: int = key) -> None:
            issued.pop(observation_id, None)

        issued[key] = (
            ref(observation, forget),
            observation._authority_fingerprint(),
            positive_origin,
        )
        return observation

    def _record(
        self: BetfairMarketBookDelayObservation,
    ) -> tuple[object, str, bool]:
        validate(self)
        record = issued.get(id(self))
        if record is None or record[0]() is not self:
            raise BetfairMarketBookFreshnessError(
                "market-book observation was not issued by canonical Betfair adapter"
            )
        if record[1] != self._authority_fingerprint():
            raise BetfairMarketBookFreshnessError(
                "market-book observation changed after canonical adapter capture"
            )
        return record

    def assert_authoritative(self: BetfairMarketBookDelayObservation) -> None:
        record = _record(self)
        if not self.is_market_data_delayed and not record[2]:
            raise BetfairMarketBookFreshnessError(
                "positive market-book observation lacks canonical production network origin"
            )

    def assert_positive_authoritative(
        self: BetfairMarketBookDelayObservation,
    ) -> None:
        record = _record(self)
        if not record[2]:
            raise BetfairMarketBookFreshnessError(
                "positive market-book observation lacks canonical production network origin"
            )

    globals()["read_market_book_delay"] = read_market_book_delay
    BetfairMarketBookDelayObservation.assert_authoritative = assert_authoritative
    BetfairMarketBookDelayObservation.assert_positive_authoritative = (
        assert_positive_authoritative
    )


_install_market_book_authority()
del _install_market_book_authority

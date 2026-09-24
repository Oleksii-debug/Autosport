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
from secrets import token_hex
from weakref import ref

from . import betfair_account_readonly as _base


_LIST_MARKET_BOOK = "SportsAPING/v1.0/listMarketBook"
_GET_DEVELOPER_APP_KEYS = "AccountAPING/v1.0/getDeveloperAppKeys"
_CANONICAL_NETWORK_POST = _base.UrllibBetfairHttpTransport.post


class BetfairMarketBookFreshnessError(_base.BetfairReadOnlyError):
    """Raised when canonical MarketBook delay evidence cannot be obtained."""


@dataclass(frozen=True, slots=True)
class _AuthenticatedApplicationKeyContext:
    """Secret-free provider metadata for the exact application key in use."""

    developer_app_id: int
    application_version_id: int
    delay_data: bool
    active: bool
    owner_managed: bool
    authenticated_context_sha256: str


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise BetfairMarketBookFreshnessError(
            f"{field} must be a positive exact integer"
        )
    return value


def _authenticated_application_key_context(
    client: _base.BetfairReadOnlyClient,
    *,
    credentials: object,
) -> _AuthenticatedApplicationKeyContext:
    """Resolve the exact current App Key through authenticated provider metadata.

    The provider response contains the raw Application Key. It is compared only
    in memory and is never copied into evidence, logs, or authority digests.
    """

    if client._credentials is not credentials:
        raise BetfairMarketBookFreshnessError(
            "Betfair authenticated context changed before app-key verification"
        )
    request_id = client._next_request_id()
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": _GET_DEVELOPER_APP_KEYS,
            "params": {},
            "id": request_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = client._transport.post(
        _base.ACCOUNT_JSON_RPC_ENDPOINT,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Authentication": client._credentials.session_token,
        },
        body=body,
        timeout_seconds=client._timeout_seconds,
    )
    if not isinstance(payload, bytes):
        raise BetfairMarketBookFreshnessError(
            "Betfair developer-app transport must return bytes"
        )
    decoded = _base._decode_json(payload)
    envelope = _base._mapping(decoded, "getDeveloperAppKeys response")
    if envelope.get("jsonrpc") != "2.0":
        raise BetfairMarketBookFreshnessError(
            "getDeveloperAppKeys response has invalid jsonrpc version"
        )
    response_id = envelope.get("id")
    if type(response_id) is not int or response_id != request_id:
        raise BetfairMarketBookFreshnessError(
            "getDeveloperAppKeys response id does not match request id"
        )
    if "error" in envelope and envelope["error"] is not None:
        if "result" in envelope:
            raise BetfairMarketBookFreshnessError(
                "getDeveloperAppKeys response contains both error and result"
            )
        raise BetfairMarketBookFreshnessError(
            "Betfair JSON-RPC returned an error for getDeveloperAppKeys"
        )
    if "result" not in envelope:
        raise BetfairMarketBookFreshnessError(
            "getDeveloperAppKeys response is missing result"
        )

    applications = _base._sequence(
        envelope["result"], "getDeveloperAppKeys result"
    )
    matches: list[tuple[int, int, bool, bool, bool]] = []
    expected_key = client._credentials.application_key
    for app_index, raw_app in enumerate(applications):
        app = _base._mapping(
            raw_app, f"getDeveloperAppKeys result[{app_index}]"
        )
        app_id = _positive_int(app.get("appId"), "developer_app_id")
        versions = _base._sequence(
            app.get("appVersions"),
            f"getDeveloperAppKeys result[{app_index}].appVersions",
        )
        for version_index, raw_version in enumerate(versions):
            version = _base._mapping(
                raw_version,
                (
                    "getDeveloperAppKeys "
                    f"result[{app_index}].appVersions[{version_index}]"
                ),
            )
            application_key = _base._provider_text(
                version,
                "applicationKey",
                "application_key",
            )
            if application_key != expected_key:
                continue
            version_id = _positive_int(
                version.get("versionId"), "application_version_id"
            )
            delay_data = _base._provider_bool(version, "delayData")
            active = _base._provider_bool(version, "active")
            owner_managed = _base._provider_bool(version, "ownerManaged")
            matches.append(
                (app_id, version_id, delay_data, active, owner_managed)
            )

    if len(matches) != 1:
        raise BetfairMarketBookFreshnessError(
            "authenticated Application Key must match exactly one provider app version"
        )
    if client._credentials is not credentials:
        raise BetfairMarketBookFreshnessError(
            "Betfair authenticated context changed during app-key verification"
        )

    app_id, version_id, delay_data, active, owner_managed = matches[0]
    # A random process-local nonce makes this identity session-instance scoped
    # without hashing or persisting either credential secret. Positive evidence
    # is deliberately not restart-authoritative and must be reacquired.
    context_payload = {
        "schema": "autosport.betfair_authenticated_data_context",
        "schema_version": 1,
        "provider": "betfair",
        "account_endpoint": _base.ACCOUNT_JSON_RPC_ENDPOINT,
        "betting_endpoint": _base.BETTING_JSON_RPC_ENDPOINT,
        "adapter_id": _base.ADAPTER_ID,
        "adapter_version": _base.ADAPTER_VERSION,
        "developer_app_id": app_id,
        "application_version_id": version_id,
        "delay_data": delay_data,
        "active": active,
        "owner_managed": owner_managed,
        "session_instance_nonce": token_hex(32),
    }
    context_sha = sha256(
        json.dumps(
            context_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()
    return _AuthenticatedApplicationKeyContext(
        developer_app_id=app_id,
        application_version_id=version_id,
        delay_data=delay_data,
        active=active,
        owner_managed=owner_managed,
        authenticated_context_sha256=context_sha,
    )


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
    authenticated_context_sha256: str | None = None
    developer_app_id: int | None = None
    application_version_id: int | None = None
    application_key_delay_data: bool | None = None
    application_key_active: bool | None = None
    application_key_owner_managed: bool | None = None

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

        context_values = (
            self.authenticated_context_sha256,
            self.developer_app_id,
            self.application_version_id,
            self.application_key_delay_data,
            self.application_key_active,
            self.application_key_owner_managed,
        )
        if any(value is not None for value in context_values):
            if any(value is None for value in context_values):
                raise BetfairMarketBookFreshnessError(
                    "authenticated application-key context must be complete or absent"
                )
            _base._sha256_hex(
                self.authenticated_context_sha256,
                "authenticated_context_sha256",
            )
            _positive_int(self.developer_app_id, "developer_app_id")
            _positive_int(self.application_version_id, "application_version_id")
            for value, field in (
                (self.application_key_delay_data, "application_key_delay_data"),
                (self.application_key_active, "application_key_active"),
                (self.application_key_owner_managed, "application_key_owner_managed"),
            ):
                if type(value) is not bool:
                    raise BetfairMarketBookFreshnessError(
                        f"{field} must be exact bool"
                    )

    @property
    def application_key_class(self) -> str:
        if self.application_key_delay_data is None:
            return "unknown"
        return "delayed" if self.application_key_delay_data else "live"

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
            self.authenticated_context_sha256,
            self.developer_app_id,
            self.application_version_id,
            self.application_key_delay_data,
            self.application_key_active,
            self.application_key_owner_managed,
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
    credentials = client._credentials
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

    application_context = (
        _authenticated_application_key_context(
            client,
            credentials=credentials,
        )
        if network_origin
        else None
    )
    if network_origin and client._credentials is not credentials:
        raise BetfairMarketBookFreshnessError(
            "Betfair authenticated context changed during MarketBook capture"
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
        authenticated_context_sha256=(
            None
            if application_context is None
            else application_context.authenticated_context_sha256
        ),
        developer_app_id=(
            None if application_context is None else application_context.developer_app_id
        ),
        application_version_id=(
            None
            if application_context is None
            else application_context.application_version_id
        ),
        application_key_delay_data=(
            None if application_context is None else application_context.delay_data
        ),
        application_key_active=(
            None if application_context is None else application_context.active
        ),
        application_key_owner_managed=(
            None if application_context is None else application_context.owner_managed
        ),
    )


def _install_market_book_authority() -> None:
    issued: dict[int, tuple[object, str, bool, object, object]] = {}
    validate = BetfairMarketBookDelayObservation.__post_init__

    def read_market_book_delay(
        client: _base.BetfairReadOnlyClient,
        market_id: str,
    ) -> BetfairMarketBookDelayObservation:
        positive_origin = _canonical_network_transport(client)
        credentials = client._credentials
        observation = _read_market_book_delay(client, market_id)
        if positive_origin and client._credentials is not credentials:
            raise BetfairMarketBookFreshnessError(
                "Betfair authenticated context changed before authority registration"
            )
        key = id(observation)

        def forget(_weakref: object, *, observation_id: int = key) -> None:
            issued.pop(observation_id, None)

        issued[key] = (
            ref(observation, forget),
            observation._authority_fingerprint(),
            positive_origin,
            client,
            credentials,
        )
        return observation

    def _record(
        self: BetfairMarketBookDelayObservation,
    ) -> tuple[object, str, bool, object, object]:
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

    def _assert_positive_record(
        self: BetfairMarketBookDelayObservation,
        record: tuple[object, str, bool, object, object],
    ) -> None:
        if not record[2]:
            raise BetfairMarketBookFreshnessError(
                "positive market-book observation lacks canonical production network origin"
            )
        if self.authenticated_context_sha256 is None:
            raise BetfairMarketBookFreshnessError(
                "positive market-book observation lacks authenticated app-key context"
            )
        if self.application_key_class != "live":
            raise BetfairMarketBookFreshnessError(
                "positive market-book observation requires provider LIVE application-key tier"
            )
        if self.application_key_active is not True:
            raise BetfairMarketBookFreshnessError(
                "positive market-book observation requires active provider application key"
            )
        client = record[3]
        credentials = record[4]
        if (
            type(client) is not _base.BetfairReadOnlyClient
            or client._credentials is not credentials
            or not _canonical_network_transport(client)
        ):
            raise BetfairMarketBookFreshnessError(
                "authenticated Betfair context changed after MarketBook capture"
            )
        if self.is_market_data_delayed:
            raise BetfairMarketBookFreshnessError(
                "positive market-book observation is provider-delayed"
            )

    def assert_authoritative(self: BetfairMarketBookDelayObservation) -> None:
        record = _record(self)
        if not self.is_market_data_delayed:
            _assert_positive_record(self, record)

    def assert_positive_authoritative(
        self: BetfairMarketBookDelayObservation,
    ) -> None:
        _assert_positive_record(self, _record(self))

    globals()["read_market_book_delay"] = read_market_book_delay
    BetfairMarketBookDelayObservation.assert_authoritative = assert_authoritative
    BetfairMarketBookDelayObservation.assert_positive_authoritative = (
        assert_positive_authoritative
    )


_install_market_book_authority()
del _install_market_book_authority

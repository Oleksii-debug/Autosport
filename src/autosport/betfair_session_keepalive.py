"""One-shot Betfair authenticated-session keepAlive observation.

This module proves only that the exact current product-owned authenticated Betfair
session received one canonical provider keepAlive SUCCESS response.  It does not
schedule future keepAlive requests, infer liveness from unrelated API activity, log
in again, authorize wagers, or persist session authority across restart.

Raw application keys and session tokens stay in memory and are never included in
public observation evidence, hashes, reprs, or durable state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
from secrets import token_bytes
import ssl
from threading import RLock
from types import MappingProxyType
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from weakref import ReferenceType, ref

from .betfair_account_readonly import BetfairReadOnlyClient, BetfairSessionCredentials
from .betfair_session_origin import (
    BetfairAuthenticatedJurisdiction,
    BetfairLoginJurisdiction,
    require_authoritative_betfair_authenticated_jurisdiction,
)


VENUE_ID = "betfair"
KEEPALIVE_SCHEMA = "autosport.betfair_session_keepalive_observation"
KEEPALIVE_SCHEMA_VERSION = 1
_MAX_RESPONSE_BYTES = 64 * 1024

_KEEPALIVE_ENDPOINTS: Mapping[BetfairLoginJurisdiction, str] = MappingProxyType(
    {
        BetfairLoginJurisdiction.GLOBAL_COM: "https://identitysso.betfair.com/api/keepAlive",
        BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND: "https://identitysso.betfair.com.au/api/keepAlive",
        BetfairLoginJurisdiction.ITALY: "https://identitysso.betfair.it/api/keepAlive",
        BetfairLoginJurisdiction.SPAIN: "https://identitysso.betfair.es/api/keepAlive",
        BetfairLoginJurisdiction.ROMANIA: "https://identitysso.betfair.ro/api/keepAlive",
    }
)
_KEEPALIVE_KEYS = frozenset({"token", "product", "status", "error"})
_KEEPALIVE_ERRORS = frozenset(
    {"INPUT_VALIDATION_ERROR", "INTERNAL_ERROR", "NO_SESSION"}
)


class BetfairSessionKeepAliveError(RuntimeError):
    """Raised when one keepAlive observation cannot be trusted safely."""


@dataclass(frozen=True, slots=True, repr=False)
class BetfairKeepAliveResponse:
    """Memory-only parsed provider response.

    token and product are intentionally retained only long enough to bind the provider
    response back to the exact credentials that initiated the request.  repr/str never
    reveal either value.
    """

    token: str
    product: str
    status: str
    error: str
    response_sha256: str

    def __post_init__(self) -> None:
        _secret_text(self.token, "token", allow_empty=self.status == "FAIL")
        _secret_text(self.product, "product", allow_empty=self.status == "FAIL")
        if self.status not in {"SUCCESS", "FAIL"}:
            raise BetfairSessionKeepAliveError(
                "keepAlive status must be SUCCESS or FAIL"
            )
        if type(self.error) is not str:
            raise BetfairSessionKeepAliveError("keepAlive error must be text")
        if self.status == "SUCCESS":
            if self.error != "":
                raise BetfairSessionKeepAliveError(
                    "successful keepAlive response must have empty error"
                )
            if not self.token or not self.product:
                raise BetfairSessionKeepAliveError(
                    "successful keepAlive response must echo token and product"
                )
        elif self.error not in _KEEPALIVE_ERRORS:
            raise BetfairSessionKeepAliveError(
                "failed keepAlive response has undocumented error code"
            )
        _sha256_hex(self.response_sha256, "response_sha256")

    def __repr__(self) -> str:
        return (
            "BetfairKeepAliveResponse("
            "token=<redacted>, product=<redacted>, "
            f"status={self.status!r}, error={self.error!r}, "
            f"response_sha256={self.response_sha256!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairSessionKeepAliveObservation:
    """Secret-free evidence for one product-issued keepAlive SUCCESS."""

    venue_id: str
    session_context_id: str
    jurisdiction: BetfairLoginJurisdiction
    endpoint: str
    observed_at: datetime
    response_sha256: str

    def __post_init__(self) -> None:
        if self.venue_id != VENUE_ID:
            raise BetfairSessionKeepAliveError("venue_id is product-owned")
        _text(self.session_context_id, "session_context_id")
        if type(self.jurisdiction) is not BetfairLoginJurisdiction:
            raise BetfairSessionKeepAliveError(
                "jurisdiction must be exact BetfairLoginJurisdiction"
            )
        if self.endpoint != keepalive_endpoint(self.jurisdiction):
            raise BetfairSessionKeepAliveError(
                "keepAlive endpoint does not match jurisdiction"
            )
        _utc(self.observed_at, "observed_at")
        _sha256_hex(self.response_sha256, "response_sha256")

    @property
    def observation_id(self) -> str:
        payload = {
            "schema": KEEPALIVE_SCHEMA,
            "schema_version": KEEPALIVE_SCHEMA_VERSION,
            "venue_id": self.venue_id,
            "session_context_id": self.session_context_id,
            "jurisdiction": self.jurisdiction.value,
            "endpoint": self.endpoint,
            "observed_at": _instant(self.observed_at),
            "response_sha256": self.response_sha256,
        }
        return sha256(_canonical_json(payload)).hexdigest()

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def funds_authorized(self) -> bool:
        return False

    @property
    def settlement_authorized(self) -> bool:
        return False

    @property
    def durable_restart_authority(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class _IssuedKeepAliveRecord:
    value_ref: ReferenceType[BetfairSessionKeepAliveObservation]
    jurisdiction_ref: ReferenceType[BetfairAuthenticatedJurisdiction]
    client_ref: ReferenceType[BetfairReadOnlyClient]
    observation_id: str
    credential_binding: bytes


class UrllibBetfairKeepAliveTransport:
    """Canonical TLS/no-redirect transport for the identity SSO keepAlive call."""

    def __init__(self, *, max_response_bytes: int = _MAX_RESPONSE_BYTES) -> None:
        if (
            type(max_response_bytes) is not int
            or max_response_bytes < 1024
            or max_response_bytes > _MAX_RESPONSE_BYTES
        ):
            raise BetfairSessionKeepAliveError(
                "max_response_bytes must be an integer in [1024, 65536]"
            )
        self._max_response_bytes = max_response_bytes

    def post_keep_alive(
        self,
        endpoint: str,
        *,
        application_key: str,
        session_token: str,
        timeout_seconds: float,
    ) -> bytes:
        if endpoint not in _KEEPALIVE_ENDPOINTS.values():
            raise BetfairSessionKeepAliveError(
                "keepAlive endpoint is not canonical"
            )
        _secret_text(application_key, "application_key")
        _secret_text(session_token, "session_token")
        _positive_timeout(timeout_seconds)

        context = ssl.create_default_context()
        request = Request(
            endpoint,
            data=b"",
            method="POST",
            headers={
                "Accept": "application/json",
                "X-Application": application_key,
                "X-Authentication": session_token,
            },
        )
        opener = build_opener(
            HTTPSHandler(context=context),
            _NoRedirectHandler(),
        )
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status != 200:
                    raise BetfairSessionKeepAliveError(
                        f"Betfair keepAlive returned HTTP status {status!r}"
                    )
                final_url = getattr(response, "geturl", lambda: None)()
                if final_url != endpoint:
                    raise BetfairSessionKeepAliveError(
                        "Betfair keepAlive response did not originate from exact endpoint"
                    )
                payload = response.read(self._max_response_bytes + 1)
        except HTTPError as exc:
            raise BetfairSessionKeepAliveError(
                f"Betfair keepAlive returned HTTP status {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            raise BetfairSessionKeepAliveError(
                "Betfair keepAlive transport failed"
            ) from exc
        if len(payload) > self._max_response_bytes:
            raise BetfairSessionKeepAliveError(
                "Betfair keepAlive response exceeds safe limit"
            )
        return payload


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_CANONICAL_TRANSPORT_POST = UrllibBetfairKeepAliveTransport.post_keep_alive
_CANONICAL_TRANSPORT_POST_CODE = getattr(_CANONICAL_TRANSPORT_POST, "__code__", None)
_CANONICAL_BUILD_OPENER = build_opener
_CANONICAL_HTTPS_HANDLER = HTTPSHandler
_CANONICAL_REDIRECT_HANDLER = HTTPRedirectHandler
_CANONICAL_SSL_CONTEXT_FACTORY = ssl.create_default_context
_CANONICAL_REQUEST = Request
_CANONICAL_CLIENT_TYPE = BetfairReadOnlyClient
_CANONICAL_CREDENTIALS_TYPE = BetfairSessionCredentials
_CANONICAL_JURISDICTION_TYPE = BetfairAuthenticatedJurisdiction
_CANONICAL_JURISDICTION_REQUIRE = (
    require_authoritative_betfair_authenticated_jurisdiction
)


def keepalive_endpoint(jurisdiction: BetfairLoginJurisdiction) -> str:
    if type(jurisdiction) is not BetfairLoginJurisdiction:
        raise BetfairSessionKeepAliveError(
            "jurisdiction must be exact BetfairLoginJurisdiction"
        )
    return _KEEPALIVE_ENDPOINTS[jurisdiction]


def parse_keepalive_response(payload: bytes) -> BetfairKeepAliveResponse:
    if type(payload) is not bytes or not payload:
        raise BetfairSessionKeepAliveError(
            "keepAlive response must be non-empty bytes"
        )
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise BetfairSessionKeepAliveError(
            "keepAlive response exceeds safe limit"
        )
    document = _strict_json_object(payload)
    if frozenset(document) != _KEEPALIVE_KEYS:
        raise BetfairSessionKeepAliveError(
            "keepAlive response schema does not match provider contract"
        )
    status = document["status"]
    error = document["error"]
    token = document["token"]
    product = document["product"]
    if type(status) is not str or status not in {"SUCCESS", "FAIL"}:
        raise BetfairSessionKeepAliveError(
            "keepAlive status must be SUCCESS or FAIL"
        )
    if type(error) is not str:
        raise BetfairSessionKeepAliveError("keepAlive error must be text")
    if type(token) is not str or type(product) is not str:
        raise BetfairSessionKeepAliveError(
            "keepAlive token/product must be text"
        )
    return BetfairKeepAliveResponse(
        token=token,
        product=product,
        status=status,
        error=error,
        response_sha256=sha256(payload).hexdigest(),
    )


def validate_keepalive_success_response(
    response: BetfairKeepAliveResponse,
    *,
    credentials: BetfairSessionCredentials,
) -> BetfairKeepAliveResponse:
    """Bind a parsed SUCCESS response to exact in-memory request credentials.

    This helper is a consistency check only.  It does not issue session authority.
    """
    if type(response) is not BetfairKeepAliveResponse:
        raise BetfairSessionKeepAliveError(
            "response must be exact BetfairKeepAliveResponse"
        )
    if type(credentials) is not BetfairSessionCredentials:
        raise BetfairSessionKeepAliveError(
            "credentials must be exact BetfairSessionCredentials"
        )
    if response.status != "SUCCESS":
        raise BetfairSessionKeepAliveError(
            f"Betfair keepAlive was not successful: {response.error}"
        )
    if not hmac.compare_digest(response.token, credentials.session_token):
        raise BetfairSessionKeepAliveError(
            "keepAlive response token does not match current session"
        )
    if not hmac.compare_digest(response.product, credentials.application_key):
        raise BetfairSessionKeepAliveError(
            "keepAlive response product does not match current application"
        )
    return response


def _canonical_network_transport(transport: object) -> bool:
    if type(transport) is not UrllibBetfairKeepAliveTransport:
        return False
    if type(transport).post_keep_alive is not _CANONICAL_TRANSPORT_POST:
        return False
    if (
        getattr(type(transport).post_keep_alive, "__code__", None)
        is not _CANONICAL_TRANSPORT_POST_CODE
    ):
        return False
    if build_opener is not _CANONICAL_BUILD_OPENER:
        return False
    if HTTPSHandler is not _CANONICAL_HTTPS_HANDLER:
        return False
    if HTTPRedirectHandler is not _CANONICAL_REDIRECT_HANDLER:
        return False
    if ssl.create_default_context is not _CANONICAL_SSL_CONTEXT_FACTORY:
        return False
    if Request is not _CANONICAL_REQUEST:
        return False
    state = getattr(transport, "__dict__", None)
    return (
        type(state) is dict
        and set(state) == {"_max_response_bytes"}
        and type(state["_max_response_bytes"]) is int
        and 1024 <= state["_max_response_bytes"] <= _MAX_RESPONSE_BYTES
    )


def _build_keepalive_authority_runtime():
    """Build product issuer/verifier with process-local hidden authority state."""

    lock = RLock()
    issued: dict[int, _IssuedKeepAliveRecord] = {}
    hmac_key = token_bytes(32)

    # Pin every primitive used to mint or verify authority.  Public parsing helpers
    # remain useful for diagnostics/tests but are deliberately outside the trust root.
    client_type = BetfairReadOnlyClient
    credentials_type = BetfairSessionCredentials
    jurisdiction_type = BetfairAuthenticatedJurisdiction
    login_jurisdiction_type = BetfairLoginJurisdiction
    observation_type = BetfairSessionKeepAliveObservation
    transport_type = UrllibBetfairKeepAliveTransport
    error_type = BetfairSessionKeepAliveError
    jurisdiction_require = require_authoritative_betfair_authenticated_jurisdiction
    endpoint_table = _KEEPALIVE_ENDPOINTS
    canonical_transport_post = transport_type.post_keep_alive
    canonical_transport_post_code = getattr(canonical_transport_post, "__code__", None)
    canonical_observation_init = observation_type.__init__
    canonical_observation_post_init = observation_type.__post_init__
    canonical_build_opener = build_opener
    canonical_https_handler = HTTPSHandler
    canonical_redirect_handler = HTTPRedirectHandler
    canonical_ssl_context_factory = ssl.create_default_context
    canonical_request = Request
    redirect_handler_type = _NoRedirectHandler
    canonical_redirect_request = redirect_handler_type.redirect_request
    json_module = json
    json_loads = json.loads
    json_loads_code = getattr(json_loads, "__code__", None)
    json_decode_error = json.JSONDecodeError
    json_decoder = json.JSONDecoder
    json_decoder_init = json_decoder.__init__
    json_decoder_init_code = getattr(json_decoder_init, "__code__", None)
    json_decoder_decode = json_decoder.decode
    json_decoder_decode_code = getattr(json_decoder_decode, "__code__", None)
    json_decoder_raw_decode = json_decoder.raw_decode
    json_decoder_raw_decode_code = getattr(
        json_decoder_raw_decode, "__code__", None
    )
    # JSONDecoder.__init__ resolves its lower parser graph dynamically from
    # json.decoder globals. Capturing only JSONDecoder and its methods is not
    # sufficient: scanner.make_scanner/JSONObject/JSONArray/scanstring can be
    # rebound while every captured decoder method remains byte-identical.
    json_decoder_module = json.decoder
    json_scanner_module = json.scanner
    json_scanner_make = json_scanner_module.make_scanner
    json_scanner_make_code = getattr(json_scanner_make, "__code__", None)
    json_object_parser = json_decoder_module.JSONObject
    json_object_parser_code = getattr(json_object_parser, "__code__", None)
    json_array_parser = json_decoder_module.JSONArray
    json_array_parser_code = getattr(json_array_parser, "__code__", None)
    json_scanstring = json_decoder_module.scanstring
    json_scanstring_code = getattr(json_scanstring, "__code__", None)
    sha256_fn = sha256
    hmac_digest = hmac.digest
    hmac_compare_digest = hmac.compare_digest
    datetime_type = datetime
    utc = timezone.utc
    max_response_bytes = _MAX_RESPONSE_BYTES
    exact_keys = _KEEPALIVE_KEYS
    venue_id = VENUE_ID

    def json_executable_graph_matches() -> bool:
        return bool(
            json is json_module
            and getattr(json_module, "loads", None) is json_loads
            and getattr(json_loads, "__code__", None) is json_loads_code
            and getattr(json_module, "JSONDecodeError", None)
            is json_decode_error
            and getattr(json_module, "JSONDecoder", None) is json_decoder
            and getattr(json_module, "decoder", None) is json_decoder_module
            and getattr(json_module, "scanner", None) is json_scanner_module
            and getattr(json_decoder_module, "JSONDecodeError", None)
            is json_decode_error
            and getattr(json_decoder_module, "JSONDecoder", None) is json_decoder
            and getattr(json_decoder_module, "scanner", None)
            is json_scanner_module
            and getattr(json_scanner_module, "make_scanner", None)
            is json_scanner_make
            and getattr(json_scanner_make, "__code__", None)
            is json_scanner_make_code
            and getattr(json_decoder_module, "JSONObject", None)
            is json_object_parser
            and getattr(json_object_parser, "__code__", None)
            is json_object_parser_code
            and getattr(json_decoder_module, "JSONArray", None)
            is json_array_parser
            and getattr(json_array_parser, "__code__", None)
            is json_array_parser_code
            and getattr(json_decoder_module, "scanstring", None)
            is json_scanstring
            and getattr(json_scanstring, "__code__", None)
            is json_scanstring_code
            and getattr(json_decoder, "__init__", None) is json_decoder_init
            and getattr(json_decoder_init, "__code__", None)
            is json_decoder_init_code
            and getattr(json_decoder, "decode", None) is json_decoder_decode
            and getattr(json_decoder_decode, "__code__", None)
            is json_decoder_decode_code
            and getattr(json_decoder, "raw_decode", None)
            is json_decoder_raw_decode
            and getattr(json_decoder_raw_decode, "__code__", None)
            is json_decoder_raw_decode_code
        )

    def implementation_is_current() -> bool:
        return (
            json_executable_graph_matches()
            and BetfairReadOnlyClient is client_type
            and BetfairSessionCredentials is credentials_type
            and BetfairAuthenticatedJurisdiction is jurisdiction_type
            and BetfairLoginJurisdiction is login_jurisdiction_type
            and BetfairSessionKeepAliveObservation is observation_type
            and UrllibBetfairKeepAliveTransport is transport_type
            and transport_type.post_keep_alive is canonical_transport_post
            and getattr(transport_type.post_keep_alive, "__code__", None)
            is canonical_transport_post_code
            and require_authoritative_betfair_authenticated_jurisdiction
            is jurisdiction_require
            and _KEEPALIVE_ENDPOINTS is endpoint_table
            and VENUE_ID == venue_id
            and _NoRedirectHandler is redirect_handler_type
            and redirect_handler_type.redirect_request
            is canonical_redirect_request
            and observation_type.__init__ is canonical_observation_init
            and observation_type.__post_init__ is canonical_observation_post_init
        )

    def canonical_network_transport(transport: object) -> bool:
        if type(transport) is not transport_type:
            return False
        if type(transport).post_keep_alive is not canonical_transport_post:
            return False
        if (
            getattr(type(transport).post_keep_alive, "__code__", None)
            is not canonical_transport_post_code
        ):
            return False
        if build_opener is not canonical_build_opener:
            return False
        if HTTPSHandler is not canonical_https_handler:
            return False
        if HTTPRedirectHandler is not canonical_redirect_handler:
            return False
        if ssl.create_default_context is not canonical_ssl_context_factory:
            return False
        if Request is not canonical_request:
            return False
        state = getattr(transport, "__dict__", None)
        return (
            type(state) is dict
            and set(state) == {"_max_response_bytes"}
            and type(state["_max_response_bytes"]) is int
            and 1024 <= state["_max_response_bytes"] <= max_response_bytes
        )

    def credential_binding(credentials: BetfairSessionCredentials) -> bytes:
        if type(credentials) is not credentials_type:
            raise error_type(
                "credential binding requires canonical BetfairSessionCredentials"
            )
        try:
            material = (
                credentials.application_key.encode("utf-8")
                + b"\x00"
                + credentials.session_token.encode("utf-8")
            )
        except (AttributeError, UnicodeEncodeError) as exc:
            raise error_type("Betfair credentials are malformed") from exc
        return hmac_digest(hmac_key, material, "sha256")

    def endpoint_for(jurisdiction: BetfairLoginJurisdiction) -> str:
        if type(jurisdiction) is not login_jurisdiction_type:
            raise error_type(
                "jurisdiction must be exact BetfairLoginJurisdiction"
            )
        try:
            return endpoint_table[jurisdiction]
        except KeyError as exc:
            raise error_type(
                "keepAlive jurisdiction has no explicit endpoint contract"
            ) from exc

    def parse_success_payload(
        payload: bytes,
        *,
        credentials: BetfairSessionCredentials,
    ) -> str:
        if type(payload) is not bytes or not payload:
            raise error_type("keepAlive response must be non-empty bytes")
        if len(payload) > max_response_bytes:
            raise error_type("keepAlive response exceeds safe limit")
        try:
            raw = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise error_type("keepAlive response is not UTF-8 JSON") from exc

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise error_type(
                        f"duplicate keepAlive-response JSON key: {key}"
                    )
                result[key] = value
            return result

        def reject_constant(value: str) -> None:
            raise error_type(
                f"non-standard keepAlive-response JSON constant: {value}"
            )

        if not json_executable_graph_matches():
            raise error_type(
                "canonical keepAlive JSON executable authority changed"
            )
        try:
            document = json_loads(
                raw,
                cls=json_decoder,
                object_pairs_hook=pairs,
                parse_constant=reject_constant,
            )
        except json_decode_error as exc:
            raise error_type("keepAlive response is not valid JSON") from exc
        if not json_executable_graph_matches():
            raise error_type(
                "canonical keepAlive JSON executable authority changed"
            )
        if type(document) is not dict or frozenset(document) != exact_keys:
            raise error_type(
                "keepAlive response schema does not match provider contract"
            )
        token = document["token"]
        product = document["product"]
        status = document["status"]
        error = document["error"]
        if (
            type(token) is not str
            or type(product) is not str
            or type(status) is not str
            or type(error) is not str
        ):
            raise error_type(
                "keepAlive response fields must be exact text values"
            )
        if status != "SUCCESS":
            # NO_SESSION and every other provider failure are terminal for this
            # observation attempt; no stale positive object is issued.
            raise error_type(
                "Betfair keepAlive was not successful"
            )
        if error != "":
            raise error_type(
                "successful keepAlive response must have empty error"
            )
        if not hmac_compare_digest(token, credentials.session_token):
            raise error_type(
                "keepAlive response token does not match current session"
            )
        if not hmac_compare_digest(product, credentials.application_key):
            raise error_type(
                "keepAlive response product does not match current application"
            )
        return sha256_fn(payload).hexdigest()

    def observation_fingerprint(
        value: BetfairSessionKeepAliveObservation,
    ) -> str:
        if type(value) is not observation_type:
            raise error_type("keepAlive observation type changed")
        observed = value.observed_at
        if (
            type(observed) is not datetime_type
            or observed.tzinfo is None
            or observed.utcoffset() is None
        ):
            raise error_type("keepAlive observed_at is not timezone-aware")
        material = (
            value.venue_id,
            value.session_context_id,
            value.jurisdiction.value,
            value.endpoint,
            observed.astimezone(utc).isoformat(),
            value.response_sha256,
        )
        return sha256_fn(repr(material).encode("utf-8")).hexdigest()

    def remember(
        value: BetfairSessionKeepAliveObservation,
        jurisdiction: BetfairAuthenticatedJurisdiction,
        client: BetfairReadOnlyClient,
        credentials: BetfairSessionCredentials,
    ) -> None:
        key = id(value)

        def discard(
            dead_ref: ReferenceType[BetfairSessionKeepAliveObservation],
        ) -> None:
            with lock:
                record = issued.get(key)
                if record is not None and record.value_ref is dead_ref:
                    issued.pop(key, None)

        with lock:
            issued[key] = _IssuedKeepAliveRecord(
                value_ref=ref(value, discard),
                jurisdiction_ref=ref(jurisdiction),
                client_ref=ref(client),
                observation_id=observation_fingerprint(value),
                credential_binding=credential_binding(credentials),
            )

    def keep_alive(
        jurisdiction: BetfairAuthenticatedJurisdiction,
        *,
        client: BetfairReadOnlyClient,
        timeout_seconds: float = 10.0,
    ) -> BetfairSessionKeepAliveObservation:
        if not implementation_is_current():
            raise error_type(
                "canonical authenticated-session authority binding changed"
            )
        if type(client) is not client_type:
            raise error_type("client must be exact BetfairReadOnlyClient")
        if type(jurisdiction) is not jurisdiction_type:
            raise error_type(
                "jurisdiction must be exact BetfairAuthenticatedJurisdiction"
            )
        try:
            jurisdiction_require(jurisdiction, client=client)
        except Exception as exc:
            raise error_type(
                "keepAlive requires current authenticated jurisdiction authority"
            ) from exc

        credentials = getattr(client, "_credentials", None)
        if type(credentials) is not credentials_type:
            raise error_type(
                "authenticated client credentials are not canonical"
            )
        _positive_timeout(timeout_seconds)
        endpoint = endpoint_for(jurisdiction.jurisdiction)
        transport = transport_type()
        if not canonical_network_transport(transport):
            raise error_type(
                "canonical Betfair keepAlive transport origin is unavailable"
            )

        payload = transport.post_keep_alive(
            endpoint,
            application_key=credentials.application_key,
            session_token=credentials.session_token,
            timeout_seconds=timeout_seconds,
        )

        if not canonical_network_transport(transport):
            raise error_type(
                "canonical Betfair keepAlive transport changed during request"
            )
        try:
            jurisdiction_require(jurisdiction, client=client)
        except Exception as exc:
            raise error_type(
                "authenticated session changed during keepAlive"
            ) from exc
        # Recompute credential binding after I/O before trusting the response so
        # in-place credential rotation during the request cannot mint liveness.
        before_issue_binding = credential_binding(credentials)
        response_sha256 = parse_success_payload(
            payload,
            credentials=credentials,
        )
        if not hmac_compare_digest(
            before_issue_binding,
            credential_binding(credentials),
        ):
            raise error_type(
                "authenticated credentials changed during keepAlive"
            )

        value = observation_type(
            venue_id=venue_id,
            session_context_id=jurisdiction.session_context_id,
            jurisdiction=jurisdiction.jurisdiction,
            endpoint=endpoint,
            observed_at=datetime_type.now(utc),
            response_sha256=response_sha256,
        )
        if (
            value.endpoint != endpoint
            or value.session_context_id != jurisdiction.session_context_id
            or value.jurisdiction is not jurisdiction.jurisdiction
        ):
            raise error_type(
                "keepAlive observation construction was altered"
            )
        remember(value, jurisdiction, client, credentials)
        return value

    def is_authoritative(
        value: object,
        *,
        client: BetfairReadOnlyClient | None = None,
    ) -> bool:
        if type(value) is not observation_type or not implementation_is_current():
            return False
        with lock:
            record = issued.get(id(value))
            if record is None or record.value_ref() is not value:
                return False
            jurisdiction = record.jurisdiction_ref()
            issued_client = record.client_ref()
            if jurisdiction is None or issued_client is None:
                return False
            if client is not None and issued_client is not client:
                return False
            credentials = getattr(issued_client, "_credentials", None)
            if type(credentials) is not credentials_type:
                return False
            try:
                if not hmac_compare_digest(
                    record.observation_id,
                    observation_fingerprint(value),
                ):
                    return False
                if not hmac_compare_digest(
                    record.credential_binding,
                    credential_binding(credentials),
                ):
                    return False
                jurisdiction_require(jurisdiction, client=issued_client)
                return (
                    value.venue_id == venue_id
                    and value.session_context_id
                    == jurisdiction.session_context_id
                    and value.jurisdiction is jurisdiction.jurisdiction
                    and value.endpoint
                    == endpoint_for(jurisdiction.jurisdiction)
                )
            except Exception:
                return False

    def require(
        value: object,
        *,
        client: BetfairReadOnlyClient | None = None,
    ) -> BetfairSessionKeepAliveObservation:
        if not is_authoritative(value, client=client):
            raise error_type(
                "keepAlive observation lacks current session authority"
            )
        assert type(value) is observation_type
        return value

    return keep_alive, is_authoritative, require


(
    keep_alive_betfair_session,
    is_authoritative_betfair_session_keepalive,
    require_authoritative_betfair_session_keepalive,
) = _build_keepalive_authority_runtime()
del _build_keepalive_authority_runtime


def _strict_json_object(payload: bytes) -> dict[str, object]:
    try:
        raw = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BetfairSessionKeepAliveError(
            "keepAlive response is not UTF-8 JSON"
        ) from exc

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise BetfairSessionKeepAliveError(
                    f"duplicate keepAlive-response JSON key: {key}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise BetfairSessionKeepAliveError(
            f"non-standard keepAlive-response JSON constant: {value}"
        )

    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise BetfairSessionKeepAliveError(
            "keepAlive response is not valid JSON"
        ) from exc
    if type(parsed) is not dict:
        raise BetfairSessionKeepAliveError(
            "keepAlive response must be a JSON object"
        )
    return parsed


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairSessionKeepAliveError(
            "keepAlive observation is not canonical JSON"
        ) from exc


def _text(value: object, field: str) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise BetfairSessionKeepAliveError(
            f"{field} must be canonical non-empty text"
        )
    return value


def _secret_text(
    value: object,
    field: str,
    *,
    allow_empty: bool = False,
) -> str:
    if type(value) is not str or "\x00" in value:
        raise BetfairSessionKeepAliveError(f"{field} must be secret text")
    if not allow_empty and not value:
        raise BetfairSessionKeepAliveError(
            f"{field} must be non-empty secret text"
        )
    return value


def _sha256_hex(value: object, field: str) -> str:
    raw = _text(value, field)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise BetfairSessionKeepAliveError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return raw


def _utc(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise BetfairSessionKeepAliveError(
            f"{field} must be timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _instant(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _positive_timeout(value: object) -> float:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise BetfairSessionKeepAliveError(
            "timeout_seconds must be a positive number"
        )
    numeric = float(value)
    if numeric <= 0 or numeric == float("inf") or numeric != numeric:
        raise BetfairSessionKeepAliveError(
            "timeout_seconds must be a finite positive number"
        )
    return numeric

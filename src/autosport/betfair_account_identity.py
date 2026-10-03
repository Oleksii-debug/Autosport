"""Product-issued Betfair authenticated account/session-context identity.

The ordinary Betfair Accounts API does not expose a stable customer/account identifier
through ``getAccountDetails``.  This module therefore proves only the narrower fact
Autosport can actually establish for the personal-developer path: one account-details
observation came from one exact canonical authenticated client/session context in this
process.

The public identity intentionally does not contain application keys, session tokens,
credential hashes, configured account labels, or a claim of cross-session account
equivalence.  Persistence/copying preserves evidence bytes, not source authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import hmac
import http.client as _http_client
import json
import socket as _socket
import ssl as _ssl
import urllib.request as _urllib_request
from sys import _getframe
from secrets import token_bytes, token_hex
from threading import RLock
from weakref import ReferenceType, WeakKeyDictionary, ref

from . import betfair_account_readonly as _readonly_module
from .betfair_account_readonly import (
    BetfairAccountDetailsObservation,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
)

VENUE_ID = "betfair"
IDENTITY_SCOPE = "SESSION_CONTEXT"
IDENTITY_SCHEMA = "autosport.betfair_authenticated_account_context"
IDENTITY_SCHEMA_VERSION = 1
_CONTEXT_PREFIX = "betfair-session-context:"


class BetfairAccountIdentityError(RuntimeError):
    """Raised when authenticated account-context identity cannot be proven safely."""


class BetfairAccountIdentityMode(str, Enum):
    PERSONAL_DEVELOPER = "PERSONAL_DEVELOPER"
    LICENSED_VENDOR = "LICENSED_VENDOR"


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairAuthenticatedAccountIdentity:
    """Ephemeral process-issued identity for one exact authenticated session context."""

    venue_id: str
    mode: BetfairAccountIdentityMode
    identity_scope: str
    session_context_id: str
    currency_code: str
    account_details_sha256: str
    observed_at: str

    def __post_init__(self) -> None:
        if self.venue_id != VENUE_ID:
            raise BetfairAccountIdentityError("venue_id is product-owned")
        if self.mode is not BetfairAccountIdentityMode.PERSONAL_DEVELOPER:
            raise BetfairAccountIdentityError(
                "stable licensed-vendor account identity is not available on this authority"
            )
        if self.identity_scope != IDENTITY_SCOPE:
            raise BetfairAccountIdentityError(
                "personal-developer identity must remain session-context scoped"
            )
        if not isinstance(self.session_context_id, str) or not self.session_context_id.startswith(
            _CONTEXT_PREFIX
        ):
            raise BetfairAccountIdentityError("session_context_id is not canonical")
        _sha256_hex(
            self.session_context_id.removeprefix(_CONTEXT_PREFIX),
            "session_context_id",
        )
        _currency_code(self.currency_code)
        _sha256_hex(self.account_details_sha256, "account_details_sha256")
        _canonical_timestamp(self.observed_at)

    @property
    def stable_account_identity_proven(self) -> bool:
        return False

    @property
    def stable_account_id(self) -> None:
        return None

    @property
    def cross_session_equivalence_proven(self) -> bool:
        return False

    @property
    def identity_id(self) -> str:
        payload = {
            "schema": IDENTITY_SCHEMA,
            "schema_version": IDENTITY_SCHEMA_VERSION,
            "venue_id": self.venue_id,
            "mode": self.mode.value,
            "identity_scope": self.identity_scope,
            "session_context_id": self.session_context_id,
            "currency_code": self.currency_code,
            "account_details_sha256": self.account_details_sha256,
            "observed_at": self.observed_at,
            "stable_account_identity_proven": False,
        }
        return sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class _CanonicalClientOrigin:
    transport: object
    clock: object
    credentials: object
    credential_binding: bytes


@dataclass(slots=True)
class _ClientContextRecord:
    client_ref: ReferenceType[BetfairReadOnlyClient]
    origin: _CanonicalClientOrigin
    session_context_id: str
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class _IssuedIdentityRecord:
    value_ref: ReferenceType[BetfairAuthenticatedAccountIdentity]
    identity_id: str
    client_ref: ReferenceType[BetfairReadOnlyClient]
    session_context_id: str


def _make_account_identity_authority():
    """Build one process-local authority with closure-hidden issuance state.

    No module attribute exposes the origin registry, registration capability, or
    credential-binding key. Public functions returned by this factory share the
    hidden state and pin every authority-bearing client/network dispatch identity.
    """

    process_hmac_key = token_bytes(32)
    credentials_type = BetfairSessionCredentials
    client_type = BetfairReadOnlyClient
    transport_type = UrllibBetfairHttpTransport
    details_type = BetfairAccountDetailsObservation
    identity_type = BetfairAuthenticatedAccountIdentity
    origin_type = _CanonicalClientOrigin
    context_record_type = _ClientContextRecord
    issued_record_type = _IssuedIdentityRecord
    identity_error_type = BetfairAccountIdentityError
    readonly_error_type = BetfairReadOnlyError
    personal_mode = BetfairAccountIdentityMode.PERSONAL_DEVELOPER
    venue_id = VENUE_ID
    identity_scope = IDENTITY_SCOPE
    identity_schema = IDENTITY_SCHEMA
    identity_schema_version = IDENTITY_SCHEMA_VERSION
    context_prefix = _CONTEXT_PREFIX
    readonly_module = _readonly_module
    json_dumps = json.dumps
    sha256_fn = sha256
    hmac_digest = hmac.digest
    hmac_compare_digest = hmac.compare_digest
    token_hex_fn = token_hex
    weakref_fn = ref
    getframe_fn = _getframe
    object_id = id
    exact_type = type
    string_type = str
    integer_type = int
    tuple_type = tuple
    missing_value = object()

    canonical_client_init = client_type.__init__
    canonical_read_account_details = client_type.read_account_details
    canonical_rpc = client_type._rpc
    canonical_next_request_id = client_type._next_request_id
    canonical_observed_at = client_type._observed_at
    canonical_network_post = transport_type.post
    canonical_network_post_code = getattr(canonical_network_post, "__code__", None)
    canonical_build_opener = readonly_module.build_opener
    canonical_build_opener_code = getattr(canonical_build_opener, "__code__", None)
    canonical_rpc_code = getattr(canonical_rpc, "__code__", None)
    canonical_request_type = readonly_module.Request
    canonical_request_init = getattr(canonical_request_type, "__init__", None)
    canonical_request_init_code = getattr(canonical_request_init, "__code__", None)
    canonical_read_method_endpoint = readonly_module._READ_METHOD_ENDPOINT
    canonical_read_method_items = tuple(canonical_read_method_endpoint.items())
    canonical_account_endpoint = readonly_module.ACCOUNT_JSON_RPC_ENDPOINT
    canonical_betting_endpoint = readonly_module.BETTING_JSON_RPC_ENDPOINT
    canonical_read_method_names = tuple(
        getattr(readonly_module, name)
        for name in (
            "_GET_ACCOUNT_FUNDS",
            "_GET_ACCOUNT_DETAILS",
            "_LIST_CURRENT_ORDERS",
            "_LIST_CLEARED_ORDERS",
            "_LIST_MARKET_CATALOGUE",
        )
    )
    canonical_identity_init = identity_type.__init__
    canonical_identity_post_init = identity_type.__post_init__

    # Execution readback provider-origin authority is stricter than structural K07
    # session identity.  Freeze the Python-visible HTTPS/TLS dispatch graph that a
    # canonical urllib request traverses.  Deterministic tests may still replace
    # these surfaces to exercise parsing, but such captures cannot mint live
    # provider-origin proof.
    network_dispatch_surfaces = (
        (_urllib_request.OpenerDirector, "open", _urllib_request.OpenerDirector.open),
        (_urllib_request.HTTPSHandler, "__init__", _urllib_request.HTTPSHandler.__init__),
        (_urllib_request.HTTPSHandler, "https_open", _urllib_request.HTTPSHandler.https_open),
        (
            _urllib_request.AbstractHTTPHandler,
            "do_open",
            _urllib_request.AbstractHTTPHandler.do_open,
        ),
        (
            _http_client,
            "_create_https_context",
            getattr(_http_client, "_create_https_context", None),
        ),
        (_http_client.HTTPSConnection, "__init__", _http_client.HTTPSConnection.__init__),
        (_http_client.HTTPSConnection, "connect", _http_client.HTTPSConnection.connect),
        (_http_client.HTTPConnection, "connect", _http_client.HTTPConnection.connect),
        (_http_client.HTTPConnection, "request", _http_client.HTTPConnection.request),
        (
            _http_client.HTTPConnection,
            "_send_request",
            getattr(_http_client.HTTPConnection, "_send_request", None),
        ),
        (_http_client.HTTPConnection, "send", _http_client.HTTPConnection.send),
        (
            _http_client.HTTPConnection,
            "getresponse",
            _http_client.HTTPConnection.getresponse,
        ),
        (_ssl.SSLContext, "wrap_socket", _ssl.SSLContext.wrap_socket),
        (_ssl.SSLSocket, "_create", getattr(_ssl.SSLSocket, "_create", None)),
        (_socket, "create_connection", _socket.create_connection),
        (_socket, "getaddrinfo", _socket.getaddrinfo),
        (_socket.socket, "connect", _socket.socket.connect),
        (_socket.socket, "sendall", _socket.socket.sendall),
        (_socket.socket, "makefile", _socket.socket.makefile),
    )

    def execution_readback_network_dispatch_is_current() -> bool:
        try:
            if (
                transport_type.post is not canonical_network_post
                or getattr(canonical_network_post, "__code__", None)
                is not canonical_network_post_code
                or client_type._rpc is not canonical_rpc
                or getattr(canonical_rpc, "__code__", None) is not canonical_rpc_code
                or readonly_module.build_opener is not canonical_build_opener
                or getattr(canonical_build_opener, "__code__", None)
                is not canonical_build_opener_code
                or readonly_module.Request is not canonical_request_type
                or getattr(canonical_request_type, "__init__", None)
                is not canonical_request_init
                or getattr(canonical_request_init, "__code__", None)
                is not canonical_request_init_code
                or readonly_module._READ_METHOD_ENDPOINT
                is not canonical_read_method_endpoint
                or tuple(canonical_read_method_endpoint.items())
                != canonical_read_method_items
                or readonly_module.ACCOUNT_JSON_RPC_ENDPOINT
                != canonical_account_endpoint
                or readonly_module.BETTING_JSON_RPC_ENDPOINT
                != canonical_betting_endpoint
                or tuple(
                    getattr(readonly_module, name, missing_value)
                    for name in (
                        "_GET_ACCOUNT_FUNDS",
                        "_GET_ACCOUNT_DETAILS",
                        "_LIST_CURRENT_ORDERS",
                        "_LIST_CLEARED_ORDERS",
                        "_LIST_MARKET_CATALOGUE",
                    )
                )
                != canonical_read_method_names
            ):
                return False
            return all(
                getattr(owner, name, missing_value) is expected
                for owner, name, expected in network_dispatch_surfaces
            )
        except (AttributeError, TypeError):
            return False

    lock = RLock()
    canonical_client_origins: WeakKeyDictionary = WeakKeyDictionary()
    client_contexts: dict[int, _ClientContextRecord] = {}
    issued: dict[int, _IssuedIdentityRecord] = {}

    def validate_sha256(value: str, field: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise identity_error_type(f"{field} must be lowercase SHA-256 hex")
        return value

    def validate_currency(value: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 3
            or not value.isascii()
            or not value.isalpha()
            or value != value.upper()
        ):
            raise identity_error_type(
                "account currency_code must be three-letter uppercase ASCII"
            )
        return value

    def validate_timestamp(value: str) -> str:
        if not isinstance(value, str) or not value or value != value.strip():
            raise identity_error_type(
                "account observed_at must be canonical timestamp text"
            )
        return value

    def credential_binding(credentials: BetfairSessionCredentials) -> bytes:
        if type(credentials) is not credentials_type:
            raise identity_error_type(
                "authenticated context requires canonical BetfairSessionCredentials"
            )
        material = (
            credentials.application_key.encode("utf-8")
            + b"\x00"
            + credentials.session_token.encode("utf-8")
        )
        return hmac_digest(process_hmac_key, material, "sha256")

    def issued_identity_digest(value: BetfairAuthenticatedAccountIdentity) -> str:
        # Authority integrity must not depend on the public identity_id property
        # or module-level validation/JSON helpers after closure initialization.
        payload = {
            "schema": identity_schema,
            "schema_version": identity_schema_version,
            "venue_id": value.venue_id,
            "mode": value.mode.value,
            "identity_scope": value.identity_scope,
            "session_context_id": value.session_context_id,
            "currency_code": value.currency_code,
            "account_details_sha256": value.account_details_sha256,
            "observed_at": value.observed_at,
            "stable_account_identity_proven": False,
        }
        try:
            encoded = json_dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        except (AttributeError, TypeError, ValueError, UnicodeEncodeError) as exc:
            raise identity_error_type(
                "account identity cannot be verified as canonical JSON"
            ) from exc
        return sha256_fn(encoded).hexdigest()

    def identity_class_is_current() -> bool:
        return (
            identity_type.__init__ is canonical_identity_init
            and identity_type.__post_init__ is canonical_identity_post_init
        )

    def client_class_dispatch_is_current() -> bool:
        return (
            client_type.__init__ is canonical_client_init
            and client_type.read_account_details
            is canonical_read_account_details
            and client_type._rpc is canonical_rpc
            and client_type._next_request_id
            is canonical_next_request_id
            and client_type._observed_at is canonical_observed_at
            and transport_type.post is canonical_network_post
            and readonly_module.build_opener is canonical_build_opener
        )

    def client_dispatch_is_current(client: BetfairReadOnlyClient) -> bool:
        if not client_class_dispatch_is_current():
            return False
        instance_dict = getattr(client, "__dict__", None)
        if type(instance_dict) is not dict:
            return False
        return not any(
            name in instance_dict
            for name in (
                "read_account_details",
                "_rpc",
                "_next_request_id",
                "_observed_at",
            )
        )

    def canonical_network_transport(transport: object) -> bool:
        if not client_class_dispatch_is_current():
            return False
        if type(transport) is not transport_type:
            return False
        transport_dict = getattr(transport, "__dict__", None)
        if type(transport_dict) is not dict:
            return False
        if set(transport_dict) != {"_max_response_bytes"}:
            return False
        max_response_bytes = transport_dict.get("_max_response_bytes")
        return type(max_response_bytes) is int and max_response_bytes > 0

    def origin_matches(
        origin: _CanonicalClientOrigin,
        client: BetfairReadOnlyClient,
    ) -> bool:
        try:
            if (
                type(client) is not client_type
                or not client_dispatch_is_current(client)
                or not canonical_network_transport(client._transport)
                or client._transport is not origin.transport
                or client._clock is not origin.clock
                or client._credentials is not origin.credentials
                or client._venue_id != venue_id
            ):
                return False
            return hmac_compare_digest(
                origin.credential_binding,
                credential_binding(client._credentials),
            )
        except (AttributeError, TypeError, identity_error_type):
            return False

    def context_is_current(
        record: _ClientContextRecord,
        client: BetfairReadOnlyClient,
        *,
        revoke_on_failure: bool,
    ) -> bool:
        if record.revoked:
            return False
        valid = record.client_ref() is client and origin_matches(record.origin, client)
        if not valid and revoke_on_failure:
            record.revoked = True
        return valid

    def context_for(client: BetfairReadOnlyClient) -> _ClientContextRecord:
        with lock:
            origin = canonical_client_origins.get(client)
            if origin is None:
                raise identity_error_type(
                    "account identity requires product-owned Betfair transport/clock origin"
                )
            existing = client_contexts.get(id(client))
            if existing is not None:
                if existing.client_ref() is not client:
                    client_contexts.pop(id(client), None)
                elif not context_is_current(
                    existing,
                    client,
                    revoke_on_failure=True,
                ):
                    raise identity_error_type(
                        "authenticated client/session context was rotated or mutated"
                    )
                else:
                    return existing

            if not origin_matches(origin, client):
                raise identity_error_type(
                    "authenticated client origin changed before identity issuance"
                )
            identity = id(client)

            def discard(dead_ref: ReferenceType[BetfairReadOnlyClient]) -> None:
                with lock:
                    record = client_contexts.get(identity)
                    if record is not None and record.client_ref is dead_ref:
                        client_contexts.pop(identity, None)

            record = context_record_type(
                client_ref=weakref_fn(client, discard),
                origin=origin,
                session_context_id=context_prefix + token_hex_fn(32),
            )
            client_contexts[identity] = record
            return record

    def remember_issued(
        value: BetfairAuthenticatedAccountIdentity,
        client: BetfairReadOnlyClient,
        context: _ClientContextRecord,
    ) -> None:
        identity = id(value)

        def discard(
            dead_ref: ReferenceType[BetfairAuthenticatedAccountIdentity],
        ) -> None:
            with lock:
                record = issued.get(identity)
                if record is not None and record.value_ref is dead_ref:
                    issued.pop(identity, None)

        with lock:
            issued[identity] = issued_record_type(
                value_ref=weakref_fn(value, discard),
                identity_id=issued_identity_digest(value),
                client_ref=weakref_fn(client),
                session_context_id=context.session_context_id,
            )

    def build_client(
        credentials: BetfairSessionCredentials,
        *,
        timeout_seconds: float = 10.0,
        account_label: str = "authenticated-account",
    ) -> BetfairReadOnlyClient:
        """Create one canonical client whose authenticated origin K07 may attest."""

        if type(credentials) is not credentials_type:
            raise identity_error_type(
                "authenticated context requires canonical BetfairSessionCredentials"
            )
        if not client_class_dispatch_is_current():
            raise identity_error_type(
                "canonical Betfair client/network implementation changed"
            )
        binding = credential_binding(credentials)
        client = client_type(
            credentials,
            timeout_seconds=timeout_seconds,
            venue_id=venue_id,
            account_id=account_label,
        )
        if (
            type(client) is not client_type
            or not client_dispatch_is_current(client)
            or not canonical_network_transport(client._transport)
            or client._credentials is not credentials
        ):
            raise identity_error_type(
                "canonical Betfair client factory produced invalid origin"
            )
        origin = origin_type(
            client._transport,
            client._clock,
            client._credentials,
            binding,
        )
        with lock:
            canonical_client_origins[client] = origin
        return client

    def resolve_identity(
        client: BetfairReadOnlyClient,
        *,
        mode: BetfairAccountIdentityMode = BetfairAccountIdentityMode.PERSONAL_DEVELOPER,
    ) -> BetfairAuthenticatedAccountIdentity:
        """Issue identity for the exact current authenticated client/session context."""

        if type(client) is not client_type:
            raise identity_error_type(
                "account identity requires exact canonical BetfairReadOnlyClient"
            )
        if mode is not personal_mode:
            raise identity_error_type(
                "LICENSED_VENDOR stable account identity is not implemented on canonical transport"
            )

        context = context_for(client)
        if canonical_read_account_details is not client_type.read_account_details:
            raise identity_error_type(
                "canonical Betfair account-details read authority changed"
            )
        try:
            # Invoke the import-time canonical implementation directly. The method
            # itself dispatches through _rpc/_next_request_id/_observed_at, whose
            # class identities and instance-shadow absence are fenced above and
            # rechecked after acquisition.
            details = canonical_read_account_details(client)
        except readonly_error_type as exc:
            raise identity_error_type(
                "authenticated Betfair account-details acquisition failed"
            ) from exc
        if type(details) is not details_type:
            raise identity_error_type(
                "account-details acquisition returned non-canonical evidence"
            )
        if not context_is_current(context, client, revoke_on_failure=True):
            raise identity_error_type(
                "authenticated client/session context changed during account-details acquisition"
            )

        expected_currency = validate_currency(details.currency_code)
        expected_details_sha256 = validate_sha256(
            details.evidence.source_payload_sha256,
            "account_details_sha256",
        )
        expected_observed_at = validate_timestamp(details.evidence.observed_at)
        if not identity_class_is_current():
            raise identity_error_type(
                "authenticated account identity implementation changed"
            )
        value = identity_type(
            venue_id=venue_id,
            mode=personal_mode,
            identity_scope=identity_scope,
            session_context_id=context.session_context_id,
            currency_code=expected_currency,
            account_details_sha256=expected_details_sha256,
            observed_at=expected_observed_at,
        )
        if (
            not identity_class_is_current()
            or value.venue_id != venue_id
            or value.mode is not personal_mode
            or value.identity_scope != identity_scope
            or value.session_context_id != context.session_context_id
            or value.currency_code != expected_currency
            or value.account_details_sha256 != expected_details_sha256
            or value.observed_at != expected_observed_at
        ):
            raise identity_error_type(
                "authenticated account identity construction was altered"
            )
        remember_issued(value, client, context)
        return value

    def is_authoritative(
        value: object,
        *,
        client: BetfairReadOnlyClient | None = None,
    ) -> bool:
        if type(value) is not identity_type or not identity_class_is_current():
            return False
        with lock:
            record = issued.get(id(value))
            if record is None or record.value_ref() is not value:
                return False
            try:
                current_identity_digest = issued_identity_digest(value)
            except identity_error_type:
                return False
            if not hmac_compare_digest(record.identity_id, current_identity_digest):
                return False
            issued_client = record.client_ref()
            if issued_client is None:
                return False
            if client is not None and issued_client is not client:
                return False
            context = client_contexts.get(id(issued_client))
            if (
                context is None
                or context.client_ref() is not issued_client
                or context.session_context_id != record.session_context_id
            ):
                return False
            return context_is_current(
                context,
                issued_client,
                revoke_on_failure=True,
            )

    def require_authoritative(
        value: object,
        *,
        client: BetfairReadOnlyClient | None = None,
    ) -> BetfairAuthenticatedAccountIdentity:
        if not is_authoritative(value, client=client):
            raise identity_error_type(
                "Betfair account identity lacks current authenticated-context authority"
            )
        assert type(value) is identity_type
        return value

    def bind_execution_readback_origin_authority(
        caller_code: object,
        caller_witnesses: tuple[tuple[str, object], ...],
    ):
        """Bind one readback-origin proof issuer to the canonical acquisition wrapper.

        The returned issuer can mint a proof only while called from the exact code
        object supplied by the readback composition layer.  The proof is keyed by
        K07's existing process authority and binds the exact live client/session,
        evidence object identity and immutable capture fingerprint.
        """

        if not hasattr(caller_code, "co_code"):
            raise identity_error_type(
                "execution readback origin requires canonical caller code"
            )
        if (
            exact_type(caller_witnesses) is not tuple_type
            or not caller_witnesses
            or any(
                exact_type(item) is not tuple_type
                or len(item) != 2
                or exact_type(item[0]) is not string_type
                or not item[0]
                for item in caller_witnesses
            )
        ):
            raise identity_error_type(
                "execution readback origin caller witnesses are invalid"
            )

        caller_marker = "__AUTOSPORT_BETFAIR_READBACK_ORIGIN_CALLER_CODE__"

        def issue_origin(
            client: BetfairReadOnlyClient,
            identity: BetfairAuthenticatedAccountIdentity,
            *,
            capture_identity: int,
            capture_fingerprint: str,
        ) -> str:
            expected_caller = "__AUTOSPORT_BETFAIR_READBACK_ORIGIN_CALLER_CODE__"
            caller_frame = getframe_fn(1)
            if caller_frame.f_code is not expected_caller:
                raise identity_error_type(
                    "execution readback origin proof may only be issued by canonical acquisition"
                )
            caller_locals = caller_frame.f_locals
            for witness_name, witness_value in caller_witnesses:
                if caller_locals.get(witness_name, missing_value) is not witness_value:
                    raise identity_error_type(
                        "execution readback origin caller composition changed"
                    )
            if not execution_readback_network_dispatch_is_current():
                raise identity_error_type(
                    "execution readback provider-network dispatch is not canonical"
                )
            require_authoritative(identity, client=client)
            if (
                exact_type(capture_identity) is not integer_type
                or capture_identity <= 0
                or exact_type(capture_fingerprint) is not string_type
            ):
                raise identity_error_type(
                    "execution readback origin proof inputs are invalid"
                )
            validate_sha256(capture_fingerprint, "capture_fingerprint")
            context = context_for(client)
            if context.session_context_id != identity.session_context_id:
                raise identity_error_type(
                    "execution readback origin session context changed"
                )
            payload = json_dumps(
                {
                    "schema": "autosport.betfair_execution_readback_origin",
                    "schema_version": 1,
                    "client_identity": object_id(client),
                    "session_context_id": context.session_context_id,
                    "account_identity_id": identity.identity_id,
                    "capture_identity": capture_identity,
                    "capture_fingerprint": capture_fingerprint,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
            return hmac_digest(process_hmac_key, payload, "sha256").hex()

        constants = issue_origin.__code__.co_consts
        if sum(item == caller_marker for item in constants) != 1:
            raise identity_error_type(
                "execution readback origin caller anchor is ambiguous"
            )
        issue_origin.__code__ = issue_origin.__code__.replace(
            co_consts=tuple(
                caller_code if item == caller_marker else item
                for item in constants
            )
        )

        def verify_origin(
            proof: object,
            client: BetfairReadOnlyClient,
            identity: BetfairAuthenticatedAccountIdentity,
            *,
            capture_identity: int,
            capture_fingerprint: str,
        ) -> bool:
            try:
                if not execution_readback_network_dispatch_is_current():
                    return False
                require_authoritative(identity, client=client)
                if (
                    exact_type(proof) is not string_type
                    or exact_type(capture_identity) is not integer_type
                    or capture_identity <= 0
                    or exact_type(capture_fingerprint) is not string_type
                ):
                    return False
                validate_sha256(capture_fingerprint, "capture_fingerprint")
                context = context_for(client)
                if context.session_context_id != identity.session_context_id:
                    return False
                payload = json_dumps(
                    {
                        "schema": "autosport.betfair_execution_readback_origin",
                        "schema_version": 1,
                        "client_identity": object_id(client),
                        "session_context_id": context.session_context_id,
                        "account_identity_id": identity.identity_id,
                        "capture_identity": capture_identity,
                        "capture_fingerprint": capture_fingerprint,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                ).encode("utf-8")
                expected = hmac_digest(process_hmac_key, payload, "sha256").hex()
                return hmac_compare_digest(proof, expected)
            except (AttributeError, TypeError, ValueError, identity_error_type):
                return False

        return issue_origin, verify_origin, execution_readback_network_dispatch_is_current

    return (
        build_client,
        resolve_identity,
        is_authoritative,
        require_authoritative,
        bind_execution_readback_origin_authority,
    )


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairAccountIdentityError("account identity is not canonical JSON") from exc


def _sha256_hex(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BetfairAccountIdentityError(f"{field} must be lowercase SHA-256 hex")
    return value


def _currency_code(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or value != value.upper()
    ):
        raise BetfairAccountIdentityError(
            "account currency_code must be three-letter uppercase ASCII"
        )
    return value


def _canonical_timestamp(value: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairAccountIdentityError("account observed_at must be canonical timestamp text")
    # The canonical client already validates evidence timestamps. Keep the exact
    # provider-observation text so identity binds the source evidence byte-for-byte.
    return value


(
    build_betfair_authenticated_client,
    resolve_betfair_authenticated_account_identity,
    is_authoritative_betfair_account_identity,
    require_authoritative_betfair_account_identity,
    _bind_betfair_execution_readback_origin_authority,
) = _make_account_identity_authority()
del _make_account_identity_authority

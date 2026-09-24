"""Product-issued Betfair authenticated account/session-context identity.

The ordinary Betfair Accounts API does not expose a stable customer/account identifier
through ``getAccountDetails``.  This module therefore proves only the narrower fact
Autosport can actually establish for the personal-developer path: one account-details
observation is bound to one exact canonical authenticated client/session context in
this process.

That process-local authority deliberately stops above remote transport origin.  The
same Python process can substitute I/O below the canonical HTTP/opener stack, so K07
must not be consumed as proof that account-details bytes causally came from remote
Betfair.  Remote-provider origin and provider account-details origin remain explicitly
unproven here and require a separate operational/acquisition authority.

The public identity intentionally does not contain application keys, session tokens,
credential hashes, configured account labels, or a claim of cross-session account
equivalence.  Persistence/copying preserves evidence bytes, not source authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
import hmac
import json
import urllib.request as _urllib_request
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
IDENTITY_SCHEMA_VERSION = 2
_CONTEXT_PREFIX = "betfair-session-context:"


class BetfairAccountIdentityError(RuntimeError):
    """Raised when authenticated account-context identity cannot be proven safely."""


class BetfairAccountIdentityMode(str, Enum):
    PERSONAL_DEVELOPER = "PERSONAL_DEVELOPER"
    LICENSED_VENDOR = "LICENSED_VENDOR"


def _identity_projection_material(
    schema: str,
    schema_version: int,
    venue_id: str,
    mode: str,
    identity_scope: str,
    session_context_id: str,
    currency_code: str,
    account_details_sha256: str,
    observed_at: str,
) -> bytes:
    """Encode K07 identity fields without a mutable JSON codec dependency.

    The length-prefixed field order is versioned by schema and schema_version.
    This helper deliberately depends only on immutable scalar inputs and Python
    built-ins; the authority closure pins both its function identity and bytecode.
    """

    parts = (
        schema,
        str(schema_version),
        venue_id,
        mode,
        identity_scope,
        session_context_id,
        currency_code,
        account_details_sha256,
        observed_at,
        "false",
        "false",
        "false",
    )
    encoded = bytearray()
    for part in parts:
        raw = part.encode("utf-8")
        encoded.extend(len(raw).to_bytes(8, "big", signed=False))
        encoded.extend(raw)
    return bytes(encoded)


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
    def remote_provider_origin_proven(self) -> bool:
        """K07 alone does not prove below-process remote Betfair transport origin."""
        return False

    @property
    def provider_account_details_origin_proven(self) -> bool:
        """Account-details bytes are not independently attested as remote-provider bytes."""
        return False

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
        material = _identity_projection_material(
            IDENTITY_SCHEMA,
            IDENTITY_SCHEMA_VERSION,
            self.venue_id,
            self.mode.value,
            self.identity_scope,
            self.session_context_id,
            self.currency_code,
            self.account_details_sha256,
            self.observed_at,
        )
        return sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class _CanonicalClientOrigin:
    transport: object
    opener: object
    opener_handlers: tuple[object, ...]
    opener_dispatch: tuple[tuple[str, object, tuple[object, ...]], ...]
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
    evidence_type = readonly_module.BetfairEvidence
    rpc_result_type = readonly_module._RpcResult
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
    identity_projection_material = _identity_projection_material
    identity_projection_material_code = getattr(
        identity_projection_material, "__code__", None
    )
    sha256_fn = sha256
    hmac_digest = hmac.digest
    hmac_compare_digest = hmac.compare_digest
    token_hex_fn = token_hex
    weakref_fn = ref

    canonical_client_init = client_type.__init__
    canonical_read_account_details = client_type.read_account_details
    canonical_rpc = client_type._rpc
    canonical_next_request_id = client_type._next_request_id
    canonical_observed_at = client_type._observed_at
    canonical_transport_init = transport_type.__init__
    canonical_network_post = transport_type.post
    redirect_handler_type = readonly_module._RejectAuthenticatedRedirects
    canonical_redirect_request = redirect_handler_type.redirect_request
    opener_type = _urllib_request.OpenerDirector
    http_redirect_handler_type = _urllib_request.HTTPRedirectHandler
    https_handler_type = _urllib_request.HTTPSHandler
    abstract_http_handler_type = _urllib_request.AbstractHTTPHandler
    http_error_processor_type = _urllib_request.HTTPErrorProcessor
    canonical_opener_open = opener_type.open
    canonical_opener_internal_open = opener_type._open
    canonical_opener_call_chain = opener_type._call_chain
    canonical_opener_error = opener_type.error
    canonical_https_open = https_handler_type.https_open
    canonical_https_request = https_handler_type.https_request
    canonical_do_open = abstract_http_handler_type.do_open
    canonical_https_response = http_error_processor_type.https_response
    canonical_details_init = details_type.__init__
    canonical_details_post_init = details_type.__post_init__
    canonical_evidence_init = evidence_type.__init__
    canonical_evidence_post_init = evidence_type.__post_init__
    canonical_rpc_result_init = rpc_result_type.__init__
    canonical_identity_init = identity_type.__init__
    canonical_identity_post_init = identity_type.__post_init__
    semantic_property_bindings = tuple(
        (
            name,
            descriptor,
            descriptor.fget,
            getattr(descriptor.fget, "__code__", None),
        )
        for name in (
            "remote_provider_origin_proven",
            "provider_account_details_origin_proven",
            "stable_account_identity_proven",
            "stable_account_id",
            "cross_session_equivalence_proven",
            "identity_id",
        )
        for descriptor in (identity_type.__dict__[name],)
    )
    readonly_globals = canonical_read_account_details.__globals__
    function_type = type(canonical_read_account_details)
    missing_global = object()

    def capture_readonly_dependencies(
        *roots: object,
    ) -> tuple[tuple[str, object, object | None], ...]:
        """Freeze the application-level dependency graph of canonical acquisition functions."""
        captured: dict[str, tuple[object, object | None]] = {}
        pending = list(roots)
        visited: set[int] = set()
        while pending:
            candidate = pending.pop()
            if type(candidate) is not function_type:
                continue
            candidate_id = id(candidate)
            if candidate_id in visited:
                continue
            visited.add(candidate_id)
            if candidate.__globals__ is not readonly_globals:
                continue
            for name in candidate.__code__.co_names:
                if name not in readonly_globals:
                    continue
                value = readonly_globals[name]
                code = (
                    value.__code__
                    if type(value) is function_type
                    and value.__globals__ is readonly_globals
                    else None
                )
                previous = captured.get(name)
                if previous is not None and (
                    previous[0] is not value or previous[1] is not code
                ):
                    raise identity_error_type(
                        "canonical Betfair dependency changed during K07 initialization"
                    )
                captured[name] = (value, code)
                if code is not None:
                    pending.append(value)
        return tuple(
            (name, value, code)
            for name, (value, code) in sorted(captured.items())
        )

    readonly_dependency_bindings = capture_readonly_dependencies(
        canonical_client_init,
        canonical_read_account_details,
        canonical_rpc,
        canonical_next_request_id,
        canonical_observed_at,
        canonical_transport_init,
        canonical_network_post,
    )

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

    def identity_projection_material_for(
        value: BetfairAuthenticatedAccountIdentity,
    ) -> bytes:
        try:
            return identity_projection_material(
                identity_schema,
                identity_schema_version,
                value.venue_id,
                value.mode.value,
                value.identity_scope,
                value.session_context_id,
                value.currency_code,
                value.account_details_sha256,
                value.observed_at,
            )
        except (AttributeError, TypeError, UnicodeEncodeError, OverflowError) as exc:
            raise identity_error_type(
                "account identity projection fields are malformed"
            ) from exc

    def public_identity_digest(
        value: BetfairAuthenticatedAccountIdentity,
    ) -> str:
        return sha256_fn(identity_projection_material_for(value)).hexdigest()

    def issued_identity_digest(
        value: BetfairAuthenticatedAccountIdentity,
    ) -> str:
        # Hidden issuance integrity uses a process-local keyed digest over the
        # same deterministic scalar projection as the public identity_id. It
        # intentionally does not depend on json.dumps/JSONEncoder executable
        # state, so JSON-code mutation cannot mask post-issuance DTO mutation.
        return hmac_digest(
            process_hmac_key,
            identity_projection_material_for(value),
            "sha256",
        ).hex()

    def identity_projection_dependencies_are_current() -> bool:
        return bool(
            _identity_projection_material is identity_projection_material
            and getattr(_identity_projection_material, "__code__", None)
            is identity_projection_material_code
            and sha256 is sha256_fn
            and IDENTITY_SCHEMA == identity_schema
            and IDENTITY_SCHEMA_VERSION == identity_schema_version
        )

    def identity_class_is_current() -> bool:
        return bool(
            identity_projection_dependencies_are_current()
            and identity_type.__init__ is canonical_identity_init
            and identity_type.__post_init__ is canonical_identity_post_init
            and all(
                identity_type.__dict__.get(name) is descriptor
                and descriptor.fget is getter
                and getattr(getter, "__code__", None) is getter_code
                for name, descriptor, getter, getter_code
                in semantic_property_bindings
            )
        )

    def readonly_dependencies_are_current() -> bool:
        for name, expected, expected_code in readonly_dependency_bindings:
            current = readonly_globals.get(name, missing_global)
            if current is not expected:
                return False
            if expected_code is not None:
                if type(current) is not function_type or current.__code__ is not expected_code:
                    return False
        return True

    def client_class_dispatch_is_current() -> bool:
        return (
            client_type.__init__ is canonical_client_init
            and client_type.read_account_details
            is canonical_read_account_details
            and client_type._rpc is canonical_rpc
            and client_type._next_request_id
            is canonical_next_request_id
            and client_type._observed_at is canonical_observed_at
            and transport_type.__init__ is canonical_transport_init
            and transport_type.post is canonical_network_post
            and redirect_handler_type.redirect_request
            is canonical_redirect_request
            and opener_type.open is canonical_opener_open
            and opener_type._open is canonical_opener_internal_open
            and opener_type._call_chain is canonical_opener_call_chain
            and opener_type.error is canonical_opener_error
            and details_type.__init__ is canonical_details_init
            and details_type.__post_init__ is canonical_details_post_init
            and evidence_type.__init__ is canonical_evidence_init
            and evidence_type.__post_init__ is canonical_evidence_post_init
            and rpc_result_type.__init__ is canonical_rpc_result_init
            and readonly_dependencies_are_current()
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

    def opener_dispatch_snapshot(
        opener: object,
    ) -> tuple[tuple[str, object, tuple[object, ...]], ...] | None:
        records: list[tuple[str, object, tuple[object, ...]]] = []
        for map_name in ("handle_open", "process_request", "process_response"):
            mapping = getattr(opener, map_name, None)
            if type(mapping) is not dict:
                return None
            for key, handlers in mapping.items():
                if type(key) not in (str, int) or type(handlers) is not list:
                    return None
                records.append((map_name, key, tuple(handlers)))
        error_mapping = getattr(opener, "handle_error", None)
        if type(error_mapping) is not dict:
            return None
        for protocol, by_code in error_mapping.items():
            if type(protocol) not in (str, int) or type(by_code) is not dict:
                return None
            for code, handlers in by_code.items():
                if type(code) not in (str, int) or type(handlers) is not list:
                    return None
                records.append((f"handle_error:{protocol}", code, tuple(handlers)))
        records.sort(key=lambda item: (item[0], type(item[1]).__name__, str(item[1])))
        return tuple(records)

    def dispatch_matches(
        current: tuple[tuple[str, object, tuple[object, ...]], ...] | None,
        expected: tuple[tuple[str, object, tuple[object, ...]], ...],
    ) -> bool:
        if current is None or len(current) != len(expected):
            return False
        for actual, wanted in zip(current, expected):
            if actual[0] != wanted[0] or actual[1] != wanted[1]:
                return False
            if len(actual[2]) != len(wanted[2]):
                return False
            if any(
                actual_handler is not expected_handler
                for actual_handler, expected_handler in zip(actual[2], wanted[2])
            ):
                return False
        return True

    def canonical_network_transport(
        transport: object,
        *,
        origin: _CanonicalClientOrigin | None = None,
    ) -> bool:
        if not client_class_dispatch_is_current():
            return False
        if type(transport) is not transport_type:
            return False
        transport_dict = getattr(transport, "__dict__", None)
        if type(transport_dict) is not dict:
            return False
        if set(transport_dict) != {"_max_response_bytes", "_opener"}:
            return False
        max_response_bytes = transport_dict.get("_max_response_bytes")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            return False

        opener = transport_dict.get("_opener")
        if type(opener) is not opener_type or type(opener).open is not canonical_opener_open:
            return False
        opener_dict = getattr(opener, "__dict__", None)
        if type(opener_dict) is not dict or any(
            name in opener_dict
            for name in ("open", "_open", "_call_chain", "error")
        ):
            return False
        handlers = getattr(opener, "handlers", None)
        if type(handlers) is not list:
            return False
        handler_tuple = tuple(handlers)
        redirect_handlers = tuple(
            handler
            for handler in handler_tuple
            if isinstance(handler, http_redirect_handler_type)
        )
        if (
            len(redirect_handlers) != 1
            or type(redirect_handlers[0]) is not redirect_handler_type
        ):
            return False

        https_handlers = tuple(
            handler
            for handler in handler_tuple
            if isinstance(handler, https_handler_type)
        )
        if len(https_handlers) != 1 or type(https_handlers[0]) is not https_handler_type:
            return False
        https_handler = https_handlers[0]
        https_handler_dict = getattr(https_handler, "__dict__", None)
        if (
            type(https_handler_dict) is not dict
            or "https_open" in https_handler_dict
            or "https_request" in https_handler_dict
            or "do_open" in https_handler_dict
            or https_handler_type.https_open is not canonical_https_open
            or https_handler_type.https_request is not canonical_https_request
            or abstract_http_handler_type.do_open is not canonical_do_open
            or http_error_processor_type.https_response is not canonical_https_response
        ):
            return False

        dispatch = opener_dispatch_snapshot(opener)
        if dispatch is None:
            return False
        dispatch_handlers = tuple(
            handlers
            for map_name, key, handlers in dispatch
            if map_name == "handle_open" and key == "https"
        )
        request_handlers = tuple(
            handlers
            for map_name, key, handlers in dispatch
            if map_name == "process_request" and key == "https"
        )
        response_handlers = tuple(
            handlers
            for map_name, key, handlers in dispatch
            if map_name == "process_response" and key == "https"
        )
        if (
            len(dispatch_handlers) != 1
            or len(dispatch_handlers[0]) != 1
            or dispatch_handlers[0][0] is not https_handler
            or len(request_handlers) != 1
            or len(request_handlers[0]) != 1
            or request_handlers[0][0] is not https_handler
            or len(response_handlers) != 1
            or len(response_handlers[0]) != 1
            or type(response_handlers[0][0]) is not http_error_processor_type
            or "https_response" in getattr(response_handlers[0][0], "__dict__", {})
            or any(
                not any(handler is registered for registered in handler_tuple)
                for _map_name, _key, handlers in dispatch
                for handler in handlers
            )
        ):
            return False

        if origin is not None:
            if type(origin) is not origin_type or origin.opener is not opener:
                return False
            if len(origin.opener_handlers) != len(handler_tuple):
                return False
            if any(
                current is not expected
                for current, expected in zip(handler_tuple, origin.opener_handlers)
            ):
                return False
            if not dispatch_matches(dispatch, origin.opener_dispatch):
                return False
        return True

    def origin_matches(
        origin: _CanonicalClientOrigin,
        client: BetfairReadOnlyClient,
    ) -> bool:
        try:
            if (
                type(client) is not client_type
                or not client_dispatch_is_current(client)
                or not canonical_network_transport(
                    client._transport,
                    origin=origin,
                )
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

        if not identity_projection_dependencies_are_current():
            raise identity_error_type(
                "account identity projection dependencies changed before issuance"
            )
        hidden_digest = issued_identity_digest(value)
        expected_public_id = public_identity_digest(value)
        try:
            current_public_id = value.identity_id
        except (AttributeError, TypeError, ValueError) as exc:
            raise identity_error_type(
                "account identity public projection cannot be verified"
            ) from exc
        if (
            not identity_projection_dependencies_are_current()
            or not hmac_compare_digest(expected_public_id, current_public_id)
        ):
            raise identity_error_type(
                "account identity public projection changed before issuance"
            )

        with lock:
            issued[identity] = issued_record_type(
                value_ref=weakref_fn(value, discard),
                identity_id=hidden_digest,
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
        opener = client._transport._opener
        dispatch = opener_dispatch_snapshot(opener)
        if dispatch is None:
            raise identity_error_type(
                "canonical Betfair opener dispatch is not inspectable"
            )
        origin = origin_type(
            client._transport,
            opener,
            tuple(opener.handlers),
            dispatch,
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
        """Verify current process-local K07 session-context issuance only."""
        if type(value) is not identity_type or not identity_class_is_current():
            return False
        with lock:
            record = issued.get(id(value))
            if record is None or record.value_ref() is not value:
                return False
            try:
                current_identity_digest = issued_identity_digest(value)
                expected_public_id = public_identity_digest(value)
                current_public_id = value.identity_id
            except (identity_error_type, AttributeError, TypeError, ValueError):
                return False
            if not identity_projection_dependencies_are_current():
                return False
            if not hmac_compare_digest(record.identity_id, current_identity_digest):
                return False
            if not hmac_compare_digest(expected_public_id, current_public_id):
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
        """Require current K07 context authority, never remote-provider origin proof."""
        if not is_authoritative(value, client=client):
            raise identity_error_type(
                "Betfair account identity lacks current authenticated-context authority"
            )
        assert type(value) is identity_type
        return value

    return build_client, resolve_identity, is_authoritative, require_authoritative


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
) = _make_account_identity_authority()
del _make_account_identity_authority

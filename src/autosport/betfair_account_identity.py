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
import json
from secrets import token_bytes, token_hex
from threading import RLock
from weakref import ReferenceType, WeakKeyDictionary, ref

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
_PROCESS_HMAC_KEY = token_bytes(32)


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


_LOCK = RLock()
_CLIENT_CONTEXTS: dict[int, _ClientContextRecord] = {}
_ISSUED: dict[int, _IssuedIdentityRecord] = {}


def _credential_binding(credentials: BetfairSessionCredentials) -> bytes:
    if type(credentials) is not BetfairSessionCredentials:
        raise BetfairAccountIdentityError(
            "authenticated context requires canonical BetfairSessionCredentials"
        )
    # This process-local HMAC is never serialized or exposed.  It exists only to
    # detect object.__setattr__ mutation of the frozen credential object.
    material = (
        credentials.application_key.encode("utf-8")
        + b"\x00"
        + credentials.session_token.encode("utf-8")
    )
    return hmac.digest(_PROCESS_HMAC_KEY, material, "sha256")


_CANONICAL_CLIENT_ORIGINS: WeakKeyDictionary = WeakKeyDictionary()


def build_betfair_authenticated_client(
    credentials: BetfairSessionCredentials,
    *,
    timeout_seconds: float = 10.0,
    account_label: str = "authenticated-account",
) -> BetfairReadOnlyClient:
    """Create one canonical client whose authenticated origin K07 may attest.

    K07 owns this narrow factory instead of rewriting BetfairReadOnlyClient.__init__
    at import time. Directly constructed clients remain valid read-only clients,
    but they do not gain K07 provenance merely because this module was imported.
    """

    if type(credentials) is not BetfairSessionCredentials:
        raise BetfairAccountIdentityError(
            "authenticated context requires canonical BetfairSessionCredentials"
        )
    binding = _credential_binding(credentials)
    client = BetfairReadOnlyClient(
        credentials,
        timeout_seconds=timeout_seconds,
        venue_id=VENUE_ID,
        account_id=account_label,
    )
    if (
        type(client) is not BetfairReadOnlyClient
        or type(client._transport) is not UrllibBetfairHttpTransport
        or client._credentials is not credentials
    ):
        raise BetfairAccountIdentityError(
            "canonical Betfair client factory produced invalid origin"
        )
    origin = _CanonicalClientOrigin(
        client._transport,
        client._clock,
        client._credentials,
        binding,
    )
    with _LOCK:
        _CANONICAL_CLIENT_ORIGINS[client] = origin
    return client


def resolve_betfair_authenticated_account_identity(
    client: BetfairReadOnlyClient,
    *,
    mode: BetfairAccountIdentityMode = BetfairAccountIdentityMode.PERSONAL_DEVELOPER,
) -> BetfairAuthenticatedAccountIdentity:
    """Issue identity for the exact current authenticated client/session context.

    PERSONAL_DEVELOPER intentionally proves no stable cross-session provider account id.
    LICENSED_VENDOR remains fail-closed until the canonical client owns a qualified
    provider-issued customer identifier path.
    """

    if type(client) is not BetfairReadOnlyClient:
        raise BetfairAccountIdentityError(
            "account identity requires exact canonical BetfairReadOnlyClient"
        )
    if mode is not BetfairAccountIdentityMode.PERSONAL_DEVELOPER:
        raise BetfairAccountIdentityError(
            "LICENSED_VENDOR stable account identity is not implemented on canonical transport"
        )

    context = _context_for(client)
    try:
        details = client.read_account_details()
    except BetfairReadOnlyError as exc:
        raise BetfairAccountIdentityError(
            "authenticated Betfair account-details acquisition failed"
        ) from exc
    if type(details) is not BetfairAccountDetailsObservation:
        raise BetfairAccountIdentityError(
            "account-details acquisition returned non-canonical evidence"
        )
    if not _context_is_current(context, client, revoke_on_failure=True):
        raise BetfairAccountIdentityError(
            "authenticated client/session context changed during account-details acquisition"
        )

    value = BetfairAuthenticatedAccountIdentity(
        venue_id=VENUE_ID,
        mode=BetfairAccountIdentityMode.PERSONAL_DEVELOPER,
        identity_scope=IDENTITY_SCOPE,
        session_context_id=context.session_context_id,
        currency_code=_currency_code(details.currency_code),
        account_details_sha256=_sha256_hex(
            details.evidence.source_payload_sha256,
            "account_details_sha256",
        ),
        observed_at=_canonical_timestamp(details.evidence.observed_at),
    )
    _remember_issued(value, client, context)
    return value


def is_authoritative_betfair_account_identity(
    value: object,
    *,
    client: BetfairReadOnlyClient | None = None,
) -> bool:
    """Return whether identity remains an unchanged current-process issuance."""

    if type(value) is not BetfairAuthenticatedAccountIdentity:
        return False
    with _LOCK:
        record = _ISSUED.get(id(value))
        if record is None or record.value_ref() is not value:
            return False
        if not hmac.compare_digest(record.identity_id, value.identity_id):
            return False
        issued_client = record.client_ref()
        if issued_client is None:
            return False
        if client is not None and issued_client is not client:
            return False
        context = _CLIENT_CONTEXTS.get(id(issued_client))
        if (
            context is None
            or context.client_ref() is not issued_client
            or context.session_context_id != record.session_context_id
        ):
            return False
        return _context_is_current(context, issued_client, revoke_on_failure=True)


def require_authoritative_betfair_account_identity(
    value: object,
    *,
    client: BetfairReadOnlyClient | None = None,
) -> BetfairAuthenticatedAccountIdentity:
    if not is_authoritative_betfair_account_identity(value, client=client):
        raise BetfairAccountIdentityError(
            "Betfair account identity lacks current authenticated-context authority"
        )
    assert type(value) is BetfairAuthenticatedAccountIdentity
    return value


def _context_for(client: BetfairReadOnlyClient) -> _ClientContextRecord:
    with _LOCK:
        origin = _CANONICAL_CLIENT_ORIGINS.get(client)
        if origin is None:
            raise BetfairAccountIdentityError(
                "account identity requires product-owned Betfair transport/clock origin"
            )
        existing = _CLIENT_CONTEXTS.get(id(client))
        if existing is not None:
            if existing.client_ref() is not client:
                _CLIENT_CONTEXTS.pop(id(client), None)
            elif not _context_is_current(existing, client, revoke_on_failure=True):
                raise BetfairAccountIdentityError(
                    "authenticated client/session context was rotated or mutated"
                )
            else:
                return existing

        if not _origin_matches(origin, client):
            raise BetfairAccountIdentityError(
                "authenticated client origin changed before identity issuance"
            )
        identity = id(client)

        def discard(dead_ref: ReferenceType[BetfairReadOnlyClient]) -> None:
            with _LOCK:
                record = _CLIENT_CONTEXTS.get(identity)
                if record is not None and record.client_ref is dead_ref:
                    _CLIENT_CONTEXTS.pop(identity, None)

        record = _ClientContextRecord(
            client_ref=ref(client, discard),
            origin=origin,
            session_context_id=_CONTEXT_PREFIX + token_hex(32),
        )
        _CLIENT_CONTEXTS[identity] = record
        return record


def _origin_matches(origin: _CanonicalClientOrigin, client: BetfairReadOnlyClient) -> bool:
    try:
        if (
            type(client) is not BetfairReadOnlyClient
            or type(client._transport) is not UrllibBetfairHttpTransport
            or client._transport is not origin.transport
            or client._clock is not origin.clock
            or client._credentials is not origin.credentials
            or client._venue_id != VENUE_ID
        ):
            return False
        return hmac.compare_digest(
            origin.credential_binding,
            _credential_binding(client._credentials),
        )
    except (AttributeError, TypeError, BetfairAccountIdentityError):
        return False


def _context_is_current(
    record: _ClientContextRecord,
    client: BetfairReadOnlyClient,
    *,
    revoke_on_failure: bool,
) -> bool:
    if record.revoked:
        return False
    valid = record.client_ref() is client and _origin_matches(record.origin, client)
    if not valid and revoke_on_failure:
        record.revoked = True
    return valid


def _remember_issued(
    value: BetfairAuthenticatedAccountIdentity,
    client: BetfairReadOnlyClient,
    context: _ClientContextRecord,
) -> None:
    identity = id(value)

    def discard(dead_ref: ReferenceType[BetfairAuthenticatedAccountIdentity]) -> None:
        with _LOCK:
            record = _ISSUED.get(identity)
            if record is not None and record.value_ref is dead_ref:
                _ISSUED.pop(identity, None)

    with _LOCK:
        _ISSUED[identity] = _IssuedIdentityRecord(
            value_ref=ref(value, discard),
            identity_id=value.identity_id,
            client_ref=ref(client),
            session_context_id=context.session_context_id,
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

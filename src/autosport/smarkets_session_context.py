"""Smarkets authenticated runtime session/account-context authority.

This module composes the fixed-origin orders acquisition boundary with an
authenticated ``GET /v3/accounts/`` read performed under the same volatile
session token.  It deliberately does not persist credentials, derive identity
from secrets/caller labels, or grant any execution authority.

The provider-owned ``account.account_id`` is bound to the exact validated
account-response bytes and a distinct process-local session generation.  A fresh
process must reauthenticate and reacquire before positive resolution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import secrets
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.request import Request

from . import smarkets_orders_acquisition as orders_acquisition


SMARKETS_ACCOUNTS_ENDPOINT: Final = "https://api.smarkets.com/v3/accounts/"
_MAX_ACCOUNTS_RESPONSE_BYTES: Final = 2 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_SESSION_SEAL: Final = object()
_ACCOUNT_SEAL: Final = object()
_READBACK_SEAL: Final = object()


class SmarketsSessionContextError(RuntimeError):
    """Authenticated Smarkets session/account context could not be established."""


def _validated_token(value: object) -> str:
    try:
        return orders_acquisition._nonempty_token(value)
    except orders_acquisition.SmarketsOrdersAcquisitionError as exc:
        raise SmarketsSessionContextError("Smarkets session token is invalid") from exc


def _validated_timeout(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SmarketsSessionContextError("timeout_seconds must be numeric")
    timeout = float(value)
    if not (0.1 <= timeout <= 60.0):
        raise SmarketsSessionContextError("timeout_seconds must be in [0.1, 60]")
    return timeout


_ACCOUNT_REQUIRED_STRING_FIELDS: Final = (
    "account_id",
    "balance",
    "available_balance",
    "exposure",
    "currency",
)
_ACCOUNT_OPTIONAL_STRING_FIELDS: Final = (
    "bonus_balance",
    "commission_type",
    "signup_date",
)


def _account_record(raw: bytes) -> dict[str, object]:
    """Parse the provider-owned account envelope without accepting caller-shaped DTOs."""
    try:
        parsed = orders_acquisition._strict_json(raw)
    except orders_acquisition.SmarketsOrdersAcquisitionError as exc:
        # Do not propagate provider-controlled payload fragments into operator text.
        raise SmarketsSessionContextError("Smarkets accounts payload is invalid JSON") from None

    if type(parsed) is not dict or type(parsed.get("account")) is not dict:
        raise SmarketsSessionContextError(
            "Smarkets accounts payload does not match the provider account envelope"
        )

    account = parsed["account"]
    assert type(account) is dict
    for field_name in _ACCOUNT_REQUIRED_STRING_FIELDS:
        value = account.get(field_name)
        if type(value) is not str or not value.strip():
            raise SmarketsSessionContextError(
                "Smarkets accounts payload lacks required provider account fields"
            )
    for field_name in _ACCOUNT_OPTIONAL_STRING_FIELDS:
        if field_name in account and type(account[field_name]) is not str:
            raise SmarketsSessionContextError(
                "Smarkets accounts payload has invalid optional provider account fields"
            )
    return account


def _provider_date(value: object) -> str:
    try:
        return orders_acquisition._provider_date(value)
    except orders_acquisition.SmarketsOrdersAcquisitionError as exc:
        raise SmarketsSessionContextError(
            "Smarkets accounts response Date header is invalid"
        ) from exc


def _content_type(value: object) -> str:
    if type(value) is not str:
        raise SmarketsSessionContextError("Smarkets accounts response lacks Content-Type")
    media_type = value.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise SmarketsSessionContextError(
            "Smarkets accounts response is not application/json"
        )
    return value.strip()


def _open_accounts_request(request: Request, timeout: float):
    """Reuse the existing fixed-origin TLS/no-proxy/no-redirect transport seam."""
    return orders_acquisition._open_orders_request(request, timeout)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_generation_id() -> str:
    # Opaque process-local discriminator. It is not derived from the secret.
    return secrets.token_hex(16)


def _digest_parts(*parts: str) -> str:
    material = "\x00".join(parts).encode("utf-8")
    return sha256(material).hexdigest()


@dataclass(frozen=True, slots=True, init=False)
class SmarketsAccountReadbackWitness:
    """Safe metadata for one authenticated fixed-origin accounts response."""

    endpoint: str
    http_status: int
    provider_date: str
    content_type: str
    payload_sha256: str
    payload_size: int
    product_available_at: str
    provider_account_id: str

    def __init__(
        self,
        *,
        endpoint: str,
        http_status: int,
        provider_date: str,
        content_type: str,
        payload_sha256: str,
        payload_size: int,
        product_available_at: str,
        provider_account_id: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _ACCOUNT_SEAL:
            raise TypeError(
                "SmarketsAccountReadbackWitness is issued only by authenticated account readback"
            )
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "http_status", http_status)
        object.__setattr__(self, "provider_date", provider_date)
        object.__setattr__(self, "content_type", content_type)
        object.__setattr__(self, "payload_sha256", payload_sha256)
        object.__setattr__(self, "payload_size", payload_size)
        object.__setattr__(self, "product_available_at", product_available_at)
        object.__setattr__(self, "provider_account_id", provider_account_id)

    def __reduce__(self):
        raise TypeError(
            "SmarketsAccountReadbackWitness is intentionally non-serializable; "
            "reacquire after restart"
        )


@dataclass(frozen=True, slots=True, init=False)
class SmarketsSessionOrdersReadback:
    """Orders evidence bound to one exact authenticated runtime session context."""

    session_generation_id: str
    account_context_sha256: str
    provider_account_id: str
    accounts_payload_sha256: str
    accounts_available_at: str
    orders_endpoint: str
    orders_payload_sha256: str
    orders_payload_size: int
    orders_provider_date: str
    orders_received_at: str
    session_bound_at: str
    evidence_sha256: str
    _orders_record: orders_acquisition.SmarketsOrdersAcquisitionRecord = field(
        repr=False,
        compare=False,
    )

    def __init__(
        self,
        *,
        session_generation_id: str,
        account_context_sha256: str,
        provider_account_id: str,
        accounts_payload_sha256: str,
        accounts_available_at: str,
        orders_endpoint: str,
        orders_payload_sha256: str,
        orders_payload_size: int,
        orders_provider_date: str,
        orders_received_at: str,
        session_bound_at: str,
        evidence_sha256: str,
        orders_record: orders_acquisition.SmarketsOrdersAcquisitionRecord,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _READBACK_SEAL:
            raise TypeError(
                "SmarketsSessionOrdersReadback is issued only by an authenticated session"
            )
        object.__setattr__(self, "session_generation_id", session_generation_id)
        object.__setattr__(self, "account_context_sha256", account_context_sha256)
        object.__setattr__(self, "provider_account_id", provider_account_id)
        object.__setattr__(self, "accounts_payload_sha256", accounts_payload_sha256)
        object.__setattr__(self, "accounts_available_at", accounts_available_at)
        object.__setattr__(self, "orders_endpoint", orders_endpoint)
        object.__setattr__(self, "orders_payload_sha256", orders_payload_sha256)
        object.__setattr__(self, "orders_payload_size", orders_payload_size)
        object.__setattr__(self, "orders_provider_date", orders_provider_date)
        object.__setattr__(self, "orders_received_at", orders_received_at)
        object.__setattr__(self, "session_bound_at", session_bound_at)
        object.__setattr__(self, "evidence_sha256", evidence_sha256)
        object.__setattr__(self, "_orders_record", orders_record)

    @property
    def orders_record(self) -> orders_acquisition.SmarketsOrdersAcquisitionRecord:
        return self._orders_record

    def __reduce__(self):
        raise TypeError(
            "SmarketsSessionOrdersReadback is intentionally non-serializable; "
            "reacquire after restart"
        )


class SmarketsAuthenticatedSession:
    """Volatile authenticated session generation bound to account readback."""

    __slots__ = (
        "_session_token",
        "_generation_id",
        "_account_witness",
        "_account_context_sha256",
        "_issued_readbacks",
        "_closed",
    )

    def __init__(
        self,
        *,
        session_token: str,
        generation_id: str,
        account_witness: SmarketsAccountReadbackWitness,
        account_context_sha256: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _SESSION_SEAL:
            raise TypeError(
                "SmarketsAuthenticatedSession is issued only by open_smarkets_authenticated_session"
            )
        self._session_token: str | None = session_token
        self._generation_id = generation_id
        self._account_witness = account_witness
        self._account_context_sha256 = account_context_sha256
        self._issued_readbacks: dict[str, SmarketsSessionOrdersReadback] = {}
        self._closed = False

    @property
    def generation_id(self) -> str:
        return self._generation_id

    @property
    def account_context_sha256(self) -> str:
        return self._account_context_sha256

    @property
    def provider_account_id(self) -> str:
        return self._account_witness.provider_account_id

    @property
    def account_witness(self) -> SmarketsAccountReadbackWitness:
        return self._account_witness

    @property
    def closed(self) -> bool:
        return self._closed

    def __repr__(self) -> str:
        return (
            "SmarketsAuthenticatedSession("
            f"generation_id={self._generation_id!r}, "
            f"account_context_sha256={self._account_context_sha256!r}, "
            f"closed={self._closed!r})"
        )

    def __reduce__(self):
        raise TypeError(
            "SmarketsAuthenticatedSession is intentionally non-serializable; "
            "reauthenticate after restart"
        )

    def __enter__(self) -> "SmarketsAuthenticatedSession":
        self._require_open()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _require_open(self) -> str:
        if self._closed or self._session_token is None:
            raise SmarketsSessionContextError("Smarkets authenticated session is closed")
        return self._session_token

    def close(self) -> None:
        self._session_token = None
        self._issued_readbacks.clear()
        self._closed = True

    def acquire_orders(
        self,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> SmarketsSessionOrdersReadback:
        """Acquire orders under this exact live session/account context."""
        token = self._require_open()
        timeout = _validated_timeout(timeout_seconds)
        try:
            record = orders_acquisition.acquire_smarkets_orders_payload(
                token,
                timeout_seconds=timeout,
            )
        except orders_acquisition.SmarketsOrdersAcquisitionError as exc:
            # Never forward provider payload/token material from a lower-level error.
            raise SmarketsSessionContextError(
                "Smarkets orders readback is unavailable for this session"
            ) from None

        if type(record) is not orders_acquisition.SmarketsOrdersAcquisitionRecord:
            raise SmarketsSessionContextError(
                "Smarkets orders acquisition returned unsupported record"
            )

        session_bound_at = _utc_now_iso()
        evidence_sha256 = _digest_parts(
            "autosport.smarkets.session-orders-readback.v1",
            self._generation_id,
            self._account_context_sha256,
            self._account_witness.provider_account_id,
            self._account_witness.payload_sha256,
            self._account_witness.product_available_at,
            record.endpoint,
            record.payload_sha256,
            str(record.payload_size),
            record.provider_date,
            record.received_at,
            session_bound_at,
        )
        readback = SmarketsSessionOrdersReadback(
            session_generation_id=self._generation_id,
            account_context_sha256=self._account_context_sha256,
            provider_account_id=self._account_witness.provider_account_id,
            accounts_payload_sha256=self._account_witness.payload_sha256,
            accounts_available_at=self._account_witness.product_available_at,
            orders_endpoint=record.endpoint,
            orders_payload_sha256=record.payload_sha256,
            orders_payload_size=record.payload_size,
            orders_provider_date=record.provider_date,
            orders_received_at=record.received_at,
            session_bound_at=session_bound_at,
            evidence_sha256=evidence_sha256,
            orders_record=record,
            _seal=_READBACK_SEAL,
        )
        self._issued_readbacks[evidence_sha256] = readback
        return readback

    def resolve_orders_readback(
        self,
        candidate: object,
    ) -> SmarketsSessionOrdersReadback:
        """Resolve only evidence actually issued by this exact live session object."""
        self._require_open()
        if type(candidate) is not SmarketsSessionOrdersReadback:
            raise SmarketsSessionContextError(
                "Smarkets orders evidence is not canonical session readback"
            )
        stored = self._issued_readbacks.get(candidate.evidence_sha256)
        if stored is not candidate:
            raise SmarketsSessionContextError(
                "Smarkets orders evidence was not issued by this session context"
            )
        if (
            candidate.session_generation_id != self._generation_id
            or candidate.account_context_sha256 != self._account_context_sha256
            or candidate.provider_account_id != self._account_witness.provider_account_id
            or candidate.accounts_payload_sha256 != self._account_witness.payload_sha256
        ):
            raise SmarketsSessionContextError(
                "Smarkets orders evidence does not match this session/account context"
            )
        return candidate


def open_smarkets_authenticated_session(
    session_token: str,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> SmarketsAuthenticatedSession:
    """Resolve account context and establish one volatile authenticated generation.

    Credentials remain only in the returned in-memory session object.  Safe
    account evidence contains exact response-byte and causal-availability
    digests, never the raw token or account payload.
    """
    token = _validated_token(session_token)
    timeout = _validated_timeout(timeout_seconds)

    request = Request(
        SMARKETS_ACCOUNTS_ENDPOINT,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Session-Token {token}",
            "User-Agent": "Autosport/smarkets-session-context-1",
        },
    )

    try:
        response = _open_accounts_request(request, timeout)
        with response:
            final_url = response.geturl()
            status = response.getcode()
            if final_url != SMARKETS_ACCOUNTS_ENDPOINT:
                raise SmarketsSessionContextError(
                    "Smarkets accounts response final URL is not the fixed official endpoint"
                )
            if type(status) is not int or status != 200:
                raise SmarketsSessionContextError(
                    f"Smarkets accounts endpoint returned non-success status {status!r}"
                )
            headers = getattr(response, "headers", None)
            if headers is None:
                raise SmarketsSessionContextError(
                    "Smarkets accounts response lacks headers"
                )
            content_type = _content_type(headers.get("Content-Type"))
            provider_date = _provider_date(headers.get("Date"))
            try:
                raw = orders_acquisition._read_complete_body(response)
            except orders_acquisition.SmarketsOrdersAcquisitionError as exc:
                raise SmarketsSessionContextError(
                    "Smarkets accounts response body framing is invalid or incomplete"
                ) from exc
    except SmarketsSessionContextError:
        raise
    except HTTPError as exc:
        raise SmarketsSessionContextError(
            f"Smarkets accounts endpoint is unavailable (HTTP {exc.code})"
        ) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise SmarketsSessionContextError(
            "Smarkets accounts HTTPS acquisition failed"
        ) from exc

    if type(raw) is not bytes:
        raise SmarketsSessionContextError("Smarkets accounts response body is not bytes")
    if len(raw) > _MAX_ACCOUNTS_RESPONSE_BYTES:
        raise SmarketsSessionContextError("Smarkets accounts payload exceeds byte limit")
    if not raw:
        raise SmarketsSessionContextError("Smarkets accounts payload is empty")

    account = _account_record(raw)
    provider_account_id = account["account_id"]
    assert type(provider_account_id) is str
    available_at = _utc_now_iso()
    payload_sha256 = sha256(raw).hexdigest()
    witness = SmarketsAccountReadbackWitness(
        endpoint=SMARKETS_ACCOUNTS_ENDPOINT,
        http_status=200,
        provider_date=provider_date,
        content_type=content_type,
        payload_sha256=payload_sha256,
        payload_size=len(raw),
        product_available_at=available_at,
        provider_account_id=provider_account_id,
        _seal=_ACCOUNT_SEAL,
    )
    generation_id = _new_generation_id()
    account_context_sha256 = _digest_parts(
        "autosport.smarkets.session-account-context.v1",
        generation_id,
        SMARKETS_ACCOUNTS_ENDPOINT,
        provider_account_id,
        payload_sha256,
        available_at,
    )
    return SmarketsAuthenticatedSession(
        session_token=token,
        generation_id=generation_id,
        account_witness=witness,
        account_context_sha256=account_context_sha256,
        _seal=_SESSION_SEAL,
    )

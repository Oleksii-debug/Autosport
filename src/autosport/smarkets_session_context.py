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
from urllib.parse import urlencode
from urllib.request import Request

from . import smarkets_orders_acquisition as orders_acquisition


SMARKETS_ACCOUNTS_ENDPOINT: Final = "https://api.smarkets.com/v3/accounts/"
SMARKETS_ACCOUNT_ACTIVITY_ENDPOINT: Final = "https://api.smarkets.com/v3/accounts/activity/"
_MAX_ACCOUNTS_RESPONSE_BYTES: Final = 2 * 1024 * 1024
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_SESSION_SEAL: Final = object()
_ACCOUNT_SEAL: Final = object()
_READBACK_SEAL: Final = object()
_AUTHENTICATED_READ_SEAL: Final = object()


class SmarketsSessionContextError(RuntimeError):
    """Authenticated Smarkets session/account context could not be established."""


def _activity_filter_text(value: object, name: str) -> str:
    if type(value) is not str:
        raise SmarketsSessionContextError(f"{name} must be text")
    normalized = value.strip()
    if not normalized or normalized != value:
        raise SmarketsSessionContextError(f"{name} must be non-empty canonical text")
    if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise SmarketsSessionContextError(f"{name} contains control characters")
    return normalized


def _activity_filter_tuple(value: object, name: str) -> tuple[str, ...]:
    if type(value) is not tuple:
        raise SmarketsSessionContextError(f"{name} must be an immutable tuple")
    normalized = tuple(_activity_filter_text(item, name) for item in value)
    return tuple(sorted(set(normalized)))


def _activity_timestamp(value: datetime, name: str) -> str:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise SmarketsSessionContextError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SmarketsAccountActivityQuery:
    """Closed canonical query contract for the fixed account-activity endpoint."""

    timestamp_min: datetime | None = None
    timestamp_max: datetime | None = None
    limit: int | None = None
    market_ids: tuple[str, ...] = ()
    order_ids: tuple[str, ...] = ()
    pagination_last_seq: int | None = None
    pagination_last_subseq: int | None = None
    sort: str | None = None
    sources: tuple[str, ...] = ()
    event_info: bool | None = None

    def __post_init__(self) -> None:
        if self.timestamp_min is not None:
            _activity_timestamp(self.timestamp_min, "timestamp_min")
        if self.timestamp_max is not None:
            _activity_timestamp(self.timestamp_max, "timestamp_max")
        if self.timestamp_min is not None and self.timestamp_max is not None:
            if self.timestamp_min > self.timestamp_max:
                raise SmarketsSessionContextError(
                    "timestamp_min must not be later than timestamp_max"
                )
        if self.limit is not None:
            if type(self.limit) is not int or not 0 <= self.limit <= 500:
                raise SmarketsSessionContextError("limit must be an integer in [0, 500]")
        object.__setattr__(
            self,
            "market_ids",
            _activity_filter_tuple(self.market_ids, "market_ids"),
        )
        object.__setattr__(
            self,
            "order_ids",
            _activity_filter_tuple(self.order_ids, "order_ids"),
        )
        if (self.pagination_last_seq is None) != (
            self.pagination_last_subseq is None
        ):
            raise SmarketsSessionContextError(
                "pagination_last_seq and pagination_last_subseq must be supplied together"
            )
        for name in ("pagination_last_seq", "pagination_last_subseq"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise SmarketsSessionContextError(
                    f"{name} must be a non-negative integer"
                )
        if self.sort is not None and self.sort not in {
            "seq,subseq",
            "-seq,-subseq",
        }:
            raise SmarketsSessionContextError(
                "sort must be seq,subseq or -seq,-subseq"
            )
        object.__setattr__(
            self,
            "sources",
            _activity_filter_tuple(self.sources, "sources"),
        )
        if self.event_info is not None and type(self.event_info) is not bool:
            raise SmarketsSessionContextError("event_info must be bool or None")

    def to_query_string(self) -> str:
        pairs: list[tuple[str, str]] = []
        if self.timestamp_min is not None:
            pairs.append(
                ("timestamp_min", _activity_timestamp(self.timestamp_min, "timestamp_min"))
            )
        if self.timestamp_max is not None:
            pairs.append(
                ("timestamp_max", _activity_timestamp(self.timestamp_max, "timestamp_max"))
            )
        if self.limit is not None:
            pairs.append(("limit", str(self.limit)))
        pairs.extend(("market_id", value) for value in self.market_ids)
        pairs.extend(("order_id", value) for value in self.order_ids)
        if self.pagination_last_seq is not None:
            pairs.append(("pagination_last_seq", str(self.pagination_last_seq)))
            pairs.append(
                ("pagination_last_subseq", str(self.pagination_last_subseq))
            )
        if self.sort is not None:
            pairs.append(("sort", self.sort))
        pairs.extend(("source", value) for value in self.sources)
        if self.event_info is not None:
            pairs.append(("event_info", "true" if self.event_info else "false"))
        return urlencode(pairs)


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
    provider_currency: str

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
        provider_currency: str,
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
        object.__setattr__(self, "provider_currency", provider_currency)

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


@dataclass(frozen=True, slots=True, init=False)
class SmarketsSessionAuthenticatedRead:
    """Raw fixed-origin response issued by one exact live session generation."""

    session_generation_id: str
    account_context_sha256: str
    provider_account_id: str
    provider_currency: str
    endpoint: str
    request_query: str
    http_status: int
    provider_date: str
    content_type: str
    product_available_at: str
    payload_sha256: str
    payload_size: int
    evidence_sha256: str
    _payload: bytes = field(repr=False, compare=False)

    def __init__(
        self,
        *,
        session_generation_id: str,
        account_context_sha256: str,
        provider_account_id: str,
        provider_currency: str,
        endpoint: str,
        request_query: str,
        http_status: int,
        provider_date: str,
        content_type: str,
        product_available_at: str,
        payload_sha256: str,
        payload_size: int,
        evidence_sha256: str,
        payload: bytes,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _AUTHENTICATED_READ_SEAL:
            raise TypeError(
                "SmarketsSessionAuthenticatedRead is issued only by an authenticated session"
            )
        object.__setattr__(self, "session_generation_id", session_generation_id)
        object.__setattr__(self, "account_context_sha256", account_context_sha256)
        object.__setattr__(self, "provider_account_id", provider_account_id)
        object.__setattr__(self, "provider_currency", provider_currency)
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "request_query", request_query)
        object.__setattr__(self, "http_status", http_status)
        object.__setattr__(self, "provider_date", provider_date)
        object.__setattr__(self, "content_type", content_type)
        object.__setattr__(self, "product_available_at", product_available_at)
        object.__setattr__(self, "payload_sha256", payload_sha256)
        object.__setattr__(self, "payload_size", payload_size)
        object.__setattr__(self, "evidence_sha256", evidence_sha256)
        object.__setattr__(self, "_payload", payload)

    @property
    def payload(self) -> bytes:
        return self._payload

    def __reduce__(self):
        raise TypeError(
            "SmarketsSessionAuthenticatedRead is intentionally non-serializable; "
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
        "_issued_authenticated_reads",
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
        self._issued_authenticated_reads: dict[str, SmarketsSessionAuthenticatedRead] = {}
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
    def provider_currency(self) -> str:
        return self._account_witness.provider_currency

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
        self._issued_authenticated_reads.clear()
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

    def acquire_account_activity(
        self,
        *,
        query: SmarketsAccountActivityQuery | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> SmarketsSessionAuthenticatedRead:
        """Acquire one canonical account-activity query under this exact live session."""
        token = self._require_open()
        timeout = _validated_timeout(timeout_seconds)
        if query is None:
            request_query = ""
        elif type(query) is SmarketsAccountActivityQuery:
            request_query = query.to_query_string()
        else:
            raise SmarketsSessionContextError(
                "query must be SmarketsAccountActivityQuery or None"
            )
        request_url = SMARKETS_ACCOUNT_ACTIVITY_ENDPOINT
        if request_query:
            request_url = f"{request_url}?{request_query}"
        request = Request(
            request_url,
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
                if final_url != request_url:
                    raise SmarketsSessionContextError(
                        "Smarkets account activity final URL does not match the canonical request"
                    )
                if type(status) is not int or status != 200:
                    raise SmarketsSessionContextError(
                        "Smarkets account activity endpoint returned non-success status"
                    )
                headers = getattr(response, "headers", None)
                if headers is None:
                    raise SmarketsSessionContextError(
                        "Smarkets account activity response lacks headers"
                    )
                content_type = _content_type(headers.get("Content-Type"))
                provider_date = _provider_date(headers.get("Date"))
                try:
                    raw = orders_acquisition._read_complete_body(response)
                except orders_acquisition.SmarketsOrdersAcquisitionError:
                    raise SmarketsSessionContextError(
                        "Smarkets account activity response body framing is invalid or incomplete"
                    ) from None
        except SmarketsSessionContextError:
            raise
        except HTTPError as exc:
            raise SmarketsSessionContextError(
                f"Smarkets account activity endpoint is unavailable (HTTP {exc.code})"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise SmarketsSessionContextError(
                "Smarkets account activity HTTPS acquisition failed"
            ) from None

        if type(raw) is not bytes or not raw:
            raise SmarketsSessionContextError(
                "Smarkets account activity response body is empty or invalid"
            )

        available_at = _utc_now_iso()
        payload_sha256 = sha256(raw).hexdigest()
        evidence_sha256 = _digest_parts(
            "autosport.smarkets.session-authenticated-read.v2",
            self._generation_id,
            self._account_context_sha256,
            self._account_witness.provider_account_id,
            self._account_witness.provider_currency,
            SMARKETS_ACCOUNT_ACTIVITY_ENDPOINT,
            request_query,
            provider_date,
            content_type,
            available_at,
            payload_sha256,
            str(len(raw)),
        )
        read = SmarketsSessionAuthenticatedRead(
            session_generation_id=self._generation_id,
            account_context_sha256=self._account_context_sha256,
            provider_account_id=self._account_witness.provider_account_id,
            provider_currency=self._account_witness.provider_currency,
            endpoint=SMARKETS_ACCOUNT_ACTIVITY_ENDPOINT,
            request_query=request_query,
            http_status=200,
            provider_date=provider_date,
            content_type=content_type,
            product_available_at=available_at,
            payload_sha256=payload_sha256,
            payload_size=len(raw),
            evidence_sha256=evidence_sha256,
            payload=raw,
            _seal=_AUTHENTICATED_READ_SEAL,
        )
        self._issued_authenticated_reads[evidence_sha256] = read
        return read

    def resolve_account_activity_read(
        self,
        candidate: object,
    ) -> SmarketsSessionAuthenticatedRead:
        """Resolve only account-activity bytes issued by this exact live session object."""
        self._require_open()
        if type(candidate) is not SmarketsSessionAuthenticatedRead:
            raise SmarketsSessionContextError(
                "Smarkets account activity evidence is not canonical session readback"
            )
        stored = self._issued_authenticated_reads.get(candidate.evidence_sha256)
        if stored is not candidate:
            raise SmarketsSessionContextError(
                "Smarkets account activity evidence was not issued by this session context"
            )
        if (
            candidate.session_generation_id != self._generation_id
            or candidate.account_context_sha256 != self._account_context_sha256
            or candidate.provider_account_id != self._account_witness.provider_account_id
            or candidate.provider_currency != self._account_witness.provider_currency
            or candidate.endpoint != SMARKETS_ACCOUNT_ACTIVITY_ENDPOINT
        ):
            raise SmarketsSessionContextError(
                "Smarkets account activity evidence does not match this session/account context"
            )
        expected_evidence_sha256 = _digest_parts(
            "autosport.smarkets.session-authenticated-read.v2",
            candidate.session_generation_id,
            candidate.account_context_sha256,
            candidate.provider_account_id,
            candidate.provider_currency,
            candidate.endpoint,
            candidate.request_query,
            candidate.provider_date,
            candidate.content_type,
            candidate.product_available_at,
            candidate.payload_sha256,
            str(candidate.payload_size),
        )
        if candidate.evidence_sha256 != expected_evidence_sha256:
            raise SmarketsSessionContextError(
                "Smarkets account activity evidence fields no longer match issued identity"
            )
        return candidate

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
        # Provider/transport exception detail is not product evidence and may
        # contain credential-bearing diagnostics. Preserve only the bounded
        # numeric HTTP status and suppress the lower-level traceback cause.
        raise SmarketsSessionContextError(
            f"Smarkets accounts endpoint is unavailable (HTTP {exc.code})"
        ) from None
    except (URLError, TimeoutError, OSError):
        # Do not chain transport diagnostics into operator/log traceback text:
        # the runtime session token must remain secret even on acquisition failure.
        raise SmarketsSessionContextError(
            "Smarkets accounts HTTPS acquisition failed"
        ) from None

    if type(raw) is not bytes:
        raise SmarketsSessionContextError("Smarkets accounts response body is not bytes")
    if len(raw) > _MAX_ACCOUNTS_RESPONSE_BYTES:
        raise SmarketsSessionContextError("Smarkets accounts payload exceeds byte limit")
    if not raw:
        raise SmarketsSessionContextError("Smarkets accounts payload is empty")

    account = _account_record(raw)
    provider_account_id = account["account_id"]
    provider_currency = account["currency"]
    assert type(provider_account_id) is str
    assert type(provider_currency) is str
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
        provider_currency=provider_currency,
        _seal=_ACCOUNT_SEAL,
    )
    generation_id = _new_generation_id()
    account_context_sha256 = _digest_parts(
        "autosport.smarkets.session-account-context.v2",
        generation_id,
        SMARKETS_ACCOUNTS_ENDPOINT,
        provider_account_id,
        provider_currency,
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

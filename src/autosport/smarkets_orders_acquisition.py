"""Authenticated, fixed-origin acquisition for Smarkets order readback payloads.

This module establishes only provider-origin evidence for the official Smarkets
``GET /v3/orders/`` endpoint. It does not place/cancel orders, approve execution,
or claim real-money readiness. Downstream reconciliation must still bind a
witness to an existing canonical execution action and product-owned approval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from http.client import IncompleteRead
import json
import ssl
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

SMARKETS_ORDERS_ENDPOINT: Final = "https://api.smarkets.com/v3/orders/"
_MAX_RESPONSE_BYTES: Final = 8 * 1024 * 1024
_READ_CHUNK_BYTES: Final = 64 * 1024
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_ORIGIN_SEAL: Final = object()


class SmarketsOrdersAcquisitionError(RuntimeError):
    """The fixed-origin Smarkets readback could not be authenticated safely."""


class _NoRedirect(HTTPRedirectHandler):
    """Refuse redirects so origin authority cannot silently move hosts or paths."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _nonempty_token(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise SmarketsOrdersAcquisitionError("session token must be a non-empty trimmed string")
    if len(value) > 4096 or "\r" in value or "\n" in value:
        raise SmarketsOrdersAcquisitionError("session token is malformed")
    return value


def _strict_json(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SmarketsOrdersAcquisitionError("Smarkets orders payload is not UTF-8 JSON") from exc

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SmarketsOrdersAcquisitionError(
                    f"Smarkets orders payload contains duplicate JSON key {key!r}"
                )
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            object_pairs_hook=no_duplicates,
            parse_constant=lambda value: (_ for _ in ()).throw(
                SmarketsOrdersAcquisitionError(
                    f"Smarkets orders payload contains non-finite JSON value {value!r}"
                )
            ),
        )
    except SmarketsOrdersAcquisitionError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:
        raise SmarketsOrdersAcquisitionError("Smarkets orders payload is invalid JSON") from exc


def _provider_date(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise SmarketsOrdersAcquisitionError("Smarkets response lacks a valid Date header")
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SmarketsOrdersAcquisitionError("Smarkets response Date header is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SmarketsOrdersAcquisitionError("Smarkets response Date header is not timezone-aware")
    return parsed.isoformat()


def _content_type(value: object) -> str:
    if type(value) is not str:
        raise SmarketsOrdersAcquisitionError("Smarkets response lacks Content-Type")
    media_type = value.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise SmarketsOrdersAcquisitionError("Smarkets orders response is not application/json")
    return value.strip()


def _declared_content_length(headers: object) -> int | None:
    get_all = getattr(headers, "get_all", None)
    if callable(get_all):
        values = get_all("Content-Length", [])
    else:
        get = getattr(headers, "get", None)
        value = get("Content-Length") if callable(get) else None
        values = [] if value is None else [value]

    if not values:
        return None
    if len(values) != 1:
        raise SmarketsOrdersAcquisitionError(
            "Smarkets orders response has ambiguous Content-Length"
        )

    value = values[0]
    if type(value) is not str:
        raise SmarketsOrdersAcquisitionError(
            "Smarkets orders response Content-Length is invalid"
        )
    normalized = value.strip()
    if not normalized or not normalized.isascii() or not normalized.isdigit():
        raise SmarketsOrdersAcquisitionError(
            "Smarkets orders response Content-Length is invalid"
        )

    length = int(normalized)
    if length > _MAX_RESPONSE_BYTES:
        raise SmarketsOrdersAcquisitionError("Smarkets orders payload exceeds byte limit")
    return length


def _read_complete_body(response: object) -> bytes:
    headers = getattr(response, "headers", None)
    if headers is None:
        raise SmarketsOrdersAcquisitionError("Smarkets orders response lacks headers")
    declared_length = _declared_content_length(headers)
    chunks: list[bytes] = []
    total = 0

    try:
        if declared_length is not None:
            remaining = declared_length
            while remaining:
                chunk = response.read(min(_READ_CHUNK_BYTES, remaining))
                if type(chunk) is not bytes:
                    raise SmarketsOrdersAcquisitionError(
                        "Smarkets orders response body is not bytes"
                    )
                if not chunk:
                    raise SmarketsOrdersAcquisitionError(
                        "Smarkets orders response body ended before Content-Length"
                    )
                if len(chunk) > remaining:
                    raise SmarketsOrdersAcquisitionError(
                        "Smarkets orders response exceeds declared Content-Length"
                    )
                chunks.append(chunk)
                total += len(chunk)
                remaining -= len(chunk)

            extra = response.read(1)
            if type(extra) is not bytes:
                raise SmarketsOrdersAcquisitionError(
                    "Smarkets orders response body is not bytes"
                )
            if extra:
                raise SmarketsOrdersAcquisitionError(
                    "Smarkets orders response exceeds declared Content-Length"
                )
        else:
            while True:
                budget = _MAX_RESPONSE_BYTES + 1 - total
                if budget <= 0:
                    raise SmarketsOrdersAcquisitionError(
                        "Smarkets orders payload exceeds byte limit"
                    )
                chunk = response.read(min(_READ_CHUNK_BYTES, budget))
                if type(chunk) is not bytes:
                    raise SmarketsOrdersAcquisitionError(
                        "Smarkets orders response body is not bytes"
                    )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    raise SmarketsOrdersAcquisitionError(
                        "Smarkets orders payload exceeds byte limit"
                    )
    except IncompleteRead as exc:
        raise SmarketsOrdersAcquisitionError(
            "Smarkets orders response body is incomplete"
        ) from exc

    raw = b"".join(chunks)
    if declared_length is not None and len(raw) != declared_length:
        raise SmarketsOrdersAcquisitionError(
            "Smarkets orders response body length does not match Content-Length"
        )
    return raw


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _product_received_at() -> str:
    value = _utc_now()
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SmarketsOrdersAcquisitionError(
            "product receipt clock must return a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc).isoformat()


def _open_orders_request(request: Request, timeout: float):
    """Internal HTTPS authority boundary; tests patch this private seam only."""
    context = ssl.create_default_context()
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=context),
        _NoRedirect(),
    )
    return opener.open(request, timeout=timeout)


@dataclass(frozen=True, slots=True, init=False)
class SmarketsOrdersPayloadWitness:
    """Sealed evidence emitted only after a fixed-origin HTTPS orders readback."""

    endpoint: str
    http_status: int
    provider_date: str
    received_at: str
    content_type: str
    payload_sha256: str
    payload_size: int
    _payload: bytes

    def __init__(
        self,
        *,
        endpoint: str,
        http_status: int,
        provider_date: str,
        received_at: str,
        content_type: str,
        payload_sha256: str,
        payload_size: int,
        payload: bytes,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _ORIGIN_SEAL:
            raise TypeError(
                "SmarketsOrdersPayloadWitness is issued only by fixed-origin acquisition"
            )
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "http_status", http_status)
        object.__setattr__(self, "provider_date", provider_date)
        object.__setattr__(self, "received_at", received_at)
        object.__setattr__(self, "content_type", content_type)
        object.__setattr__(self, "payload_sha256", payload_sha256)
        object.__setattr__(self, "payload_size", payload_size)
        object.__setattr__(self, "_payload", bytes(payload))

    @property
    def payload_bytes(self) -> bytes:
        return bytes(self._payload)

    def parsed_json(self) -> Any:
        """Return strict parsed JSON while retaining raw-byte digest as authority."""
        return _strict_json(self._payload)

    def matches_payload(self, payload: bytes) -> bool:
        if type(payload) is not bytes:
            return False
        return len(payload) == self.payload_size and sha256(payload).hexdigest() == self.payload_sha256

    def __reduce__(self):
        raise TypeError(
            "SmarketsOrdersPayloadWitness is intentionally non-serializable; reacquire after restart"
        )


def acquire_smarkets_orders_payload(
    session_token: str,
    *,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
) -> SmarketsOrdersPayloadWitness:
    """Fetch and seal exact bytes from the official Smarkets orders endpoint.

    The caller can supply credentials and a bounded timeout only. The URL,
    HTTP method, opener, redirect policy, accepted status, media type, and byte
    ceiling are product-owned and are not caller-selectable.
    """
    token = _nonempty_token(session_token)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
        raise SmarketsOrdersAcquisitionError("timeout_seconds must be numeric")
    timeout = float(timeout_seconds)
    if not (0.1 <= timeout <= 60.0):
        raise SmarketsOrdersAcquisitionError("timeout_seconds must be in [0.1, 60]")

    request = Request(
        SMARKETS_ORDERS_ENDPOINT,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Session-Token {token}",
            "User-Agent": "Autosport/smarkets-orders-readback-1",
        },
    )

    try:
        response = _open_orders_request(request, timeout)
        with response:
            final_url = response.geturl()
            status = response.getcode()
            if final_url != SMARKETS_ORDERS_ENDPOINT:
                raise SmarketsOrdersAcquisitionError(
                    "Smarkets orders response final URL is not the fixed official endpoint"
                )
            if type(status) is not int or status != 200:
                raise SmarketsOrdersAcquisitionError(
                    f"Smarkets orders endpoint returned non-success status {status!r}"
                )
            content_type = _content_type(response.headers.get("Content-Type"))
            provider_date = _provider_date(response.headers.get("Date"))
            raw = _read_complete_body(response)
    except SmarketsOrdersAcquisitionError:
        raise
    except HTTPError as exc:
        raise SmarketsOrdersAcquisitionError(
            f"Smarkets orders endpoint returned HTTP {exc.code}"
        ) from exc
    except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
        raise SmarketsOrdersAcquisitionError("Smarkets orders HTTPS acquisition failed") from exc

    if type(raw) is not bytes:
        raise SmarketsOrdersAcquisitionError("Smarkets orders response body is not bytes")
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise SmarketsOrdersAcquisitionError("Smarkets orders payload exceeds byte limit")
    if not raw:
        raise SmarketsOrdersAcquisitionError("Smarkets orders payload is empty")

    _strict_json(raw)
    received_at = _product_received_at()
    digest = sha256(raw).hexdigest()
    return SmarketsOrdersPayloadWitness(
        endpoint=SMARKETS_ORDERS_ENDPOINT,
        http_status=200,
        provider_date=provider_date,
        received_at=received_at,
        content_type=content_type,
        payload_sha256=digest,
        payload_size=len(raw),
        payload=raw,
        _seal=_ORIGIN_SEAL,
    )

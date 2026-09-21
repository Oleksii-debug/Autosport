"""Fail-closed Betfair Exchange Stream TLS/authentication transport.

This module owns only the provider TLS/authentication/session boundary and bounded
opaque CRLF frame ingress. It does not decode stream JSON semantics, persist
credentials, auto-login with username/password/certificates, or expose betting-write
methods. Positive transport-origin objects are issued only after an exact persistence
receipt is returned for the frame.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import socket
import ssl
import time
from types import MappingProxyType
from typing import Protocol
from weakref import WeakKeyDictionary

from .betfair_account_readonly import BetfairSessionCredentials


BETFAIR_STREAM_HOST = "stream-api.betfair.com"
BETFAIR_STREAM_PORT = 443
ADAPTER_ID = "betfair-exchange-stream-tls"
ADAPTER_VERSION = "2"
_AUTH_REQUEST_ID = 1
_HANDSHAKE_MAX_BYTES = 64 * 1024
_DEFAULT_MAX_FRAME_BYTES = 4 * 1024 * 1024
_SOCKET_READ_BYTES = 64 * 1024
_MIN_TIMEOUT_SECONDS = 0.1
_MAX_TIMEOUT_SECONDS = 60.0
_BACKOFF_BASE_SECONDS = 1.0
_BACKOFF_MAX_SECONDS = 60.0
_ALLOWED_APP_KEY_CLASSES = frozenset({"DELAYED", "LIVE"})
_ALLOWED_ENVIRONMENTS = frozenset({"PRODUCTION"})


class BetfairStreamTransportError(RuntimeError):
    """Base fail-closed stream transport error."""


class BetfairStreamProtocolError(BetfairStreamTransportError):
    """Raised when the provider stream handshake/framing is structurally invalid."""


class BetfairStreamAuthenticationError(BetfairStreamTransportError):
    """Raised when credentials or provider authentication cannot be accepted."""


class BetfairStreamPersistenceError(BetfairStreamTransportError):
    """Raised when raw ingress cannot be durably accepted before publication."""


class BetfairStreamBackoffError(BetfairStreamTransportError):
    """Raised when a caller retries connection before the bounded retry window."""


@dataclass(frozen=True, slots=True)
class BetfairStreamSessionIdentity:
    """Non-secret product identity for one credential/session epoch."""

    account_id: str
    app_identity_id: str
    app_key_class: str
    session_epoch: int
    environment: str = "PRODUCTION"
    provider_id: str = "betfair"

    def __post_init__(self) -> None:
        _required_token(self.account_id, "account_id")
        _required_token(self.app_identity_id, "app_identity_id")
        if self.provider_id != "betfair":
            raise ValueError("provider_id must be canonical 'betfair'")
        if self.environment not in _ALLOWED_ENVIRONMENTS:
            raise ValueError("environment is not a supported Betfair stream environment")
        if self.app_key_class not in _ALLOWED_APP_KEY_CLASSES:
            raise ValueError("app_key_class must be DELAYED or LIVE")
        if type(self.session_epoch) is not int or self.session_epoch <= 0:
            raise ValueError("session_epoch must be a positive non-boolean integer")

    @property
    def identity_sha256(self) -> str:
        return sha256(
            _canonical_json(
                {
                    "adapter_id": ADAPTER_ID,
                    "adapter_version": ADAPTER_VERSION,
                    "provider_id": self.provider_id,
                    "account_id": self.account_id,
                    "app_identity_id": self.app_identity_id,
                    "app_key_class": self.app_key_class,
                    "session_epoch": self.session_epoch,
                    "environment": self.environment,
                }
            )
        ).hexdigest()

    @property
    def grants_provider_write_authority(self) -> bool:
        return False

    @property
    def grants_live_decision_authority(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, repr=False)
class BetfairStreamCredentialLease:
    """Secret-bearing lease paired with independently supplied non-secret epoch metadata."""

    account_id: str
    app_identity_id: str
    app_key_class: str
    session_epoch: int
    credentials: BetfairSessionCredentials

    def __post_init__(self) -> None:
        _required_token(self.account_id, "account_id")
        _required_token(self.app_identity_id, "app_identity_id")
        if self.app_key_class not in _ALLOWED_APP_KEY_CLASSES:
            raise ValueError("app_key_class must be DELAYED or LIVE")
        if type(self.session_epoch) is not int or self.session_epoch <= 0:
            raise ValueError("session_epoch must be a positive non-boolean integer")
        if type(self.credentials) is not BetfairSessionCredentials:
            raise TypeError("credentials must be canonical BetfairSessionCredentials")

    def __repr__(self) -> str:
        return (
            "BetfairStreamCredentialLease("
            f"account_id={self.account_id!r}, app_identity_id={self.app_identity_id!r}, "
            f"app_key_class={self.app_key_class!r}, session_epoch={self.session_epoch!r}, "
            "credentials=<redacted>)"
        )

    def matches(self, identity: BetfairStreamSessionIdentity) -> bool:
        return (
            self.account_id == identity.account_id
            and self.app_identity_id == identity.app_identity_id
            and self.app_key_class == identity.app_key_class
            and self.session_epoch == identity.session_epoch
        )


class BetfairStreamSecretProvider(Protocol):
    """Memory-only secret source; autonomous login remains a separate component."""

    def get_session_lease(self) -> BetfairStreamCredentialLease: ...


@dataclass(frozen=True, slots=True, repr=False)
class BetfairStreamRawFrame:
    """Structural exact CRLF frame presented to the durable sink before publication."""

    session_identity_sha256: str
    connection_id: str
    frame_sequence: int
    payload: bytes
    payload_sha256: str

    def __post_init__(self) -> None:
        _validate_frame_fields(
            self.session_identity_sha256,
            self.connection_id,
            self.frame_sequence,
            self.payload,
            self.payload_sha256,
        )

    def __repr__(self) -> str:
        return (
            "BetfairStreamRawFrame("
            f"session_identity_sha256={self.session_identity_sha256!r}, "
            f"connection_id={self.connection_id!r}, "
            f"frame_sequence={self.frame_sequence!r}, "
            f"payload_sha256={self.payload_sha256!r}, payload=<redacted>)"
        )


@dataclass(frozen=True, slots=True)
class BetfairStreamPersistenceReceipt:
    """Sink acknowledgement binding the exact frame it claims to have persisted."""

    session_identity_sha256: str
    connection_id: str
    frame_sequence: int
    payload_sha256: str

    def __post_init__(self) -> None:
        _sha256_hex(self.session_identity_sha256, "session_identity_sha256")
        _required_token(self.connection_id, "connection_id")
        if type(self.frame_sequence) is not int or self.frame_sequence <= 0:
            raise ValueError("frame_sequence must be a positive non-boolean integer")
        _sha256_hex(self.payload_sha256, "payload_sha256")

    @classmethod
    def for_frame(
        cls, frame: BetfairStreamRawFrame
    ) -> BetfairStreamPersistenceReceipt:
        if type(frame) is not BetfairStreamRawFrame:
            raise TypeError("frame must be BetfairStreamRawFrame")
        return cls(
            frame.session_identity_sha256,
            frame.connection_id,
            frame.frame_sequence,
            frame.payload_sha256,
        )

    def matches(self, frame: BetfairStreamRawFrame) -> bool:
        return (
            self.session_identity_sha256 == frame.session_identity_sha256
            and self.connection_id == frame.connection_id
            and self.frame_sequence == frame.frame_sequence
            and self.payload_sha256 == frame.payload_sha256
        )


class BetfairStreamFrameSink(Protocol):
    def persist(
        self, frame: BetfairStreamRawFrame
    ) -> BetfairStreamPersistenceReceipt: ...


@dataclass(frozen=True, slots=True, repr=False, weakref_slot=True, eq=False)
class BetfairStreamPersistedFrame:
    """Transport-issued frame eligible for downstream semantic decoding."""

    session_identity_sha256: str
    connection_id: str
    frame_sequence: int
    payload: bytes
    payload_sha256: str

    def __post_init__(self) -> None:
        _validate_frame_fields(
            self.session_identity_sha256,
            self.connection_id,
            self.frame_sequence,
            self.payload,
            self.payload_sha256,
        )

    def __repr__(self) -> str:
        return (
            "BetfairStreamPersistedFrame("
            f"session_identity_sha256={self.session_identity_sha256!r}, "
            f"connection_id={self.connection_id!r}, "
            f"frame_sequence={self.frame_sequence!r}, "
            f"payload_sha256={self.payload_sha256!r}, payload=<redacted>)"
        )

    def assert_transport_issued(self) -> None:
        expected = _ISSUED_PERSISTED_FRAMES.get(self)
        if expected is None or expected != _persisted_frame_fingerprint(self):
            raise BetfairStreamPersistenceError(
                "Betfair persisted frame was not issued by the authenticated transport"
            )


_ISSUED_PERSISTED_FRAMES: WeakKeyDictionary[BetfairStreamPersistedFrame, str] = (
    WeakKeyDictionary()
)


class _TlsSocket(Protocol):
    def recv(self, size: int) -> bytes: ...
    def sendall(self, data: bytes) -> None: ...
    def settimeout(self, value: float) -> None: ...
    def shutdown(self, how: int) -> None: ...
    def close(self) -> None: ...


class BetfairStreamTlsTransport:
    """Verified TLS/auth session with bounded retry and persist-before-publish frames."""

    def __init__(
        self,
        *,
        identity: BetfairStreamSessionIdentity,
        secret_provider: BetfairStreamSecretProvider,
        timeout_seconds: float = 10.0,
        max_frame_bytes: int = _DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        if not isinstance(identity, BetfairStreamSessionIdentity):
            raise TypeError("identity must be BetfairStreamSessionIdentity")
        if not hasattr(secret_provider, "get_session_lease"):
            raise TypeError("secret_provider must provide get_session_lease()")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not _MIN_TIMEOUT_SECONDS
            <= float(timeout_seconds)
            <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds must be in 0.1..60.0")
        if (
            type(max_frame_bytes) is not int
            or max_frame_bytes <= 0
            or max_frame_bytes > 16 * 1024 * 1024
        ):
            raise ValueError("max_frame_bytes must be in 1..16777216")
        self._identity = identity
        self._secret_provider = secret_provider
        self._timeout_seconds = float(timeout_seconds)
        self._max_frame_bytes = max_frame_bytes
        self._socket: _TlsSocket | None = None
        self._receive_buffer = bytearray()
        self._connection_id: str | None = None
        self._frame_sequence = 0
        self._consecutive_connect_failures = 0
        self._next_connect_monotonic = 0.0

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(adapter_id={ADAPTER_ID!r}, "
            f"adapter_version={ADAPTER_VERSION!r}, "
            f"session_identity_sha256={self._identity.identity_sha256!r}, "
            f"connected={self._socket is not None!r})"
        )

    @property
    def identity(self) -> BetfairStreamSessionIdentity:
        return self._identity

    @property
    def is_authenticated(self) -> bool:
        return self._socket is not None and self._connection_id is not None

    @property
    def connection_id(self) -> str | None:
        return self._connection_id

    @property
    def grants_provider_write_authority(self) -> bool:
        return False

    @property
    def grants_live_decision_authority(self) -> bool:
        return False

    @property
    def requires_resubscription_after_connect(self) -> bool:
        return True

    def connect(self) -> str:
        """Open verified TLS and require a positive provider authentication status."""

        if self._socket is not None:
            raise BetfairStreamTransportError(
                "Betfair stream transport is already connected"
            )
        if time.monotonic() < self._next_connect_monotonic:
            raise BetfairStreamBackoffError(
                "Betfair stream reconnect backoff is active"
            )

        try:
            lease = self._secret_provider.get_session_lease()
        except Exception:
            self._record_connect_failure()
            raise BetfairStreamAuthenticationError(
                "Betfair stream credentials are unavailable"
            ) from None
        if (
            type(lease) is not BetfairStreamCredentialLease
            or not lease.matches(self._identity)
        ):
            self._record_connect_failure()
            raise BetfairStreamAuthenticationError(
                "Betfair stream credential lease does not match the configured session epoch"
            )

        try:
            stream = _open_verified_tls_socket(self._timeout_seconds)
        except (OSError, ssl.SSLError, TimeoutError):
            self._record_connect_failure()
            raise BetfairStreamTransportError(
                "Betfair stream TLS connection failed"
            ) from None

        self._socket = stream
        self._receive_buffer.clear()
        self._connection_id = None
        self._frame_sequence = 0

        try:
            connection = self._read_handshake_object("connection")
            if connection.get("op") != "connection":
                raise BetfairStreamProtocolError(
                    "Betfair stream did not begin with a connection message"
                )
            connection_id = _required_token(
                connection.get("connectionId"), "connectionId"
            )

            credentials = lease.credentials
            auth_request = {
                "op": "authentication",
                "id": _AUTH_REQUEST_ID,
                "appKey": credentials.application_key,
                "session": credentials.session_token,
            }
            auth_bytes = _canonical_json(auth_request) + b"\r\n"
            try:
                stream.sendall(auth_bytes)
            except (OSError, ssl.SSLError, TimeoutError):
                raise BetfairStreamTransportError(
                    "Betfair stream authentication request failed"
                ) from None
            finally:
                auth_request = None
                auth_bytes = None
                credentials = None
                lease = None

            status = self._read_handshake_object("authentication status")
            if status.get("op") != "status":
                raise BetfairStreamProtocolError(
                    "Betfair stream authentication response is not a status message"
                )
            if (
                type(status.get("id")) is not int
                or status.get("id") != _AUTH_REQUEST_ID
            ):
                raise BetfairStreamProtocolError(
                    "Betfair stream authentication response id mismatch"
                )
            if status.get("statusCode") != "SUCCESS":
                raise BetfairStreamAuthenticationError(
                    "Betfair stream authentication was rejected"
                )
            if status.get("connectionClosed") is True:
                raise BetfairStreamAuthenticationError(
                    "Betfair stream authentication closed the connection"
                )
            status_connection_id = status.get("connectionId")
            if (
                status_connection_id is not None
                and status_connection_id != connection_id
            ):
                raise BetfairStreamProtocolError(
                    "Betfair stream authentication connection id mismatch"
                )

            self._connection_id = connection_id
            self._consecutive_connect_failures = 0
            self._next_connect_monotonic = 0.0
            return connection_id
        except BetfairStreamTransportError:
            self.close()
            self._record_connect_failure()
            raise
        except (
            OSError,
            ssl.SSLError,
            TimeoutError,
            UnicodeError,
            ValueError,
            TypeError,
        ):
            self.close()
            self._record_connect_failure()
            raise BetfairStreamProtocolError(
                "Betfair stream handshake failed validation"
            ) from None

    def read_persisted_frame(
        self,
        sink: BetfairStreamFrameSink,
    ) -> BetfairStreamPersistedFrame:
        """Return one complete exact CRLF frame only after matching persistence receipt."""

        if not hasattr(sink, "persist"):
            raise TypeError("sink must provide persist()")
        stream = self._socket
        connection_id = self._connection_id
        if stream is None or connection_id is None:
            raise BetfairStreamTransportError(
                "Betfair stream transport is not authenticated"
            )

        while True:
            marker = self._receive_buffer.find(b"\r\n")
            if marker >= 0:
                if marker > self._max_frame_bytes:
                    self._close_with_backoff()
                    raise BetfairStreamProtocolError(
                        "Betfair stream frame exceeded the configured size limit"
                    )
                payload = bytes(self._receive_buffer[: marker + 2])
                del self._receive_buffer[: marker + 2]
                if marker == 0:
                    self._close_with_backoff()
                    raise BetfairStreamProtocolError(
                        "Betfair stream frame must not be empty"
                    )
                break

            bare_lf = self._receive_buffer.find(b"\n")
            if bare_lf >= 0 and (
                bare_lf == 0 or self._receive_buffer[bare_lf - 1] != 0x0D
            ):
                self._close_with_backoff()
                raise BetfairStreamProtocolError(
                    "Betfair stream frame used a non-CRLF delimiter"
                )
            if len(self._receive_buffer) > self._max_frame_bytes:
                self._close_with_backoff()
                raise BetfairStreamProtocolError(
                    "Betfair stream frame exceeded the configured size limit"
                )

            try:
                block = stream.recv(_SOCKET_READ_BYTES)
            except (OSError, ssl.SSLError, TimeoutError):
                self._close_with_backoff()
                raise BetfairStreamTransportError(
                    "Betfair stream receive failed"
                ) from None
            if not block:
                had_partial = bool(self._receive_buffer)
                self._close_with_backoff()
                if had_partial:
                    raise BetfairStreamProtocolError(
                        "Betfair stream disconnected with a truncated frame"
                    )
                raise BetfairStreamTransportError(
                    "Betfair stream connection closed before receiving data"
                )
            self._receive_buffer.extend(block)

        self._frame_sequence += 1
        raw_frame = BetfairStreamRawFrame(
            session_identity_sha256=self._identity.identity_sha256,
            connection_id=connection_id,
            frame_sequence=self._frame_sequence,
            payload=payload,
            payload_sha256=sha256(payload).hexdigest(),
        )
        try:
            receipt = sink.persist(raw_frame)
        except Exception:
            self._close_with_backoff()
            raise BetfairStreamPersistenceError(
                "Betfair stream raw frame persistence failed"
            ) from None
        if (
            type(receipt) is not BetfairStreamPersistenceReceipt
            or not receipt.matches(raw_frame)
        ):
            self._close_with_backoff()
            raise BetfairStreamPersistenceError(
                "Betfair stream persistence receipt did not match the raw frame"
            )

        issued = BetfairStreamPersistedFrame(
            raw_frame.session_identity_sha256,
            raw_frame.connection_id,
            raw_frame.frame_sequence,
            raw_frame.payload,
            raw_frame.payload_sha256,
        )
        _ISSUED_PERSISTED_FRAMES[issued] = _persisted_frame_fingerprint(issued)
        return issued


    def _close_with_backoff(self) -> None:
        """Close a failed live connection and rate-limit the next reconnect attempt."""

        self.close()
        self._record_connect_failure()

    def close(self) -> None:
        stream = self._socket
        self._socket = None
        self._connection_id = None
        self._receive_buffer.clear()
        if stream is None:
            return
        try:
            stream.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        try:
            stream.close()
        except OSError:
            pass

    def _record_connect_failure(self) -> None:
        self._consecutive_connect_failures += 1
        exponent = min(self._consecutive_connect_failures - 1, 16)
        delay = min(
            _BACKOFF_BASE_SECONDS * (2**exponent),
            _BACKOFF_MAX_SECONDS,
        )
        self._next_connect_monotonic = time.monotonic() + delay

    def _read_handshake_object(self, label: str) -> dict[str, object]:
        stream = self._socket
        if stream is None:
            raise BetfairStreamTransportError(
                "Betfair stream transport is not connected"
            )

        while True:
            marker = self._receive_buffer.find(b"\r\n")
            if marker >= 0:
                if marker > _HANDSHAKE_MAX_BYTES:
                    raise BetfairStreamProtocolError(
                        "Betfair stream handshake message exceeded the size limit"
                    )
                raw = bytes(self._receive_buffer[:marker])
                del self._receive_buffer[: marker + 2]
                if not raw:
                    raise BetfairStreamProtocolError(
                        "Betfair stream handshake contained an empty message"
                    )
                return _strict_json_object(raw, label)
            if len(self._receive_buffer) > _HANDSHAKE_MAX_BYTES:
                raise BetfairStreamProtocolError(
                    "Betfair stream handshake message exceeded the size limit"
                )
            try:
                block = stream.recv(
                    min(
                        4096,
                        _HANDSHAKE_MAX_BYTES
                        + 2
                        - len(self._receive_buffer),
                    )
                )
            except (OSError, ssl.SSLError, TimeoutError):
                raise BetfairStreamTransportError(
                    "Betfair stream handshake receive failed"
                ) from None
            if not block:
                raise BetfairStreamTransportError(
                    "Betfair stream connection closed during handshake"
                )
            self._receive_buffer.extend(block)


def _open_verified_tls_socket(timeout_seconds: float) -> _TlsSocket:
    raw = socket.create_connection(
        (BETFAIR_STREAM_HOST, BETFAIR_STREAM_PORT),
        timeout=timeout_seconds,
    )
    try:
        context = ssl.create_default_context()
        tls = context.wrap_socket(
            raw,
            server_hostname=BETFAIR_STREAM_HOST,
        )
        tls.settimeout(timeout_seconds)
        return tls
    except Exception:
        try:
            raw.close()
        except OSError:
            pass
        raise


def _persisted_frame_fingerprint(
    frame: BetfairStreamPersistedFrame,
) -> str:
    return sha256(
        _canonical_json(
            {
                "session_identity_sha256": frame.session_identity_sha256,
                "connection_id": frame.connection_id,
                "frame_sequence": frame.frame_sequence,
                "payload_sha256": frame.payload_sha256,
            }
        )
    ).hexdigest()


def _validate_frame_fields(
    session_identity_sha256: object,
    connection_id: object,
    frame_sequence: object,
    payload: object,
    payload_sha256: object,
) -> None:
    _sha256_hex(session_identity_sha256, "session_identity_sha256")
    _required_token(connection_id, "connection_id")
    if type(frame_sequence) is not int or frame_sequence <= 0:
        raise ValueError("frame_sequence must be a positive non-boolean integer")
    if type(payload) is not bytes or not payload:
        raise ValueError("payload must be non-empty bytes")
    if not payload.endswith(b"\r\n"):
        raise ValueError("payload must contain one complete CRLF-terminated frame")
    if b"\r\n" in payload[:-2]:
        raise ValueError(
            "payload must contain exactly one CRLF-terminated frame"
        )
    _sha256_hex(payload_sha256, "payload_sha256")
    if payload_sha256 != sha256(payload).hexdigest():
        raise ValueError("payload_sha256 does not match payload")


def _strict_json_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        text_value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise BetfairStreamProtocolError(
            f"Betfair stream {label} is not valid UTF-8"
        ) from None

    def pairs(
        items: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        decoded = json.loads(
            text_value,
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite")
            ),
        )
    except (json.JSONDecodeError, ValueError):
        raise BetfairStreamProtocolError(
            f"Betfair stream {label} is not strict JSON"
        ) from None
    if type(decoded) is not dict:
        raise BetfairStreamProtocolError(
            f"Betfair stream {label} must be a JSON object"
        )
    return decoded


def _required_token(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty trimmed text")
    if any(ord(character) < 0x20 for character in value):
        raise ValueError(f"{name} contains control characters")
    return value


def _sha256_hex(value: object, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(
            character not in "0123456789abcdef"
            for character in value
        )
    ):
        raise ValueError(f"{name} must be lowercase SHA-256 hex")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


PUBLIC_PROTOCOL = MappingProxyType(
    {
        "host": BETFAIR_STREAM_HOST,
        "port": BETFAIR_STREAM_PORT,
        "transport": "TLS_SOCKET_CRLF_JSON",
        "max_frame_bytes": _DEFAULT_MAX_FRAME_BYTES,
        "minimum_reconnect_backoff_seconds": _BACKOFF_BASE_SECONDS,
        "maximum_reconnect_backoff_seconds": _BACKOFF_MAX_SECONDS,
        "requires_authentication_before_subscription": True,
        "requires_resubscription_after_reconnect": True,
        "market_write_authority": False,
        "betting_write_authority": False,
        "live_decision_authority": False,
    }
)


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "BETFAIR_STREAM_HOST",
    "BETFAIR_STREAM_PORT",
    "PUBLIC_PROTOCOL",
    "BetfairStreamAuthenticationError",
    "BetfairStreamBackoffError",
    "BetfairStreamCredentialLease",
    "BetfairStreamFrameSink",
    "BetfairStreamPersistenceError",
    "BetfairStreamPersistenceReceipt",
    "BetfairStreamPersistedFrame",
    "BetfairStreamProtocolError",
    "BetfairStreamRawFrame",
    "BetfairStreamSecretProvider",
    "BetfairStreamSessionIdentity",
    "BetfairStreamTlsTransport",
    "BetfairStreamTransportError",
]

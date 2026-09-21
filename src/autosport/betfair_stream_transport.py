"""Fail-closed Betfair Exchange Stream TLS/authentication transport.

The transport owns only the provider connection/authentication/session boundary and
bounded raw byte ingress. It deliberately does not decode market/order stream
messages, mutate Market Mirror state, persist credentials, or expose betting-write
methods. Post-authentication bytes are returned only after the caller's durable sink
has accepted the exact raw chunk.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import socket
import ssl
from types import MappingProxyType
from typing import Callable, Protocol

from .betfair_account_readonly import BetfairSessionCredentials


BETFAIR_STREAM_HOST = "stream-api.betfair.com"
BETFAIR_STREAM_PORT = 443
ADAPTER_ID = "betfair-exchange-stream-tls"
ADAPTER_VERSION = "1"
_AUTH_REQUEST_ID = 1
_HANDSHAKE_MAX_BYTES = 64 * 1024
_ALLOWED_APP_KEY_CLASSES = frozenset({"DELAYED", "LIVE"})
_ALLOWED_ENVIRONMENTS = frozenset({"PRODUCTION"})


class BetfairStreamTransportError(RuntimeError):
    """Base fail-closed stream transport error."""


class BetfairStreamProtocolError(BetfairStreamTransportError):
    """Raised when the provider stream handshake is structurally invalid."""


class BetfairStreamAuthenticationError(BetfairStreamTransportError):
    """Raised when provider authentication is not positively acknowledged."""


class BetfairStreamPersistenceError(BetfairStreamTransportError):
    """Raised when raw ingress cannot be durably accepted before publication."""


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
        payload = {
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "app_identity_id": self.app_identity_id,
            "app_key_class": self.app_key_class,
            "session_epoch": self.session_epoch,
            "environment": self.environment,
        }
        return sha256(_canonical_json(payload)).hexdigest()

    @property
    def grants_provider_write_authority(self) -> bool:
        return False


class BetfairStreamSecretProvider(Protocol):
    """Memory-only credential source. Implementations own secret acquisition/storage."""

    def get_session_credentials(
        self, identity: BetfairStreamSessionIdentity
    ) -> BetfairSessionCredentials: ...


@dataclass(frozen=True, slots=True, repr=False)
class BetfairStreamRawChunk:
    """Exact post-authentication transport bytes awaiting semantic stream decoding."""

    session_identity_sha256: str
    connection_id: str
    chunk_sequence: int
    payload: bytes
    payload_sha256: str

    def __post_init__(self) -> None:
        _sha256_hex(self.session_identity_sha256, "session_identity_sha256")
        _required_token(self.connection_id, "connection_id")
        if type(self.chunk_sequence) is not int or self.chunk_sequence <= 0:
            raise ValueError("chunk_sequence must be a positive non-boolean integer")
        if type(self.payload) is not bytes or not self.payload:
            raise ValueError("payload must be non-empty bytes")
        _sha256_hex(self.payload_sha256, "payload_sha256")
        if self.payload_sha256 != sha256(self.payload).hexdigest():
            raise ValueError("payload_sha256 does not match payload")

    def __repr__(self) -> str:
        return (
            "BetfairStreamRawChunk("
            f"session_identity_sha256={self.session_identity_sha256!r}, "
            f"connection_id={self.connection_id!r}, "
            f"chunk_sequence={self.chunk_sequence!r}, "
            f"payload_sha256={self.payload_sha256!r}, payload=<redacted>)"
        )


class _TlsSocket(Protocol):
    def recv(self, size: int) -> bytes: ...
    def sendall(self, data: bytes) -> None: ...
    def shutdown(self, how: int) -> None: ...
    def close(self) -> None: ...


class BetfairStreamTlsTransport:
    """Synchronous verified-TLS Betfair stream transport with persist-first ingress."""

    def __init__(
        self,
        *,
        identity: BetfairStreamSessionIdentity,
        secret_provider: BetfairStreamSecretProvider,
        timeout_seconds: float = 10.0,
        max_chunk_bytes: int = 64 * 1024,
    ) -> None:
        if not isinstance(identity, BetfairStreamSessionIdentity):
            raise TypeError("identity must be BetfairStreamSessionIdentity")
        if not hasattr(secret_provider, "get_session_credentials"):
            raise TypeError("secret_provider must provide get_session_credentials()")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        if (
            type(max_chunk_bytes) is not int
            or max_chunk_bytes <= 0
            or max_chunk_bytes > 8 * 1024 * 1024
        ):
            raise ValueError("max_chunk_bytes must be in 1..8388608")
        self._identity = identity
        self._secret_provider = secret_provider
        self._timeout_seconds = float(timeout_seconds)
        self._max_chunk_bytes = max_chunk_bytes
        self._socket: _TlsSocket | None = None
        self._receive_buffer = bytearray()
        self._connection_id: str | None = None
        self._chunk_sequence = 0

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

    def connect(self) -> str:
        """Open verified TLS, authenticate, and return the provider connection id."""

        if self._socket is not None:
            raise BetfairStreamTransportError("Betfair stream transport is already connected")

        try:
            stream = _open_verified_tls_socket(self._timeout_seconds)
        except (OSError, ssl.SSLError, TimeoutError):
            raise BetfairStreamTransportError(
                "Betfair stream TLS connection failed"
            ) from None

        self._socket = stream
        self._receive_buffer.clear()
        self._connection_id = None
        self._chunk_sequence = 0

        try:
            connection = self._read_handshake_object("connection")
            if connection.get("op") != "connection":
                raise BetfairStreamProtocolError(
                    "Betfair stream did not begin with a connection message"
                )
            connection_id = _required_token(
                connection.get("connectionId"), "connectionId"
            )

            try:
                credentials = self._secret_provider.get_session_credentials(self._identity)
            except Exception:
                raise BetfairStreamAuthenticationError(
                    "Betfair stream credentials are unavailable"
                ) from None
            if type(credentials) is not BetfairSessionCredentials:
                raise BetfairStreamAuthenticationError(
                    "Betfair stream credential provider returned an invalid credential type"
                )

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

            status = self._read_handshake_object("authentication status")
            if status.get("op") != "status":
                raise BetfairStreamProtocolError(
                    "Betfair stream authentication response is not a status message"
                )
            if type(status.get("id")) is not int or status.get("id") != _AUTH_REQUEST_ID:
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
            if status_connection_id is not None and status_connection_id != connection_id:
                raise BetfairStreamProtocolError(
                    "Betfair stream authentication connection id mismatch"
                )

            self._connection_id = connection_id
            return connection_id
        except BetfairStreamTransportError:
            self.close()
            raise
        except (OSError, ssl.SSLError, TimeoutError, UnicodeError, ValueError, TypeError):
            self.close()
            raise BetfairStreamProtocolError(
                "Betfair stream handshake failed validation"
            ) from None

    def read_persisted_chunk(
        self,
        persist: Callable[[BetfairStreamRawChunk], None],
    ) -> BetfairStreamRawChunk:
        """Read one bounded raw chunk and publish it only after persist returns."""

        if not callable(persist):
            raise TypeError("persist must be callable")
        stream = self._socket
        connection_id = self._connection_id
        if stream is None or connection_id is None:
            raise BetfairStreamTransportError(
                "Betfair stream transport is not authenticated"
            )

        try:
            if self._receive_buffer:
                payload = bytes(self._receive_buffer[: self._max_chunk_bytes])
                del self._receive_buffer[: len(payload)]
            else:
                payload = stream.recv(self._max_chunk_bytes)
        except (OSError, ssl.SSLError, TimeoutError):
            self.close()
            raise BetfairStreamTransportError(
                "Betfair stream receive failed"
            ) from None
        if not payload:
            self.close()
            raise BetfairStreamTransportError(
                "Betfair stream connection closed before receiving data"
            )

        self._chunk_sequence += 1
        chunk = BetfairStreamRawChunk(
            session_identity_sha256=self._identity.identity_sha256,
            connection_id=connection_id,
            chunk_sequence=self._chunk_sequence,
            payload=payload,
            payload_sha256=sha256(payload).hexdigest(),
        )
        try:
            persist(chunk)
        except Exception:
            self.close()
            raise BetfairStreamPersistenceError(
                "Betfair stream raw ingress persistence failed"
            ) from None
        return chunk

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

    def _read_handshake_object(self, label: str) -> dict[str, object]:
        stream = self._socket
        if stream is None:
            raise BetfairStreamTransportError("Betfair stream transport is not connected")

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
                        _HANDSHAKE_MAX_BYTES + 2 - len(self._receive_buffer),
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
        (BETFAIR_STREAM_HOST, BETFAIR_STREAM_PORT), timeout=timeout_seconds
    )
    try:
        context = ssl.create_default_context()
        return context.wrap_socket(raw, server_hostname=BETFAIR_STREAM_HOST)
    except Exception:
        try:
            raw.close()
        except OSError:
            pass
        raise


def _strict_json_object(raw: bytes, label: str) -> dict[str, object]:
    try:
        text_value = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise BetfairStreamProtocolError(
            f"Betfair stream {label} is not valid UTF-8"
        ) from None

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
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
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite")),
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
        or any(character not in "0123456789abcdef" for character in value)
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
        "market_write_authority": False,
        "betting_write_authority": False,
    }
)


__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "BETFAIR_STREAM_HOST",
    "BETFAIR_STREAM_PORT",
    "PUBLIC_PROTOCOL",
    "BetfairStreamAuthenticationError",
    "BetfairStreamPersistenceError",
    "BetfairStreamProtocolError",
    "BetfairStreamRawChunk",
    "BetfairStreamSecretProvider",
    "BetfairStreamSessionIdentity",
    "BetfairStreamTlsTransport",
    "BetfairStreamTransportError",
]

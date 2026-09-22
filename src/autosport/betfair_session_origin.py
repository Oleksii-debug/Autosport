"""Product-issued Betfair login/jurisdiction origin bound to authenticated K07 context.

The Betting and Accounts JSON-RPC endpoints are shared by multiple Betfair
jurisdictions, so they cannot prove whether an authenticated session came from the
Global/.com, .es, .it, .ro, or .com.au login domain.  This module records that fact at
the only causal point where Autosport can prove it: a successful product-owned login
request to one exact jurisdiction endpoint.

No username, password, application key, session token, certificate/key bytes, or raw
login response is serialized into the public authority objects.  This module does not
place, cancel, replace, or otherwise authorize wagers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
import hmac
import json
from pathlib import Path
from secrets import token_bytes
import ssl
from threading import RLock
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from weakref import ReferenceType, ref

from .betfair_account_identity import (
    BetfairAuthenticatedAccountIdentity,
    require_authoritative_betfair_account_identity,
)
from .betfair_account_readonly import BetfairReadOnlyClient, BetfairSessionCredentials


VENUE_ID = "betfair"
LOGIN_METHOD = "NON_INTERACTIVE_CERT"
ORIGIN_SCHEMA = "autosport.betfair_session_origin"
ORIGIN_SCHEMA_VERSION = 1
BOUND_SCHEMA = "autosport.betfair_authenticated_jurisdiction"
BOUND_SCHEMA_VERSION = 1
_MAX_RESPONSE_BYTES = 64 * 1024
_PROCESS_HMAC_KEY = token_bytes(32)


class BetfairSessionOriginError(RuntimeError):
    """Raised when login-origin or jurisdiction authority cannot be proven safely."""


class BetfairLoginJurisdiction(str, Enum):
    GLOBAL_COM = "GLOBAL_COM"
    AUSTRALIA_NEW_ZEALAND = "AUSTRALIA_NEW_ZEALAND"
    ITALY = "ITALY"
    SPAIN = "SPAIN"
    ROMANIA = "ROMANIA"


_CERT_LOGIN_ENDPOINTS = {
    BetfairLoginJurisdiction.GLOBAL_COM: "https://identitysso-cert.betfair.com/api/certlogin",
    BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND: "https://identitysso-cert.betfair.com.au/api/certlogin",
    BetfairLoginJurisdiction.ITALY: "https://identitysso-cert.betfair.it/api/certlogin",
    BetfairLoginJurisdiction.SPAIN: "https://identitysso-cert.betfair.es/api/certlogin",
    BetfairLoginJurisdiction.ROMANIA: "https://identitysso-cert.betfair.ro/api/certlogin",
}


@dataclass(frozen=True, slots=True, repr=False)
class BetfairNonInteractiveLoginSecrets:
    """Memory-only login inputs. Repr/str never disclose secret values."""

    application_key: str
    username: str
    password: str
    certificate_path: Path
    private_key_path: Path | None = None

    def __post_init__(self) -> None:
        _secret_text(self.application_key, "application_key")
        _secret_text(self.username, "username")
        _secret_text(self.password, "password")
        if not isinstance(self.certificate_path, Path):
            raise BetfairSessionOriginError("certificate_path must be pathlib.Path")
        if self.private_key_path is not None and not isinstance(self.private_key_path, Path):
            raise BetfairSessionOriginError("private_key_path must be pathlib.Path or None")

    def __repr__(self) -> str:
        return (
            "BetfairNonInteractiveLoginSecrets("
            "application_key=<redacted>, username=<redacted>, password=<redacted>, "
            "certificate_path=<redacted>, private_key_path=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True, repr=False)
class BetfairNonInteractiveLoginResponse:
    login_status: str
    session_token: str | None
    response_sha256: str

    def __post_init__(self) -> None:
        _login_status(self.login_status)
        _sha256_hex(self.response_sha256, "response_sha256")
        if self.login_status == "SUCCESS":
            _secret_text(self.session_token, "session_token")
        elif self.session_token is not None:
            raise BetfairSessionOriginError(
                "failed login response must not carry a session token"
            )

    def __repr__(self) -> str:
        token = "<redacted>" if self.session_token is not None else "None"
        return (
            "BetfairNonInteractiveLoginResponse("
            f"login_status={self.login_status!r}, session_token={token}, "
            f"response_sha256={self.response_sha256!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairSessionOrigin:
    """Public secret-free proof bytes for one product-owned successful login."""

    venue_id: str
    login_method: str
    jurisdiction: BetfairLoginJurisdiction
    login_endpoint: str
    issued_at: datetime
    response_sha256: str

    def __post_init__(self) -> None:
        if self.venue_id != VENUE_ID:
            raise BetfairSessionOriginError("venue_id is product-owned")
        if self.login_method != LOGIN_METHOD:
            raise BetfairSessionOriginError("login_method is product-owned")
        if type(self.jurisdiction) is not BetfairLoginJurisdiction:
            raise BetfairSessionOriginError(
                "jurisdiction must be exact BetfairLoginJurisdiction"
            )
        if self.login_endpoint != _CERT_LOGIN_ENDPOINTS[self.jurisdiction]:
            raise BetfairSessionOriginError(
                "login_endpoint does not match jurisdiction"
            )
        _utc(self.issued_at, "issued_at")
        _sha256_hex(self.response_sha256, "response_sha256")

    @property
    def origin_id(self) -> str:
        payload = {
            "schema": ORIGIN_SCHEMA,
            "schema_version": ORIGIN_SCHEMA_VERSION,
            "venue_id": self.venue_id,
            "login_method": self.login_method,
            "jurisdiction": self.jurisdiction.value,
            "login_endpoint": self.login_endpoint,
            "issued_at": _instant(self.issued_at),
            "response_sha256": self.response_sha256,
        }
        return sha256(_canonical_json(payload)).hexdigest()

    @property
    def execution_authorized(self) -> bool:
        return False


@dataclass(frozen=True, slots=True, repr=False)
class BetfairLoginSession:
    """In-memory return value; credentials remain redacted by their own contract."""

    credentials: BetfairSessionCredentials
    origin: BetfairSessionOrigin

    def __post_init__(self) -> None:
        if type(self.credentials) is not BetfairSessionCredentials:
            raise BetfairSessionOriginError(
                "credentials must be exact BetfairSessionCredentials"
            )
        if type(self.origin) is not BetfairSessionOrigin:
            raise BetfairSessionOriginError("origin must be exact BetfairSessionOrigin")

    def __repr__(self) -> str:
        return (
            "BetfairLoginSession(credentials=<redacted>, "
            f"jurisdiction={self.origin.jurisdiction.value!r}, "
            f"origin_id={self.origin.origin_id!r})"
        )

    __str__ = __repr__


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairAuthenticatedJurisdiction:
    """Jurisdiction proof bound to one exact current K07 session context."""

    venue_id: str
    jurisdiction: BetfairLoginJurisdiction
    session_context_id: str
    account_identity_id: str
    session_origin_id: str

    def __post_init__(self) -> None:
        if self.venue_id != VENUE_ID:
            raise BetfairSessionOriginError("venue_id is product-owned")
        if type(self.jurisdiction) is not BetfairLoginJurisdiction:
            raise BetfairSessionOriginError(
                "jurisdiction must be exact BetfairLoginJurisdiction"
            )
        _text(self.session_context_id, "session_context_id")
        _sha256_hex(self.account_identity_id, "account_identity_id")
        _sha256_hex(self.session_origin_id, "session_origin_id")

    @property
    def authority_id(self) -> str:
        payload = {
            "schema": BOUND_SCHEMA,
            "schema_version": BOUND_SCHEMA_VERSION,
            "venue_id": self.venue_id,
            "jurisdiction": self.jurisdiction.value,
            "session_context_id": self.session_context_id,
            "account_identity_id": self.account_identity_id,
            "session_origin_id": self.session_origin_id,
        }
        return sha256(_canonical_json(payload)).hexdigest()

    @property
    def execution_authorized(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class _IssuedOriginRecord:
    value_ref: ReferenceType[BetfairSessionOrigin]
    origin_id: str
    credentials: BetfairSessionCredentials
    credential_binding: bytes


@dataclass(frozen=True, slots=True)
class _IssuedBoundRecord:
    value_ref: ReferenceType[BetfairAuthenticatedJurisdiction]
    authority_id: str
    origin_ref: ReferenceType[BetfairSessionOrigin]
    identity_ref: ReferenceType[BetfairAuthenticatedAccountIdentity]
    client_ref: ReferenceType[BetfairReadOnlyClient]


_LOCK = RLock()
_ISSUED_ORIGINS: dict[int, _IssuedOriginRecord] = {}
_ISSUED_BOUND: dict[int, _IssuedBoundRecord] = {}


class UrllibBetfairCertLoginTransport:
    """Minimal HTTPS certificate-login transport; no provider write APIs exist here."""

    def __init__(self, *, max_response_bytes: int = _MAX_RESPONSE_BYTES) -> None:
        if type(max_response_bytes) is not int or max_response_bytes < 1024:
            raise BetfairSessionOriginError(
                "max_response_bytes must be an integer >= 1024"
            )
        self._max_response_bytes = max_response_bytes

    def post_cert_login(
        self,
        endpoint: str,
        *,
        application_key: str,
        username: str,
        password: str,
        certificate_path: Path,
        private_key_path: Path | None,
        timeout_seconds: float,
    ) -> bytes:
        if endpoint not in _CERT_LOGIN_ENDPOINTS.values():
            raise BetfairSessionOriginError("certificate-login endpoint is not canonical")
        _positive_timeout(timeout_seconds)
        context = ssl.create_default_context()
        try:
            context.load_cert_chain(
                certfile=str(certificate_path),
                keyfile=(None if private_key_path is None else str(private_key_path)),
            )
        except (OSError, ssl.SSLError) as exc:
            raise BetfairSessionOriginError(
                "Betfair client certificate/key could not be loaded"
            ) from exc
        body = urlencode({"username": username, "password": password}).encode("utf-8")
        request = Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Application": application_key,
            },
        )
        opener = build_opener(HTTPSHandler(context=context), _NoRedirectHandler())
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                status = getattr(response, "status", None)
                if status != 200:
                    raise BetfairSessionOriginError(
                        f"Betfair login returned HTTP status {status!r}"
                    )
                final_url = getattr(response, "geturl", lambda: None)()
                if final_url != endpoint:
                    raise BetfairSessionOriginError(
                        "Betfair login response did not originate from exact endpoint"
                    )
                payload = response.read(self._max_response_bytes + 1)
        except HTTPError as exc:
            raise BetfairSessionOriginError(
                f"Betfair login returned HTTP status {exc.code}"
            ) from exc
        except (URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            raise BetfairSessionOriginError("Betfair login transport failed") from exc
        if len(payload) > self._max_response_bytes:
            raise BetfairSessionOriginError("Betfair login response exceeds safe limit")
        return payload


class _NoRedirectHandler(HTTPRedirectHandler):
    """Fail closed instead of following a provider authentication redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


_CANONICAL_TRANSPORT_POST = UrllibBetfairCertLoginTransport.post_cert_login
_CANONICAL_BUILD_OPENER = build_opener
_CANONICAL_HTTPS_HANDLER = HTTPSHandler
_CANONICAL_REDIRECT_HANDLER = HTTPRedirectHandler
_CANONICAL_SSL_CONTEXT_FACTORY = ssl.create_default_context
_CANONICAL_ACCOUNT_IDENTITY_REQUIRE = require_authoritative_betfair_account_identity
_CANONICAL_CREDENTIALS_TYPE = BetfairSessionCredentials
_CANONICAL_CLIENT_TYPE = BetfairReadOnlyClient
_CANONICAL_IDENTITY_TYPE = BetfairAuthenticatedAccountIdentity


def cert_login_endpoint(jurisdiction: BetfairLoginJurisdiction) -> str:
    if type(jurisdiction) is not BetfairLoginJurisdiction:
        raise BetfairSessionOriginError(
            "jurisdiction must be exact BetfairLoginJurisdiction"
        )
    return _CERT_LOGIN_ENDPOINTS[jurisdiction]


def parse_noninteractive_login_response(payload: bytes) -> BetfairNonInteractiveLoginResponse:
    if type(payload) is not bytes or not payload:
        raise BetfairSessionOriginError("login response must be non-empty bytes")
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise BetfairSessionOriginError("login response exceeds safe limit")
    document = _strict_json_object(payload)
    status = _login_status(document.get("loginStatus"))
    token = document.get("sessionToken")
    if status == "SUCCESS":
        token = _secret_text(token, "sessionToken")
    else:
        if token not in (None, ""):
            raise BetfairSessionOriginError(
                "failed login response unexpectedly carried sessionToken"
            )
        token = None
    return BetfairNonInteractiveLoginResponse(
        login_status=status,
        session_token=token,
        response_sha256=sha256(payload).hexdigest(),
    )


def login_betfair_noninteractive(
    secrets: BetfairNonInteractiveLoginSecrets,
    *,
    jurisdiction: BetfairLoginJurisdiction,
    timeout_seconds: float = 10.0,
) -> BetfairLoginSession:
    """Perform canonical certificate login and issue secret-free jurisdiction origin."""

    if type(secrets) is not BetfairNonInteractiveLoginSecrets:
        raise BetfairSessionOriginError(
            "secrets must be exact BetfairNonInteractiveLoginSecrets"
        )
    endpoint = cert_login_endpoint(jurisdiction)
    _positive_timeout(timeout_seconds)
    transport = UrllibBetfairCertLoginTransport()
    if not _canonical_network_transport(transport):
        raise BetfairSessionOriginError(
            "canonical Betfair certificate-login transport origin is unavailable"
        )
    payload = transport.post_cert_login(
        endpoint,
        application_key=secrets.application_key,
        username=secrets.username,
        password=secrets.password,
        certificate_path=secrets.certificate_path,
        private_key_path=secrets.private_key_path,
        timeout_seconds=timeout_seconds,
    )
    if not _canonical_network_transport(transport):
        raise BetfairSessionOriginError(
            "canonical Betfair certificate-login transport changed during login"
        )
    response = parse_noninteractive_login_response(payload)
    if response.login_status != "SUCCESS" or response.session_token is None:
        raise BetfairSessionOriginError(
            f"Betfair login was not successful: {response.login_status}"
        )
    if BetfairSessionCredentials is not _CANONICAL_CREDENTIALS_TYPE:
        raise BetfairSessionOriginError("Betfair credentials type binding changed")
    credentials = BetfairSessionCredentials(
        application_key=secrets.application_key,
        session_token=response.session_token,
    )
    origin = BetfairSessionOrigin(
        venue_id=VENUE_ID,
        login_method=LOGIN_METHOD,
        jurisdiction=jurisdiction,
        login_endpoint=endpoint,
        issued_at=datetime.now(timezone.utc),
        response_sha256=response.response_sha256,
    )
    _remember_origin(origin, credentials)
    return BetfairLoginSession(credentials=credentials, origin=origin)


def is_authoritative_betfair_session_origin(
    value: object,
    *,
    credentials: BetfairSessionCredentials | None = None,
) -> bool:
    if type(value) is not BetfairSessionOrigin:
        return False
    if BetfairSessionCredentials is not _CANONICAL_CREDENTIALS_TYPE:
        return False
    with _LOCK:
        record = _ISSUED_ORIGINS.get(id(value))
        if record is None or record.value_ref() is not value:
            return False
        if not hmac.compare_digest(record.origin_id, value.origin_id):
            return False
        if credentials is not None and record.credentials is not credentials:
            return False
        try:
            current = _credential_binding(record.credentials)
        except (AttributeError, TypeError, BetfairSessionOriginError):
            return False
        return hmac.compare_digest(record.credential_binding, current)


def require_authoritative_betfair_session_origin(
    value: object,
    *,
    credentials: BetfairSessionCredentials | None = None,
) -> BetfairSessionOrigin:
    if not is_authoritative_betfair_session_origin(value, credentials=credentials):
        raise BetfairSessionOriginError(
            "Betfair session origin lacks current canonical login authority"
        )
    assert type(value) is BetfairSessionOrigin
    return value


def bind_betfair_authenticated_jurisdiction(
    origin: BetfairSessionOrigin,
    identity: BetfairAuthenticatedAccountIdentity,
    *,
    client: BetfairReadOnlyClient,
) -> BetfairAuthenticatedJurisdiction:
    """Bind product-owned login origin to the exact current K07 session context."""

    if (
        BetfairReadOnlyClient is not _CANONICAL_CLIENT_TYPE
        or BetfairAuthenticatedAccountIdentity is not _CANONICAL_IDENTITY_TYPE
        or require_authoritative_betfair_account_identity
        is not _CANONICAL_ACCOUNT_IDENTITY_REQUIRE
    ):
        raise BetfairSessionOriginError(
            "canonical K07 identity verifier binding changed"
        )
    if type(client) is not BetfairReadOnlyClient:
        raise BetfairSessionOriginError("client must be exact BetfairReadOnlyClient")
    if type(identity) is not BetfairAuthenticatedAccountIdentity:
        raise BetfairSessionOriginError(
            "identity must be exact BetfairAuthenticatedAccountIdentity"
        )
    credentials = getattr(client, "_credentials", None)
    if type(credentials) is not BetfairSessionCredentials:
        raise BetfairSessionOriginError(
            "authenticated client credentials are not canonical"
        )
    require_authoritative_betfair_session_origin(origin, credentials=credentials)
    try:
        require_authoritative_betfair_account_identity(identity, client=client)
    except Exception as exc:
        raise BetfairSessionOriginError(
            "K07 authenticated session-context identity is not authoritative"
        ) from exc
    value = BetfairAuthenticatedJurisdiction(
        venue_id=VENUE_ID,
        jurisdiction=origin.jurisdiction,
        session_context_id=identity.session_context_id,
        account_identity_id=identity.identity_id,
        session_origin_id=origin.origin_id,
    )
    _remember_bound(value, origin, identity, client)
    return value


def is_authoritative_betfair_authenticated_jurisdiction(
    value: object,
    *,
    client: BetfairReadOnlyClient | None = None,
) -> bool:
    if type(value) is not BetfairAuthenticatedJurisdiction:
        return False
    if (
        BetfairReadOnlyClient is not _CANONICAL_CLIENT_TYPE
        or BetfairAuthenticatedAccountIdentity is not _CANONICAL_IDENTITY_TYPE
        or require_authoritative_betfair_account_identity
        is not _CANONICAL_ACCOUNT_IDENTITY_REQUIRE
    ):
        return False
    with _LOCK:
        record = _ISSUED_BOUND.get(id(value))
        if record is None or record.value_ref() is not value:
            return False
        if not hmac.compare_digest(record.authority_id, value.authority_id):
            return False
        origin = record.origin_ref()
        identity = record.identity_ref()
        issued_client = record.client_ref()
        if origin is None or identity is None or issued_client is None:
            return False
        if client is not None and issued_client is not client:
            return False
        credentials = getattr(issued_client, "_credentials", None)
        if type(credentials) is not BetfairSessionCredentials:
            return False
        if not is_authoritative_betfair_session_origin(
            origin, credentials=credentials
        ):
            return False
        try:
            require_authoritative_betfair_account_identity(
                identity, client=issued_client
            )
        except Exception:
            return False
        return (
            value.session_context_id == identity.session_context_id
            and value.account_identity_id == identity.identity_id
            and value.session_origin_id == origin.origin_id
            and value.jurisdiction is origin.jurisdiction
        )


def require_authoritative_betfair_authenticated_jurisdiction(
    value: object,
    *,
    client: BetfairReadOnlyClient | None = None,
) -> BetfairAuthenticatedJurisdiction:
    if not is_authoritative_betfair_authenticated_jurisdiction(value, client=client):
        raise BetfairSessionOriginError(
            "Betfair jurisdiction lacks current authenticated-session authority"
        )
    assert type(value) is BetfairAuthenticatedJurisdiction
    return value


def _canonical_network_transport(transport: object) -> bool:
    if type(transport) is not UrllibBetfairCertLoginTransport:
        return False
    if type(transport).post_cert_login is not _CANONICAL_TRANSPORT_POST:
        return False
    if build_opener is not _CANONICAL_BUILD_OPENER:
        return False
    if HTTPSHandler is not _CANONICAL_HTTPS_HANDLER:
        return False
    if HTTPRedirectHandler is not _CANONICAL_REDIRECT_HANDLER:
        return False
    if ssl.create_default_context is not _CANONICAL_SSL_CONTEXT_FACTORY:
        return False
    state = getattr(transport, "__dict__", None)
    return (
        type(state) is dict
        and set(state) == {"_max_response_bytes"}
        and type(state["_max_response_bytes"]) is int
        and state["_max_response_bytes"] >= 1024
    )


def _credential_binding(credentials: BetfairSessionCredentials) -> bytes:
    if type(credentials) is not BetfairSessionCredentials:
        raise BetfairSessionOriginError(
            "credential binding requires canonical BetfairSessionCredentials"
        )
    try:
        material = (
            credentials.application_key.encode("utf-8")
            + b"\x00"
            + credentials.session_token.encode("utf-8")
        )
    except (AttributeError, UnicodeEncodeError) as exc:
        raise BetfairSessionOriginError("Betfair credentials are malformed") from exc
    return hmac.digest(_PROCESS_HMAC_KEY, material, "sha256")


def _remember_origin(
    value: BetfairSessionOrigin,
    credentials: BetfairSessionCredentials,
) -> None:
    key = id(value)

    def discard(dead_ref: ReferenceType[BetfairSessionOrigin]) -> None:
        with _LOCK:
            record = _ISSUED_ORIGINS.get(key)
            if record is not None and record.value_ref is dead_ref:
                _ISSUED_ORIGINS.pop(key, None)

    with _LOCK:
        _ISSUED_ORIGINS[key] = _IssuedOriginRecord(
            value_ref=ref(value, discard),
            origin_id=value.origin_id,
            credentials=credentials,
            credential_binding=_credential_binding(credentials),
        )


def _remember_bound(
    value: BetfairAuthenticatedJurisdiction,
    origin: BetfairSessionOrigin,
    identity: BetfairAuthenticatedAccountIdentity,
    client: BetfairReadOnlyClient,
) -> None:
    key = id(value)

    def discard(dead_ref: ReferenceType[BetfairAuthenticatedJurisdiction]) -> None:
        with _LOCK:
            record = _ISSUED_BOUND.get(key)
            if record is not None and record.value_ref is dead_ref:
                _ISSUED_BOUND.pop(key, None)

    with _LOCK:
        _ISSUED_BOUND[key] = _IssuedBoundRecord(
            value_ref=ref(value, discard),
            authority_id=value.authority_id,
            origin_ref=ref(origin),
            identity_ref=ref(identity),
            client_ref=ref(client),
        )


def _strict_json_object(payload: bytes) -> dict[str, object]:
    try:
        raw = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BetfairSessionOriginError("login response is not UTF-8 JSON") from exc

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise BetfairSessionOriginError(
                    f"duplicate login-response JSON key: {key}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise BetfairSessionOriginError(
            f"non-standard login-response JSON constant: {value}"
        )

    try:
        parsed = json.loads(
            raw,
            object_pairs_hook=pairs,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise BetfairSessionOriginError("login response is not valid JSON") from exc
    if type(parsed) is not dict:
        raise BetfairSessionOriginError("login response must be a JSON object")
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
        raise BetfairSessionOriginError("session-origin identity is not canonical JSON") from exc


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairSessionOriginError(f"{field} must be canonical non-empty text")
    return value


def _login_status(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or not value.isascii()
        or any(ch not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for ch in value)
    ):
        raise BetfairSessionOriginError(
            "loginStatus must be a bounded uppercase provider status code"
        )
    return value


def _secret_text(value: object, field: str) -> str:
    if type(value) is not str or not value or "\x00" in value:
        raise BetfairSessionOriginError(f"{field} must be non-empty secret text")
    return value


def _sha256_hex(value: object, field: str) -> str:
    raw = _text(value, field)
    if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
        raise BetfairSessionOriginError(f"{field} must be lowercase SHA-256 hex")
    return raw


def _utc(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise BetfairSessionOriginError(f"{field} must be timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _instant(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _positive_timeout(value: object) -> float:
    if type(value) not in {int, float} or isinstance(value, bool):
        raise BetfairSessionOriginError("timeout_seconds must be a positive number")
    numeric = float(value)
    if numeric <= 0 or numeric == float("inf") or numeric != numeric:
        raise BetfairSessionOriginError("timeout_seconds must be a finite positive number")
    return numeric

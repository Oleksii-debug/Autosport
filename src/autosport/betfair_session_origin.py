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
from types import MappingProxyType
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


class BetfairSessionOriginError(RuntimeError):
    """Raised when login-origin or jurisdiction authority cannot be proven safely."""


class BetfairLoginJurisdiction(str, Enum):
    GLOBAL_COM = "GLOBAL_COM"
    AUSTRALIA_NEW_ZEALAND = "AUSTRALIA_NEW_ZEALAND"
    ITALY = "ITALY"
    SPAIN = "SPAIN"
    ROMANIA = "ROMANIA"


_CERT_LOGIN_ENDPOINTS: Mapping[BetfairLoginJurisdiction, str] = MappingProxyType(
    {
        BetfairLoginJurisdiction.GLOBAL_COM: "https://identitysso-cert.betfair.com/api/certlogin",
        BetfairLoginJurisdiction.AUSTRALIA_NEW_ZEALAND: "https://identitysso-cert.betfair.com.au/api/certlogin",
        BetfairLoginJurisdiction.ITALY: "https://identitysso-cert.betfair.it/api/certlogin",
        BetfairLoginJurisdiction.SPAIN: "https://identitysso-cert.betfair.es/api/certlogin",
        BetfairLoginJurisdiction.ROMANIA: "https://identitysso-cert.betfair.ro/api/certlogin",
    }
)


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


def _build_origin_authority_runtime():
    """Build the canonical issuer/verifier with a hidden executable trust root."""

    lock = RLock()
    issued: dict[int, _IssuedOriginRecord] = {}
    hmac_key = token_bytes(32)

    error_type = BetfairSessionOriginError
    secrets_type = BetfairNonInteractiveLoginSecrets
    jurisdiction_type = BetfairLoginJurisdiction
    origin_type = BetfairSessionOrigin
    login_session_type = BetfairLoginSession
    credentials_type = BetfairSessionCredentials
    transport_type = UrllibBetfairCertLoginTransport
    endpoint_table = _CERT_LOGIN_ENDPOINTS
    venue_id = VENUE_ID
    login_method = LOGIN_METHOD
    max_response_bytes = _MAX_RESPONSE_BYTES

    transport_init = transport_type.__init__
    transport_init_code = getattr(transport_init, "__code__", None)
    transport_post = transport_type.post_cert_login
    transport_post_code = getattr(transport_post, "__code__", None)
    no_redirect_type = _NoRedirectHandler
    no_redirect_request = no_redirect_type.redirect_request
    no_redirect_request_code = getattr(no_redirect_request, "__code__", None)
    build_opener_fn = build_opener
    build_opener_code = getattr(build_opener_fn, "__code__", None)
    https_handler_type = HTTPSHandler
    redirect_handler_type = HTTPRedirectHandler
    ssl_module = ssl
    ssl_context_factory = ssl.create_default_context
    ssl_context_factory_code = getattr(ssl_context_factory, "__code__", None)
    request_type = Request
    urlencode_fn = urlencode
    urlencode_code = getattr(urlencode_fn, "__code__", None)

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

    origin_init = origin_type.__init__
    origin_init_code = getattr(origin_init, "__code__", None)
    origin_post_init = origin_type.__post_init__
    origin_post_init_code = getattr(origin_post_init, "__code__", None)
    weakref_ref = ref
    sha256_fn = sha256
    hmac_digest = hmac.digest
    hmac_compare_digest = hmac.compare_digest
    datetime_type = datetime
    utc = timezone.utc

    def json_executable_graph_matches() -> bool:
        return bool(
            json is json_module
            and getattr(json_module, "loads", None) is json_loads
            and getattr(json_loads, "__code__", None) is json_loads_code
            and getattr(json_module, "JSONDecodeError", None)
            is json_decode_error
            and getattr(json_module, "JSONDecoder", None) is json_decoder
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
        return bool(
            BetfairSessionOriginError is error_type
            and BetfairNonInteractiveLoginSecrets is secrets_type
            and BetfairLoginJurisdiction is jurisdiction_type
            and BetfairSessionOrigin is origin_type
            and BetfairLoginSession is login_session_type
            and BetfairSessionCredentials is credentials_type
            and UrllibBetfairCertLoginTransport is transport_type
            and _CERT_LOGIN_ENDPOINTS is endpoint_table
            and VENUE_ID == venue_id
            and LOGIN_METHOD == login_method
            and _MAX_RESPONSE_BYTES == max_response_bytes
            and transport_type.__init__ is transport_init
            and getattr(transport_init, "__code__", None)
            is transport_init_code
            and transport_type.post_cert_login is transport_post
            and getattr(transport_post, "__code__", None)
            is transport_post_code
            and _NoRedirectHandler is no_redirect_type
            and no_redirect_type.redirect_request is no_redirect_request
            and getattr(no_redirect_request, "__code__", None)
            is no_redirect_request_code
            and build_opener is build_opener_fn
            and getattr(build_opener_fn, "__code__", None)
            is build_opener_code
            and HTTPSHandler is https_handler_type
            and HTTPRedirectHandler is redirect_handler_type
            and ssl is ssl_module
            and ssl_module.create_default_context is ssl_context_factory
            and getattr(ssl_context_factory, "__code__", None)
            is ssl_context_factory_code
            and Request is request_type
            and urlencode is urlencode_fn
            and getattr(urlencode_fn, "__code__", None) is urlencode_code
            and origin_type.__init__ is origin_init
            and getattr(origin_init, "__code__", None) is origin_init_code
            and origin_type.__post_init__ is origin_post_init
            and getattr(origin_post_init, "__code__", None)
            is origin_post_init_code
            and json_executable_graph_matches()
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
        if type(jurisdiction) is not jurisdiction_type:
            raise error_type(
                "jurisdiction must be exact BetfairLoginJurisdiction"
            )
        try:
            return endpoint_table[jurisdiction]
        except KeyError as exc:
            raise error_type(
                "jurisdiction has no canonical certificate-login endpoint"
            ) from exc

    def canonical_network_transport(transport: object) -> bool:
        if not implementation_is_current():
            return False
        state = getattr(transport, "__dict__", None)
        return bool(
            type(transport) is transport_type
            and type(transport).post_cert_login is transport_post
            and type(state) is dict
            and set(state) == {"_max_response_bytes"}
            and state["_max_response_bytes"] == max_response_bytes
        )

    def parse_success_payload(payload: bytes) -> tuple[str, str]:
        if type(payload) is not bytes or not payload:
            raise error_type("login response must be non-empty bytes")
        if len(payload) > max_response_bytes:
            raise error_type("login response exceeds safe limit")
        try:
            raw = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise error_type("login response is not UTF-8 JSON") from exc

        def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise error_type(
                        f"duplicate login-response JSON key: {key}"
                    )
                result[key] = value
            return result

        def reject_constant(value: str) -> None:
            raise error_type(
                f"non-standard login-response JSON constant: {value}"
            )

        if not json_executable_graph_matches():
            raise error_type(
                "canonical Betfair login JSON authority changed"
            )
        try:
            document = json_loads(
                raw,
                cls=json_decoder,
                object_pairs_hook=pairs,
                parse_constant=reject_constant,
            )
        except json_decode_error as exc:
            raise error_type("login response is not valid JSON") from exc
        if not json_executable_graph_matches():
            raise error_type(
                "canonical Betfair login JSON authority changed"
            )
        if (
            type(document) is not dict
            or frozenset(document) != {"sessionToken", "loginStatus"}
        ):
            raise error_type(
                "successful login response schema does not match provider contract"
            )
        status = document["loginStatus"]
        token = document["sessionToken"]
        if status != "SUCCESS":
            raise error_type("Betfair login was not successful")
        if type(token) is not str or not token or "\x00" in token:
            raise error_type("successful login response lacks canonical sessionToken")
        return token, sha256_fn(payload).hexdigest()

    def origin_fingerprint(value: BetfairSessionOrigin) -> str:
        if type(value) is not origin_type:
            raise error_type("session origin type changed")
        issued_at = value.issued_at
        if (
            type(issued_at) is not datetime_type
            or issued_at.tzinfo is None
            or issued_at.utcoffset() is None
        ):
            raise error_type("session origin issued_at is not timezone-aware")
        if (
            value.venue_id != venue_id
            or value.login_method != login_method
            or type(value.jurisdiction) is not jurisdiction_type
            or value.login_endpoint != endpoint_for(value.jurisdiction)
            or type(value.response_sha256) is not str
            or len(value.response_sha256) != 64
            or any(
                ch not in "0123456789abcdef"
                for ch in value.response_sha256
            )
        ):
            raise error_type("session origin fields are not canonical")
        material = (
            value.venue_id,
            value.login_method,
            value.jurisdiction.value,
            value.login_endpoint,
            issued_at.astimezone(utc).isoformat(),
            value.response_sha256,
        )
        return sha256_fn(repr(material).encode("utf-8")).hexdigest()

    def remember(
        value: BetfairSessionOrigin,
        credentials: BetfairSessionCredentials,
    ) -> None:
        key = id(value)

        def discard(dead_ref: ReferenceType[BetfairSessionOrigin]) -> None:
            with lock:
                record = issued.get(key)
                if record is not None and record.value_ref is dead_ref:
                    issued.pop(key, None)

        with lock:
            issued[key] = _IssuedOriginRecord(
                value_ref=weakref_ref(value, discard),
                origin_id=origin_fingerprint(value),
                credentials=credentials,
                credential_binding=credential_binding(credentials),
            )

    def login(
        secrets: BetfairNonInteractiveLoginSecrets,
        *,
        jurisdiction: BetfairLoginJurisdiction,
        timeout_seconds: float = 10.0,
    ) -> BetfairLoginSession:
        if not implementation_is_current():
            raise error_type(
                "canonical Betfair login authority binding changed"
            )
        if type(secrets) is not secrets_type:
            raise error_type(
                "secrets must be exact BetfairNonInteractiveLoginSecrets"
            )
        endpoint = endpoint_for(jurisdiction)
        _positive_timeout(timeout_seconds)
        transport = transport_type()
        if not canonical_network_transport(transport):
            raise error_type(
                "canonical Betfair certificate-login transport origin is unavailable"
            )
        payload = transport_post(
            transport,
            endpoint,
            application_key=secrets.application_key,
            username=secrets.username,
            password=secrets.password,
            certificate_path=secrets.certificate_path,
            private_key_path=secrets.private_key_path,
            timeout_seconds=timeout_seconds,
        )
        if not canonical_network_transport(transport):
            raise error_type(
                "canonical Betfair certificate-login transport changed during login"
            )
        session_token, response_sha256 = parse_success_payload(payload)
        if not implementation_is_current():
            raise error_type(
                "canonical Betfair login authority changed during login"
            )
        credentials = credentials_type(
            application_key=secrets.application_key,
            session_token=session_token,
        )
        origin = origin_type(
            venue_id=venue_id,
            login_method=login_method,
            jurisdiction=jurisdiction,
            login_endpoint=endpoint,
            issued_at=datetime_type.now(utc),
            response_sha256=response_sha256,
        )
        if (
            not implementation_is_current()
            or origin.venue_id != venue_id
            or origin.login_method != login_method
            or origin.jurisdiction is not jurisdiction
            or origin.login_endpoint != endpoint
        ):
            raise error_type(
                "canonical Betfair login observation construction changed"
            )
        remember(origin, credentials)
        return login_session_type(credentials=credentials, origin=origin)

    def is_authoritative(
        value: object,
        *,
        credentials: BetfairSessionCredentials | None = None,
    ) -> bool:
        if type(value) is not origin_type or not implementation_is_current():
            return False
        with lock:
            record = issued.get(id(value))
            if record is None or record.value_ref() is not value:
                return False
            try:
                if not hmac_compare_digest(
                    record.origin_id,
                    origin_fingerprint(value),
                ):
                    return False
            except Exception:
                return False
            if credentials is not None and record.credentials is not credentials:
                return False
            try:
                current = credential_binding(record.credentials)
            except Exception:
                return False
            return hmac_compare_digest(record.credential_binding, current)

    def require(
        value: object,
        *,
        credentials: BetfairSessionCredentials | None = None,
    ) -> BetfairSessionOrigin:
        if not is_authoritative(value, credentials=credentials):
            raise error_type(
                "Betfair session origin lacks current canonical login authority"
            )
        assert type(value) is origin_type
        return value

    return login, is_authoritative, require

def _build_bound_authority_runtime(require_origin):
    """Build K07 binding issuer/verifier with hidden canonical dependencies."""

    lock = RLock()
    issued: dict[int, _IssuedBoundRecord] = {}

    error_type = BetfairSessionOriginError
    client_type = BetfairReadOnlyClient
    credentials_type = BetfairSessionCredentials
    identity_type = BetfairAuthenticatedAccountIdentity
    jurisdiction_type = BetfairLoginJurisdiction
    origin_type = BetfairSessionOrigin
    bound_type = BetfairAuthenticatedJurisdiction
    identity_require = require_authoritative_betfair_account_identity
    venue_id = VENUE_ID
    login_method = LOGIN_METHOD
    origin_schema = ORIGIN_SCHEMA
    origin_schema_version = ORIGIN_SCHEMA_VERSION
    endpoint_table = _CERT_LOGIN_ENDPOINTS
    hmac_compare_digest = hmac.compare_digest
    sha256_fn = sha256
    weakref_ref = ref
    datetime_type = datetime
    utc = timezone.utc

    bound_init = bound_type.__init__
    bound_init_code = getattr(bound_init, "__code__", None)
    bound_post_init = bound_type.__post_init__
    bound_post_init_code = getattr(bound_post_init, "__code__", None)

    json_module = json
    json_dumps = json.dumps
    json_dumps_code = getattr(json_dumps, "__code__", None)
    json_encoder = json.JSONEncoder
    json_encoder_init = json_encoder.__init__
    json_encoder_init_code = getattr(json_encoder_init, "__code__", None)
    json_encoder_encode = json_encoder.encode
    json_encoder_encode_code = getattr(json_encoder_encode, "__code__", None)
    json_encoder_iterencode = json_encoder.iterencode
    json_encoder_iterencode_code = getattr(
        json_encoder_iterencode, "__code__", None
    )
    json_encoder_default = json_encoder.default
    json_encoder_default_code = getattr(json_encoder_default, "__code__", None)

    def json_executable_graph_matches() -> bool:
        return bool(
            json is json_module
            and getattr(json_module, "dumps", None) is json_dumps
            and getattr(json_dumps, "__code__", None) is json_dumps_code
            and getattr(json_module, "JSONEncoder", None) is json_encoder
            and getattr(json_encoder, "__init__", None) is json_encoder_init
            and getattr(json_encoder_init, "__code__", None)
            is json_encoder_init_code
            and getattr(json_encoder, "encode", None) is json_encoder_encode
            and getattr(json_encoder_encode, "__code__", None)
            is json_encoder_encode_code
            and getattr(json_encoder, "iterencode", None)
            is json_encoder_iterencode
            and getattr(json_encoder_iterencode, "__code__", None)
            is json_encoder_iterencode_code
            and getattr(json_encoder, "default", None)
            is json_encoder_default
            and getattr(json_encoder_default, "__code__", None)
            is json_encoder_default_code
        )

    def implementation_is_current() -> bool:
        return bool(
            BetfairSessionOriginError is error_type
            and BetfairReadOnlyClient is client_type
            and BetfairSessionCredentials is credentials_type
            and BetfairAuthenticatedAccountIdentity is identity_type
            and BetfairLoginJurisdiction is jurisdiction_type
            and BetfairSessionOrigin is origin_type
            and BetfairAuthenticatedJurisdiction is bound_type
            and require_authoritative_betfair_account_identity
            is identity_require
            and VENUE_ID == venue_id
            and LOGIN_METHOD == login_method
            and ORIGIN_SCHEMA == origin_schema
            and ORIGIN_SCHEMA_VERSION == origin_schema_version
            and _CERT_LOGIN_ENDPOINTS is endpoint_table
            and bound_type.__init__ is bound_init
            and getattr(bound_init, "__code__", None) is bound_init_code
            and bound_type.__post_init__ is bound_post_init
            and getattr(bound_post_init, "__code__", None)
            is bound_post_init_code
            and json_executable_graph_matches()
        )

    def canonical_origin_id(origin: BetfairSessionOrigin) -> str:
        if type(origin) is not origin_type:
            raise error_type("session origin type changed")
        issued_at = origin.issued_at
        if (
            type(issued_at) is not datetime_type
            or issued_at.tzinfo is None
            or issued_at.utcoffset() is None
            or origin.venue_id != venue_id
            or origin.login_method != login_method
            or type(origin.jurisdiction) is not jurisdiction_type
            or endpoint_table.get(origin.jurisdiction) != origin.login_endpoint
            or type(origin.response_sha256) is not str
            or len(origin.response_sha256) != 64
            or any(
                ch not in "0123456789abcdef"
                for ch in origin.response_sha256
            )
        ):
            raise error_type("session origin fields are not canonical")
        if not json_executable_graph_matches():
            raise error_type("canonical session-origin JSON authority changed")
        payload = {
            "schema": origin_schema,
            "schema_version": origin_schema_version,
            "venue_id": origin.venue_id,
            "login_method": origin.login_method,
            "jurisdiction": origin.jurisdiction.value,
            "login_endpoint": origin.login_endpoint,
            "issued_at": issued_at.astimezone(utc).isoformat().replace(
                "+00:00", "Z"
            ),
            "response_sha256": origin.response_sha256,
        }
        encoded = json_dumps(
            payload,
            cls=json_encoder,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        if not json_executable_graph_matches():
            raise error_type("canonical session-origin JSON authority changed")
        return sha256_fn(encoded).hexdigest()

    def bound_fingerprint(value: BetfairAuthenticatedJurisdiction) -> str:
        if type(value) is not bound_type:
            raise error_type("authenticated jurisdiction type changed")
        if (
            value.venue_id != venue_id
            or type(value.jurisdiction) is not jurisdiction_type
            or type(value.session_context_id) is not str
            or not value.session_context_id
            or type(value.account_identity_id) is not str
            or type(value.session_origin_id) is not str
        ):
            raise error_type("authenticated jurisdiction fields are not canonical")
        material = (
            value.venue_id,
            value.jurisdiction.value,
            value.session_context_id,
            value.account_identity_id,
            value.session_origin_id,
        )
        return sha256_fn(repr(material).encode("utf-8")).hexdigest()

    def remember(
        value: BetfairAuthenticatedJurisdiction,
        origin: BetfairSessionOrigin,
        identity: BetfairAuthenticatedAccountIdentity,
        client: BetfairReadOnlyClient,
    ) -> None:
        key = id(value)

        def discard(dead_ref: ReferenceType[BetfairAuthenticatedJurisdiction]) -> None:
            with lock:
                record = issued.get(key)
                if record is not None and record.value_ref is dead_ref:
                    issued.pop(key, None)

        with lock:
            issued[key] = _IssuedBoundRecord(
                value_ref=weakref_ref(value, discard),
                authority_id=bound_fingerprint(value),
                origin_ref=weakref_ref(origin),
                identity_ref=weakref_ref(identity),
                client_ref=weakref_ref(client),
            )

    def bind(
        origin: BetfairSessionOrigin,
        identity: BetfairAuthenticatedAccountIdentity,
        *,
        client: BetfairReadOnlyClient,
    ) -> BetfairAuthenticatedJurisdiction:
        if not implementation_is_current():
            raise error_type(
                "canonical K07 identity verifier binding changed"
            )
        if type(client) is not client_type:
            raise error_type("client must be exact BetfairReadOnlyClient")
        if type(identity) is not identity_type:
            raise error_type(
                "identity must be exact BetfairAuthenticatedAccountIdentity"
            )
        credentials = getattr(client, "_credentials", None)
        if type(credentials) is not credentials_type:
            raise error_type(
                "authenticated client credentials are not canonical"
            )
        require_origin(origin, credentials=credentials)
        try:
            identity_require(identity, client=client)
        except Exception as exc:
            raise error_type(
                "K07 authenticated session-context identity is not authoritative"
            ) from exc
        if not implementation_is_current():
            raise error_type(
                "canonical K07 identity verifier changed during binding"
            )
        origin_id = canonical_origin_id(origin)
        value = bound_type(
            venue_id=venue_id,
            jurisdiction=origin.jurisdiction,
            session_context_id=identity.session_context_id,
            account_identity_id=identity.identity_id,
            session_origin_id=origin_id,
        )
        if (
            not implementation_is_current()
            or value.venue_id != venue_id
            or value.jurisdiction is not origin.jurisdiction
            or value.session_context_id != identity.session_context_id
            or value.account_identity_id != identity.identity_id
            or value.session_origin_id != origin_id
        ):
            raise error_type(
                "authenticated jurisdiction construction changed"
            )
        remember(value, origin, identity, client)
        return value

    def is_authoritative(
        value: object,
        *,
        client: BetfairReadOnlyClient | None = None,
    ) -> bool:
        if type(value) is not bound_type or not implementation_is_current():
            return False
        with lock:
            record = issued.get(id(value))
            if record is None or record.value_ref() is not value:
                return False
            try:
                if not hmac_compare_digest(
                    record.authority_id,
                    bound_fingerprint(value),
                ):
                    return False
            except Exception:
                return False
            origin = record.origin_ref()
            identity = record.identity_ref()
            issued_client = record.client_ref()
            if origin is None or identity is None or issued_client is None:
                return False
            if client is not None and issued_client is not client:
                return False
            credentials = getattr(issued_client, "_credentials", None)
            if type(credentials) is not credentials_type:
                return False
            try:
                require_origin(origin, credentials=credentials)
                identity_require(identity, client=issued_client)
                origin_id = canonical_origin_id(origin)
            except Exception:
                return False
            return (
                value.session_context_id == identity.session_context_id
                and value.account_identity_id == identity.identity_id
                and value.session_origin_id == origin_id
                and value.jurisdiction is origin.jurisdiction
            )

    def require(
        value: object,
        *,
        client: BetfairReadOnlyClient | None = None,
    ) -> BetfairAuthenticatedJurisdiction:
        if not is_authoritative(value, client=client):
            raise error_type(
                "Betfair jurisdiction lacks current authenticated-session authority"
            )
        assert type(value) is bound_type
        return value

    return bind, is_authoritative, require

(
    login_betfair_noninteractive,
    is_authoritative_betfair_session_origin,
    require_authoritative_betfair_session_origin,
) = _build_origin_authority_runtime()
(
    bind_betfair_authenticated_jurisdiction,
    is_authoritative_betfair_authenticated_jurisdiction,
    require_authoritative_betfair_authenticated_jurisdiction,
) = _build_bound_authority_runtime(require_authoritative_betfair_session_origin)
# Keep only the canonical closures reachable; do not expose a caller-invocable issuer/registry.
del _build_origin_authority_runtime
del _build_bound_authority_runtime


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

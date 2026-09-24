"""Strict sandbox-only ProphetX wallet evidence adapter.

This module intentionally implements only the documented read-only Trading API balance
surface. It never authenticates with access/secret keys, places/cancels orders, infers a
production host, or turns ProphetX exposure-credit / locked-funds fields into spendable cash.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from http.client import HTTPException
import json
import math
import ssl
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import (
    AbstractHTTPHandler,
    HTTPErrorProcessor,
    HTTPRedirectHandler,
    HTTPSHandler,
    OpenerDirector,
    ProxyHandler,
    Request,
    build_opener,
)
from weakref import WeakKeyDictionary

from .bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)


SANDBOX_BASE_URL = "https://api.sandbox.prophetx.dev/partner"
BALANCE_URL = f"{SANDBOX_BASE_URL}/v4/mm/get_balance"
ADAPTER_ID = "prophetx-trading-api-readonly-sandbox"
ADAPTER_VERSION = "1"
PROVIDER_CURRENCY = "USD"
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ProphetXReadOnlyError(RuntimeError):
    """Raised when ProphetX read-only evidence cannot be accepted safely."""


@dataclass(frozen=True, slots=True, repr=False)
class ProphetXSessionToken:
    access_token: str

    def __post_init__(self) -> None:
        _session_token(self.access_token)

    def __repr__(self) -> str:
        return "ProphetXSessionToken(access_token=<redacted>)"


@dataclass(frozen=True, slots=True)
class ProphetXHttpResponse:
    status: int
    final_url: str
    content_type: str | None
    content_encoding: str | None
    body: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.status, int) or isinstance(self.status, bool):
            raise TypeError("status must be an integer")
        _required_text(self.final_url, "final_url")
        if self.content_type is not None and not isinstance(self.content_type, str):
            raise TypeError("content_type must be str or None")
        if self.content_encoding is not None and not isinstance(self.content_encoding, str):
            raise TypeError("content_encoding must be str or None")
        if not isinstance(self.body, bytes):
            raise TypeError("body must be bytes")


class ProphetXHttpTransport(Protocol):
    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProphetXHttpResponse: ...


class _RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise ProphetXReadOnlyError("ProphetX HTTP redirect refused")


def _build_wallet_stdlib_dispatch_authority():
    """Freeze network dispatch before callers can construct canonical wallet clients."""

    opener_type = OpenerDirector
    redirect_handler_type = _RejectRedirectHandler
    https_handler_type = HTTPSHandler
    abstract_http_handler_type = AbstractHTTPHandler
    http_error_processor_type = HTTPErrorProcessor

    canonical_opener_open = opener_type.open
    canonical_opener_internal_open = opener_type._open
    canonical_opener_call_chain = opener_type._call_chain
    canonical_opener_error = opener_type.error
    canonical_redirect_request = redirect_handler_type.redirect_request
    canonical_https_open = https_handler_type.https_open
    canonical_https_request = https_handler_type.https_request
    canonical_do_open = abstract_http_handler_type.do_open
    canonical_https_response = http_error_processor_type.https_response

    canonical_codes = (
        canonical_opener_open.__code__,
        canonical_opener_internal_open.__code__,
        canonical_opener_call_chain.__code__,
        canonical_opener_error.__code__,
        canonical_redirect_request.__code__,
        canonical_https_open.__code__,
        canonical_https_request.__code__,
        canonical_do_open.__code__,
        canonical_https_response.__code__,
    )

    def is_current() -> bool:
        live = (
            opener_type.open,
            opener_type._open,
            opener_type._call_chain,
            opener_type.error,
            redirect_handler_type.redirect_request,
            https_handler_type.https_open,
            https_handler_type.https_request,
            abstract_http_handler_type.do_open,
            http_error_processor_type.https_response,
        )
        return all(
            current is canonical
            and getattr(current, "__code__", None) is canonical_code
            for current, canonical, canonical_code in zip(
                live,
                (
                    canonical_opener_open,
                    canonical_opener_internal_open,
                    canonical_opener_call_chain,
                    canonical_opener_error,
                    canonical_redirect_request,
                    canonical_https_open,
                    canonical_https_request,
                    canonical_do_open,
                    canonical_https_response,
                ),
                canonical_codes,
            )
        )

    return is_current, canonical_opener_open


(
    _wallet_stdlib_dispatch_is_canonical,
    _canonical_wallet_opener_open,
) = _build_wallet_stdlib_dispatch_authority()
del _build_wallet_stdlib_dispatch_authority


def _build_provider_fetch_factory():
    """Freeze provider-fetch construction authority at module import."""

    stdlib_dispatch_is_canonical = _wallet_stdlib_dispatch_is_canonical
    canonical_stdlib_opener_open = _canonical_wallet_opener_open
    opener_class = OpenerDirector
    request_class = Request
    canonical_http_redirect_handler_type = HTTPRedirectHandler
    canonical_proxy_handler_type = ProxyHandler
    canonical_redirect_handler_type = _RejectRedirectHandler
    canonical_https_handler_type = HTTPSHandler
    canonical_abstract_http_handler_type = AbstractHTTPHandler
    canonical_http_error_processor_type = HTTPErrorProcessor
    ssl_context_type = ssl.SSLContext
    cert_required = ssl.CERT_REQUIRED
    response_type = ProphetXHttpResponse
    error_type = ProphetXReadOnlyError
    http_error_type = HTTPError
    url_error_type = URLError
    timeout_error_type = TimeoutError
    os_error_type = OSError
    http_exception_type = HTTPException
    balance_url = BALANCE_URL

    def make_provider_fetch(
        opener: object,
        max_response_bytes: int,
    ):
        """Capture one construction-time primitive for positive wallet authority."""

        opener_type = type(opener)
        if opener_type is not opener_class:
            raise error_type(
                "canonical ProphetX account network authority requires OpenerDirector"
            )
        if not stdlib_dispatch_is_canonical():
            raise error_type(
                "canonical ProphetX account network dispatch changed "
                "before construction"
            )

        open_response = canonical_stdlib_opener_open.__get__(opener, opener_class)
        request_type = request_class
        http_redirect_handler_type = canonical_http_redirect_handler_type
        proxy_handler_type = canonical_proxy_handler_type
        redirect_handler_type = canonical_redirect_handler_type
        https_handler_type = canonical_https_handler_type
        abstract_http_handler_type = canonical_abstract_http_handler_type
        http_error_processor_type = canonical_http_error_processor_type

        canonical_opener_open = canonical_stdlib_opener_open
        canonical_opener_internal_open = opener_type._open
        canonical_opener_call_chain = opener_type._call_chain
        canonical_opener_error = opener_type.error
        canonical_redirect_request = redirect_handler_type.redirect_request
        canonical_https_open = https_handler_type.https_open
        canonical_https_request = https_handler_type.https_request
        canonical_do_open = abstract_http_handler_type.do_open
        canonical_https_response = http_error_processor_type.https_response

        def tls_context_snapshot(context: object):
            if type(context) is not ssl_context_type:
                return None
            try:
                ciphers = tuple(
                    (
                        cipher.get("id"),
                        cipher.get("name"),
                        cipher.get("protocol"),
                        cipher.get("strength_bits"),
                        cipher.get("alg_bits"),
                        cipher.get("aead"),
                        cipher.get("symmetric"),
                        cipher.get("digest"),
                        cipher.get("kea"),
                        cipher.get("auth"),
                    )
                    for cipher in context.get_ciphers()
                )
                trust_anchors = tuple(
                    sorted(
                        sha256(certificate).hexdigest()
                        for certificate in context.get_ca_certs(binary_form=True)
                    )
                )
                store_stats = tuple(sorted(context.cert_store_stats().items()))
                return (
                    int(context.verify_mode),
                    context.check_hostname,
                    int(context.verify_flags),
                    int(context.minimum_version),
                    int(context.maximum_version),
                    int(context.options),
                    getattr(context, "hostname_checks_common_name", None),
                    getattr(context, "security_level", None),
                    store_stats,
                    trust_anchors,
                    ciphers,
                )
            except (AttributeError, TypeError, ValueError, ssl.SSLError):
                return None

        def dispatch_snapshot(current_opener: object):
            records: list[tuple[str, object, tuple[object, ...]]] = []
            for map_name in ("handle_open", "process_request", "process_response"):
                mapping = getattr(current_opener, map_name, None)
                if type(mapping) is not dict:
                    return None
                for key, handlers in mapping.items():
                    if type(key) not in (str, int) or type(handlers) is not list:
                        return None
                    records.append((map_name, key, tuple(handlers)))
            error_mapping = getattr(current_opener, "handle_error", None)
            if type(error_mapping) is not dict:
                return None
            for protocol, by_code in error_mapping.items():
                if type(protocol) not in (str, int) or type(by_code) is not dict:
                    return None
                for code, handlers in by_code.items():
                    if type(code) not in (str, int) or type(handlers) is not list:
                        return None
                    records.append(
                        (f"handle_error:{protocol}", code, tuple(handlers))
                    )
            records.sort(
                key=lambda item: (
                    item[0],
                    type(item[1]).__name__,
                    str(item[1]),
                )
            )
            return tuple(records)

        expected_handlers_raw = getattr(opener, "handlers", None)
        expected_dispatch = dispatch_snapshot(opener)
        if type(expected_handlers_raw) is not list or expected_dispatch is None:
            raise error_type(
                "canonical ProphetX account opener graph is not inspectable"
            )
        expected_handlers = tuple(expected_handlers_raw)

        redirect_handlers = tuple(
            handler
            for handler in expected_handlers
            if isinstance(handler, http_redirect_handler_type)
        )
        https_handlers = tuple(
            handler
            for handler in expected_handlers
            if isinstance(handler, https_handler_type)
        )
        response_handlers = tuple(
            handlers
            for map_name, key, handlers in expected_dispatch
            if map_name == "process_response" and key == "https"
        )
        if (
            len(redirect_handlers) != 1
            or type(redirect_handlers[0]) is not redirect_handler_type
            or len(https_handlers) != 1
            or type(https_handlers[0]) is not https_handler_type
            or len(response_handlers) != 1
            or len(response_handlers[0]) != 1
            or type(response_handlers[0][0]) is not http_error_processor_type
            or any(isinstance(handler, proxy_handler_type) for handler in expected_handlers)
        ):
            raise error_type(
                "canonical ProphetX account opener graph is invalid"
            )
        expected_redirect_handler = redirect_handlers[0]
        expected_https_handler = https_handlers[0]
        expected_error_processor = response_handlers[0][0]
        expected_tls_context = getattr(expected_https_handler, "_context", None)
        expected_tls_context_state = tls_context_snapshot(expected_tls_context)
        legacy_check_hostname_missing = object()
        expected_legacy_check_hostname = getattr(
            expected_https_handler,
            "_check_hostname",
            legacy_check_hostname_missing,
        )
        if (
            expected_tls_context_state is None
            or expected_tls_context.verify_mode != cert_required
            or expected_tls_context.check_hostname is not True
            or expected_legacy_check_hostname
            not in (legacy_check_hostname_missing, None)
        ):
            raise error_type(
                "canonical ProphetX account TLS verifier is invalid"
            )

        if (
            getattr(open_response, "__self__", None) is not opener
            or getattr(open_response, "__func__", None) is not canonical_opener_open
        ):
            raise error_type(
                "canonical ProphetX account opener dispatch is invalid"
            )

        def dispatch_matches(current, expected) -> bool:
            if current is None or len(current) != len(expected):
                return False
            for actual, wanted in zip(current, expected):
                if actual[0] != wanted[0] or actual[1] != wanted[1]:
                    return False
                if len(actual[2]) != len(wanted[2]):
                    return False
                if any(
                    actual_handler is not expected_handler
                    for actual_handler, expected_handler in zip(
                        actual[2], wanted[2]
                    )
                ):
                    return False
            return True

        def authority_is_current() -> bool:
            if (
                not stdlib_dispatch_is_canonical()
                or type(opener) is not opener_type
                or opener_type.open is not canonical_opener_open
                or opener_type._open is not canonical_opener_internal_open
                or opener_type._call_chain is not canonical_opener_call_chain
                or opener_type.error is not canonical_opener_error
                or redirect_handler_type.redirect_request
                is not canonical_redirect_request
                or https_handler_type.https_open is not canonical_https_open
                or https_handler_type.https_request is not canonical_https_request
                or abstract_http_handler_type.do_open is not canonical_do_open
                or http_error_processor_type.https_response
                is not canonical_https_response
            ):
                return False

            current_tls_context = getattr(expected_https_handler, "_context", None)
            if (
                current_tls_context is not expected_tls_context
                or tls_context_snapshot(current_tls_context)
                != expected_tls_context_state
                or getattr(
                    expected_https_handler,
                    "_check_hostname",
                    legacy_check_hostname_missing,
                )
                is not expected_legacy_check_hostname
            ):
                return False

            opener_dict = getattr(opener, "__dict__", None)
            if type(opener_dict) is not dict or any(
                name in opener_dict for name in ("open", "_open", "_call_chain", "error")
            ):
                return False

            handlers = getattr(opener, "handlers", None)
            if type(handlers) is not list or len(handlers) != len(expected_handlers):
                return False
            if any(
                current is not expected
                for current, expected in zip(handlers, expected_handlers)
            ):
                return False
            if any(isinstance(handler, proxy_handler_type) for handler in handlers):
                return False

            redirect_dict = getattr(expected_redirect_handler, "__dict__", None)
            https_dict = getattr(expected_https_handler, "__dict__", None)
            error_processor_dict = getattr(expected_error_processor, "__dict__", None)
            if (
                type(redirect_dict) is not dict
                or "redirect_request" in redirect_dict
                or type(https_dict) is not dict
                or any(
                    name in https_dict
                    for name in ("https_open", "https_request", "do_open")
                )
                or type(error_processor_dict) is not dict
                or "https_response" in error_processor_dict
            ):
                return False

            current_dispatch = dispatch_snapshot(opener)
            if not dispatch_matches(current_dispatch, expected_dispatch):
                return False

            dispatch_handlers = tuple(
                handlers
                for map_name, key, handlers in current_dispatch
                if map_name == "handle_open" and key == "https"
            )
            request_handlers = tuple(
                handlers
                for map_name, key, handlers in current_dispatch
                if map_name == "process_request" and key == "https"
            )
            response_handlers_now = tuple(
                handlers
                for map_name, key, handlers in current_dispatch
                if map_name == "process_response" and key == "https"
            )
            if (
                len(dispatch_handlers) != 1
                or len(dispatch_handlers[0]) != 1
                or dispatch_handlers[0][0] is not expected_https_handler
                or len(request_handlers) != 1
                or len(request_handlers[0]) != 1
                or request_handlers[0][0] is not expected_https_handler
                or len(response_handlers_now) != 1
                or len(response_handlers_now[0]) != 1
                or response_handlers_now[0][0] is not expected_error_processor
                or any(
                    not any(handler is registered for registered in expected_handlers)
                    for _map_name, _key, mapped_handlers in current_dispatch
                    for handler in mapped_handlers
                )
            ):
                return False
            return True

        def require_authority() -> None:
            if not authority_is_current():
                raise error_type(
                    "canonical ProphetX account network authority changed"
                )

        def fetch(
            url: str,
            *,
            headers: Mapping[str, str],
            timeout_seconds: float,
        ) -> ProphetXHttpResponse:
            if url != balance_url:
                raise error_type(
                    "ProphetX transport target is outside the fixed balance origin"
                )
            require_authority()
            request = request_type(url, headers=dict(headers), method="GET")
            try:
                with canonical_opener_open(
                    opener, request, timeout=timeout_seconds
                ) as response:
                    body = response.read(max_response_bytes + 1)
                    if len(body) > max_response_bytes:
                        raise error_type(
                            "ProphetX response exceeded the size limit"
                        )
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        stripped = content_length.strip()
                        if (
                            not stripped
                            or not stripped.isascii()
                            or not stripped.isdigit()
                        ):
                            raise error_type(
                                "ProphetX response Content-Length is invalid"
                            )
                        if int(stripped) != len(body):
                            raise error_type(
                                "ProphetX response Content-Length does not match body"
                            )
                    result = response_type(
                        status=int(response.getcode()),
                        final_url=str(response.geturl()),
                        content_type=response.headers.get("Content-Type"),
                        content_encoding=response.headers.get("Content-Encoding"),
                        body=body,
                    )
                require_authority()
                return result
            except error_type:
                raise
            except http_error_type as exc:
                status = exc.code
                exc.close()
                raise error_type(
                    f"ProphetX HTTP request failed with status {status}"
                ) from None
            except (
                url_error_type,
                timeout_error_type,
                os_error_type,
                http_exception_type,
            ):
                raise error_type("ProphetX network request failed") from None

        return fetch


    return make_provider_fetch


_make_provider_fetch = _build_provider_fetch_factory()
del _build_provider_fetch_factory

def _build_transport_init():
    provider_fetch_factory = _make_provider_fetch
    create_default_context = ssl.create_default_context
    build_canonical_opener = build_opener
    proxy_handler_type = ProxyHandler
    redirect_handler_type = _RejectRedirectHandler
    https_handler_type = HTTPSHandler
    value_error_type = ValueError

    def sealed_init(
        self,
        *,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        if (
            not isinstance(max_response_bytes, int)
            or isinstance(max_response_bytes, bool)
            or max_response_bytes <= 0
        ):
            raise value_error_type(
                "max_response_bytes must be a positive integer"
            )
        self._max_response_bytes = max_response_bytes
        tls_context = create_default_context()
        tls_context.set_alpn_protocols(["http/1.1"])
        self._opener = build_canonical_opener(
            proxy_handler_type({}),
            redirect_handler_type(),
            https_handler_type(context=tls_context),
        )
        self._provider_fetch = provider_fetch_factory(
            self._opener,
            max_response_bytes,
        )

    return sealed_init


class UrllibProphetXHttpTransport:
    __init__ = _build_transport_init()

    def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> ProphetXHttpResponse:
        if url != BALANCE_URL:
            raise ProphetXReadOnlyError(
                "ProphetX transport target is outside the fixed balance origin"
            )
        request = Request(url, headers=dict(headers), method="GET")
        try:
            with self._opener.open(request, timeout=timeout_seconds) as response:
                body = response.read(self._max_response_bytes + 1)
                if len(body) > self._max_response_bytes:
                    raise ProphetXReadOnlyError(
                        "ProphetX response exceeded the size limit"
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    stripped = content_length.strip()
                    if (
                        not stripped
                        or not stripped.isascii()
                        or not stripped.isdigit()
                    ):
                        raise ProphetXReadOnlyError(
                            "ProphetX response Content-Length is invalid"
                        )
                    if int(stripped) != len(body):
                        raise ProphetXReadOnlyError(
                            "ProphetX response Content-Length does not match body"
                        )
                return ProphetXHttpResponse(
                    status=int(response.getcode()),
                    final_url=str(response.geturl()),
                    content_type=response.headers.get("Content-Type"),
                    content_encoding=response.headers.get("Content-Encoding"),
                    body=body,
                )
        except ProphetXReadOnlyError:
            raise
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise ProphetXReadOnlyError(
                f"ProphetX HTTP request failed with status {status}"
            ) from None
        except (URLError, TimeoutError, OSError, HTTPException):
            raise ProphetXReadOnlyError("ProphetX network request failed") from None


del _build_transport_init

_CANONICAL_WALLET_GET = UrllibProphetXHttpTransport.get
_PROVIDER_TRANSPORTS: WeakKeyDictionary[
    object, UrllibProphetXHttpTransport
] = WeakKeyDictionary()
_PROVIDER_FETCHES: WeakKeyDictionary[object, object] = WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class ProphetXEvidence:
    observed_at: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        _iso_timestamp(self.observed_at, "observed_at")
        _sha256_hex(self.source_payload_sha256, "source_payload_sha256")


@dataclass(frozen=True, slots=True)
class ProphetXWalletObservation:
    """Provider-native wallet evidence from one exact get_balance response.

    Only 'balance' maps to canonical available cash. GEC, matched funds and unmatched
    funds remain provider-specific evidence because they have distinct economic semantics.
    """

    balance: Decimal
    gec_balance: Decimal
    matched_order_balance: Decimal
    unmatched_order_balance: Decimal
    unmatched_order_balance_status: str
    unmatched_order_last_synced_at: str | None
    evidence: ProphetXEvidence

    def __post_init__(self) -> None:
        _nonnegative_decimal(self.balance, "balance")
        _nonnegative_decimal(self.gec_balance, "gec_balance")
        _nonnegative_decimal(self.matched_order_balance, "matched_order_balance")
        _nonnegative_decimal(self.unmatched_order_balance, "unmatched_order_balance")
        if self.unmatched_order_balance_status not in {"succeed", "failed"}:
            raise ProphetXReadOnlyError(
                "unmatched_order_balance_status must be 'succeed' or 'failed'"
            )
        if self.unmatched_order_last_synced_at is not None:
            _iso_timestamp(
                self.unmatched_order_last_synced_at,
                "unmatched_order_last_synced_at",
            )
        if (
            self.unmatched_order_balance_status == "succeed"
            and self.unmatched_order_last_synced_at is None
        ):
            raise ProphetXReadOnlyError(
                "successful unmatched-order balance requires last-synced timestamp"
            )
        if not isinstance(self.evidence, ProphetXEvidence):
            raise ProphetXReadOnlyError("wallet evidence must be ProphetXEvidence")


class ProphetXReadOnlyClient:
    """One fixed-origin ProphetX sandbox balance reader.

    The caller supplies an already-issued bearer session token. Login/session renewal is a
    separate credential-plane concern and is intentionally outside this source slice.

    Caller-supplied transports remain useful for deterministic structural parsing tests, but
    they cannot mint canonical provider/account authority. Positive BALANCE_READ authority is
    reserved for the exact fixed-origin transport instance constructed internally by this
    client and is invalidated if that transport is replaced after construction.
    """

    def __init__(
        self,
        session: ProphetXSessionToken,
        *,
        transport: ProphetXHttpTransport | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        venue_id: str = "prophetx",
        account_id: str = "default-account",
    ) -> None:
        if not isinstance(session, ProphetXSessionToken):
            raise TypeError("session must be ProphetXSessionToken")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive and finite")
        self._session = session
        if transport is None:
            canonical_transport = UrllibProphetXHttpTransport()
            self._transport: ProphetXHttpTransport = canonical_transport
            if type(self) is ProphetXReadOnlyClient:
                _PROVIDER_TRANSPORTS[self] = canonical_transport
                _PROVIDER_FETCHES[self] = canonical_transport._provider_fetch
        else:
            self._transport = transport
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._venue_id = _required_text(venue_id, "venue_id")
        self._account_id = _required_text(account_id, "account_id")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(adapter_id={ADAPTER_ID!r}, "
            f"adapter_version={ADAPTER_VERSION!r}, environment='sandbox')"
        )

    def read_wallet(
        self,
        *,
        _authority_resolver=None,
    ) -> ProphetXWalletObservation:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {self._session.access_token}",
        }
        authoritative_fetch = None
        authority_resolver = None
        if _authority_resolver is not None:
            authority_resolver = _authority_resolver
            authoritative_fetch = authority_resolver(self)
            response = authoritative_fetch(
                BALANCE_URL,
                headers=headers,
                timeout_seconds=self._timeout_seconds,
            )
        else:
            canonical_transport = _PROVIDER_TRANSPORTS.get(self)
            if canonical_transport is not None:
                authority_resolver = _require_canonical_network_authority
                authoritative_fetch = authority_resolver(self)
                response = authoritative_fetch(
                    BALANCE_URL,
                    headers=headers,
                    timeout_seconds=self._timeout_seconds,
                )
            else:
                response = self._transport.get(
                    BALANCE_URL,
                    headers=headers,
                    timeout_seconds=self._timeout_seconds,
                )
        self._validate_http_response(response)
        if (
            authoritative_fetch is not None
            and authority_resolver(self) is not authoritative_fetch
        ):
            raise ProphetXReadOnlyError(
                "canonical ProphetX account network authority changed during acquisition"
            )
        payload_sha256 = sha256(response.body).hexdigest()
        decoded = _decode_json(response.body)
        envelope = _mapping(decoded, "ProphetX balance response")
        data = _mapping(envelope.get("data"), "ProphetX balance response.data")
        balance = _provider_money(data, "balance")
        gec_balance = _provider_money(data, "gec_balance")
        matched_order_balance = _provider_money(data, "matched_order_balance")
        unmatched_order_balance = _provider_money(data, "unmatched_order_balance")
        status = _provider_text(
            data,
            "unmatched_order_balance_status",
            "unmatched_order_balance_status",
        )
        last_synced_raw = data.get("unmatched_order_last_synced_at")
        if last_synced_raw is not None:
            last_synced = _required_text(
                last_synced_raw,
                "unmatched_order_last_synced_at",
            )
        else:
            last_synced = None

        # This is product availability time, not a provider-native timestamp.
        # Capture it only after complete-body, representation and field parsing
        # have succeeded so accepted evidence cannot be backdated by parse time.
        observed_at = self._observed_at()
        return ProphetXWalletObservation(
            balance=balance,
            gec_balance=gec_balance,
            matched_order_balance=matched_order_balance,
            unmatched_order_balance=unmatched_order_balance,
            unmatched_order_balance_status=status,
            unmatched_order_last_synced_at=last_synced,
            evidence=ProphetXEvidence(observed_at, payload_sha256),
        )

    def capability_profile(self) -> BookmakerCapabilityProfile:
        wallet = self.read_wallet()
        self._require_synchronized_wallet(wallet)
        self._require_provider_origin_authority()
        return self._profile_for(wallet)

    def read_account_snapshot(
        self,
        requested_capabilities: frozenset[BookmakerCapability],
        /,
    ) -> BookmakerAccountSnapshot:
        if not isinstance(requested_capabilities, frozenset):
            raise TypeError("requested_capabilities must be a frozenset")
        if not requested_capabilities:
            raise ProphetXReadOnlyError(
                "at least one account capability must be requested"
            )
        if requested_capabilities != frozenset({BookmakerCapability.BALANCE_READ}):
            raise ProphetXReadOnlyError(
                "ProphetX wallet adapter implements only balance_read"
            )

        wallet = self.read_wallet()
        self._require_synchronized_wallet(wallet)
        self._require_provider_origin_authority()
        profile = self._profile_for(wallet)
        balance = BookmakerBalanceObservation(
            venue_id=self._venue_id,
            account_id=self._account_id,
            adapter_id=ADAPTER_ID,
            observation_id=f"wallet:{wallet.evidence.source_payload_sha256}",
            currency=PROVIDER_CURRENCY,
            available_balance=wallet.balance,
            observed_at=wallet.evidence.observed_at,
            source_payload_sha256=wallet.evidence.source_payload_sha256,
            total_balance=None,
            exposure=None,
            retained_commission=None,
            exposure_limit=None,
        )
        return BookmakerAccountSnapshot(
            profile=profile,
            observed_capabilities=frozenset({BookmakerCapability.BALANCE_READ}),
            observed_at=wallet.evidence.observed_at,
            balance=balance,
        )

    def _profile_for(
        self, wallet: ProphetXWalletObservation
    ) -> BookmakerCapabilityProfile:
        digest = wallet.evidence.source_payload_sha256
        return BookmakerCapabilityProfile(
            venue_id=self._venue_id,
            account_id=self._account_id,
            adapter_id=ADAPTER_ID,
            adapter_version=ADAPTER_VERSION,
            profile_version=1,
            facts=(
                BookmakerCapabilityFact(
                    BookmakerCapability.BALANCE_READ,
                    BookmakerCapabilityState.SUPPORTED,
                ),
            ),
            observed_at=wallet.evidence.observed_at,
            source_ref=f"prophetx://sandbox/wallet/{digest}",
            source_payload_sha256=digest,
        )

    def _require_provider_origin_authority(self) -> None:
        _require_canonical_network_authority(self)

    @staticmethod
    def _require_synchronized_wallet(wallet: ProphetXWalletObservation) -> None:
        if wallet.unmatched_order_balance_status != "succeed":
            raise ProphetXReadOnlyError(
                "ProphetX unmatched-order balance synchronization is not successful"
            )

    @staticmethod
    def _validate_http_response(response: ProphetXHttpResponse) -> None:
        if not isinstance(response, ProphetXHttpResponse):
            raise ProphetXReadOnlyError(
                "ProphetX transport must return ProphetXHttpResponse"
            )
        if response.status != 200:
            raise ProphetXReadOnlyError(
                f"ProphetX HTTP request failed with status {response.status}"
            )
        if response.final_url != BALANCE_URL:
            raise ProphetXReadOnlyError(
                "ProphetX response origin changed unexpectedly"
            )
        if len(response.body) > _MAX_RESPONSE_BYTES:
            raise ProphetXReadOnlyError(
                "ProphetX response exceeded the size limit"
            )
        if response.content_encoding is not None:
            encoding = response.content_encoding.strip().lower()
            if encoding and encoding != "identity":
                raise ProphetXReadOnlyError(
                    "ProphetX response used unsupported content encoding"
                )
        if response.content_type is None:
            raise ProphetXReadOnlyError(
                "ProphetX response is missing Content-Type"
            )
        media_type = response.content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ProphetXReadOnlyError(
                "ProphetX response Content-Type is not application/json"
            )

    def _observed_at(self) -> str:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise ProphetXReadOnlyError(
                "clock must return timezone-aware datetime"
            )
        return value.isoformat()

    @staticmethod
    def _build_provider_origin_issuers(
        read_wallet_impl,
        require_sync_impl,
        profile_for_impl,
        provider_transports,
        provider_fetches,
        canonical_transport_type,
        canonical_wallet_get,
        error_type,
    ):
        """Bind positive issuance to captured provider-origin authorities."""

        def authoritative_network_fetch(client):
            canonical = provider_transports.get(client)
            expected_fetch = provider_fetches.get(client)
            if (
                canonical is None
                or type(canonical) is not canonical_transport_type
                or client._transport is not canonical
                or type(canonical).get is not canonical_wallet_get
                or expected_fetch is None
                or canonical._provider_fetch is not expected_fetch
            ):
                raise error_type(
                    "canonical ProphetX account authority requires "
                    "product-owned transport"
                )
            return expected_fetch

        def authoritative_wallet(client):
            expected_fetch = authoritative_network_fetch(client)
            wallet = read_wallet_impl(
                client,
                _authority_resolver=authoritative_network_fetch,
            )
            if authoritative_network_fetch(client) is not expected_fetch:
                raise error_type(
                    "canonical ProphetX account network authority changed "
                    "during acquisition"
                )
            require_sync_impl(wallet)
            return wallet

        def capability_profile(self) -> BookmakerCapabilityProfile:
            wallet = authoritative_wallet(self)
            return profile_for_impl(self, wallet)

        def read_account_snapshot(
            self,
            requested_capabilities: frozenset[BookmakerCapability],
            /,
        ) -> BookmakerAccountSnapshot:
            if not isinstance(requested_capabilities, frozenset):
                raise TypeError("requested_capabilities must be a frozenset")
            if not requested_capabilities:
                raise error_type(
                    "at least one account capability must be requested"
                )
            if requested_capabilities != frozenset(
                {BookmakerCapability.BALANCE_READ}
            ):
                raise error_type(
                    "ProphetX wallet adapter implements only balance_read"
                )

            wallet = authoritative_wallet(self)
            profile = profile_for_impl(self, wallet)
            balance = BookmakerBalanceObservation(
                venue_id=self._venue_id,
                account_id=self._account_id,
                adapter_id=ADAPTER_ID,
                observation_id=(
                    f"wallet:{wallet.evidence.source_payload_sha256}"
                ),
                currency=PROVIDER_CURRENCY,
                available_balance=wallet.balance,
                observed_at=wallet.evidence.observed_at,
                source_payload_sha256=wallet.evidence.source_payload_sha256,
                total_balance=None,
                exposure=None,
                retained_commission=None,
                exposure_limit=None,
            )
            return BookmakerAccountSnapshot(
                profile=profile,
                observed_capabilities=frozenset(
                    {BookmakerCapability.BALANCE_READ}
                ),
                observed_at=wallet.evidence.observed_at,
                balance=balance,
            )

        return capability_profile, read_account_snapshot

    # Positive provider/account issuance never resolves read_wallet or the
    # provider-origin resolver through caller-writable instance/module aliases.
    # Public read_wallet keeps its structural parsing seam, while these two
    # authority-bearing methods use captured registry objects and exact callables.
    capability_profile, read_account_snapshot = (
        _build_provider_origin_issuers.__func__(
            read_wallet,
            _require_synchronized_wallet.__func__,
            _profile_for,
            _PROVIDER_TRANSPORTS,
            _PROVIDER_FETCHES,
            UrllibProphetXHttpTransport,
            _CANONICAL_WALLET_GET,
            ProphetXReadOnlyError,
        )
    )
    del _build_provider_origin_issuers


def _require_canonical_network_authority(
    client: ProphetXReadOnlyClient,
):
    """Return the exact construction-time wallet fetch or fail closed on drift."""

    canonical = _PROVIDER_TRANSPORTS.get(client)
    expected_fetch = _PROVIDER_FETCHES.get(client)
    if (
        type(client) is not ProphetXReadOnlyClient
        or canonical is None
        or type(canonical) is not UrllibProphetXHttpTransport
        or client._transport is not canonical
        or type(canonical).get is not _CANONICAL_WALLET_GET
        or expected_fetch is None
        or canonical._provider_fetch is not expected_fetch
    ):
        raise ProphetXReadOnlyError(
            "canonical ProphetX account authority requires product-owned transport"
        )
    return expected_fetch


def _decode_json(payload: bytes) -> object:
    def reject_duplicate_pairs(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ProphetXReadOnlyError(
                    "ProphetX JSON contains duplicate object key"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> object:
        raise ProphetXReadOnlyError(
            "ProphetX JSON contains non-standard numeric constant"
        )

    try:
        return json.loads(
            payload.decode("utf-8"),
            parse_float=Decimal,
            parse_int=Decimal,
            object_pairs_hook=reject_duplicate_pairs,
            parse_constant=reject_constant,
        )
    except ProphetXReadOnlyError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ProphetXReadOnlyError(
            "ProphetX response is not valid UTF-8 JSON"
        ) from None


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ProphetXReadOnlyError(f"{field} must be a JSON object")
    return value


def _provider_text(
    value: Mapping[str, object], key: str, field: str
) -> str:
    if key not in value:
        raise ProphetXReadOnlyError(
            f"{field} is missing from provider response"
        )
    return _required_text(value[key], field)


def _provider_money(value: Mapping[str, object], key: str) -> Decimal:
    if key not in value:
        raise ProphetXReadOnlyError(
            f"{key} is missing from provider response"
        )
    raw = value[key]
    if not isinstance(raw, Decimal):
        raise ProphetXReadOnlyError(
            f"{key} must be a JSON number decoded without binary float"
        )
    return _nonnegative_decimal(raw, key)


def _session_token(value: object) -> str:
    text = _required_text(value, "access_token")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in text):
        raise ProphetXReadOnlyError(
            "access_token must not contain whitespace or control characters"
        )
    return text


def _required_text(value: object, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise ProphetXReadOnlyError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _nonnegative_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ProphetXReadOnlyError(
            f"{field} must be a finite Decimal"
        )
    if value < 0:
        raise ProphetXReadOnlyError(
            f"{field} must be non-negative"
        )
    return value


def _iso_timestamp(value: object, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(
            text.replace("Z", "+00:00")
        )
    except ValueError:
        raise ProphetXReadOnlyError(
            f"{field} must be ISO-8601"
        ) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProphetXReadOnlyError(
            f"{field} must include timezone offset"
        )
    return parsed


def _sha256_hex(value: object, field: str) -> str:
    text = _required_text(value, field)
    if len(text) != 64 or any(
        char not in "0123456789abcdef" for char in text
    ):
        raise ProphetXReadOnlyError(
            f"{field} must be lowercase 64-character SHA-256"
        )
    return text


def _build_positive_wallet_provenance_guard():
    """Freeze the parser/DTO graph used to mint canonical wallet evidence."""

    client_type = ProphetXReadOnlyClient
    error_type = ProphetXReadOnlyError
    profile_impl = client_type.capability_profile
    snapshot_impl = client_type.read_account_snapshot
    validate_response_impl = client_type._validate_http_response
    observed_at_impl = client_type._observed_at
    profile_for_impl = client_type._profile_for
    require_sync_impl = client_type._require_synchronized_wallet

    decode_json_impl = _decode_json
    mapping_impl = _mapping
    provider_text_impl = _provider_text
    provider_money_impl = _provider_money
    required_text_impl = _required_text
    nonnegative_decimal_impl = _nonnegative_decimal
    iso_timestamp_impl = _iso_timestamp
    sha256_hex_impl = _sha256_hex

    decode_json_code = decode_json_impl.__code__
    mapping_code = mapping_impl.__code__
    provider_text_code = provider_text_impl.__code__
    provider_money_code = provider_money_impl.__code__
    required_text_code = required_text_impl.__code__
    nonnegative_decimal_code = nonnegative_decimal_impl.__code__
    iso_timestamp_code = iso_timestamp_impl.__code__
    sha256_hex_code = sha256_hex_impl.__code__
    validate_response_code = validate_response_impl.__code__
    observed_at_code = observed_at_impl.__code__
    profile_for_code = profile_for_impl.__code__
    require_sync_code = require_sync_impl.__code__

    json_module = json
    json_loads_impl = json.loads
    decimal_type = Decimal
    datetime_type = datetime
    mapping_type = Mapping
    sha256_impl = sha256
    evidence_type = ProphetXEvidence
    wallet_type = ProphetXWalletObservation
    response_type = ProphetXHttpResponse

    account_snapshot_type = BookmakerAccountSnapshot
    balance_observation_type = BookmakerBalanceObservation
    capability_type = BookmakerCapability
    capability_fact_type = BookmakerCapabilityFact
    capability_profile_type = BookmakerCapabilityProfile
    capability_state_type = BookmakerCapabilityState
    adapter_id = ADAPTER_ID
    adapter_version = ADAPTER_VERSION
    provider_currency = PROVIDER_CURRENCY

    def same_function(current, expected, expected_code) -> bool:
        return (
            current is expected
            and getattr(current, "__code__", None) is expected_code
        )

    def require_provenance(client) -> None:
        instance_dict = getattr(client, "__dict__", None)
        if (
            type(client) is not client_type
            or type(instance_dict) is not dict
            or any(
                name in instance_dict
                for name in ("_validate_http_response", "_observed_at")
            )
            or not same_function(
                client_type._validate_http_response,
                validate_response_impl,
                validate_response_code,
            )
            or not same_function(
                client_type._observed_at,
                observed_at_impl,
                observed_at_code,
            )
            or not same_function(
                profile_for_impl,
                client_type._profile_for,
                profile_for_code,
            )
            or not same_function(
                require_sync_impl,
                client_type._require_synchronized_wallet,
                require_sync_code,
            )
            or not same_function(_decode_json, decode_json_impl, decode_json_code)
            or not same_function(_mapping, mapping_impl, mapping_code)
            or not same_function(
                _provider_text,
                provider_text_impl,
                provider_text_code,
            )
            or not same_function(
                _provider_money,
                provider_money_impl,
                provider_money_code,
            )
            or not same_function(
                _required_text,
                required_text_impl,
                required_text_code,
            )
            or not same_function(
                _nonnegative_decimal,
                nonnegative_decimal_impl,
                nonnegative_decimal_code,
            )
            or not same_function(
                _iso_timestamp,
                iso_timestamp_impl,
                iso_timestamp_code,
            )
            or not same_function(
                _sha256_hex,
                sha256_hex_impl,
                sha256_hex_code,
            )
            or json is not json_module
            or json.loads is not json_loads_impl
            or Decimal is not decimal_type
            or datetime is not datetime_type
            or Mapping is not mapping_type
            or sha256 is not sha256_impl
            or ProphetXReadOnlyError is not error_type
            or ProphetXEvidence is not evidence_type
            or ProphetXWalletObservation is not wallet_type
            or ProphetXHttpResponse is not response_type
            or BookmakerAccountSnapshot is not account_snapshot_type
            or BookmakerBalanceObservation is not balance_observation_type
            or BookmakerCapability is not capability_type
            or BookmakerCapabilityFact is not capability_fact_type
            or BookmakerCapabilityProfile is not capability_profile_type
            or BookmakerCapabilityState is not capability_state_type
            or ADAPTER_ID != adapter_id
            or ADAPTER_VERSION != adapter_version
            or PROVIDER_CURRENCY != provider_currency
        ):
            raise error_type(
                "canonical ProphetX account parsing authority changed"
            )

    def capability_profile(self):
        require_provenance(self)
        result = profile_impl(self)
        require_provenance(self)
        return result

    def read_account_snapshot(self, requested_capabilities, /):
        require_provenance(self)
        result = snapshot_impl(self, requested_capabilities)
        require_provenance(self)
        return result

    return capability_profile, read_account_snapshot


(
    ProphetXReadOnlyClient.capability_profile,
    ProphetXReadOnlyClient.read_account_snapshot,
) = _build_positive_wallet_provenance_guard()
del _build_positive_wallet_provenance_guard

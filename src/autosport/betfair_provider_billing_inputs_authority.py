"""Product-owned issuance verification for Betfair provider billing observations.

The descriptive DTOs in :mod:`betfair_provider_billing_inputs` deliberately remain
serializable/inspectable data. Their public constructors and deterministic digests
are therefore not provenance. Positive consumers must use this module's canonical
read wrapper and validator: the wrapper owns construction of the existing strict
Betfair read-only client with the canonical HTTP transport and a closure-captured
UTC clock, then records the exact returned observation in a closure-private
issuance relation. Callers can provide credentials and bounded read scope, but
cannot inject a client, transport, clock, venue/account label, or pre-built
observation through the supported API.

The provenance threat boundary is the repository's canonical trusted-process
boundary, matching :mod:`_provider_receipt_trust_root`: it protects against
caller-created objects, structural witnesses, consumer API misuse, issued-object
tamper, and caller injection through supported APIs. The executable identity
checks below are fail-fast defense in depth for known drift/rebinding seams.

It deliberately does *not* claim an OS sandbox against arbitrary code injection or
arbitrary monkeypatching of Python/stdlib runtime internals inside the already
trusted Autosport process. Such code can replace transitive network functions
below any finite in-process fence. A stronger origin guarantee requires
provider-signed evidence or a separately isolated service/process issuer; the
current Betfair provider contract supplies neither. This module must not imply that
stronger guarantee.

This is an in-process observation capability, not a second provider client, billing
store, economic classifier, allocation authority, or durable cost record.
"""
from __future__ import annotations

from datetime import datetime, timezone
import urllib.request as _urllib_request

from . import betfair_account_readonly as _readonly
from . import betfair_provider_billing_inputs as _inputs
from .betfair_account_readonly import (
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
    UrllibBetfairHttpTransport,
)


PROVIDER_BILLING_ORIGIN_TRUST_SCOPE = "trusted-autosport-process-v1"
PROVIDER_BILLING_ORIGIN_PROTECTS = (
    "caller-created-observation",
    "caller-injected-client-transport-clock",
    "consumer-api-misuse",
    "issued-object-tamper",
)
PROVIDER_BILLING_ORIGIN_EXCLUDES = (
    "arbitrary-same-process-code-injection",
    "arbitrary-stdlib-runtime-monkeypatch",
)


class BetfairProviderBillingInputsAuthorityError(BetfairReadOnlyError):
    """Raised when a provider-billing observation lacks canonical read issuance."""


def _build_observation_authority():
    read_impl = _inputs.__dict__["read_betfair_provider_billing_inputs"]
    source_cls = _inputs.__dict__["BetfairProviderBillingInputsObservation"]
    source_post_init = source_cls.__dict__["__post_init__"]
    client_cls = BetfairReadOnlyClient
    credentials_cls = BetfairSessionCredentials
    transport_cls = UrllibBetfairHttpTransport

    # #1496 moved the canonical credential-bearing transport from the mutable
    # module-global urllib opener to one instance-owned no-redirect opener. Bind
    # exactly that executable graph here; do not resurrect the obsolete urlopen
    # or urllib.request._opener authority boundary.
    client_init = client_cls.__dict__["__init__"]
    client_new = client_cls.__dict__.get("__new__")
    transport_init = transport_cls.__dict__["__init__"]
    transport_new = transport_cls.__dict__.get("__new__")
    transport_post = transport_cls.__dict__["post"]
    readonly_client_export = _readonly.__dict__["BetfairReadOnlyClient"]
    readonly_transport_export = _readonly.__dict__["UrllibBetfairHttpTransport"]
    request_ctor = _readonly.__dict__["Request"]
    readonly_build_opener = _readonly.__dict__["build_opener"]
    redirect_handler_cls = _readonly.__dict__["_RejectAuthenticatedRedirects"]
    redirect_request = redirect_handler_cls.__dict__["redirect_request"]

    stdlib_opener_cls = _urllib_request.__dict__["OpenerDirector"]
    stdlib_opener_open = stdlib_opener_cls.__dict__["open"]
    stdlib_opener_internal_open = stdlib_opener_cls.__dict__["_open"]
    stdlib_opener_call_chain = stdlib_opener_cls.__dict__["_call_chain"]
    stdlib_opener_error = stdlib_opener_cls.__dict__["error"]
    stdlib_redirect_handler_cls = _urllib_request.__dict__["HTTPRedirectHandler"]
    stdlib_https_handler_cls = _urllib_request.__dict__["HTTPSHandler"]
    stdlib_https_handler_open = stdlib_https_handler_cls.__dict__["https_open"]
    stdlib_https_handler_request = stdlib_https_handler_cls.__dict__["https_request"]
    stdlib_abstract_http_handler_cls = _urllib_request.__dict__["AbstractHTTPHandler"]
    stdlib_abstract_http_do_open = stdlib_abstract_http_handler_cls.__dict__["do_open"]
    stdlib_http_error_processor_cls = _urllib_request.__dict__["HTTPErrorProcessor"]
    stdlib_https_response = stdlib_http_error_processor_cls.__dict__["https_response"]

    now_utc = datetime.now
    utc = timezone.utc
    base_error_cls = BetfairReadOnlyError
    error_cls = BetfairProviderBillingInputsAuthorityError
    get_attr = object.__getattribute__
    object_new = object.__new__

    # Strongly retaining the issued object prevents id reuse while its issuance is
    # authoritative. The stored projection detects object.__setattr__ tampering.
    issued: dict[int, tuple[object, tuple[object, ...]]] = {}

    def assert_static_executable_authority() -> None:
        """Fail fast on known executable drift inside the trusted-process boundary."""

        if (
            _readonly.__dict__.get("BetfairReadOnlyClient") is not readonly_client_export
            or readonly_client_export is not client_cls
            or client_cls.__dict__.get("__init__") is not client_init
            or client_cls.__dict__.get("__new__") is not client_new
        ):
            raise error_cls("provider billing client executable drifted")
        if (
            _readonly.__dict__.get("UrllibBetfairHttpTransport")
            is not readonly_transport_export
            or readonly_transport_export is not transport_cls
            or transport_cls.__dict__.get("__init__") is not transport_init
            or transport_cls.__dict__.get("__new__") is not transport_new
            or transport_cls.__dict__.get("post") is not transport_post
        ):
            raise error_cls("provider billing transport executable drifted")
        if _readonly.__dict__.get("Request") is not request_ctor:
            raise error_cls("provider billing HTTP request executable drifted")
        if _readonly.__dict__.get("build_opener") is not readonly_build_opener:
            raise error_cls("provider billing network opener factory drifted")
        if (
            _readonly.__dict__.get("_RejectAuthenticatedRedirects")
            is not redirect_handler_cls
            or redirect_handler_cls.__dict__.get("redirect_request")
            is not redirect_request
        ):
            raise error_cls("provider billing redirect policy executable drifted")
        if (
            _urllib_request.__dict__.get("OpenerDirector") is not stdlib_opener_cls
            or stdlib_opener_cls.__dict__.get("open") is not stdlib_opener_open
            or stdlib_opener_cls.__dict__.get("_open") is not stdlib_opener_internal_open
            or stdlib_opener_cls.__dict__.get("_call_chain")
            is not stdlib_opener_call_chain
            or stdlib_opener_cls.__dict__.get("error") is not stdlib_opener_error
        ):
            raise error_cls("provider billing lower network opener drifted")
        if _urllib_request.__dict__.get("HTTPRedirectHandler") is not stdlib_redirect_handler_cls:
            raise error_cls("provider billing redirect handler executable drifted")
        if (
            _urllib_request.__dict__.get("HTTPSHandler") is not stdlib_https_handler_cls
            or stdlib_https_handler_cls.__dict__.get("https_open")
            is not stdlib_https_handler_open
            or stdlib_https_handler_cls.__dict__.get("https_request")
            is not stdlib_https_handler_request
        ):
            raise error_cls("provider billing HTTPS handler executable drifted")
        if (
            _urllib_request.__dict__.get("AbstractHTTPHandler")
            is not stdlib_abstract_http_handler_cls
            or stdlib_abstract_http_handler_cls.__dict__.get("do_open")
            is not stdlib_abstract_http_do_open
        ):
            raise error_cls("provider billing lower HTTP handler executable drifted")
        if (
            _urllib_request.__dict__.get("HTTPErrorProcessor")
            is not stdlib_http_error_processor_cls
            or stdlib_http_error_processor_cls.__dict__.get("https_response")
            is not stdlib_https_response
        ):
            raise error_cls("provider billing HTTPS response executable drifted")

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

    def transport_snapshot(
        transport: object,
    ) -> tuple[object, tuple[object, ...], tuple[tuple[str, object, tuple[object, ...]], ...]]:
        assert_static_executable_authority()
        if type(transport) is not transport_cls:
            raise error_cls("provider billing production transport is not canonical")
        state = vars(transport)
        if set(state) != {"_max_response_bytes", "_opener"}:
            raise error_cls("provider billing production transport state drifted")
        max_response_bytes = state.get("_max_response_bytes")
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise error_cls("provider billing production transport size limit drifted")

        opener = state.get("_opener")
        if type(opener) is not stdlib_opener_cls or any(
            name in vars(opener) for name in ("open", "_open", "_call_chain", "error")
        ):
            raise error_cls("provider billing private network opener drifted")
        handlers = getattr(opener, "handlers", None)
        if type(handlers) is not list:
            raise error_cls("provider billing private opener handlers drifted")
        handler_tuple = tuple(handlers)

        redirect_handlers = tuple(
            handler
            for handler in handler_tuple
            if isinstance(handler, stdlib_redirect_handler_cls)
        )
        if (
            len(redirect_handlers) != 1
            or type(redirect_handlers[0]) is not redirect_handler_cls
            or "redirect_request" in vars(redirect_handlers[0])
        ):
            raise error_cls("provider billing no-redirect policy drifted")

        https_handlers = tuple(
            handler for handler in handler_tuple if isinstance(handler, stdlib_https_handler_cls)
        )
        if len(https_handlers) != 1 or type(https_handlers[0]) is not stdlib_https_handler_cls:
            raise error_cls("provider billing canonical HTTPS handler is invalid")
        https_handler = https_handlers[0]
        if any(name in vars(https_handler) for name in ("https_open", "https_request", "do_open")):
            raise error_cls("provider billing canonical HTTPS handler is shadowed")

        dispatch = opener_dispatch_snapshot(opener)
        if dispatch is None:
            raise error_cls("provider billing private opener dispatch is invalid")
        https_open_handlers = tuple(
            values
            for map_name, key, values in dispatch
            if map_name == "handle_open" and key == "https"
        )
        https_request_handlers = tuple(
            values
            for map_name, key, values in dispatch
            if map_name == "process_request" and key == "https"
        )
        https_response_handlers = tuple(
            values
            for map_name, key, values in dispatch
            if map_name == "process_response" and key == "https"
        )
        if (
            len(https_open_handlers) != 1
            or len(https_open_handlers[0]) != 1
            or https_open_handlers[0][0] is not https_handler
            or len(https_request_handlers) != 1
            or len(https_request_handlers[0]) != 1
            or https_request_handlers[0][0] is not https_handler
            or len(https_response_handlers) != 1
            or len(https_response_handlers[0]) != 1
            or type(https_response_handlers[0][0]) is not stdlib_http_error_processor_cls
            or "https_response" in vars(https_response_handlers[0][0])
            or any(
                not any(handler is registered for registered in handler_tuple)
                for _map_name, _key, values in dispatch
                for handler in values
            )
        ):
            raise error_cls("provider billing private opener dispatch drifted")
        return opener, handler_tuple, dispatch

    def assert_transport_unchanged(
        transport: object,
        expected: tuple[
            object,
            tuple[object, ...],
            tuple[tuple[str, object, tuple[object, ...]], ...],
        ],
    ) -> None:
        current_opener, current_handlers, current_dispatch = transport_snapshot(transport)
        expected_opener, expected_handlers, expected_dispatch = expected
        if current_opener is not expected_opener or len(current_handlers) != len(expected_handlers):
            raise error_cls("provider billing private opener identity drifted")
        if any(
            current is not wanted
            for current, wanted in zip(current_handlers, expected_handlers)
        ):
            raise error_cls("provider billing private opener handlers drifted")
        if len(current_dispatch) != len(expected_dispatch):
            raise error_cls("provider billing private opener dispatch drifted")
        for current, wanted in zip(current_dispatch, expected_dispatch):
            if current[0] != wanted[0] or current[1] != wanted[1]:
                raise error_cls("provider billing private opener dispatch drifted")
            if len(current[2]) != len(wanted[2]) or any(
                handler is not expected_handler
                for handler, expected_handler in zip(current[2], wanted[2])
            ):
                raise error_cls("provider billing private opener dispatch drifted")

    def projection(source: object) -> tuple[object, ...]:
        entitlement = get_attr(source, "entitlement")
        statement = get_attr(source, "statement")
        return (
            id(entitlement),
            id(statement),
            get_attr(source, "observed_at"),
            get_attr(source, "evidence_sha256"),
        )

    def validate_structure(source: object) -> None:
        try:
            source_post_init(source)
        except base_error_cls as exc:
            raise error_cls(
                "provider billing observation failed canonical validation"
            ) from exc

    def register(source: object):
        if type(source) is not source_cls:
            raise error_cls(
                "canonical provider billing read returned unexpected observation type"
            )
        validate_structure(source)
        issued[id(source)] = (source, projection(source))
        return source

    def read(
        credentials: BetfairSessionCredentials,
        *,
        timeout_seconds: float = 10.0,
        from_record: int = 0,
        record_count: int = 100,
        statement_from: str | None = None,
        statement_to: str | None = None,
    ):
        """Run one product-owned provider read and register its exact result."""

        if type(credentials) is not credentials_cls:
            raise TypeError("credentials must be exact BetfairSessionCredentials")
        assert_static_executable_authority()

        def product_clock():
            return now_utc(utc)

        transport = object_new(transport_cls)
        transport_init(transport)
        transport_origin = transport_snapshot(transport)

        client = object_new(client_cls)
        client_init(
            client,
            credentials,
            transport=transport,
            timeout_seconds=timeout_seconds,
            clock=product_clock,
            venue_id="betfair",
            account_id="provider-billing-product",
        )
        if type(client) is not client_cls:
            raise error_cls("provider billing production client is not canonical")
        state = vars(client)
        if (
            state.get("_credentials") is not credentials
            or state.get("_transport") is not transport
            or state.get("_clock") is not product_clock
            or state.get("_venue_id") != "betfair"
            or state.get("_account_id") != "provider-billing-product"
            or state.get("_timeout_seconds") != float(timeout_seconds)
        ):
            raise error_cls("provider billing production client state drifted")

        assert_transport_unchanged(transport, transport_origin)
        source = read_impl(
            client,
            from_record=from_record,
            record_count=record_count,
            statement_from=statement_from,
            statement_to=statement_to,
        )
        # Persistent executable or private-opener mutation during provider I/O
        # cannot be legitimized merely because returned JSON is structurally valid.
        assert_transport_unchanged(transport, transport_origin)
        return register(source)

    def validate(source: object):
        """Return only an exact, untampered observation issued by ``read`` above."""

        if type(source) is not source_cls:
            raise TypeError(
                "source must be exact BetfairProviderBillingInputsObservation"
            )
        validate_structure(source)
        registered = issued.get(id(source))
        if registered is None or registered[0] is not source:
            raise error_cls(
                "provider billing observation must be issued by canonical provider read"
            )
        if projection(source) != registered[1]:
            raise error_cls(
                "provider billing observation no longer matches issued identity"
            )
        return source

    return read, validate


(
    read_verified_betfair_provider_billing_inputs,
    validate_betfair_provider_billing_inputs_observation,
) = _build_observation_authority()
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
from hashlib import sha256
import hmac
from secrets import token_bytes
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

    # Capture known production executable identities before any provider read as
    # defense in depth. The public lower-level client intentionally remains
    # injectable for deterministic adapters/tests; the supported positive API does
    # not accept that injection. These checks are not an arbitrary-code sandbox.
    client_init = client_cls.__dict__["__init__"]
    client_new = client_cls.__dict__.get("__new__")
    transport_init = transport_cls.__dict__["__init__"]
    transport_new = transport_cls.__dict__.get("__new__")
    transport_post = transport_cls.__dict__["post"]
    readonly_client_export = _readonly.__dict__["BetfairReadOnlyClient"]
    readonly_transport_export = _readonly.__dict__["UrllibBetfairHttpTransport"]
    request_ctor = _readonly.__dict__["Request"]
    network_open = _readonly.__dict__["urlopen"]
    stdlib_opener_cls = _urllib_request.__dict__["OpenerDirector"]
    stdlib_opener_open = stdlib_opener_cls.__dict__["open"]
    stdlib_build_opener = _urllib_request.__dict__["build_opener"]
    stdlib_https_handler_cls = _urllib_request.__dict__["HTTPSHandler"]
    stdlib_https_handler_open = stdlib_https_handler_cls.__dict__["https_open"]
    stdlib_abstract_http_handler_cls = _urllib_request.__dict__["AbstractHTTPHandler"]
    stdlib_abstract_http_do_open = stdlib_abstract_http_handler_cls.__dict__["do_open"]

    # ``urllib.request.urlopen`` otherwise resolves the mutable module-global
    # ``_opener`` at call time. Own that exact dispatch root up front so public
    # ``install_opener(...)`` cannot redirect positive provider issuance while the
    # captured ``urlopen``/class/method identities remain unchanged. The object is
    # closure-retained and identity-fenced before and after every provider read.
    product_opener = stdlib_build_opener()
    if type(product_opener) is not stdlib_opener_cls or "open" in vars(product_opener):
        raise BetfairProviderBillingInputsAuthorityError(
            "provider billing canonical network opener is invalid"
        )

    # OpenerDirector stores handler objects and resolves their protocol methods
    # dynamically. Owning only the opener object is therefore insufficient:
    # rebinding HTTPSHandler.https_open (or swapping the opener handler dispatch
    # tables) would otherwise gain execution while _opener identity stayed
    # unchanged. Seal the exact HTTPS handler executable and dispatch graph.
    product_handlers = tuple(product_opener.handlers)
    product_https_handlers = tuple(
        handler
        for handler in product_handlers
        if type(handler) is stdlib_https_handler_cls
    )
    if len(product_https_handlers) != 1:
        raise BetfairProviderBillingInputsAuthorityError(
            "provider billing canonical HTTPS handler is invalid"
        )
    product_https_handler = product_https_handlers[0]
    if "https_open" in vars(product_https_handler):
        raise BetfairProviderBillingInputsAuthorityError(
            "provider billing canonical HTTPS handler is shadowed"
        )
    product_handler_ids = tuple(id(handler) for handler in product_handlers)
    product_https_dispatch = tuple(
        (getattr(handler, "handler_order", None), id(handler))
        for handler in product_opener.handle_open.get("https", ())
    )
    if not any(
        handler_id == id(product_https_handler)
        for _order, handler_id in product_https_dispatch
    ):
        raise BetfairProviderBillingInputsAuthorityError(
            "provider billing canonical HTTPS dispatch is invalid"
        )

    _urllib_request.__dict__["_opener"] = product_opener

    now_utc = datetime.now
    utc = timezone.utc
    base_error_cls = BetfairReadOnlyError
    error_cls = BetfairProviderBillingInputsAuthorityError
    get_attr = object.__getattribute__
    object_new = object.__new__
    hmac_new = hmac.new
    compare_digest = hmac.compare_digest
    hash_ctor = sha256
    session_binding_key = token_bytes(32)

    # Strongly retaining the issued observation prevents id reuse while its issuance
    # is authoritative.  The third tuple element is a process-local HMAC binding of
    # the authenticated credential values; raw credentials are deliberately not
    # retained by the issuance registry or exported into evidence.
    issued: dict[int, tuple[object, tuple[object, ...], bytes]] = {}
    traversals: dict[int, tuple[object, bytes]] = {}

    def assert_executable_authority() -> None:
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
        if _readonly.__dict__.get("urlopen") is not network_open:
            raise error_cls("provider billing network opener drifted")
        if (
            _urllib_request.__dict__.get("OpenerDirector") is not stdlib_opener_cls
            or stdlib_opener_cls.__dict__.get("open") is not stdlib_opener_open
        ):
            raise error_cls("provider billing lower network opener drifted")
        if (
            _urllib_request.__dict__.get("HTTPSHandler")
            is not stdlib_https_handler_cls
            or stdlib_https_handler_cls.__dict__.get("https_open")
            is not stdlib_https_handler_open
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
            _urllib_request.__dict__.get("_opener") is not product_opener
            or type(product_opener) is not stdlib_opener_cls
            or "open" in vars(product_opener)
        ):
            raise error_cls("provider billing installed network opener drifted")
        if (
            tuple(id(handler) for handler in product_opener.handlers)
            != product_handler_ids
            or tuple(
                (getattr(handler, "handler_order", None), id(handler))
                for handler in product_opener.handle_open.get("https", ())
            )
            != product_https_dispatch
            or type(product_https_handler) is not stdlib_https_handler_cls
            or "https_open" in vars(product_https_handler)
        ):
            raise error_cls("provider billing HTTPS handler dispatch drifted")

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

    def session_binding(credentials: BetfairSessionCredentials) -> bytes:
        if type(credentials) is not credentials_cls:
            raise error_cls(
                "provider billing session capability must be exact canonical credentials"
            )
        application_key = credentials.application_key.encode("utf-8")
        session_token = credentials.session_token.encode("utf-8")
        payload = (
            b"autosport.betfair.provider-billing-session-v1\x00"
            + len(application_key).to_bytes(8, "big")
            + application_key
            + len(session_token).to_bytes(8, "big")
            + session_token
        )
        return hmac_new(session_binding_key, payload, hash_ctor).digest()

    def register(source: object, binding: bytes):
        if type(source) is not source_cls:
            raise error_cls(
                "canonical provider billing read returned unexpected observation type"
            )
        if type(binding) is not bytes or len(binding) != hash_ctor().digest_size:
            raise error_cls("provider billing session binding is invalid")
        # Re-run the closure-backed canonical structural/digest validator before the
        # observation enters the private issuance relation.  Only an authority-keyed
        # opaque session binding is retained; raw credentials/session tokens are not
        # stored in the registry and the binding never enters durable evidence.
        validate_structure(source)
        issued[id(source)] = (
            source,
            projection(source),
            binding,
        )
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
        """Run one product-owned provider read and register its exact result.

        Positive issuance deliberately does not accept a caller-created
        ``BetfairReadOnlyClient``. That client supports transport/clock injection for
        lower-level deterministic testing, so accepting it here would let a caller
        turn synthetic JSON and caller time into positive provider provenance.
        """

        if type(credentials) is not credentials_cls:
            raise TypeError("credentials must be exact BetfairSessionCredentials")
        assert_executable_authority()

        def product_clock():
            return now_utc(utc)

        # Bypass mutable class-call dispatch and invoke the captured canonical
        # constructors directly. The class dictionaries are also fenced above, so
        # a caller cannot replace the public constructors and have that replacement
        # execute on this authority path.
        transport = object_new(transport_cls)
        transport_init(transport)
        if type(transport) is not transport_cls or "post" in vars(transport):
            raise error_cls("provider billing production transport is not canonical")

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

        source = read_impl(
            client,
            from_record=from_record,
            record_count=record_count,
            statement_from=statement_from,
            statement_to=statement_to,
        )
        # A persistent executable/global-opener rebind that occurs during provider
        # I/O cannot be legitimized merely because the returned JSON is valid.
        assert_executable_authority()
        return register(source, session_binding(credentials))

    def validate(source: object):
        """Return only an exact, untampered observation issued by ``read`` above."""

        if type(source) is not source_cls:
            raise TypeError(
                "source must be exact BetfairProviderBillingInputsObservation"
            )
        # Structural/digest validation is repeated at every authority use so an
        # issued object cannot be mutated and still rely on its original registry
        # entry. The registry projection additionally prevents recomputed-field
        # tampering from replacing the exact issued identity.
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

    def read_traversal(
        credentials: BetfairSessionCredentials,
        *,
        record_count: int = 100,
        statement_from: str | None = None,
        statement_to: str | None = None,
        max_pages: int = 1000,
    ):
        """Acquire one bounded product-owned account-statement pagination sweep."""

        if type(credentials) is not credentials_cls:
            raise TypeError("credentials must be exact BetfairSessionCredentials")
        if (
            isinstance(max_pages, bool)
            or not isinstance(max_pages, int)
            or max_pages <= 0
            or max_pages > 1000
        ):
            raise ValueError("max_pages must be an integer from 1 through 1000")

        binding = session_binding(credentials)
        pages: list[object] = []
        from_record = 0
        for _page_index in range(max_pages):
            source = read(
                credentials,
                from_record=from_record,
                record_count=record_count,
                statement_from=statement_from,
                statement_to=statement_to,
            )
            registered = issued[id(source)]
            if not compare_digest(registered[2], binding):
                raise error_cls(
                    "provider billing authenticated session changed during traversal"
                )
            pages.append(source)
            statement = get_attr(source, "statement")
            more_available = get_attr(statement, "more_available")
            items = get_attr(statement, "items")
            if not more_available:
                result = tuple(pages)
                traversals[id(result)] = (result, binding)
                return result
            item_count = len(items)
            if item_count <= 0:
                raise error_cls(
                    "provider billing traversal cannot progress from an empty "
                    "non-terminal page"
                )
            from_record += item_count

        raise error_cls(
            "provider billing traversal did not reach a terminal page "
            "within max_pages"
        )

    def validate_traversal(pages: object):
        """Validate one exact product-owned authenticated pagination sweep.

        The tuple itself must have been issued by the canonical traversal reader.
        Therefore a consumer cannot mint positive traversal evidence by splicing
        separately issued pages, even when those pages used the same authenticated
        session. This remains weaker than a stable cross-session account identity.
        """

        if type(pages) is not tuple or not pages:
            raise TypeError("pages must be a non-empty exact tuple")
        traversal = traversals.get(id(pages))
        if traversal is None or traversal[0] is not pages:
            raise error_cls(
                "provider billing traversal must be issued by canonical "
                "pagination acquisition"
            )

        traversal_binding = traversal[1]
        for source in pages:
            current = validate(source)
            registered = issued[id(current)]
            if not compare_digest(registered[2], traversal_binding):
                raise error_cls(
                    "provider billing traversal pages must share one "
                    "authenticated session capability"
                )
        return pages

    return read, validate, read_traversal, validate_traversal


(
    read_verified_betfair_provider_billing_inputs,
    validate_betfair_provider_billing_inputs_observation,
    read_verified_betfair_provider_billing_inputs_traversal,
    validate_betfair_provider_billing_inputs_traversal,
) = _build_observation_authority()

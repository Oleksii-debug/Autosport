"""Product-owned issuance verification for Betfair provider billing observations.

The descriptive DTOs in :mod:`betfair_provider_billing_inputs` deliberately remain
serializable/inspectable data. Their public constructors and deterministic digests
are therefore not provenance. Positive consumers must use this module's canonical
read wrapper and validator: the wrapper owns construction of the existing strict
Betfair read-only client with the canonical HTTP transport and a closure-captured
UTC clock, then records the exact returned observation in a closure-private
issuance relation. Callers can provide credentials and bounded read scope, but
cannot inject a transport, clock, venue/account label, pre-built client, or rebound
provider executable into positive provider-billing issuance.

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


class BetfairProviderBillingInputsAuthorityError(BetfairReadOnlyError):
    """Raised when a provider-billing observation lacks canonical read issuance."""


def _build_observation_authority():
    read_impl = _inputs.__dict__["read_betfair_provider_billing_inputs"]
    source_cls = _inputs.__dict__["BetfairProviderBillingInputsObservation"]
    source_post_init = source_cls.__dict__["__post_init__"]
    client_cls = BetfairReadOnlyClient
    credentials_cls = BetfairSessionCredentials
    transport_cls = UrllibBetfairHttpTransport

    # Capture the production executable identities before any provider read. The
    # public lower-level client intentionally remains injectable for ordinary
    # deterministic adapters/tests; this positive issuance boundary does not.
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
    _urllib_request.__dict__["_opener"] = product_opener

    now_utc = datetime.now
    utc = timezone.utc
    base_error_cls = BetfairReadOnlyError
    error_cls = BetfairProviderBillingInputsAuthorityError
    get_attr = object.__getattribute__
    object_new = object.__new__

    # Strongly retaining the issued object prevents id reuse while its issuance is
    # authoritative. The stored projection detects object.__setattr__ tampering.
    issued: dict[int, tuple[object, tuple[object, ...]]] = {}

    def assert_executable_authority() -> None:
        """Reject same-process rebinding of every executable root used for I/O."""

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
            _urllib_request.__dict__.get("_opener") is not product_opener
            or type(product_opener) is not stdlib_opener_cls
            or "open" in vars(product_opener)
        ):
            raise error_cls("provider billing installed network opener drifted")

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
        # Re-run the closure-backed canonical structural/digest validator before the
        # observation enters the private issuance relation.
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
        return register(source)

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

    return read, validate


(
    read_verified_betfair_provider_billing_inputs,
    validate_betfair_provider_billing_inputs_observation,
) = _build_observation_authority()

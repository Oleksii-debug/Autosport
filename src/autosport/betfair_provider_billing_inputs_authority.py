"""Product-owned issuance verification for Betfair provider billing observations.

The descriptive DTOs in :mod:`betfair_provider_billing_inputs` deliberately remain
serializable/inspectable data. Their public constructors and deterministic digests
are therefore not provenance. Positive consumers must use this module's canonical
read wrapper and validator: the wrapper owns construction of the existing strict
Betfair read-only client with the canonical HTTP transport and a closure-captured
UTC clock, then records the exact returned observation in a closure-private
issuance relation. Callers can provide credentials and bounded read scope, but
cannot inject a transport, clock, venue/account label, or pre-built client into
positive provider-billing issuance.

This is an in-process observation capability, not a second provider client, billing
store, economic classifier, allocation authority, or durable cost record.
"""
from __future__ import annotations

from datetime import datetime, timezone

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
    transport_post = transport_cls.__dict__["post"]
    now_utc = datetime.now
    utc = timezone.utc
    base_error_cls = BetfairReadOnlyError
    error_cls = BetfairProviderBillingInputsAuthorityError
    get_attr = object.__getattribute__

    # Strongly retaining the issued object prevents id reuse while its issuance is
    # authoritative. The stored projection detects object.__setattr__ tampering.
    issued: dict[int, tuple[object, tuple[object, ...]]] = {}

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

        def product_clock():
            return now_utc(utc)

        transport = transport_cls()
        # Keep the production transport exact and unshadowed. This protects the
        # positive wrapper from a caller-supplied structural transport even if a
        # pre-built client is available elsewhere in the application.
        if type(transport) is not transport_cls or "post" in vars(transport):
            raise error_cls("provider billing production transport is not canonical")
        if transport_cls.__dict__.get("post") is not transport_post:
            raise error_cls("provider billing production transport method drifted")

        client = client_cls(
            credentials,
            transport=transport,
            timeout_seconds=timeout_seconds,
            clock=product_clock,
            venue_id="betfair",
            account_id="provider-billing-product",
        )
        source = read_impl(
            client,
            from_record=from_record,
            record_count=record_count,
            statement_from=statement_from,
            statement_to=statement_to,
        )
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

from __future__ import annotations

from . import betdaq_account_readonly as _account_readonly
from .betdaq_account_readonly import (
    BetdaqAccountReadOnlyError,
    BetdaqCredentials,
    BetdaqSoapTransport,
    UrllibBetdaqSoapTransport,
)
from .betdaq_readonly_provider import (
    BetdaqGetPricesRequest,
    BetdaqTransientTransportError,
)
from .betdaq_readonly_request_wire import build_get_prices_soap11_request


class BetdaqReadOnlyLiveTransport:
    """Thin GetPrices bridge over Autosport's existing BETDAQ HTTPS transport.

    This object owns no retry, rate, entitlement, account or write authority. It only
    serializes one GetPrices call with the canonical credential bundle and delegates
    one HTTPS POST to the already product-owned transport.
    """

    __slots__ = ("_credentials", "_transport")

    def __init__(
        self,
        *,
        credentials: BetdaqCredentials,
        transport: BetdaqSoapTransport | None = None,
    ) -> None:
        if type(credentials) is not BetdaqCredentials:
            raise TypeError("credentials must be canonical BetdaqCredentials")
        selected: object = UrllibBetdaqSoapTransport() if transport is None else transport
        if not callable(getattr(selected, "post", None)):
            raise TypeError("transport must expose post")
        self._credentials = credentials
        self._transport = selected

    @property
    def provider_origin_verified(self) -> bool:
        """True only for the product-owned canonical HTTPS implementation."""

        try:
            _account_readonly._require_canonical_account_transport(self._transport)
        except BetdaqAccountReadOnlyError:
            return False
        return True

    def get_prices(
        self,
        request: BetdaqGetPricesRequest,
        *,
        timeout_seconds: float,
    ) -> bytes:
        if type(request) is not BetdaqGetPricesRequest:
            raise TypeError("request must be BetdaqGetPricesRequest")
        wire = build_get_prices_soap11_request(self._credentials, request)
        try:
            payload = self._transport.post(
                wire.endpoint,
                headers=wire.headers,
                body=wire.body,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            # Never expose arbitrary transport/provider diagnostics because a custom
            # transport could include credential-bearing request material in them.
            raise BetdaqTransientTransportError(
                "BETDAQ GetPrices transport failed"
            ) from None
        if type(payload) is not bytes:
            raise BetdaqTransientTransportError(
                "BETDAQ GetPrices transport returned non-bytes payload"
            )
        return payload

    def __repr__(self) -> str:
        return (
            "BetdaqReadOnlyLiveTransport("
            f"provider_origin_verified={self.provider_origin_verified})"
        )


def canonical_get_prices_origin_verified(transport: object) -> bool:
    """Package-private provenance check resistant to lookalike/subclass transports."""

    return (
        type(transport) is BetdaqReadOnlyLiveTransport
        and transport.provider_origin_verified
    )

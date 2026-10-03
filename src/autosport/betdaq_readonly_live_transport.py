from __future__ import annotations

from .betdaq_account_readonly import (
    BetdaqAccountReadOnlyError,
    BetdaqCredentials,
    BetdaqSoapTransport,
    UrllibBetdaqSoapTransport,
)
from .betdaq_event_tree_request_wire import (
    BetdaqEventSubTreeRequest,
    build_get_event_subtree_no_selections_soap11_request,
)
from .betdaq_readonly_provider import (
    BetdaqGetPricesRequest,
    BetdaqTransientTransportError,
)
from .betdaq_readonly_request_wire import build_get_prices_soap11_request
from .betdaq_rate_governor import (
    BetdaqRateAdmission,
    BetdaqRateGovernor,
    admit_betdaq_rate_request,
)
from .providers import ProviderUnavailableError

_CANONICAL_POST = UrllibBetdaqSoapTransport.post


class BetdaqReadOnlyLiveTransport:
    """Read-only BETDAQ bridge over Autosport's existing HTTPS transport.

    This object owns no retry policy, entitlement, account, origin-proof or write
    authority. It consumes one admission from the product-owned shared BETDAQ rate
    governor immediately before each HTTPS POST, so every actual retry is separately
    budgeted without creating a second scheduler or rate-policy stack.

    Retryability is evidence, not a default. The current canonical #1610 transport
    deliberately collapses HTTP/network failure classes to BetdaqAccountReadOnlyError,
    so that opaque error is non-retryable here. Only a transport that preserves an
    explicit transient signal may enter BetdaqReadOnlyProvider's bounded retry loop.
    """

    __slots__ = (
        "_credentials",
        "_transport",
        "_rate_governor",
        "_last_rate_admission",
        "_canonical_only",
    )

    def __setattr__(self, name: str, value: object) -> None:
        # Authority-bearing composition references are immutable after construction.
        # The one intentionally changing observation slot is updated internally via
        # object.__setattr__, so normal caller assignment cannot forge evidence.
        if hasattr(self, name):
            raise AttributeError(
                "BETDAQ live transport composition is immutable after construction"
            )
        object.__setattr__(self, name, value)

    def __init__(
        self,
        *,
        credentials: BetdaqCredentials,
        rate_governor: BetdaqRateGovernor,
        transport: BetdaqSoapTransport | None = None,
        canonical_only: bool = False,
    ) -> None:
        if type(credentials) is not BetdaqCredentials:
            raise TypeError("credentials must be canonical BetdaqCredentials")
        if type(rate_governor) is not BetdaqRateGovernor:
            raise TypeError("rate_governor must be canonical BetdaqRateGovernor")
        if type(canonical_only) is not bool:
            raise TypeError("canonical_only must be bool")
        selected: object = UrllibBetdaqSoapTransport() if transport is None else transport
        if not callable(getattr(selected, "post", None)):
            raise TypeError("transport must expose post")
        self._credentials = credentials
        self._transport = selected
        self._rate_governor = rate_governor
        self._last_rate_admission: BetdaqRateAdmission | None = None
        self._canonical_only = canonical_only
        if self._canonical_only and not self.canonical_transport_selected:
            raise TypeError(
                "canonical-only BETDAQ transport requires product-owned HTTPS transport"
            )

    @property
    def rate_governor(self) -> BetdaqRateGovernor:
        return self._rate_governor

    @property
    def last_rate_admission(self) -> BetdaqRateAdmission | None:
        return self._last_rate_admission

    @property
    def canonical_transport_selected(self) -> bool:
        """Structural composition fact only; explicitly not provider-origin proof."""

        if type(self._transport) is not UrllibBetdaqSoapTransport:
            return False
        bound_post = getattr(self._transport, "post", None)
        return (
            getattr(bound_post, "__self__", None) is self._transport
            and getattr(bound_post, "__func__", None) is _CANONICAL_POST
        )

    def _post_readonly(
        self,
        wire: object,
        *,
        method: str,
        timeout_seconds: float,
    ) -> bytes:
        # Validate exact dispatch authority before consuming provider budget. In
        # canonical-only mode the captured class method is invoked unbound, so an
        # instance-level post shadow cannot redirect canonical acquisition.
        if self._canonical_only and not self.canonical_transport_selected:
            raise ProviderUnavailableError(
                "canonical BETDAQ HTTPS transport authority was replaced"
            )
        object.__setattr__(
            self,
            "_last_rate_admission",
            admit_betdaq_rate_request(self._rate_governor, method),
        )
        try:
            if self._canonical_only:
                payload = _CANONICAL_POST(
                    self._transport,
                    getattr(wire, "endpoint"),
                    headers=getattr(wire, "headers"),
                    body=getattr(wire, "body"),
                    timeout_seconds=timeout_seconds,
                )
            else:
                payload = self._transport.post(
                    getattr(wire, "endpoint"),
                    headers=getattr(wire, "headers"),
                    body=getattr(wire, "body"),
                    timeout_seconds=timeout_seconds,
                )
        except (BetdaqTransientTransportError, TimeoutError, ConnectionError):
            raise
        except BetdaqAccountReadOnlyError:
            raise ProviderUnavailableError(
                f"BETDAQ {method} transport failed without retryable classification"
            ) from None
        except Exception:
            raise ProviderUnavailableError(
                f"BETDAQ {method} transport failed without retryable classification"
            ) from None
        if type(payload) is not bytes:
            raise ProviderUnavailableError(
                f"BETDAQ {method} transport returned non-bytes payload"
            )
        return payload

    def get_prices(
        self,
        request: BetdaqGetPricesRequest,
        *,
        timeout_seconds: float,
    ) -> bytes:
        if type(request) is not BetdaqGetPricesRequest:
            raise TypeError("request must be BetdaqGetPricesRequest")
        wire = build_get_prices_soap11_request(self._credentials, request)
        # Serialize/validate first so malformed local input cannot burn provider budget.
        return self._post_readonly(
            wire,
            method="GetPrices",
            timeout_seconds=timeout_seconds,
        )

    def get_event_subtree_no_selections(
        self,
        request: BetdaqEventSubTreeRequest,
        *,
        timeout_seconds: float,
    ) -> bytes:
        if type(request) is not BetdaqEventSubTreeRequest:
            raise TypeError("request must be BetdaqEventSubTreeRequest")
        wire = build_get_event_subtree_no_selections_soap11_request(
            self._credentials,
            request,
        )
        # The same product-owned transport/governor stack is reused; no discovery
        # scheduler or second request-budget authority is introduced.
        return self._post_readonly(
            wire,
            method="GetEventSubTreeNoSelections",
            timeout_seconds=timeout_seconds,
        )

    def __repr__(self) -> str:
        return (
            "BetdaqReadOnlyLiveTransport("
            f"canonical_transport_selected={self.canonical_transport_selected}, "
            f"canonical_only={self._canonical_only}, "
            "rate_governed=True)"
        )

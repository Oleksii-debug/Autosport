from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Sequence

from .betdaq_account_readonly import BetdaqCredentials, BetdaqSoapTransport
from .betdaq_catalogue_binding import (
    BetdaqCatalogueEvidence,
    BetdaqLiveCatalogueResolver,
)
from .betdaq_readonly_live_transport import BetdaqReadOnlyLiveTransport
from .betdaq_readonly_market_wire import BetdaqSoapProtocolError
from .betdaq_rate_governor import BetdaqRateGovernor
from .betdaq_readonly_provider import (
    BetdaqMarketBinding,
    BetdaqReadOnlyProvider,
    BetdaqResolvedMarketBinding,
    Clock,
    RequestIdFactory,
    utc_now_iso,
)


class BetdaqLiveReadOnlyProvider(BetdaqReadOnlyProvider):
    """GetPrices composition over Autosport's existing BETDAQ HTTPS stack.

    This class makes the live network path usable without creating another credential
    or HTTP architecture. It deliberately does *not* promote provider-origin truth:
    current-main transport hardening for redirect/proxy/response-bound semantics is a
    separate live lineage, so the inherited provider keeps UNVERIFIED_PROVIDER_ORIGIN
    until that authority is integrated and explicitly composed.
    """

    def __init__(
        self,
        *,
        credentials: BetdaqCredentials,
        market_bindings: Sequence[BetdaqMarketBinding],
        threshold_amount: Decimal,
        rate_governor: BetdaqRateGovernor,
        transport: BetdaqSoapTransport | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
        max_message_age_seconds: int | None = None,
        clock: Clock = utc_now_iso,
        request_id_factory: RequestIdFactory | None = None,
    ) -> None:
        # ThresholdAmount is economic request content and participates in request
        # identity.  The product live path must not dispatch comparison/as_tuple/
        # formatting through a caller-defined Decimal subclass before the request is
        # fingerprinted or serialized.  Injected base-provider test paths remain
        # explicitly non-authoritative for provider origin; the canonical live
        # composition accepts only the exact built-in Decimal authority type.
        if type(threshold_amount) is not Decimal:
            raise TypeError("threshold_amount must be exact Decimal")
        live_transport = BetdaqReadOnlyLiveTransport(
            credentials=credentials,
            rate_governor=rate_governor,
            transport=transport,
        )
        super().__init__(
            transport=live_transport,
            market_bindings=market_bindings,
            threshold_amount=threshold_amount,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            max_message_age_seconds=max_message_age_seconds,
            clock=clock,
            request_id_factory=request_id_factory,
        )
        self._live_transport = live_transport
        self._catalogue_resolver = BetdaqLiveCatalogueResolver(
            transport=live_transport,
            timeout_seconds=self.timeout_seconds,
            clock=clock,
        )
        self._catalogue_bindings: dict[int, BetdaqResolvedMarketBinding] = {}
        self._last_catalogue_evidence: BetdaqCatalogueEvidence | None = None

    @property
    def live_transport(self) -> BetdaqReadOnlyLiveTransport:
        return self._live_transport

    @property
    def last_catalogue_evidence(self) -> BetdaqCatalogueEvidence | None:
        return self._last_catalogue_evidence

    def _binding_for_market(
        self,
        market: object,
    ) -> BetdaqResolvedMarketBinding:
        market_id = getattr(market, "market_id", None)
        if type(market_id) is not int:
            raise BetdaqSoapProtocolError("BETDAQ market identity is malformed")
        try:
            return self._catalogue_bindings[market_id]
        except KeyError as exc:
            raise BetdaqSoapProtocolError(
                "live BETDAQ market lacks catalogue-bound provider identity"
            ) from exc

    def _load(self) -> None:
        # Live canonicalization first re-resolves provider-native event/market identity
        # from BETDAQ's own event tree. Caller BetdaqMarketBinding values now select
        # requested market/event scope only; their sport/MarketType fields are never
        # allowed to become live canonical semantics.
        resolved, catalogue_evidence = self._catalogue_resolver.resolve(
            tuple(self._bindings.values())
        )
        self._catalogue_bindings = dict(resolved)
        self._last_catalogue_evidence = catalogue_evidence
        try:
            super()._load()
            self._flags = (
                *self._flags,
                "CATALOGUE_BOUND_EVENT_IDENTITY",
                "CANONICAL_SPORT_UNMAPPED",
                "CANONICAL_MARKET_TYPE_UNMAPPED",
            )
            if self._evidence is None:
                raise AssertionError("successful live load must publish request evidence")
            self._evidence = replace(
                self._evidence,
                catalogue_response_sha256=catalogue_evidence.response_sha256,
                catalogue_rate_admission_receipt=(
                    catalogue_evidence.rate_admission_receipt
                ),
                catalogue_event_classifier_ids=(
                    catalogue_evidence.requested_event_classifier_ids
                ),
            )
        finally:
            # Never leave a stale provider-tree mapping available for a later load.
            self._catalogue_bindings = {}

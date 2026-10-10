from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import hashlib
from typing import Sequence

from .betdaq_account_readonly import BetdaqCredentials, BetdaqSoapTransport
from .betdaq_catalogue_binding import (
    BetdaqCatalogueEvidence,
    BetdaqLiveCatalogueResolver,
    betdaq_event_scope_id,
)
from .betdaq_readonly_live_transport import BetdaqReadOnlyLiveTransport
from .betdaq_readonly_market_wire import BetdaqSoapProtocolError
from .betdaq_rate_governor import BetdaqRateAdmission, BetdaqRateGovernor
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
        bindings = tuple(market_bindings)
        for binding in bindings:
            if type(binding) is BetdaqMarketBinding:
                betdaq_event_scope_id(binding.provider_event_id)
        live_transport = BetdaqReadOnlyLiveTransport(
            credentials=credentials,
            rate_governor=rate_governor,
            transport=transport,
            canonical_only=True,
        )
        super().__init__(
            transport=live_transport,
            market_bindings=bindings,
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
            max_attempts=max_attempts,
            max_message_age_seconds=max_message_age_seconds,
        )
        self._catalogue_bindings: dict[int, BetdaqResolvedMarketBinding] = {}
        self._last_catalogue_evidence: BetdaqCatalogueEvidence | None = None

    @property
    def live_transport(self) -> BetdaqReadOnlyLiveTransport:
        return self._live_transport

    @property
    def last_catalogue_evidence(self) -> BetdaqCatalogueEvidence | None:
        return self._last_catalogue_evidence

    def _rate_admission_receipt(self) -> str | None:
        # Capture exactly once: last_rate_admission is an observation slot shared by
        # both read-only operations. Validation and receipt extraction must bind to
        # the same object rather than performing a second mutable-slot read.
        admission = self._live_transport.last_rate_admission
        if admission is None:
            return None
        if type(admission) is not BetdaqRateAdmission:
            raise TypeError(
                "live transport last_rate_admission must be canonical BetdaqRateAdmission"
            )
        if admission.method != "GetPrices":
            raise ValueError(
                "live BETDAQ GetPrices acquisition bound wrong rate admission"
            )
        return admission.receipt_sha256

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
        previous_publication = (
            self._pending,
            self._offset,
            self._cursor,
            self._flags,
            self._evidence,
            self._last_catalogue_evidence,
        )
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
            aggregate = hashlib.sha256()
            aggregate.update(bytes.fromhex(self._evidence.aggregate_sha256))
            aggregate.update(bytes.fromhex(catalogue_evidence.request_fingerprint))
            aggregate.update(bytes.fromhex(catalogue_evidence.response_sha256))
            combined_evidence = replace(
                self._evidence,
                aggregate_sha256=aggregate.hexdigest(),
                catalogue_request_fingerprint=(
                    catalogue_evidence.request_fingerprint
                ),
                catalogue_response_sha256=catalogue_evidence.response_sha256,
                catalogue_rate_admission_receipts=(
                    catalogue_evidence.rate_admission_receipts
                ),
                catalogue_event_classifier_ids=(
                    catalogue_evidence.requested_event_classifier_ids
                ),
            )
            # Publish the request/catalogue evidence pair only after the full live
            # snapshot succeeds. A failed successor acquisition must not splice a
            # fresh catalogue witness onto an older successful GetPrices witness.
            self._evidence = combined_evidence
            self._last_catalogue_evidence = catalogue_evidence
        except Exception:
            (
                self._pending,
                self._offset,
                self._cursor,
                self._flags,
                self._evidence,
                self._last_catalogue_evidence,
            ) = previous_publication
            raise
        finally:
            # Never leave a stale provider-tree mapping available for a later load.
            self._catalogue_bindings = {}

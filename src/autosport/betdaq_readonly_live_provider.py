from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Sequence

from .betdaq_account_readonly import BetdaqCredentials, BetdaqSoapTransport
from .betdaq_readonly_live_transport import BetdaqReadOnlyLiveTransport
from .betdaq_readonly_provider import (
    BetdaqMarketBinding,
    BetdaqReadOnlyProvider,
    Clock,
    RequestIdFactory,
    utc_now_iso,
)


class BetdaqLiveReadOnlyProvider(BetdaqReadOnlyProvider):
    """Product-owned live GetPrices composition over the canonical BETDAQ HTTPS stack.

    Provider-origin authority is promoted only after the inherited provider has
    successfully parsed and validated the complete snapshot and only when the bridge
    is backed by the product-owned canonical HTTPS transport.
    """

    def __init__(
        self,
        *,
        credentials: BetdaqCredentials,
        market_bindings: Sequence[BetdaqMarketBinding],
        threshold_amount: Decimal,
        transport: BetdaqSoapTransport | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
        max_message_age_seconds: int | None = None,
        clock: Clock = utc_now_iso,
        request_id_factory: RequestIdFactory | None = None,
    ) -> None:
        live_transport = BetdaqReadOnlyLiveTransport(
            credentials=credentials,
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

    @property
    def live_transport(self) -> BetdaqReadOnlyLiveTransport:
        return self._live_transport

    def _load(self) -> None:
        super()._load()
        if not self._live_transport.provider_origin_verified:
            return
        # super()._load() publishes state only after every chunk parsed successfully.
        # Upgrade only the single origin dimension; timestamp freshness, entitlement,
        # write permission and every other truth boundary remain unchanged.
        self._flags = tuple(
            flag for flag in self._flags if flag != "UNVERIFIED_PROVIDER_ORIGIN"
        )
        if self._evidence is not None:
            self._evidence = replace(
                self._evidence,
                provider_origin_verified=True,
            )

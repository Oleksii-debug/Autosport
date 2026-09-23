from __future__ import annotations

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

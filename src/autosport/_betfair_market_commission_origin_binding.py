"""Bind Betfair commission receipts to the exact client origin that acquired them.

The durable commission receipt intentionally contains only secret-free provider
bytes. Its account-details digest is evidence-instance integrity, not a stable
Betfair account discriminator. This process-local guard therefore records the
canonical client object owned by each ``BetfairMarketCommissionAuthority`` and
binds every positively issued receipt to that exact origin at capture/reacquisition
time.

The registry is deliberately not durable: after restart, a receipt regains positive
origin authority only after the source reacquires it through the newly constructed
canonical client. Once construction finishes the authority's client reference is
immutable, so even a transient A->B->A assignment cannot mix account-details and
cleared-order reads from different authenticated origins during one capture.
"""
from __future__ import annotations

from threading import RLock
from weakref import WeakKeyDictionary

from .betfair_account_readonly import BetfairReadOnlyClient
from .betfair_market_commission_authority import (
    BetfairMarketCommissionAuthority,
    BetfairMarketCommissionAuthorityError,
    BetfairMarketCommissionReceipt,
)


_LOCK = RLock()
_SOURCE_ORIGINS: WeakKeyDictionary = WeakKeyDictionary()
_RECEIPT_ORIGINS: WeakKeyDictionary = WeakKeyDictionary()


def _install_origin_binding() -> None:
    raw_init = BetfairMarketCommissionAuthority.__init__
    if getattr(raw_init, "_autosport_receipt_origin_binding", False):
        return
    raw_capture = BetfairMarketCommissionAuthority.capture_market
    raw_setattr = BetfairMarketCommissionAuthority.__setattr__

    def bound_setattr(
        self: BetfairMarketCommissionAuthority,
        name: str,
        value: object,
    ) -> None:
        if name == "_client":
            with _LOCK:
                origin = _SOURCE_ORIGINS.get(self)
            if origin is not None and value is not origin:
                raise BetfairMarketCommissionAuthorityError(
                    "commission authority client origin is immutable after construction"
                )
        raw_setattr(self, name, value)

    def bound_init(self: BetfairMarketCommissionAuthority, *args, **kwargs) -> None:
        raw_init(self, *args, **kwargs)
        client = self._client
        if type(client) is not BetfairReadOnlyClient:
            raise BetfairMarketCommissionAuthorityError(
                "commission authority origin client is not canonical"
            )
        with _LOCK:
            _SOURCE_ORIGINS[self] = client
            _RECEIPT_ORIGINS[self] = {}

    def bound_capture(
        self: BetfairMarketCommissionAuthority,
        market_id: str,
        *,
        settled_from: str | None = None,
        settled_to: str | None = None,
    ) -> BetfairMarketCommissionReceipt:
        with _LOCK:
            origin = _SOURCE_ORIGINS.get(self)
        if type(origin) is not BetfairReadOnlyClient or self._client is not origin:
            raise BetfairMarketCommissionAuthorityError(
                "commission authority client origin changed after construction"
            )
        receipt = raw_capture(
            self,
            market_id,
            settled_from=settled_from,
            settled_to=settled_to,
        )
        if type(receipt) is not BetfairMarketCommissionReceipt:
            raise BetfairMarketCommissionAuthorityError(
                "commission capture returned non-canonical receipt"
            )
        if self._client is not origin:
            raise BetfairMarketCommissionAuthorityError(
                "commission authority client origin changed during capture"
            )
        with _LOCK:
            bindings = _RECEIPT_ORIGINS.get(self)
            if bindings is None:
                raise BetfairMarketCommissionAuthorityError(
                    "commission source origin registry is unavailable"
                )
            previous = bindings.get(receipt.receipt_id)
            if previous is not None and previous is not origin:
                raise BetfairMarketCommissionAuthorityError(
                    "commission receipt has conflicting client origins"
                )
            bindings[receipt.receipt_id] = origin
        return receipt

    bound_init._autosport_receipt_origin_binding = True  # type: ignore[attr-defined]
    bound_init._autosport_receipt_origin_raw_init = raw_init  # type: ignore[attr-defined]
    bound_capture._autosport_receipt_origin_binding = True  # type: ignore[attr-defined]
    bound_setattr._autosport_receipt_origin_binding = True  # type: ignore[attr-defined]
    BetfairMarketCommissionAuthority.__setattr__ = bound_setattr
    BetfairMarketCommissionAuthority.__init__ = bound_init
    BetfairMarketCommissionAuthority.capture_market = bound_capture


def resolve_bound_receipt(
    source: BetfairMarketCommissionAuthority,
    *,
    receipt_id: str,
    record_sha256: str,
    as_of,
) -> tuple[BetfairMarketCommissionReceipt, BetfairReadOnlyClient]:
    """Resolve a receipt together with the immutable current-process client origin."""

    if type(source) is not BetfairMarketCommissionAuthority:
        raise BetfairMarketCommissionAuthorityError(
            "commission source must be exact BetfairMarketCommissionAuthority"
        )
    receipt = source.resolve(
        receipt_id=receipt_id,
        record_sha256=record_sha256,
        as_of=as_of,
    )
    with _LOCK:
        source_origin = _SOURCE_ORIGINS.get(source)
        bindings = _RECEIPT_ORIGINS.get(source)
        receipt_origin = None if bindings is None else bindings.get(receipt.receipt_id)
    if (
        type(source_origin) is not BetfairReadOnlyClient
        or type(receipt_origin) is not BetfairReadOnlyClient
        or receipt_origin is not source_origin
        or source._client is not source_origin
    ):
        raise BetfairMarketCommissionAuthorityError(
            "commission receipt lacks immutable current-process client-origin authority"
        )
    return receipt, receipt_origin


_install_origin_binding()
del _install_origin_binding

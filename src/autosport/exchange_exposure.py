from __future__ import annotations

from decimal import Decimal


_SUPPORTED_EXCHANGE_SIDES = frozenset({"BACK", "LAY"})


def locked_capital_for_exchange_side(
    *,
    stake: Decimal,
    odds: Decimal,
    exchange_side: str,
) -> Decimal:
    """Return exact capital at risk for one exchange order.

    BACK locks the matched/order stake. LAY locks liability: stake * (odds - 1).
    This helper deliberately performs no monetary quantization so callers keep
    exact Decimal economics until their existing accounting boundary.
    """
    if type(stake) is not Decimal:
        raise TypeError("stake must be Decimal")
    if type(odds) is not Decimal:
        raise TypeError("odds must be Decimal")
    if type(exchange_side) is not str:
        raise TypeError("exchange_side must be str")
    if not stake.is_finite() or stake <= 0:
        raise ValueError("stake must be a finite Decimal > 0")
    if not odds.is_finite() or odds <= 1:
        raise ValueError("odds must be a finite Decimal > 1")

    side = exchange_side.strip().upper()
    if side not in _SUPPORTED_EXCHANGE_SIDES:
        raise ValueError("exchange_side must be BACK or LAY")
    if side == "BACK":
        return stake
    return stake * (odds - Decimal("1"))


__all__ = ["locked_capital_for_exchange_side"]

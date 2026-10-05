from __future__ import annotations

from decimal import Decimal


_SUPPORTED_EXCHANGE_SIDES = frozenset({"BACK", "LAY"})


def _coefficient_and_exponent(value: Decimal) -> tuple[int, int]:
    parts = value.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    return coefficient, int(parts.exponent)


def _from_coefficient(coefficient: int, exponent: int) -> Decimal:
    sign = 1 if coefficient < 0 else 0
    digits = tuple(int(ch) for ch in str(abs(coefficient)))
    return Decimal((sign, digits, exponent))


def _subtract_exact(left: Decimal, right: Decimal) -> Decimal:
    left_coefficient, left_exponent = _coefficient_and_exponent(left)
    right_coefficient, right_exponent = _coefficient_and_exponent(right)
    exponent = min(left_exponent, right_exponent)
    left_scaled = left_coefficient * (10 ** (left_exponent - exponent))
    right_scaled = right_coefficient * (10 ** (right_exponent - exponent))
    return _from_coefficient(left_scaled - right_scaled, exponent)


def _multiply_exact(left: Decimal, right: Decimal) -> Decimal:
    left_coefficient, left_exponent = _coefficient_and_exponent(left)
    right_coefficient, right_exponent = _coefficient_and_exponent(right)
    return _from_coefficient(
        left_coefficient * right_coefficient,
        left_exponent + right_exponent,
    )


def locked_capital_for_exchange_side(
    *,
    stake: Decimal,
    odds: Decimal,
    exchange_side: str,
) -> Decimal:
    """Return exact capital at risk for one exchange order.

    BACK locks the matched/order stake. LAY locks liability: stake * (odds - 1).
    Arithmetic is coefficient/exponent based so ambient Decimal precision cannot
    round the liability before the caller's explicit accounting boundary.
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
    return _multiply_exact(stake, _subtract_exact(odds, Decimal("1")))


__all__ = ["locked_capital_for_exchange_side"]

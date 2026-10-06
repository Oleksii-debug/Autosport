from __future__ import annotations

from decimal import Decimal
from types import FunctionType

from .real_execution_ledger import _validate_decimal_text_resource_bound


_SUPPORTED_EXCHANGE_SIDES = frozenset({"BACK", "LAY", "back", "lay"})


def _coefficient_and_exponent(value: Decimal) -> tuple[int, int]:
    parts = value.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    return coefficient, int(parts.exponent)


def _from_coefficient(
    coefficient: int,
    exponent: int,
    _decimal_type=Decimal,
) -> Decimal:
    # Decimal(int) is exact and bypasses Python's process-global int-to-string
    # digit limit; the product-owned resource validator remains authoritative.
    coefficient_parts = _decimal_type(coefficient).as_tuple()
    return _decimal_type(
        (coefficient_parts.sign, coefficient_parts.digits, exponent)
    )


def _subtract_exact(
    left: Decimal,
    right: Decimal,
    _split=_coefficient_and_exponent,
    _build=_from_coefficient,
) -> Decimal:
    left_coefficient, left_exponent = _split(left)
    right_coefficient, right_exponent = _split(right)
    exponent = min(left_exponent, right_exponent)
    left_scaled = left_coefficient * (10 ** (left_exponent - exponent))
    right_scaled = right_coefficient * (10 ** (right_exponent - exponent))
    return _build(left_scaled - right_scaled, exponent)


def _multiply_exact(
    left: Decimal,
    right: Decimal,
    _split=_coefficient_and_exponent,
    _build=_from_coefficient,
) -> Decimal:
    left_coefficient, left_exponent = _split(left)
    right_coefficient, right_exponent = _split(right)
    return _build(
        left_coefficient * right_coefficient,
        left_exponent + right_exponent,
    )


_RESOURCE_VALIDATOR = _validate_decimal_text_resource_bound
_RESOURCE_VALIDATOR_CODE = _RESOURCE_VALIDATOR.__code__
_RESOURCE_VALIDATOR_DEFAULTS = _RESOURCE_VALIDATOR.__defaults__
_RESOURCE_VALIDATOR_KWDEFAULTS = _RESOURCE_VALIDATOR.__kwdefaults__
_RESOURCE_VALIDATOR_CLOSURE = _RESOURCE_VALIDATOR.__closure__
_RESOURCE_VALIDATOR_GLOBALS = _RESOURCE_VALIDATOR.__globals__
_RESOURCE_LIMIT_NAME = "_MAX_EXECUTION_DECIMAL_TEXT_LENGTH"
_RESOURCE_LIMIT = _RESOURCE_VALIDATOR_GLOBALS[_RESOURCE_LIMIT_NAME]


def _require_resource_validator_authority() -> None:
    if (
        _RESOURCE_VALIDATOR.__code__ is not _RESOURCE_VALIDATOR_CODE
        or _RESOURCE_VALIDATOR.__defaults__ is not _RESOURCE_VALIDATOR_DEFAULTS
        or _RESOURCE_VALIDATOR.__kwdefaults__ is not _RESOURCE_VALIDATOR_KWDEFAULTS
        or _RESOURCE_VALIDATOR.__closure__ is not _RESOURCE_VALIDATOR_CLOSURE
        or _RESOURCE_VALIDATOR.__globals__ is not _RESOURCE_VALIDATOR_GLOBALS
        or _RESOURCE_VALIDATOR_GLOBALS.get(_RESOURCE_LIMIT_NAME) is not _RESOURCE_LIMIT
    ):
        raise ValueError("exchange exposure resource authority drifted")


_EXACT_HELPER_WITNESSES = tuple(
    (
        helper,
        helper.__code__,
        helper.__defaults__,
        helper.__kwdefaults__,
        helper.__closure__,
    )
    for helper in (
        _coefficient_and_exponent,
        _from_coefficient,
        _subtract_exact,
        _multiply_exact,
    )
)


def _build_locked_capital_calculator(
    *,
    decimal_type,
    supported_sides,
    subtract,
    multiply,
    helper_witnesses,
    function_type,
    resource_validator,
):
    """Seal the money-moving arithmetic graph outside caller-controlled kwargs."""

    def calculate(
        *,
        stake: Decimal,
        odds: Decimal,
        exchange_side: str,
    ) -> Decimal:
        _require_resource_validator_authority()
        for function, code, defaults, kwdefaults, closure in helper_witnesses:
            if (
                type(function) is not function_type
                or function.__code__ is not code
                or function.__defaults__ is not defaults
                or function.__kwdefaults__ is not kwdefaults
                or function.__closure__ is not closure
            ):
                raise ValueError("exchange exposure arithmetic authority drifted")

        if type(stake) is not decimal_type:
            raise TypeError("stake must be Decimal")
        if type(odds) is not decimal_type:
            raise TypeError("odds must be Decimal")
        if type(exchange_side) is not str:
            raise TypeError("exchange_side must be str")
        if not stake.is_finite() or stake <= 0:
            raise ValueError("stake must be a finite Decimal > 0")
        if not odds.is_finite() or odds <= 1:
            raise ValueError("odds must be a finite Decimal > 1")
        resource_validator(stake)
        resource_validator(odds)
        _require_resource_validator_authority()

        if exchange_side not in supported_sides:
            raise ValueError(
                "exchange_side must be an exact canonical BACK/LAY or back/lay token"
            )
        if exchange_side in {"BACK", "back"}:
            return stake
        locked_capital = multiply(
            stake,
            subtract(odds, decimal_type("1")),
        )
        resource_validator(locked_capital)
        return locked_capital

    return calculate


locked_capital_for_exchange_side = _build_locked_capital_calculator(
    decimal_type=Decimal,
    supported_sides=_SUPPORTED_EXCHANGE_SIDES,
    subtract=_subtract_exact,
    multiply=_multiply_exact,
    helper_witnesses=_EXACT_HELPER_WITNESSES,
    function_type=FunctionType,
    resource_validator=_validate_decimal_text_resource_bound,
)
locked_capital_for_exchange_side.__name__ = "locked_capital_for_exchange_side"
locked_capital_for_exchange_side.__qualname__ = "locked_capital_for_exchange_side"


__all__ = ["locked_capital_for_exchange_side"]

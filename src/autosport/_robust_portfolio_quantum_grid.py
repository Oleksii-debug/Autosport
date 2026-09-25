from __future__ import annotations

from decimal import Decimal, localcontext

from . import portfolio_plan as _portfolio_plan


_ORIGINAL_DERIVE = _portfolio_plan.RobustPortfolioProposal.derive.__func__
_MAX_GRID_DECIMAL_COEFFICIENT_DIGITS = 4096
_MAX_GRID_DECIMAL_ABS_EXPONENT = 4096
_MAX_GRID_ALIGNED_INTEGER_DIGITS = 4096


def _coefficient_and_exponent(value: Decimal) -> tuple[int, int, int]:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("grid value must be a non-negative finite Decimal")
    parts = value.as_tuple()
    if not isinstance(parts.exponent, int):
        raise ValueError("grid Decimal exponent must be an integer")
    if len(parts.digits) > _MAX_GRID_DECIMAL_COEFFICIENT_DIGITS:
        raise ValueError("grid Decimal coefficient exceeds resource limit")
    if abs(parts.exponent) > _MAX_GRID_DECIMAL_ABS_EXPONENT:
        raise ValueError("grid Decimal exponent exceeds resource limit")
    if not parts.digits or all(digit == 0 for digit in parts.digits):
        return 0, 0, 1

    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    return coefficient, int(parts.exponent), len(parts.digits)


def _decimal_from_nonnegative_coefficient(coefficient: int, exponent: int) -> Decimal:
    if coefficient < 0:
        raise ValueError("grid coefficient must be non-negative")
    if coefficient == 0:
        return Decimal("0")

    reversed_digits: list[int] = []
    remaining = coefficient
    while remaining:
        remaining, digit = divmod(remaining, 10)
        reversed_digits.append(digit)
        if len(reversed_digits) > _MAX_GRID_ALIGNED_INTEGER_DIGITS:
            raise ValueError("grid result exceeds resource limit")
    digits = tuple(reversed(reversed_digits))
    return Decimal((0, digits, exponent))


def _aligned_integer(
    coefficient: int,
    coefficient_digits: int,
    shift: int,
) -> int:
    if shift < 0:
        raise ValueError("grid alignment shift must be non-negative")
    if coefficient == 0:
        return 0
    if coefficient_digits + shift > _MAX_GRID_ALIGNED_INTEGER_DIGITS:
        raise ValueError("grid alignment exceeds resource limit")
    return coefficient * (10 ** shift)


def _floor_to_quantum_grid(value: Decimal, quantum: Decimal) -> Decimal:
    """Return the greatest exact non-negative multiple of quantum <= value.

    Decimal.quantize() only aligns exponents; for a quantum such as ``0.05`` it
    does not enforce a 5-cent grid. Work with bounded integer
    coefficients/exponents so the grid operation is independent of ambient
    Decimal precision/rounding without permitting attacker-sized arithmetic.
    """

    value_coefficient, value_exponent, value_digits = _coefficient_and_exponent(value)
    quantum_coefficient, quantum_exponent, quantum_digits = _coefficient_and_exponent(quantum)
    if quantum_coefficient <= 0:
        raise ValueError("quantum must be positive")
    if value_coefficient == 0:
        return Decimal("0")

    common_exponent = min(value_exponent, quantum_exponent)
    value_units = _aligned_integer(
        value_coefficient,
        value_digits,
        value_exponent - common_exponent,
    )
    quantum_units = _aligned_integer(
        quantum_coefficient,
        quantum_digits,
        quantum_exponent - common_exponent,
    )
    multiples = value_units // quantum_units
    return _decimal_from_nonnegative_coefficient(
        multiples * quantum_units,
        common_exponent,
    )


def _derive_on_exact_quantum_grid(
    cls,
    base_stakes: tuple[Decimal, ...],
    evidence: _portfolio_plan.PortfolioDependencyEvidence,
    *,
    quantum: Decimal = Decimal("0.01"),
):
    # Reject pathological *valid* quantum shapes before Decimal.quantize() can
    # treat a huge coefficient as irrelevant metadata and hand it to our exact
    # grid path. Preserve the owning implementation's validation/error contract
    # for non-Decimal, non-finite, zero and negative quantum values.
    if (
        isinstance(quantum, Decimal)
        and quantum.is_finite()
        and quantum > 0
    ):
        _coefficient_and_exponent(quantum)

    # Delegate validation and canonical stress-factor derivation to the owning
    # implementation. Its exponent-only quantize result is intentionally not
    # trusted as the final monetary grid projection.
    derived = _ORIGINAL_DERIVE(cls, base_stakes, evidence, quantum=quantum)

    with localcontext(_portfolio_plan._ROBUST_STRESS_DECIMAL_CONTEXT):
        stressed_limits = tuple(
            stake * derived.robust_scale for stake in derived.base_stakes
        )
    proposed_stakes = tuple(
        _floor_to_quantum_grid(limit, quantum) for limit in stressed_limits
    )

    return cls(
        base_stakes=derived.base_stakes,
        proposed_stakes=proposed_stakes,
        dependency_haircut_fraction=derived.dependency_haircut_fraction,
        uncertainty_fraction=derived.uncertainty_fraction,
        fee_fraction=derived.fee_fraction,
        partial_fill_stress_fraction=derived.partial_fill_stress_fraction,
        robust_scale=derived.robust_scale,
    )


if not getattr(
    _portfolio_plan.RobustPortfolioProposal,
    "_autosport_exact_quantum_grid_installed",
    False,
):
    _portfolio_plan.RobustPortfolioProposal.derive = classmethod(
        _derive_on_exact_quantum_grid
    )
    _portfolio_plan.RobustPortfolioProposal._autosport_exact_quantum_grid_installed = True

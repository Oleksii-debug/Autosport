from __future__ import annotations

from decimal import Decimal, localcontext

from . import portfolio_plan as _portfolio_plan


_ORIGINAL_DERIVE = _portfolio_plan.RobustPortfolioProposal.derive.__func__


def _coefficient_and_exponent(value: Decimal) -> tuple[int, int]:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ValueError("grid value must be a non-negative finite Decimal")
    parts = value.as_tuple()
    coefficient = 0
    for digit in parts.digits:
        coefficient = coefficient * 10 + digit
    if parts.sign:
        coefficient = -coefficient
    return coefficient, int(parts.exponent)


def _decimal_from_nonnegative_coefficient(coefficient: int, exponent: int) -> Decimal:
    if coefficient < 0:
        raise ValueError("grid coefficient must be non-negative")
    digits = tuple(int(char) for char in str(coefficient))
    return Decimal((0, digits, exponent))


def _floor_to_quantum_grid(value: Decimal, quantum: Decimal) -> Decimal:
    """Return the greatest exact non-negative multiple of quantum <= value.

    Decimal.quantize() only aligns exponents; for a quantum such as ``0.05`` it
    does not enforce a 5-cent grid.  Work with integer coefficients/exponents so
    the grid operation is independent of ambient Decimal precision and rounding.
    """

    value_coefficient, value_exponent = _coefficient_and_exponent(value)
    quantum_coefficient, quantum_exponent = _coefficient_and_exponent(quantum)
    if quantum_coefficient <= 0:
        raise ValueError("quantum must be positive")

    common_exponent = min(value_exponent, quantum_exponent)
    value_units = value_coefficient * (10 ** (value_exponent - common_exponent))
    quantum_units = quantum_coefficient * (10 ** (quantum_exponent - common_exponent))
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
    # Delegate validation and canonical stress-factor derivation to the owning
    # implementation.  Its exponent-only quantize result is intentionally not
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

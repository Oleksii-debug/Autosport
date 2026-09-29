from __future__ import annotations

from decimal import Decimal, ROUND_HALF_EVEN, localcontext

import pytest
from hypothesis import given, settings, strategies as st

from autosport.calculation import CalculationEngine


# CalculationEngine's integrated deterministic Decimal contract is 160 significant
# digits with ROUND_HALF_EVEN. Property oracles that compare rounded engine outputs
# must use the same contract rather than a higher-precision one-sided inequality.
_ENGINE_DECIMAL_PRECISION = 160

_DECIMAL_ODDS = st.decimals(
    min_value=Decimal("1.001"),
    max_value=Decimal("100"),
    places=3,
    allow_nan=False,
    allow_infinity=False,
)
_PROBABILITIES = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1"),
    places=3,
    allow_nan=False,
    allow_infinity=False,
)
_NONNEGATIVE_MONEY = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("100000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)
_UNIT_FRACTIONS = st.decimals(
    min_value=Decimal("0"),
    max_value=Decimal("1"),
    places=3,
    allow_nan=False,
    allow_infinity=False,
)


def _outputs(result) -> dict[str, str]:
    return dict(result.outputs)


def _canonical_fractional_allocation(
    full_kelly_fraction: Decimal,
    fraction: Decimal,
    cap: Decimal,
) -> Decimal:
    with localcontext() as context:
        context.prec = _ENGINE_DECIMAL_PRECISION
        context.rounding = ROUND_HALF_EVEN
        return min(full_kelly_fraction * fraction, cap)


@settings(max_examples=64, deadline=None)
@given(decimal_odds=_DECIMAL_ODDS)
def test_implied_probability_is_normalized_and_process_decimal_context_independent(
    decimal_odds: Decimal,
) -> None:
    engine = CalculationEngine()

    with localcontext() as context:
        context.prec = 6
        low_precision = engine.implied_probability(decimal_odds)
    with localcontext() as context:
        context.prec = 80
        high_precision = engine.implied_probability(str(decimal_odds))

    assert low_precision.input_hash == high_precision.input_hash
    assert low_precision.result_hash == high_precision.result_hash
    probability = Decimal(_outputs(low_precision)["implied_probability"])
    assert Decimal("0") < probability < Decimal("1")


@settings(max_examples=64, deadline=None)
@given(
    probability=_PROBABILITIES,
    decimal_odds=_DECIMAL_ODDS,
    stake=_NONNEGATIVE_MONEY,
)
def test_expected_return_preserves_closed_form_decimal_identity(
    probability: Decimal,
    decimal_odds: Decimal,
    stake: Decimal,
) -> None:
    result = CalculationEngine().expected_return(probability, decimal_odds, stake)
    outputs = _outputs(result)

    expected_return_per_unit = probability * decimal_odds
    expected_profit_per_unit = expected_return_per_unit - Decimal("1")
    expected_profit = expected_profit_per_unit * stake

    assert result.exact
    assert Decimal(outputs["expected_return_per_unit"]) == expected_return_per_unit
    assert Decimal(outputs["expected_profit_per_unit"]) == expected_profit_per_unit
    assert Decimal(outputs["expected_profit"]) == expected_profit


@settings(max_examples=64, deadline=None)
@given(first=_DECIMAL_ODDS, second=_DECIMAL_ODDS)
def test_multiplicative_devig_is_permutation_invariant_and_normalized(
    first: Decimal,
    second: Decimal,
) -> None:
    engine = CalculationEngine()
    forward_market = {"alpha": first, "beta": second}
    reverse_market = {"beta": second, "alpha": first}

    try:
        forward = engine.multiplicative_devig(forward_market)
    except ValueError as exc:
        # The canonical engine deliberately fails closed when the exact market
        # margin subtraction would round. Mapping order cannot change whether a
        # pair belongs to that fail-closed domain.
        assert str(exc) == "market margin arithmetic would require rounding"
        with pytest.raises(
            ValueError,
            match=r"^market margin arithmetic would require rounding$",
        ):
            engine.multiplicative_devig(reverse_market)
        return

    reverse = engine.multiplicative_devig(reverse_market)
    assert forward.input_hash == reverse.input_hash
    assert forward.result_hash == reverse.result_hash

    outputs = _outputs(forward)
    with localcontext() as context:
        context.prec = 200
        fair_total = (
            Decimal(outputs["fair_probability.alpha"])
            + Decimal(outputs["fair_probability.beta"])
        )
    assert abs(fair_total - Decimal("1")) <= Decimal("1e-150")


def test_multiplicative_devig_rounding_failure_is_permutation_invariant() -> None:
    first = Decimal("19.901")
    second = Decimal("20.100")
    engine = CalculationEngine()

    for market in (
        {"alpha": first, "beta": second},
        {"beta": second, "alpha": first},
    ):
        with pytest.raises(
            ValueError,
            match=r"^market margin arithmetic would require rounding$",
        ):
            engine.multiplicative_devig(market)


@settings(max_examples=64, deadline=None)
@given(
    probability=_PROBABILITIES,
    decimal_odds=_DECIMAL_ODDS,
    fraction=_UNIT_FRACTIONS,
    cap=_UNIT_FRACTIONS,
)
def test_fractional_kelly_never_exceeds_declared_cap_or_goes_negative(
    probability: Decimal,
    decimal_odds: Decimal,
    fraction: Decimal,
    cap: Decimal,
) -> None:
    result = CalculationEngine().fractional_kelly(
        probability,
        decimal_odds,
        fraction=fraction,
        cap=cap,
    )
    outputs = _outputs(result)

    full = Decimal(outputs["full_kelly_fraction"])
    recommended = Decimal(outputs["capped_fraction"])
    assert full >= Decimal("0")
    assert Decimal("0") <= recommended <= cap
    assert recommended == _canonical_fractional_allocation(full, fraction, cap)


def test_fractional_kelly_rounding_regression_uses_canonical_engine_precision() -> None:
    probability = Decimal("0.911")
    decimal_odds = Decimal("54.773")
    fraction = Decimal("0.729")
    cap = Decimal("1")

    result = CalculationEngine().fractional_kelly(
        probability,
        decimal_odds,
        fraction=fraction,
        cap=cap,
    )
    outputs = _outputs(result)
    full = Decimal(outputs["full_kelly_fraction"])
    recommended = Decimal(outputs["capped_fraction"])

    assert recommended == _canonical_fractional_allocation(full, fraction, cap)

    # This exact generated case is retained because the old property compared the
    # 160-digit rounded runtime value one-sided against an unrelated 200-digit
    # recomputation and therefore rejected correct ROUND_HALF_EVEN behavior.
    with localcontext() as context:
        context.prec = 200
        higher_precision_product = full * fraction
    assert recommended > higher_precision_product


@settings(max_examples=64, deadline=None)
@given(stake=_NONNEGATIVE_MONEY, decimal_odds=_DECIMAL_ODDS)
def test_paper_payout_matches_exact_decimal_money_identity(
    stake: Decimal,
    decimal_odds: Decimal,
) -> None:
    result = CalculationEngine().paper_payout(stake, decimal_odds)
    outputs = _outputs(result)

    payout = stake * decimal_odds
    assert result.exact
    assert Decimal(outputs["payout"]) == payout
    assert Decimal(outputs["profit"]) == payout - stake

from __future__ import annotations

from decimal import Decimal, localcontext

from hypothesis import given, settings, strategies as st

from autosport.calculation import CalculationEngine


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
    forward = engine.multiplicative_devig({"alpha": first, "beta": second})
    reverse = engine.multiplicative_devig({"beta": second, "alpha": first})

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
    assert recommended <= full * fraction if full > 0 else recommended == 0


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

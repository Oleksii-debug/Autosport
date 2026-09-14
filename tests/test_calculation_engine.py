from __future__ import annotations

from decimal import Decimal, getcontext

import pytest

from autosport.calculation import CalculationEngine


def _outputs(result) -> dict[str, str]:
    return dict(result.outputs)


def test_implied_probability_reports_approximation_and_stable_hashes() -> None:
    engine = CalculationEngine()

    first = engine.implied_probability("3")
    second = engine.implied_probability(Decimal("3.000"))

    assert first.calculation_id == "implied_probability"
    assert first.classification == "approximate_decimal"
    assert not first.exact
    assert _outputs(first)["implied_probability"].startswith("0.333333333333")
    assert _outputs(first)["break_even_probability"] == _outputs(first)["implied_probability"]
    assert first.input_hash == second.input_hash
    assert first.result_hash == second.result_hash
    assert len(first.input_hash) == 64
    assert len(first.result_hash) == 64


def test_multiplicative_devig_is_order_independent_and_sums_to_one() -> None:
    engine = CalculationEngine()

    forward = engine.multiplicative_devig({"home": "2.10", "away": "1.80"})
    reverse = engine.multiplicative_devig({"away": Decimal("1.800"), "home": Decimal("2.1000")})

    assert forward.result_hash == reverse.result_hash
    outputs = _outputs(forward)
    fair_total = Decimal(outputs["fair_probability.home"]) + Decimal(outputs["fair_probability.away"])
    assert fair_total == Decimal("1")
    assert Decimal(outputs["overround"]) > Decimal("1")
    assert forward.classification == "approximate_decimal"
    assert dict(forward.input_units) == {
        "decimal_odds.away": "decimal_odds",
        "decimal_odds.home": "decimal_odds",
    }


def test_expected_return_and_paper_payout_are_exact_decimal_arithmetic() -> None:
    engine = CalculationEngine()

    expected = engine.expected_return("0.60", "2.00", "25.00")
    payout = engine.paper_payout("25.00", "2.00")

    assert expected.exact
    assert _outputs(expected) == {
        "expected_profit": "5",
        "expected_profit_per_unit": "0.2",
        "expected_return_per_unit": "1.2",
    }
    assert payout.exact
    assert _outputs(payout) == {"payout": "50", "profit": "25"}
    assert "real-money" in payout.assumptions[0]


def test_fractional_kelly_zero_edge_and_cap_are_explicit() -> None:
    engine = CalculationEngine()

    losing = engine.fractional_kelly("0.40", "2", fraction="0.5", cap="0.2")
    capped = engine.fractional_kelly("0.80", "2", fraction="1", cap="0.10")

    assert _outputs(losing)["capped_fraction"] == "0"
    assert _outputs(losing)["full_kelly_fraction"] == "0"
    assert _outputs(capped)["full_kelly_fraction"] == "0.6"
    assert _outputs(capped)["capped_fraction"] == "0.1"
    assert capped.classification == "approximate_decimal"


def test_performance_summary_reports_roi_yield_and_turnover() -> None:
    result = CalculationEngine().performance_summary(
        net_profit="25",
        turnover="250",
        starting_bankroll="100",
    )

    assert _outputs(result) == {"roi": "0.25", "turnover": "250", "yield": "0.1"}
    assert dict(result.output_units)["turnover"] == "paper_currency"


def test_maximum_drawdown_uses_chronological_running_peak() -> None:
    result = CalculationEngine().maximum_drawdown(["100", "120", "90", "110", "60", "130"])

    assert _outputs(result)["maximum_drawdown_absolute"] == "60"
    assert _outputs(result)["maximum_drawdown_fraction"] == "0.5"
    assert result.assumptions == ("balances are supplied in chronological order",)


@pytest.mark.parametrize(
    "value",
    [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-Infinity"), "NaN", "Infinity"],
)
def test_nonfinite_inputs_fail_closed(value) -> None:
    engine = CalculationEngine()

    with pytest.raises(ValueError, match="finite"):
        engine.implied_probability(value)


@pytest.mark.parametrize("value", [True, False, 2.0, object()])
def test_ambiguous_numeric_types_fail_closed(value) -> None:
    with pytest.raises(ValueError, match="Decimal, integer, or canonical decimal text"):
        CalculationEngine().implied_probability(value)


def test_whitespace_and_extreme_numeric_inputs_fail_closed() -> None:
    engine = CalculationEngine()

    with pytest.raises(ValueError, match="canonical decimal text"):
        engine.paper_payout(" 10", "2")
    with pytest.raises(ValueError, match="supported precision"):
        engine.paper_payout("1." + "1" * 81, "2")
    with pytest.raises(ValueError, match="supported magnitude"):
        engine.paper_payout("1e101", "2")


def test_invalid_ranges_and_market_shapes_fail_closed() -> None:
    engine = CalculationEngine()

    with pytest.raises(ValueError, match="greater than 1"):
        engine.implied_probability("1")
    with pytest.raises(ValueError, match="at least two selections"):
        engine.multiplicative_devig({"only": "2"})
    with pytest.raises(ValueError, match="non-empty trimmed string"):
        engine.multiplicative_devig({" home ": "2", "away": "2"})
    with pytest.raises(ValueError, match="at most 1"):
        engine.expected_return("1.01", "2")
    with pytest.raises(ValueError, match="greater than 0"):
        engine.performance_summary("1", "0", "100")
    with pytest.raises(ValueError, match="at least one balance"):
        engine.maximum_drawdown([])
    with pytest.raises(ValueError, match="at least 0"):
        engine.maximum_drawdown(["100", "-1"])


def test_result_serialization_contains_full_truth_and_hashes() -> None:
    result = CalculationEngine().paper_payout("10", "2.5")
    payload = result.as_dict()

    assert payload["calculation_id"] == "paper_payout"
    assert payload["version"] == 1
    assert payload["method"] == "decimal_odds_payout"
    assert payload["classification"] == "exact"
    assert payload["inputs"] == {"decimal_odds": "2.5", "stake": "10"}
    assert payload["outputs"] == {"payout": "25", "profit": "15"}
    assert payload["input_hash"] == result.input_hash
    assert payload["result_hash"] == result.result_hash


def test_engine_does_not_mutate_callers_decimal_context() -> None:
    context = getcontext()
    original_precision = context.prec
    original_flags = dict(context.flags)

    CalculationEngine().implied_probability("3")
    CalculationEngine().fractional_kelly("0.61", "2.35", "0.5", "0.1")

    assert context.prec == original_precision
    assert context.flags == original_flags

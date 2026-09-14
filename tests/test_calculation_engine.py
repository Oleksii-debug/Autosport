from __future__ import annotations

from decimal import Decimal, getcontext, localcontext

import pytest

from autosport.calculation import CalculationEngine


def _outputs(result) -> dict[str, str]:
    return dict(result.outputs)


def test_odds_conversion_preserves_exact_fraction_and_unrounded_american_truth() -> None:
    engine = CalculationEngine()

    positive = engine.odds_conversion("2.5")
    negative = engine.odds_conversion("1.5")

    assert positive.exact
    assert _outputs(positive) == {
        "american_odds_unrounded": "150",
        "decimal_odds": "2.5",
        "fractional_denominator": "2",
        "fractional_numerator": "3",
    }
    assert negative.classification == "approximate_decimal"
    assert _outputs(negative)["american_odds_unrounded"] == "-200"
    assert _outputs(negative)["fractional_numerator"] == "1"
    assert _outputs(negative)["fractional_denominator"] == "2"


def test_reverse_odds_conversion_is_typed_and_truth_labelled() -> None:
    engine = CalculationEngine()

    positive = engine.american_to_decimal_odds("150")
    negative = engine.american_to_decimal_odds("-200")
    fractional = engine.fractional_to_decimal_odds("3", "2")

    assert positive.exact
    assert _outputs(positive)["decimal_odds"] == "2.5"
    assert negative.classification == "approximate_decimal"
    assert _outputs(negative)["decimal_odds"] == "1.5"
    assert _outputs(fractional)["decimal_odds"] == "2.5"
    with pytest.raises(ValueError, match="at most -100 or at least 100"):
        engine.american_to_decimal_odds("50")


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
    assert Decimal(outputs["overround"]) > Decimal("1")
    with localcontext() as context:
        context.prec = 200
        fair_total = Decimal(outputs["fair_probability.home"]) + Decimal(outputs["fair_probability.away"])
        assert fair_total == Decimal("1")
        assert Decimal(outputs["market_margin"]) == Decimal(outputs["overround"]) - Decimal("1")
    assert Decimal(outputs["fair_decimal_odds.home"]) > Decimal("1")
    assert Decimal(outputs["fair_decimal_odds.away"]) > Decimal("1")
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


def test_performance_summary_preserves_canonical_roi_and_labels_bankroll_return() -> None:
    result = CalculationEngine().performance_summary(
        net_profit="25",
        turnover="250",
        starting_bankroll="100",
    )

    assert _outputs(result) == {
        "bankroll_return": "0.25",
        "roi": "0.1",
        "turnover": "250",
        "yield": "0.1",
    }
    assert dict(result.output_units) == {
        "bankroll_return": "fraction",
        "roi": "fraction",
        "turnover": "paper_currency",
        "yield": "fraction",
    }
    assert "settled-stake denominator" in result.assumptions[1]


def test_return_dispersion_distinguishes_population_and_sample_contracts() -> None:
    engine = CalculationEngine()

    population = engine.return_dispersion(["1", "2", "3"])
    sample = engine.return_dispersion(["1", "2", "3"], sample=True)

    population_outputs = _outputs(population)
    assert population_outputs["count"] == "3"
    assert population_outputs["mean"] == "2"
    assert population_outputs["variance"].startswith("0.666666666666")
    assert population_outputs["standard_deviation"].startswith("0.816496580927")
    assert _outputs(sample)["variance"] == "1"
    assert _outputs(sample)["standard_deviation"] == "1"
    assert population.result_hash != sample.result_hash
    assert population.classification == "approximate_decimal"


def test_normal_confidence_interval_requires_explicit_model_acknowledgement() -> None:
    engine = CalculationEngine()

    with pytest.raises(ValueError, match="explicit assumption"):
        engine.normal_confidence_interval("10", "2", "1.96")

    result = engine.normal_confidence_interval(
        "10",
        "2",
        "1.96",
        assumption="normal_approximation_acknowledged",
    )

    assert result.classification == "approximate_decimal"
    assert _outputs(result) == {
        "lower_bound": "6.08",
        "margin": "3.92",
        "mean": "10",
        "upper_bound": "13.92",
    }
    assert "normal-approximation" in result.assumptions[0]


def test_paper_parlay_exact_payout_does_not_invent_joint_probability() -> None:
    result = CalculationEngine().paper_parlay("10", ["2", "1.5"])

    assert result.exact
    assert _outputs(result) == {
        "combined_decimal_odds": "3",
        "payout": "30",
        "profit": "20",
    }
    assert "joint probability" in result.assumptions[0]


def test_paper_parlay_probability_requires_explicit_independence() -> None:
    engine = CalculationEngine()

    with pytest.raises(ValueError, match="probability_assumption='independent'"):
        engine.paper_parlay("10", ["2", "1.5"], probabilities=["0.5", "0.4"])
    with pytest.raises(ValueError, match="match the number"):
        engine.paper_parlay(
            "10",
            ["2", "1.5", "3"],
            probabilities=["0.5", "0.4"],
            probability_assumption="independent",
        )

    result = engine.paper_parlay(
        "10",
        ["2", "1.5"],
        probabilities=["0.5", "0.4"],
        probability_assumption="independent",
    )
    assert result.classification == "approximate_decimal"
    assert _outputs(result)["joint_probability"] == "0.2"
    assert "independent" in result.assumptions[0]


def test_finite_scenario_table_never_infers_completeness() -> None:
    engine = CalculationEngine()

    partial = engine.finite_scenario_table(
        {"loss": "-10", "win": "15"},
        completeness="partial",
    )
    complete = engine.finite_scenario_table(
        {"win": "15", "loss": "-10"},
        completeness="complete",
    )

    assert partial.exact
    assert _outputs(partial)["scenario_count"] == "2"
    assert _outputs(partial)["worst_case"] == "-10"
    assert _outputs(partial)["best_case"] == "15"
    assert _outputs(partial)["scenario_profit.loss"] == "-10"
    assert "partial" in partial.method
    assert "complete" in complete.method
    assert partial.result_hash != complete.result_hash
    with pytest.raises(ValueError, match="exactly 'complete' or 'partial'"):
        engine.finite_scenario_table({"only": "1"}, completeness="unknown")


def test_text_identifiers_are_utf8_safe_and_non_ascii_hashes_are_stable() -> None:
    engine = CalculationEngine()

    with pytest.raises(ValueError, match="UTF-8 encodable"):
        engine.multiplicative_devig({"\ud800": "2", "valid": "2"})
    with pytest.raises(ValueError, match="UTF-8 encodable"):
        engine.finite_scenario_table({"\ud800": "1"}, completeness="partial")

    forward = engine.multiplicative_devig({"господарі": "2.10", "гості": "1.80"})
    reverse = engine.multiplicative_devig({"гості": Decimal("1.800"), "господарі": Decimal("2.1000")})
    assert forward.result_hash == reverse.result_hash
    assert "decimal_odds.гості" in dict(forward.inputs)
    assert "decimal_odds.господарі" in dict(forward.inputs)


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
    with pytest.raises(ValueError, match="sample variance requires"):
        engine.return_dispersion(["1"], sample=True)
    with pytest.raises(ValueError, match="sample must be a boolean"):
        engine.return_dispersion(["1", "2"], sample=1)


def test_result_serialization_contains_full_truth_and_hashes() -> None:
    result = CalculationEngine().paper_payout("10", "2.5")
    payload = result.as_dict()

    assert payload["calculation_id"] == "paper_payout"
    assert payload["version"] == 1
    assert payload["engine_version"] == "calculation-engine-v1"
    assert payload["method"] == "decimal_odds_payout"
    assert payload["classification"] == "exact"
    assert payload["inputs"] == {"decimal_odds": "2.5", "stake": "10"}
    assert payload["outputs"] == {"payout": "25", "profit": "15"}
    assert payload["input_hash"] == result.input_hash
    assert payload["result_hash"] == result.result_hash


@pytest.mark.parametrize("precision", [6, 10, 28, 50, 100])
def test_engine_results_are_independent_of_callers_decimal_precision(precision: int) -> None:
    engine = CalculationEngine()

    with localcontext() as baseline_context:
        baseline_context.prec = 160
        baseline = (
            engine.multiplicative_devig({"home": "2.10", "away": "1.80"}).result_hash,
            engine.american_to_decimal_odds("-137").result_hash,
            engine.maximum_drawdown(["100", "123.45", "67.89", "130"]).result_hash,
        )

    with localcontext() as caller_context:
        caller_context.prec = precision
        observed = (
            engine.multiplicative_devig({"home": "2.10", "away": "1.80"}).result_hash,
            engine.american_to_decimal_odds("-137").result_hash,
            engine.maximum_drawdown(["100", "123.45", "67.89", "130"]).result_hash,
        )

    assert observed == baseline


def test_engine_does_not_mutate_callers_decimal_context() -> None:
    context = getcontext()
    original_precision = context.prec
    original_flags = dict(context.flags)

    CalculationEngine().implied_probability("3")
    CalculationEngine().fractional_kelly("0.61", "2.35", "0.5", "0.1")
    CalculationEngine().return_dispersion(["0.1", "0.2", "0.3"])
    CalculationEngine().paper_parlay(
        "10",
        ["2", "1.5"],
        probabilities=["0.5", "0.4"],
        probability_assumption="independent",
    )

    assert context.prec == original_precision
    assert context.flags == original_flags

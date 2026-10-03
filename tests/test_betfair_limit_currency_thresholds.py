from decimal import Decimal, localcontext

import pytest

from autosport.betfair_limit_currency_thresholds import (
    CURRENCY_PARAMETERS,
    RULESET_EFFECTIVE_INTERVAL_PROVEN,
    BetfairCurrencyThresholdError,
    BetfairCurrencyThresholdState,
    evaluate_standard_limit_currency_thresholds,
)


def assess(
    currency="GBP",
    *,
    side="BACK",
    price="2",
    size="2",
    target=None,
):
    return evaluate_standard_limit_currency_thresholds(
        currency_code=currency,
        side=side,
        price=Decimal(price),
        size=None if size is None else Decimal(size),
        bet_target_type=target,
    )


def test_ruleset_is_explicitly_not_currentness_or_execution_authority():
    result = assess()
    assert RULESET_EFFECTIVE_INTERVAL_PROVEN is False
    assert result.ruleset_effective_interval_proven is False
    assert result.currency_thresholds_satisfied is True
    assert result.account_currency_bound is False
    assert result.jurisdiction_bound is False
    assert result.market_admissibility_proven is False
    assert result.execution_authorized is False
    assert result.real_money_execution is False


@pytest.mark.parametrize(
    ("code", "minimum", "payout"),
    [
        ("GBP", "2", "10"),
        ("EUR", "2", "20"),
        ("USD", "3", "20"),
        ("HKD", "25", "125"),
        ("AUD", "5", "30"),
        ("CAD", "6", "30"),
        ("DKK", "30", "150"),
        ("NOK", "30", "150"),
        ("SEK", "30", "150"),
        ("SGD", "6", "30"),
        ("RON", "10", "50"),
        ("BRL", "10", "50"),
        ("MXN", "60", "300"),
        ("PEN", "10", "50"),
        ("HUF", "800", "4000"),
        ("ISK", "350", "1750"),
        ("NZD", "2", "10"),
        ("ARS", "100", "500"),
        ("GEL", "10", "50"),
    ],
)
def test_currency_parameter_snapshot_values(code, minimum, payout):
    rules = CURRENCY_PARAMETERS[code]
    assert rules.min_bet_size == Decimal(minimum)
    assert rules.min_bet_payout == Decimal(payout)


@pytest.mark.parametrize("side", ["BACK", "LAY"])
def test_standard_stake_at_currency_minimum_satisfies_thresholds(side):
    result = assess(side=side, size="2", price="1.01")
    assert result.state is BetfairCurrencyThresholdState.THRESHOLDS_SATISFIED
    assert result.gross_payout == Decimal("2.02")


def test_below_minimum_with_exact_payout_threshold_needs_jurisdiction():
    result = assess(size="1", price="10")
    assert (
        result.state
        is BetfairCurrencyThresholdState.BELOW_MINIMUM_PAYOUT_EXCEPTION_REQUIRES_JURISDICTION
    )
    assert result.low_stake_exception_candidate is True
    assert result.currency_thresholds_satisfied is False
    assert result.jurisdiction_bound is False


def test_below_minimum_and_payout_is_rejected_by_currency_thresholds():
    result = assess(size="1", price="9.99")
    assert result.state is BetfairCurrencyThresholdState.BELOW_CURRENCY_THRESHOLDS
    assert result.gross_payout == Decimal("9.99")


@pytest.mark.parametrize("currency", ["DKK", "SEK"])
def test_jurisdiction_excluded_currency_never_launders_low_stake_exception(currency):
    rules = CURRENCY_PARAMETERS[currency]
    size = rules.min_bet_size / Decimal("2")
    price = rules.min_bet_payout / size
    result = evaluate_standard_limit_currency_thresholds(
        currency_code=currency,
        side="BACK",
        price=price,
        size=size,
    )
    assert (
        result.state
        is BetfairCurrencyThresholdState.BELOW_MINIMUM_PAYOUT_EXCEPTION_REQUIRES_JURISDICTION
    )
    assert result.execution_authorized is False


@pytest.mark.parametrize("target", ["PAYOUT", "BACKERS_PROFIT"])
def test_target_sizing_is_not_reinterpreted_as_standard_size(target):
    result = assess(target=target, size="0.01", price="1000")
    assert result.state is BetfairCurrencyThresholdState.TARGET_SIZING_OUTSIDE_SCOPE
    assert result.size is None
    assert result.gross_payout is None
    assert result.execution_authorized is False


def test_unknown_currency_fails_closed_without_threshold_fabrication():
    result = assess(currency="XYZ", size="5", price="2")
    assert result.state is BetfairCurrencyThresholdState.UNSUPPORTED_CURRENCY
    assert result.size is None
    assert result.min_bet_size is None
    assert result.min_bet_payout is None
    assert result.gross_payout is None


@pytest.mark.parametrize("bad", ["gbp", "GB", "GBPP", "G1P", " GBP"])
def test_currency_code_must_be_canonical(bad):
    with pytest.raises(BetfairCurrencyThresholdError, match="currency_code"):
        evaluate_standard_limit_currency_thresholds(
            currency_code=bad,
            side="BACK",
            price=Decimal("2"),
            size=Decimal("2"),
        )


@pytest.mark.parametrize("bad", ["back", "LAY ", "", "BUY"])
def test_side_must_be_exact(bad):
    with pytest.raises(BetfairCurrencyThresholdError, match="side"):
        assess(side=bad)


@pytest.mark.parametrize(
    "bad",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-1"),
        Decimal("0"),
        Decimal("1"),
    ],
)
def test_price_must_be_finite_and_above_one(bad):
    with pytest.raises(BetfairCurrencyThresholdError):
        evaluate_standard_limit_currency_thresholds(
            currency_code="GBP",
            side="BACK",
            price=bad,
            size=Decimal("2"),
        )


@pytest.mark.parametrize(
    "bad",
    [Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), Decimal("0")],
)
def test_standard_size_must_be_positive_finite_decimal(bad):
    with pytest.raises(BetfairCurrencyThresholdError):
        evaluate_standard_limit_currency_thresholds(
            currency_code="GBP",
            side="BACK",
            price=Decimal("2"),
            size=bad,
        )


def test_binary_float_ingress_is_rejected():
    with pytest.raises(BetfairCurrencyThresholdError, match="Decimal"):
        evaluate_standard_limit_currency_thresholds(
            currency_code="GBP",
            side="BACK",
            price=2.0,  # type: ignore[arg-type]
            size=Decimal("2"),
        )


def test_missing_standard_size_fails_closed():
    with pytest.raises(BetfairCurrencyThresholdError, match="requires size"):
        assess(size=None)


def test_unknown_target_mode_fails_closed():
    with pytest.raises(BetfairCurrencyThresholdError, match="bet_target_type"):
        assess(target="SOMETHING_ELSE")


def test_exact_payout_is_independent_of_ambient_decimal_precision():
    with localcontext() as context:
        context.prec = 2
        result = assess(currency="HUF", size="799.999", price="5.00001")
    assert result.gross_payout == Decimal("4000.00299999")
    assert (
        result.state
        is BetfairCurrencyThresholdState.BELOW_MINIMUM_PAYOUT_EXCEPTION_REQUIRES_JURISDICTION
    )


def test_oversized_decimal_shape_fails_before_expensive_arithmetic():
    with pytest.raises(BetfairCurrencyThresholdError, match="bounded Decimal shape"):
        assess(size="1e-100", price="1e100")


def test_threshold_result_is_immutable():
    result = assess()
    with pytest.raises((AttributeError, TypeError)):
        result.state = BetfairCurrencyThresholdState.BELOW_CURRENCY_THRESHOLDS  # type: ignore[misc]

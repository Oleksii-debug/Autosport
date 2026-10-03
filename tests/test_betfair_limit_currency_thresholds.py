from decimal import Decimal, localcontext

import pytest

import autosport.betfair_limit_currency_thresholds as threshold_module
from autosport.betfair_limit_currency_thresholds import (
    CURRENCY_PARAMETERS,
    RULESET_EFFECTIVE_INTERVAL_PROVEN,
    BetfairCurrencyParameters,
    BetfairCurrencyThresholdAssessment,
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
    assert result.snapshot_standard_minimum_met is True
    assert result.account_currency_bound is False
    assert result.jurisdiction_bound is False
    assert result.current_provider_constraint_proven is False
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
    assert result.state is BetfairCurrencyThresholdState.SNAPSHOT_STANDARD_MINIMUM_MET
    assert result.gross_payout == Decimal("2.02")


def test_below_minimum_with_exact_payout_threshold_needs_jurisdiction():
    result = assess(size="1", price="10")
    assert (
        result.state
        is BetfairCurrencyThresholdState.SNAPSHOT_LOWER_PAYOUT_MECHANIC_MET_REQUIRES_JURISDICTION
    )
    assert result.low_stake_exception_candidate is True
    assert result.snapshot_standard_minimum_met is False
    assert result.jurisdiction_bound is False


def test_below_minimum_and_payout_is_rejected_by_currency_thresholds():
    result = assess(size="1", price="9.99")
    assert result.state is BetfairCurrencyThresholdState.SNAPSHOT_BELOW_THRESHOLDS
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
        is BetfairCurrencyThresholdState.SNAPSHOT_LOWER_PAYOUT_MECHANIC_MET_REQUIRES_JURISDICTION
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
        is BetfairCurrencyThresholdState.SNAPSHOT_LOWER_PAYOUT_MECHANIC_MET_REQUIRES_JURISDICTION
    )


def test_oversized_decimal_shape_fails_before_expensive_arithmetic():
    with pytest.raises(BetfairCurrencyThresholdError, match="bounded Decimal shape"):
        assess(size="1e-100", price="1e100")


def test_threshold_result_is_immutable():
    result = assess()
    with pytest.raises((AttributeError, TypeError)):
        result.state = BetfairCurrencyThresholdState.SNAPSHOT_BELOW_THRESHOLDS  # type: ignore[misc]

class _HostileDecimal(Decimal):
    def is_finite(self):
        raise AssertionError("hostile Decimal is_finite executed")


class _HostileStr(str):
    def isascii(self):
        raise AssertionError("hostile str isascii executed")

    def __hash__(self):
        raise AssertionError("hostile str hash executed")


def test_snapshot_ingress_rejects_subclasses_before_virtual_dispatch():
    with pytest.raises(BetfairCurrencyThresholdError, match="currency_code"):
        evaluate_standard_limit_currency_thresholds(
            currency_code=_HostileStr("GBP"),
            side="BACK",
            price=Decimal("2"),
            size=Decimal("2"),
        )

    with pytest.raises(BetfairCurrencyThresholdError, match="positive finite Decimal"):
        evaluate_standard_limit_currency_thresholds(
            currency_code="GBP",
            side="BACK",
            price=_HostileDecimal("2"),
            size=Decimal("2"),
        )

    with pytest.raises(BetfairCurrencyThresholdError, match="side"):
        evaluate_standard_limit_currency_thresholds(
            currency_code="GBP",
            side=_HostileStr("BACK"),
            price=Decimal("2"),
            size=Decimal("2"),
        )

    with pytest.raises(BetfairCurrencyThresholdError, match="bet_target_type"):
        evaluate_standard_limit_currency_thresholds(
            currency_code="GBP",
            side="BACK",
            price=Decimal("2"),
            size=Decimal("2"),
            bet_target_type=_HostileStr("PAYOUT"),
        )



def test_hard_false_authority_surface_uses_non_python_getters_and_is_sealed():
    result = assess()
    hard_false_names = (
        "ruleset_effective_interval_proven",
        "account_currency_bound",
        "jurisdiction_bound",
        "current_provider_constraint_proven",
        "market_admissibility_proven",
        "execution_authorized",
        "real_money_execution",
    )

    for name in hard_false_names:
        descriptor = BetfairCurrencyThresholdAssessment.__dict__[name]
        assert isinstance(descriptor, property)
        assert descriptor.fget is not None
        assert not hasattr(descriptor.fget, "__code__")
        assert getattr(result, name) is False

    with pytest.raises(TypeError, match="authority surface is sealed"):
        BetfairCurrencyThresholdAssessment._execution_authorized_constant = True

    with pytest.raises(TypeError, match="authority surface is sealed"):
        BetfairCurrencyThresholdAssessment.execution_authorized = property(
            lambda _self: True
        )

    with pytest.raises(AttributeError):
        object.__setattr__(result, "_execution_authorized_constant", True)

    with pytest.raises(AttributeError):
        object.__setattr__(result, "real_money_execution", True)


def test_historical_currency_parameter_rows_are_structurally_immutable():
    rules = CURRENCY_PARAMETERS["GBP"]

    with pytest.raises(AttributeError):
        object.__setattr__(rules, "min_bet_size", Decimal("0.01"))

    with pytest.raises(TypeError):
        CURRENCY_PARAMETERS["GBP"] = rules  # type: ignore[index]

    assert CURRENCY_PARAMETERS["GBP"].min_bet_size == Decimal("2")


def test_evaluator_keeps_canonical_snapshot_when_public_mapping_is_rebound(
    monkeypatch,
):
    forged = dict(CURRENCY_PARAMETERS)
    forged["GBP"] = BetfairCurrencyParameters(
        currency_code="GBP",
        min_bet_size=Decimal("0.01"),
        min_bsp_liability=Decimal("10"),
        min_bet_payout=Decimal("0.01"),
    )
    monkeypatch.setattr(threshold_module, "CURRENCY_PARAMETERS", forged)

    result = threshold_module.evaluate_standard_limit_currency_thresholds(
        currency_code="GBP",
        side="BACK",
        price=Decimal("2"),
        size=Decimal("1"),
    )

    assert result.min_bet_size == Decimal("2")
    assert result.min_bet_payout == Decimal("10")
    assert (
        result.state
        is BetfairCurrencyThresholdState.SNAPSHOT_BELOW_THRESHOLDS
    )
    assert result.current_provider_constraint_proven is False
    assert result.execution_authorized is False

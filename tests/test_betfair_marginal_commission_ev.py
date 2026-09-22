from decimal import Decimal, getcontext, setcontext

import pytest

from autosport.betfair_marginal_commission_ev import (
    BetfairMarginalCommissionEVError,
    BetfairMarketOutcomeEconomicInput,
    calculate_betfair_marginal_commission_ev,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64


def scenario(outcome_id: str, probability: str, existing: str, candidate: str):
    return BetfairMarketOutcomeEconomicInput(
        outcome_id,
        Decimal(probability),
        Decimal(existing),
        Decimal(candidate),
    )


def calculate(outcomes, rate="0.05"):
    return calculate_betfair_marginal_commission_ev(
        account_id="betfair-account-evidence:" + SHA_A,
        market_id="1.23456789",
        currency="GBP",
        effective_commission_rate=Decimal(rate),
        probability_evidence_sha256=SHA_A,
        exposure_evidence_sha256=SHA_B,
        candidate_evidence_sha256=SHA_C,
        commission_rate_evidence_sha256=SHA_D,
        outcomes=outcomes,
    )


def test_market_netting_sign_flip_matches_reference_oracle():
    result = calculate((scenario("home", "0.5", "100", "-80"), scenario("away", "0.5", "-100", "80")))
    assert result.gross_candidate_ev == Decimal("0")
    assert result.marginal_after_commission_ev == Decimal("2")
    assert result.candidate_standalone_after_commission_ev == Decimal("-2")
    assert {row.outcome_id: row.marginal_after_commission_pnl for row in result.outcomes} == {
        "home": Decimal("-76"), "away": Decimal("80")
    }
    assert result.decision_authorized is False


def test_market_level_commission_precedes_expectation():
    result = calculate((scenario("a", "0.2", "50", "25"), scenario("b", "0.3", "-20", "30"), scenario("c", "0.5", "-10", "-10")), rate="0.10")
    assert result.marginal_after_commission_ev == Decimal("8.2")
    rows = {row.outcome_id: row for row in result.outcomes}
    assert rows["a"].base_after_commission_pnl == Decimal("45")
    assert rows["b"].combined_after_commission_pnl == Decimal("9")
    assert rows["c"].combined_after_commission_pnl == Decimal("-20")


def test_losing_outcome_has_no_ordinary_commission():
    result = calculate((scenario("win", "0.25", "-5", "-10"), scenario("lose", "0.75", "0", "0")), rate="0.75")
    row = {item.outcome_id: item for item in result.outcomes}["win"]
    assert row.base_after_commission_pnl == Decimal("-5")
    assert row.combined_after_commission_pnl == Decimal("-15")


def test_zero_commission_reduces_to_gross_candidate_ev():
    result = calculate((scenario("a", "0.4", "123", "7"), scenario("b", "0.6", "-45", "-3")), rate="0")
    assert result.marginal_after_commission_ev == result.gross_candidate_ev


def test_equivalent_decimal_encodings_are_identity_stable():
    first = calculate((scenario("a", "0.50", "100.00", "-80.0"), scenario("b", "0.500", "-100.0", "+80.00")), rate="0.0500")
    second = calculate((scenario("a", "5e-1", "1e2", "-8e1"), scenario("b", "5e-1", "-1e2", "8e1")), rate="5e-2")
    assert first.calculation_sha256 == second.calculation_sha256
    assert first.marginal_after_commission_ev == second.marginal_after_commission_ev


def test_outcome_order_does_not_change_projection_identity():
    first = calculate((scenario("b", "0.6", "-20", "5"), scenario("a", "0.4", "40", "-10")), rate="0.03")
    second = calculate((scenario("a", "0.4", "40", "-10"), scenario("b", "0.6", "-20", "5")), rate="0.03")
    assert first == second
    assert tuple(row.outcome_id for row in first.outcomes) == ("a", "b")


def test_ambient_decimal_precision_cannot_change_result():
    old = getcontext().copy()
    try:
        getcontext().prec = 6
        low = calculate((scenario("a", "0.333333333333333333", "100", "17"), scenario("b", "0.666666666666666667", "-40", "-8.5")), rate="0.0375")
        getcontext().prec = 50
        high = calculate((scenario("a", "0.333333333333333333", "100", "17"), scenario("b", "0.666666666666666667", "-40", "-8.5")), rate="0.0375")
    finally:
        setcontext(old)
    assert low == high


@pytest.mark.parametrize(
    ("outcomes", "message"),
    [
        ((), "between 1 and"),
        ((scenario("a", "0.4", "0", "1"), scenario("b", "0.5", "0", "-1")), "sum exactly to 1"),
        ((scenario("dup", "0.5", "0", "1"), scenario("dup", "0.5", "0", "-1")), "unique"),
    ],
)
def test_invalid_outcome_space_fails_closed(outcomes, message):
    with pytest.raises(BetfairMarginalCommissionEVError, match=message):
        calculate(outcomes)


@pytest.mark.parametrize("rate", ["-0.0001", "1.0001"])
def test_invalid_rate_fails_closed(rate):
    with pytest.raises(BetfairMarginalCommissionEVError, match="between 0 and 1"):
        calculate((scenario("only", "1", "0", "1"),), rate=rate)


def test_float_and_nonfinite_decimal_ingress_fail_closed():
    with pytest.raises(BetfairMarginalCommissionEVError, match="finite Decimal"):
        BetfairMarketOutcomeEconomicInput("a", 0.5, Decimal("0"), Decimal("1"))
    with pytest.raises(BetfairMarginalCommissionEVError, match="finite Decimal"):
        BetfairMarketOutcomeEconomicInput("a", Decimal("1"), Decimal("NaN"), Decimal("1"))


def test_evidence_identity_and_currency_are_strict():
    good = (scenario("only", "1", "0", "1"),)
    kwargs = dict(
        account_id="account", market_id="market", effective_commission_rate=Decimal("0.05"),
        exposure_evidence_sha256=SHA_B, candidate_evidence_sha256=SHA_C,
        commission_rate_evidence_sha256=SHA_D, outcomes=good,
    )
    with pytest.raises(BetfairMarginalCommissionEVError, match="lowercase SHA-256"):
        calculate_betfair_marginal_commission_ev(currency="GBP", probability_evidence_sha256="A" * 64, **kwargs)
    with pytest.raises(BetfairMarginalCommissionEVError, match="uppercase three-letter"):
        calculate_betfair_marginal_commission_ev(currency="gbp", probability_evidence_sha256=SHA_A, **kwargs)
    with pytest.raises(BetfairMarginalCommissionEVError, match="uppercase three-letter"):
        calculate_betfair_marginal_commission_ev(currency=123, probability_evidence_sha256=SHA_A, **kwargs)


def test_oversized_decimal_envelope_fails_before_arithmetic():
    with pytest.raises(BetfairMarginalCommissionEVError, match="bounded Decimal"):
        scenario("only", "1", "1e1001", "0")

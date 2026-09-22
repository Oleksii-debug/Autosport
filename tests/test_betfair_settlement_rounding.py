from __future__ import annotations

import random
from decimal import Decimal, localcontext

import pytest

from autosport.betfair_settlement_rounding import (
    BetfairOrdinarySettlementProjection,
    BetfairSettlementRoundingError,
    MonetaryUncertaintyInterval,
    ThresholdDisposition,
    classify_strictly_above_threshold,
    continuous_after_commission,
    monetary_uncertainty_interval,
    project_ordinary_two_stage_rounding,
)


def test_decisive_half_cent_example_changes_subcent_economics() -> None:
    result = project_ordinary_two_stage_rounding(Decimal("0.005"), Decimal("0.05"))

    assert result.gross_posted_model == Decimal("0.01")
    assert result.commission_posted_model == Decimal("0.00")
    assert result.net_posted_model == Decimal("0.01")
    assert result.continuous_net == Decimal("0.00475")
    assert result.absolute_model_delta == Decimal("0.00525")
    assert result.absolute_model_delta < result.model_error_bound_exclusive


def test_commission_is_rounded_only_after_rounded_positive_gross() -> None:
    result = project_ordinary_two_stage_rounding(Decimal("0.10"), Decimal("0.05"))

    assert result.gross_posted_model == Decimal("0.10")
    assert result.commission_posted_model == Decimal("0.01")
    assert result.net_posted_model == Decimal("0.09")


def test_loss_rounds_half_away_from_zero_and_carries_no_commission() -> None:
    result = project_ordinary_two_stage_rounding(Decimal("-0.005"), Decimal("0.05"))

    assert result.gross_posted_model == Decimal("-0.01")
    assert result.commission_posted_model == Decimal("0.00")
    assert result.net_posted_model == Decimal("-0.01")
    assert result.continuous_net == Decimal("-0.005")


def test_projection_cannot_claim_provider_applicability_or_exact_posted_cash() -> None:
    result = project_ordinary_two_stage_rounding(Decimal("10.125"), Decimal("0.05"))

    assert result.provider_applicability_proven is False
    assert result.provider_posted_exact is False
    assert result.execution_authorized is False
    assert result.real_money_execution is False
    assert "NON_BSP" in result.settlement_model


def test_rate_boundaries_are_supported_exactly() -> None:
    no_commission = project_ordinary_two_stage_rounding(Decimal("1.234"), Decimal("0"))
    full_commission = project_ordinary_two_stage_rounding(Decimal("1.234"), Decimal("1"))

    assert no_commission.net_posted_model == Decimal("1.23")
    assert full_commission.net_posted_model == Decimal("0.00")


@pytest.mark.parametrize("rate", [Decimal("-0.0001"), Decimal("1.0001")])
def test_invalid_commission_rate_fails_closed(rate: Decimal) -> None:
    with pytest.raises(BetfairSettlementRoundingError):
        project_ordinary_two_stage_rounding(Decimal("1"), rate)


@pytest.mark.parametrize("bad", [1, 1.0, True, "1.0"])
def test_binary_or_coerced_money_inputs_fail_closed(bad: object) -> None:
    with pytest.raises(BetfairSettlementRoundingError):
        project_ordinary_two_stage_rounding(bad, Decimal("0.05"))  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_nonfinite_money_fails_closed(bad: Decimal) -> None:
    with pytest.raises(BetfairSettlementRoundingError):
        project_ordinary_two_stage_rounding(bad, Decimal("0.05"))


def test_extreme_decimal_resources_fail_before_large_materialization() -> None:
    with pytest.raises(BetfairSettlementRoundingError):
        project_ordinary_two_stage_rounding(Decimal("1e1000"), Decimal("0.05"))
    with pytest.raises(BetfairSettlementRoundingError):
        project_ordinary_two_stage_rounding(Decimal("1e-1000"), Decimal("0.05"))


def test_arithmetic_is_independent_of_ambient_decimal_precision() -> None:
    expected = project_ordinary_two_stage_rounding(
        Decimal("12345678901234567890.005"), Decimal("0.075"),
    )

    with localcontext() as context:
        context.prec = 3
        context.rounding = "ROUND_DOWN"
        actual = project_ordinary_two_stage_rounding(
            Decimal("12345678901234567890.005"), Decimal("0.075"),
        )

    assert actual == expected


def test_near_one_cent_model_delta_remains_strictly_below_bound() -> None:
    result = project_ordinary_two_stage_rounding(Decimal("0.825"), Decimal("0.006"))

    assert result.absolute_model_delta == Decimal("0.009950")
    assert result.absolute_model_delta < result.model_error_bound_exclusive


def test_structural_two_stage_model_stays_strictly_inside_one_cent() -> None:
    rng = random.Random(20260922)
    for _ in range(10_000):
        gross = Decimal(rng.randint(-10_000_000, 10_000_000)).scaleb(-4)
        rate = Decimal(rng.randint(0, 2000)).scaleb(-4)
        result = project_ordinary_two_stage_rounding(gross, rate)
        assert result.absolute_model_delta < Decimal("0.01")


def test_continuous_model_is_exact_under_low_precision_context() -> None:
    with localcontext() as context:
        context.prec = 2
        value = continuous_after_commission(Decimal("123456.789"), Decimal("0.12345"))

    assert value == Decimal("108216.04839795")


def test_uncertainty_interval_preserves_unknown_provider_rounding() -> None:
    interval = monetary_uncertainty_interval(Decimal("0.00475"), Decimal("0.01"))

    assert interval.lower == Decimal("-0.00525")
    assert interval.upper == Decimal("0.01475")
    assert interval.provider_posted_exact is False
    assert interval.execution_authorized is False


def test_threshold_classification_requires_whole_interval_on_one_side() -> None:
    passing = monetary_uncertainty_interval(Decimal("0.05"), Decimal("0.01"))
    failing = monetary_uncertainty_interval(Decimal("-0.02"), Decimal("0.01"))
    crossing = monetary_uncertainty_interval(Decimal("0.005"), Decimal("0.01"))

    assert classify_strictly_above_threshold(passing, Decimal("0.03")) is ThresholdDisposition.PASS
    assert classify_strictly_above_threshold(failing, Decimal("0")) is ThresholdDisposition.FAIL
    assert (
        classify_strictly_above_threshold(crossing, Decimal("0"))
        is ThresholdDisposition.INDETERMINATE
    )


def test_direct_projection_construction_cannot_forge_model_values() -> None:
    with pytest.raises(BetfairSettlementRoundingError):
        BetfairOrdinarySettlementProjection(
            gross_unrounded=Decimal("0.005"),
            commission_rate=Decimal("0.05"),
            gross_posted_model=Decimal("0.01"),
            commission_posted_model=Decimal("0.00"),
            net_posted_model=Decimal("99.00"),
            continuous_net=Decimal("0.00475"),
            absolute_model_delta=Decimal("0.00525"),
        )


def test_direct_uncertainty_interval_cannot_forge_threshold_bounds() -> None:
    with pytest.raises(BetfairSettlementRoundingError):
        MonetaryUncertaintyInterval(
            center=Decimal("0"),
            epsilon=Decimal("0.01"),
            lower=Decimal("1"),
            upper=Decimal("2"),
        )


def test_zero_epsilon_can_decide_exactly_without_claiming_provider_origin() -> None:
    interval = monetary_uncertainty_interval(Decimal("0.01"), Decimal("0"))

    assert classify_strictly_above_threshold(interval, Decimal("0")) is ThresholdDisposition.PASS
    assert interval.provider_posted_exact is False

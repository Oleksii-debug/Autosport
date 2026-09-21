from decimal import Decimal, localcontext

import pytest

from autosport.betfair_italy_order_admission import (
    ItalianLimitAdmissionState,
    ItalianLimitInstruction,
    ItalianOrderAdmissionError,
    evaluate_italian_limit_batch,
)


def I(side="BACK", size="2.00", price="2.00", target=None, selection_id=1):
    return ItalianLimitInstruction(
        selection_id=selection_id,
        side=side,
        size=Decimal(size),
        price=Decimal(price),
        bet_target_type=target,
    )


def test_back_minimum_and_increment_are_admissible():
    result = evaluate_italian_limit_batch(
        (I(size="2.00"), I(size="2.50", selection_id=2))
    )
    assert result.state is ItalianLimitAdmissionState.ADMISSIBLE
    assert result.reason_codes == ()
    assert result.execution_authorized is False


def test_back_below_two_euros_is_rejected():
    result = evaluate_italian_limit_batch((I(size="1.50"),))
    assert "I0:BACK_STAKE_BELOW_EUR_2" in result.reason_codes


def test_back_non_half_euro_increment_is_rejected():
    result = evaluate_italian_limit_batch((I(size="2.25"),))
    assert "I0:BACK_STAKE_NOT_EUR_0_50_INCREMENT" in result.reason_codes


def test_lay_corresponding_backer_stake_minimum_is_half_euro():
    assert evaluate_italian_limit_batch((I(side="LAY", size="0.50"),)).admissible
    rejected = evaluate_italian_limit_batch((I(side="LAY", size="0.49"),))
    assert "I0:LAY_BACKER_STAKE_BELOW_EUR_0_50" in rejected.reason_codes


def test_fifty_instructions_are_allowed():
    batch = tuple(I(selection_id=i + 1) for i in range(50))
    assert evaluate_italian_limit_batch(batch).admissible


def test_fifty_one_instructions_are_rejected():
    batch = tuple(I(selection_id=i + 1) for i in range(51))
    result = evaluate_italian_limit_batch(batch)
    assert result.reason_codes[0] == "TOO_MANY_INSTRUCTIONS"


def test_mixed_back_and_lay_batch_is_rejected():
    result = evaluate_italian_limit_batch(
        (I("BACK"), I("LAY", size="0.50", selection_id=2))
    )
    assert "MIXED_BACK_LAY_BATCH" in result.reason_codes


@pytest.mark.parametrize("target", ["PAYOUT", "BACKERS_PROFIT"])
def test_target_sizing_is_unavailable_in_italy(target):
    result = evaluate_italian_limit_batch(
        (I(size="0.01", price="10000000", target=target),)
    )
    assert result.reason_codes == ("I0:TARGET_MODE_UNAVAILABLE_IT",)
    assert result.preselected_returns_eur == ()


def test_back_preselected_return_equal_to_10000_is_allowed():
    result = evaluate_italian_limit_batch((I(size="10.00", price="1000"),))
    assert result.admissible
    assert result.preselected_returns_eur == (Decimal("10000.00"),)


def test_back_preselected_return_above_10000_is_rejected():
    result = evaluate_italian_limit_batch((I(size="10.50", price="1000"),))
    assert "I0:PRESELECTED_RETURN_EXCEEDS_EUR_10000" in result.reason_codes


def test_lay_preselected_return_equal_to_10000_is_allowed():
    result = evaluate_italian_limit_batch(
        (I(side="LAY", size="10.00", price="1000"),)
    )
    assert result.admissible


def test_lay_preselected_return_above_10000_is_rejected():
    result = evaluate_italian_limit_batch(
        (I(side="LAY", size="10.01", price="1000"),)
    )
    assert "I0:PRESELECTED_RETURN_EXCEEDS_EUR_10000" in result.reason_codes


def test_empty_batch_is_rejected_without_fabricating_returns():
    result = evaluate_italian_limit_batch(())
    assert result.state is ItalianLimitAdmissionState.REJECTED
    assert result.reason_codes == ("EMPTY_BATCH",)
    assert result.preselected_returns_eur == ()


def test_instruction_collection_must_be_immutable_tuple():
    with pytest.raises(ItalianOrderAdmissionError, match="immutable tuple"):
        evaluate_italian_limit_batch([I()])  # type: ignore[arg-type]


@pytest.mark.parametrize("side", ["back", "LAY ", "", "BUY"])
def test_side_is_exact_and_fail_closed(side):
    with pytest.raises(ItalianOrderAdmissionError, match="exactly BACK or LAY"):
        I(side=side)


@pytest.mark.parametrize(
    "bad",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("-1"),
        Decimal("0"),
    ],
)
def test_nonpositive_or_nonfinite_size_rejected(bad):
    with pytest.raises(ItalianOrderAdmissionError, match="positive finite Decimal"):
        ItalianLimitInstruction(1, "BACK", bad, Decimal("2"))


@pytest.mark.parametrize(
    "bad",
    [
        Decimal("NaN"),
        Decimal("Infinity"),
        Decimal("1"),
        Decimal("0.99"),
    ],
)
def test_invalid_price_rejected(bad):
    with pytest.raises(ItalianOrderAdmissionError):
        ItalianLimitInstruction(1, "BACK", Decimal("2"), bad)


def test_binary_float_is_not_accepted_as_money():
    with pytest.raises(ItalianOrderAdmissionError, match="positive finite Decimal"):
        ItalianLimitInstruction(1, "BACK", 2.0, Decimal("2"))  # type: ignore[arg-type]


def test_unknown_target_mode_is_malformed_not_silently_accepted():
    with pytest.raises(ItalianOrderAdmissionError, match="bet_target_type"):
        I(target="SOMETHING_ELSE")


def test_selection_id_must_be_positive_integer_not_bool():
    with pytest.raises(ItalianOrderAdmissionError, match="selection_id"):
        I(selection_id=True)
    with pytest.raises(ItalianOrderAdmissionError, match="selection_id"):
        I(selection_id=0)


def test_preselected_return_is_independent_of_ambient_decimal_precision():
    instruction = I(size="1234.50", price="8.10")
    with localcontext() as ctx:
        ctx.prec = 3
        result = evaluate_italian_limit_batch((instruction,))
    assert result.admissible
    assert result.preselected_returns_eur == (Decimal("9999.4500"),)


def test_all_reasons_are_preserved_deterministically():
    result = evaluate_italian_limit_batch(
        (
            I("BACK", size="1.25", price="10000", target="PAYOUT"),
            I("LAY", size="0.49", price="30000", selection_id=2),
        )
    )
    assert result.reason_codes == (
        "MIXED_BACK_LAY_BATCH",
        "I0:TARGET_MODE_UNAVAILABLE_IT",
        "I1:LAY_BACKER_STAKE_BELOW_EUR_0_50",
        "I1:PRESELECTED_RETURN_EXCEEDS_EUR_10000",
    )
    assert result.preselected_returns_eur == (Decimal("14700.00"),)

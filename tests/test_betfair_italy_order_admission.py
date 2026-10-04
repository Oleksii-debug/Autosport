from decimal import Decimal, localcontext

import pytest

import autosport.betfair_italy_order_admission as italy_guard
from autosport.betfair_italy_order_admission import (
    ItalianLimitAdmissionState,
    ItalianLimitBatchAdmission,
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


def assert_unbound_match(result):
    assert (
        result.state
        is ItalianLimitAdmissionState.RULESET_SATISFIED_UNBOUND
    )
    assert result.ruleset_satisfied_unbound is True
    assert result.admissible is False
    assert result.jurisdiction_bound is False
    assert result.account_currency_bound is False
    assert result.current_provider_rules_proven is False
    assert result.execution_authorized is False
    assert result.real_money_execution is False


def test_back_minimum_and_increment_match_only_unbound_ruleset():
    result = evaluate_italian_limit_batch(
        (I(size="2.00"), I(size="2.50", selection_id=2))
    )
    assert_unbound_match(result)
    assert result.reason_codes == ()


def test_back_below_two_euros_is_rejected():
    result = evaluate_italian_limit_batch((I(size="1.50"),))
    assert "I0:BACK_STAKE_BELOW_EUR_2" in result.reason_codes


def test_back_non_half_euro_increment_is_rejected():
    result = evaluate_italian_limit_batch((I(size="2.25"),))
    assert "I0:BACK_STAKE_NOT_EUR_0_50_INCREMENT" in result.reason_codes


def test_lay_corresponding_backer_stake_minimum_matches_unbound_ruleset():
    accepted = evaluate_italian_limit_batch(
        (I(side="LAY", size="0.50"),)
    )
    assert_unbound_match(accepted)
    rejected = evaluate_italian_limit_batch(
        (I(side="LAY", size="0.49"),)
    )
    assert "I0:LAY_BACKER_STAKE_BELOW_EUR_0_50" in rejected.reason_codes


def test_fifty_instructions_match_only_unbound_ruleset():
    batch = tuple(I(selection_id=i + 1) for i in range(50))
    assert_unbound_match(evaluate_italian_limit_batch(batch))


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


def test_back_preselected_return_equal_to_10000_matches_unbound_ruleset():
    result = evaluate_italian_limit_batch(
        (I(size="10.00", price="1000"),)
    )
    assert_unbound_match(result)
    assert result.preselected_returns_eur == (Decimal("10000.00"),)


def test_back_preselected_return_above_10000_is_rejected():
    result = evaluate_italian_limit_batch(
        (I(size="10.50", price="1000"),)
    )
    assert "I0:PRESELECTED_RETURN_EXCEEDS_EUR_10000" in result.reason_codes


def test_lay_preselected_return_equal_to_10000_matches_unbound_ruleset():
    result = evaluate_italian_limit_batch(
        (I(side="LAY", size="10.00", price="1000"),)
    )
    assert_unbound_match(result)


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


def test_instruction_collection_must_be_exact_immutable_tuple():
    with pytest.raises(ItalianOrderAdmissionError, match="exact tuple"):
        evaluate_italian_limit_batch([I()])  # type: ignore[arg-type]


def test_instruction_subclass_cannot_enter_exact_projection_contract():
    class DerivedInstruction(ItalianLimitInstruction):
        pass

    value = DerivedInstruction(
        selection_id=1,
        side="BACK",
        size=Decimal("2"),
        price=Decimal("2"),
    )
    with pytest.raises(
        ItalianOrderAdmissionError,
        match="exact ItalianLimitInstruction",
    ):
        evaluate_italian_limit_batch((value,))


@pytest.mark.parametrize("side", ["back", "LAY ", "", "BUY", 1])
def test_side_is_exact_and_fail_closed(side):
    with pytest.raises(ItalianOrderAdmissionError, match="exactly BACK or LAY"):
        ItalianLimitInstruction(
            1,
            side,  # type: ignore[arg-type]
            Decimal("2"),
            Decimal("2"),
        )


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
    with pytest.raises(ItalianOrderAdmissionError, match="exact positive finite Decimal"):
        ItalianLimitInstruction(
            1, "BACK", 2.0, Decimal("2")  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "bad",
    [
        Decimal("1e19"),
        Decimal("1e-19"),
        Decimal("9" * 65),
    ],
)
def test_decimal_shape_is_bounded_before_fraction_or_arithmetic(bad):
    with pytest.raises(ItalianOrderAdmissionError, match="bounded Decimal shape"):
        ItalianLimitInstruction(1, "BACK", bad, Decimal("2"))


def test_price_shape_is_bounded_before_multiplication():
    with pytest.raises(ItalianOrderAdmissionError, match="bounded Decimal shape"):
        ItalianLimitInstruction(
            1, "BACK", Decimal("2"), Decimal("1e19")
        )


def test_unknown_target_mode_is_malformed_not_silently_accepted():
    with pytest.raises(ItalianOrderAdmissionError, match="bet_target_type"):
        I(target="SOMETHING_ELSE")


def test_selection_id_must_be_positive_exact_integer_not_bool():
    with pytest.raises(ItalianOrderAdmissionError, match="selection_id"):
        I(selection_id=True)
    with pytest.raises(ItalianOrderAdmissionError, match="selection_id"):
        I(selection_id=0)


def test_preselected_return_is_independent_of_ambient_decimal_precision():
    instruction = I(size="1234.50", price="8.10")
    with localcontext() as ctx:
        ctx.prec = 3
        result = evaluate_italian_limit_batch((instruction,))
    assert_unbound_match(result)
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


def test_no_positive_provider_admission_state_exists():
    assert {item.value for item in ItalianLimitAdmissionState} == {
        "RULESET_SATISFIED_UNBOUND",
        "REJECTED",
    }


def test_caller_cannot_relabel_unbound_ruleset_result_as_admissible():
    with pytest.raises(ItalianOrderAdmissionError, match="cannot carry"):
        ItalianLimitBatchAdmission(
            state=ItalianLimitAdmissionState.RULESET_SATISFIED_UNBOUND,
            reason_codes=("forged_reason",),
            preselected_returns_eur=(Decimal("4"),),
        )


def test_ruleset_checks_ignore_public_threshold_rebinding(monkeypatch):
    monkeypatch.setattr(italy_guard, "MAX_PLACE_INSTRUCTIONS", 1000)
    monkeypatch.setattr(italy_guard, "BACK_MIN_STAKE_EUR", Decimal("0.01"))
    monkeypatch.setattr(
        italy_guard,
        "BACK_STAKE_INCREMENT_EUR",
        Decimal("0.01"),
    )
    monkeypatch.setattr(
        italy_guard,
        "LAY_MIN_BACKER_STAKE_EUR",
        Decimal("0.01"),
    )
    monkeypatch.setattr(
        italy_guard,
        "MAX_PRESELECTED_RETURN_EUR",
        Decimal("999999999"),
    )

    too_many = tuple(I(selection_id=i + 1) for i in range(51))
    assert (
        evaluate_italian_limit_batch(too_many).reason_codes[0]
        == "TOO_MANY_INSTRUCTIONS"
    )
    assert "I0:BACK_STAKE_BELOW_EUR_2" in evaluate_italian_limit_batch(
        (I(size="1.50"),)
    ).reason_codes
    assert "I0:BACK_STAKE_NOT_EUR_0_50_INCREMENT" in evaluate_italian_limit_batch(
        (I(size="2.25"),)
    ).reason_codes
    assert "I0:LAY_BACKER_STAKE_BELOW_EUR_0_50" in evaluate_italian_limit_batch(
        (I(side="LAY", size="0.49"),)
    ).reason_codes
    assert "I0:PRESELECTED_RETURN_EXCEEDS_EUR_10000" in evaluate_italian_limit_batch(
        (I(size="10.50", price="1000"),)
    ).reason_codes


def test_hard_false_admission_authority_surface_is_sealed():
    result = evaluate_italian_limit_batch((I(),))
    hard_false_names = (
        "admissible",
        "jurisdiction_bound",
        "account_currency_bound",
        "current_provider_rules_proven",
        "execution_authorized",
        "real_money_execution",
    )

    for name in hard_false_names:
        descriptor = ItalianLimitBatchAdmission.__dict__[name]
        assert isinstance(descriptor, property)
        assert descriptor.fget is not None
        assert not hasattr(descriptor.fget, "__code__")
        assert getattr(result, name) is False

    with pytest.raises(TypeError, match="authority surface is sealed"):
        ItalianLimitBatchAdmission.execution_authorized = property(
            lambda _self: True
        )

    with pytest.raises(TypeError, match="authority surface is sealed"):
        ItalianLimitBatchAdmission._execution_authorized_constant = True

    with pytest.raises(AttributeError):
        object.__setattr__(result, "_execution_authorized_constant", True)

    with pytest.raises(AttributeError):
        object.__setattr__(result, "real_money_execution", True)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("selection_id", 0, "selection_id"),
        ("side", "BUY", "side"),
        ("size", Decimal("0"), "size"),
        ("price", Decimal("1"), "price"),
        ("bet_target_type", "UNKNOWN_TARGET", "bet_target_type"),
    ],
)
def test_evaluation_revalidates_post_construction_slot_mutation(
    field, value, message
):
    instruction = I()
    # frozen dataclasses are not an integrity boundary: object.__setattr__
    # can still write slots, so evaluation must validate its own snapshot.
    object.__setattr__(instruction, field, value)

    with pytest.raises(ItalianOrderAdmissionError, match=message):
        evaluate_italian_limit_batch((instruction,))


def test_overlimit_batch_rejects_before_member_validation():
    # Once the request already violates the provider's 50-instruction shape,
    # no caller-controlled member scan is needed to reach a fail-closed result.
    result = evaluate_italian_limit_batch((object(),) * 51)  # type: ignore[arg-type]
    assert result.state is ItalianLimitAdmissionState.REJECTED
    assert result.reason_codes == ("TOO_MANY_INSTRUCTIONS",)
    assert result.preselected_returns_eur == ()


def test_exact_return_arithmetic_ignores_ambient_decimal_exponent_limits():
    instruction = I(size="1234.50", price="8.10")
    with localcontext() as ctx:
        ctx.prec = 3
        ctx.Emax = 2
        ctx.Emin = -2
        result = evaluate_italian_limit_batch((instruction,))

    assert result.state is ItalianLimitAdmissionState.RULESET_SATISFIED_UNBOUND
    assert result.preselected_returns_eur == (Decimal("9999.4500"),)


def test_evaluator_captures_authority_dependencies_against_module_rebinding(
    monkeypatch,
):
    instruction = I()
    monkeypatch.setattr(italy_guard, "ItalianLimitInstruction", object)
    monkeypatch.setattr(italy_guard, "ItalianLimitBatchAdmission", object)
    monkeypatch.setattr(italy_guard, "ItalianLimitAdmissionState", object)
    monkeypatch.setattr(italy_guard, "Decimal", str)
    monkeypatch.setattr(italy_guard, "Fraction", object)
    monkeypatch.setattr(italy_guard, "_MAX_DECIMAL_DIGITS", 1)
    monkeypatch.setattr(italy_guard, "_MAX_ABS_EXPONENT", 0)

    result = evaluate_italian_limit_batch((instruction,))
    assert result.state is ItalianLimitAdmissionState.RULESET_SATISFIED_UNBOUND
    assert result.reason_codes == ()
    assert result.admissible is False
    assert result.execution_authorized is False
    assert result.real_money_execution is False


def test_evaluator_uses_captured_instruction_slot_descriptors(monkeypatch):
    instruction = I(side="LAY", size="0.49")
    monkeypatch.setattr(ItalianLimitInstruction, "side", "BACK")
    monkeypatch.setattr(ItalianLimitInstruction, "size", Decimal("100"))

    result = evaluate_italian_limit_batch((instruction,))
    assert result.state is ItalianLimitAdmissionState.REJECTED
    assert result.reason_codes == ("I0:LAY_BACKER_STAKE_BELOW_EUR_0_50",)


def test_result_diagnostic_state_slots_are_class_sealed():
    for name, value in (
        ("state", ItalianLimitAdmissionState.RULESET_SATISFIED_UNBOUND),
        ("reason_codes", ()),
        ("preselected_returns_eur", (Decimal("1"),)),
        ("ruleset_satisfied_unbound", property(lambda _self: True)),
    ):
        with pytest.raises(TypeError, match="authority surface is sealed"):
            setattr(ItalianLimitBatchAdmission, name, value)


def test_result_diagnostic_state_is_instance_immutable():
    result = evaluate_italian_limit_batch((I(size="1.50"),))
    assert result.state is ItalianLimitAdmissionState.REJECTED

    with pytest.raises(AttributeError):
        object.__setattr__(
            result,
            "state",
            ItalianLimitAdmissionState.RULESET_SATISFIED_UNBOUND,
        )
    with pytest.raises(AttributeError):
        object.__setattr__(result, "reason_codes", ())
    with pytest.raises(AttributeError):
        object.__setattr__(
            result,
            "preselected_returns_eur",
            (Decimal("4"),),
        )

    assert result.state is ItalianLimitAdmissionState.REJECTED
    assert result.ruleset_satisfied_unbound is False
    assert "I0:BACK_STAKE_BELOW_EUR_2" in result.reason_codes
    assert result.execution_authorized is False


def test_ruleset_match_does_not_mint_price_ladder_authority():
    # 2.01 is intentionally outside this module's authority question: the
    # canonical price-ladder/tick owner (#1504 family) must decide it.
    result = evaluate_italian_limit_batch((I(size="2.00", price="2.01"),))

    assert result.state is ItalianLimitAdmissionState.RULESET_SATISFIED_UNBOUND
    assert result.reason_codes == ()
    assert result.ruleset_satisfied_unbound is True
    assert result.admissible is False
    assert result.current_provider_rules_proven is False
    assert result.execution_authorized is False
    assert result.real_money_execution is False

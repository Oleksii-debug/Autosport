from __future__ import annotations

import dis
from functools import partial
from types import FunctionType

import pytest

import autosport.betfair_commission_applicability as applicability


def _closure_value(function: FunctionType, name: str):
    closure = function.__closure__ or ()
    freevars = function.__code__.co_freevars
    assert name in freevars
    return closure[freevars.index(name)].cell_contents


def _authority_witness(function: FunctionType) -> tuple[FunctionType, object]:
    matches = tuple(
        item
        for item in function.__code__.co_consts
        if isinstance(item, tuple)
        and len(item) == 2
        and type(item[0]) is FunctionType
        and item[0].__name__ == "require_executable_authority"
    )
    assert len(matches) == 1
    authority, authority_code = matches[0]
    assert type(authority) is FunctionType
    return authority, authority_code


def _hostile_fee_input_reader(_client, *, market_id: str):
    raise AssertionError(f"hostile fee-input reader executed for {market_id}")


def test_in_place_fee_input_reader_code_retarget_fails_before_provider_ingress() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    reader = _closure_value(public_assess, "fee_input_reader")
    captured_function = _closure_value(public_assess, "fee_input_reader_function")
    captured_code = _closure_value(public_assess, "fee_input_reader_function_code")

    assert type(reader) is partial
    assert reader.func is captured_function
    assert captured_function.__code__ is captured_code
    original_code = captured_function.__code__
    try:
        captured_function.__code__ = _hostile_fee_input_reader.__code__
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="fee input reader executable authority changed",
        ):
            public_assess(object(), market_id="1.234")
    finally:
        captured_function.__code__ = original_code


def test_fee_input_reader_partial_keyword_mutation_fails_before_provider_ingress() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    reader = _closure_value(public_assess, "fee_input_reader")

    assert type(reader) is partial
    assert reader.keywords == {}
    reader.keywords["hostile"] = True
    try:
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="fee input reader executable authority changed",
        ):
            public_assess(object(), market_id="1.234")
    finally:
        reader.keywords.pop("hostile")


def test_assessment_boundary_code_constant_witnesses_executable_graph_around_provider_call() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    authority, authority_code = _authority_witness(public_assess)

    assert authority.__code__ is authority_code
    assert "require_executable_authority" not in public_assess.__code__.co_freevars

    instructions = tuple(dis.get_instructions(public_assess))
    direct_authority_calls = 0
    for index, instruction in enumerate(instructions):
        if instruction.opname != "LOAD_FAST" or instruction.argval != "authority":
            continue
        following = instructions[index + 1 : index + 4]
        if any(item.opname == "LOAD_ATTR" for item in following):
            continue
        if any(item.opname == "CALL" for item in following):
            direct_authority_calls += 1

    # Four calls cover pre-read, post-read, post fee-input identity derivation and
    # post assessment-id derivation. The guard itself is loaded from a code-constant
    # witness so writable closure cells cannot replace the guard identity/code pair.
    assert direct_authority_calls == 4
    authority()

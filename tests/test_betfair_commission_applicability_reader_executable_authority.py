from __future__ import annotations

import dis
from types import FunctionType

import pytest

import autosport.betfair_commission_applicability as applicability


def _closure_value(function: FunctionType, name: str):
    closure = function.__closure__ or ()
    freevars = function.__code__.co_freevars
    assert name in freevars
    return closure[freevars.index(name)].cell_contents


def _hostile_fee_input_reader(_client, *, market_id: str):
    raise AssertionError(f"hostile fee-input reader executed for {market_id}")


def test_in_place_fee_input_reader_code_retarget_fails_before_provider_ingress() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    reader = _closure_value(public_assess, "fee_input_reader")
    captured_code = _closure_value(public_assess, "fee_input_reader_code")

    assert type(reader) is FunctionType
    assert reader.__code__ is captured_code
    original_code = reader.__code__
    try:
        reader.__code__ = _hostile_fee_input_reader.__code__
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="fee input reader executable authority changed",
        ):
            public_assess(object(), market_id="1.234")
    finally:
        reader.__code__ = original_code


def test_assessment_boundary_witnesses_reader_before_and_after_provider_call() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    authority = _closure_value(public_assess, "require_reader_authority")

    assert type(authority) is FunctionType
    calls = [
        instruction
        for instruction in dis.get_instructions(public_assess)
        if instruction.opname == "LOAD_DEREF"
        and instruction.argval == "require_reader_authority"
    ]
    # The verifier is loaded for calls on both sides of the provider read. This freezes
    # both pre-call retargeting and a hostile/self-restoring mutation during the read.
    assert len(calls) == 2
    authority()

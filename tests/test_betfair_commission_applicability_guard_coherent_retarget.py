from __future__ import annotations

from types import FunctionType

import pytest

import autosport.betfair_commission_applicability as applicability


def _closure_cell(function: FunctionType, name: str):
    closure = function.__closure__ or ()
    freevars = function.__code__.co_freevars
    assert name in freevars
    return closure[freevars.index(name)]


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


def _hostile_reader(*_args: object, **_kwargs: object):
    raise RuntimeError("hostile fee-input reader reached")


def test_guard_rejects_coherent_reader_code_and_expected_witness_retarget() -> None:
    """Expected reader code must not be caller-mutable guard authority.

    The public boundary anchors the guard function/code in its own code constants, but
    the guard historically kept the expected provider-reader code in a writable closure
    cell.  Retargeting both the reader and that expected-code cell coherently must fail
    closed before the hostile provider reader can execute.
    """

    public_assess = applicability.assess_betfair_commission_applicability
    authority, authority_code = _authority_witness(public_assess)
    assert authority.__code__ is authority_code

    reader = _closure_cell(public_assess, "fee_input_reader").cell_contents
    reader_function = _closure_cell(
        public_assess, "fee_input_reader_function"
    ).cell_contents
    expected_code_cell = _closure_cell(
        authority, "fee_input_reader_function_code"
    )

    assert reader.func is reader_function
    original_code = reader_function.__code__
    original_expected_code = expected_code_cell.cell_contents
    hostile_code = _hostile_reader.__code__
    assert hostile_code.co_freevars == original_code.co_freevars

    try:
        reader_function.__code__ = hostile_code
        expected_code_cell.cell_contents = hostile_code
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="fee input reader executable authority changed",
        ):
            public_assess(object(), market_id="1.234")
    finally:
        expected_code_cell.cell_contents = original_expected_code
        reader_function.__code__ = original_code

from __future__ import annotations

from functools import partial
from types import FunctionType

import pytest

import autosport.betfair_commission_applicability as applicability


def _closure_value(function: FunctionType, name: str):
    closure = function.__closure__ or ()
    freevars = function.__code__.co_freevars
    assert name in freevars
    return closure[freevars.index(name)].cell_contents


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
    return authority, authority_code


def _provider_number_partial(public_assess: FunctionType) -> partial:
    reader = _closure_value(public_assess, "fee_input_reader")
    assert type(reader) is partial
    # `_read_betfair_execution_fee_inputs` binds provider_number at positional
    # capability index 9.
    provider_number = reader.args[9]
    assert type(provider_number) is partial
    return provider_number


def _hostile_provider_number(_error_type, decimal_type, _value, _key, _field):
    return decimal_type("99")


def test_nested_fee_reader_partial_code_retarget_fails_before_provider_ingress() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    provider_number = _provider_number_partial(public_assess)
    nested_function = provider_number.func
    assert type(nested_function) is FunctionType

    original_code = nested_function.__code__
    try:
        nested_function.__code__ = _hostile_provider_number.__code__
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="fee input reader nested executable authority changed",
        ):
            # The nested graph fence must fire before client/provider ingress. Without
            # it, this reaches the reader and fails for the unrelated non-client value.
            public_assess(object(), market_id="1.234")
    finally:
        nested_function.__code__ = original_code


def test_nested_reader_witness_closure_retarget_does_not_disable_code_constant_fence() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    authority, authority_code = _authority_witness(public_assess)
    assert authority.__code__ is authority_code

    partial_witness_cell = _closure_cell(authority, "reader_partial_witnesses")
    function_witness_cell = _closure_cell(authority, "reader_function_witnesses")
    original_partial_witnesses = partial_witness_cell.cell_contents
    original_function_witnesses = function_witness_cell.cell_contents

    provider_number = _provider_number_partial(public_assess)
    nested_function = provider_number.func
    assert type(nested_function) is FunctionType
    original_code = nested_function.__code__
    try:
        # These freevars remain for diagnostics/adversarial visibility only. The
        # authority-bearing immutable graph is embedded in the guard code constants.
        partial_witness_cell.cell_contents = ()
        function_witness_cell.cell_contents = ()
        nested_function.__code__ = _hostile_provider_number.__code__
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="fee input reader nested executable authority changed",
        ):
            public_assess(object(), market_id="1.234")
    finally:
        nested_function.__code__ = original_code
        partial_witness_cell.cell_contents = original_partial_witnesses
        function_witness_cell.cell_contents = original_function_witnesses


def test_nested_fee_reader_partial_keyword_mutation_fails_before_provider_ingress() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    provider_number = _provider_number_partial(public_assess)

    assert provider_number.keywords == {}
    hostile = object()
    provider_number.keywords["hostile"] = hostile
    try:
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="fee input reader nested executable authority changed",
        ):
            public_assess(object(), market_id="1.234")
    finally:
        assert provider_number.keywords.pop("hostile") is hostile

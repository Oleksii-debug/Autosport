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


def _hostile_provider_number(_error_type, decimal_type, _value, _key, _field):
    return decimal_type("99")


def test_nested_fee_reader_partial_code_retarget_fails_before_provider_ingress() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    reader = _closure_value(public_assess, "fee_input_reader")

    assert type(reader) is partial
    # `_read_betfair_execution_fee_inputs` binds provider_number at positional
    # capability index 9. The outer reader/args identities stay unchanged if that
    # nested partial's Python function code is replaced in-place.
    provider_number = reader.args[9]
    assert type(provider_number) is partial
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

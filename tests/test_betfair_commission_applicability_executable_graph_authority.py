from __future__ import annotations

from types import FunctionType

import pytest

import autosport.betfair_commission_applicability as applicability


def _closure_value(function: FunctionType, name: str):
    closure = function.__closure__ or ()
    freevars = function.__code__.co_freevars
    assert name in freevars
    return closure[freevars.index(name)].cell_contents


def _hostile_canonical_json(_payload: object) -> bytes:
    return b"hostile-constant-identity"


def _hostile_identity_payload(_assessment, *, schema: str, schema_version: int):
    return {"schema": schema, "schema_version": schema_version}


def _hostile_fee_payload(_observation):
    return {"market_id": "caller-retargeted"}


def _hostile_authority_noop() -> None:
    return None


@pytest.mark.parametrize(
    ("closure_name", "hostile_code", "message"),
    (
        (
            "canonical_json",
            _hostile_canonical_json.__code__,
            "assessment identity executable authority changed",
        ),
        (
            "identity_payload_builder",
            _hostile_identity_payload.__code__,
            "assessment identity executable authority changed",
        ),
        (
            "fee_input_payload_builder",
            _hostile_fee_payload.__code__,
            "fee input identity executable authority changed",
        ),
    ),
)
def test_captured_identity_python_code_retarget_fails_before_provider_read(
    closure_name: str,
    hostile_code,
    message: str,
) -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    target = _closure_value(public_assess, closure_name)
    assert type(target) is FunctionType
    original_code = target.__code__
    try:
        target.__code__ = hostile_code
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match=message,
        ):
            public_assess(object(), market_id="1.234")
    finally:
        target.__code__ = original_code


def test_reachable_executable_guard_code_retarget_fails_before_boundary_dispatch() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    public_require = applicability.require_product_betfair_commission_applicability
    authority = _closure_value(public_assess, "require_executable_authority")
    assert authority is _closure_value(public_require, "require_executable_authority")
    assert type(authority) is FunctionType
    original_code = authority.__code__
    try:
        authority.__code__ = _hostile_authority_noop.__code__
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="commission applicability executable authority guard changed",
        ):
            public_assess(object(), market_id="1.234")
        with pytest.raises(
            applicability.BetfairCommissionApplicabilityError,
            match="commission applicability executable authority guard changed",
        ):
            public_require(object())
    finally:
        authority.__code__ = original_code


def test_fee_input_hash_partial_binding_is_witnessed_exactly() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    authority = _closure_value(public_assess, "require_executable_authority")
    fee_payload_builder = _closure_value(public_assess, "fee_payload_builder")
    fee_payload_function = _closure_value(public_assess, "fee_payload_function")
    fee_payload_args = _closure_value(public_assess, "fee_payload_args")

    assert fee_payload_builder.func is fee_payload_function
    assert fee_payload_builder.args == fee_payload_args
    authority()

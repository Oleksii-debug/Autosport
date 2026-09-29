from __future__ import annotations

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


def _compatible_hostile_guard_code(function: FunctionType):
    """Build hostile code assignable to a closure-bearing target function.

    CPython rejects ``function.__code__`` replacement when the replacement code's
    freevar count differs from the target closure size. The adversarial fixture must
    therefore preserve the exact freevar topology or it only tests the interpreter's
    setter guard rather than Autosport's executable-authority boundary.
    """

    freevars = function.__code__.co_freevars
    assignments = "\n".join(f"    {name} = None" for name in freevars)
    references = ", ".join(freevars)
    source = (
        "def factory():\n"
        f"{assignments}\n"
        "    def hostile():\n"
        f"        _ = ({references},)\n"
        "        raise RuntimeError('hostile executable authority guard reached')\n"
        "    return hostile\n"
    )
    namespace: dict[str, object] = {}
    exec(source, namespace)  # noqa: S102 - deterministic adversarial test fixture.
    hostile = namespace["factory"]()
    assert type(hostile) is FunctionType
    assert hostile.__code__.co_freevars == freevars
    return hostile.__code__


def _hostile_canonical_json(_payload: object) -> bytes:
    return b"hostile-constant-identity"


def _hostile_identity_payload(_assessment, *, schema: str, schema_version: int):
    return {"schema": schema, "schema_version": schema_version}


def _hostile_fee_payload(_observation):
    return {"market_id": "caller-retargeted"}


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


def test_reachable_executable_guard_is_code_constant_anchored_and_retarget_fails() -> None:
    public_assess = applicability.assess_betfair_commission_applicability
    public_require = applicability.require_product_betfair_commission_applicability

    assert "require_executable_authority" not in public_assess.__code__.co_freevars
    assert "require_executable_authority_code" not in public_assess.__code__.co_freevars
    assert "require_executable_authority" not in public_require.__code__.co_freevars
    assert "require_executable_authority_code" not in public_require.__code__.co_freevars

    authority, authority_code = _authority_witness(public_assess)
    require_authority, require_authority_code = _authority_witness(public_require)
    assert require_authority is authority
    assert require_authority_code is authority_code
    assert authority.__code__ is authority_code

    original_code = authority.__code__
    hostile_code = _compatible_hostile_guard_code(authority)
    try:
        authority.__code__ = hostile_code
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
    authority, authority_code = _authority_witness(public_assess)
    fee_payload_builder = _closure_value(public_assess, "fee_payload_builder")
    fee_payload_function = _closure_value(public_assess, "fee_payload_function")
    fee_payload_args = _closure_value(public_assess, "fee_payload_args")

    assert authority.__code__ is authority_code
    assert fee_payload_builder.func is fee_payload_function
    assert fee_payload_builder.args == fee_payload_args
    authority()

from __future__ import annotations

from decimal import Decimal
from types import FunctionType

from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


def _reconstruct_pre_wrapper_evaluate() -> FunctionType:
    """Recover the already-published raw evaluate spec used by the active wrappers."""

    public_root = vars(PaperRiskPolicy)["evaluate"]
    assert type(public_root) is FunctionType
    outer_spec = public_root.__globals__["_EVALUATE_SPEC"]
    assert type(outer_spec) is tuple and len(outer_spec) == 6
    outer_globals = dict(outer_spec[5])
    nested_spec = outer_globals["_EVALUATE_SPEC"]
    assert type(nested_spec) is tuple and len(nested_spec) == 6

    code, name, defaults, kwdefaults, closure, global_bindings = nested_spec
    assert type(global_bindings) is tuple
    reconstructed = FunctionType(
        code,
        dict(global_bindings),
        name=name,
        argdefs=defaults,
        closure=closure,
    )
    if kwdefaults is not None:
        reconstructed.__kwdefaults__ = dict(kwdefaults)
    return reconstructed


def _bounded_policy() -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("0.10"),
        max_committed_fraction=Decimal("0.10"),
        minimum_cash_reserve_fraction=Decimal("0"),
    )


def _permissive_fraction_limits(self) -> tuple[Decimal, Decimal]:
    del self
    return Decimal("1"), Decimal("1")


def test_reconstructed_evaluate_rejects_transitive_fraction_limit_descriptor_retarget() -> None:
    """First-level _derived_risk_values witness must cover its class dispatch graph."""

    reconstructed = _reconstruct_pre_wrapper_evaluate()
    policy = _bounded_policy()
    book = PaperBook("100")
    amount = Decimal("50")

    assert reconstructed(policy, book, amount).allowed is False

    original = vars(PaperRiskPolicy)["_effective_fraction_limits"]
    assert type(original) is FunctionType
    type.__setattr__(PaperRiskPolicy, "_effective_fraction_limits", _permissive_fraction_limits)
    try:
        decision = reconstructed(policy, book, amount)
        assert decision.allowed is False, (
            "reconstructed evaluate accepted a stake after transitive risk-helper "
            "descriptor retargeting"
        )
    finally:
        type.__setattr__(PaperRiskPolicy, "_effective_fraction_limits", original)


def test_reconstructed_evaluate_rejects_transitive_fraction_limit_code_retarget() -> None:
    """In-place helper code mutation must not preserve reconstructed evaluate authority."""

    reconstructed = _reconstruct_pre_wrapper_evaluate()
    policy = _bounded_policy()
    book = PaperBook("100")
    amount = Decimal("50")

    assert reconstructed(policy, book, amount).allowed is False

    helper = vars(PaperRiskPolicy)["_effective_fraction_limits"]
    assert type(helper) is FunctionType
    original_code = helper.__code__
    helper.__code__ = _permissive_fraction_limits.__code__
    try:
        decision = reconstructed(policy, book, amount)
        assert decision.allowed is False, (
            "reconstructed evaluate accepted a stake after transitive risk-helper "
            "in-place code retargeting"
        )
    finally:
        helper.__code__ = original_code

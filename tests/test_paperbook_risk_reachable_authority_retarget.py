from __future__ import annotations

from decimal import Decimal
from types import FunctionType

import pytest

from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, RiskDecision


def _noop_validator(cls, book) -> None:
    del cls, book


def _noop_graph_check(*args, **kwargs) -> None:
    del args, kwargs


def _descriptor_in_mro(owner: type, name: str) -> object:
    for candidate in owner.__mro__:
        namespace = vars(candidate)
        if name in namespace:
            return namespace[name]
    raise AssertionError(f"missing canonical descriptor: {name}")


def test_public_risk_wrapper_cannot_retarget_reachable_generation_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested public risk specs must not expose mutable generation admission helpers."""

    book = PaperBook("100")
    # Empty opening/causal history is still product-issued and therefore passes the
    # outer private-authority guard. Only canonical loaded-state validation can reject
    # this impossible balance.
    book.balance = Decimal("999")

    monkeypatch.setattr(
        PaperBook,
        "_validate_loaded_state",
        classmethod(_noop_validator),
    )

    # The owner-facing root seal intentionally publishes a zero-state subclass facade;
    # private read roots remain inherited from the already-composed canonical base.
    # Resolve the actual descriptor through the MRO so this falsifier cannot turn RED
    # merely because the root facade moved the descriptor off vars(PaperRiskPolicy).
    descriptor = _descriptor_in_mro(PaperRiskPolicy, "_book_state")
    assert type(descriptor) is classmethod
    public_wrapper = descriptor.__func__
    assert type(public_wrapper) is FunctionType

    # The private-authority wrapper stores the prior generation wrapper as a delegate
    # spec. Its global-bindings tuple currently exposes the live inner guard function,
    # whose __globals__ is the generation guard's mutable authority mapping.
    nested_spec = public_wrapper.__globals__["_BOOK_STATE_SPEC"]
    assert type(nested_spec) is tuple and len(nested_spec) == 6
    nested_global_bindings = nested_spec[5]
    assert type(nested_global_bindings) is tuple
    nested_globals = dict(nested_global_bindings)
    inner_guard = nested_globals["_guarded_risk_call"]
    assert type(inner_guard) is FunctionType

    generation_globals = inner_guard.__globals__
    monkeypatch.setitem(
        generation_globals,
        "_FROZEN_VALIDATE_LOADED_STATE",
        _noop_validator,
    )
    monkeypatch.setitem(
        generation_globals,
        "_FROZEN_REQUIRE_CLASS_GRAPH",
        _noop_graph_check,
    )

    # Retargeting helpers reachable from a public risk callable must never turn
    # structurally impossible caller-authored economics into a positive risk state.
    assert PaperRiskPolicy._book_state(book) is None


def test_sealed_owner_root_cannot_retarget_wrapper_global_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Class-level sealing must also protect the executable helper dispatch beneath it."""

    book = PaperBook("100")
    policy = PaperRiskPolicy()
    root = vars(PaperRiskPolicy)["evaluate"]
    assert type(root) is FunctionType
    hostile_executed = False

    def hostile_private_guard(*args, **kwargs):
        nonlocal hostile_executed
        del args, kwargs
        hostile_executed = True
        return RiskDecision(True, "hostile wrapper-global bypass")

    # Adding/replacing a global is deliberately used rather than replacing the sealed
    # class attribute. A real root seal must not leave its positive decision dispatch
    # controlled by the mutable __globals__ dictionary of the exported Python function.
    monkeypatch.setitem(root.__globals__, "_guarded_private_call", hostile_private_guard)

    decision = policy.evaluate(book, Decimal("99"))
    assert hostile_executed is False
    assert decision.allowed is False

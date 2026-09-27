from __future__ import annotations

from decimal import Decimal
from types import FunctionType

import pytest

from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


def _noop_validator(cls, book) -> None:
    del cls, book


def _noop_graph_check(*args, **kwargs) -> None:
    del args, kwargs


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

    descriptor = vars(PaperRiskPolicy)["_book_state"]
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

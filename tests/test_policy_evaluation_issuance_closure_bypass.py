from __future__ import annotations

from types import FunctionType

import pytest

import autosport.external_validity_policy_issuance as issuance


def _closure_values(function: FunctionType) -> tuple[object, ...]:
    closure = function.__closure__ or ()
    captured: list[object] = []
    for cell in closure:
        try:
            captured.append(cell.cell_contents)
        except ValueError:
            continue
    return tuple(captured)


def _closure_functions(function: FunctionType) -> tuple[FunctionType, ...]:
    return tuple(
        value for value in _closure_values(function) if isinstance(value, FunctionType)
    )


def test_product_policy_evaluation_guards_do_not_expose_original_authority_callables() -> None:
    """Public guard reflection must not recover an unguarded issuance entrypoint.

    Python closure cells are caller-inspectable. A guard that retains the original
    issuer/resolver/verifier/open callable in a closure therefore hands callers a
    direct bypass around the dispatch checks it is supposed to enforce.
    """

    guarded_entries = (
        issuance._open_canonical_authorities,
        issuance.issue_product_policy_evaluation,
        issuance.resolve_product_policy_evaluation,
        issuance.verify_product_policy_evaluation,
    )
    authority_names = {
        "_open_canonical_authorities",
        "issue_product_policy_evaluation",
        "resolve_product_policy_evaluation",
        "verify_product_policy_evaluation",
    }

    exposed: list[str] = []
    for guarded in guarded_entries:
        for captured in _closure_functions(guarded):
            if (
                captured.__module__ == "autosport.external_validity_policy_issuance"
                and captured.__name__ in authority_names
            ):
                exposed.append(f"{guarded.__name__}->{captured.__name__}")

    assert exposed == [], (
        "dispatch guard exposes unguarded product issuance authority through "
        f"inspectable closure cells: {sorted(exposed)}"
    )


def test_reflected_dispatch_state_does_not_offer_an_ordinary_predecessor_escape() -> None:
    values = _closure_values(issuance.issue_product_policy_evaluation)
    states = tuple(value for value in values if type(value).__name__ == "_DispatchState")

    assert len(states) == 1
    state = states[0]
    assert not hasattr(state, "_predecessor")

    for name in (
        "_DispatchState__original_open",
        "_DispatchState__original_issue",
        "_DispatchState__original_resolve",
        "_DispatchState__original_verify",
    ):
        with pytest.raises(AttributeError, match="not exposed"):
            getattr(state, name)

    with pytest.raises(RuntimeError, match="already sealed"):
        state.bind_public(None, None, None, None)

    with pytest.raises(AttributeError, match="state is sealed"):
        state._public_open = None

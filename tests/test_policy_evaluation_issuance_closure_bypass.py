from __future__ import annotations

from datetime import datetime, timezone
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


def _dispatch_state():
    values = _closure_values(issuance.issue_product_policy_evaluation)
    states: list[object] = []
    for value in values:
        candidate = value
        if type(candidate).__name__ != "_DispatchState":
            candidate = getattr(value, "__self__", None)
        if (
            candidate is not None
            and type(candidate).__name__ == "_DispatchState"
            and not any(candidate is prior for prior in states)
        ):
            states.append(candidate)
    assert len(states) == 1
    return states[0]


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
    state = _dispatch_state()
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


def test_trusted_clock_callable_does_not_late_read_mutable_dispatch_state() -> None:
    state = _dispatch_state()
    clock = object.__getattribute__(state, "_trusted_clock_callable")
    original_datetime = object.__getattribute__(state, "_trusted_datetime")
    original_utc = object.__getattribute__(state, "_trusted_utc")
    called = False

    class ForgedDateTime:
        @classmethod
        def now(cls, tz=None):
            nonlocal called
            del cls, tz
            called = True
            raise AssertionError("mutable dispatch-state clock must not run")

    object.__setattr__(state, "_trusted_datetime", ForgedDateTime)
    object.__setattr__(state, "_trusted_utc", object())
    try:
        observed = clock()
    finally:
        object.__setattr__(state, "_trusted_datetime", original_datetime)
        object.__setattr__(state, "_trusted_utc", original_utc)

    assert getattr(clock, "__self__", None) is None
    assert type(observed) is datetime
    assert observed.tzinfo is not None
    assert observed.utcoffset() == timezone.utc.utcoffset(observed)
    assert called is False


def test_object_setattr_clock_rebind_fails_against_external_state_witness() -> None:
    state = _dispatch_state()
    original = object.__getattribute__(state, "_trusted_datetime")
    called = False

    class ForgedDateTime:
        @classmethod
        def now(cls, tz=None):
            nonlocal called
            del cls, tz
            called = True
            raise AssertionError("forged clock must not run")

    object.__setattr__(state, "_trusted_datetime", ForgedDateTime)
    try:
        with pytest.raises(
            issuance.ProductPolicyEvaluationIssuanceError,
            match="dispatch state instance",
        ):
            issuance._open_canonical_authorities(None)
    finally:
        object.__setattr__(state, "_trusted_datetime", original)

    assert called is False


def test_object_setattr_helper_and_module_rebind_cannot_move_witness_in_lockstep() -> None:
    state = _dispatch_state()
    original_state = object.__getattribute__(state, "_derive")
    original_module = issuance._derive_policy_evaluation
    called = False

    def forged_derive(*args, **kwargs):
        nonlocal called
        del args, kwargs
        called = True
        raise AssertionError("forged derive must not run")

    object.__setattr__(state, "_derive", forged_derive)
    issuance._derive_policy_evaluation = forged_derive
    try:
        with pytest.raises(
            issuance.ProductPolicyEvaluationIssuanceError,
            match="dispatch state instance",
        ):
            issuance.issue_product_policy_evaluation(
                None,
                None,
                source_evaluation_bundle_id="caller-forged",
            )
    finally:
        issuance._derive_policy_evaluation = original_module
        object.__setattr__(state, "_derive", original_state)

    assert called is False


def test_object_setattr_predecessor_rebind_fails_before_forged_dispatch() -> None:
    state = _dispatch_state()
    slot = "_DispatchState__original_issue"
    original = object.__getattribute__(state, slot)
    called = False

    def forged_issue(*args, **kwargs):
        nonlocal called
        del args, kwargs
        called = True
        raise AssertionError("forged predecessor must not run")

    object.__setattr__(state, slot, forged_issue)
    try:
        with pytest.raises(
            issuance.ProductPolicyEvaluationIssuanceError,
            match="dispatch state instance",
        ):
            issuance.issue_product_policy_evaluation(
                None,
                None,
                source_evaluation_bundle_id="caller-forged",
            )
    finally:
        object.__setattr__(state, slot, original)

    assert called is False


def test_reflected_original_issue_still_crosses_current_dispatch_choke_point() -> None:
    state = _dispatch_state()
    original_issue = object.__getattribute__(
        state,
        "_DispatchState__original_issue",
    )
    original_derive = issuance._derive_policy_evaluation
    called = False

    def forged_derive(*args, **kwargs):
        nonlocal called
        del args, kwargs
        called = True
        raise AssertionError("forged derive must not run")

    issuance._derive_policy_evaluation = forged_derive
    try:
        with pytest.raises(
            issuance.ProductPolicyEvaluationIssuanceError,
            match="direct helper graph",
        ):
            original_issue(
                None,
                None,
                source_evaluation_bundle_id="caller-forged",
            )
    finally:
        issuance._derive_policy_evaluation = original_derive

    assert called is False

from __future__ import annotations

import builtins
from types import FunctionType

import pytest

import autosport.betfair_timeout_reconciliation as timeout_authority
import autosport.supervised_execution as supervised_execution
import autosport.supervised_provider_evidence as provider_evidence


def _closure_cell(function: FunctionType, name: str):
    closure = function.__closure__
    assert closure is not None
    freevars = function.__code__.co_freevars
    assert name in freevars
    return closure[freevars.index(name)]


def test_timeout_resolver_rejects_late_builtin_global_shadow_before_dispatch() -> None:
    """A late global must not replace a builtin used by timeout authority code."""

    globals_mapping = timeout_authority.__dict__
    assert "type" not in globals_mapping
    hostile_calls = 0

    def hostile_type(value):
        nonlocal hostile_calls
        hostile_calls += 1
        return builtins.type(value)

    globals_mapping["type"] = hostile_type
    try:
        with pytest.raises(Exception):
            timeout_authority.resolve_betfair_timeout_provider_state(
                object(),
                object(),
                object(),
                attempt_id="attempt-builtin-shadow",
                expected_profile_sha256="0" * 64,
                readback=object(),
            )
    finally:
        del globals_mapping["type"]

    assert hostile_calls == 0, (
        "timeout authority dispatched through a late global shadow of builtin type"
    )


def test_provider_verifier_rejects_late_builtin_global_shadow_before_dispatch() -> None:
    """Provider evidence verification must freeze builtin name resolution too."""

    globals_mapping = provider_evidence.__dict__
    assert "type" not in globals_mapping
    hostile_calls = 0

    def hostile_type(value):
        nonlocal hostile_calls
        hostile_calls += 1
        return builtins.type(value)

    globals_mapping["type"] = hostile_type
    try:
        with pytest.raises(Exception):
            provider_evidence.verify_betfair_provider_state(
                object(),
                object(),
                expected_profile_sha256="0" * 64,
                readback=object(),
            )
    finally:
        del globals_mapping["type"]

    assert hostile_calls == 0, (
        "provider evidence authority dispatched through a late global shadow of "
        "builtin type"
    )


def test_not_found_reconciler_rejects_late_builtin_shadow_before_dispatch() -> None:
    """The final UNKNOWN-to-NOT_FOUND transition must freeze builtin resolution."""

    globals_mapping = supervised_execution.__dict__
    assert "len" not in globals_mapping
    hostile_calls = 0

    def hostile_len(value):
        nonlocal hostile_calls
        hostile_calls += 1
        return builtins.len(value)

    globals_mapping["len"] = hostile_len
    try:
        with pytest.raises(Exception):
            supervised_execution.reconcile_provider_not_found(
                object(),
                object(),
                attempt_id="attempt-not-found-builtin-shadow",
                readback=object(),
            )
    finally:
        del globals_mapping["len"]

    assert hostile_calls == 0, (
        "provider NOT_FOUND reconciliation dispatched through a late global shadow "
        "of builtin len"
    )


def test_provider_name_resolution_guard_rejects_root_closure_retarget() -> None:
    """The final seal itself must not expose a writable root-callable capability."""

    guarded = timeout_authority.resolve_betfair_timeout_provider_state
    assert isinstance(guarded, FunctionType)
    root_cell = _closure_cell(guarded, "root")
    original_root = root_cell.cell_contents
    hostile_calls = 0

    def hostile_root(*args, **kwargs):
        nonlocal hostile_calls
        hostile_calls += 1
        return None

    root_cell.cell_contents = hostile_root
    try:
        with pytest.raises(RuntimeError, match="guard anchor changed"):
            guarded(
                object(),
                object(),
                object(),
                attempt_id="attempt-root-retarget",
                expected_profile_sha256="0" * 64,
                readback=object(),
            )
    finally:
        root_cell.cell_contents = original_root

    assert hostile_calls == 0, "retargeted root callable executed before guard rejection"

from __future__ import annotations

import builtins

import pytest

import autosport.betfair_timeout_reconciliation as timeout_authority
import autosport.supervised_provider_evidence as provider_evidence


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

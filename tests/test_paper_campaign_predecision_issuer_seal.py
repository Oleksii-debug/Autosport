from __future__ import annotations

import pytest

from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime


_ISSUER_METHOD = "_autosport_issue_predecision_learning_observation"


def test_exact_runtime_instance_shadow_cannot_replace_predecision_observation_issuer():
    runtime = object.__new__(PaperExecutionAdoptionRuntime)
    attacker_called = False

    def attacker(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker Observation issuer must never execute")

    # Direct __dict__ mutation bypasses __set__, so this is the decisive form of
    # the old exploit. A data descriptor must still win during normal lookup.
    runtime.__dict__[_ISSUER_METHOD] = attacker

    resolved = getattr(runtime, _ISSUER_METHOD)

    assert resolved is not attacker
    assert getattr(resolved, "__self__", None) is runtime
    assert attacker_called is False


def test_predecision_observation_issuer_rebinding_is_rejected():
    runtime = object.__new__(PaperExecutionAdoptionRuntime)

    with pytest.raises(AttributeError, match="cannot be rebound"):
        setattr(runtime, _ISSUER_METHOD, lambda **_kwargs: None)

from __future__ import annotations

import pytest

from autosport._paper_execution_decision_origin import (
    PaperExecutionDecisionOriginError,
)
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


def test_class_rebinding_cannot_dispatch_attacker_predecision_issuer():
    runtime = object.__new__(PaperExecutionAdoptionRuntime)
    descriptor = PaperExecutionAdoptionRuntime.__dict__[_ISSUER_METHOD]
    attacker_called = False

    def attacker(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker Observation issuer must never execute")

    setattr(PaperExecutionAdoptionRuntime, _ISSUER_METHOD, attacker)
    try:
        with pytest.raises(
            PaperExecutionDecisionOriginError,
            match="issuer class dispatch changed",
        ):
            getattr(runtime, _ISSUER_METHOD)
    finally:
        setattr(PaperExecutionAdoptionRuntime, _ISSUER_METHOD, descriptor)

    assert attacker_called is False


def test_descriptor_executable_rebinding_fails_closed_before_dispatch():
    runtime = object.__new__(PaperExecutionAdoptionRuntime)
    descriptor = PaperExecutionAdoptionRuntime.__dict__[_ISSUER_METHOD]
    canonical_issuer = object.__getattribute__(descriptor, "_issuer")
    attacker_called = False

    def attacker(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker Observation issuer must never execute")

    object.__setattr__(descriptor, "_issuer", attacker)
    try:
        with pytest.raises(
            PaperExecutionDecisionOriginError,
            match="issuer executable seal changed",
        ):
            getattr(runtime, _ISSUER_METHOD)
    finally:
        object.__setattr__(descriptor, "_issuer", canonical_issuer)

    assert attacker_called is False

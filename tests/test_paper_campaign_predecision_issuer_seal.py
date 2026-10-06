from __future__ import annotations

import pytest

from autosport._paper_execution_decision_origin import (
    PaperExecutionDecisionOriginError,
)
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
from autosport.campaign_inception import CampaignInceptionReceipt
from autosport.campaign_provider_cycle_capture import CampaignCompleteBoardCycleReceipt
from autosport.forward_evidence_completeness import ForwardEvidenceProtocolEnvelope


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


def _closure_function(root, name: str):
    seen: set[int] = set()
    pending = [root]
    while pending:
        candidate = pending.pop()
        identity = id(candidate)
        if identity in seen:
            continue
        seen.add(identity)
        if getattr(candidate, "__name__", None) == name:
            return candidate
        for cell in getattr(candidate, "__closure__", ()) or ():
            try:
                value = cell.cell_contents
            except ValueError:
                continue
            if callable(value):
                pending.append(value)
    raise AssertionError(f"closure function {name} not found")


def test_predecision_cycle_receipt_descriptor_rebind_fails_closed_before_getter():
    descriptor = PaperExecutionAdoptionRuntime.__dict__[_ISSUER_METHOD]
    issuer = object.__getattribute__(descriptor, "_issuer")
    reader = _closure_function(issuer, "read_authority_fields")
    receipt = object.__new__(CampaignCompleteBoardCycleReceipt)
    values = {
        "campaign_id": "campaign-a",
        "source_id": "source-a",
        "campaign_receipt_sha256": "a" * 64,
        "receipt_sha256": "b" * 64,
        "provider_captured_at": "2026-10-06T00:00:00+00:00",
    }
    for name, value in values.items():
        object.__setattr__(receipt, name, value)

    captured = tuple(
        (name, CampaignCompleteBoardCycleReceipt.__dict__[name])
        for name in values
    )
    original = CampaignCompleteBoardCycleReceipt.__dict__["receipt_sha256"]
    hostile_called = False

    def hostile_getter(_self):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile cycle receipt getter executed")

    setattr(
        CampaignCompleteBoardCycleReceipt,
        "receipt_sha256",
        property(hostile_getter),
    )
    try:
        with pytest.raises(
            PaperExecutionDecisionOriginError,
            match="campaign cycle receipt descriptor authority changed",
        ):
            reader(
                receipt,
                expected_type=CampaignCompleteBoardCycleReceipt,
                descriptors=captured,
                label="campaign cycle receipt",
            )
    finally:
        setattr(CampaignCompleteBoardCycleReceipt, "receipt_sha256", original)

    assert hostile_called is False


@pytest.mark.parametrize(
    ("authority_type", "values", "field_name", "label"),
    (
        (
            CampaignInceptionReceipt,
            {
                "receipt_sha256": "c" * 64,
                "campaign_id": "campaign-a",
                "source_id": "source-a",
                "evaluation_universe_sha256": "d" * 64,
                "observation_not_before": "2026-10-06T00:00:00+00:00",
                "observation_not_after": "2026-10-06T01:00:00+00:00",
            },
            "receipt_sha256",
            "campaign inception receipt",
        ),
        (
            ForwardEvidenceProtocolEnvelope,
            {
                "campaign_id": "campaign-a",
                "protocol_sha256": "e" * 64,
            },
            "campaign_id",
            "forward protocol",
        ),
    ),
)
def test_predecision_authority_descriptor_rebind_fails_closed_before_getter(
    authority_type,
    values,
    field_name,
    label,
):
    descriptor = PaperExecutionAdoptionRuntime.__dict__[_ISSUER_METHOD]
    issuer = object.__getattribute__(descriptor, "_issuer")
    reader = _closure_function(issuer, "read_authority_fields")
    instance = object.__new__(authority_type)
    for name, value in values.items():
        object.__setattr__(instance, name, value)

    captured = tuple(
        (name, authority_type.__dict__[name])
        for name in values
    )
    original = authority_type.__dict__[field_name]
    hostile_called = False

    def hostile_getter(_self):
        nonlocal hostile_called
        hostile_called = True
        raise AssertionError("hostile authority getter executed")

    setattr(authority_type, field_name, property(hostile_getter))
    try:
        with pytest.raises(
            PaperExecutionDecisionOriginError,
            match=f"{label} descriptor authority changed",
        ):
            reader(
                instance,
                expected_type=authority_type,
                descriptors=captured,
                label=label,
            )
    finally:
        setattr(authority_type, field_name, original)

    assert hostile_called is False

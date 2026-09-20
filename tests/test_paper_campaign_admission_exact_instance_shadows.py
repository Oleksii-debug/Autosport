from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from autosport.paper_campaign_runtime import PaperCampaignRuntime
from autosport.paper_execution_reality import PaperExecutionLedger
from paper_campaign_admission_test_support import AdmissionFixture


def test_exact_decision_ledger_instance_shadow_is_rejected_before_admission(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    decision_ledger = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl")
    attacker_called = False

    def attacker_verified_records():
        nonlocal attacker_called
        attacker_called = True
        return []

    decision_ledger.__dict__["verified_records"] = attacker_verified_records

    with pytest.raises((PaperCampaignAdmissionError, TypeError)):
        coordinator = fixture.coordinator(decision_ledger=decision_ledger)
        fixture.admit(coordinator)

    assert attacker_called is False
    assert not (fixture.workspace / "paper-campaign-admission.json").exists()


def test_alternate_same_workspace_decision_ledger_is_rejected_before_construction(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    alternate = JsonlDecisionLedger(fixture.workspace / "alternate-decisions.jsonl")
    state_path = fixture.workspace / "paper-campaign-admission.json"

    with pytest.raises(PaperCampaignAdmissionError, match="canonical workspace decisions.jsonl"):
        fixture.coordinator(decision_ledger=alternate)

    assert not state_path.exists()


def test_exact_execution_ledger_instance_shadow_is_rejected_before_admission(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    with patch.dict(
        os.environ,
        {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(fixture.authority)},
    ):
        execution_ledger = PaperExecutionLedger(fixture.execution_ledger_path)
    attacker_called = False

    def attacker_events(run_id: str):
        nonlocal attacker_called
        attacker_called = True
        return []

    execution_ledger.__dict__["events"] = attacker_events

    with pytest.raises((PaperCampaignAdmissionError, TypeError)):
        coordinator = fixture.coordinator(execution_ledger=execution_ledger)
        fixture.admit(coordinator)

    assert attacker_called is False
    assert not (fixture.workspace / "paper-campaign-admission.json").exists()


def test_decision_ledger_shadow_added_after_construction_is_rejected_at_read(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    decision_ledger = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl")
    coordinator = fixture.coordinator(decision_ledger=decision_ledger)
    state_path = fixture.workspace / "paper-campaign-admission.json"
    state_before = state_path.read_bytes()
    decisions_before = (fixture.workspace / "decisions.jsonl").read_bytes()
    attacker_called = False

    def attacker_verified_records():
        nonlocal attacker_called
        attacker_called = True
        return []

    decision_ledger.__dict__["verified_records"] = attacker_verified_records

    with pytest.raises(PaperCampaignAdmissionError):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert state_path.read_bytes() == state_before
    assert (fixture.workspace / "decisions.jsonl").read_bytes() == decisions_before


def test_decision_ledger_path_mutation_after_construction_fails_closed(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    decision_ledger = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl")
    coordinator = fixture.coordinator(decision_ledger=decision_ledger)
    state_path = fixture.workspace / "paper-campaign-admission.json"
    state_before = state_path.read_bytes()
    decisions_before = (fixture.workspace / "decisions.jsonl").read_bytes()

    decision_ledger.path = fixture.workspace / "alternate-decisions.jsonl"

    with pytest.raises(PaperCampaignAdmissionError, match="canonical workspace decisions.jsonl"):
        fixture.admit(coordinator)

    assert state_path.read_bytes() == state_before
    assert (fixture.workspace / "decisions.jsonl").read_bytes() == decisions_before


def test_execution_ledger_shadow_added_after_construction_is_rejected_at_read(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    with patch.dict(
        os.environ,
        {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(fixture.authority)},
    ):
        execution_ledger = PaperExecutionLedger(fixture.execution_ledger_path)
    coordinator = fixture.coordinator(execution_ledger=execution_ledger)
    state_path = fixture.workspace / "paper-campaign-admission.json"
    state_before = state_path.read_bytes()
    decisions_before = (fixture.workspace / "decisions.jsonl").read_bytes()
    attacker_called = False

    def attacker_events(run_id: str):
        nonlocal attacker_called
        attacker_called = True
        return []

    execution_ledger.__dict__["events"] = attacker_events

    with pytest.raises(PaperCampaignAdmissionError):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert state_path.read_bytes() == state_before
    assert (fixture.workspace / "decisions.jsonl").read_bytes() == decisions_before


def test_decision_ledger_replacement_after_construction_fails_closed(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    state_path = fixture.workspace / "paper-campaign-admission.json"
    state_before = state_path.read_bytes()
    decisions_before = (fixture.workspace / "decisions.jsonl").read_bytes()
    coordinator.decision_ledger = JsonlDecisionLedger(
        fixture.workspace / "replacement-decisions.jsonl"
    )

    with pytest.raises(PaperCampaignAdmissionError, match="authority changed"):
        fixture.admit(coordinator)

    assert state_path.read_bytes() == state_before
    assert (fixture.workspace / "decisions.jsonl").read_bytes() == decisions_before


def test_execution_ledger_replacement_after_construction_fails_closed(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    state_path = fixture.workspace / "paper-campaign-admission.json"
    state_before = state_path.read_bytes()
    decisions_before = (fixture.workspace / "decisions.jsonl").read_bytes()
    with patch.dict(
        os.environ,
        {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(fixture.authority)},
    ):
        replacement = PaperExecutionLedger(fixture.execution_ledger_path)
    coordinator.execution_ledger = replacement

    with pytest.raises(PaperCampaignAdmissionError, match="authority changed"):
        fixture.admit(coordinator)

    assert state_path.read_bytes() == state_before
    assert (fixture.workspace / "decisions.jsonl").read_bytes() == decisions_before


def test_runtime_subclass_is_rejected_before_admission_coordinator_construction(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    canonical = fixture.coordinator()

    class RuntimeSubtype(PaperCampaignRuntime):
        pass

    runtime = RuntimeSubtype(
        environment=canonical.runtime.environment,
        settlement_bridge=canonical.runtime.settlement_bridge,
    )
    state_path = fixture.workspace / "subtype-paper-campaign-admission.json"

    with pytest.raises(TypeError, match="exact PaperCampaignRuntime"):
        PaperCampaignAdmissionCoordinator(
            state_path,
            paper_book_path=fixture.workspace / "paper_book.json",
            decision_ledger=canonical.decision_ledger,
            runtime=runtime,
            execution_ledger=canonical.execution_ledger,
        )

    assert not state_path.exists()


def test_runtime_replacement_after_construction_fails_before_admission_mutation(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    state_path = fixture.workspace / "paper-campaign-admission.json"
    state_before = state_path.read_bytes()
    decisions_before = (fixture.workspace / "decisions.jsonl").read_bytes()

    replacement = PaperCampaignRuntime(
        environment=coordinator.runtime.environment,
        settlement_bridge=coordinator.runtime.settlement_bridge,
    )
    coordinator.runtime = replacement

    with pytest.raises(PaperCampaignAdmissionError, match="runtime authority changed"):
        fixture.admit(coordinator)

    assert state_path.read_bytes() == state_before
    assert (fixture.workspace / "decisions.jsonl").read_bytes() == decisions_before


def test_settlement_bridge_read_shadow_after_construction_is_never_invoked(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    state_path = fixture.workspace / "paper-campaign-admission.json"
    state_before = state_path.read_bytes()
    decisions_before = (fixture.workspace / "decisions.jsonl").read_bytes()
    attacker_called = False

    def attacker_read():
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker settlement bridge callback must never run")

    coordinator.runtime.settlement_bridge.__dict__["_read"] = attacker_read

    with pytest.raises(
        PaperCampaignAdmissionError,
        match="settlement-learning bridge authority method is shadowed",
    ):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert state_path.read_bytes() == state_before
    assert (fixture.workspace / "decisions.jsonl").read_bytes() == decisions_before


def test_runtime_environment_replacement_fails_before_admission_mutation(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    state_path = fixture.workspace / "paper-campaign-admission.json"
    state_before = state_path.read_bytes()
    decisions_before = (fixture.workspace / "decisions.jsonl").read_bytes()

    coordinator.runtime.environment = object()

    with pytest.raises(PaperCampaignAdmissionError, match="environment authority changed"):
        fixture.admit(coordinator)

    assert state_path.read_bytes() == state_before
    assert (fixture.workspace / "decisions.jsonl").read_bytes() == decisions_before


def test_bridge_agent_loop_replacement_fails_before_admission_mutation(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    state_path = fixture.workspace / "paper-campaign-admission.json"
    state_before = state_path.read_bytes()
    decisions_before = (fixture.workspace / "decisions.jsonl").read_bytes()

    coordinator.runtime.settlement_bridge.agent_loop = object()

    with pytest.raises(PaperCampaignAdmissionError, match="AgentLoop authority changed"):
        fixture.admit(coordinator)

    assert state_path.read_bytes() == state_before
    assert (fixture.workspace / "decisions.jsonl").read_bytes() == decisions_before


def test_runtime_method_shadow_after_construction_is_never_invoked(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    state_path = fixture.workspace / "paper-campaign-admission.json"
    state_before = state_path.read_bytes()
    decisions_before = (fixture.workspace / "decisions.jsonl").read_bytes()
    attacker_called = False

    def attacker_begin_and_bind_paper_ticket(**_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("attacker runtime callback must never run")

    coordinator.runtime.__dict__["begin_and_bind_paper_ticket"] = (
        attacker_begin_and_bind_paper_ticket
    )

    with pytest.raises(PaperCampaignAdmissionError, match="runtime authority method is shadowed"):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert state_path.read_bytes() == state_before
    assert (fixture.workspace / "decisions.jsonl").read_bytes() == decisions_before

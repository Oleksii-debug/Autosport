from __future__ import annotations

import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import autosport._paper_campaign_admission_consumer_guard as admission_guard
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


def test_canonical_concrete_decision_ledger_path_is_accepted(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    decision_ledger = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl")

    coordinator = fixture.coordinator(decision_ledger=decision_ledger)

    assert coordinator.decision_ledger is decision_ledger
    assert type(decision_ledger.path) is type(Path())


def test_decision_ledger_path_subclass_is_rejected_before_construction(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    decision_ledger = JsonlDecisionLedger(fixture.workspace / "decisions.jsonl")
    canonical_path_type = type(Path())

    class ForgedPath(canonical_path_type):
        pass

    decision_ledger.path = ForgedPath(fixture.workspace / "decisions.jsonl")

    with pytest.raises(PaperCampaignAdmissionError, match="canonical workspace decisions.jsonl"):
        fixture.coordinator(decision_ledger=decision_ledger)

    assert not (fixture.workspace / "paper-campaign-admission.json").exists()


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


def _four_state_bytes(fixture: AdmissionFixture):
    return (
        (fixture.workspace / "paper-campaign-admission.json").read_bytes(),
        (fixture.workspace / "decisions.jsonl").read_bytes(),
        (fixture.workspace / "agent-loop.json").read_bytes(),
        (fixture.workspace / "paper_book.json").read_bytes(),
    )


def test_decision_class_and_pinned_mirror_cosubstitution_never_executes_attacker(
    tmp_path, monkeypatch
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = _four_state_bytes(fixture)
    attacker_called = False

    def attacker_verified_records(_self):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("forged DecisionLedger callback must never execute")

    monkeypatch.setattr(
        admission_guard,
        "_PINNED_DECISION_VERIFIED_RECORDS",
        attacker_verified_records,
    )
    monkeypatch.setattr(JsonlDecisionLedger, "verified_records", attacker_verified_records)

    with pytest.raises(PaperCampaignAdmissionError, match="class method changed"):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert _four_state_bytes(fixture) == before


def test_execution_class_and_pinned_mirror_cosubstitution_never_executes_attacker(
    tmp_path, monkeypatch
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = _four_state_bytes(fixture)
    attacker_called = False

    def attacker_events(_self, _run_id: str):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("forged execution-ledger callback must never execute")

    monkeypatch.setattr(admission_guard, "_PINNED_EXECUTION_EVENTS", attacker_events)
    monkeypatch.setattr(PaperExecutionLedger, "events", attacker_events)

    with pytest.raises(PaperCampaignAdmissionError, match="class method changed"):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert _four_state_bytes(fixture) == before


def test_original_resolved_decision_mirror_substitution_is_not_authority(
    tmp_path, monkeypatch
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = _four_state_bytes(fixture)
    attacker_called = False

    def attacker_resolver(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutable resolver mirror must never execute")

    monkeypatch.setattr(
        admission_guard,
        "_ORIGINAL_RESOLVED_EXECUTION_DECISION_ID",
        attacker_resolver,
    )

    with pytest.raises(PaperCampaignAdmissionError):
        coordinator._resolved_execution_decision_id(
            run_id="missing-run",
            reservation={"trigger_id": "missing-decision"},
            origin={
                "decision_id": "missing-decision",
                "decision_record_ordinal": 999999,
                "decision_record_sha256": "0" * 64,
            },
        )

    assert attacker_called is False
    assert _four_state_bytes(fixture) == before


def test_original_execution_attempt_mirror_substitution_is_not_authority(
    tmp_path, monkeypatch
):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = _four_state_bytes(fixture)
    attacker_called = False

    def attacker_attempt(*_args, **_kwargs):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("mutable execution-attempt mirror must never execute")

    monkeypatch.setattr(
        admission_guard,
        "_ORIGINAL_EXECUTION_ATTEMPT",
        attacker_attempt,
    )

    with pytest.raises(PaperCampaignAdmissionError):
        coordinator._execution_attempt(
            run_id="missing-run",
            attempt_id="missing-attempt",
        )

    assert attacker_called is False
    assert _four_state_bytes(fixture) == before


def test_reload_repairs_mutable_mirrors_from_installed_closure_seal(tmp_path, monkeypatch):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    before = _four_state_bytes(fixture)

    def attacker_verified_records(_self):
        raise AssertionError("reloaded mirror must not retain attacker delegate")

    def attacker_events(_self, _run_id: str):
        raise AssertionError("reloaded mirror must not retain attacker delegate")

    monkeypatch.setattr(
        admission_guard,
        "_PINNED_DECISION_VERIFIED_RECORDS",
        attacker_verified_records,
    )
    monkeypatch.setattr(admission_guard, "_PINNED_EXECUTION_EVENTS", attacker_events)
    monkeypatch.setattr(
        admission_guard,
        "_ORIGINAL_RESOLVED_EXECUTION_DECISION_ID",
        attacker_verified_records,
    )
    monkeypatch.setattr(
        admission_guard,
        "_ORIGINAL_EXECUTION_ATTEMPT",
        attacker_events,
    )

    reloaded = importlib.reload(admission_guard)

    assert reloaded._PINNED_DECISION_VERIFIED_RECORDS is not attacker_verified_records
    assert reloaded._PINNED_EXECUTION_EVENTS is not attacker_events
    assert reloaded._ORIGINAL_RESOLVED_EXECUTION_DECISION_ID is not attacker_verified_records
    assert reloaded._ORIGINAL_EXECUTION_ATTEMPT is not attacker_events

    with pytest.raises(PaperCampaignAdmissionError):
        coordinator._execution_attempt(
            run_id="missing-run",
            attempt_id="missing-attempt",
        )
    assert _four_state_bytes(fixture) == before

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper_campaign_admission import PaperCampaignAdmissionError
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
    attacker_called = False

    def attacker_verified_records():
        nonlocal attacker_called
        attacker_called = True
        return []

    decision_ledger.__dict__["verified_records"] = attacker_verified_records

    with pytest.raises(PaperCampaignAdmissionError):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert not (fixture.workspace / "paper-campaign-admission.json").exists()


def test_execution_ledger_shadow_added_after_construction_is_rejected_at_read(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    with patch.dict(
        os.environ,
        {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(fixture.authority)},
    ):
        execution_ledger = PaperExecutionLedger(fixture.execution_ledger_path)
    coordinator = fixture.coordinator(execution_ledger=execution_ledger)
    attacker_called = False

    def attacker_events(run_id: str):
        nonlocal attacker_called
        attacker_called = True
        return []

    execution_ledger.__dict__["events"] = attacker_events

    with pytest.raises(PaperCampaignAdmissionError):
        fixture.admit(coordinator)

    assert attacker_called is False
    assert not (fixture.workspace / "paper-campaign-admission.json").exists()


def test_decision_ledger_replacement_after_construction_fails_closed(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    coordinator.decision_ledger = JsonlDecisionLedger(
        fixture.workspace / "replacement-decisions.jsonl"
    )

    with pytest.raises(PaperCampaignAdmissionError, match="authority changed"):
        fixture.admit(coordinator)

    assert not (fixture.workspace / "paper-campaign-admission.json").exists()


def test_execution_ledger_replacement_after_construction_fails_closed(tmp_path):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    with patch.dict(
        os.environ,
        {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(fixture.authority)},
    ):
        replacement = PaperExecutionLedger(fixture.execution_ledger_path)
    coordinator.execution_ledger = replacement

    with pytest.raises(PaperCampaignAdmissionError, match="authority changed"):
        fixture.admit(coordinator)

    assert not (fixture.workspace / "paper-campaign-admission.json").exists()

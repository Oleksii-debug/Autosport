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

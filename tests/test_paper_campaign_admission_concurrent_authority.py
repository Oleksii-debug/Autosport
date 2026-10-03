from __future__ import annotations

import os
from threading import Event, Thread
from unittest.mock import patch

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.paper_campaign_admission import PaperCampaignAdmissionError
from autosport.paper_execution_reality import PaperExecutionLedger
from paper_campaign_admission_test_support import AdmissionFixture


def _run_admission_in_thread(fixture, coordinator):
    errors: list[BaseException] = []

    def target() -> None:
        try:
            fixture.admit(coordinator)
        except BaseException as exc:  # the test must retain the exact worker failure
            errors.append(exc)

    thread = Thread(target=target, daemon=True)
    thread.start()
    return thread, errors


def test_racing_decision_ledger_replacement_cannot_redirect_authority(tmp_path, monkeypatch):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    state_path = fixture.workspace / "paper-campaign-admission.json"
    decision_path = fixture.workspace / "decisions.jsonl"
    state_before = state_path.read_bytes()
    decisions_before = decision_path.read_bytes()
    read_entered = Event()
    allow_read = Event()
    attacker_called = False

    original_verified_records = JsonlDecisionLedger.verified_records

    def blocked_verified_records(self):
        read_entered.set()
        assert allow_read.wait(5), "canonical decision read did not receive release"
        return original_verified_records(self)

    monkeypatch.setattr(JsonlDecisionLedger, "verified_records", blocked_verified_records)
    thread, errors = _run_admission_in_thread(fixture, coordinator)
    assert read_entered.wait(5), "admission did not reach the pinned decision read"

    replacement = JsonlDecisionLedger(fixture.workspace / "replacement-decisions.jsonl")

    def attacker_verified_records():
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("replacement Decision Ledger callback must never run")

    replacement.__dict__["verified_records"] = attacker_verified_records
    coordinator.decision_ledger = replacement
    allow_read.set()
    thread.join(5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PaperCampaignAdmissionError)
    assert "Decision Ledger authority changed" in str(errors[0])
    assert attacker_called is False
    assert state_path.read_bytes() == state_before
    assert decision_path.read_bytes() == decisions_before


def test_racing_execution_ledger_replacement_cannot_redirect_authority(tmp_path, monkeypatch):
    fixture = AdmissionFixture(tmp_path)
    coordinator = fixture.coordinator()
    state_path = fixture.workspace / "paper-campaign-admission.json"
    decision_path = fixture.workspace / "decisions.jsonl"
    state_before = state_path.read_bytes()
    decisions_before = decision_path.read_bytes()
    read_entered = Event()
    allow_read = Event()
    attacker_called = False

    original_events = PaperExecutionLedger.events

    def blocked_events(self, run_id: str):
        read_entered.set()
        assert allow_read.wait(5), "canonical execution read did not receive release"
        return original_events(self, run_id)

    monkeypatch.setattr(PaperExecutionLedger, "events", blocked_events)
    thread, errors = _run_admission_in_thread(fixture, coordinator)
    assert read_entered.wait(5), "admission did not reach the pinned execution read"

    with patch.dict(
        os.environ,
        {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(fixture.authority)},
    ):
        replacement = PaperExecutionLedger(fixture.execution_ledger_path)

    def attacker_events(run_id: str):
        nonlocal attacker_called
        attacker_called = True
        raise AssertionError("replacement execution Ledger callback must never run")

    replacement.__dict__["events"] = attacker_events
    coordinator.execution_ledger = replacement
    allow_read.set()
    thread.join(5)

    assert not thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], PaperCampaignAdmissionError)
    assert "PAPER execution authority changed" in str(errors[0])
    assert attacker_called is False
    assert state_path.read_bytes() == state_before
    assert decision_path.read_bytes() == decisions_before
